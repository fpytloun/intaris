/**
 * Analysis tab component — behavioral risk profile, charts, and analysis history.
 *
 * Shows the agent-scoped behavioral profile (risk level, alerts, context),
 * trend charts (risk over time, findings distribution), and a paginated
 * list of cross-session analyses with expandable findings and recommendations.
 */

/* global Alpine, IntarisAPI, Chart */

// ── Color constants (shared with dashboard.js) ──────────────────────

const ANALYSIS_COLORS = {
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

const ANALYSIS_RISK_COLORS = {
  low:      ANALYSIS_COLORS.cyan,
  medium:   ANALYSIS_COLORS.amber,
  high:     ANALYSIS_COLORS.orange,
  critical: ANALYSIS_COLORS.red,
};

const ANALYSIS_SEVERITY_COLORS = {
  low:      ANALYSIS_COLORS.cyan,
  medium:   ANALYSIS_COLORS.amber,
  high:     ANALYSIS_COLORS.orange,
  critical: ANALYSIS_COLORS.red,
};

// Category colors for finding types
const CATEGORY_COLORS = {
  intent_drift:               ANALYSIS_COLORS.purple,
  restriction_circumvention:  ANALYSIS_COLORS.red,
  scope_creep:                ANALYSIS_COLORS.orange,
  insecure_reasoning:         ANALYSIS_COLORS.pink,
  unusual_tool_pattern:       ANALYSIS_COLORS.amber,
  injection_attempt:          ANALYSIS_COLORS.red,
  escalation_pattern:         ANALYSIS_COLORS.teal,
  delegation_misalignment:    ANALYSIS_COLORS.slate,
};

// Numeric mapping for risk level line chart
const RISK_LEVEL_VALUE = { low: 1, medium: 2, high: 3, critical: 4 };

// ── Center-text plugin (reuse if already registered by dashboard) ────

const _analysisCenterTextId = 'analysisCenterText';
if (typeof Chart !== 'undefined' && !Chart.registry.plugins.get(_analysisCenterTextId)) {
  Chart.register({
    id: _analysisCenterTextId,
    afterDraw(chart) {
      const centerText = chart.options.plugins?.[_analysisCenterTextId];
      if (!centerText?.text) return;

      const { ctx, chartArea } = chart;
      if (!ctx || !chartArea) return;

      const { left, right, top, bottom } = chartArea;
      const cx = (left + right) / 2;
      const cy = (top + bottom) / 2;

      ctx.save();
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';

      ctx.font = 'bold 20px ' + Chart.defaults.font.family;
      ctx.fillStyle = ANALYSIS_COLORS.text;
      ctx.fillText(centerText.text, cx, cy - 6);

      if (centerText.subtext) {
        ctx.font = '10px ' + Chart.defaults.font.family;
        ctx.fillStyle = ANALYSIS_COLORS.muted;
        ctx.fillText(centerText.subtext, cx, cy + 12);
      }

      ctx.restore();
    },
  });
}

// ── Component ────────────────────────────────────────────────────────

function analysisTab() {
  return {
    initialized: false,

    // Profile
    profile: { risk_level: 'low', profile_version: 0, active_alerts: [], context_summary: null, updated_at: null },
    profileLoading: false,

    // Analyses list
    analyses: [],
    analysesLoading: false,
    analysesPage: 1,
    analysesPages: 1,
    analysesTotal: 0,

    // Actions
    triggeringAnalysis: false,

    // Chart instances
    _charts: {},

    init() {
      this.loadData();

      // Refresh on tab change
      window.addEventListener('intaris:tab-changed', (e) => {
        if (e.detail.tab === 'analysis') this.loadData();
      });

      // Refresh on user/agent change
      window.addEventListener('intaris:user-changed', () => {
        this._destroyAllCharts();
        this.loadData();
      });
      window.addEventListener('intaris:agent-changed', () => {
        this._destroyAllCharts();
        this.loadData();
      });
      window.addEventListener('intaris:logout', () => {
        this._destroyAllCharts();
      });
    },

    async loadData() {
      await Promise.all([this.loadProfile(), this.loadAnalyses()]);
      this.initialized = true;
      requestAnimationFrame(() => this._renderAllCharts());
    },

    _agentFilter() {
      const f = Alpine.store('nav')?.selectedAgent;
      return f || undefined;
    },

    async loadProfile() {
      this.profileLoading = true;
      try {
        const params = {};
        const agent = this._agentFilter();
        if (agent) params.agent_id = agent;
        this.profile = await IntarisAPI.getProfile(params);
      } catch (e) {
        // Profile may 403 for non-bound keys — show defaults
        this.profile = { risk_level: 'low', profile_version: 0, active_alerts: [], context_summary: null, updated_at: null };
      } finally {
        this.profileLoading = false;
      }
    },

    async loadAnalyses(page = 1) {
      this.analysesLoading = true;
      try {
        const params = { page, limit: 20 };
        const agent = this._agentFilter();
        if (agent) params.agent_id = agent;
        const data = await IntarisAPI.listAnalyses(params);
        this.analyses = (data.items || []).map(a => ({ ...a, _expanded: false }));
        this.analysesPage = data.page;
        this.analysesPages = data.pages;
        this.analysesTotal = data.total;
      } catch (e) {
        this.analyses = [];
      } finally {
        this.analysesLoading = false;
      }
    },

    async triggerAnalysis() {
      this.triggeringAnalysis = true;
      try {
        const params = {};
        const agent = this._agentFilter();
        if (agent) params.agent_id = agent;
        await IntarisAPI.triggerAnalysis(params);
        Alpine.store('notify')?.success('Analysis triggered');
        // Reload after a short delay to pick up results
        setTimeout(() => this.loadData(), 3000);
      } catch (e) {
        Alpine.store('notify')?.error(e.message || 'Failed to trigger analysis');
      } finally {
        this.triggeringAnalysis = false;
      }
    },

    // ── Chart data aggregation ─────────────────────────────────────

    /** Aggregate all findings across loaded analyses. */
    _allFindings() {
      const findings = [];
      for (const a of this.analyses) {
        if (a.findings) findings.push(...a.findings);
      }
      return findings;
    },

    /** Count occurrences by key in an array of objects. */
    _countBy(items, key) {
      const counts = {};
      for (const item of items) {
        const val = item[key] || 'unknown';
        counts[val] = (counts[val] || 0) + 1;
      }
      return counts;
    },

    /** Count analyses by risk_level. */
    _riskDistribution() {
      return this._countBy(this.analyses, 'risk_level');
    },

    // ── Chart rendering ────────────────────────────────────────────

    get hasChartData() {
      return this.analyses.length > 0;
    },

    _renderAllCharts() {
      if (!this.analyses.length || typeof Chart === 'undefined') return;

      const findings = this._allFindings();

      // Doughnut charts
      this._renderDoughnut(
        'analysisCategoriesChart',
        this._countBy(findings, 'category'),
        CATEGORY_COLORS,
        'categories',
      );
      this._renderDoughnut(
        'analysisSeverityChart',
        this._countBy(findings, 'severity'),
        ANALYSIS_SEVERITY_COLORS,
        'findings',
      );
      this._renderDoughnut(
        'analysisRiskLevelsChart',
        this._riskDistribution(),
        ANALYSIS_RISK_COLORS,
        'analyses',
      );

      // Time series
      this._renderRiskTimeline();
      this._renderFindingsTimeline();
    },

    _renderDoughnut(canvasId, data, colorMap, subtext) {
      // Update existing chart in-place if possible
      const existing = this._charts[canvasId];
      if (existing && existing.canvas && existing.canvas.isConnected) {
        this._updateDoughnut(canvasId, data, colorMap);
        return;
      }

      const canvas = document.getElementById(canvasId);
      if (!canvas || !canvas.getContext('2d')) return;

      if (existing) {
        existing.destroy();
        delete this._charts[canvasId];
      }

      const labels = Object.keys(data);
      const values = Object.values(data);
      const total = values.reduce((a, b) => a + b, 0);
      const colors = labels.map(l => colorMap[l] || ANALYSIS_COLORS.muted);

      this._charts[canvasId] = new Chart(canvas, {
        type: 'doughnut',
        data: {
          labels,
          datasets: [{
            data: values,
            backgroundColor: colors,
            borderColor: ANALYSIS_COLORS.surface,
            borderWidth: 2,
            hoverBorderColor: ANALYSIS_COLORS.text,
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
                color: ANALYSIS_COLORS.text,
                padding: 10,
                font: { size: 10 },
                generateLabels(chart) {
                  const ds = chart.data.datasets[0];
                  return chart.data.labels.map((lbl, i) => ({
                    text: lbl + ' (' + ds.data[i] + ')',
                    fillStyle: ds.backgroundColor[i],
                    fontColor: ANALYSIS_COLORS.text,
                    strokeStyle: 'transparent',
                    pointStyle: 'circle',
                    hidden: false,
                    index: i,
                  }));
                },
              },
            },
            [_analysisCenterTextId]: {
              text: total.toString(),
              subtext: subtext || 'total',
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
      if (!chart.canvas || !chart.canvas.isConnected || chart.canvas.offsetParent === null) return;

      const labels = Object.keys(data);
      const values = Object.values(data);
      const total = values.reduce((a, b) => a + b, 0);
      const colors = labels.map(l => colorMap[l] || ANALYSIS_COLORS.muted);

      chart.data.labels = labels;
      chart.data.datasets[0].data = values;
      chart.data.datasets[0].backgroundColor = colors;
      if (chart.options?.plugins?.[_analysisCenterTextId]) {
        chart.options.plugins[_analysisCenterTextId].text = total.toString();
      }
      try { chart.update('none'); } catch (e) { /* stale layout */ }
    },

    _renderRiskTimeline() {
      if (this.analyses.length < 2) return;

      // Sort chronologically (oldest first)
      const sorted = [...this.analyses].sort(
        (a, b) => new Date(a.created_at) - new Date(b.created_at),
      );

      const labels = sorted.map(a => {
        const d = new Date(a.created_at);
        return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
      });
      const values = sorted.map(a => RISK_LEVEL_VALUE[a.risk_level] || 0);
      const pointColors = sorted.map(a => ANALYSIS_RISK_COLORS[a.risk_level] || ANALYSIS_COLORS.muted);

      const canvasId = 'analysisRiskTimeChart';
      const existing = this._charts[canvasId];
      if (existing && existing.canvas && existing.canvas.isConnected) {
        existing.data.labels = labels;
        existing.data.datasets[0].data = values;
        existing.data.datasets[0].pointBackgroundColor = pointColors;
        try { existing.update('none'); } catch (e) { /* stale */ }
        return;
      }

      const canvas = document.getElementById(canvasId);
      if (!canvas || !canvas.getContext('2d')) return;

      if (existing) {
        existing.destroy();
        delete this._charts[canvasId];
      }

      this._charts[canvasId] = new Chart(canvas, {
        type: 'line',
        data: {
          labels,
          datasets: [{
            label: 'Risk Level',
            data: values,
            borderColor: ANALYSIS_COLORS.cyan + 'B3',
            backgroundColor: ANALYSIS_COLORS.cyan + '1A',
            fill: true,
            tension: 0.3,
            pointRadius: 6,
            pointHoverRadius: 8,
            pointBackgroundColor: pointColors,
            pointBorderColor: ANALYSIS_COLORS.surface,
            pointBorderWidth: 2,
          }],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { display: false },
            tooltip: {
              callbacks: {
                label(ctx) {
                  const levelNames = ['', 'Low', 'Medium', 'High', 'Critical'];
                  return ' Risk: ' + (levelNames[ctx.raw] || 'Unknown');
                },
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
              min: 0.5,
              max: 4.5,
              grid: { color: ANALYSIS_COLORS.border },
              ticks: {
                stepSize: 1,
                font: { size: 10 },
                callback(value) {
                  const labels = { 1: 'Low', 2: 'Medium', 3: 'High', 4: 'Critical' };
                  return labels[value] || '';
                },
              },
            },
          },
        },
      });
    },

    _renderFindingsTimeline() {
      if (this.analyses.length < 2) return;

      // Sort chronologically
      const sorted = [...this.analyses].sort(
        (a, b) => new Date(a.created_at) - new Date(b.created_at),
      );

      const labels = sorted.map(a => {
        const d = new Date(a.created_at);
        return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
      });

      // Build stacked datasets by severity
      const severities = ['low', 'medium', 'high', 'critical'];
      const datasets = severities.map(sev => ({
        label: sev.charAt(0).toUpperCase() + sev.slice(1),
        data: sorted.map(a => {
          const findings = a.findings || [];
          return findings.filter(f => f.severity === sev).length;
        }),
        backgroundColor: ANALYSIS_SEVERITY_COLORS[sev] + 'CC',
        borderRadius: 2,
        borderSkipped: false,
      }));

      const canvasId = 'analysisFindingsTimeChart';
      const existing = this._charts[canvasId];
      if (existing && existing.canvas && existing.canvas.isConnected) {
        existing.data.labels = labels;
        existing.data.datasets = datasets;
        try { existing.update('none'); } catch (e) { /* stale */ }
        return;
      }

      const canvas = document.getElementById(canvasId);
      if (!canvas || !canvas.getContext('2d')) return;

      if (existing) {
        existing.destroy();
        delete this._charts[canvasId];
      }

      this._charts[canvasId] = new Chart(canvas, {
        type: 'bar',
        data: { labels, datasets },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: {
              position: 'bottom',
              labels: {
                color: ANALYSIS_COLORS.text,
                padding: 10,
                font: { size: 10 },
                usePointStyle: true,
                pointStyle: 'circle',
              },
            },
            tooltip: {
              mode: 'index',
              intersect: false,
              callbacks: {
                label(ctx) {
                  return ' ' + ctx.dataset.label + ': ' + ctx.raw;
                },
              },
            },
          },
          scales: {
            x: {
              stacked: true,
              grid: { display: false },
              ticks: {
                maxRotation: 0,
                autoSkip: true,
                maxTicksLimit: 12,
                font: { size: 10 },
              },
            },
            y: {
              stacked: true,
              beginAtZero: true,
              grid: { color: ANALYSIS_COLORS.border },
              ticks: {
                precision: 0,
                font: { size: 10 },
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
  };
}
