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
 * - Time series charts: one data point per analysis, sorted chronologically.
 *   Session counts per severity/category.
 *
 * Chart.js + Alpine.js compatibility:
 * - Chart instances are stored in a module-level Map (not on the Alpine
 *   reactive proxy) to prevent Alpine from wrapping Chart.js internals.
 * - Canvas elements are unwrapped with Alpine.raw() before passing to
 *   Chart.js constructor.
 */

/* global Alpine, IntarisAPI, Chart */

// ── Color constants ─────────────────────────────────────────────────

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

// Note: risk_level and finding severity are now numeric (1-10).
// Color mapping uses the global riskScoreChartColor() and riskBand()
// helpers from app.js. The named-key maps below are for the severity
// doughnut chart which groups findings by band name.
const ANALYSIS_SEVERITY_BAND_COLORS = {
  minimal:  ANALYSIS_COLORS.cyan,
  low:      ANALYSIS_COLORS.green,
  moderate: ANALYSIS_COLORS.amber,
  elevated: ANALYSIS_COLORS.orange,
  high:     ANALYSIS_COLORS.red,
  critical: '#EF4444',
};

// Category colors for finding types (L2 indicators + L3 cross-session findings)
const CATEGORY_COLORS = {
  // L2 summary indicators
  intent_drift:               ANALYSIS_COLORS.purple,
  restriction_circumvention:  ANALYSIS_COLORS.red,
  scope_creep:                ANALYSIS_COLORS.orange,
  insecure_reasoning:         ANALYSIS_COLORS.pink,
  unusual_tool_pattern:       ANALYSIS_COLORS.amber,
  injection_attempt:          '#EF4444',
  escalation_pattern:         ANALYSIS_COLORS.teal,
  delegation_misalignment:    ANALYSIS_COLORS.slate,
  // L3 cross-session findings (concerning)
  coordinated_access:         ANALYSIS_COLORS.purple,
  progressive_escalation:     '#EF4444',
  intent_masking:             ANALYSIS_COLORS.pink,
  tool_abuse:                 ANALYSIS_COLORS.orange,
  persistent_misalignment:    ANALYSIS_COLORS.amber,
  insecure_reasoning_pattern: ANALYSIS_COLORS.teal,
  // L3 cross-session findings (positive/neutral)
  consistent_alignment:       ANALYSIS_COLORS.green,
  normal_development:         ANALYSIS_COLORS.cyan,
  improving_posture:          ANALYSIS_COLORS.teal,
};

const _FALLBACK_PALETTE = [
  '#818CF8', '#FB7185', '#38BDF8', '#A3E635', '#E879F9',
  '#FACC15', '#4ADE80', '#F97316', '#67E8F9', '#C084FC',
];

function _categoryColor(name, index) {
  if (CATEGORY_COLORS[name]) return CATEGORY_COLORS[name];
  return _FALLBACK_PALETTE[index % _FALLBACK_PALETTE.length];
}

// ── Module-level chart storage (outside Alpine reactivity) ──────────
//
// Chart.js instances MUST NOT be stored on the Alpine reactive proxy.
// Alpine wraps objects in Proxy, which breaks Chart.js internal property
// access (e.g., plugin.events becomes undefined through the proxy,
// causing "can't access property 'includes' of undefined").

const _analysisCharts = new Map();

function _destroyAllAnalysisCharts() {
  _analysisCharts.forEach((chart) => {
    try { chart.destroy(); } catch (e) { /* ignore */ }
  });
  _analysisCharts.clear();
}

function _getChart(id) {
  return _analysisCharts.get(id);
}

function _setChart(id, chart) {
  _analysisCharts.set(id, chart);
}

function _deleteChart(id) {
  const c = _analysisCharts.get(id);
  if (c) {
    try { c.destroy(); } catch (e) { /* ignore */ }
    _analysisCharts.delete(id);
  }
}

/** Get a raw (non-proxied) canvas element by ID. */
function _getCanvas(id) {
  const el = document.getElementById(id);
  if (!el) return null;
  // Unwrap Alpine proxy if present
  return typeof Alpine !== 'undefined' && Alpine.raw ? Alpine.raw(el) : el;
}

// ── Helpers ──────────────────────────────────────────────────────────

function _countSessionsByKey(findings, key) {
  const sessionSets = {};
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

/** Group findings by severity band (riskBand of numeric severity). */
function _countSessionsByBand(findings) {
  const sessionSets = {};
  for (const f of findings) {
    const band = typeof f.severity === 'number' ? riskBand(f.severity) : (f.severity || 'unknown');
    if (!sessionSets[band]) sessionSets[band] = new Set();
    for (const sid of (f.session_ids || [])) {
      sessionSets[band].add(sid);
    }
  }
  const counts = {};
  for (const [k, s] of Object.entries(sessionSets)) {
    counts[k] = s.size;
  }
  return counts;
}

/** Count sessions for a specific severity band across findings. */
function _countSessionsForBand(findings, band) {
  const sessions = new Set();
  for (const f of findings) {
    const b = typeof f.severity === 'number' ? riskBand(f.severity) : (f.severity || 'unknown');
    if (b === band) {
      for (const sid of (f.session_ids || [])) {
        sessions.add(sid);
      }
    }
  }
  return sessions.size;
}

function _sortChronological(analyses) {
  return [...analyses].sort(
    (a, b) => new Date(a.created_at) - new Date(b.created_at),
  );
}

function _formatAnalysisLabel(createdAt) {
  const d = new Date(createdAt);
  return d.toLocaleDateString(undefined, {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  });
}

// ── Component ────────────────────────────────────────────────────────

function analysisTab() {
  return {
    initialized: false,

    // Profile
    profile: { risk_level: 1, profile_version: 0, active_alerts: [], context_summary: null, updated_at: null },
    profileLoading: false,

    // Analyses list
    analyses: [],
    analysesLoading: false,
    analysesPage: 1,
    analysesPages: 1,
    analysesTotal: 0,

    // Actions
    triggeringAnalysis: false,
    backfillingSummaries: false,
    showBackfillModal: false,
    backfillDays: 7,
    backfillForce: false,
    backfillResult: null,

    init() {
      this.loadData();

      window.addEventListener('intaris:tab-changed', (e) => {
        if (e.detail.tab === 'analysis') {
          _destroyAllAnalysisCharts();
          this.loadData();
        }
      });

      window.addEventListener('intaris:user-changed', () => {
        _destroyAllAnalysisCharts();
        this.loadData();
      });
      window.addEventListener('intaris:agent-changed', () => {
        _destroyAllAnalysisCharts();
        this.loadData();
      });
      window.addEventListener('intaris:logout', () => {
        _destroyAllAnalysisCharts();
      });
    },

    async loadData() {
      await Promise.all([this.loadProfile(), this.loadAnalyses()]);
      this.initialized = true;

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
        this.profile = { risk_level: 1, profile_version: 0, active_alerts: [], context_summary: null, updated_at: null };
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
        setTimeout(() => this.loadData(), 3000);
      } catch (e) {
        Alpine.store('notify')?.error(e.message || 'Failed to trigger analysis');
      } finally {
        this.triggeringAnalysis = false;
      }
    },

    async backfillSummaries() {
      this.backfillingSummaries = true;
      this.backfillResult = null;
      try {
        const params = {
          lookback_days: this.backfillDays,
          force: this.backfillForce,
        };
        const agent = this._agentFilter();
        if (agent) params.agent_id = agent;
        const result = await IntarisAPI.backfillSummaries(params);
        this.backfillResult = result;
        Alpine.store('notify')?.success(
          `Backfill: ${result.enqueued} enqueued, ${result.skipped} skipped`
        );
      } catch (e) {
        Alpine.store('notify')?.error(e.message || 'Backfill failed');
      } finally {
        this.backfillingSummaries = false;
      }
    },

    // ── Chart data helpers ─────────────────────────────────────────

    _latestAnalysis() {
      if (!this.analyses.length) return null;
      return this.analyses[0];
    },

    // ── Chart rendering ────────────────────────────────────────────

    _renderAllCharts() {
      // Copy analyses out of Alpine proxy for chart rendering
      const analyses = Alpine.raw(this.analyses);
      if (!analyses.length || typeof Chart === 'undefined') return;

      const latest = analyses[0];
      const latestFindings = latest?.findings || [];

      // Each chart is wrapped in try/catch so one failure doesn't
      // prevent the others from rendering.
      try {
        this._renderDoughnut(
          'analysisCategoriesChart',
          _countSessionsByKey(latestFindings, 'category'),
          null,
          'sessions',
        );
      } catch (e) { console.warn('analysisCategoriesChart error:', e); }

      try {
        this._renderDoughnut(
          'analysisSeverityChart',
          _countSessionsByBand(latestFindings),
          ANALYSIS_SEVERITY_BAND_COLORS,
          'sessions',
        );
      } catch (e) { console.warn('analysisSeverityChart error:', e); }

      try { this._renderRiskTimeline(analyses); }
      catch (e) { console.warn('analysisRiskTimeChart error:', e); }

      try { this._renderFindingsTimeline(analyses); }
      catch (e) { console.warn('analysisFindingsTimeChart error:', e); }

      try { this._renderCategoriesTimeline(analyses); }
      catch (e) { console.warn('analysisCategoriesTimeChart error:', e); }
    },

    _renderDoughnut(canvasId, data, colorMap, subtext) {
      const existing = _getChart(canvasId);
      if (existing && existing.canvas && existing.canvas.isConnected) {
        // Update in-place
        const labels = Object.keys(data);
        const values = Object.values(data);
        const total = values.reduce((a, b) => a + b, 0);
        const colors = colorMap
          ? labels.map(l => colorMap[l] || ANALYSIS_COLORS.muted)
          : labels.map((l, i) => _categoryColor(l, i));

        existing.data.labels = labels;
        existing.data.datasets[0].data = values;
        existing.data.datasets[0].backgroundColor = colors;
        if (existing.options?.plugins?.centerText) {
          existing.options.plugins.centerText.text = total.toString();
        }
        try { existing.update('none'); } catch (e) { /* stale */ }
        return;
      }

      const canvas = _getCanvas(canvasId);
      if (!canvas || !canvas.getContext('2d')) return;

      _deleteChart(canvasId);

      const labels = Object.keys(data);
      const values = Object.values(data);
      const total = values.reduce((a, b) => a + b, 0);
      const colors = colorMap
        ? labels.map(l => colorMap[l] || ANALYSIS_COLORS.muted)
        : labels.map((l, i) => _categoryColor(l, i));

      _setChart(canvasId, new Chart(canvas, {
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
          maintainAspectRatio: false,
          cutout: '65%',
          plugins: {
            legend: {
              position: 'right',
              labels: {
                color: ANALYSIS_COLORS.text,
                padding: 8,
                boxWidth: 12,
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
            centerText: {
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
      }));
    },

    _renderRiskTimeline(analyses) {
      const sorted = _sortChronological(analyses);
      if (sorted.length < 2) return;

      const labels = sorted.map(a => _formatAnalysisLabel(a.created_at));
      const values = sorted.map(a => Number(a.risk_level) || 1);
      const pointColors = sorted.map(a => riskScoreChartColor(a.risk_level));

      const canvasId = 'analysisRiskTimeChart';
      const existing = _getChart(canvasId);
      if (existing && existing.canvas && existing.canvas.isConnected) {
        existing.data.labels = labels;
        existing.data.datasets[0].data = values;
        existing.data.datasets[0].pointBackgroundColor = pointColors;
        try { existing.update('none'); } catch (e) { /* stale */ }
        return;
      }

      const canvas = _getCanvas(canvasId);
      if (!canvas || !canvas.getContext('2d')) return;

      _deleteChart(canvasId);

      _setChart(canvasId, new Chart(canvas, {
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
                  return ' Risk: ' + ctx.raw + ' (' + riskBand(ctx.raw).toUpperCase() + ')';
                },
              },
            },
          },
          scales: {
            x: {
              grid: { display: false },
              ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 10, font: { size: 10 } },
            },
            y: {
              min: 0, max: 10.5,
              grid: { color: ANALYSIS_COLORS.border },
              ticks: {
                stepSize: 2, font: { size: 10 },
                callback(value) {
                  const labels = { 1: 'Min', 3: 'Low', 5: 'Mod', 7: 'Elev', 9: 'High', 10: 'Crit' };
                  return labels[value] || '';
                },
              },
            },
          },
        },
      }));
    },

    _renderFindingsTimeline(analyses) {
      const sorted = _sortChronological(analyses);
      if (sorted.length < 2) return;

      const labels = sorted.map(a => _formatAnalysisLabel(a.created_at));
      const bands = ['minimal', 'low', 'moderate', 'elevated', 'high', 'critical'];
      const datasets = bands.map(band => ({
        label: band.charAt(0).toUpperCase() + band.slice(1),
        data: sorted.map(a => _countSessionsForBand(a.findings || [], band)),
        backgroundColor: (ANALYSIS_SEVERITY_BAND_COLORS[band] || ANALYSIS_COLORS.muted) + 'CC',
        borderRadius: 2,
        borderSkipped: false,
      }));

      this._renderStackedBar('analysisFindingsTimeChart', labels, datasets);
    },

    _renderCategoriesTimeline(analyses) {
      const sorted = _sortChronological(analyses);
      if (sorted.length < 2) return;

      const labels = sorted.map(a => _formatAnalysisLabel(a.created_at));

      const allCategories = new Set();
      for (const a of sorted) {
        for (const f of (a.findings || [])) {
          allCategories.add(f.category || 'unknown');
        }
      }
      const categories = [...allCategories].sort();

      const datasets = categories.map((cat, i) => ({
        label: cat,
        data: sorted.map(a => _countSessionsForValue(a.findings || [], 'category', cat)),
        backgroundColor: _categoryColor(cat, i) + 'CC',
        borderRadius: 2,
        borderSkipped: false,
      }));

      this._renderStackedBar('analysisCategoriesTimeChart', labels, datasets);
    },

    _renderStackedBar(canvasId, labels, datasets) {
      const existing = _getChart(canvasId);
      if (existing && existing.canvas && existing.canvas.isConnected) {
        existing.data.labels = labels;
        existing.data.datasets = datasets;
        try { existing.update('none'); } catch (e) { /* stale */ }
        return;
      }

      const canvas = _getCanvas(canvasId);
      if (!canvas || !canvas.getContext('2d')) return;

      _deleteChart(canvasId);

      _setChart(canvasId, new Chart(canvas, {
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
                  if (ctx.raw === 0) return null;
                  return ' ' + ctx.dataset.label + ': ' + ctx.raw + ' sessions';
                },
              },
            },
          },
          scales: {
            x: {
              stacked: true,
              grid: { display: false },
              ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 10, font: { size: 10 } },
            },
            y: {
              stacked: true,
              beginAtZero: true,
              grid: { color: ANALYSIS_COLORS.border },
              ticks: { precision: 0, font: { size: 10 } },
            },
          },
        },
      }));
    },

    _destroyAllCharts() {
      _destroyAllAnalysisCharts();
    },
  };
}
