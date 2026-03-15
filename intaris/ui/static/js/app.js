/**
 * Intaris UI — Alpine.js stores and app initialization.
 *
 * Stores:
 * - auth: login state, API key, identity, user switching
 * - nav: active tab navigation
 * - notify: toast notification system
 * - ws: shared WebSocket connection for real-time updates
 */

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
      const key = IntarisAPI.getKey();
      if (key) {
        await this.verify();
      } else {
        this.loading = false;
      }
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
      window.dispatchEvent(new CustomEvent('intaris:agent-changed', {
        detail: { agentId }
      }));
    },

    // ── Session modal ───────────────────────────────────────

    async openSessionModal(sessionId) {
      if (!sessionId) return;
      this.sessionModalLoading = true;
      this.sessionModal = null;
      this.sessionModalAudit = [];
      this.sessionModalChildren = [];
      this.sessionModalAuditExpandedId = null;
      this.sessionModalAuditRecord = null;
      try {
        const [session, audit, children] = await Promise.all([
          IntarisAPI.getSession(sessionId),
          IntarisAPI.listAudit({ session_id: sessionId, limit: 20 }),
          IntarisAPI.listSessions({ parent_session_id: sessionId, limit: 50 }),
        ]);
        this.sessionModal = session;
        this.sessionModalAudit = audit.items || [];
        this.sessionModalChildren = children.items || [];
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
