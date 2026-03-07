/**
 * Intaris UI — Alpine.js stores and app initialization.
 *
 * Stores:
 * - auth: login state, API key, identity, user switching
 * - nav: active tab navigation
 * - notify: toast notification system
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

    async login(apiKey) {
      this.error = '';
      this.loading = true;
      IntarisAPI.setKey(apiKey);
      try {
        const identity = await IntarisAPI.whoami();
        this.identity = identity;
        this.authenticated = true;
        if (identity.can_switch_user) {
          await this.loadUsers();
          if (!identity.user_id && this.users.length > 0) {
            this.switchUser(this.users[0]);
          } else {
            this.selectedUser = identity.user_id || '';
            IntarisAPI.setSelectedUser('');
          }
        } else {
          this.selectedUser = identity.user_id || '';
          IntarisAPI.setSelectedUser('');
        }
      } catch (e) {
        IntarisAPI.clearKey();
        this.authenticated = false;
        this.error = e.message === 'Unauthorized'
          ? 'Invalid API key'
          : `Connection failed: ${e.message}`;
      } finally {
        this.loading = false;
      }
    },

    async verify() {
      this.loading = true;
      try {
        const identity = await IntarisAPI.whoami();
        this.identity = identity;
        this.authenticated = true;
        if (identity.can_switch_user) {
          await this.loadUsers();
          const stored = IntarisAPI.getSelectedUser();
          if (stored) {
            this.selectedUser = stored;
          } else if (identity.user_id) {
            this.selectedUser = identity.user_id;
          } else if (this.users.length > 0) {
            this.switchUser(this.users[0]);
          }
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
    },
  });

  // ── Nav Store ────────────────────────────────────────────────
  Alpine.store('nav', {
    activeTab: 'dashboard',

    setTab(tab) {
      this.activeTab = tab;
      window.dispatchEvent(new CustomEvent('intaris:tab-changed', {
        detail: { tab }
      }));
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
});
