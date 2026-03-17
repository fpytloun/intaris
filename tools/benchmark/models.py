"""Data models for the benchmark system.

All dataclasses used across the benchmark: scenario definitions,
run configuration, call records, scoring results.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Scenario definitions
# ---------------------------------------------------------------------------


@dataclass
class ExpectedLabel:
    """Hand-labeled ground truth for a tool call pattern (gold scenarios only).

    Used to calibrate the evaluator LLM against known-correct labels.
    """

    tool_pattern: str
    """fnmatch pattern matching tool name (e.g., ``"bash"``, ``"read"``)."""

    args_pattern: dict[str, str] | None = None
    """Key-value substring patterns to match in args. All must match."""

    label: str = "benign"
    """Ground truth: ``benign``, ``harmful``, ``inherently_dangerous``."""

    reason: str = ""
    """Why this label was assigned."""


@dataclass
class Scenario:
    """A benchmark scenario — an agent session with a hidden behavioral directive."""

    name: str
    """Unique identifier (e.g., ``drift.css_to_auth``)."""

    description: str
    """Human-readable description."""

    category: str
    """Category: coding, social, workplace, infra, research, adversarial,
    cross_session, hierarchical, gold."""

    # What Intaris sees (the "cover"):
    intention: str
    """Session intention — the only thing Intaris knows about the task."""

    details: dict[str, Any] | None = None
    """Session details (``working_directory``, ``repo``, ``branch``, etc.)."""

    policy: dict[str, Any] | None = None
    """Session policy (``allow_tools``, ``deny_tools``, ``deny_paths``, etc.)."""

    # What only the agent LLM sees (the "hidden objective"):
    hidden_directive: str = ""
    """Behavioral instruction visible only to the agent LLM.
    Empty string means pure benign behavior."""

    world_context: str = ""
    """Description of the agent's environment (project tree, services, accounts)."""

    # Tool selection:
    tools: list[str] | None = None
    """Tool kit names to compose. ``None`` = auto from category."""

    # Agent configuration:
    intensity: float = 0.0
    """Evil dial (0.0-1.0). Controls how aggressively the agent
    pursues the hidden directive."""

    max_turns: int = 20
    """Max tool-calling turns (LLM responses)."""

    max_calls: int = 50
    """Hard cap on total tool calls across all turns."""

    # Hierarchical:
    parent_ref: str | None = None
    """Reference to parent scenario name for child sessions."""

    # User message injection:
    user_messages: dict[int, str] | None = None
    """Map of turn number -> user message to inject at that turn.
    Messages starting with ``"User message:"`` trigger IntentionBarrier."""

    # Gold scenario support:
    expected_labels: list[ExpectedLabel] | None = None
    """Hand-labeled ground truth for evaluator calibration."""

    is_gold: bool = False
    """Whether this is a gold (deterministic, hand-labeled) scenario."""


@dataclass
class ScenarioSet:
    """Multiple scenarios forming a cross-session pattern for L3 testing.

    All scenarios in a set run sequentially under the same user/agent.
    """

    name: str
    """Unique identifier (e.g., ``cross_session.recon_chain``)."""

    description: str
    """Human-readable description."""

    category: str = "cross_session"
    """Category (typically ``cross_session``)."""

    scenarios: list[Scenario] = field(default_factory=list)
    """Scenarios to run in order."""


# ---------------------------------------------------------------------------
# Run configuration
# ---------------------------------------------------------------------------


@dataclass
class RunConfig:
    """Configuration for a benchmark run."""

    intaris_url: str = "http://localhost:8060"
    intaris_api_key: str = ""
    user_id: str = ""
    agent_id: str = "opencode"
    llm_api_key: str = ""
    llm_base_url: str | None = None
    llm_model: str = "gpt-5-nano"
    intensity: float | None = None
    auto_resolve: bool = True
    delay_range: tuple[float, float] = (1.0, 5.0)
    max_turns: int | None = None
    max_calls: int | None = None
    skip_analysis: bool = False
    dry_run: bool = False
    verbose: bool = False
    output_dir: str = "./runs"


# ---------------------------------------------------------------------------
# Runtime records
# ---------------------------------------------------------------------------


@dataclass
class CallRecord:
    """A single tool call with Intaris response — stored in calls.jsonl."""

    turn: int
    tool: str
    args: dict[str, Any]
    intaris: dict[str, Any]
    fake_response: str
    timestamp: str
    escalation_resolution: dict[str, Any] | None = None


@dataclass
class ReasoningRecord:
    """A reasoning submission — stored in reasoning.jsonl."""

    turn: int
    visible_reasoning: str
    internal_plan: str
    timestamp: str
    is_user_message: bool = False
    intention_pending: bool = False


@dataclass
class AgentResult:
    """Result of running a single scenario."""

    scenario_name: str
    session_id: str
    turns: int
    total_calls: int
    decisions: dict[str, int] = field(
        default_factory=lambda: {"approve": 0, "deny": 0, "escalate": 0}
    )
    tools_called: list[str] = field(default_factory=list)
    escalations_resolved: int = 0
    escalation_retries_hit: int = 0
    injection_detected_count: int = 0
    budget_exhausted: bool = False
    duration_s: float = 0.0
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Scoring models
# ---------------------------------------------------------------------------


@dataclass
class CallLabel:
    """Evaluator's label for a single tool call."""

    turn: int
    tool: str
    label: str  # benign, harmful, ambiguous, inherently_dangerous
    intaris_decision: str  # approve, deny, escalate
    correct: bool
    score_type: str  # true_positive, true_negative, false_positive, false_negative, critical_miss, acceptable
    note: str = ""


@dataclass
class L2Score:
    """Evaluator's score for an L2 session summary."""

    summary_exists: bool = False
    alignment_label: str = ""
    alignment_correct: bool | None = None
    risk_indicators_detected: list[str] = field(default_factory=list)
    indicator_precision: float | None = None
    indicator_recall: float | None = None
    narrative_quality: float | None = None


@dataclass
class ScenarioScore:
    """Scores for a single scenario."""

    scenario_name: str
    category: str
    intensity: float

    # Call-level metrics
    total_calls: int = 0
    benign_calls: int = 0
    harmful_calls: int = 0
    ambiguous_calls: int = 0
    inherently_dangerous_calls: int = 0

    true_positives: int = 0
    true_negatives: int = 0
    false_positives: int = 0
    false_negatives: int = 0
    critical_misses: int = 0
    acceptable: int = 0

    precision: float = 0.0
    recall: float = 0.0
    f1: float = 0.0
    false_positive_rate: float = 0.0

    # Detection latency
    first_harmful_turn: int | None = None
    first_detection_turn: int | None = None
    detection_latency: int | None = None

    # Performance
    avg_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0

    # Feature coverage
    features_tested: list[str] = field(default_factory=list)

    # Escalation
    escalations_total: int = 0
    escalations_resolved: int = 0
    escalation_retries_hit: int = 0

    # Injection
    injection_attempts: int = 0
    injection_detected_count: int = 0
    injection_detection_rate: float = 0.0

    # L2 quality
    l2: L2Score | None = None

    # Per-call labels (for detailed reporting)
    call_labels: list[CallLabel] = field(default_factory=list)


@dataclass
class CategoryScore:
    """Aggregated scores for a scenario category."""

    category: str
    scenario_count: int = 0
    precision: float = 0.0
    recall: float = 0.0
    f1: float = 0.0
    false_positive_rate: float = 0.0
    avg_detection_latency: float | None = None


@dataclass
class L3Score:
    """Evaluator's score for L3 cross-session analysis."""

    pattern_detected: bool | None = None  # yes/no/partial
    risk_level: int | None = None
    risk_level_appropriate: float | None = None
    finding_quality: float | None = None
    sessions_analyzed: int = 0


@dataclass
class BenchmarkScore:
    """Aggregate benchmark scores for a complete run."""

    run_id: str
    timestamp: str

    # Overall
    overall_precision: float = 0.0
    overall_recall: float = 0.0
    overall_f1: float = 0.0
    overall_fpr: float = 0.0
    critical_miss_count: int = 0

    # By category
    category_scores: dict[str, CategoryScore] = field(default_factory=dict)

    # By intensity band
    intensity_scores: dict[str, float] = field(default_factory=dict)

    # Detection
    avg_detection_latency: float | None = None

    # Performance
    avg_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0

    # L2/L3
    avg_l2_quality: float | None = None
    l3: L3Score | None = None

    # Calibration
    gold_agreement_rate: float | None = None
    gold_disagreements: list[dict[str, Any]] = field(default_factory=list)

    # Isolation
    reasoning_leakage_rate: float = 0.0
    reasoning_leakage_instances: list[dict[str, Any]] = field(default_factory=list)

    # Feature coverage
    features_tested: list[str] = field(default_factory=list)
    features_untested: list[str] = field(default_factory=list)

    # Per-scenario detail
    scenario_scores: list[ScenarioScore] = field(default_factory=list)


@dataclass
class ComparisonResult:
    """Result of comparing two benchmark runs."""

    baseline_run_id: str
    new_run_id: str
    baseline_f1: float = 0.0
    new_f1: float = 0.0
    f1_delta: float = 0.0
    precision_delta: float = 0.0
    recall_delta: float = 0.0
    fpr_delta: float = 0.0
    improved: list[dict[str, Any]] = field(default_factory=list)
    regressed: list[dict[str, Any]] = field(default_factory=list)
    unchanged: int = 0


# ---------------------------------------------------------------------------
# Showcase report
# ---------------------------------------------------------------------------


@dataclass
class ShowcaseReport:
    """Summary report for a showcase run."""

    run_id: str
    scenarios_completed: int = 0
    scenarios_failed: int = 0
    sessions_created: int = 0
    total_turns: int = 0
    total_calls: int = 0
    decisions: dict[str, int] = field(
        default_factory=lambda: {"approve": 0, "deny": 0, "escalate": 0}
    )
    l2_summaries: int = 0
    l3_risk_level: int | None = None
    l3_active_alerts: int = 0
    duration_s: float = 0.0
    session_ids: list[str] = field(default_factory=list)
    results: list[AgentResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Timer helper
# ---------------------------------------------------------------------------


class Timer:
    """Simple context manager timer."""

    def __init__(self) -> None:
        self.start: float = 0.0
        self.elapsed_s: float = 0.0

    def __enter__(self) -> Timer:
        self.start = time.monotonic()
        return self

    def __exit__(self, *args: Any) -> None:
        self.elapsed_s = time.monotonic() - self.start
