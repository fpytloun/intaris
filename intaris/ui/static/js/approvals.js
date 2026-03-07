/**
 * Approvals tab — pending escalations awaiting user decision.
 *
 * Uses the shared WebSocket store ($store.ws) for real-time updates
 * with 10s polling fallback when WebSocket is disconnected.
 */

// ── Configuration constants ──────────────────────────────────
const WS_LOAD_DEBOUNCE_MS = 300;
const RECENTLY_RESOLVED_TTL_MS = 10000;
const POLL_INTERVAL_MS = 10000;

function approvalsTab() {
  return {
    initialized: false,
    loading: false,
    pending: [],
    total: 0,
    resolving: {},      // call_id -> true while resolving
    noteText: {},       // call_id -> note text

    // Polling fallback state
    pollTimer: null,
    _loadDebounce: null,
    recentlyResolved: new Map(),  // call_id -> timestamp
    _tabActive: false,

    init() {
      window.addEventListener('intaris:tab-changed', (e) => {
        if (e.detail.tab === 'approvals') {
          this._tabActive = true;
          if (!this.initialized) {
            this.initialized = true;
          }
          this.load();
          // Start polling fallback if WebSocket is not connected
          if (!Alpine.store('ws').connected) {
            this.startPolling();
          }
        } else {
          this._tabActive = false;
          this.stopPolling();
          clearTimeout(this._loadDebounce);
        }
      });
      window.addEventListener('intaris:user-changed', () => {
        if (this.initialized && this._tabActive) {
          this.load();
        }
      });
      window.addEventListener('intaris:logout', () => {
        this.stopPolling();
        clearTimeout(this._loadDebounce);
      });

      // Subscribe to shared WebSocket events
      window.addEventListener('intaris:ws-message', (e) => {
        if (this._tabActive) {
          this._handleWsMessage(e.detail);
        }
      });
    },

    // ── WebSocket message handling ───────────────────────────

    get wsConnected() {
      return Alpine.store('ws').connected;
    },

    _handleWsMessage(data) {
      if (data.type === 'evaluated' && data.decision === 'escalate') {
        this._scheduleLoad();
      } else if (data.type === 'decided') {
        // Another user (or this user via resolve()) resolved an item
        const callId = data.call_id;
        if (callId) {
          this._markResolved(callId);
          this.pending = this.pending.filter(p => p.call_id !== callId);
          this.total = this.pending.length;
        }
      }
    },

    // ── Fallback polling ─────────────────────────────────────

    startPolling() {
      this.stopPolling();
      this.pollTimer = setInterval(() => this.load(), POLL_INTERVAL_MS);
    },

    stopPolling() {
      if (this.pollTimer) {
        clearInterval(this.pollTimer);
        this.pollTimer = null;
      }
    },

    // ── Data loading ─────────────────────────────────────────

    /**
     * Debounced load — coalesces rapid WebSocket "evaluated" events
     * into a single REST call.
     */
    _scheduleLoad() {
      clearTimeout(this._loadDebounce);
      this._loadDebounce = setTimeout(() => this.load(), WS_LOAD_DEBOUNCE_MS);
    },

    async load() {
      this.loading = !this.initialized;
      try {
        const result = await IntarisAPI.listAudit({
          decision: 'escalate',
          resolved: false,
          limit: 50,
        });
        this.pending = this._filterResolved(result.items || []);
        this.total = this.pending.length;
      } catch (e) {
        Alpine.store('notify').error('Failed to load approvals: ' + e.message);
      } finally {
        this.loading = false;
      }
    },

    // ── Recently resolved tracking ───────────────────────────

    _markResolved(callId) {
      this.recentlyResolved.set(callId, Date.now());
    },

    _filterResolved(items) {
      const now = Date.now();
      // Clean expired entries
      for (const [id, ts] of this.recentlyResolved) {
        if (now - ts > RECENTLY_RESOLVED_TTL_MS) {
          this.recentlyResolved.delete(id);
        }
      }
      return items.filter(item => !this.recentlyResolved.has(item.call_id));
    },

    // ── User actions ─────────────────────────────────────────

    async resolve(callId, decision) {
      if (this.resolving[callId]) return;
      this.resolving[callId] = true;
      try {
        const note = this.noteText[callId] || null;
        await IntarisAPI.resolveDecision(callId, decision, note);
        Alpine.store('notify').success(
          `Call ${decision === 'approve' ? 'approved' : 'denied'}`
        );
        // Optimistic removal + mark as recently resolved
        this._markResolved(callId);
        this.pending = this.pending.filter(p => p.call_id !== callId);
        this.total = this.pending.length;
        delete this.noteText[callId];
      } catch (e) {
        Alpine.store('notify').error('Failed to resolve: ' + e.message);
      } finally {
        delete this.resolving[callId];
      }
    },

    // ── Helpers ──────────────────────────────────────────────

    riskBadgeClass(risk) {
      return 'badge badge-' + (risk || 'low');
    },

    formatTime(ts) {
      if (!ts) return '';
      return new Date(ts).toLocaleString();
    },

    formatArgs(args) {
      if (!args) return '';
      if (typeof args === 'string') return args;
      const str = JSON.stringify(args);
      return str.length > 200 ? str.substring(0, 200) + '...' : str;
    },

    truncate(str, len) {
      if (!str) return '';
      return str.length > len ? str.substring(0, len) + '...' : str;
    },
  };
}
