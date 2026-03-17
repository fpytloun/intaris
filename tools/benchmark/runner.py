"""Run-phase orchestrator for the Intaris benchmark.

Orchestrates scenario execution, session lifecycle, escalation handling,
L2/L3 analysis triggers, and progress reporting.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import openai

from tools.benchmark.agent import SimulatedAgent, build_system_prompt
from tools.benchmark.client import IntarisClient, IntarisError
from tools.benchmark.models import (
    AgentResult,
    RunConfig,
    Scenario,
    ScenarioSet,
    ShowcaseReport,
    Timer,
)
from tools.benchmark.recorder import SessionRecorder
from tools.benchmark.store import RunStore
from tools.benchmark.tools import get_tool_names, get_tools

# Lazy import to avoid circular — imported in _run_scenario_inner
# from tools.benchmark.agent import SimulatedAgent, build_system_prompt, run_scripted

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ScenarioRunner
# ---------------------------------------------------------------------------


class ScenarioRunner:
    """Orchestrates benchmark scenario execution against a live Intaris instance.

    Manages session lifecycle, tool evaluation, escalation handling, and
    analysis triggering for individual scenarios, scenario sets, and full
    showcase runs.
    """

    def __init__(self, config: RunConfig) -> None:
        self.config = config
        self.run_id = (
            f"bench-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid4().hex[:8]}"
        )
        # user_id: unique per run if not overridden
        self.user_id = config.user_id or f"bench-{self.run_id[-12:]}"
        self.store = RunStore(config.output_dir, self.run_id)
        self._intaris: IntarisClient | None = None
        self._llm: openai.OpenAI | None = None

    # ------------------------------------------------------------------
    # Lazy-init properties
    # ------------------------------------------------------------------

    @property
    def intaris(self) -> IntarisClient:
        """Lazy-init Intaris client."""
        if self._intaris is None:
            self._intaris = IntarisClient(
                base_url=self.config.intaris_url,
                api_key=self.config.intaris_api_key,
                user_id=self.user_id,
                agent_id=self.config.agent_id,
            )
        return self._intaris

    @property
    def llm(self) -> openai.OpenAI:
        """Lazy-init OpenAI client."""
        if self._llm is None:
            kwargs: dict[str, Any] = {"api_key": self.config.llm_api_key}
            if self.config.llm_base_url:
                kwargs["base_url"] = self.config.llm_base_url
            self._llm = openai.OpenAI(**kwargs)
        return self._llm

    # ------------------------------------------------------------------
    # Core: run a single scenario
    # ------------------------------------------------------------------

    def run_scenario(
        self,
        scenario: Scenario,
        *,
        parent_session_id: str | None = None,
    ) -> AgentResult:
        """Execute a single scenario.

        Steps:
          1. Generate unique session_id
          2. Resolve intensity (config override or scenario default)
          3. Get tools for the scenario
          4. Save scenario definition to store (including hidden_directive
             as ground truth)
          5. Create Intaris session (intention, details, policy,
             parent_session_id) — NEVER sends hidden_directive
          6. Build agent system prompt (includes hidden_directive +
             intensity)
          7. Create SessionRecorder
          8. Create SimulatedAgent and run it
          9. Record user message event for the initial task
         10. After agent completes: checkpoint, status, summary, flush,
             save session state
         11. Return AgentResult
        """
        timer = Timer()
        with timer:
            return self._run_scenario_inner(
                scenario, parent_session_id=parent_session_id, timer=timer
            )

    def _run_scenario_inner(
        self,
        scenario: Scenario,
        *,
        parent_session_id: str | None,
        timer: Timer,
    ) -> AgentResult:
        # 1. Generate session ID
        session_id = self._generate_session_id(scenario)

        # 2. Resolve intensity
        intensity = (
            self.config.intensity
            if self.config.intensity is not None
            else scenario.intensity
        )

        # 3. Get tools
        tools = get_tools(scenario.tools, scenario.category)
        tool_names = get_tool_names(tools)

        logger.info(
            "  Session: %s",
            session_id,
        )

        # 4. Dry-run: log what would happen, skip execution
        if self.config.dry_run:
            logger.info(
                "  [DRY-RUN] Would run %s (intensity=%.1f, tools=%s, max_turns=%d)",
                scenario.name,
                intensity,
                tool_names,
                self.config.max_turns or scenario.max_turns,
            )
            return AgentResult(
                scenario_name=scenario.name,
                session_id=session_id,
                turns=0,
                total_calls=0,
                duration_s=0.0,
            )

        # 5. Save scenario definition (ground truth — includes hidden_directive)
        scenario_data = asdict(scenario)
        scenario_data["_resolved_intensity"] = intensity
        scenario_data["_session_id"] = session_id
        scenario_data["_tools"] = tool_names
        self.store.save_scenario(scenario.name, scenario_data)

        # 6. Create Intaris session — NEVER send hidden_directive
        details = dict(scenario.details) if scenario.details else {}
        details.setdefault("source", "benchmark")
        # Only set a default working_directory for coding-related categories
        # (avoids triggering path-protection classifier for non-file scenarios)
        _FILE_CATEGORIES = {"coding", "adversarial", "gold", "hierarchical"}
        if "working_directory" not in details and scenario.category in _FILE_CATEGORIES:
            details["working_directory"] = "/home/bench/project"

        try:
            self.intaris.create_session(
                session_id,
                scenario.intention,
                details=details,
                policy=scenario.policy,
                parent_session_id=parent_session_id,
            )
        except IntarisError as exc:
            logger.error("Failed to create session %s: %s", session_id, exc)
            return AgentResult(
                scenario_name=scenario.name,
                session_id=session_id,
                turns=0,
                total_calls=0,
                duration_s=timer.elapsed_s,
                errors=[f"Session creation failed: {exc}"],
            )

        # 7. Create SessionRecorder
        recorder = SessionRecorder(self.intaris, session_id)

        # 8. Dispatch based on scenario mode
        if scenario.mode == "scripted":
            from tools.benchmark.agent import run_scripted

            result = run_scripted(
                scenario,
                self.intaris,
                session_id,
                recorder,
                self.store,
                config=self.config,
            )
        else:
            # Generative mode — LLM agent
            tools_desc = "Available tools: " + ", ".join(tool_names)
            system_prompt = build_system_prompt(scenario, intensity, tools_desc)

            agent = SimulatedAgent(
                llm_client=self.llm,
                model=self.config.llm_model,
                tools=tools,
                system_prompt=system_prompt,
                world_context=scenario.world_context,
            )
            result = agent.run(
                scenario,
                self.intaris,
                session_id,
                recorder,
                self.store,
                config=self.config,
            )

        # 10. Post-run: checkpoint, status, summary, flush, save state
        self._finalize_scenario(scenario, session_id, result, recorder)

        # Fill in timing
        result.duration_s = timer.elapsed_s

        # Log summary
        logger.info("  Turns: %d, Calls: %d", result.turns, result.total_calls)
        logger.info(
            "  Decisions: approve=%d, deny=%d, escalate=%d",
            result.decisions.get("approve", 0),
            result.decisions.get("deny", 0),
            result.decisions.get("escalate", 0),
        )

        return result

    def _finalize_scenario(
        self,
        scenario: Scenario,
        session_id: str,
        result: AgentResult,
        recorder: SessionRecorder,
    ) -> None:
        """Post-run lifecycle: checkpoint, status, summary, flush, save."""
        # Submit checkpoint with summary stats
        checkpoint_content = json.dumps(
            {
                "scenario": scenario.name,
                "turns": result.turns,
                "total_calls": result.total_calls,
                "decisions": result.decisions,
                "tools_called": result.tools_called[:20],
                "escalations_resolved": result.escalations_resolved,
                "errors": len(result.errors),
            },
            indent=2,
        )
        try:
            self.intaris.submit_checkpoint(session_id, checkpoint_content)
        except IntarisError:
            logger.debug("Failed to submit final checkpoint for %s", session_id)

        # Update session status to completed
        try:
            self.intaris.update_status(session_id, "completed")
        except IntarisError as exc:
            logger.warning("Failed to complete session %s: %s", session_id, exc)
            result.errors.append(f"Status update failed: {exc}")

        # Submit agent summary
        summary_text = (
            f"Benchmark scenario '{scenario.name}' completed. "
            f"Turns: {result.turns}, Calls: {result.total_calls}. "
            f"Decisions: {result.decisions}. "
            f"Escalations resolved: {result.escalations_resolved}."
        )
        if result.errors:
            summary_text += f" Errors: {len(result.errors)}."
        try:
            self.intaris.submit_agent_summary(session_id, summary_text)
        except IntarisError:
            logger.debug("Failed to submit agent summary for %s", session_id)

        # Flush recorder
        recorder.flush()

        # Save session state from Intaris
        try:
            session_state = self.intaris.get_session(session_id)
            self.store.save_session(scenario.name, session_state)
        except IntarisError:
            logger.debug("Failed to fetch session state for %s", session_id)

    # ------------------------------------------------------------------
    # Scenario sets (cross-session L3 testing)
    # ------------------------------------------------------------------

    def run_scenario_set(
        self,
        scenario_set: ScenarioSet,
    ) -> list[AgentResult]:
        """Run all scenarios in a set sequentially (same user/agent).

        For cross-session L3 testing.  All scenarios share the same
        user_id and agent_id but get separate sessions.
        """
        results: list[AgentResult] = []

        logger.info(
            "Running scenario set: %s (%d scenarios)",
            scenario_set.name,
            len(scenario_set.scenarios),
        )

        for i, scenario in enumerate(scenario_set.scenarios, 1):
            logger.info(
                "  [%d/%d] %s (intensity=%.1f)",
                i,
                len(scenario_set.scenarios),
                scenario.name,
                self.config.intensity
                if self.config.intensity is not None
                else scenario.intensity,
            )
            result = self.run_scenario(scenario)
            results.append(result)

        return results

    # ------------------------------------------------------------------
    # Showcase: full benchmark run
    # ------------------------------------------------------------------

    def run_showcase(self) -> ShowcaseReport:
        """Run all registered scenarios + trigger full analysis pipeline.

        Steps:
          1. Health check Intaris instance
          2. Save run metadata
          3. Import and iterate all scenarios
          4. If not skip_analysis: trigger L2, wait, trigger L3, wait,
             fetch profile
          5. Build and return ShowcaseReport
        """
        overall_timer = Timer()
        with overall_timer:
            return self._run_showcase_inner(overall_timer)

    def _run_showcase_inner(self, overall_timer: Timer) -> ShowcaseReport:
        # 1. Health check
        self._health_check()

        # 2. Import scenarios
        from tools.benchmark.scenarios import get_all_scenario_sets, get_all_scenarios

        scenarios = get_all_scenarios()
        scenario_sets = get_all_scenario_sets()
        total_items = len(scenarios) + len(scenario_sets)

        logger.info(
            "Phase 1: Running scenarios (%d individual + %d sets)",
            len(scenarios),
            len(scenario_sets),
        )

        results: list[AgentResult] = []
        session_ids: list[str] = []
        item_num = 0

        # Run individual scenarios
        for scenario in scenarios:
            item_num += 1
            intensity = (
                self.config.intensity
                if self.config.intensity is not None
                else scenario.intensity
            )
            logger.info(
                "[%d/%d] Running scenario: %s (intensity=%.1f)",
                item_num,
                total_items,
                scenario.name,
                intensity,
            )
            result = self.run_scenario(scenario)
            results.append(result)
            session_ids.append(result.session_id)
            logger.info("  Duration: %.1fs", result.duration_s)

        # Run scenario sets
        for sset in scenario_sets:
            item_num += 1
            logger.info(
                "[%d/%d] Running scenario set: %s (%d scenarios)",
                item_num,
                total_items,
                sset.name,
                len(sset.scenarios),
            )
            set_results = self.run_scenario_set(sset)
            results.extend(set_results)
            session_ids.extend(r.session_id for r in set_results)

        # 3. Save run metadata
        self._save_run_meta(session_ids)

        # 4. Analysis pipeline
        if not self.config.skip_analysis:
            self._trigger_analysis(session_ids)

        # 5. Build report
        report = self._build_report(results, session_ids)
        report.duration_s = overall_timer.elapsed_s

        total_calls = sum(r.total_calls for r in results)
        logger.info(
            "Showcase complete: %d scenarios, %d sessions, %d calls",
            len(results),
            len(session_ids),
            total_calls,
        )

        return report

    # ------------------------------------------------------------------
    # Filtered run
    # ------------------------------------------------------------------

    def run_filtered(
        self,
        *,
        scenario_name: str | None = None,
        category: str | None = None,
    ) -> ShowcaseReport:
        """Run specific scenario(s) filtered by name or category.

        Similar to showcase but only runs matching scenarios.
        """
        overall_timer = Timer()
        with overall_timer:
            return self._run_filtered_inner(
                scenario_name=scenario_name,
                category=category,
                overall_timer=overall_timer,
            )

    def _run_filtered_inner(
        self,
        *,
        scenario_name: str | None,
        category: str | None,
        overall_timer: Timer,
    ) -> ShowcaseReport:
        # Health check
        self._health_check()

        # Import scenarios
        from tools.benchmark.scenarios import get_all_scenario_sets, get_all_scenarios

        all_scenarios = get_all_scenarios()
        all_sets = get_all_scenario_sets()

        # Filter individual scenarios
        scenarios: list[Scenario] = []
        for s in all_scenarios:
            if scenario_name and s.name != scenario_name:
                continue
            if category and s.category != category:
                continue
            scenarios.append(s)

        # Filter scenario sets
        sets: list[ScenarioSet] = []
        for ss in all_sets:
            if category and ss.category != category:
                continue
            if scenario_name:
                # Check if any scenario in the set matches
                matching = [s for s in ss.scenarios if s.name == scenario_name]
                if not matching:
                    continue
            sets.append(ss)

        total_items = len(scenarios) + len(sets)
        if total_items == 0:
            logger.warning(
                "No scenarios match filters (name=%s, category=%s)",
                scenario_name,
                category,
            )
            return ShowcaseReport(run_id=self.run_id)

        logger.info(
            "Running %d individual scenarios + %d sets",
            len(scenarios),
            len(sets),
        )

        results: list[AgentResult] = []
        session_ids: list[str] = []
        item_num = 0

        for scenario in scenarios:
            item_num += 1
            intensity = (
                self.config.intensity
                if self.config.intensity is not None
                else scenario.intensity
            )
            logger.info(
                "[%d/%d] Running scenario: %s (intensity=%.1f)",
                item_num,
                total_items,
                scenario.name,
                intensity,
            )
            result = self.run_scenario(scenario)
            results.append(result)
            session_ids.append(result.session_id)
            logger.info("  Duration: %.1fs", result.duration_s)

        for sset in sets:
            item_num += 1
            logger.info(
                "[%d/%d] Running scenario set: %s (%d scenarios)",
                item_num,
                total_items,
                sset.name,
                len(sset.scenarios),
            )
            set_results = self.run_scenario_set(sset)
            results.extend(set_results)
            session_ids.extend(r.session_id for r in set_results)

        # Save run metadata
        self._save_run_meta(session_ids)

        # Analysis pipeline
        if not self.config.skip_analysis:
            self._trigger_analysis(session_ids)

        # Build report
        report = self._build_report(results, session_ids)
        report.duration_s = overall_timer.elapsed_s

        return report

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _generate_session_id(self, scenario: Scenario) -> str:
        """Generate URL-safe session ID."""
        safe_name = scenario.name.replace(".", "-").replace("_", "-")
        return f"sim-{safe_name}-{self.run_id[-8:]}"

    def _health_check(self) -> None:
        """Verify Intaris is reachable."""
        try:
            resp = self.intaris.health()
            logger.info("Intaris health check OK (%s)", resp.get("status", "unknown"))
        except IntarisError as exc:
            raise RuntimeError(
                f"Intaris not reachable at {self.config.intaris_url}: {exc}"
            ) from exc

    def _trigger_analysis(self, session_ids: list[str]) -> None:
        """Trigger L2 summaries and L3 analysis for all sessions.

        1. Trigger L2 for each session
        2. Wait for summary tasks to complete
        3. Trigger L3 analysis
        4. Wait for analysis tasks to complete
        5. Fetch and save profile + analysis results
        """
        if not session_ids:
            return

        # Phase 2: L2 summaries
        logger.info("Phase 2: Triggering L2 summaries...")
        for sid in session_ids:
            try:
                self.intaris.trigger_summary(sid)
                logger.debug("  Triggered summary for %s", sid)
            except IntarisError as exc:
                logger.warning("  Failed to trigger summary for %s: %s", sid, exc)

        if not self._wait_for_tasks("summary"):
            logger.warning("L2 summary tasks did not complete within timeout")

        # Save summaries
        for sid in session_ids:
            try:
                summaries = self.intaris.get_summary(sid)
                self.store.save_summaries(sid, summaries)
            except IntarisError:
                logger.debug("Failed to fetch summaries for %s", sid)

        # Phase 3: L3 analysis
        logger.info("Phase 3: Triggering L3 analysis...")
        try:
            self.intaris.trigger_analysis(agent_id=self.config.agent_id)
        except IntarisError as exc:
            logger.warning("Failed to trigger L3 analysis: %s", exc)
            return

        if not self._wait_for_tasks("analysis"):
            logger.warning("L3 analysis tasks did not complete within timeout")

        # Fetch and save profile + analysis
        try:
            profile = self.intaris.get_profile(agent_id=self.config.agent_id)
            self.store.save_profile(profile)
            logger.info("  Profile saved (risk_level=%s)", profile.get("risk_level"))
        except IntarisError:
            logger.debug("Failed to fetch behavioral profile")

        try:
            analysis = self.intaris.get_analysis(agent_id=self.config.agent_id)
            self.store.save_analysis(analysis)
        except IntarisError:
            logger.debug("Failed to fetch analysis results")

    def _wait_for_tasks(self, task_type: str, *, timeout_s: int = 300) -> bool:
        """Poll task queue until all tasks of given type complete.

        Uses exponential backoff: 2s -> 3s -> 4.5s -> ... -> 10s max.
        Returns True if completed, False on timeout.
        """
        delay = 2.0
        max_delay = 10.0
        elapsed = 0.0

        while elapsed < timeout_s:
            time.sleep(delay)
            elapsed += delay

            try:
                status = self.intaris.get_task_status(task_type=task_type)
            except IntarisError:
                logger.debug("Failed to poll task status for %s", task_type)
                delay = min(delay * 1.5, max_delay)
                continue

            pending = status.get("pending", 0)
            running = status.get("running", 0)
            active = pending + running

            if active == 0:
                completed = status.get("completed", 0)
                failed = status.get("failed", 0)
                logger.info(
                    "  %s tasks done: %d completed, %d failed",
                    task_type,
                    completed,
                    failed,
                )
                return True

            logger.debug(
                "  %s tasks: %d pending, %d running (%.0fs elapsed)",
                task_type,
                pending,
                running,
                elapsed,
            )
            delay = min(delay * 1.5, max_delay)

        return False

    def _save_run_meta(self, session_ids: list[str]) -> None:
        """Save run.json with config, timestamp, scenario list, totals."""
        meta: dict[str, Any] = {
            "run_id": self.run_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "user_id": self.user_id,
            "agent_id": self.config.agent_id,
            "intaris_url": self.config.intaris_url,
            "llm_model": self.config.llm_model,
            "intensity_override": self.config.intensity,
            "auto_resolve": self.config.auto_resolve,
            "delay_range": list(self.config.delay_range),
            "max_turns": self.config.max_turns,
            "max_calls": self.config.max_calls,
            "skip_analysis": self.config.skip_analysis,
            "dry_run": self.config.dry_run,
            "session_ids": session_ids,
            "session_count": len(session_ids),
        }
        self.store.save_run_meta(meta)
        logger.debug("Run metadata saved: %s", self.store.run_dir / "run.json")

    def _build_report(
        self, results: list[AgentResult], session_ids: list[str]
    ) -> ShowcaseReport:
        """Aggregate AgentResults into a ShowcaseReport."""
        report = ShowcaseReport(run_id=self.run_id)
        report.session_ids = list(session_ids)
        report.results = list(results)

        for r in results:
            if r.errors:
                report.scenarios_failed += 1
            else:
                report.scenarios_completed += 1

            report.total_turns += r.turns
            report.total_calls += r.total_calls

            for decision, count in r.decisions.items():
                report.decisions[decision] = report.decisions.get(decision, 0) + count

        report.sessions_created = len(session_ids)

        # Try to get L2/L3 stats from saved data
        for sid in session_ids:
            summaries = self.store.load_summaries(sid)
            if summaries:
                intaris_summaries = summaries.get("intaris_summaries", [])
                report.l2_summaries += len(intaris_summaries)

        profile = self.store.load_profile()
        if profile:
            report.l3_risk_level = profile.get("risk_level")
            alerts = profile.get("active_alerts", [])
            report.l3_active_alerts = len(alerts) if isinstance(alerts, list) else 0

        # Collect errors
        for r in results:
            for err in r.errors:
                report.errors.append(f"[{r.scenario_name}] {err}")

        return report

    def cleanup(self) -> None:
        """Close the Intaris client."""
        if self._intaris is not None:
            self._intaris.close()
            self._intaris = None


# ---------------------------------------------------------------------------
# Module-level convenience functions (used by CLI)
# ---------------------------------------------------------------------------


def run_showcase(config: RunConfig) -> ShowcaseReport:
    """Run all registered scenarios + trigger full analysis pipeline.

    Convenience wrapper around ``ScenarioRunner.run_showcase()``.
    """
    runner = ScenarioRunner(config)
    try:
        report = runner.run_showcase()
        _log_report(report)
        return report
    finally:
        runner.cleanup()


def run_filtered(
    config: RunConfig,
    *,
    scenario: str | None = None,
    category: str | None = None,
) -> ShowcaseReport:
    """Run specific scenario(s) filtered by name or category.

    Convenience wrapper around ``ScenarioRunner.run_filtered()``.
    """
    runner = ScenarioRunner(config)
    try:
        report = runner.run_filtered(scenario_name=scenario, category=category)
        _log_report(report)
        return report
    finally:
        runner.cleanup()


def _log_report(report: ShowcaseReport) -> None:
    """Log a human-readable report summary."""
    logger.info("=" * 60)
    logger.info("Benchmark Run: %s", report.run_id)
    logger.info("=" * 60)
    logger.info(
        "Scenarios: %d completed, %d failed",
        report.scenarios_completed,
        report.scenarios_failed,
    )
    logger.info("Sessions: %d", report.sessions_created)
    logger.info("Total turns: %d", report.total_turns)
    logger.info("Total calls: %d", report.total_calls)
    logger.info(
        "Decisions: approve=%d, deny=%d, escalate=%d",
        report.decisions.get("approve", 0),
        report.decisions.get("deny", 0),
        report.decisions.get("escalate", 0),
    )
    if report.l2_summaries:
        logger.info("L2 summaries: %d", report.l2_summaries)
    if report.l3_risk_level is not None:
        logger.info(
            "L3 risk level: %d (alerts: %d)",
            report.l3_risk_level,
            report.l3_active_alerts,
        )
    logger.info("Duration: %.1fs", report.duration_s)
    if report.errors:
        logger.warning("Errors (%d):", len(report.errors))
        for err in report.errors[:10]:
            logger.warning("  %s", err)
        if len(report.errors) > 10:
            logger.warning("  ... and %d more", len(report.errors) - 10)
