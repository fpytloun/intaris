/**
 * Approvals tab — pending escalations awaiting user decision.
 *
 * Uses WebSocket for real-time updates with 10s polling fallback.
 * Connects to /api/v1/stream and listens for "evaluated" (new
 * escalations) and "decided" (resolved items) events.
 */

// ── Configuration constants ──────────────────────────────────
const WS_LOAD_DEBOUNCE_MS = 300;
const RECENTLY_RESOLVED_TTL_MS = 10000;
const WS_RETRY_INTERVAL_MS = 60000;
const WS_MAX_RECONNECT_DELAY_MS = 30000;
const WS_INITIAL_RECONNECT_DELAY_MS = 1000;
const WS_MAX_RECONNECT_ATTEMPTS = 5;
const POLL_INTERVAL_MS = 10000;

function approvalsTab() {
  return {
    initialized: false,
    loading: false,
    pending: [],
    total: 0,
    resolving: {},      // call_id -> true while resolving
    noteText: {},       // call_id -> note text

    // WebSocket state
    ws: null,
    wsConnected: false,
    reconnectTimer: null,
    reconnectDelay: WS_INITIAL_RECONNECT_DELAY_MS,
    reconnectAttempts: 0,
    fallbackPolling: false,
    pollTimer: null,
    _loadDebounce: null,
    _wsRetryTimer: null,
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
          this.connectWs();
        } else {
          this._tabActive = false;
          this.disconnectWs();
          this.stopPolling();
          clearTimeout(this._loadDebounce);
        }
      });
      window.addEventListener('intaris:user-changed', () => {
        if (this.initialized && this._tabActive) {
          this.disconnectWs();
          this.load();
          this.connectWs();
        }
      });
      window.addEventListener('intaris:logout', () => {
        this.disconnectWs();
        this.stopPolling();
        clearTimeout(this._loadDebounce);
      });
    },

    // ── WebSocket lifecycle ──────────────────────────────────

    connectWs() {
      // Don't connect if already connected or tab not active
      if (this.ws || !this._tabActive) return;

      this.ws = IntarisAPI.connectWebSocket({
        onOpen: () => {
          this.wsConnected = true;
          this.reconnectAttempts = 0;
          this.reconnectDelay = WS_INITIAL_RECONNECT_DELAY_MS;
          // If we were in fallback polling, stop it
          if (this.fallbackPolling) {
            this.fallbackPolling = false;
            this.stopPolling();
            clearInterval(this._wsRetryTimer);
            this._wsRetryTimer = null;
          }
        },
        onMessage: (data) => this._handleWsMessage(data),
        onClose: () => {
          this.wsConnected = false;
          this.ws = null;
          if (this._tabActive) {
            this._scheduleReconnect();
          }
        },
        onError: () => {
          // onclose will also fire after onerror, so reconnect
          // is handled there. Just mark as disconnected.
          this.wsConnected = false;
        },
      });
    },

    disconnectWs() {
      if (this.ws) {
        this.ws.onclose = null;  // Prevent reconnect on intentional close
        this.ws.close();
        this.ws = null;
      }
      this.wsConnected = false;
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
      clearInterval(this._wsRetryTimer);
      this._wsRetryTimer = null;
      this.reconnectAttempts = 0;
      this.reconnectDelay = WS_INITIAL_RECONNECT_DELAY_MS;
      this.fallbackPolling = false;
      this.recentlyResolved.clear();
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

    _scheduleReconnect() {
      if (this.fallbackPolling) return;  // Already in fallback mode

      this.reconnectAttempts++;
      if (this.reconnectAttempts >= WS_MAX_RECONNECT_ATTEMPTS) {
        this._startFallbackPolling();
        return;
      }

      this.reconnectTimer = setTimeout(() => {
        this.reconnectTimer = null;
        this.connectWs();
      }, this.reconnectDelay);

      // Exponential backoff
      this.reconnectDelay = Math.min(
        this.reconnectDelay * 2,
        WS_MAX_RECONNECT_DELAY_MS
      );
    },

    // ── Fallback polling ─────────────────────────────────────

    _startFallbackPolling() {
      this.fallbackPolling = true;
      this.startPolling();
      // Periodically retry WebSocket connection
      this._wsRetryTimer = setInterval(() => {
        if (!this.ws && this._tabActive) {
          this.reconnectAttempts = 0;  // Reset for the retry attempt
          this.connectWs();
        }
      }, WS_RETRY_INTERVAL_MS);
    },

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
