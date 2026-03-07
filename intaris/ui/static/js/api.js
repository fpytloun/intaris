/**
 * Intaris API client — auth-aware fetch wrapper.
 *
 * Stores the API key in localStorage and sends it as X-API-Key
 * on every request. Supports X-User-Id for multi-user switching.
 * Auto-triggers logout on 401 responses.
 */

const IntarisAPI = {
  /** Base URL for API calls (same origin) */
  baseUrl: '/api/v1',

  /** Get stored API key */
  getKey() {
    return localStorage.getItem('intaris_api_key') || '';
  },

  /** Store API key */
  setKey(key) {
    localStorage.setItem('intaris_api_key', key);
  },

  /** Clear stored key */
  clearKey() {
    localStorage.removeItem('intaris_api_key');
  },

  /** Get the currently selected user_id for switching */
  getSelectedUser() {
    return localStorage.getItem('intaris_selected_user') || '';
  },

  /** Set the selected user_id for switching */
  setSelectedUser(userId) {
    if (userId) {
      localStorage.setItem('intaris_selected_user', userId);
    } else {
      localStorage.removeItem('intaris_selected_user');
    }
  },

  /**
   * Build request headers with auth and identity.
   */
  _headers(extra = {}) {
    const headers = {
      'Content-Type': 'application/json',
      ...extra,
    };
    const key = this.getKey();
    if (key) {
      headers['X-API-Key'] = key;
    }
    const selectedUser = this.getSelectedUser();
    if (selectedUser) {
      headers['X-User-Id'] = selectedUser;
    }
    return headers;
  },

  /**
   * Core fetch wrapper with error handling.
   */
  async _fetch(path, options = {}) {
    const url = `${this.baseUrl}${path}`;
    const { headers: extraHeaders, ...rest } = options;
    const response = await fetch(url, {
      ...rest,
      headers: this._headers(extraHeaders),
    });

    if (response.status === 401) {
      if (window.Alpine) {
        const store = Alpine.store('auth');
        if (store) store.logout();
      }
      throw new Error('Unauthorized');
    }

    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail || body.error || `HTTP ${response.status}`);
    }

    return response.json();
  },

  /** GET with query params */
  async get(path, params = {}) {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) {
      if (v !== null && v !== undefined && v !== '') {
        qs.append(k, String(v));
      }
    }
    const query = qs.toString();
    return this._fetch(query ? `${path}?${query}` : path);
  },

  /** POST with JSON body */
  async post(path, body = {}) {
    return this._fetch(path, {
      method: 'POST',
      body: JSON.stringify(body),
    });
  },

  /** PATCH with JSON body */
  async patch(path, body = {}) {
    return this._fetch(path, {
      method: 'PATCH',
      body: JSON.stringify(body),
    });
  },

  // ── Convenience methods ──────────────────────────────────

  whoami() {
    return this.get('/whoami');
  },

  stats() {
    return this.get('/stats');
  },

  config() {
    return this.get('/config');
  },

  listSessions(params = {}) {
    return this.get('/sessions', params);
  },

  getSession(sessionId) {
    return this.get(`/session/${encodeURIComponent(sessionId)}`);
  },

  updateStatus(sessionId, status) {
    return this.patch(`/session/${encodeURIComponent(sessionId)}/status`, { status });
  },

  listAudit(params = {}) {
    return this.get('/audit', params);
  },

  getAuditRecord(callId) {
    return this.get(`/audit/${encodeURIComponent(callId)}`);
  },

  resolveDecision(callId, decision, note) {
    return this.post('/decision', { call_id: callId, decision, note: note || null });
  },

  // ── MCP Server Management ───────────────────────────────

  /** PUT with JSON body */
  async put(path, body = {}) {
    return this._fetch(path, {
      method: 'PUT',
      body: JSON.stringify(body),
    });
  },

  /** DELETE */
  async del(path) {
    return this._fetch(path, { method: 'DELETE' });
  },

  listMCPServers(params = {}) {
    return this.get('/mcp/servers', params);
  },

  getMCPServer(name) {
    return this.get(`/mcp/servers/${encodeURIComponent(name)}`);
  },

  upsertMCPServer(name, body) {
    return this.put(`/mcp/servers/${encodeURIComponent(name)}`, body);
  },

  deleteMCPServer(name) {
    return this.del(`/mcp/servers/${encodeURIComponent(name)}`);
  },

  getMCPToolPreferences(serverName) {
    return this.get(`/mcp/servers/${encodeURIComponent(serverName)}/preferences`);
  },

  setMCPToolPreference(serverName, toolName, preference) {
    return this.put(
      `/mcp/servers/${encodeURIComponent(serverName)}/preferences/${encodeURIComponent(toolName)}`,
      { preference }
    );
  },

  deleteMCPToolPreference(serverName, toolName) {
    return this.del(
      `/mcp/servers/${encodeURIComponent(serverName)}/preferences/${encodeURIComponent(toolName)}`
    );
  },
};
