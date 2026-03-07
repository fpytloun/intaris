/**
 * Sessions tab — list, filter, and manage sessions.
 */
function sessionsTab() {
  return {
    initialized: false,
    loading: false,
    sessions: [],
    total: 0,
    page: 1,
    pages: 1,
    statusFilter: '',
    expandedId: null,
    expandedSession: null,
    sessionAudit: [],

    init() {
      window.addEventListener('intaris:tab-changed', (e) => {
        if (e.detail.tab === 'sessions' && !this.initialized) {
          this.initialized = true;
          this.load();
        }
      });
      window.addEventListener('intaris:user-changed', () => {
        if (this.initialized) this.load();
      });
    },

    async load() {
      this.loading = true;
      try {
        const params = { page: this.page, limit: 20 };
        if (this.statusFilter) params.status = this.statusFilter;
        const result = await IntarisAPI.listSessions(params);
        this.sessions = result.items || [];
        this.total = result.total;
        this.pages = result.pages;
      } catch (e) {
        Alpine.store('notify').error('Failed to load sessions: ' + e.message);
      } finally {
        this.loading = false;
      }
    },

    filterByStatus(status) {
      this.statusFilter = status;
      this.page = 1;
      this.load();
    },

    prevPage() {
      if (this.page > 1) { this.page--; this.load(); }
    },

    nextPage() {
      if (this.page < this.pages) { this.page++; this.load(); }
    },

    async toggleExpand(session) {
      if (this.expandedId === session.session_id) {
        this.expandedId = null;
        this.expandedSession = null;
        this.sessionAudit = [];
        return;
      }
      this.expandedId = session.session_id;
      this.expandedSession = session;
      try {
        const audit = await IntarisAPI.listAudit({
          session_id: session.session_id,
          limit: 20,
        });
        this.sessionAudit = audit.items || [];
      } catch (e) {
        Alpine.store('notify').error('Failed to load session audit: ' + e.message);
      }
    },

    async updateStatus(sessionId, newStatus) {
      try {
        await IntarisAPI.updateStatus(sessionId, newStatus);
        Alpine.store('notify').success(`Session ${newStatus}`);
        this.load();
      } catch (e) {
        Alpine.store('notify').error('Failed to update status: ' + e.message);
      }
    },

    statusBadgeClass(status) {
      return 'badge badge-' + (status || 'active');
    },

    decisionBadgeClass(decision) {
      return 'badge badge-' + (decision || 'low');
    },

    formatTime(ts) {
      if (!ts) return '';
      return new Date(ts).toLocaleString();
    },

    truncate(str, len) {
      if (!str) return '';
      return str.length > len ? str.substring(0, len) + '...' : str;
    },
  };
}
