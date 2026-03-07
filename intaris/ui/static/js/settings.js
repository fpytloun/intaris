/**
 * Settings tab — read-only server configuration display.
 */
function settingsTab() {
  return {
    initialized: false,
    loading: false,
    config: null,

    init() {
      window.addEventListener('intaris:tab-changed', (e) => {
        if (e.detail.tab === 'settings' && !this.initialized) {
          this.initialized = true;
          this.load();
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
  };
}
