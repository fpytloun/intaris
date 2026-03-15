/**
 * Analysis tab component — behavioral risk profile and analysis history.
 *
 * Shows the agent-scoped behavioral profile (risk level, alerts, context)
 * and a paginated list of cross-session analyses with expandable findings
 * and recommendations.
 */

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

    init() {
      this.loadData();

      // Refresh on tab change
      window.addEventListener('intaris:tab-changed', (e) => {
        if (e.detail === 'analysis') this.loadData();
      });

      // Refresh on user/agent change
      window.addEventListener('intaris:user-changed', () => this.loadData());
      window.addEventListener('intaris:agent-changed', () => this.loadData());
    },

    async loadData() {
      await Promise.all([this.loadProfile(), this.loadAnalyses()]);
      this.initialized = true;
    },

    _agentFilter() {
      const f = Alpine.store('nav')?.agentFilter;
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
  };
}
