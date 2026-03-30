/**
 * Sessions tab — list, filter, and manage sessions.
 *
 * Supports session tree display (parent/child relationships),
 * WebSocket live updates, search, and expandable audit records.
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
    alignmentFilter: '',
    minRiskFilter: '',
    riskCategoryFilter: '',
    sortBy: 'created_at',
    sortDir: 'desc',
    searchQuery: '',
    expandedId: null,
    expandedSession: null,
    sessionAudit: [],
    expandedAuditId: null,
    expandedAuditRecord: null,
    auditLimit: 5,
    auditTotal: 0,
    resolvingAudit: {},
    auditNoteText: {},
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
      window.addEventListener('intaris:agent-changed', () => {
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
        // Try to find session in current page
        let session = this.sessions.find(s => s.session_id === sessionId);
        if (!session) {
          // Try searching for it
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
          // Skip if search is active and doesn't match
          if (this.searchQuery) return;
          // Deduplicate
          if (this.sessions.some(s => s.session_id === data.session_id)) return;
          const isChild = !!data.parent_session_id;
          // In tree view, child sessions are fetched with their parent;
          // don't add orphan children to the top level
          if (this.treeView && isChild && !this.sessions.some(s => s.session_id === data.parent_session_id)) return;
          this.sessions.unshift({
            session_id: data.session_id,
            user_id: data.user_id,
            title: data.title || null,
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
          // Default new parent sessions to collapsed
          if (!isChild) {
            this.collapsedSessions = { ...this.collapsedSessions, [data.session_id]: true };
          }
          if (!isChild) this.total++;
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
          if (data.title !== undefined) session.title = data.title;
          if (data.intention) session.intention = data.intention;
          if (data.details) session.details = data.details;
          session.last_activity_at = new Date().toISOString();
          session.updated_at = new Date().toISOString();
        }
        // Also update the expanded session if it's the same one
        if (this.expandedSession && this.expandedSession.session_id === data.session_id) {
          if (data.title !== undefined) this.expandedSession.title = data.title;
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

        // If this session is expanded, add to its audit feed.
        // Deduplicate by call_id to prevent Alpine x-for duplicate key crashes.
        if (this.expandedId === data.session_id && data.call_id) {
          const auditCallId = data.call_id;
          const isNew = !this.sessionAudit.some(r => r.call_id === auditCallId);
          this.sessionAudit = [
            {
              call_id: auditCallId,
              decision: data.decision,
              tool: data.tool,
              risk: data.risk,
              record_type: data.record_type || 'tool_call',
              classification: data.classification,
              evaluation_path: data.path,
              latency_ms: data.latency_ms,
              session_id: data.session_id,
              timestamp: data.timestamp || new Date().toISOString(),
            },
            ...this.sessionAudit.filter(r => r.call_id !== auditCallId),
          ].slice(0, Math.max(this.auditLimit, this.sessionAudit.length + 1));
          if (isNew) this.auditTotal++;
        }
      }
    },

    async load() {
      this.loading = true;
      try {
        const params = { page: this.page, limit: 20 };
        if (this.statusFilter) params.status = this.statusFilter;
        if (this.alignmentFilter) params.alignment = this.alignmentFilter;
        if (this.minRiskFilter) params.min_risk = this.minRiskFilter;
        if (this.riskCategoryFilter) params.risk_category = this.riskCategoryFilter;
        if (this.sortBy && this.sortBy !== 'created_at') params.sort = this.sortBy;
        if (this.sortDir === 'asc') params.sort_dir = 'asc';
        if (this.searchQuery) params.q = this.searchQuery;
        const agentFilter = Alpine.store('nav').selectedAgent;
        if (agentFilter) params.agent_id = agentFilter;
        // Tree-aware filtering: status/search filter roots only,
        // pagination counts roots only, all children included
        if (this.treeView) params.tree = true;
        const result = await IntarisAPI.listSessions(params);
        this._mergeSessions(result.items || []);
        this._autoCollapse();
        this.total = result.total;
        this.pages = result.pages;
      } catch (e) {
        Alpine.store('notify').error('Failed to load sessions: ' + e.message);
      } finally {
        this.loading = false;
      }
    },

    /**
     * Merge new session data into existing array, preserving object identity
     * for sessions that already exist. This prevents Alpine.js from
     * re-rendering the entire list and causing visual "jumping".
     */
    _mergeSessions(newItems) {
      const oldById = {};
      for (const s of this.sessions) {
        oldById[s.session_id] = s;
      }

      const merged = [];
      for (const newSession of newItems) {
        const existing = oldById[newSession.session_id];
        if (existing) {
          // Update existing object in-place to preserve identity
          Object.assign(existing, newSession);
          merged.push(existing);
        } else {
          merged.push(newSession);
        }
      }

      this.sessions = merged;
    },

    /**
     * Auto-collapse parent sessions that haven't been explicitly
     * expanded by the user. New parents default to collapsed.
     */
    _autoCollapse() {
      const parents = this._parentIds;
      if (parents.size === 0) return;
      let changed = false;
      for (const id of parents) {
        // Only set collapsed if not already tracked (preserve user state)
        if (!(id in this.collapsedSessions)) {
          this.collapsedSessions[id] = true;
          changed = true;
        }
      }
      if (changed) {
        this.collapsedSessions = { ...this.collapsedSessions };
      }
    },

    /**
     * Highlight search query matches in text by wrapping them in <mark>.
     * Returns raw HTML — use with x-html in the template.
     */
    highlightMatch(text) {
      if (!text || !this.searchQuery) return this._escapeHtml(text || '');
      const escaped = this._escapeHtml(text);
      const query = this._escapeHtml(this.searchQuery);
      // Case-insensitive replacement
      const regex = new RegExp('(' + query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')', 'gi');
      return escaped.replace(regex, '<mark class="bg-yellow-200 dark:bg-yellow-700 rounded px-0.5">$1</mark>');
    },

    _escapeHtml(str) {
      const div = document.createElement('div');
      div.textContent = str;
      return div.innerHTML;
    },

    // ── Search ────────────────────────────────────────────────

    applySearch() {
      this.page = 1;
      this.load();
    },

    filterByStatus(status) {
      this.statusFilter = status;
      this.page = 1;
      this.load();
    },

    filterByAlignment(alignment) {
      this.alignmentFilter = alignment;
      this.page = 1;
      this.load();
    },

    filterByMinRisk(val) {
      this.minRiskFilter = val;
      this.page = 1;
      this.load();
    },

    filterByRiskCategory(val) {
      this.riskCategoryFilter = val;
      this.page = 1;
      this.load();
    },

    setSort(col) {
      if (this.sortBy === col) {
        this.sortDir = this.sortDir === 'desc' ? 'asc' : 'desc';
      } else {
        this.sortBy = col;
        this.sortDir = 'desc';
      }
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
      if (!this.treeView) {
        return this.sessions.map(s => {
          s._depth = 0;
          s._children = [];
          return s;
        });
      }

      const byId = {};
      const roots = [];

      // Index all sessions — reuse existing objects to preserve identity
      for (const s of this.sessions) {
        s._depth = 0;
        s._children = [];
        byId[s.session_id] = s;
      }

      // Separate roots and children
      for (const s of this.sessions) {
        if (s.parent_session_id && byId[s.parent_session_id]) {
          s._depth = 1;
          byId[s.parent_session_id]._children.push(s);
        } else {
          roots.push(s);
        }
      }

      // Sort roots by created_at DESC (newest first) — stable sort that
      // doesn't cause visual jumping when last_activity_at updates via WebSocket
      roots.sort((a, b) => {
        const aTime = a.created_at || '';
        const bTime = b.created_at || '';
        return bTime.localeCompare(aTime);
      });

      // Flatten tree: parent followed by its children (unless collapsed)
      const result = [];
      for (const root of roots) {
        // Sort children by created_at DESC (newest first)
        root._children.sort((a, b) => {
          const aTime = a.created_at || '';
          const bTime = b.created_at || '';
          return bTime.localeCompare(aTime);
        });
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
      // Explicitly set all parents to expanded (false = not collapsed)
      const expanded = {};
      for (const id of this._parentIds) {
        expanded[id] = false;
      }
      this.collapsedSessions = expanded;
    },

    async toggleExpand(session) {
      if (this.expandedId === session.session_id) {
        // Collapsing — also collapse child tree if parent has children
        this.expandedId = null;
        this.expandedSession = null;
        this.sessionAudit = [];
        this.expandedAuditId = null;
        this.expandedAuditRecord = null;
        this.auditLimit = 5;
        this.auditTotal = 0;
        if (session._children?.length > 0) {
          this.collapsedSessions[session.session_id] = true;
          this.collapsedSessions = { ...this.collapsedSessions };
        }
        return;
      }
      // Expanding — also expand child tree if parent has children
      this.expandedId = session.session_id;
      this.expandedSession = session;
      this.expandedAuditId = null;
      this.expandedAuditRecord = null;
      this.auditLimit = 5;
      this.auditTotal = 0;
      if (session._children?.length > 0 && this.collapsedSessions[session.session_id]) {
        delete this.collapsedSessions[session.session_id];
        this.collapsedSessions = { ...this.collapsedSessions };
      }
      try {
        const [audit, summaries] = await Promise.all([
          IntarisAPI.listAudit({
            session_id: session.session_id,
            limit: 5,
          }),
          IntarisAPI.getSessionSummaries(session.session_id).catch(() => ({
            intaris_summaries: [],
            agent_summaries: [],
          })),
        ]);
        this.sessionAudit = audit.items || [];
        this.auditTotal = audit.total || 0;
        // Attach summaries to session for template access
        const intarisSummaries = (summaries.intaris_summaries || []).map(s => ({
          ...s,
          _expanded: false,
          risk_indicators: typeof s.risk_indicators === 'string'
            ? JSON.parse(s.risk_indicators) : (s.risk_indicators || []),
          tools_used: typeof s.tools_used === 'string'
            ? JSON.parse(s.tools_used) : (s.tools_used || []),
        }));
        // Separate compacted and window summaries
        const compacted = intarisSummaries.filter(s => s.summary_type === 'compacted');
        const windows = intarisSummaries.filter(s => s.summary_type !== 'compacted');
        const hasCompacted = compacted.length > 0;
        session._summaries = {
          intaris_summaries: intarisSummaries,
          compacted_summary: hasCompacted ? compacted[0] : null,
          window_summaries: windows,
          // Auto-expand windows when no compacted summary exists
          _windowsExpanded: !hasCompacted && windows.length > 0,
          agent_summaries: summaries.agent_summaries || [],
        };
        session._summaryTriggering = false;
      } catch (e) {
        Alpine.store('notify').error('Failed to load session audit: ' + e.message);
      }
    },

    /**
     * Open Console modal filtered to a summary's time window.
     */
    openSummaryConsole(session, summary) {
      window.dispatchEvent(new CustomEvent('intaris:open-console', {
        detail: {
          sessionId: session.session_id,
          afterTs: summary.window_start,
          beforeTs: summary.window_end,
        },
      }));
    },

    /**
     * Open Events modal filtered to a summary's time window.
     */
    openSummaryEvents(session, summary) {
      window.dispatchEvent(new CustomEvent('intaris:open-recording', {
        detail: {
          sessionId: session.session_id,
          afterTs: summary.window_start,
          beforeTs: summary.window_end,
        },
      }));
    },

    async triggerSummary(session) {
      session._summaryTriggering = true;
      try {
        const since = new Date().toISOString();
        await IntarisAPI.triggerSessionSummary(session.session_id);
        Alpine.store('notify').success('Summary generation triggered');
        // Poll for completion instead of fixed delay
        session._cancelSummaryPoll = pollTaskProgress({
          taskType: 'summary',
          sessionId: session.session_id,
          since,
          total: 1,
          interval: 2000,
          maxDuration: 120000,
          onDone: async () => {
            try {
              const summaries = await IntarisAPI.getSessionSummaries(session.session_id);
              const intarisSummaries = (summaries.intaris_summaries || []).map(s => ({
                ...s,
                _expanded: false,
                risk_indicators: typeof s.risk_indicators === 'string'
                  ? JSON.parse(s.risk_indicators) : (s.risk_indicators || []),
                tools_used: typeof s.tools_used === 'string'
                  ? JSON.parse(s.tools_used) : (s.tools_used || []),
              }));
              const compacted = intarisSummaries.filter(s => s.summary_type === 'compacted');
              const windows = intarisSummaries.filter(s => s.summary_type !== 'compacted');
              const hasCompacted = compacted.length > 0;
              session._summaries = {
                intaris_summaries: intarisSummaries,
                compacted_summary: hasCompacted ? compacted[0] : null,
                window_summaries: windows,
                _windowsExpanded: !hasCompacted && windows.length > 0,
                agent_summaries: summaries.agent_summaries || [],
              };
            } catch {}
            session._summaryTriggering = false;
            session._cancelSummaryPoll = null;
          },
        });
      } catch (e) {
        Alpine.store('notify').error(e.message || 'Failed to trigger summary');
        session._summaryTriggering = false;
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

    /**
     * Resolve an escalated audit record (approve/deny) from the session detail.
     */
    async resolveEscalation(callId, decision) {
      if (!callId || !decision) return;
      if (this.resolvingAudit[callId]) return;
      this.resolvingAudit = { ...this.resolvingAudit, [callId]: true };
      try {
        const note = this.auditNoteText[callId] || null;
        await IntarisAPI.resolveDecision(callId, decision, note);
        Alpine.store('notify').success(
          `Call ${decision === 'approve' ? 'approved' : 'denied'}`
        );
        // Update the record in-place so the UI reflects the resolution
        const record = this.sessionAudit.find(r => r.call_id === callId);
        if (record) {
          record.user_decision = decision;
        }
        if (this.expandedAuditRecord && this.expandedAuditRecord.call_id === callId) {
          this.expandedAuditRecord.user_decision = decision;
          this.expandedAuditRecord.user_note = note;
          this.expandedAuditRecord.resolved_at = new Date().toISOString();
        }
        delete this.auditNoteText[callId];
      } catch (e) {
        Alpine.store('notify').error('Failed to resolve: ' + (e.message || String(e)));
      } finally {
        const { [callId]: _, ...rest } = this.resolvingAudit;
        this.resolvingAudit = rest;
      }
    },

    async loadMoreAudit() {
      if (!this.expandedId) return;
      this.auditLimit += 10;
      try {
        const audit = await IntarisAPI.listAudit({
          session_id: this.expandedId,
          limit: this.auditLimit,
        });
        this.sessionAudit = audit.items || [];
        this.auditTotal = audit.total || 0;
      } catch (e) {
        Alpine.store('notify').error('Failed to load more evaluations: ' + e.message);
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

    recordTypeBadgeClass(type) {
      const classes = {
        reasoning: 'badge badge-reasoning',
        checkpoint: 'badge badge-checkpoint',
        summary: 'badge badge-summary',
      };
      return classes[type] || 'badge badge-low';
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
