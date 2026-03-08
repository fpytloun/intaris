/**
 * Session player — event timeline viewer with live tailing.
 *
 * Provides a scrollable event list for session recordings with:
 * - Paginated loading (load more on scroll)
 * - Event type filtering
 * - Event source filtering (default: agent-only, hides intaris events)
 * - Expandable event details
 * - Live tailing via WebSocket (auto-scroll to latest)
 * - Play/pause mode with configurable speed
 * - Auto-scroll to bottom on new events
 */
function sessionPlayer() {
  return {
    // State
    sessionId: null,
    events: [],
    lastSeq: 0,
    hasMore: false,
    loading: false,
    error: null,
    visible: false,

    // Filtering
    typeFilter: '__all__',
    sourceFilter: '__agent__',

    // Live tail
    liveTail: false,
    ws: null,

    // Player mode
    playing: false,
    playSpeed: 1,
    playIndex: 0,
    playTimer: null,

    // Expanded event
    expandedSeq: null,

    // Pagination
    pageSize: 50,

    // Auto-scroll
    autoScroll: true,

    /**
     * Open the player for a session.
     */
    async open(sessionId) {
      this.sessionId = sessionId;
      this.events = [];
      this.lastSeq = 0;
      this.hasMore = false;
      this.error = null;
      this.expandedSeq = null;
      this.visible = true;
      this.autoScroll = true;
      this.stopLiveTail();
      this.stopPlaying();
      await this.loadEvents();
      this.scrollToBottom();
    },

    /**
     * Close the player.
     */
    close() {
      this.visible = false;
      this.sessionId = null;
      this.events = [];
      this.stopLiveTail();
      this.stopPlaying();
    },

    /**
     * Load events from the API.
     */
    async loadEvents() {
      if (!this.sessionId || this.loading) return;
      this.loading = true;
      this.error = null;

      try {
        const params = {
          after_seq: this.lastSeq,
          limit: this.pageSize,
        };
        if (this.typeFilter !== '__all__') params.type = this.typeFilter;
        if (this.sourceFilter === '__agent__') {
          params.source = 'opencode,claude-code,client';
        } else if (this.sourceFilter !== '__all__') {
          params.source = this.sourceFilter;
        }

        const result = await IntarisAPI.getSessionEvents(this.sessionId, params);
        const newEvents = result.events || [];

        // Deduplicate by seq
        const existingSeqs = new Set(this.events.map(e => e.seq));
        for (const event of newEvents) {
          if (!existingSeqs.has(event.seq)) {
            this.events.push(event);
            existingSeqs.add(event.seq);
          }
        }

        this.lastSeq = result.last_seq || this.lastSeq;
        this.hasMore = result.has_more || false;
      } catch (e) {
        this.error = 'Failed to load events: ' + e.message;
      } finally {
        this.loading = false;
      }

      if (this.autoScroll) this.scrollToBottom();
    },

    /**
     * Load more events (pagination).
     */
    async loadMore() {
      if (this.hasMore && !this.loading) {
        await this.loadEvents();
      }
    },

    /**
     * Reload events with a new filter.
     */
    async applyFilter() {
      this.events = [];
      this.lastSeq = 0;
      this.hasMore = false;
      this.autoScroll = true;
      await this.loadEvents();
    },

    /**
     * Start live tailing via WebSocket.
     */
    startLiveTail() {
      if (this.liveTail || !this.sessionId) return;
      this.liveTail = true;

      this.ws = IntarisAPI.connectWebSocket({
        sessionId: this.sessionId,
        onMessage: (data) => {
          if (data.type === 'session_event' && data.event) {
            const event = data.event;
            // Deduplicate
            if (!this.events.some(e => e.seq === event.seq)) {
              // Apply type filter
              if (this.typeFilter !== '__all__' && event.type !== this.typeFilter) return;
              // Apply source filter
              if (this.sourceFilter === '__agent__') {
                if (!['opencode', 'claude-code', 'client'].includes(event.source)) return;
              } else if (this.sourceFilter !== '__all__') {
                if (event.source !== this.sourceFilter) return;
              }
              this.events.push(event);
              if (event.seq > this.lastSeq) this.lastSeq = event.seq;
              if (this.autoScroll) this.scrollToBottom();
            }
          }
        },
        onClose: () => {
          this.liveTail = false;
          this.ws = null;
        },
        onError: () => {
          this.liveTail = false;
          this.ws = null;
        },
      });
    },

    /**
     * Stop live tailing.
     */
    stopLiveTail() {
      this.liveTail = false;
      if (this.ws) {
        this.ws.close();
        this.ws = null;
      }
    },

    /**
     * Toggle live tailing.
     */
    toggleLiveTail() {
      if (this.liveTail) {
        this.stopLiveTail();
      } else {
        this.startLiveTail();
      }
    },

    // ── Auto-scroll ─────────────────────────────────────────

    scrollToBottom() {
      this.$nextTick(() => {
        const el = this.$refs.eventList;
        if (el) el.scrollTop = el.scrollHeight;
      });
    },

    handleScroll() {
      const el = this.$refs.eventList;
      if (!el) return;
      // Consider "at bottom" if within 50px of the end
      const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 50;
      this.autoScroll = atBottom;
    },

    // ── Player mode ──────────────────────────────────────────

    startPlaying() {
      if (this.playing || this.events.length === 0) return;
      this.playing = true;
      this.playIndex = 0;
      this._scheduleNext();
    },

    stopPlaying() {
      this.playing = false;
      if (this.playTimer) {
        clearTimeout(this.playTimer);
        this.playTimer = null;
      }
    },

    togglePlaying() {
      if (this.playing) {
        this.stopPlaying();
      } else {
        this.startPlaying();
      }
    },

    setSpeed(speed) {
      this.playSpeed = speed;
    },

    _scheduleNext() {
      if (!this.playing || this.playIndex >= this.events.length) {
        this.stopPlaying();
        return;
      }

      // Calculate delay based on timestamp difference
      let delayMs = 500; // default
      if (this.playIndex > 0 && this.playIndex < this.events.length) {
        const prev = this.events[this.playIndex - 1];
        const curr = this.events[this.playIndex];
        if (prev.ts && curr.ts) {
          const diff = new Date(curr.ts) - new Date(prev.ts);
          delayMs = Math.max(50, Math.min(diff / this.playSpeed, 3000));
        }
      }

      this.playTimer = setTimeout(() => {
        this.expandedSeq = this.events[this.playIndex]?.seq;
        this.playIndex++;
        if (this.autoScroll) this.scrollToBottom();
        this._scheduleNext();
      }, delayMs);
    },

    // ── Event display helpers ────────────────────────────────

    toggleExpand(event) {
      if (this.expandedSeq === event.seq) {
        this.expandedSeq = null;
      } else {
        this.expandedSeq = event.seq;
      }
    },

    eventTypeBadge(type) {
      const classes = {
        message: 'badge-approve',
        tool_call: 'badge-escalate',
        tool_result: 'badge-fast',
        evaluation: 'badge-deny',
        part: 'badge-low',
        lifecycle: 'badge-medium',
        checkpoint: 'badge-high',
        reasoning: 'badge-approve',
        transcript: 'badge-low',
      };
      return 'badge ' + (classes[type] || 'badge-low');
    },

    eventSummary(event) {
      const data = event.data || {};
      switch (event.type) {
        case 'tool_call':
          return data.tool || 'tool call';
        case 'tool_result':
          return (data.tool || 'result') + (data.isError ? ' (error)' : '');
        case 'evaluation':
          return `${data.tool || '?'}: ${data.decision || '?'} (${data.risk || '?'})`;
        case 'message':
          if (data.role === 'user') return 'User: ' + (data.text || '');
          return (data.role || 'message') + (data.model ? ` [${data.model}]` : '');
        case 'part':
          return (data.part?.type || 'part') + (data.part?.text ? ': ' + data.part.text : '');
        case 'lifecycle':
          return data.event_type || data.status || 'lifecycle';
        case 'checkpoint':
          return data.content || '';
        case 'reasoning':
          return data.content || '';
        case 'transcript':
          return data.type || 'transcript entry';
        default:
          return event.type;
      }
    },

    eventDetail(event) {
      return JSON.stringify(event.data || {}, null, 2);
    },

    formatTime(ts) {
      if (!ts) return '';
      return new Date(ts).toLocaleTimeString();
    },

    get eventCount() {
      return this.events.length;
    },

    get filteredTypes() {
      return [
        '__all__', 'message', 'tool_call', 'tool_result', 'evaluation',
        'part', 'lifecycle', 'checkpoint', 'reasoning', 'transcript',
      ];
    },

    get filteredSources() {
      return ['__all__', '__agent__', 'intaris', 'opencode', 'claude-code', 'client'];
    },

    sourceLabel(s) {
      if (s === '__all__') return 'All sources';
      if (s === '__agent__') return 'Agent only';
      return s;
    },
  };
}
