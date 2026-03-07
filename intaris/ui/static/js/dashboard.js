/**
 * Dashboard tab — overview stats and recent activity.
 */
function dashboardTab() {
  return {
    initialized: false,
    loading: false,
    stats: null,
    recentActivity: [],

    init() {
      window.addEventListener('intaris:tab-changed', (e) => {
        if (e.detail.tab === 'dashboard') this.load();
      });
      window.addEventListener('intaris:user-changed', () => {
        if (this.initialized) this.load();
      });
      // Auto-load on first render
      this.load();
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
  };
}
