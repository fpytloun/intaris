/**
 * Intaris UI — Alpine.js stores and app initialization.
 *
 * Stores:
 * - auth: login state, API key, identity, user switching
 * - nav: active tab navigation
 * - notify: toast notification system
 * - ws: shared WebSocket connection for real-time updates
 */

// ── Risk score helpers (shared across all tabs) ──────────────────────
// Maps numeric risk scores (1-10) to named bands and CSS classes.
// Mirrors the Python risk_band() function in analyzer.py.

/** Map a numeric score (1-10) to a named band. */
function riskBand(score) {
  const s = Number(score) || 1;
  if (s <= 2) return 'minimal';
  if (s <= 4) return 'low';
  if (s <= 6) return 'moderate';
  if (s <= 8) return 'elevated';
  if (s === 9) return 'high';
  return 'critical';
}

/** Format a risk score for display: "3 (LOW)" */
function riskScoreLabel(score) {
  const s = Number(score) || 1;
  return s + ' (' + riskBand(s).toUpperCase() + ')';
}

/** Return Tailwind dot color class object for a risk score. */
function riskScoreColor(score) {
  const s = Number(score) || 1;
  if (s <= 2) return 'bg-green-500';
  if (s <= 4) return 'bg-cyan-500';
  if (s <= 6) return 'bg-yellow-500';
  if (s <= 8) return 'bg-orange-500';
  return 'bg-red-500';
}

/** Return dot class as an object for Alpine :class binding. */
function riskScoreDotClass(score) {
  const cls = riskScoreColor(score);
  return { [cls]: true };
}

/** Return badge class object for a severity score (numeric 1-10). */
function severityBadgeClass(score) {
  const s = Number(score) || 1;
  return {
    'badge-low': s <= 4,
    'badge-medium': s >= 5 && s <= 6,
    'badge-high': s >= 7 && s <= 8,
    'badge-critical': s >= 9,
  };
}

/** Map a numeric risk score to a chart color hex. */
function riskScoreChartColor(score) {
  const s = Number(score) || 1;
  if (s <= 2) return '#22D3EE'; // cyan
  if (s <= 4) return '#34D399'; // green
  if (s <= 6) return '#FBBF24'; // amber
  if (s <= 8) return '#FB923C'; // orange
  return '#F87171'; // red
}

// ── Risk indicator / finding category tooltips ──────────────────────
// Descriptions for hover tooltips on risk indicator and finding category names.
// Used with the .has-tooltip CSS class and data-tooltip attribute.

const RISK_TOOLTIPS = {
  // L2 session risk indicators
  intent_drift: 'Agent gradually shifting away from declared intention',
  restriction_circumvention: 'Attempts to bypass safety denials (retrying denied operations)',
  scope_creep: 'Accessing resources beyond expected project scope',
  insecure_reasoning: 'Reasoning that suggests unsafe decision-making',
  unusual_tool_pattern: 'Unexpected tool usage sequences or frequencies',
  injection_attempt: 'Signs of prompt injection in tool args or reasoning',
  escalation_pattern: 'Increasing frequency of denied or escalated calls',
  delegation_misalignment: 'Sub-session actions diverge from parent session intention',
  // L3 cross-session finding categories (positive)
  consistent_alignment: 'Agent consistently follows declared intentions across sessions',
  normal_development: 'Standard development activity with no concerning patterns',
  improving_posture: 'Risk indicators or misalignment decreasing over time',
  // L3 cross-session finding categories (concerning)
  coordinated_access: 'Sessions together access broader resources than any single intention justifies',
  progressive_escalation: 'Behavior becoming measurably riskier over time',
  intent_masking: 'Individual intentions appear benign but collectively suggest a different goal',
  tool_abuse: 'Repeated misuse of specific tools across sessions',
  persistent_misalignment: 'Consistent partial or full misalignment across multiple sessions',
  insecure_reasoning_pattern: 'Recurring patterns of unsafe or confused reasoning across sessions',
};

/** Get tooltip description for a risk indicator or finding category name. */
function riskTooltip(name) {
  return RISK_TOOLTIPS[name] || '';
}

// ── Task progress polling helper ─────────────────────────────────────

/**
 * Poll task status at a regular interval until all tasks complete.
 *
 * @param {Object} opts
 * @param {string}   [opts.taskType]    - Filter by task type (summary/analysis)
 * @param {string}   [opts.sessionId]   - Filter by session ID
 * @param {string}   [opts.agentId]     - Filter by agent ID
 * @param {string}   [opts.since]       - ISO 8601 cutoff for created_at
 * @param {number}   [opts.total]       - Total expected tasks (for percentage calc)
 * @param {number}   [opts.interval]    - Poll interval in ms (default 3000)
 * @param {number}   [opts.maxDuration] - Max poll duration in ms (default 600000 = 10 min)
 * @param {Function} opts.onUpdate      - Called with { pending, running, completed, failed, cancelled, processed, pct }
 * @param {Function} [opts.onDone]      - Called when pending+running = 0 or maxDuration exceeded
 * @returns {Function} cancel - Call to stop polling
 */
function pollTaskProgress(opts) {
  let active = true;
  const interval = opts.interval || 3000;
  const maxDuration = opts.maxDuration || 600000;
  const total = opts.total || 0;
  const startTime = Date.now();

  const poll = async () => {
    if (!active) return;
    // Check max duration
    if (Date.now() - startTime > maxDuration) {
      if (opts.onDone) opts.onDone({ pending: 0, running: 0, completed: 0, failed: 0, cancelled: 0, processed: 0, pct: 100, timedOut: true });
      return;
    }
    try {
      const params = {};
      if (opts.taskType) params.task_type = opts.taskType;
      if (opts.sessionId) params.session_id = opts.sessionId;
      if (opts.agentId !== undefined) params.agent_id = opts.agentId;
      if (opts.since) params.since = opts.since;
      const status = await IntarisAPI.getTaskStatus(params);
      const processed = (status.completed || 0) + (status.failed || 0) + (status.cancelled || 0);
      const pending = (status.pending || 0) + (status.running || 0);
      const pct = total > 0 ? Math.min(100, Math.round(processed / total * 100)) : (pending > 0 ? 50 : 100);
      const update = { ...status, processed, pct };
      if (opts.onUpdate) opts.onUpdate(update);
      if (pending > 0 && active) {
        setTimeout(poll, interval);
      } else if (opts.onDone) {
        opts.onDone({ ...update, pct: 100 });
      }
    } catch (e) {
      // Keep polling on transient errors
      if (active) setTimeout(poll, interval);
    }
  };
  // Start after a short delay to let the task enqueue settle
  setTimeout(poll, 1000);

  return () => { active = false; };
}

document.addEventListener('alpine:init', () => {

  // ── Auth Store ───────────────────────────────────────────────
  Alpine.store('auth', {
    authenticated: false,
    loading: true,
    error: '',
    identity: null,
    users: [],
    selectedUser: '',

    async init() {
      // Check for exchange token in URL (Cognis cross-service SSO)
      const params = new URLSearchParams(window.location.search);
      const exchangeToken = params.get('token');
      if (exchangeToken) {
        try {
          const resp = await fetch('/api/v1/auth/exchange', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ token: exchangeToken })
          });
          if (resp.ok) {
            // Clean token from URL after successful exchange
            window.history.replaceState({}, '', window.location.pathname);
            // Cookie set by response — proceed with cookie auth
            await this.tryCookieAuth();
            if (this.authenticated) return;
            // Exchange succeeded (cookie set) but session verification
            // failed. The token is single-use, so retrying the link
            // won't work. Show an actionable error.
            this.error = 'SSO session created but verification failed. Please reload the page or use an API key.';
            return;
          }
        } catch (e) { console.warn('Exchange token auth failed, falling back:', e); }
      }
      const key = IntarisAPI.getKey();
      if (key) {
        await this.verify();
      } else {
        // Try cookie-based auth (cross-service SSO from Cognis)
        await this.tryCookieAuth();
      }
    },

    async tryCookieAuth() {
      try {
        const identity = await IntarisAPI.whoami();
        if (identity && identity.user_id) {
          IntarisAPI.cookieAuth = true;
          this.identity = identity;
          this.authenticated = true;
          this.selectedUser = identity.user_id;
          if (identity.can_switch_user) {
            await this.loadUsers();
          }
          this.loading = false;
          return;
        }
      } catch {
        // Cookie auth not available — show login form
      }
      this.loading = false;
    },

    async login(apiKey, userId) {
      this.error = '';
      this.loading = true;
      IntarisAPI.setKey(apiKey || '');
      // Set user ID before /whoami so X-User-Id header is sent
      if (userId) {
        IntarisAPI.setSelectedUser(userId);
      }
      try {
        const identity = await IntarisAPI.whoami();
        this.identity = identity;
        // Resolve effective user: from key binding, from header, or from input
        const effectiveUser = identity.user_id || userId || '';
        if (!effectiveUser) {
          throw new Error('User ID is required. Enter a User ID or use a bound API key.');
        }
        this.authenticated = true;
        if (identity.can_switch_user) {
          // Ensure the selected user is set for subsequent API calls
          if (!identity.user_id && userId) {
            IntarisAPI.setSelectedUser(userId);
          }
          this.selectedUser = effectiveUser;
          await this.loadUsers();
        } else {
          this.selectedUser = identity.user_id || '';
          IntarisAPI.setSelectedUser('');
        }
      } catch (e) {
        IntarisAPI.clearKey();
        IntarisAPI.setSelectedUser('');
        this.authenticated = false;
        this.error = e.message === 'Unauthorized'
          ? 'Invalid API key'
          : e.message || `Connection failed`;
      } finally {
        this.loading = false;
      }
    },

    async verify() {
      this.loading = true;
      try {
        const identity = await IntarisAPI.whoami();
        this.identity = identity;
        const stored = IntarisAPI.getSelectedUser();
        const effectiveUser = identity.user_id || stored || '';
        if (!effectiveUser) {
          // No user identity available — need to re-login with User ID
          this.authenticated = false;
          return;
        }
        this.authenticated = true;
        if (identity.can_switch_user) {
          this.selectedUser = effectiveUser;
          await this.loadUsers();
        } else {
          this.selectedUser = identity.user_id || '';
        }
      } catch {
        IntarisAPI.clearKey();
        IntarisAPI.setSelectedUser('');
        this.authenticated = false;
      } finally {
        this.loading = false;
      }
    },

    async loadUsers() {
      try {
        const stats = await IntarisAPI.stats();
        this.users = stats.users || [];
        Alpine.store('nav').agents = stats.agents || [];
        Alpine.store('nav').restoreAgent();
      } catch {
        this.users = [];
      }
    },

    switchUser(userId) {
      this.selectedUser = userId;
      if (this.identity && userId === this.identity.user_id) {
        IntarisAPI.setSelectedUser('');
      } else {
        IntarisAPI.setSelectedUser(userId);
      }
      window.dispatchEvent(new CustomEvent('intaris:user-changed', {
        detail: { userId }
      }));
    },

    logout() {
      IntarisAPI.clearKey();
      IntarisAPI.setSelectedUser('');
      localStorage.removeItem('intaris_selected_agent');
      this.authenticated = false;
      this.identity = null;
      this.users = [];
      this.selectedUser = '';
      this.error = '';
      Alpine.store('nav').selectedAgent = '';
      Alpine.store('nav').agents = [];
      window.dispatchEvent(new CustomEvent('intaris:logout'));
    },
  });

  // ── Nav Store ────────────────────────────────────────────────
  Alpine.store('nav', {
    activeTab: 'dashboard',
    selectedAgent: '',
    agents: [],
    pendingApprovals: 0,

    // Session modal state
    sessionModal: null,
    sessionModalAudit: [],
    sessionModalChildren: [],
    sessionModalSummaries: null,
    sessionModalLoading: false,
    sessionModalAuditExpandedId: null,
    sessionModalAuditRecord: null,
    sessionModalResolving: {},
    sessionModalNoteText: {},

    setTab(tab) {
      this.activeTab = tab;
      window.dispatchEvent(new CustomEvent('intaris:tab-changed', {
        detail: { tab }
      }));
    },

    setAgent(agentId) {
      this.selectedAgent = agentId;
      localStorage.setItem('intaris_selected_agent', agentId);
      window.dispatchEvent(new CustomEvent('intaris:agent-changed', {
        detail: { agentId }
      }));
    },

    /** Restore persisted agent selection after agents list is populated. */
    restoreAgent() {
      const saved = localStorage.getItem('intaris_selected_agent');
      if (saved && this.agents.includes(saved)) {
        this.selectedAgent = saved;
      } else {
        this.selectedAgent = '';
      }
    },

    // ── Session modal ───────────────────────────────────────

    async openSessionModal(sessionId) {
      if (!sessionId) return;
      this.sessionModalLoading = true;
      this.sessionModal = null;
      this.sessionModalAudit = [];
      this.sessionModalChildren = [];
      this.sessionModalSummaries = null;
      this.sessionModalAuditExpandedId = null;
      this.sessionModalAuditRecord = null;
      try {
        const [session, audit, children, summaries] = await Promise.all([
          IntarisAPI.getSession(sessionId),
          IntarisAPI.listAudit({ session_id: sessionId, limit: 20 }),
          IntarisAPI.listSessions({ parent_session_id: sessionId, limit: 50 }),
          IntarisAPI.getSessionSummaries(sessionId).catch(() => ({
            intaris_summaries: [],
            agent_summaries: [],
          })),
        ]);
        this.sessionModal = { ...session, _detailsExpanded: false, _policyExpanded: false };
        this.sessionModalAudit = audit.items || [];
        this.sessionModalChildren = children.items || [];
        // Parse summaries — same logic as sessions.js
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
        this.sessionModalSummaries = {
          compacted_summary: compacted.length > 0 ? compacted[0] : null,
          window_summaries: windows,
          _windowsExpanded: !compacted.length && windows.length > 0,
          agent_summaries: summaries.agent_summaries || [],
        };
      } catch (e) {
        Alpine.store('notify').error('Session not found: ' + sessionId);
        this.sessionModalLoading = false;
        return;
      }
      this.sessionModalLoading = false;
    },

    closeSessionModal() {
      this.sessionModal = null;
      this.sessionModalAudit = [];
      this.sessionModalChildren = [];
      this.sessionModalSummaries = null;
      this.sessionModalLoading = false;
      this.sessionModalAuditExpandedId = null;
      this.sessionModalAuditRecord = null;
    },

    async toggleModalAuditExpand(record) {
      if (this.sessionModalAuditExpandedId === record.call_id) {
        this.sessionModalAuditExpandedId = null;
        this.sessionModalAuditRecord = null;
        return;
      }
      this.sessionModalAuditExpandedId = record.call_id;
      try {
        this.sessionModalAuditRecord = await IntarisAPI.getAuditRecord(record.call_id);
      } catch (e) {
        this.sessionModalAuditRecord = record;
      }
    },

    async resolveModalEscalation(callId, decision) {
      if (!callId || !decision) return;
      if (this.sessionModalResolving[callId]) return;
      this.sessionModalResolving = { ...this.sessionModalResolving, [callId]: true };
      try {
        const note = this.sessionModalNoteText[callId] || null;
        await IntarisAPI.resolveDecision(callId, decision, note);
        Alpine.store('notify').success(
          `Call ${decision === 'approve' ? 'approved' : 'denied'}`
        );
        const record = this.sessionModalAudit.find(r => r.call_id === callId);
        if (record) record.user_decision = decision;
        if (this.sessionModalAuditRecord && this.sessionModalAuditRecord.call_id === callId) {
          this.sessionModalAuditRecord.user_decision = decision;
          this.sessionModalAuditRecord.user_note = note;
          this.sessionModalAuditRecord.resolved_at = new Date().toISOString();
        }
        delete this.sessionModalNoteText[callId];
      } catch (e) {
        Alpine.store('notify').error('Failed to resolve: ' + (e.message || String(e)));
      } finally {
        const { [callId]: _, ...rest } = this.sessionModalResolving;
        this.sessionModalResolving = rest;
      }
    },

    async updateSessionStatus(sessionId, newStatus) {
      try {
        await IntarisAPI.updateStatus(sessionId, newStatus);
        Alpine.store('notify').success(`Session ${newStatus}`);
        if (this.sessionModal && this.sessionModal.session_id === sessionId) {
          this.sessionModal.status = newStatus;
        }
      } catch (e) {
        Alpine.store('notify').error('Failed to update status: ' + e.message);
      }
    },
  });

  // ── Notify Store ─────────────────────────────────────────────
  Alpine.store('notify', {
    toasts: [],
    _id: 0,

    add(message, type = 'info', duration = 4000) {
      const id = ++this._id;
      this.toasts.push({ id, message, type });
      if (duration > 0) {
        setTimeout(() => this.remove(id), duration);
      }
    },

    remove(id) {
      this.toasts = this.toasts.filter(t => t.id !== id);
    },

    success(msg) { this.add(msg, 'success'); },
    error(msg) { this.add(msg, 'error', 6000); },
    info(msg) { this.add(msg, 'info'); },
    warning(msg) { this.add(msg, 'warning', 5000); },
  });

  // ── WebSocket Store ──────────────────────────────────────────
  // Shared WebSocket connection for real-time updates across all tabs.
  // Tabs subscribe via window events dispatched by this store.
  Alpine.store('ws', {
    ws: null,
    connected: false,
    _reconnectTimer: null,
    _reconnectDelay: 1000,
    _reconnectAttempts: 0,
    _maxReconnectAttempts: 10,
    _maxReconnectDelay: 30000,

    // Browser notifications — per-event-type preferences
    notificationsEnabled: localStorage.getItem('intaris_notifications') === 'true',
    notifyOnEscalation: localStorage.getItem('intaris_notify_escalation') !== 'false',  // default on
    notifyOnDeny: localStorage.getItem('intaris_notify_deny') === 'true',               // default off
    notifyOnSuspend: localStorage.getItem('intaris_notify_suspend') !== 'false',         // default on
    notificationPermission: typeof Notification !== 'undefined' ? Notification.permission : 'denied',

    connect() {
      if (this.ws) return;
      if (!Alpine.store('auth').authenticated) return;

      this.ws = IntarisAPI.connectWebSocket({
        onOpen: () => {
          this.connected = true;
          this._reconnectAttempts = 0;
          this._reconnectDelay = 1000;
        },
        onMessage: (data) => {
          // Track pending approvals count globally
          const nav = Alpine.store('nav');
          if (data.type === 'evaluated' && data.decision === 'escalate') {
            nav.pendingApprovals = Math.max(0, (nav.pendingApprovals || 0) + 1);
            if (this.notifyOnEscalation) {
              this._showBrowserNotification({
                title: `Approval needed${data.risk ? ` [${data.risk}]` : ''}`,
                body: `Tool: ${data.tool || 'unknown'}\nSession: ${data.session_id || ''}`,
                tag: 'intaris-escalation-' + data.call_id,
                tab: 'approvals',
              });
            }
          } else if (data.type === 'evaluated' && data.decision === 'deny') {
            if (this.notifyOnDeny) {
              this._showBrowserNotification({
                title: `Tool call denied${data.risk ? ` [${data.risk}]` : ''}`,
                body: `Tool: ${data.tool || 'unknown'}\nSession: ${data.session_id || ''}`,
                tag: 'intaris-denial-' + data.call_id,
                tab: 'sessions',
              });
            }
          } else if (data.type === 'decided') {
            nav.pendingApprovals = Math.max(0, (nav.pendingApprovals || 0) - 1);
          } else if (data.type === 'session_status_changed' && data.status === 'suspended') {
            if (this.notifyOnSuspend) {
              this._showBrowserNotification({
                title: 'Session suspended',
                body: `Session: ${data.session_id || ''}\n${data.status_reason || ''}`,
                tag: 'intaris-suspend-' + data.session_id,
                tab: 'sessions',
              });
            }
          }

          // Dispatch typed events for tabs to listen on
          window.dispatchEvent(new CustomEvent('intaris:ws-message', {
            detail: data,
          }));
        },
        onClose: () => {
          this.connected = false;
          this.ws = null;
          this._scheduleReconnect();
        },
        onError: () => {
          this.connected = false;
        },
      });
    },

    disconnect() {
      if (this.ws) {
        this.ws.onclose = null;
        this.ws.close();
        this.ws = null;
      }
      this.connected = false;
      clearTimeout(this._reconnectTimer);
      this._reconnectTimer = null;
      this._reconnectAttempts = 0;
      this._reconnectDelay = 1000;
    },

    _scheduleReconnect() {
      if (!Alpine.store('auth').authenticated) return;

      this._reconnectAttempts++;
      if (this._reconnectAttempts > this._maxReconnectAttempts) return;

      this._reconnectTimer = setTimeout(() => {
        this._reconnectTimer = null;
        this.connect();
      }, this._reconnectDelay);

      this._reconnectDelay = Math.min(
        this._reconnectDelay * 2,
        this._maxReconnectDelay,
      );
    },

    // ── Browser notifications ─────────────────────────────────

    async requestNotificationPermission() {
      if (typeof Notification === 'undefined') return;
      const result = await Notification.requestPermission();
      this.notificationPermission = result;
      if (result === 'granted') {
        this.notificationsEnabled = true;
        localStorage.setItem('intaris_notifications', 'true');
      }
    },

    toggleNotifications(enabled) {
      this.notificationsEnabled = enabled;
      localStorage.setItem('intaris_notifications', enabled ? 'true' : 'false');
      if (enabled && this.notificationPermission !== 'granted') {
        this.requestNotificationPermission();
      }
    },

    toggleNotifyPreference(key, enabled) {
      this[key] = enabled;
      const storageKey = {
        notifyOnEscalation: 'intaris_notify_escalation',
        notifyOnDeny: 'intaris_notify_deny',
        notifyOnSuspend: 'intaris_notify_suspend',
      }[key];
      if (storageKey) localStorage.setItem(storageKey, enabled ? 'true' : 'false');
    },

    _showBrowserNotification({ title, body, tag, tab }) {
      if (!this.notificationsEnabled) return;
      if (typeof Notification === 'undefined') return;
      if (Notification.permission !== 'granted') return;
      // Don't notify if the tab is focused
      if (document.hasFocus()) return;

      try {
        const n = new Notification(title, {
          body,
          icon: '/ui/favicon.ico',
          tag,
          requireInteraction: true,
        });
        n.onclick = () => {
          window.focus();
          if (tab) Alpine.store('nav').setTab(tab);
          n.close();
        };
        // Auto-close after 30s
        setTimeout(() => n.close(), 30000);
      } catch (e) {
        // Notification API may throw in some contexts
      }
    },
  });

  // Auto-connect/disconnect WebSocket based on auth state
  window.addEventListener('intaris:user-changed', () => {
    Alpine.store('ws').disconnect();
    Alpine.store('ws').connect();
  });
  window.addEventListener('intaris:logout', () => {
    Alpine.store('ws').disconnect();
  });

  // Connect after auth verification completes
  const _checkAuth = setInterval(() => {
    const auth = Alpine.store('auth');
    if (!auth.loading && auth.authenticated) {
      clearInterval(_checkAuth);
      Alpine.store('ws').connect();
      // Fetch initial pending approvals count
      IntarisAPI.listAudit({ decision: 'escalate', resolved: false, limit: 1 })
        .then(result => {
          Alpine.store('nav').pendingApprovals = result.total || 0;
        })
        .catch(() => {});
    } else if (!auth.loading && !auth.authenticated) {
      clearInterval(_checkAuth);
    }
  }, 100);
});
