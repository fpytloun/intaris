/**
 * Settings tab — server configuration display + notification channel management.
 */
function settingsTab() {
  return {
    initialized: false,
    loading: false,
    config: null,

    // Notification channels
    channels: [],
    channelsLoading: false,
    showChannelForm: false,
    editingChannel: null,
    channelForm: {
      name: '',
      provider: 'pushover',
      enabled: true,
      config: {},
      useCustomEvents: false,
      events: {},
    },
    testing: null,

    init() {
      window.addEventListener('intaris:tab-changed', (e) => {
        if (e.detail.tab === 'settings' && !this.initialized) {
          this.initialized = true;
          this.load();
          this.loadChannels();
        }
      });
    },

    async load() {
      this.loading = true;
      try {
        this.config = await IntarisAPI.config();
      } catch (e) {
        Alpine.store('notify').error('Failed to load config: ' + e.message);
      } finally {
        this.loading = false;
      }
    },

    // ── Notification Channels ──────────────────────────────────

    async loadChannels() {
      this.channelsLoading = true;
      try {
        const result = await IntarisAPI.listNotificationChannels();
        this.channels = result.items || [];
      } catch (e) {
        Alpine.store('notify').error('Failed to load channels: ' + e.message);
      } finally {
        this.channelsLoading = false;
      }
    },

    /** All supported event types with labels and default-on state */
    allEventTypes() {
      return [
        { key: 'escalation', label: 'Escalations', desc: 'Tool call requires approval', defaultOn: true },
        { key: 'resolution', label: 'Resolutions', desc: 'Escalation resolved', defaultOn: true },
        { key: 'session_suspended', label: 'Session suspended', desc: 'Session was suspended', defaultOn: true },
        { key: 'denial', label: 'Denials', desc: 'Tool call denied', defaultOn: false },
        { key: 'summary_alert', label: 'Summary alerts (L2)', desc: 'Misaligned or high-risk session summary', defaultOn: false },
        { key: 'analysis_alert', label: 'Analysis alerts (L3)', desc: 'High/critical behavioral risk from cross-session analysis', defaultOn: false },
      ];
    },

    /** Build default events checkbox state */
    _defaultEventsState() {
      const state = {};
      for (const et of this.allEventTypes()) {
        state[et.key] = et.defaultOn;
      }
      return state;
    },

    /** Build events checkbox state from an array of event keys */
    _eventsFromArray(arr) {
      const state = {};
      for (const et of this.allEventTypes()) {
        state[et.key] = arr.includes(et.key);
      }
      return state;
    },

    /** Convert events checkbox state to array (or null for defaults) */
    _eventsToPayload() {
      if (!this.channelForm.useCustomEvents) return undefined;
      const selected = [];
      for (const et of this.allEventTypes()) {
        if (this.channelForm.events[et.key]) selected.push(et.key);
      }
      return selected;
    },

    /** Format event count label for channel list */
    channelEventsLabel(ch) {
      if (!ch.events) return 'Default events';
      return ch.events.length + '/' + this.allEventTypes().length + ' events';
    },

    openAddChannel() {
      this.editingChannel = null;
      this.channelForm = {
        name: '',
        provider: 'pushover',
        enabled: true,
        config: {},
        useCustomEvents: false,
        events: this._defaultEventsState(),
      };
      this.showChannelForm = true;
    },

    openEditChannel(ch) {
      this.editingChannel = ch.name;
      const hasCustomEvents = Array.isArray(ch.events);
      this.channelForm = {
        name: ch.name,
        provider: ch.provider,
        enabled: ch.enabled,
        config: {},
        useCustomEvents: hasCustomEvents,
        events: hasCustomEvents ? this._eventsFromArray(ch.events) : this._defaultEventsState(),
      };
      this.showChannelForm = true;
    },

    cancelChannelForm() {
      this.showChannelForm = false;
      this.editingChannel = null;
    },

    /** Get the config fields for the selected provider */
    providerFields() {
      const p = this.channelForm.provider;
      if (p === 'webhook') return [
        { key: 'url', label: 'Webhook URL', type: 'url', required: true },
        { key: 'secret', label: 'Signing Secret', type: 'password', required: false },
      ];
      if (p === 'pushover') return [
        { key: 'user_key', label: 'User Key', type: 'text', required: true },
        { key: 'app_token', label: 'App Token', type: 'password', required: true },
        { key: 'priority', label: 'Priority (-2 to 2)', type: 'number', required: false },
        { key: 'device', label: 'Device', type: 'text', required: false },
      ];
      if (p === 'slack') return [
        { key: 'webhook_url', label: 'Slack Webhook URL', type: 'url', required: true },
      ];
      return [];
    },

    async saveChannel() {
      const name = this.editingChannel || this.channelForm.name;
      if (!name) {
        Alpine.store('notify').error('Channel name is required');
        return;
      }

      // Build config from form fields, omitting empty values
      const config = {};
      for (const f of this.providerFields()) {
        const val = this.channelForm.config[f.key];
        if (val !== undefined && val !== '') {
          config[f.key] = f.type === 'number' ? Number(val) : val;
        }
      }

      try {
        const payload = {
          provider: this.channelForm.provider,
          enabled: this.channelForm.enabled,
          config: Object.keys(config).length > 0 ? config : undefined,
        };
        const events = this._eventsToPayload();
        if (events !== undefined) payload.events = events;
        await IntarisAPI.upsertNotificationChannel(name, payload);
        Alpine.store('notify').success('Channel saved');
        this.showChannelForm = false;
        this.editingChannel = null;
        await this.loadChannels();
      } catch (e) {
        Alpine.store('notify').error('Failed to save channel: ' + e.message);
      }
    },

    async deleteChannel(name) {
      if (!confirm(`Delete notification channel "${name}"?`)) return;
      try {
        await IntarisAPI.deleteNotificationChannel(name);
        Alpine.store('notify').success('Channel deleted');
        await this.loadChannels();
      } catch (e) {
        Alpine.store('notify').error('Failed to delete channel: ' + e.message);
      }
    },

    async testChannel(name) {
      this.testing = name;
      try {
        await IntarisAPI.testNotificationChannel(name);
        Alpine.store('notify').success('Test notification sent');
      } catch (e) {
        Alpine.store('notify').error('Test failed: ' + e.message);
      } finally {
        this.testing = null;
      }
    },

    async toggleChannel(ch) {
      try {
        await IntarisAPI.upsertNotificationChannel(ch.name, {
          provider: ch.provider,
          enabled: !ch.enabled,
        });
        await this.loadChannels();
      } catch (e) {
        Alpine.store('notify').error('Failed to toggle channel: ' + e.message);
      }
    },

    channelHealthLabel(ch) {
      if (ch.failure_count > 5) return 'Failing';
      if (ch.failure_count > 0) return 'Degraded';
      if (ch.last_success_at) return 'Healthy';
      return 'Untested';
    },

    channelHealthColor(ch) {
      if (ch.failure_count > 5) return 'text-red-400';
      if (ch.failure_count > 0) return 'text-yellow-400';
      if (ch.last_success_at) return 'text-green-400';
      return 'text-gray-400';
    },
  };
}
