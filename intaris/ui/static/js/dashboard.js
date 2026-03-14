/**
 * Dashboard tab — overview stats, charts, and recent activity.
 *
 * Uses Chart.js for doughnut/bar/line visualizations.
 * Subscribes to WebSocket events for live counter updates.
 */

/* global Alpine, IntarisAPI, Chart */

// ── Brand color palette ──────────────────────────────────────────────

const CHART_COLORS = {
  cyan:    '#22D3EE',
  teal:    '#2DD4BF',
  green:   '#34D399',
  amber:   '#FBBF24',
  orange:  '#FB923C',
  red:     '#F87171',
  purple:  '#A78BFA',
  pink:    '#F472B6',
  slate:   '#94A3B8',
  muted:   '#64748B',
  border:  '#1E293B',
  surface: '#121A2B',
  text:    '#E6EDF3',
};

// Semantic color maps for each chart type
const DECISION_COLORS = {
  approve:  CHART_COLORS.cyan,
  deny:     CHART_COLORS.red,
  escalate: CHART_COLORS.amber,
};

const RISK_COLORS = {
  low:      CHART_COLORS.cyan,
  medium:   CHART_COLORS.amber,
  high:     CHART_COLORS.orange,
  critical: CHART_COLORS.red,
};

const PATH_COLORS = {
  fast:     CHART_COLORS.cyan,
  llm:      CHART_COLORS.purple,
  critical: CHART_COLORS.red,
};

const SESSION_COLORS = {
  active:     CHART_COLORS.cyan,
  idle:       CHART_COLORS.slate,
  completed:  CHART_COLORS.green,
  suspended:  CHART_COLORS.amber,
  terminated: CHART_COLORS.red,
};

const CLASSIFICATION_COLORS = {
  read:     CHART_COLORS.cyan,
  write:    CHART_COLORS.purple,
  critical: CHART_COLORS.red,
  escalate: CHART_COLORS.amber,
};

// ── Chart.js global defaults ─────────────────────────────────────────

if (typeof Chart !== 'undefined') {
  Chart.defaults.color = CHART_COLORS.text;
  Chart.defaults.borderColor = CHART_COLORS.border;
  Chart.defaults.font.family = 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace';
  Chart.defaults.font.size = 11;
  Chart.defaults.plugins.legend.labels.usePointStyle = true;
  Chart.defaults.plugins.legend.labels.pointStyle = 'circle';
  Chart.defaults.plugins.legend.labels.padding = 12;
  Chart.defaults.plugins.tooltip.backgroundColor = CHART_COLORS.surface;
  Chart.defaults.plugins.tooltip.borderColor = CHART_COLORS.border;
  Chart.defaults.plugins.tooltip.borderWidth = 1;
  Chart.defaults.plugins.tooltip.cornerRadius = 6;
  Chart.defaults.plugins.tooltip.padding = 8;
  Chart.defaults.plugins.tooltip.titleFont = { weight: 'normal', size: 11 };
  Chart.defaults.plugins.tooltip.bodyFont = { size: 12 };
}

// ── Center-text plugin for doughnut charts ───────────────────────────

const centerTextPlugin = {
  id: 'centerText',
  afterDraw(chart) {
    const centerText = chart.options.plugins?.centerText;
    if (!centerText?.text) return;

    const { ctx, chartArea } = chart;
    if (!ctx || !chartArea) return;

    const { left, right, top, bottom } = chartArea;
    const cx = (left + right) / 2;
    const cy = (top + bottom) / 2;

    ctx.save();
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';

    // Value (large)
    ctx.font = 'bold 20px ' + Chart.defaults.font.family;
    ctx.fillStyle = CHART_COLORS.text;
    ctx.fillText(centerText.text, cx, cy - 6);

    // Label (small)
    if (centerText.subtext) {
      ctx.font = '10px ' + Chart.defaults.font.family;
      ctx.fillStyle = CHART_COLORS.muted;
      ctx.fillText(centerText.subtext, cx, cy + 12);
    }

    ctx.restore();
  },
};

if (typeof Chart !== 'undefined') {
  Chart.register(centerTextPlugin);
}

// ── Dashboard component ──────────────────────────────────────────────

function dashboardTab() {
  return {
    initialized: false,
    loading: false,
    stats: null,
    recentActivity: [],

    // Chart instances (for cleanup)
    _charts: {},
    _refreshTimer: null,

    init() {
      window.addEventListener('intaris:tab-changed', (e) => {
        if (e.detail.tab === 'dashboard') {
          this.load();
          this._startPeriodicRefresh();
        } else {
          this._stopPeriodicRefresh();
          this._destroyAllCharts();
        }
      });
      window.addEventListener('intaris:user-changed', () => {
        if (this.initialized) this.load();
      });
      window.addEventListener('intaris:agent-changed', () => {
        if (this.initialized) this.load();
      });
      window.addEventListener('intaris:logout', () => {
        this._stopPeriodicRefresh();
        this._destroyAllCharts();
      });

      // Subscribe to WebSocket events for live updates
      window.addEventListener('intaris:ws-message', (e) => {
        this._handleWsEvent(e.detail);
      });

      // Auto-load on first render + start periodic refresh
      this.load();
      this._startPeriodicRefresh();
    },

    _startPeriodicRefresh() {
      this._stopPeriodicRefresh();
      this._refreshTimer = setInterval(() => {
        if (Alpine.store('nav').activeTab === 'dashboard') this.load();
      }, 60000);
    },

    _stopPeriodicRefresh() {
      if (this._refreshTimer) {
        clearInterval(this._refreshTimer);
        this._refreshTimer = null;
      }
    },

    _handleWsEvent(data) {
      if (!this.stats) return;

      if (data.type === 'evaluated') {
        // Increment total evaluations
        this.stats.total_evaluations = (this.stats.total_evaluations || 0) + 1;

        // Update decision distribution
        if (!this.stats.decisions) this.stats.decisions = {};
        const d = data.decision;
        this.stats.decisions[d] = (this.stats.decisions[d] || 0) + 1;

        // Update risk distribution
        if (data.risk) {
          if (!this.stats.risk_distribution) this.stats.risk_distribution = {};
          this.stats.risk_distribution[data.risk] = (this.stats.risk_distribution[data.risk] || 0) + 1;
        }

        // Update path distribution
        if (data.path) {
          if (!this.stats.path_distribution) this.stats.path_distribution = {};
          this.stats.path_distribution[data.path] = (this.stats.path_distribution[data.path] || 0) + 1;
        }

        // Update pending approvals count
        if (d === 'escalate') {
          this.stats.pending_approvals = (this.stats.pending_approvals || 0) + 1;
        }

        // Recalculate approval rate
        const total = this.stats.total_evaluations || 1;
        const approved = this.stats.decisions.approve || 0;
        this.stats.approval_rate = Math.round((approved / total) * 100);

        // Prepend to recent activity (keep last 10).
        const callId = data.call_id;
        if (callId) {
          this.recentActivity = [
            {
              call_id: callId,
              decision: data.decision,
              tool: data.tool,
              record_type: data.record_type || 'tool_call',
              risk: data.risk,
              session_id: data.session_id,
              timestamp: data.timestamp || new Date().toISOString(),
              evaluation_path: data.path,
              latency_ms: data.latency_ms,
            },
            ...this.recentActivity.filter(r => r.call_id !== callId),
          ].slice(0, 10);
        }

        // Live-update charts only when dashboard tab is visible
        if (Alpine.store('nav').activeTab === 'dashboard') {
          this._updateDoughnut('decisionsChart', this.stats.decisions, DECISION_COLORS);
          this._updateDoughnut('risksChart', this.stats.risk_distribution, RISK_COLORS);
          this._updateDoughnut('pathsChart', this.stats.path_distribution, PATH_COLORS);
        }
      }

      if (data.type === 'decided') {
        if (this.stats.pending_approvals > 0) {
          this.stats.pending_approvals--;
        }
      }

      if (data.type === 'session_created') {
        this.stats.total_sessions = (this.stats.total_sessions || 0) + 1;
        if (!this.stats.sessions_by_status) this.stats.sessions_by_status = {};
        this.stats.sessions_by_status.active = (this.stats.sessions_by_status.active || 0) + 1;
        if (Alpine.store('nav').activeTab === 'dashboard') {
          this._updateDoughnut('sessionsChart', this.stats.sessions_by_status, SESSION_COLORS);
        }
      }
    },

    async load() {
      this.loading = true;
      try {
        const agentFilter = Alpine.store('nav').selectedAgent;
        const statsParams = agentFilter ? { agent_id: agentFilter } : {};
        const auditParams = { limit: 10 };
        if (agentFilter) auditParams.agent_id = agentFilter;

        const [stats, audit] = await Promise.all([
          IntarisAPI.stats(statsParams),
          IntarisAPI.listAudit(auditParams),
        ]);
        this.stats = stats;
        this.recentActivity = audit.items || [];
        this.initialized = true;

        // Update agents list in nav store (always from unfiltered stats)
        if (stats.agents) {
          Alpine.store('nav').agents = stats.agents;
        }

        // Render charts after data is loaded (next tick for DOM readiness)
        this.$nextTick(() => this._renderAllCharts());
      } catch (e) {
        Alpine.store('notify').error('Failed to load dashboard: ' + e.message);
      } finally {
        this.loading = false;
      }
    },

    // ── Chart rendering ────────────────────────────────────────────

    _renderAllCharts() {
      if (!this.stats || typeof Chart === 'undefined') return;

      this._renderDoughnut('decisionsChart', 'Decisions', this.stats.decisions || {}, DECISION_COLORS);
      this._renderDoughnut('risksChart', 'Risk Levels', this.stats.risk_distribution || {}, RISK_COLORS);
      this._renderDoughnut('pathsChart', 'Eval Paths', this.stats.path_distribution || {}, PATH_COLORS);
      this._renderDoughnut('sessionsChart', 'Sessions', this.stats.sessions_by_status || {}, SESSION_COLORS);
      this._renderDoughnut('classificationChart', 'Classification', this.stats.classification_distribution || {}, CLASSIFICATION_COLORS);
      this._renderTimeline();
      this._renderTopTools();
    },

    _renderDoughnut(canvasId, label, data, colorMap) {
      const canvas = document.getElementById(canvasId);
      if (!canvas) return;

      // Destroy existing chart
      if (this._charts[canvasId]) {
        this._charts[canvasId].destroy();
      }

      const labels = Object.keys(data);
      const values = Object.values(data);
      const total = values.reduce((a, b) => a + b, 0);
      const colors = labels.map(l => colorMap[l] || CHART_COLORS.muted);

      this._charts[canvasId] = new Chart(canvas, {
        type: 'doughnut',
        data: {
          labels: labels,
          datasets: [{
            data: values,
            backgroundColor: colors,
            borderColor: CHART_COLORS.surface,
            borderWidth: 2,
            hoverBorderColor: CHART_COLORS.text,
            hoverBorderWidth: 2,
          }],
        },
        options: {
          responsive: true,
          maintainAspectRatio: true,
          cutout: '65%',
          plugins: {
            legend: {
              position: 'bottom',
              labels: {
                color: CHART_COLORS.text,
                padding: 10,
                font: { size: 10 },
                generateLabels(chart) {
                  const ds = chart.data.datasets[0];
                  return chart.data.labels.map((lbl, i) => ({
                    text: lbl + ' (' + ds.data[i] + ')',
                    fillStyle: ds.backgroundColor[i],
                    fontColor: CHART_COLORS.text,
                    strokeStyle: 'transparent',
                    pointStyle: 'circle',
                    hidden: false,
                    index: i,
                  }));
                },
              },
            },
            centerText: {
              text: total.toString(),
              subtext: 'total',
            },
            tooltip: {
              callbacks: {
                label(ctx) {
                  const pct = total > 0 ? Math.round((ctx.raw / total) * 100) : 0;
                  return ' ' + ctx.label + ': ' + ctx.raw + ' (' + pct + '%)';
                },
              },
            },
          },
        },
      });
    },

    _updateDoughnut(canvasId, data, colorMap) {
      const chart = this._charts[canvasId];
      if (!chart || !data) return;

      // Skip update if canvas is hidden (display:none) or detached.
      // offsetParent is null for display:none elements and their children.
      if (!chart.canvas || !chart.canvas.isConnected || chart.canvas.offsetParent === null) return;

      const labels = Object.keys(data);
      const values = Object.values(data);
      const total = values.reduce((a, b) => a + b, 0);
      const colors = labels.map(l => colorMap[l] || CHART_COLORS.muted);

      chart.data.labels = labels;
      chart.data.datasets[0].data = values;
      chart.data.datasets[0].backgroundColor = colors;
      if (chart.options?.plugins?.centerText) {
        chart.options.plugins.centerText.text = total.toString();
      }
      try {
        chart.update('none'); // no animation for live updates
      } catch (e) {
        // Chart.js may throw if layout state is stale after tab switch;
        // the next full load() will recreate the chart from scratch.
      }
    },

    _renderTimeline() {
      const canvas = document.getElementById('timelineChart');
      if (!canvas) return;

      if (this._charts.timelineChart) {
        this._charts.timelineChart.destroy();
      }

      const timeline = this.stats.activity_timeline || [];

      // Build full 24h label set (fill gaps with 0)
      const now = new Date();
      const hours = [];
      const countMap = {};
      timeline.forEach(t => { countMap[t.hour] = t.count; });

      for (let i = 23; i >= 0; i--) {
        const d = new Date(now.getTime() - i * 3600000);
        const key = d.getFullYear() + '-' +
          String(d.getMonth() + 1).padStart(2, '0') + '-' +
          String(d.getDate()).padStart(2, '0') + 'T' +
          String(d.getHours()).padStart(2, '0') + ':00';
        hours.push({
          label: String(d.getHours()).padStart(2, '0') + ':00',
          key: key,
          count: countMap[key] || 0,
        });
      }

      this._charts.timelineChart = new Chart(canvas, {
        type: 'bar',
        data: {
          labels: hours.map(h => h.label),
          datasets: [{
            label: 'Evaluations',
            data: hours.map(h => h.count),
            backgroundColor: CHART_COLORS.cyan + 'B3', // 70% opacity
            hoverBackgroundColor: CHART_COLORS.cyan,
            borderRadius: 3,
            borderSkipped: false,
          }],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { display: false },
            tooltip: {
              callbacks: {
                title(items) { return items[0].label; },
                label(ctx) { return ' ' + ctx.raw + ' evaluations'; },
              },
            },
          },
          scales: {
            x: {
              grid: { display: false },
              ticks: {
                maxRotation: 0,
                autoSkip: true,
                maxTicksLimit: 12,
                font: { size: 10 },
              },
            },
            y: {
              beginAtZero: true,
              grid: {
                color: CHART_COLORS.border,
              },
              ticks: {
                precision: 0,
                font: { size: 10 },
              },
            },
          },
        },
      });
    },

    _renderTopTools() {
      const canvas = document.getElementById('topToolsChart');
      if (!canvas) return;

      if (this._charts.topToolsChart) {
        this._charts.topToolsChart.destroy();
      }

      const tools = (this.stats.top_tools || []).slice(0, 8);
      if (tools.length === 0) return;

      // Reverse for horizontal bar (top item at top)
      const reversed = [...tools].reverse();

      // Gradient colors from cyan to teal
      const barColors = reversed.map((_, i) => {
        const ratio = i / Math.max(reversed.length - 1, 1);
        return ratio > 0.5 ? CHART_COLORS.cyan : CHART_COLORS.teal;
      });

      this._charts.topToolsChart = new Chart(canvas, {
        type: 'bar',
        data: {
          labels: reversed.map(t => t.tool),
          datasets: [{
            label: 'Calls',
            data: reversed.map(t => t.count),
            backgroundColor: barColors,
            borderRadius: 3,
            borderSkipped: false,
          }],
        },
        options: {
          indexAxis: 'y',
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { display: false },
            tooltip: {
              callbacks: {
                label(ctx) { return ' ' + ctx.raw + ' calls'; },
              },
            },
          },
          scales: {
            x: {
              beginAtZero: true,
              grid: {
                color: CHART_COLORS.border,
              },
              ticks: {
                precision: 0,
                font: { size: 10 },
              },
            },
            y: {
              grid: { display: false },
              ticks: {
                font: { size: 10, family: 'ui-monospace, SFMono-Regular, Menlo, monospace' },
              },
            },
          },
        },
      });
    },

    _destroyAllCharts() {
      Object.values(this._charts).forEach(c => {
        if (c && typeof c.destroy === 'function') c.destroy();
      });
      this._charts = {};
    },

    // ── Computed properties ─────────────────────────────────────────

    get approvalRate() {
      if (!this.stats) return '0%';
      return this.stats.approval_rate + '%';
    },

    get avgLatency() {
      if (!this.stats) return '0ms';
      return Math.round(this.stats.avg_latency_ms) + 'ms';
    },

    get hasChartData() {
      if (!this.stats) return false;
      const d = this.stats.decisions || {};
      return Object.values(d).some(v => v > 0);
    },

    // ── Badge helpers ──────────────────────────────────────────────

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

    formatTime(ts) {
      if (!ts) return '';
      const d = new Date(ts);
      return d.toLocaleString();
    },

    truncate(str, len) {
      if (!str) return '';
      return str.length > len ? str.substring(0, len) + '...' : str;
    },

    // ── Navigation ────────────────────────────────────────────────

    goToSession(sessionId) {
      Alpine.store('nav').openSessionModal(sessionId);
    },
  };
}
