/**
 * Approvals tab — pending escalations awaiting user decision,
 * plus resolved approvals history with pagination.
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
    expandedArgs: null, // call_id of item with expanded args

    // Resolved section state
    expandedResolvedId: null,   // call_id of expanded resolved item
    expandedResolvedRecord: null,
    expandedResolvedArgs: null, // call_id of resolved item with expanded args
    resolved: [],
    resolvedTotal: 0,
    resolvedPage: 1,
    resolvedPages: 1,
    resolvedLoading: false,

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
          this.loadResolved();
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
          this.loadResolved();
        }
      });
      window.addEventListener('intaris:agent-changed', () => {
        if (this.initialized && this._tabActive) {
          this.load();
          this.loadResolved();
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
          // Refresh resolved list to show the newly resolved item
          this.loadResolved();
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
        const params = {
          decision: 'escalate',
          resolved: false,
          limit: 50,
        };
        const agentFilter = Alpine.store('nav').selectedAgent;
        if (agentFilter) params.agent_id = agentFilter;
        const result = await IntarisAPI.listAudit(params);
        this.pending = this._filterResolved(result.items || []);
        this.total = this.pending.length;
        // Sync with global nav store for badge
        Alpine.store('nav').pendingApprovals = result.total || this.total;
      } catch (e) {
        Alpine.store('notify').error('Failed to load approvals: ' + e.message);
      } finally {
        this.loading = false;
      }
    },

    async loadResolved() {
      this.resolvedLoading = true;
      try {
        const params = {
          decision: 'escalate',
          resolved: true,
          page: this.resolvedPage,
          limit: 20,
        };
        const agentFilter = Alpine.store('nav').selectedAgent;
        if (agentFilter) params.agent_id = agentFilter;
        const result = await IntarisAPI.listAudit(params);
        this.resolved = result.items || [];
        this.resolvedTotal = result.total;
        this.resolvedPages = result.pages;
      } catch (e) {
        Alpine.store('notify').error('Failed to load resolved approvals: ' + e.message);
      } finally {
        this.resolvedLoading = false;
      }
    },

    prevResolvedPage() {
      if (this.resolvedPage > 1) {
        this.resolvedPage--;
        this.loadResolved();
      }
    },

    nextResolvedPage() {
      if (this.resolvedPage < this.resolvedPages) {
        this.resolvedPage++;
        this.loadResolved();
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

    /**
     * Shared resolution logic for both initial resolution and judge override.
     * @param {string} callId - The call ID to resolve.
     * @param {string} decision - "approve" or "deny".
     * @param {boolean} isOverride - Whether this overrides a judge decision.
     */
    async _resolveOrOverride(callId, decision, isOverride) {
      if (!callId || !decision) return;
      if (this.resolving[callId]) return;
      this.resolving = { ...this.resolving, [callId]: true };
      try {
        const note = this.noteText[callId] || null;
        await IntarisAPI.resolveDecision(callId, decision, note);
        if (isOverride) {
          Alpine.store('notify').success(
            `Judge decision overridden to ${decision}`
          );
        } else {
          Alpine.store('notify').success(
            `Call ${decision === 'approve' ? 'approved' : 'denied'}`
          );
        }
        if (!isOverride) {
          // Optimistic removal + mark as recently resolved
          this._markResolved(callId);
          this.pending = this.pending.filter(p => p.call_id !== callId);
          this.total = this.pending.length;
        }
        delete this.noteText[callId];
        // Refresh resolved list to show the updated item
        this.loadResolved();
      } catch (e) {
        const action = isOverride ? 'override' : 'resolve';
        console.error(`[intaris] ${action} failed:`, e);
        Alpine.store('notify').error(`Failed to ${action}: ` + (e.message || String(e)));
      } finally {
        const { [callId]: _, ...rest } = this.resolving;
        this.resolving = rest;
      }
    },

    async resolve(callId, decision) {
      return this._resolveOrOverride(callId, decision, false);
    },

    async override(callId, decision) {
      return this._resolveOrOverride(callId, decision, true);
    },

    // ── Navigation ────────────────────────────────────────────

    goToSession(sessionId) {
      Alpine.store('nav').openSessionModal(sessionId);
    },

    // ── Resolved item expand ─────────────────────────────────

    async toggleResolvedExpand(item) {
      if (this.expandedResolvedId === item.call_id) {
        this.expandedResolvedId = null;
        this.expandedResolvedRecord = null;
        this.expandedResolvedArgs = null;
        return;
      }
      this.expandedResolvedId = item.call_id;
      this.expandedResolvedArgs = null;
      try {
        this.expandedResolvedRecord = await IntarisAPI.getAuditRecord(item.call_id);
      } catch (e) {
        this.expandedResolvedRecord = item;
      }
    },

    // ── Helpers ──────────────────────────────────────────────

    decisionBadgeClass(decision) {
      return 'badge badge-' + (decision || 'low');
    },

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
      return JSON.stringify(args, null, 2);
    },

    truncateArgs(args) {
      if (!args) return '';
      if (typeof args === 'string') {
        return args.length > 200 ? args.substring(0, 200) + '...' : args;
      }
      // Use compact JSON for preview so more data fits in the truncation window
      const str = JSON.stringify(args);
      return str.length > 200 ? str.substring(0, 200) + '...' : str;
    },

    truncate(str, len) {
      if (!str) return '';
      return str.length > len ? str.substring(0, len) + '...' : str;
    },
  };
}
