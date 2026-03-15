/**
 * Analysis tab component — behavioral risk profile, charts, and analysis history.
 *
 * Shows the agent-scoped behavioral profile (risk level, alerts, context),
 * trend charts (risk over time, findings distribution), and a paginated
 * list of cross-session analyses with expandable findings and recommendations.
 *
 * Chart data strategy:
 * - Doughnut charts: show data from the LATEST analysis only (current state).
 *   Counts are based on unique sessions (from finding.session_ids), not
 *   finding count.
 * - Time series charts: per-day bucketed, using the last analysis of each day.
 *   Session counts per severity/category.
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

// Category colors for finding types (L2 indicators + L3 cross-session findings)
const CATEGORY_COLORS = {
  // L2 summary indicators
  intent_drift:               ANALYSIS_COLORS.purple,
  restriction_circumvention:  ANALYSIS_COLORS.red,
  scope_creep:                ANALYSIS_COLORS.orange,
  insecure_reasoning:         ANALYSIS_COLORS.pink,
  unusual_tool_pattern:       ANALYSIS_COLORS.amber,
  injection_attempt:          '#EF4444', // distinct red
  escalation_pattern:         ANALYSIS_COLORS.teal,
  delegation_misalignment:    ANALYSIS_COLORS.slate,
  // L3 cross-session findings
  coordinated_access:         ANALYSIS_COLORS.purple,
  progressive_escalation:     '#EF4444',
  intent_masking:             ANALYSIS_COLORS.pink,
  tool_abuse:                 ANALYSIS_COLORS.orange,
  persistent_misalignment:    ANALYSIS_COLORS.amber,
  insecure_reasoning_pattern: ANALYSIS_COLORS.teal,
};

// Fallback palette for unknown categories (cycles through distinct colors)
const _FALLBACK_PALETTE = [
  '#818CF8', '#FB7185', '#38BDF8', '#A3E635', '#E879F9',
  '#FACC15', '#4ADE80', '#F97316', '#67E8F9', '#C084FC',
];

/** Get color for a category, with fallback cycling for unknown ones. */
function _categoryColor(name, index) {
  if (CATEGORY_COLORS[name]) return CATEGORY_COLORS[name];
  return _FALLBACK_PALETTE[index % _FALLBACK_PALETTE.length];
}

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

// ── Helpers ──────────────────────────────────────────────────────────

/**
 * Count unique sessions per value of a given key across findings.
 * Each finding has a session_ids[] array. Returns { key_value: session_count }.
 */
function _countSessionsByKey(findings, key) {
  const sessionSets = {}; // key_value -> Set<session_id>
  for (const f of findings) {
    const val = f[key] || 'unknown';
    if (!sessionSets[val]) sessionSets[val] = new Set();
    for (const sid of (f.session_ids || [])) {
      sessionSets[val].add(sid);
    }
  }
  const counts = {};
  for (const [k, s] of Object.entries(sessionSets)) {
    counts[k] = s.size;
  }
  return counts;
}

/**
 * Count unique sessions for a specific value of a key within findings.
 * Returns the number of unique session_ids across matching findings.
 */
function _countSessionsForValue(findings, key, value) {
  const sessions = new Set();
  for (const f of findings) {
    if ((f[key] || 'unknown') === value) {
      for (const sid of (f.session_ids || [])) {
        sessions.add(sid);
      }
    }
  }
  return sessions.size;
}

/**
 * Bucket analyses by day (YYYY-MM-DD), keeping only the LAST analysis
 * per day (most recent created_at). Returns array sorted chronologically.
 */
function _bucketByDay(analyses) {
  // Sort chronologically first
  const sorted = [...analyses].sort(
    (a, b) => new Date(a.created_at) - new Date(b.created_at),
  );

  const dayMap = new Map(); // day string -> analysis (last wins)
  for (const a of sorted) {
    const day = new Date(a.created_at).toISOString().slice(0, 10);
    dayMap.set(day, a);
  }

  // Return in chronological order
  return [...dayMap.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([day, analysis]) => ({ day, analysis }));
}

/**
 * Format a YYYY-MM-DD string as a short date label.
 */
function _formatDayLabel(dayStr) {
  const d = new Date(dayStr + 'T00:00:00');
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
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

      // Refresh on tab change — destroy stale charts and reload
      window.addEventListener('intaris:tab-changed', (e) => {
        if (e.detail.tab === 'analysis') {
          this._destroyAllCharts();
          this.loadData();
        }
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

      // Only render charts when the tab is visible — Chart.js needs
      // non-zero canvas dimensions to render correctly.
      if (Alpine.store('nav').activeTab === 'analysis') {
        requestAnimationFrame(() => this._renderAllCharts());
      }
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
        // Profile may not exist yet — show defaults
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

    // ── Chart data helpers ─────────────────────────────────────────

    /** Get the latest (most recent) analysis, or null. */
    _latestAnalysis() {
      if (!this.analyses.length) return null;
      // analyses are sorted by created_at DESC from the API
      return this.analyses[0];
    },

    // ── Chart rendering ────────────────────────────────────────────

    get hasChartData() {
      return this.analyses.length > 0;
    },

    _renderAllCharts() {
      if (!this.analyses.length || typeof Chart === 'undefined') return;

      // Safety: skip if canvases are not visible (hidden tab)
      const testCanvas = document.getElementById('analysisCategoriesChart');
      if (!testCanvas || testCanvas.offsetParent === null) return;

      const latest = this._latestAnalysis();
      const latestFindings = latest?.findings || [];

      // Doughnut charts — latest analysis, counting unique sessions
      this._renderDoughnut(
        'analysisCategoriesChart',
        _countSessionsByKey(latestFindings, 'category'),
        null, // use _categoryColor for dynamic coloring
        'sessions',
      );
      this._renderDoughnut(
        'analysisSeverityChart',
        _countSessionsByKey(latestFindings, 'severity'),
        ANALYSIS_SEVERITY_COLORS,
        'sessions',
      );

      // Time series (per-day bucketed, session counts)
      this._renderRiskTimeline();
      this._renderFindingsTimeline();
      this._renderCategoriesTimeline();
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
      const colors = colorMap
        ? labels.map(l => colorMap[l] || ANALYSIS_COLORS.muted)
        : labels.map((l, i) => _categoryColor(l, i));

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
                  return ' ' + ctx.label + ': ' + ctx.raw + ' sessions (' + pct + '%)';
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
      const colors = colorMap
        ? labels.map(l => colorMap[l] || ANALYSIS_COLORS.muted)
        : labels.map((l, i) => _categoryColor(l, i));

      chart.data.labels = labels;
      chart.data.datasets[0].data = values;
      chart.data.datasets[0].backgroundColor = colors;
      if (chart.options?.plugins?.[_analysisCenterTextId]) {
        chart.options.plugins[_analysisCenterTextId].text = total.toString();
      }
      try { chart.update('none'); } catch (e) { /* stale layout */ }
    },

    _renderRiskTimeline() {
      const buckets = _bucketByDay(this.analyses);
      if (buckets.length < 2) return;

      const labels = buckets.map(b => _formatDayLabel(b.day));
      const values = buckets.map(b => RISK_LEVEL_VALUE[b.analysis.risk_level] || 0);
      const pointColors = buckets.map(b => ANALYSIS_RISK_COLORS[b.analysis.risk_level] || ANALYSIS_COLORS.muted);

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
      const buckets = _bucketByDay(this.analyses);
      if (buckets.length < 2) return;

      const labels = buckets.map(b => _formatDayLabel(b.day));

      // Build stacked datasets by severity — counting unique sessions
      const severities = ['low', 'medium', 'high', 'critical'];
      const datasets = severities.map(sev => ({
        label: sev.charAt(0).toUpperCase() + sev.slice(1),
        data: buckets.map(b =>
          _countSessionsForValue(b.analysis.findings || [], 'severity', sev),
        ),
        backgroundColor: ANALYSIS_SEVERITY_COLORS[sev] + 'CC',
        borderRadius: 2,
        borderSkipped: false,
      }));

      this._renderStackedBar('analysisFindingsTimeChart', labels, datasets);
    },

    _renderCategoriesTimeline() {
      const buckets = _bucketByDay(this.analyses);
      if (buckets.length < 2) return;

      const labels = buckets.map(b => _formatDayLabel(b.day));

      // Collect all unique categories across all bucketed analyses
      const allCategories = new Set();
      for (const b of buckets) {
        for (const f of (b.analysis.findings || [])) {
          allCategories.add(f.category || 'unknown');
        }
      }
      const categories = [...allCategories].sort();

      // Build stacked datasets by category — counting unique sessions
      const datasets = categories.map((cat, i) => ({
        label: cat,
        data: buckets.map(b =>
          _countSessionsForValue(b.analysis.findings || [], 'category', cat),
        ),
        backgroundColor: _categoryColor(cat, i) + 'CC',
        borderRadius: 2,
        borderSkipped: false,
      }));

      this._renderStackedBar('analysisCategoriesTimeChart', labels, datasets);
    },

    /** Shared renderer for stacked bar charts (findings + categories timelines). */
    _renderStackedBar(canvasId, labels, datasets) {
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
                  if (ctx.raw === 0) return null; // hide zero entries
                  return ' ' + ctx.dataset.label + ': ' + ctx.raw + ' sessions';
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
