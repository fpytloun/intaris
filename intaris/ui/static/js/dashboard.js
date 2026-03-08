/**
 * Dashboard tab — overview stats and recent activity.
 *
 * Subscribes to WebSocket events for live counter updates.
 */
function dashboardTab() {
  return {
    initialized: false,
    loading: false,
    stats: null,
    recentActivity: [],

    _refreshTimer: null,

    init() {
      window.addEventListener('intaris:tab-changed', (e) => {
        if (e.detail.tab === 'dashboard') {
          this.load();
          this._startPeriodicRefresh();
        } else {
          this._stopPeriodicRefresh();
        }
      });
      window.addEventListener('intaris:user-changed', () => {
        if (this.initialized) this.load();
      });
      window.addEventListener('intaris:logout', () => {
        this._stopPeriodicRefresh();
      });

      // Subscribe to WebSocket events for live updates
      window.addEventListener('intaris:ws-message', (e) => {
        this._handleWsEvent(e.detail);
      });

      // Auto-load on first render + start periodic refresh
      this.load();
      this._startPeriodicRefresh();
    },

    _startPeriodicRefresh() {
      this._stopPeriodicRefresh();
      this._refreshTimer = setInterval(() => {
        if (Alpine.store('nav').activeTab === 'dashboard') this.load();
      }, 60000);
    },

    _stopPeriodicRefresh() {
      if (this._refreshTimer) {
        clearInterval(this._refreshTimer);
        this._refreshTimer = null;
      }
    },

    _handleWsEvent(data) {
      if (!this.stats) return;

      if (data.type === 'evaluated') {
        // Increment total evaluations
        this.stats.total_evaluations = (this.stats.total_evaluations || 0) + 1;

        // Update decision distribution
        if (!this.stats.decisions) this.stats.decisions = {};
        const d = data.decision;
        this.stats.decisions[d] = (this.stats.decisions[d] || 0) + 1;

        // Update pending approvals count
        if (d === 'escalate') {
          this.stats.pending_approvals = (this.stats.pending_approvals || 0) + 1;
        }

        // Recalculate approval rate
        const total = this.stats.total_evaluations || 1;
        const approved = this.stats.decisions.approve || 0;
        this.stats.approval_rate = Math.round((approved / total) * 100);

        // Prepend to recent activity (keep last 10).
        // Deduplicate by call_id to prevent Alpine x-for duplicate key crashes
        // when a WebSocket event arrives for a record already loaded via REST.
        const callId = data.call_id;
        if (callId) {
          this.recentActivity = [
            {
              call_id: callId,
              decision: data.decision,
              tool: data.tool,
              record_type: data.record_type || 'tool_call',
              risk: data.risk,
              session_id: data.session_id,
              timestamp: data.timestamp || new Date().toISOString(),
              evaluation_path: data.path,
              latency_ms: data.latency_ms,
            },
            ...this.recentActivity.filter(r => r.call_id !== callId),
          ].slice(0, 10);
        }
      }

      if (data.type === 'decided') {
        // Decrement pending approvals
        if (this.stats.pending_approvals > 0) {
          this.stats.pending_approvals--;
        }
      }

      if (data.type === 'session_created') {
        this.stats.total_sessions = (this.stats.total_sessions || 0) + 1;
        if (!this.stats.sessions_by_status) this.stats.sessions_by_status = {};
        this.stats.sessions_by_status.active = (this.stats.sessions_by_status.active || 0) + 1;
      }

      if (data.type === 'session_status_changed') {
        // We don't track previous status, so just do a full reload periodically
        // For now, just note the change — a full reload is more accurate
      }
    },

    async load() {
      this.loading = true;
      try {
        const [stats, audit] = await Promise.all([
          IntarisAPI.stats(),
          IntarisAPI.listAudit({ limit: 10 }),
        ]);
        this.stats = stats;
        this.recentActivity = audit.items || [];
        this.initialized = true;
      } catch (e) {
        Alpine.store('notify').error('Failed to load dashboard: ' + e.message);
      } finally {
        this.loading = false;
      }
    },

    get approvalRate() {
      if (!this.stats) return '0%';
      return this.stats.approval_rate + '%';
    },

    get avgLatency() {
      if (!this.stats) return '0ms';
      return Math.round(this.stats.avg_latency_ms) + 'ms';
    },

    decisionBadgeClass(decision) {
      return 'badge badge-' + (decision || 'low');
    },

    riskBadgeClass(risk) {
      return 'badge badge-' + (risk || 'low');
    },

    pathBadgeClass(path) {
      if (path === 'critical') return 'badge badge-deny';
      return 'badge badge-' + (path || 'fast');
    },

    formatTime(ts) {
      if (!ts) return '';
      const d = new Date(ts);
      return d.toLocaleString();
    },

    truncate(str, len) {
      if (!str) return '';
      return str.length > len ? str.substring(0, len) + '...' : str;
    },

    // ── Navigation ────────────────────────────────────────────

    goToSession(sessionId) {
      Alpine.store('nav').setTab('sessions');
      window.dispatchEvent(new CustomEvent('intaris:navigate-session', {
        detail: { sessionId },
      }));
    },
  };
}
