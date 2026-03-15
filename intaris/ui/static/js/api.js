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
      let msg;
      if (Array.isArray(body.detail)) {
        // Pydantic validation errors — extract human-readable messages.
        msg = body.detail.map(e => e.msg || JSON.stringify(e)).join('; ');
      } else {
        msg = body.detail || body.error || `HTTP ${response.status}`;
      }
      throw new Error(msg);
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

  stats(params = {}) {
    return this.get('/stats', params);
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

  refreshMCPServerTools(name) {
    return this.post(`/mcp/servers/${encodeURIComponent(name)}/refresh`);
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

  // ── Notification Channels ──────────────────────────────────

  listNotificationChannels() {
    return this.get('/notifications/channels');
  },

  getNotificationChannel(name) {
    return this.get(`/notifications/channels/${encodeURIComponent(name)}`);
  },

  upsertNotificationChannel(name, body) {
    return this.put(`/notifications/channels/${encodeURIComponent(name)}`, body);
  },

  deleteNotificationChannel(name) {
    return this.del(`/notifications/channels/${encodeURIComponent(name)}`);
  },

  testNotificationChannel(name) {
    return this.post(`/notifications/channels/${encodeURIComponent(name)}/test`);
  },

  // ── Session Events (Recording) ──────────────────────────────

  /**
   * Read events from a session's event log.
   * @param {string} sessionId
   * @param {Object} params - { after_seq, limit, type }
   */
  getSessionEvents(sessionId, params = {}) {
    const qs = new URLSearchParams();
    if (params.after_seq) qs.set('after_seq', params.after_seq);
    if (params.limit) qs.set('limit', params.limit);
    if (params.type) qs.set('type', params.type);
    if (params.source) qs.set('source', params.source);
    if (params.exclude_source) qs.set('exclude_source', params.exclude_source);
    if (params.after_ts) qs.set('after_ts', params.after_ts);
    if (params.before_ts) qs.set('before_ts', params.before_ts);
    const query = qs.toString();
    return this.get(`/session/${encodeURIComponent(sessionId)}/events${query ? '?' + query : ''}`);
  },

  /**
   * Flush buffered events for a session.
   * @param {string} sessionId
   */
  flushSessionEvents(sessionId) {
    return this.post(`/session/${encodeURIComponent(sessionId)}/events/flush`);
  },

  // ── Behavioral Analysis ──────────────────────────────────────

  /**
   * Get session summaries (Intaris + agent-reported).
   * @param {string} sessionId
   */
  getSessionSummaries(sessionId) {
    return this.get(`/session/${encodeURIComponent(sessionId)}/summary`);
  },

  /**
   * Trigger summary generation for a session.
   * @param {string} sessionId
   */
  triggerSessionSummary(sessionId) {
    return this.post(`/session/${encodeURIComponent(sessionId)}/summary/trigger`);
  },

  /**
   * Get behavioral risk profile.
   * @param {Object} params - { agent_id }
   */
  getProfile(params = {}) {
    return this.get('/profile', params);
  },

  /**
   * List behavioral analyses.
   * @param {Object} params - { agent_id, page, limit }
   */
  listAnalyses(params = {}) {
    return this.get('/analysis', params);
  },

  /**
   * Trigger cross-session behavioral analysis.
   * @param {Object} params - { agent_id }
   */
  triggerAnalysis(params = {}) {
    const qs = new URLSearchParams();
    if (params.agent_id) qs.set('agent_id', params.agent_id);
    const query = qs.toString();
    return this.post(`/analysis/trigger${query ? '?' + query : ''}`);
  },

  // ── WebSocket ───────────────────────────────────────────────

  /**
   * Open a WebSocket connection to /api/v1/stream with first-message auth.
   *
   * The caller owns the returned WebSocket and is responsible for
   * calling ws.close() when done. The connection uses the same API key
   * and user identity as REST calls.
   *
   * @param {Object} options
   * @param {string|null} options.sessionId - Optional session filter (null = all sessions)
   * @param {Function} options.onMessage - Called with parsed event data (pings are filtered)
   * @param {Function} options.onOpen - Called after auth message is sent
   * @param {Function} options.onClose - Called on connection close
   * @param {Function} options.onError - Called on connection error
   * @returns {WebSocket} The WebSocket instance
   */
  connectWebSocket({ sessionId = null, onMessage, onOpen, onClose, onError } = {}) {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${proto}//${location.host}/api/v1/stream`);

    ws.onopen = () => {
      const authMsg = { type: 'auth', token: `Bearer ${this.getKey()}` };
      const userId = this.getSelectedUser();
      if (userId) authMsg.user_id = userId;
      if (sessionId) authMsg.session_id = sessionId;
      ws.send(JSON.stringify(authMsg));
      if (onOpen) onOpen();
    };

    ws.onmessage = (e) => {
      let data;
      try { data = JSON.parse(e.data); } catch { return; }
      if (data.type === 'ping') return;
      if (onMessage) onMessage(data);
    };

    ws.onclose = onClose || null;
    ws.onerror = onError || null;
    return ws;
  },
};
