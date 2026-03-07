/**
 * Approvals tab — pending escalations awaiting user decision.
 * Uses polling (10s interval) instead of WebSocket for simplicity.
 */
function approvalsTab() {
  return {
    initialized: false,
    loading: false,
    pending: [],
    total: 0,
    pollTimer: null,
    resolving: {},  // call_id -> true while resolving
    noteText: {},   // call_id -> note text

    init() {
      window.addEventListener('intaris:tab-changed', (e) => {
        if (e.detail.tab === 'approvals') {
          if (!this.initialized) {
            this.initialized = true;
          }
          this.load();
          this.startPolling();
        } else {
          this.stopPolling();
        }
      });
      window.addEventListener('intaris:user-changed', () => {
        if (this.initialized) this.load();
      });
    },

    startPolling() {
      this.stopPolling();
      this.pollTimer = setInterval(() => this.load(), 10000);
    },

    stopPolling() {
      if (this.pollTimer) {
        clearInterval(this.pollTimer);
        this.pollTimer = null;
      }
    },

    async load() {
      this.loading = !this.initialized;
      try {
        const result = await IntarisAPI.listAudit({
          decision: 'escalate',
          resolved: false,
          limit: 50,
        });
        this.pending = result.items || [];
        this.total = result.total;
      } catch (e) {
        Alpine.store('notify').error('Failed to load approvals: ' + e.message);
      } finally {
        this.loading = false;
      }
    },

    async resolve(callId, decision) {
      if (this.resolving[callId]) return;
      this.resolving[callId] = true;
      try {
        const note = this.noteText[callId] || null;
        await IntarisAPI.resolveDecision(callId, decision, note);
        Alpine.store('notify').success(
          `Call ${decision === 'approve' ? 'approved' : 'denied'}`
        );
        // Remove from list immediately
        this.pending = this.pending.filter(p => p.call_id !== callId);
        this.total = Math.max(0, this.total - 1);
        delete this.noteText[callId];
      } catch (e) {
        Alpine.store('notify').error('Failed to resolve: ' + e.message);
      } finally {
        delete this.resolving[callId];
      }
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
      // Show a compact summary
      const str = JSON.stringify(args);
      return str.length > 200 ? str.substring(0, 200) + '...' : str;
    },

    truncate(str, len) {
      if (!str) return '';
      return str.length > len ? str.substring(0, len) + '...' : str;
    },
  };
}
