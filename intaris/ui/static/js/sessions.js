/**
 * Sessions tab — list, filter, and manage sessions.
 *
 * Supports session tree display (parent/child relationships),
 * WebSocket live updates, and expandable audit records.
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
    expandedAuditId: null,
    expandedAuditRecord: null,
    treeView: true,
    collapsedSessions: {},

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

      // Subscribe to WebSocket events for live session updates
      window.addEventListener('intaris:ws-message', (e) => {
        this._handleWsEvent(e.detail);
      });

      // Handle navigation from other tabs (e.g., approvals → session)
      window.addEventListener('intaris:navigate-session', async (e) => {
        const sessionId = e.detail?.sessionId;
        if (!sessionId) return;
        if (!this.initialized) {
          this.initialized = true;
          await this.load();
        }
        // Find session in current page, or fetch directly from API
        let session = this.sessions.find(s => s.session_id === sessionId);
        if (!session) {
          try {
            session = await IntarisAPI.getSession(sessionId);
          } catch (err) {
            Alpine.store('notify').error('Session not found: ' + sessionId);
            return;
          }
        }
        if (session) {
          this.toggleExpand(session);
        }
      });
    },

    _handleWsEvent(data) {
      if (data.type === 'session_created') {
        // Add new session to list if on page 1 and matching filter
        if (this.page === 1 && (!this.statusFilter || this.statusFilter === 'active')) {
          this.sessions.unshift({
            session_id: data.session_id,
            user_id: data.user_id,
            intention: data.intention || '',
            status: data.status || 'active',
            total_calls: 0,
            approved_count: 0,
            denied_count: 0,
            escalated_count: 0,
            parent_session_id: data.parent_session_id || null,
            details: data.details || null,
            last_activity_at: new Date().toISOString(),
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
          });
          this.total++;
        }
      }

      if (data.type === 'session_status_changed') {
        const session = this.sessions.find(s => s.session_id === data.session_id);
        if (session) {
          session.status = data.status;
          session.last_activity_at = new Date().toISOString();
          session.updated_at = new Date().toISOString();
        }
      }

      if (data.type === 'session_updated') {
        const session = this.sessions.find(s => s.session_id === data.session_id);
        if (session) {
          if (data.intention) session.intention = data.intention;
          if (data.details) session.details = data.details;
          session.last_activity_at = new Date().toISOString();
          session.updated_at = new Date().toISOString();
        }
        // Also update the expanded session if it's the same one
        if (this.expandedSession && this.expandedSession.session_id === data.session_id) {
          if (data.intention) this.expandedSession.intention = data.intention;
          if (data.details) this.expandedSession.details = data.details;
          this.expandedSession.last_activity_at = new Date().toISOString();
        }
      }

      if (data.type === 'evaluated') {
        // Increment counters for the matching session
        const session = this.sessions.find(s => s.session_id === data.session_id);
        if (session) {
          session.total_calls = (session.total_calls || 0) + 1;
          if (data.decision === 'approve') session.approved_count = (session.approved_count || 0) + 1;
          else if (data.decision === 'deny') session.denied_count = (session.denied_count || 0) + 1;
          else if (data.decision === 'escalate') session.escalated_count = (session.escalated_count || 0) + 1;
          session.last_activity_at = new Date().toISOString();
          session.updated_at = new Date().toISOString();
        }

        // If this session is expanded, add to its audit feed
        if (this.expandedId === data.session_id) {
          this.sessionAudit.unshift({
            call_id: data.call_id,
            decision: data.decision,
            tool: data.tool,
            risk: data.risk,
            record_type: data.record_type || 'tool_call',
            classification: data.classification,
            evaluation_path: data.path,
            latency_ms: data.latency_ms,
            session_id: data.session_id,
            timestamp: data.timestamp || new Date().toISOString(),
          });
          if (this.sessionAudit.length > 20) {
            this.sessionAudit = this.sessionAudit.slice(0, 20);
          }
        }
      }
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

    toggleTreeView() {
      this.treeView = !this.treeView;
    },

    /**
     * Get sessions organized as a tree (parent sessions with children nested).
     * Returns flat sessions list when treeView is off.
     * Collapsed parents hide their children.
     */
    get sessionTree() {
      if (!this.treeView) return this.sessions.map(s => ({ ...s, _depth: 0, _children: [] }));

      const byId = {};
      const roots = [];

      // Index all sessions
      for (const s of this.sessions) {
        byId[s.session_id] = { ...s, _depth: 0, _children: [] };
      }

      // Separate roots and children
      for (const s of this.sessions) {
        const node = byId[s.session_id];
        if (s.parent_session_id && byId[s.parent_session_id]) {
          node._depth = 1;
          byId[s.parent_session_id]._children.push(node);
        } else {
          roots.push(node);
        }
      }

      // Sort roots by created_at DESC (newest first)
      roots.sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''));

      // Flatten tree: parent followed by its children (unless collapsed)
      const result = [];
      for (const root of roots) {
        // Sort children by created_at DESC (newest first)
        root._children.sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''));
        result.push(root);
        if (!this.collapsedSessions[root.session_id]) {
          for (const child of root._children) {
            result.push(child);
          }
        }
      }

      return result;
    },

    /**
     * Get all parent session IDs (sessions that have children).
     */
    get _parentIds() {
      const parents = new Set();
      for (const s of this.sessions) {
        if (s.parent_session_id) {
          parents.add(s.parent_session_id);
        }
      }
      return parents;
    },

    get allCollapsed() {
      const parents = this._parentIds;
      if (parents.size === 0) return false;
      for (const id of parents) {
        if (!this.collapsedSessions[id]) return false;
      }
      return true;
    },

    get hasParents() {
      return this._parentIds.size > 0;
    },

    toggleCollapse(sessionId) {
      if (this.collapsedSessions[sessionId]) {
        delete this.collapsedSessions[sessionId];
      } else {
        this.collapsedSessions[sessionId] = true;
      }
      // Trigger reactivity by reassigning
      this.collapsedSessions = { ...this.collapsedSessions };
    },

    collapseAll() {
      const collapsed = {};
      for (const id of this._parentIds) {
        collapsed[id] = true;
      }
      this.collapsedSessions = collapsed;
    },

    expandAll() {
      this.collapsedSessions = {};
    },

    async toggleExpand(session) {
      if (this.expandedId === session.session_id) {
        this.expandedId = null;
        this.expandedSession = null;
        this.sessionAudit = [];
        this.expandedAuditId = null;
        this.expandedAuditRecord = null;
        return;
      }
      this.expandedId = session.session_id;
      this.expandedSession = session;
      this.expandedAuditId = null;
      this.expandedAuditRecord = null;
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

    async toggleAuditExpand(record) {
      if (this.expandedAuditId === record.call_id) {
        this.expandedAuditId = null;
        this.expandedAuditRecord = null;
        return;
      }
      this.expandedAuditId = record.call_id;
      try {
        this.expandedAuditRecord = await IntarisAPI.getAuditRecord(record.call_id);
      } catch (e) {
        this.expandedAuditRecord = record;
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

    riskBadgeClass(risk) {
      return 'badge badge-' + (risk || 'low');
    },

    pathBadgeClass(path) {
      if (path === 'critical') return 'badge badge-deny';
      return 'badge badge-' + (path || 'fast');
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

    truncate(str, len) {
      if (!str) return '';
      return str.length > len ? str.substring(0, len) + '...' : str;
    },
  };
}
