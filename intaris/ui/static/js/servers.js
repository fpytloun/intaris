/**
 * Servers tab — MCP upstream server management.
 *
 * Lists configured MCP servers, allows adding/editing/deleting servers
 * and managing per-tool preference overrides.
 */
function serversTab() {
  return {
    initialized: false,
    loading: false,
    servers: [],
    expandedName: null,
    expandedPrefs: {},

    // Add/Edit form state
    showForm: false,
    editMode: false,
    form: {
      name: '',
      transport: 'streamable-http',
      command: '',
      args: '',
      env: '',
      cwd: '',
      url: '',
      headers: '',
      agent_pattern: '*',
      enabled: true,
    },

    init() {
      window.addEventListener('intaris:tab-changed', (e) => {
        if (e.detail.tab === 'servers' && !this.initialized) {
          this.initialized = true;
          this.load();
        }
      });
    },

    async load() {
      this.loading = true;
      try {
        const data = await IntarisAPI.listMCPServers();
        this.servers = data.items || [];
      } catch (e) {
        Alpine.store('notify').error('Failed to load servers: ' + e.message);
      } finally {
        this.loading = false;
      }
    },

    async toggleExpand(server) {
      if (this.expandedName === server.name) {
        this.expandedName = null;
        return;
      }
      this.expandedName = server.name;
      // Load tool preferences for this server
      try {
        const data = await IntarisAPI.getMCPToolPreferences(server.name);
        this.expandedPrefs = data.preferences || {};
      } catch (e) {
        this.expandedPrefs = {};
      }
    },

    // ── Form ──────────────────────────────────────────────────

    openAddForm() {
      this.editMode = false;
      this.form = {
        name: '',
        transport: 'streamable-http',
        command: '',
        args: '',
        env: '',
        cwd: '',
        url: '',
        headers: '',
        agent_pattern: '*',
        enabled: true,
      };
      this.showForm = true;
    },

    openEditForm(server) {
      this.editMode = true;
      this.form = {
        name: server.name,
        transport: server.transport,
        command: server.command || '',
        args: Array.isArray(server.args) ? server.args.join(' ') : '',
        env: '',  // Never shown (encrypted)
        cwd: server.cwd || '',
        url: server.url || '',
        headers: '',  // Never shown (encrypted)
        agent_pattern: server.agent_pattern || '*',
        enabled: server.enabled,
      };
      this.showForm = true;
    },

    async saveServer() {
      try {
        const body = {
          name: this.form.name,
          transport: this.form.transport,
          agent_pattern: this.form.agent_pattern,
          enabled: this.form.enabled,
        };

        if (this.form.transport === 'stdio') {
          body.command = this.form.command || null;
          body.args = this.form.args ? this.form.args.split(/\s+/) : null;
          body.cwd = this.form.cwd || null;
          if (this.form.env) {
            try {
              body.env = JSON.parse(this.form.env);
            } catch {
              Alpine.store('notify').error('Invalid JSON in environment variables');
              return;
            }
          }
        } else {
          body.url = this.form.url || null;
          if (this.form.headers) {
            try {
              body.headers = JSON.parse(this.form.headers);
            } catch {
              Alpine.store('notify').error('Invalid JSON in headers');
              return;
            }
          }
        }

        await IntarisAPI.upsertMCPServer(this.form.name, body);
        Alpine.store('notify').success(
          this.editMode ? 'Server updated' : 'Server created'
        );
        this.showForm = false;
        await this.load();
      } catch (e) {
        Alpine.store('notify').error('Failed to save server: ' + e.message);
      }
    },

    async deleteServer(name) {
      if (!confirm(`Delete server "${name}"? This also removes all tool preferences.`)) return;
      try {
        await IntarisAPI.deleteMCPServer(name);
        Alpine.store('notify').success('Server deleted');
        if (this.expandedName === name) this.expandedName = null;
        await this.load();
      } catch (e) {
        Alpine.store('notify').error('Failed to delete server: ' + e.message);
      }
    },

    async toggleEnabled(server) {
      try {
        await IntarisAPI.upsertMCPServer(server.name, {
          ...server,
          enabled: !server.enabled,
        });
        await this.load();
      } catch (e) {
        Alpine.store('notify').error('Failed to toggle server: ' + e.message);
      }
    },

    // ── Tool Preferences ──────────────────────────────────────

    async setPreference(serverName, toolName, preference) {
      try {
        await IntarisAPI.setMCPToolPreference(serverName, toolName, preference);
        this.expandedPrefs[toolName] = preference;
        Alpine.store('notify').success(`${toolName}: ${preference}`);
      } catch (e) {
        Alpine.store('notify').error('Failed to set preference: ' + e.message);
      }
    },

    async resetPreference(serverName, toolName) {
      try {
        await IntarisAPI.deleteMCPToolPreference(serverName, toolName);
        delete this.expandedPrefs[toolName];
        Alpine.store('notify').success(`${toolName}: reset to default`);
      } catch (e) {
        Alpine.store('notify').error('Failed to reset preference: ' + e.message);
      }
    },

    getPreference(toolName) {
      return this.expandedPrefs[toolName] || 'evaluate';
    },

    prefBadgeClass(pref) {
      const map = {
        'auto-approve': 'badge badge-approve',
        'evaluate': 'badge',
        'escalate': 'badge badge-escalate',
        'deny': 'badge badge-deny',
      };
      return map[pref] || 'badge';
    },

    transportLabel(transport) {
      const map = {
        'stdio': 'stdio',
        'streamable-http': 'HTTP',
        'sse': 'SSE',
      };
      return map[transport] || transport;
    },
  };
}
