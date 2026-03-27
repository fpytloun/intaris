/**
 * Audit log tab — filterable, paginated audit record browser.
 *
 * Subscribes to WebSocket events for live audit updates.
 */
function auditTab() {
  return {
    initialized: false,
    loading: false,
    records: [],
    total: 0,
    page: 1,
    pages: 1,
    expandedId: null,
    expandedRecord: null,
    resolving: {},
    noteText: {},

    // Filters
    filterSession: '',
    filterTool: '',
    filterDecision: '',
    filterRisk: '',
    filterPath: '',

    init() {
      window.addEventListener('intaris:tab-changed', (e) => {
        if (e.detail.tab === 'audit' && !this.initialized) {
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

      // Subscribe to WebSocket events for live audit updates
      window.addEventListener('intaris:ws-message', (e) => {
        this._handleWsEvent(e.detail);
      });
    },

    _handleWsEvent(data) {
      if (!this.initialized) return;
      if (Alpine.store('nav').activeTab !== 'audit') return;

      if (data.type === 'evaluated') {
        // Only add if on page 1 and matches current filters
        if (this.page !== 1) return;
        if (this.filterSession && data.session_id !== this.filterSession) return;
        if (this.filterTool && data.tool !== this.filterTool) return;
        if (this.filterDecision && data.decision !== this.filterDecision) return;
        if (this.filterRisk && data.risk !== this.filterRisk) return;
        if (this.filterPath && data.path !== this.filterPath) return;

        // Deduplicate by call_id to prevent Alpine x-for duplicate key crashes
        // when a WebSocket event arrives for a record already loaded via REST.
        const callId = data.call_id;
        if (!callId) return;
        this.records = [
          {
            call_id: callId,
            decision: data.decision,
            tool: data.tool,
            risk: data.risk,
            record_type: data.record_type || 'tool_call',
            classification: data.classification,
            evaluation_path: data.path,
            latency_ms: data.latency_ms,
            session_id: data.session_id,
            user_id: data.user_id,
            agent_id: data.agent_id,
            timestamp: data.timestamp || new Date().toISOString(),
          },
          ...this.records.filter(r => r.call_id !== callId),
        ].slice(0, 30);
        this.total++;
      }

      if (data.type === 'decided') {
        // Update the resolved record in-place if visible
        this._applyResolutionUpdate(data.call_id, {
          user_decision: data.user_decision,
          user_note: data.user_note,
          resolved_by: data.resolved_by,
          resolved_at: new Date().toISOString(),
        });
      }
    },

    async load() {
      this.loading = true;
      try {
        const params = { page: this.page, limit: 30 };
        if (this.filterSession) params.session_id = this.filterSession;
        if (this.filterTool) params.tool = this.filterTool;
        if (this.filterDecision) params.decision = this.filterDecision;
        if (this.filterRisk) params.risk = this.filterRisk;
        if (this.filterPath) params.path = this.filterPath;
        const agentFilter = Alpine.store('nav').selectedAgent;
        if (agentFilter) params.agent_id = agentFilter;
        const result = await IntarisAPI.listAudit(params);
        this.records = result.items || [];
        this.total = result.total;
        this.pages = result.pages;
      } catch (e) {
        Alpine.store('notify').error('Failed to load audit log: ' + e.message);
      } finally {
        this.loading = false;
      }
    },

    applyFilters() {
      this.page = 1;
      this.load();
    },

    clearFilters() {
      this.filterSession = '';
      this.filterTool = '';
      this.filterDecision = '';
      this.filterRisk = '';
      this.filterPath = '';
      this.page = 1;
      this.load();
    },

    prevPage() {
      if (this.page > 1) { this.page--; this.load(); }
    },

    nextPage() {
      if (this.page < this.pages) { this.page++; this.load(); }
    },

    async toggleExpand(record) {
      if (this.expandedId === record.call_id) {
        this.expandedId = null;
        this.expandedRecord = null;
        return;
      }
      this.expandedId = record.call_id;
      try {
        this.expandedRecord = await IntarisAPI.getAuditRecord(record.call_id);
      } catch (e) {
        this.expandedRecord = record;
      }
    },

    _applyResolutionUpdate(callId, updates) {
      if (!callId) return;
      const record = this.records.find(r => r.call_id === callId);
      if (record) {
        Object.assign(record, updates);
      }
      if (this.expandedRecord && this.expandedRecord.call_id === callId) {
        this.expandedRecord = { ...this.expandedRecord, ...updates };
      }
    },

    async _resolveRecord(callId, decision, mode) {
      if (!callId || !decision) return;
      if (this.resolving[callId]) return;

      this.resolving = { ...this.resolving, [callId]: true };
      try {
        const note = this.noteText[callId] || null;
        await IntarisAPI.resolveDecision(callId, decision, note);
        this._applyResolutionUpdate(callId, {
          user_decision: decision,
          user_note: note,
          resolved_by: 'user',
          resolved_at: new Date().toISOString(),
        });
        delete this.noteText[callId];

        if (mode === 'judge-override') {
          Alpine.store('notify').success(`Judge decision overridden to ${decision}`);
        } else if (mode === 'denial-override') {
          Alpine.store('notify').success(
            decision === 'approve'
              ? 'Denial overridden — retry will be approved'
              : 'Denial confirmed'
          );
        } else {
          Alpine.store('notify').success(
            `Call ${decision === 'approve' ? 'approved' : 'denied'}`
          );
        }
      } catch (e) {
        const action = mode === 'judge-override'
          ? 'override judge decision'
          : mode === 'denial-override'
            ? 'override denial'
            : 'resolve';
        Alpine.store('notify').error(`Failed to ${action}: ` + (e.message || String(e)));
      } finally {
        const { [callId]: _, ...rest } = this.resolving;
        this.resolving = rest;
      }
    },

    resolveEscalation(callId, decision) {
      return this._resolveRecord(callId, decision, 'resolve');
    },

    overrideDenial(callId, decision) {
      return this._resolveRecord(callId, decision, 'denial-override');
    },

    overrideJudge(callId, decision) {
      return this._resolveRecord(callId, decision, 'judge-override');
    },

    canResolveEscalation(record) {
      return record?.decision === 'escalate' && !record?.user_decision;
    },

    canOverrideDenial(record) {
      return record?.decision === 'deny'
        && !record?.user_decision
        && record?.evaluation_path !== 'fast';
    },

    canOverrideJudge(record) {
      return record?.resolved_by === 'judge';
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

    // ── Navigation ────────────────────────────────────────────

    goToSession(sessionId) {
      Alpine.store('nav').openSessionModal(sessionId);
    },
  };
}
