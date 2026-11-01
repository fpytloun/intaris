"""Safety evaluator for intaris.

Orchestrates the full evaluation pipeline:
  classify → critical check → LLM evaluation → decision matrix → audit

This is the main entry point for tool call evaluation. The /evaluate
API endpoint delegates to this module.

For MCP proxy calls, the evaluator also supports:
- Tool preference overrides (auto-approve, escalate, deny)
- Escalation retry: reuses a prior approval if the same tool+args
  combination was approved within the last 10 minutes.
- args_hash storage for escalation retry lookups.
- Approved path prefix cache: learns from both LLM approvals and
  user-approved escalations to fast-path subsequent reads to the same
  out-of-project directory. Prefixes are merged when they share a deep
  common ancestor (e.g., different npm packages under the same scope).
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Generator
from typing import TYPE_CHECKING, Any

from intaris.audit import AuditStore
from intaris.classifier import (
    Classification,
    classify,
    extract_apply_patch_paths,
    extract_paths,
    is_path_within,
    is_read_only,
    reads_secret_location,
    resolve_path,
    resolve_tool_paths,
)
from intaris.config import AnalysisConfig
from intaris.db import Database
from intaris.decision import (
    UNATTENDED_JUDGE_REVIEW,
    Decision,
    EvaluationResult,
    apply_decision_matrix,
    cap_outcome,
    clamp_outcome,
    make_fast_decision,
)
from intaris.evaluation_flow import BackendSelection, run_async, run_sync
from intaris.evaluator_backend import EvaluatorBackend
from intaris.llm import STRUCTURED_OUTPUT_MAX_TOKENS, LLMClient, parse_json_response
from intaris.policy import effective_policy_for_evaluator, normalized_policy_clauses
from intaris.precedent import find_authoritative_precedent
from intaris.prompts import (
    SAFETY_EVALUATION_SCHEMA,
    SAFETY_EVALUATION_SYSTEM_PROMPT,
    build_evaluation_user_prompt,
)
from intaris.redactor import redact
from intaris.sanitize import ANTI_INJECTION_PREAMBLE
from intaris.session import SessionStore

if TYPE_CHECKING:
    from intaris.jev import JevClient

# Escalation retry: reuse approval if same tool+args approved within this window.
_ESCALATION_RETRY_TTL_MINUTES = 10

# Maximum number of approved path prefixes per session (FIFO eviction).
_MAX_APPROVED_PATHS_PER_SESSION = 50

# Minimum common ancestor depth (in path components) for prefix merging.
# Prevents merging to overly broad prefixes like /Users or /home.
# Example: /Users/foo/.cache/opencode = 4 components (excluding root).
_MIN_MERGE_DEPTH = 4
_CONTEXT_ARG_KEY = "__intaris_context"

# Edit tools whose approval opens a session write scope (approved workspace
# edits). bash is deliberately excluded: its targets are not determinable.
_WORKSPACE_EDIT_TOOLS = frozenset({"edit", "write", "multiedit", "apply_patch"})
# Directory names that hold deploy manifests (k8s/helm); edits under them
# always go to the evaluator model.
_DEPLOY_DIRS = frozenset(
    {"deploy", "deployment", "deployments", "k8s", "kubernetes", "helm"}
)

logger = logging.getLogger(__name__)


def _args_redacted_with_context(
    args_redacted: dict[str, Any],
    context: dict[str, Any] | None,
) -> dict[str, Any]:
    """Attach redacted evaluate context to audit args without clobbering tool args."""

    if not context:
        return args_redacted
    result = dict(args_redacted)
    result[_CONTEXT_ARG_KEY] = context
    if "context" not in result:
        result["context"] = context
    return result


def _apply_authoritative_user_precedent(
    evaluation: EvaluationResult,
    *,
    tool: str,
    args_redacted: dict[str, Any],
    user_decisions: list[dict[str, Any]],
) -> EvaluationResult:
    """Apply final human same-tool precedent to low/medium-risk evaluations.

    This is a narrow guard against stale intention drift: if the LLM marks a
    low/medium-risk call as not aligned, but the same session has a recent
    final human approval for the same tool (and no newer human denial for that
    tool), honor the human precedent and treat the call as aligned.
    """
    risk = evaluation.risk.lower()
    if evaluation.aligned or risk not in ("low", "medium"):
        return evaluation

    precedent = find_authoritative_precedent(tool, args_redacted, user_decisions)
    if not precedent:
        return evaluation

    note = str(precedent.get("user_note") or "").strip()
    prior_tool = str(precedent.get("tool") or "").strip()
    reasoning = (
        "Final human approval for a sufficiently similar operation in this session is "
        "authoritative precedent. " + evaluation.reasoning
    )
    if prior_tool and prior_tool != tool:
        reasoning += f" Prior approved tool: {prior_tool}."
    if note:
        reasoning += f' User note: "{note}".'

    return EvaluationResult(
        aligned=True,
        risk=evaluation.risk,
        reasoning=reasoning,
        decision="approve",
        metadata=evaluation.metadata,
    )


def _resolve_tool_paths_for_context(
    tool: str,
    args: dict[str, Any],
    working_directory: str,
) -> list[str]:
    """Resolve tool target paths for evaluator policy context."""
    return resolve_tool_paths(tool, args, working_directory)


def _effective_working_directory(
    tool: str,
    args: dict[str, Any],
    context: dict[str, Any] | None,
    session_details: dict[str, Any],
) -> str | None:
    """Resolve the directory that applies to this specific tool call."""

    context = context or {}
    executor_environment = context.get("executor_environment")
    executor_metadata_present = isinstance(executor_environment, dict)
    candidates = [
        context.get("working_directory"),
        (executor_environment.get("cwd") if executor_metadata_present else None),
    ]
    if not executor_metadata_present:
        candidates.append(session_details.get("working_directory"))
    base_directory = next(
        (
            os.path.normpath(value)
            for value in candidates
            if isinstance(value, str) and os.path.isabs(value)
        ),
        None,
    )
    if tool == "bash":
        tool_workdir = args.get("workdir")
        if isinstance(tool_workdir, str) and tool_workdir:
            if os.path.isabs(tool_workdir):
                return os.path.normpath(tool_workdir)
            if base_directory:
                return os.path.normpath(os.path.join(base_directory, tool_workdir))
    return base_directory


def _build_path_policy_context(
    *,
    resolved_paths: list[str],
    working_directory: str,
    policy: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build deterministic path-policy facts for LLM evaluation."""
    policy = policy or {}
    allow_paths = [p for p in policy.get("allow_paths", []) if isinstance(p, str)]
    deny_paths = [p for p in policy.get("deny_paths", []) if isinstance(p, str)]
    effective_policy = effective_policy_for_evaluator(policy) or {}
    effective_allow_paths = [
        p for p in effective_policy.get("allow_paths", []) if isinstance(p, str)
    ]
    working_directory_allowed = (
        bool(working_directory)
        and bool(allow_paths)
        and any(
            _path_allowed_by_pattern(working_directory, pattern)
            for pattern in allow_paths
        )
    )
    working_directory_denied = bool(working_directory) and any(
        _path_allowed_by_pattern(working_directory, pattern) for pattern in deny_paths
    )
    return {
        "working_directory": working_directory,
        "working_directory_allowed_by_policy": working_directory_allowed,
        "working_directory_denied_by_policy": working_directory_denied,
        "target_paths": resolved_paths,
        "inside_working_directory": [
            path for path in resolved_paths if is_path_within(path, working_directory)
        ],
        "outside_working_directory": [
            path
            for path in resolved_paths
            if not is_path_within(path, working_directory)
        ],
        "allow_paths": effective_allow_paths,
        "deny_paths": deny_paths,
        "all_targets_allowed_by_policy": bool(resolved_paths)
        and bool(allow_paths)
        and all(
            any(fnmatch.fnmatch(path, pattern) for pattern in allow_paths)
            for path in resolved_paths
        ),
        "any_target_denied_by_policy": bool(resolved_paths)
        and any(
            any(fnmatch.fnmatch(path, pattern) for pattern in deny_paths)
            for path in resolved_paths
        ),
    }


def _path_allowed_by_pattern(path: str, pattern: str) -> bool:
    """Match path policy patterns against a path or the root they imply."""

    if fnmatch.fnmatch(path, pattern):
        return True
    if pattern.endswith("/*") and path == pattern[:-2]:
        return True
    return False


def _edit_targets(
    tool: str, args: dict[str, Any], working_directory: str | None
) -> list[str] | None:
    """Return every resolved target of a covered edit call.

    Returns None for other tools and whenever a target cannot be determined
    from the arguments alone. The targets equal ``resolve_tool_paths`` so the
    classifier, deny_paths and hybrid observations see the same set.
    """
    name = tool.lower()
    if name not in _WORKSPACE_EDIT_TOOLS or (
        name == "apply_patch" and tool != "apply_patch"
    ):
        return None
    raw = extract_paths(args)
    if name == "apply_patch":
        patch = args.get("patchText")
        headers = extract_apply_patch_paths(patch) if isinstance(patch, str) else []
        # Deleting files is a delete effect that needs its own assessment.
        if not headers or re.search(
            r"^(?:\*\*\* Delete File:|\+\+\+ /dev/null)", patch, re.MULTILINE
        ):
            return None
        raw.extend(headers)
    if not raw or any(path.startswith("~") for path in raw):
        return None
    if not working_directory and not all(os.path.isabs(path) for path in raw):
        return None
    targets = [resolve_path(path, working_directory or "") for path in raw]
    if name == "multiedit":
        # Per-edit paths must repeat the call's own target; anything else is
        # not seen by the path checks and needs the model.
        edits = args.get("edits")
        for item in edits if isinstance(edits, list) else []:
            nested = extract_paths(item) if isinstance(item, dict) else []
            if any(
                resolve_path(path, working_directory or "") not in targets
                for path in nested
            ):
                return None
    return targets


def _denied_targets(
    tool: str, args: dict[str, Any], working_directory: str | None
) -> list[str]:
    """Every target a human denial touched, independent of cache eligibility.

    Includes deleted and moved patch paths and per-edit multiedit paths.
    """
    if tool.lower() == "apply_patch":
        tool = "apply_patch"
    targets = resolve_tool_paths(tool, args, working_directory or "")
    edits = args.get("edits")
    for item in edits if isinstance(edits, list) else []:
        if isinstance(item, dict):
            targets.extend(
                resolve_path(path, working_directory or "")
                for path in extract_paths(item)
            )
    return targets


def _recorded_call(
    record: dict[str, Any], session_details: dict[str, Any]
) -> tuple[str, dict[str, Any], dict[str, Any], str | None]:
    """Rebuild tool, args, call context and working directory of an audit row.

    Legacy rows keep the call context inside ``args_redacted``; hybrid rows
    keep it as ``evaluation_metadata.agent_reason`` plus the mode overlay.
    """
    tool = record.get("tool") or ""
    args = record.get("args_redacted")
    args = dict(args) if isinstance(args, dict) else {}
    legacy_context = args.pop(_CONTEXT_ARG_KEY, None)
    meta = record.get("evaluation_metadata") or {}
    context = dict(meta.get("agent_reason") or legacy_context or {})
    overlay = meta.get("overlay")
    if overlay:
        context["chat_mode"] = overlay["chat_mode"]
        context["read_only_required"] = overlay["read_only_required"]
    working_directory = _effective_working_directory(
        tool, args, context, session_details
    )
    return tool, args, context, working_directory


def _protected_edit_target(path: str) -> bool:
    """Git internals and CI/CD or deploy configuration always need the model."""
    segments = path.replace("\\", "/").lower().split("/")
    return (
        ".git" in segments
        or any(
            first == ".github" and second == "workflows"
            for first, second in zip(segments, segments[1:])
        )
        or segments[-1] in {".gitlab-ci.yml", "jenkinsfile"}
        or any(segment in _DEPLOY_DIRS for segment in segments[:-1])
    )


class Evaluator:
    """Orchestrates tool call safety evaluation.

    Combines classification, LLM evaluation, decision matrix, and
    audit logging into a single pipeline.
    """

    def __init__(
        self,
        *,
        llm: LLMClient,
        session_store: SessionStore,
        audit_store: AuditStore,
        db: Database | None = None,
        analysis_config: AnalysisConfig | None = None,
        alignment_barrier: Any | None = None,
        jev: JevClient | None = None,
        evaluator_backend: EvaluatorBackend | None = None,
        task_context: Any | None = None,
        l1_input_projection: str = "auto",
        event_store: Any | None = None,
    ):
        self.event_store = event_store
        self._llm = llm
        # A pre-built ``jev`` pins that client; otherwise the selector decides
        # per call (``EVALUATOR_BACKEND=auto`` follows the Cognis route).
        self._backend = evaluator_backend or EvaluatorBackend.fixed(jev)
        self._task_context = task_context
        self._l1_input_projection = l1_input_projection
        self._sessions = session_store
        self._audit = audit_store
        self._db = db
        self._analysis_config = analysis_config
        self._analysis_enabled = analysis_config is not None and analysis_config.enabled
        self._alignment_barrier = alignment_barrier

        # Approved path prefixes per session.
        # Key: (user_id, session_id) → list of normalized directory prefixes.
        # When a read-only tool call that was reclassified to WRITE due to
        # path policy is approved (by LLM or by user via escalation), the
        # evaluator caches the approved directory prefix. Subsequent reads
        # under that prefix are fast-pathed as READ without LLM evaluation.
        # Prefixes are merged when they share a deep common ancestor.
        self._approved_paths: dict[tuple[str, str], list[str]] = {}
        # Approved write scopes per session, guarded by the same lock. Valid
        # only for the (policy_revision, state_revision) they were approved
        # at: new user evidence or a policy change resets them.
        self._approved_edit_scopes: dict[
            tuple[str, str], tuple[tuple[int, int], list[str]]
        ] = {}
        self._approved_paths_lock = threading.Lock()

    @property
    def _jev(self) -> JevClient | None:
        """Per-call backend: the Jev client, or ``None`` for the LLM evaluator."""
        return self._backend.select()

    def evaluate(
        self,
        *,
        user_id: str,
        session_id: str,
        agent_id: str | None,
        tool: str,
        args: dict[str, Any],
        context: dict[str, Any] | None = None,
        tool_preferences: dict[str, str] | None = None,
        minimum_outcome: str | None = None,
        approval_call_id: str | None = None,
        judge_unattended: bool = False,
        evaluation_policy_version: str | None = None,
        logical_call_id: str | None = None,
        expected_policy_revision: int | None = None,
        expected_policy_hash: str | None = None,
        controller_context: dict | None = None,
        process_lifecycle: dict | None = None,
        original_args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Evaluate synchronously using the canonical resumable pipeline."""
        return run_sync(
            self._evaluation_flow(
                user_id=user_id,
                session_id=session_id,
                agent_id=agent_id,
                tool=tool,
                args=args,
                context=context,
                tool_preferences=tool_preferences,
                minimum_outcome=minimum_outcome,
                approval_call_id=approval_call_id,
                judge_unattended=judge_unattended,
                evaluation_policy_version=evaluation_policy_version,
                logical_call_id=logical_call_id,
                expected_policy_revision=expected_policy_revision,
                expected_policy_hash=expected_policy_hash,
                controller_context=controller_context,
                process_lifecycle=process_lifecycle,
                original_args=original_args,
            )
        )

    async def evaluate_async(self, **kwargs: Any) -> dict[str, Any]:
        """Evaluate without retaining worker slots during metadata waits."""
        return await run_async(self._evaluation_flow(**kwargs))

    def _deny_inactive_parent(
        self,
        session,
        user_id,
        agent_id,
        tool,
        args,
        context,
        call_id,
        minimum_outcome,
        start_time,
    ):
        """Keep the existing audited lifecycle denial for inherited hard stops."""
        parent = session
        while parent.get("parent_session_id"):
            parent = self._sessions.get(parent["parent_session_id"], user_id=user_id)
            if parent.get("status") in {"suspended", "terminated"}:
                break
        status = parent.get("status", "suspended")
        if status not in {"suspended", "terminated"}:
            from intaris.policy_activation import HybridError

            raise HybridError(
                "policy_state_changed",
                "Parent lifecycle changed during evaluation",
                409,
                retryable=True,
            )
        reason = f"Session suspended — parent session is {status}"
        self._sessions.update_status(
            session["session_id"],
            "suspended",
            user_id=user_id,
            status_reason=f"Parent session is {status}",
        )
        maximum = (session.get("policy") or {}).get("maximum_outcome")
        maximum = (context or {}).get("maximum_outcome", maximum)
        latency = int((time.monotonic() - start_time) * 1000)
        self._audit.insert(
            call_id=call_id,
            user_id=user_id,
            session_id=session["session_id"],
            agent_id=agent_id,
            tool=tool,
            args_redacted=_args_redacted_with_context(
                redact(args), redact(context) if context else None
            ),
            classification="write",
            evaluation_path="fast",
            decision="deny",
            risk="low",
            reasoning=reason,
            latency_ms=latency,
            maximum_outcome=maximum,
            minimum_outcome=minimum_outcome,
            outcome_override="hard_stop.parent_lifecycle",
        )
        self._sessions.increment_counter(session["session_id"], "deny", user_id=user_id)
        return {
            "call_id": call_id,
            "decision": "deny",
            "risk": "low",
            "reasoning": reason,
            "path": "fast",
            "latency_ms": latency,
            "session_status": "suspended",
            "status_reason": f"Parent session is {status}",
            "args_redacted": _args_redacted_with_context(
                redact(args), redact(context) if context else None
            ),
            "classification": "write",
            "minimum_outcome": minimum_outcome,
            **(
                {
                    "raw_decision": "deny",
                    "effective_decision": "deny",
                    "maximum_outcome": maximum,
                    "outcome_override": "hard_stop.parent_lifecycle",
                }
                if maximum is not None
                else {}
            ),
        }

    def waits_for_alignment(self, *, user_id: str, session_id: str) -> bool:
        """Whether the active policy needs a compatibility verdict before admission.

        This is a latency hint, not permission: evaluation reloads the policy
        and still enforces instructions, hazards and lifecycle hard stops.
        """
        try:
            session = self._sessions.get(session_id, user_id=user_id)
        except ValueError:
            # Admission, not this latency hint, owns HTTP not-found handling.
            return True
        policy = session.get("policy") or {}
        snapshot = policy.get("active_policy") or {}
        if not session.get("guardrails_enabled", True):
            return False
        return not (
            policy.get("evaluation_policy_version") == "hybrid_v1"
            and (snapshot.get("profile") or policy.get("profile")) == "bounded_autonomy"
        )

    def _evaluation_flow(
        self,
        *,
        user_id: str,
        session_id: str,
        agent_id: str | None,
        tool: str,
        args: dict[str, Any],
        context: dict[str, Any] | None = None,
        tool_preferences: dict[str, str] | None = None,
        minimum_outcome: str | None = None,
        approval_call_id: str | None = None,
        judge_unattended: bool = False,
        evaluation_policy_version: str | None = None,
        logical_call_id: str | None = None,
        expected_policy_revision: int | None = None,
        expected_policy_hash: str | None = None,
        controller_context: dict | None = None,
        process_lifecycle: dict | None = None,
        original_args: dict[str, Any] | None = None,
    ) -> Generator[BackendSelection, Any, dict[str, Any]]:
        """Evaluate a tool call for safety and intention alignment.

        This is the main entry point for the evaluation pipeline:
        1. Redact secrets from args
        2. Classify the tool call (read/write/critical/escalate)
        3. For read-only: auto-approve (fast path)
        4. For critical: auto-deny (fast path)
        5. For escalate: check retry cache, else escalate (fast path)
        6. For write: LLM safety evaluation → decision matrix
        7. Log audit record (with args_hash for escalation retry)
        8. Update session counters

        Args:
            user_id: Tenant identifier.
            session_id: Session this call belongs to.
            agent_id: Agent making the call (optional).
            tool: Tool name (e.g., "bash", "edit", "mcp:add_memory").
            args: Tool arguments (will be redacted before storage).
            context: Optional additional context for evaluation.
            tool_preferences: Optional per-tool preference overrides
                mapping 'server:tool' → preference string. Used by
                the MCP proxy to pass user-configured tool policies.

        Returns:
            Dict with: call_id, decision, reasoning, risk, path, latency_ms.

        Raises:
            ValueError: If session not found.
        """
        start_time = time.monotonic()
        from intaris.evaluation_context import normalize

        context = normalize(
            context,
            user_id=user_id,
            session_id=session_id,
            evaluated_tool=tool,
            args=args,
        )
        from intaris.recent_actions import outcomes

        context["recent_outcomes"] = outcomes(
            self.event_store,
            {"user_id": user_id, "session_id": session_id},
            context.get("recent_outcomes"),
        )
        from intaris.session_scripts import attach as attach_session_script

        attach_session_script(
            context, self.event_store, user_id, session_id, tool, args
        )
        call_id = str(uuid.uuid4())
        clamp_outcome("approve", minimum_outcome)
        if minimum_outcome is not None:
            context = dict(context or {})
            context["minimum_outcome"] = minimum_outcome
            context["approval_call_id"] = approval_call_id

        # Get session for intention and policy (verifies ownership)
        session = self._sessions.get(session_id, user_id=user_id)
        if session.get("status") not in {"completed", "suspended", "terminated"}:
            from intaris.policy_activation import HybridError

            try:
                session = self._sessions.refresh_inherited_policy(
                    session_id, user_id=user_id
                )
            except HybridError as exc:
                if exc.code != "parent_session_inactive":
                    raise
                return self._deny_inactive_parent(
                    session,
                    user_id,
                    agent_id,
                    tool,
                    args,
                    context,
                    call_id,
                    minimum_outcome,
                    start_time,
                )

        policy_minimum = (session.get("policy") or {}).get("minimum_outcome")
        if policy_minimum is not None:
            minimum_outcome = clamp_outcome(
                minimum_outcome or "approve", policy_minimum
            )
            context = {**(context or {}), "minimum_outcome": minimum_outcome}
        from intaris.evaluation_override import force_legacy, session_view

        forced_legacy = force_legacy()
        session = session_view(session, forced_legacy)
        from intaris import read_only
        from intaris.policy_activation import HybridError

        active = session.get("policy") or {}
        operator_bound = (
            active.get("evaluation_policy_version") == "hybrid_v1"
            and active.get("maximum_outcome") == "approve"
        )
        if (
            not operator_bound
            and session.get("guardrails_enabled", True)
            and active.get("evaluation_policy_version") == "hybrid_v1"
            and session.get("status") == "active"
            and session.get("alignment_state") in {"pending", "error"}
            and session.get("parent_session_id")
            and (
                (active.get("active_policy") or {}).get("profile")
                or active.get("profile", "balanced")
            )
            != "bounded_autonomy"
            and (
                self._alignment_barrier is None
                or not self._alignment_barrier.has_pending(user_id, session_id)
            )
        ):
            parent = self._sessions.get(session["parent_session_id"], user_id=user_id)
            try:
                if self._alignment_barrier is None:
                    raise RuntimeError("Alignment reviewer is not configured")
                aligned, alignment_reason = self._alignment_barrier.check(
                    parent=parent,
                    child=session,
                    fail_closed=True,
                )
            except Exception as exc:
                # One provider-timeout-bounded attempt. Leave the durable gate
                # closed and return a human-resolvable escalation below.
                self._sessions.set_alignment_state(
                    session_id,
                    user_id=user_id,
                    state="error",
                    reason=f"Alignment review unavailable ({type(exc).__name__}); retry evaluation.",
                    expected_state_revision=session["state_revision"],
                )
                session = session_view(
                    self._sessions.get(session_id, user_id=user_id), forced_legacy
                )
            else:
                if not self._sessions.set_alignment_state(
                    session_id,
                    user_id=user_id,
                    state="ready" if aligned else "misaligned",
                    expected_state_revision=session["state_revision"],
                    reason=alignment_reason,
                ):
                    raise HybridError(
                        "stale_alignment", "Session changed during alignment", 409
                    )
                session = session_view(
                    self._sessions.get(session_id, user_id=user_id), forced_legacy
                )
                active = session.get("policy") or {}
                operator_bound = active.get("maximum_outcome") == "approve"
        overlay = read_only.resolve(context, active)
        if (
            active.get("evaluation_policy_version") == "hybrid_v1"
            or overlay["read_only_required"]
        ) and original_args is not None:
            args = original_args
        if (
            not forced_legacy
            and evaluation_policy_version is not None
            and evaluation_policy_version
            != active.get("evaluation_policy_version", "legacy")
        ):
            raise HybridError(
                "policy_version_mismatch",
                "Activate requested version before evaluation",
                409,
            )
        if (
            not forced_legacy
            and expected_policy_revision is not None
            and expected_policy_revision != session["policy_revision"]
        ):
            raise HybridError("stale_policy_revision", "Policy revision changed", 409)
        if (
            not forced_legacy
            and expected_policy_hash is not None
            and expected_policy_hash != active.get("policy_hash")
        ):
            raise HybridError("stale_policy_hash", "Policy hash changed", 409)
        maximum_outcome = (session.get("policy") or {}).get("maximum_outcome")
        unattended = (session.get("policy") or {}).get(
            "interaction_mode"
        ) == "none" or ((context or {}).get("interaction_mode") == "none")
        hard_bounds = {
            "maximum_outcome": maximum_outcome,
            "minimum_outcome": minimum_outcome,
        }

        # Check session status — deny evaluation for inactive sessions
        session_status = session.get("status", "active")

        # Auto-resume idle sessions silently (behavioral guardrails)
        if session_status == "idle":
            try:
                self._sessions.update_status(session_id, "active", user_id=user_id)
                session = session_view(
                    self._sessions.get(session_id, user_id=user_id), forced_legacy
                )
                session_status = "active"
                logger.debug("Auto-resumed idle session %s", session_id)
            except ValueError:
                pass

        if session_status in ("completed", "suspended", "terminated"):
            # Clean up caches for inactive sessions
            self.clear_approved_paths(user_id, session_id)
            if self._alignment_barrier is not None:
                self._alignment_barrier.clear_session(user_id, session_id)

            status_reason = session.get("status_reason")
            reasoning = f"Session is {session_status} — evaluation denied"
            if status_reason:
                reasoning = f"{reasoning}. Reason: {status_reason}"

            latency_ms = int((time.monotonic() - start_time) * 1000)
            inactive_args_redacted = _args_redacted_with_context(
                redact(args), redact(context) if context else None
            )
            self._audit.insert(
                call_id=call_id,
                user_id=user_id,
                session_id=session_id,
                agent_id=agent_id,
                tool=tool,
                args_redacted=inactive_args_redacted,
                classification="write",
                evaluation_path="fast",
                decision="deny",
                risk="low",
                reasoning=reasoning,
                latency_ms=latency_ms,
                **hard_bounds,
                outcome_override="hard_stop.session_lifecycle",
            )
            try:
                self._sessions.increment_counter(session_id, "deny", user_id=user_id)
            except ValueError:
                pass
            return {
                "call_id": call_id,
                "decision": "deny",
                **(
                    {
                        "raw_decision": "deny",
                        "effective_decision": "deny",
                        "maximum_outcome": maximum_outcome,
                        "outcome_override": "hard_stop.session_lifecycle",
                    }
                    if maximum_outcome is not None
                    else {}
                ),
                "minimum_outcome": minimum_outcome,
                "reasoning": reasoning,
                "risk": "low",
                "path": "fast",
                "latency_ms": latency_ms,
                "args_redacted": inactive_args_redacted,
                "classification": "write",
                "session_status": session_status,
                "status_reason": status_reason,
            }

        # Update session activity timestamp (for idle detection)
        try:
            self._sessions.update_activity(session_id, user_id=user_id)
        except Exception:
            logger.debug("Failed to update session activity", exc_info=True)

        # Check alignment misalignment (escalation-style, not suspension).
        # If the alignment barrier detected a misalignment and the user
        # has not yet acknowledged it, return escalate so the client can
        # poll for user approval — same flow as tool escalation.
        durable_misaligned = active.get(
            "evaluation_policy_version"
        ) == "hybrid_v1" and session.get("alignment_state") in {
            "misaligned",
            "pending",
            "error",
        }
        alignment_unready = session.get("alignment_state") in {"pending", "error"}
        # Hybrid sessions carry a classified misalignment into policy as a fact
        # (reviewed by Judge through the normal pipeline). Legacy sessions keep
        # the human hard stop below.
        hybrid_misalignment: dict[str, Any] | None = None
        if (
            not forced_legacy
            and session.get("guardrails_enabled", True)
            and not operator_bound
            and (self._alignment_barrier is not None or durable_misaligned)
        ):
            misalignment_reason = (
                self._alignment_barrier.is_misaligned(user_id, session_id)
                if self._alignment_barrier is not None
                and active.get("evaluation_policy_version") != "hybrid_v1"
                else None
            )
            if durable_misaligned:
                active_profile = (active.get("active_policy") or {}).get(
                    "profile"
                ) or active.get("profile", "balanced")
                if alignment_unready and active_profile != "bounded_autonomy":
                    raise HybridError(
                        "alignment_unavailable",
                        session.get("alignment_reason")
                        or "Parent/child classification is unavailable; retry evaluation.",
                        503,
                        retryable=True,
                    )
                hybrid_misalignment = {
                    "state": "unavailable" if alignment_unready else "incompatible",
                    "reason": session.get("alignment_reason")
                    or "Parent/child compatibility classification is unavailable.",
                    "grounded": False,
                    "overridden": False,
                }
                misalignment_reason = None
            if misalignment_reason:
                alignment_decision = (
                    "deny" if unattended else clamp_outcome("escalate", minimum_outcome)
                )
                latency_ms = int((time.monotonic() - start_time) * 1000)
                args_redacted = _args_redacted_with_context(
                    redact(args), redact(context) if context else None
                )
                args_hash = _compute_args_hash(args)
                alignment_mutation = read_only.observe(tool, args)
                if overlay["read_only_required"]:
                    from intaris.hybrid import digest
                    from intaris.hybrid_runtime import (
                        build_evidence,
                    )
                    from intaris.hybrid_runtime import (
                        classify as classify_mutation,
                    )

                    args_hash = digest(
                        {"args_hash": args_hash, "mode": read_only.identity(overlay)}
                    )
                    if alignment_mutation == "unclear" and not alignment_unready:
                        evidence = build_evidence(
                            self._audit, session, tool, args, context, agent_id, None
                        )
                        evidence["prompt"] += (
                            "\nEffective read-only overlay:\n" + json.dumps(overlay)
                        )
                        try:
                            assessment, _ = classify_mutation(
                                self._llm,
                                (yield BackendSelection(self._backend)),
                                evidence,
                            )
                        except Exception as exc:
                            raise HybridError(
                                "assessment_failed",
                                "Read-only alignment assessment failed",
                                503,
                            ) from exc
                        alignment_mutation = assessment["mutation"]
                    alignment_decision, _ = read_only.enforce(
                        alignment_decision,
                        overlay,
                        alignment_mutation,
                    )
                misalignment_reason = (
                    read_only.explanation(
                        alignment_decision, overlay, alignment_mutation
                    )
                    or misalignment_reason
                )
                alignment_metadata = (
                    {
                        "evaluation_policy_version": active.get(
                            "evaluation_policy_version", "legacy"
                        ),
                        "policy_revision": session["policy_revision"],
                        "state_revision": session["state_revision"],
                        "policy_hash": active.get("policy_hash"),
                        "human_approval_required": True,
                        "mandatory_rule_ids": ["session.alignment"],
                        "admission": None,
                        "execution_state": "not_dispatched",
                        "overlay": overlay,
                        "mode_identity": read_only.identity(overlay),
                        "mutation": alignment_mutation,
                    }
                    if durable_misaligned or overlay["read_only_required"]
                    else None
                )
                self._audit.insert(
                    call_id=call_id,
                    user_id=user_id,
                    session_id=session_id,
                    agent_id=agent_id,
                    tool=tool,
                    args_redacted=args_redacted,
                    classification="write",
                    evaluation_path="alignment",
                    decision=alignment_decision,
                    risk="high",
                    reasoning=misalignment_reason,
                    latency_ms=latency_ms,
                    args_hash=args_hash,
                    intention=session.get("intention"),
                    **(
                        {
                            "admission_snapshot": session,
                            "evaluation_metadata": alignment_metadata,
                        }
                        if alignment_metadata is not None
                        else {}
                    ),
                    **hard_bounds,
                    raw_decision="escalate",
                    outcome_override=(
                        "task.interaction_mode" if unattended else "hard_stop.alignment"
                    ),
                )
                try:
                    self._sessions.increment_counter(
                        session_id,
                        alignment_decision,
                        user_id=user_id,
                    )
                except ValueError:
                    pass
                return {
                    "call_id": call_id,
                    "decision": alignment_decision,
                    **(
                        {
                            "raw_decision": "escalate",
                            "effective_decision": alignment_decision,
                            "maximum_outcome": maximum_outcome,
                            "outcome_override": (
                                "task.interaction_mode"
                                if unattended
                                else "hard_stop.alignment"
                            ),
                        }
                        if maximum_outcome is not None
                        else {}
                    ),
                    "minimum_outcome": minimum_outcome,
                    "reasoning": misalignment_reason,
                    "risk": "high",
                    "path": "alignment",
                    **(
                        {"evaluation_metadata": alignment_metadata}
                        if alignment_metadata is not None
                        else {}
                    ),
                    "latency_ms": latency_ms,
                    "args_redacted": args_redacted,
                    "classification": "write",
                }

        # Lookup behavioral profile for context injection
        profile_version: int | None = None
        profile = None
        if self._analysis_enabled and self._db is not None:
            profile = self.get_behavioral_context(user_id, agent_id)
            if profile:
                profile_version = profile.get("profile_version")
                # Analytics is advisory evidence for existing Judge reviews,
                # never historical prose or permission changes in L1.

        # Redact secrets from args
        args_redacted = redact(args)

        # Scan for prompt injection patterns in tool args (log-only).
        # This runs AFTER redaction so we scan what the LLM will see.
        from intaris.sanitize import (
            detect_injection_patterns,
            log_injection_warning,
        )

        _injection_detected = False
        injection_scan_payload: dict[str, Any] = dict(args_redacted)
        if context:
            injection_scan_payload[_CONTEXT_ARG_KEY] = redact(context)
        args_text = json.dumps(injection_scan_payload, default=str)
        findings = detect_injection_patterns(args_text)
        if findings:
            log_injection_warning("tool_args", args_text, findings)
            _injection_detected = True

        # Also scan the session intention
        intention_text = session.get("intention", "")
        intention_findings = detect_injection_patterns(intention_text)
        if intention_findings:
            log_injection_warning("intention", intention_text, intention_findings)
            _injection_detected = True

        # Compute args_hash for escalation retry lookups
        args_hash = _compute_args_hash(args)
        if overlay["read_only_required"] or (context or {}).get("chat_mode") in {
            "plan",
            "build",
        }:
            from intaris.hybrid import digest

            args_hash = digest(
                {"args_hash": args_hash, "mode": read_only.identity(overlay)}
            )

        # Get session policy for classifier
        session_policy = session.get("policy")

        # Resolve the call-scoped working directory for path-aware classification.
        details = session.get("details") or {}
        working_directory = _effective_working_directory(tool, args, context, details)

        # Resolve parent intention for sub-sessions (intention chain).
        # Sub-agent tool calls must be aligned with BOTH the parent
        # session's intention and their own. This prevents sub-agents
        # from escaping the parent's guardrails by declaring a narrow
        # intention that doesn't cover what they're actually doing.
        parent_intention: str | None = None
        parent_session_id = session.get("parent_session_id")
        if parent_session_id:
            try:
                parent_session = self._sessions.get(parent_session_id, user_id=user_id)
                from intaris.evaluation_context import parent_scope_evidence

                parent_intention = parent_scope_evidence(parent_session)
                if hybrid_misalignment is not None:
                    from intaris.alignment import (
                        alignment_grounding,
                        parent_instruction_bodies,
                    )

                    hybrid_misalignment["grounded"] = alignment_grounding(
                        hybrid_misalignment["reason"],
                        parent_instruction_bodies(parent_session),
                    )

                # Parent lifecycle cascade: if parent is terminated or
                # suspended, auto-suspend the child session. This prevents
                # orphaned children from operating under a dead parent.
                parent_status = parent_session.get("status", "active")
                if parent_status in ("terminated", "suspended"):
                    return self._deny_inactive_parent(
                        session,
                        user_id,
                        agent_id,
                        tool,
                        args,
                        context,
                        call_id,
                        minimum_outcome,
                        start_time,
                    )
            except HybridError:
                raise
            except ValueError:
                logger.debug(
                    "Parent session %s not found for sub-session %s",
                    parent_session_id,
                    session_id,
                )

        edit_targets = self._workspace_edit_targets(
            session=session,
            tool=tool,
            args=args,
            context=context,
            working_directory=working_directory,
            minimum_outcome=minimum_outcome,
        )
        edit_scopes = (
            self._approved_workspace_scopes(
                session=session, tool=tool, args_hash=args_hash, targets=edit_targets
            )
            if edit_targets
            else None
        )

        if (session_policy or {}).get("evaluation_policy_version") == "hybrid_v1":
            from intaris.hybrid_runtime import HybridRuntime

            hybrid_result = HybridRuntime(
                llm=self._llm,
                jev=(yield BackendSelection(self._backend)),
                audit=self._audit,
                sessions=self._sessions,
                task_context=self._task_context,
                l1_input_projection=self._l1_input_projection,
                analytics_provenance=(
                    {
                        k: profile[k]
                        for k in (
                            "profile_version",
                            "analysis_id",
                            "actor",
                            "expires_at",
                        )
                    }
                    if profile
                    else None
                ),
            ).evaluate(
                session=session,
                agent_id=agent_id,
                tool=tool,
                args=args,
                args_hash=args_hash,
                context=context,
                working_directory=working_directory,
                parent_intention=parent_intention,
                tool_preferences=tool_preferences,
                minimum_outcome=minimum_outcome,
                approval_call_id=approval_call_id,
                logical_call_id=logical_call_id,
                controller_context=controller_context,
                process_lifecycle=process_lifecycle,
                approved_edit_scopes=edit_scopes,
                parent_alignment=hybrid_misalignment,
            )
            hybrid_meta = hybrid_result.get("evaluation_metadata") or {}
            if (
                edit_targets
                and hybrid_result["decision"] == "approve"
                and hybrid_meta.get("stages")
            ):
                # The L1 model approved this edit: open its write scope.
                self._record_workspace_edit(
                    user_id,
                    session_id,
                    (hybrid_meta["policy_revision"], hybrid_meta["state_revision"]),
                    edit_targets,
                    working_directory,
                )
            return hybrid_result

        # Step 1-2: Classify (with path awareness via working_directory)
        classification = classify(
            tool,
            args,
            session_policy=session_policy,
            tool_preferences=tool_preferences,
            working_directory=working_directory,
        )

        # Path approval cache override: if the classifier reclassified a
        # read-only tool to WRITE due to path policy, check whether the
        # paths have been previously approved. If so, override back to READ.
        # CRITICAL from deny_paths is never overridden (it's not WRITE).
        path_reclassified = False
        resolved_paths: list[str] = []
        if (
            classification == Classification.WRITE
            and working_directory
            and is_read_only(tool, args)
            # Credential reads never become cached fast-path reads.
            and not reads_secret_location(tool, args, working_directory)
        ):
            resolved_paths = resolve_tool_paths(tool, args, working_directory)
            if resolved_paths:
                outside_paths = [
                    rp
                    for rp in resolved_paths
                    if not is_path_within(rp, working_directory)
                ]
                if outside_paths:
                    path_reclassified = True
                    # Check approved paths cache
                    if self._check_approved_paths(
                        user_id, session_id, outside_paths, working_directory
                    ):
                        classification = Classification.READ
                        path_reclassified = False
                        logger.debug(
                            "Path approved via prior evaluation: %s",
                            outside_paths,
                        )

        # Inject deterministic path-policy facts into WRITE evaluations when
        # either the project directory or explicit target paths are known.
        if classification == Classification.WRITE:
            policy_paths = _resolve_tool_paths_for_context(
                tool, args_redacted, working_directory or ""
            )
            if not working_directory and not all(
                os.path.isabs(path) for path in policy_paths
            ):
                policy_paths = []
            if working_directory or policy_paths:
                context = dict(context) if context else {}
                if working_directory:
                    # Git-style write commands often have no explicit file
                    # arguments, so include the working directory itself.
                    context["project_path"] = working_directory
                context["path_policy"] = _build_path_policy_context(
                    resolved_paths=policy_paths,
                    working_directory=working_directory or "",
                    policy=session_policy,
                )

        # Step 3-5: Fast paths for read-only, critical, and escalate
        if classification == Classification.READ:
            reasoning = f"Read-only tool call: {tool}"
            if resolved_paths and not path_reclassified:
                # Check if this was a cache-hit override
                if any(
                    not is_path_within(rp, working_directory or "")
                    for rp in resolved_paths
                ):
                    reasoning = (
                        f"Read-only tool call: {tool} "
                        f"(path approved via prior evaluation)"
                    )
            decision = make_fast_decision("read", reasoning)
        elif classification == Classification.CRITICAL:
            # Check for prior approved override before auto-deny.
            # If the user explicitly approved the exact same command
            # (via denial override), bypass the critical auto-deny.
            retry_decision = (
                self._check_escalation_retry(
                    user_id=user_id,
                    tool=tool,
                    args_hash=args_hash,
                )
                if minimum_outcome is None and not unattended
                else None
            )
            if retry_decision is not None:
                decision = retry_decision
            else:
                decision = make_fast_decision(
                    "critical",
                    f"Critical pattern detected in {tool} call",
                )
        elif classification == Classification.ESCALATE:
            # Check escalation retry: reuse prior approval if same
            # tool+args was approved within the TTL window.
            retry_decision = (
                self._check_escalation_retry(
                    user_id=user_id,
                    tool=tool,
                    args_hash=args_hash,
                )
                if minimum_outcome is None and not unattended
                else None
            )
            if retry_decision is not None:
                decision = retry_decision
            else:
                decision = make_fast_decision(
                    "escalate",
                    f"Tool preference requires escalation for {tool}",
                )
        else:
            # Check for prior approved denial override before LLM.
            # If the user previously approved a denial of the same
            # command, skip the LLM call and approve directly.
            retry_decision = (
                self._check_escalation_retry(
                    user_id=user_id,
                    tool=tool,
                    args_hash=args_hash,
                )
                if minimum_outcome is None and not unattended
                else None
            )
            if retry_decision is not None:
                decision = retry_decision
            elif edit_scopes:
                decision = Decision(
                    decision="approve",
                    risk="low",
                    reasoning=(
                        "approved_workspace_edit: follow-up edit inside the "
                        f"approved write scope {', '.join(edit_scopes)}"
                    ),
                    path="fast",
                    metadata={
                        "permission_basis": "approved_workspace_edit",
                        "approved_workspace_scopes": edit_scopes,
                    },
                )
            else:
                # Step 6: LLM safety evaluation
                from intaris.evaluation_context import captured_sources

                context = {
                    **context,
                    "raw_task_sources": captured_sources(
                        self._llm,
                        self._sessions,
                        self._audit,
                        session,
                    ),
                }
                decision = yield from self._llm_evaluation_flow(
                    session=session,
                    tool=tool,
                    args_redacted=args_redacted,
                    agent_id=agent_id,
                    context=redact(context) if context else None,
                    parent_intention=parent_intention,
                )

        raw_decision = decision.decision
        decision.decision = cap_outcome(raw_decision, maximum_outcome)
        policy_overrode = decision.decision != raw_decision
        outcome_override = "session_policy.maximum_outcome" if policy_overrode else None

        if minimum_outcome is not None:
            approved = False
            if approval_call_id:
                try:
                    record = self._audit.get_by_call_id(
                        approval_call_id, user_id=user_id
                    )
                except ValueError:
                    record = None
                approved = bool(
                    record
                    and record.get("session_id") == session_id
                    and record.get("tool") == tool
                    and record.get("args_hash") == args_hash
                    and record.get("decision") == "escalate"
                    and record.get("user_decision") == "approve"
                    and record.get("resolved_by") == "user"
                )
                if not approved:
                    decision.decision = "deny"
                    decision.reasoning = "Approval does not match this evaluation."
                    outcome_override = "invalid_approval"
            if approved and minimum_outcome == "escalate" and raw_decision != "deny":
                decision.decision = "approve"
                decision.reasoning = f"User approved call {approval_call_id}."
                outcome_override = "human_approval"
            else:
                if approved and raw_decision == "deny":
                    decision.decision = "deny"
                    outcome_override = "request.minimum_outcome"
                bounded = clamp_outcome(decision.decision, minimum_outcome)
                if bounded != decision.decision:
                    outcome_override = "request.minimum_outcome"
                decision.decision = bounded

        if (
            unattended
            and judge_unattended
            and minimum_outcome is None
            and raw_decision in {"deny", "escalate"}
            and maximum_outcome != "deny"
        ):
            # This API-only outcome is non-executable until Judge resolves it.
            # Snapshot the task constraint for Judge even if policy changes.
            decision.decision = "escalate"
            outcome_override = UNATTENDED_JUDGE_REVIEW
        elif unattended and (
            raw_decision in {"deny", "escalate"} or decision.decision == "escalate"
        ):
            # No human approval channel exists. The task restriction wins over
            # a permissive agent maximum (including explicit yolo mode).
            if decision.decision != "deny":
                decision.decision = "deny"
                outcome_override = "task.interaction_mode"
            decision.reasoning = (
                f"{decision.reasoning} Escalation is disabled for this unattended task."
            )

        overlay_metadata = None
        if overlay["read_only_required"]:
            from intaris.hybrid_runtime import (
                build_evidence,
            )
            from intaris.hybrid_runtime import (
                classify as classify_mutation,
            )

            mutation = read_only.observe(tool, args)
            stages = []
            if mutation == "unclear":
                evidence = build_evidence(
                    self._audit,
                    session,
                    tool,
                    args,
                    context,
                    agent_id,
                    parent_intention,
                )
                evidence["prompt"] += "\nEffective read-only overlay:\n" + json.dumps(
                    overlay
                )
                try:
                    assessment, diagnostics = classify_mutation(
                        self._llm, (yield BackendSelection(self._backend)), evidence
                    )
                except Exception:
                    # Missing proof cannot become an actionable approval card.
                    # Preserve the closed ceiling when the assessor is unavailable.
                    stages.append({"stage": "read_only_l1", "status": "unavailable"})
                else:
                    mutation = assessment["mutation"]
                    stages.append(
                        {
                            "stage": "read_only_l1",
                            "classification": assessment,
                            **diagnostics,
                        }
                    )
            decision.decision, ceiling = read_only.enforce(
                decision.decision, overlay, mutation
            )
            outcome_override = ceiling or outcome_override
            decision.reasoning = (
                read_only.explanation(decision.decision, overlay, mutation)
                or decision.reasoning
            )
            overlay_metadata = {
                "evaluation_policy_version": "legacy",
                "overlay": overlay,
                "mode_identity": read_only.identity(overlay),
                "mutation": mutation,
                "policy_revision": session["policy_revision"],
                "state_revision": session["state_revision"],
                "stages": stages,
                "execution_state": "not_dispatched",
            }
            decision.metadata = overlay_metadata

        # Learn from LLM approvals: cache path prefixes for path-reclassified
        # calls so subsequent reads to the same directory are fast-pathed.
        # User-approved escalations are handled separately via
        # learn_from_approved_escalation() called from POST /decision.
        if (
            path_reclassified
            and decision.decision == "approve"
            and not policy_overrode
            and working_directory
        ):
            for rp in resolved_paths:
                if not is_path_within(rp, working_directory):
                    prefix = _compute_path_prefix(rp, working_directory)
                    self._approve_path_prefix(user_id, session_id, prefix)
                    logger.info(
                        "Learned approved path prefix: %s (session %s)",
                        prefix,
                        session_id,
                    )

        # The evaluator model approved a covered edit: open its write scope.
        if (
            edit_targets
            and decision.path == "llm"
            and raw_decision == "approve"
            and decision.decision == "approve"
        ):
            self._record_workspace_edit(
                user_id,
                session_id,
                (session["policy_revision"], session["state_revision"]),
                edit_targets,
                working_directory,
            )
        workspace_metadata = (
            decision.metadata
            if edit_scopes and decision.path == "fast" and overlay_metadata is None
            else None
        )

        # Calculate latency
        latency_ms = int((time.monotonic() - start_time) * 1000)

        # Step 7: Audit (with args_hash for escalation retry, profile_version)
        audit_context = redact(context) if context else None
        self._audit.insert(
            call_id=call_id,
            user_id=user_id,
            session_id=session_id,
            agent_id=agent_id,
            tool=tool,
            args_redacted=_args_redacted_with_context(args_redacted, audit_context),
            classification=classification.value,
            evaluation_path=decision.path,
            decision=decision.decision,
            risk=decision.risk,
            reasoning=decision.reasoning,
            latency_ms=latency_ms,
            args_hash=args_hash,
            profile_version=profile_version,
            intention=session.get("intention"),
            injection_detected=_injection_detected,
            raw_decision=raw_decision,
            maximum_outcome=maximum_outcome,
            minimum_outcome=minimum_outcome,
            outcome_override=outcome_override,
            evaluation_metadata={
                **(decision.metadata or {}),
                **(overlay_metadata or {}),
                **(workspace_metadata or {}),
            }
            or None,
            # A cached workspace approval is admitted only if the authorization
            # revision it matched is still current (atomic with the insert).
            admission_snapshot=session
            if overlay_metadata or workspace_metadata
            else None,
        )

        # Step 8: Update session counters
        try:
            self._sessions.increment_counter(
                session_id, decision.decision, user_id=user_id
            )
        except ValueError:
            logger.warning("Failed to update session counter for %s", session_id)

        # One-time intention bootstrap for sessions without user messages.
        # Sessions that never receive user messages (Claude Code, MCP proxy)
        # keep their generic initial intention. At call 10, if no user
        # messages have been received (durably recorded on the session),
        # trigger a single refinement from tool patterns. Capped at exactly
        # one update to prevent agent drift from rewriting the intention.
        total = session.get("total_calls", 0)
        intention_source = session.get("intention_source", "initial")
        if (
            total == 9
            and intention_source == "initial"
            and self._analysis_enabled
            and self._db is not None
        ):
            try:
                from intaris.background import TaskQueue

                tq = TaskQueue(self._db)
                tq.enqueue_bootstrap_if_no_user_message(user_id, session_id)
            except Exception:
                logger.debug(
                    "Failed to enqueue bootstrap intention update",
                    exc_info=True,
                )

        result = {
            "call_id": call_id,
            "decision": decision.decision,
            **(
                {
                    "raw_decision": raw_decision,
                    "effective_decision": decision.decision,
                    "maximum_outcome": maximum_outcome,
                    "outcome_override": outcome_override,
                }
                if maximum_outcome is not None
                else {}
            ),
            "minimum_outcome": minimum_outcome,
            "reasoning": decision.reasoning,
            "risk": decision.risk,
            "path": decision.path,
            "latency_ms": latency_ms,
            "args_redacted": args_redacted,
            "classification": classification.value,
            "injection_detected": _injection_detected,
        }
        if decision.metadata is not None:
            result["evaluation_metadata"] = decision.metadata
        return result

    def get_behavioral_context(
        self, user_id: str, agent_id: str | None = None
    ) -> dict[str, Any] | None:
        """Fast DB lookup of pre-computed behavioral profile.

        Looks up the agent-scoped profile first. Falls back to the
        user-level profile (agent_id='') if no agent-specific profile
        exists.

        Returns the profile dict if found, None otherwise.
        This is a ~1ms read that does not impact the evaluate hot path.

        Args:
            user_id: Tenant identifier.
            agent_id: Agent identifier (optional).

        Returns:
            Profile dict with risk_level, context_summary, profile_version,
            or None if no profile exists.
        """
        if self._db is None or not self._analysis_enabled:
            return None

        try:
            from datetime import datetime, timedelta, timezone

            from intaris.analytics_evidence import VERSION, metadata

            with self._db.cursor() as cur:
                cur.execute(
                    "SELECT bp.*, ba.evidence_metadata FROM behavioral_profiles bp "
                    "JOIN behavioral_analyses ba ON ba.id = bp.last_analysis_id "
                    "AND ba.user_id = bp.user_id "
                    "WHERE bp.user_id = ? AND bp.agent_id = ?",
                    (user_id, agent_id or ""),
                )
                row = cur.fetchone()
            if not row:
                return None
            row = dict(row)
            evidence = metadata(row["evidence_metadata"])
            if evidence.get("version") != VERSION:
                return None
            updated = datetime.fromisoformat(
                str(row["updated_at"]).replace("Z", "+00:00")
            )
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            expires = updated + timedelta(days=self._analysis_config.lookback_days)
            if datetime.now(timezone.utc) >= expires:
                return None
            alerts = row.get("active_alerts") or []
            if isinstance(alerts, str):
                alerts = json.loads(alerts)
            fresh_alerts = []
            for alert in alerts:
                observed = alert.get("observed_at")
                if not observed:
                    continue
                observed = datetime.fromisoformat(str(observed).replace("Z", "+00:00"))
                if observed.tzinfo is None:
                    observed = observed.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) < observed + timedelta(
                    days=self._analysis_config.lookback_days
                ):
                    fresh_alerts.append(alert)
            alerts = sorted(
                fresh_alerts, key=lambda a: a.get("severity", 1), reverse=True
            )
            bounded = [
                {
                    "severity": a.get("severity"),
                    "detail": str(a.get("detail", ""))[:500],
                    "session_ids": a.get("session_ids", [])[:8],
                    "analysis_id": a.get("analysis_id"),
                    "observed_at": a.get("observed_at"),
                }
                for a in alerts[:5]
            ]
            return {
                "risk_level": max([1] + [a.get("severity", 1) for a in alerts]),
                "profile_version": row["profile_version"],
                "analysis_id": row["last_analysis_id"],
                "actor": agent_id,
                "updated_at": updated.isoformat(),
                "expires_at": expires.isoformat(),
                "active_alerts": bounded,
                "omitted_alerts": max(0, len(alerts) - 5),
                "coverage_status": evidence.get("status", "unknown"),
                "context_summary": "",  # Never inject unrestricted generated prose.
            }
        except Exception:
            logger.debug("Failed to lookup behavioral profile", exc_info=True)
            return None

    def _check_escalation_retry(
        self,
        *,
        user_id: str,
        tool: str,
        args_hash: str,
    ) -> Decision | None:
        """Check if a prior escalation or denial for the same tool+args was approved.

        Looks for an audit record within the retry TTL window where:
        - Same user, tool, and args_hash (session-independent so approvals
          survive MCP proxy reconnects)
        - The record was resolved with user_decision='approve'

        This covers both escalation retry (existing) and denial override
        retry (ex-post approval of L1 denials). The underlying SQL query
        is decision-agnostic — it matches any approved override.

        Returns:
            Decision to approve (reusing prior approval), or None if no
            valid prior approval found.
        """
        from datetime import datetime, timedelta, timezone

        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(minutes=_ESCALATION_RETRY_TTL_MINUTES)
        ).isoformat()

        row = self._audit.find_approved_escalation(
            user_id=user_id,
            tool=tool,
            args_hash=args_hash,
            cutoff=cutoff,
        )

        if row is not None:
            prior_call_id = row["call_id"]
            logger.info(
                "Escalation retry: reusing approval from %s for %s",
                prior_call_id,
                tool,
            )
            return Decision(
                decision="approve",
                risk="low",
                reasoning=(
                    f"Reusing prior approval (call {prior_call_id}) — "
                    f"same tool and arguments approved within "
                    f"{_ESCALATION_RETRY_TTL_MINUTES} minutes"
                ),
                path="fast",
            )

        return None

    # ── Approved Path Prefix Cache ────────────────────────────────────

    def _check_approved_paths(
        self,
        user_id: str,
        session_id: str,
        resolved_paths: list[str],
        working_directory: str,
    ) -> bool:
        """Check if all resolved paths are under an approved prefix.

        Args:
            user_id: Tenant identifier.
            session_id: Session identifier.
            resolved_paths: Normalized absolute paths to check.
            working_directory: Session's working directory.

        Returns:
            True if every path is either within working_directory or
            under an approved prefix for this session.
        """
        key = (user_id, session_id)
        with self._approved_paths_lock:
            # Copy under lock to avoid TOCTOU with concurrent modifications
            prefixes = list(self._approved_paths.get(key, []))

        for rp in resolved_paths:
            if is_path_within(rp, working_directory):
                continue
            # Check against approved prefixes
            if not any(is_path_within(rp, prefix) for prefix in prefixes):
                return False
        return True

    def _approve_path_prefix(
        self,
        user_id: str,
        session_id: str,
        prefix: str,
    ) -> None:
        """Add an approved path prefix for a session.

        Thread-safe. Enforces a maximum number of prefixes per session
        with FIFO eviction (oldest prefix removed first).

        When a new prefix shares a deep common ancestor with an existing
        prefix (>= ``_MIN_MERGE_DEPTH`` components), the two are merged
        into the common ancestor. This naturally broadens the cache as
        the agent explores related paths (e.g., different npm packages
        under the same scope directory).

        Args:
            user_id: Tenant identifier.
            session_id: Session identifier.
            prefix: Normalized directory prefix to approve.
        """
        key = (user_id, session_id)
        norm_prefix = os.path.normpath(prefix)
        with self._approved_paths_lock:
            prefixes = self._approved_paths.setdefault(key, [])

            # Check if already covered by an existing prefix
            if any(is_path_within(norm_prefix, p) for p in prefixes):
                return

            # Try to merge with an existing prefix
            merged = _try_merge_prefix(norm_prefix, prefixes)
            if merged:
                return

            prefixes.append(norm_prefix)
            # FIFO eviction if over limit
            while len(prefixes) > _MAX_APPROVED_PATHS_PER_SESSION:
                prefixes.pop(0)

    def learn_from_approved_escalation(self, record: dict[str, Any]) -> None:
        """Cache path prefix when a user approves an escalated read.

        Called from ``POST /decision`` when a user approves an escalation.
        Extracts file paths from the audit record, computes the directory
        prefix, and caches it so subsequent reads under the same prefix
        are fast-pathed as READ.

        Also works for non-path escalations — the method is a no-op when
        the tool call doesn't involve out-of-project file paths.

        Args:
            record: Audit record dict from the resolved escalation.
        """
        if (
            record.get("resolved_by") != "user"
            and record.get("raw_decision") == "deny"
            and record.get("maximum_outcome") in {"escalate", "approve"}
        ):
            return
        try:
            self._learn_workspace_edit(record)
        except Exception:
            logger.debug("Could not learn write scope from approval", exc_info=True)
        tool = record.get("tool", "")
        args_redacted = record.get("args_redacted")
        user_id = record.get("user_id", "")
        session_id = record.get("session_id", "")

        if not args_redacted or not isinstance(args_redacted, dict):
            return

        # Only for read-only tools
        if not is_read_only(tool, args_redacted):
            return

        # Get session's working_directory
        session = self._sessions.get(session_id, user_id=user_id)
        if not session:
            return
        details = session.get("details") or {}
        working_directory = details.get("working_directory")
        if not working_directory:
            return

        # Extract and resolve paths
        resolved_paths = resolve_tool_paths(tool, args_redacted, working_directory)
        if not resolved_paths:
            return

        for resolved in resolved_paths:
            if not is_path_within(resolved, working_directory):
                prefix = _compute_path_prefix(resolved, working_directory)
                self._approve_path_prefix(user_id, session_id, prefix)
                logger.info(
                    "Learned path prefix from user approval: %s (session %s)",
                    prefix,
                    session_id,
                )

    def clear_approved_paths(self, user_id: str, session_id: str) -> None:
        """Clear approved path prefixes for a session.

        Called when a session transitions to completed/terminated/suspended,
        or when the session intention changes.

        Args:
            user_id: Tenant identifier.
            session_id: Session identifier.
        """
        key = (user_id, session_id)
        with self._approved_paths_lock:
            self._approved_paths.pop(key, None)
            self._approved_edit_scopes.pop(key, None)

    def sweep_approved_paths(self, active_keys: set[tuple[str, str]]) -> int:
        """Remove approved paths for sessions no longer active.

        Called periodically by the background worker to prevent memory
        leaks from abandoned sessions.

        Args:
            active_keys: Set of (user_id, session_id) tuples for
                sessions that are still active.

        Returns:
            Number of entries removed.
        """
        with self._approved_paths_lock:
            stale = [k for k in self._approved_paths if k not in active_keys]
            for k in stale:
                del self._approved_paths[k]
            for k in [k for k in self._approved_edit_scopes if k not in active_keys]:
                del self._approved_edit_scopes[k]
        return len(stale)

    # ── Approved Workspace Edit Scopes ────────────────────────────────

    def _workspace_edit_targets(
        self,
        *,
        session: dict[str, Any],
        tool: str,
        args: dict[str, Any],
        context: dict[str, Any] | None,
        working_directory: str | None,
        minimum_outcome: str | None,
    ) -> list[str] | None:
        """Return targets of an edit that may open or use a write scope.

        Returns None whenever only the evaluator model may decide: strict
        profile, plan/read-only mode, a minimum_outcome floor, prose
        restrictions, deny_paths, undeterminable targets, and credential,
        ``.git`` or CI/CD/deploy targets. Deny rules, tool preferences and
        protected checkpoints are enforced by the classifier (legacy) or the
        hybrid policy table before a scope can apply.
        """
        from intaris import read_only

        if minimum_outcome is not None:
            return None
        policy = session.get("policy") or {}
        snapshot = policy.get("active_policy") or {}
        if (snapshot.get("profile") or policy.get("profile")) == "strict":
            return None
        overlay = read_only.resolve(context, policy)
        if overlay["read_only_required"] or overlay["chat_mode"] == "plan":
            return None
        if snapshot:
            # Hybrid: compiled clauses are rules; advisory prose needs the model.
            prose = [
                clause
                for clause in snapshot.get("advisory_clauses") or []
                if clause.get("effect") != "allow"
            ]
        else:
            prose = normalized_policy_clauses(policy).get("deny_policies", [])
        if prose:
            return None
        targets = _edit_targets(tool, args, working_directory)
        if not targets:
            return None
        if reads_secret_location(tool, args, working_directory) or any(
            _protected_edit_target(target) for target in targets
        ):
            return None
        deny_paths = [p for p in policy.get("deny_paths") or [] if isinstance(p, str)]
        if any(fnmatch.fnmatch(t, p) for t in targets for p in deny_paths):
            return None
        return targets

    def _approved_workspace_scopes(
        self,
        *,
        session: dict[str, Any],
        tool: str,
        args_hash: str,
        targets: list[str],
    ) -> list[str] | None:
        """Return the matched write scopes when every target lies inside one."""
        key = (session["user_id"], session["session_id"])
        revision = (session["policy_revision"], session["state_revision"])
        with self._approved_paths_lock:
            entry = self._approved_edit_scopes.get(key)
        if entry is None or entry[0] != revision:
            return None
        matched: list[str] = []
        for target in targets:
            scope = next((s for s in entry[1] if is_path_within(target, s)), None)
            if scope is None:
                return None
            if scope not in matched:
                matched.append(scope)
        # A human denial of this exact call or of any of its targets wins.
        details = session.get("details") or {}
        for denial in self._audit.get_human_denials(
            session["session_id"], user_id=session["user_id"]
        ):
            if denial.get("tool") == tool and denial.get("args_hash") == args_hash:
                return None
            denied_tool, denied_args, _, denied_wd = _recorded_call(denial, details)
            if set(_denied_targets(denied_tool, denied_args, denied_wd)) & set(targets):
                return None
        return matched

    def _record_workspace_edit(
        self,
        user_id: str,
        session_id: str,
        revision: tuple[int, int],
        targets: list[str],
        working_directory: str | None,
    ) -> None:
        """Record approved write scopes for the approval's revision.

        The scope is the working directory for targets inside it, otherwise
        the target's parent directory. Scopes from an older revision are
        replaced; an approval from an older revision is ignored.
        """
        root = os.path.normpath(working_directory) if working_directory else None
        if root == os.sep:
            root = None
        scopes = [
            root if root and is_path_within(target, root) else os.path.dirname(target)
            for target in targets
        ]
        key = (user_id, session_id)
        with self._approved_paths_lock:
            current = self._approved_edit_scopes.get(key)
            if current is not None and current[0] != revision:
                if any(new < old for new, old in zip(revision, current[0])):
                    return
                current = None
            recorded = list(current[1]) if current is not None else []
            for scope in scopes:
                if not any(is_path_within(scope, existing) for existing in recorded):
                    recorded.append(scope)
            self._approved_edit_scopes[key] = (
                revision,
                recorded[-_MAX_APPROVED_PATHS_PER_SESSION:],
            )
        logger.info("Recorded approved write scope %s (session %s)", scopes, session_id)

    def _learn_workspace_edit(self, record: dict[str, Any]) -> None:
        """Open a write scope when the Judge or a human approves an edit."""
        user_id = record.get("user_id", "")
        session_id = record.get("session_id", "")
        meta = record.get("evaluation_metadata") or {}
        session = self._sessions.get(session_id, user_id=user_id)
        tool, args, context, working_directory = _recorded_call(
            record, session.get("details") or {}
        )
        targets = self._workspace_edit_targets(
            session=session,
            tool=tool,
            args=args,
            context=context,
            working_directory=working_directory,
            minimum_outcome=record.get("minimum_outcome"),
        )
        if not targets:
            return
        # Hybrid receipts carry the post-decision revision; legacy records use
        # the revision captured at resolution. Unknown or no longer current
        # means no scope (the next edit only goes to the model).
        if "policy_revision" in meta and "state_revision" in meta:
            revision = (meta["policy_revision"], meta["state_revision"])
        elif record.get("resolution_revision"):
            revision = tuple(record["resolution_revision"])
        else:
            return
        if revision != (session["policy_revision"], session["state_revision"]):
            return
        self._record_workspace_edit(
            user_id, session_id, revision, targets, working_directory
        )

    def _llm_evaluate(
        self,
        *,
        session: dict[str, Any],
        tool: str,
        args_redacted: dict[str, Any],
        agent_id: str | None,
        context: dict[str, Any] | None = None,
        parent_intention: str | None = None,
    ) -> Decision:
        """Retain the synchronous helper contract for direct callers."""
        return run_sync(
            self._llm_evaluation_flow(
                session=session,
                tool=tool,
                args_redacted=args_redacted,
                agent_id=agent_id,
                context=context,
                parent_intention=parent_intention,
            )
        )

    def _llm_evaluation_flow(
        self,
        *,
        session: dict[str, Any],
        tool: str,
        args_redacted: dict[str, Any],
        agent_id: str | None,
        context: dict[str, Any] | None = None,
        parent_intention: str | None = None,
    ) -> Generator[BackendSelection, Any, Decision]:
        """Run LLM safety evaluation and apply decision matrix.

        Args:
            session: Session dict with intention, policy, stats.
            tool: Tool name.
            args_redacted: Redacted tool arguments.
            agent_id: Agent identity.
            context: Optional additional context for evaluation.
            parent_intention: Parent session intention for sub-sessions.

        Returns:
            Decision from the decision matrix.
        """
        # Assemble context
        session_id = session["session_id"]
        user_id = session["user_id"]
        recent_history = self._audit.get_recent(
            session_id, user_id=user_id, limit=10, record_type="tool_call"
        )
        recent_reasoning = self._audit.get_recent(
            session_id, user_id=user_id, limit=4, record_type="reasoning"
        )
        user_decisions = self._audit.get_user_decisions(
            session_id, user_id=user_id, limit=5
        )

        effective_policy = effective_policy_for_evaluator(session.get("policy"))
        session_stats = {
            "total_calls": session.get("total_calls", 0),
            "approved_count": session.get("approved_count", 0),
            "denied_count": session.get("denied_count", 0),
            "escalated_count": session.get("escalated_count", 0),
        }
        user_prompt = build_evaluation_user_prompt(
            intention=session["intention"],
            policy=effective_policy,
            recent_history=recent_history,
            recent_reasoning=recent_reasoning,
            user_decisions=user_decisions,
            session_stats=session_stats,
            tool=tool,
            args=args_redacted,
            agent_id=agent_id,
            context=context,
            parent_intention=parent_intention,
        )
        from intaris.evaluation_context import (
            compact_records,
            model_view,
            projection_arguments,
            request_view,
            source_witnesses,
        )

        user_prompt, _ = model_view(user_prompt)
        messages = [
            {
                "role": "system",
                "content": SAFETY_EVALUATION_SYSTEM_PROMPT.format(
                    anti_injection=ANTI_INJECTION_PREAMBLE,
                ),
            },
            {"role": "user", "content": user_prompt},
        ]

        try:
            jev = yield BackendSelection(self._backend)
            if jev is not None:
                payload, actual_view = request_view(
                    {
                        "state": {
                            "intention": session["intention"],
                            "effective_policy": effective_policy,
                            "parent_intention": parent_intention,
                            "recent_history": compact_records(recent_history),
                            "recent_reasoning": compact_records(
                                recent_reasoning, chars=600
                            ),
                            "user_decisions": compact_records(
                                user_decisions, full_args=True
                            ),
                            "session_stats": session_stats,
                            "tool": tool,
                            "arguments": projection_arguments(args_redacted),
                            "agent_id": agent_id,
                            "context": context,
                        },
                        "evaluation_rules": SAFETY_EVALUATION_SYSTEM_PROMPT.format(
                            anti_injection=ANTI_INJECTION_PREAMBLE,
                        ),
                    },
                    boundary="jev.evaluate_tool_call",
                    coverage=(context or {})
                    .get("raw_task_sources", {})
                    .get("coverage", "unknown"),
                    witnesses=source_witnesses((context or {}).get("raw_task_sources")),
                )
                if actual_view["overflow"]:
                    raise ValueError("Final legacy Jev model input exceeds budget")
                from intaris.input_budget import source_sections

                with source_sections(
                    [
                        (
                            ("state", "tool_call", "context", "raw_task_sources"),
                            None,
                            None,
                        ),
                    ]
                ):
                    try:
                        evaluation = jev.evaluate_tool_call(**payload)
                    except ValueError as exc:
                        from intaris.input_budget import ModelResponseError

                        raise ModelResponseError(
                            "Malformed evaluator model response"
                        ) from exc
            else:
                payload, actual_view = request_view(
                    {
                        "messages": messages,
                        "json_schema": SAFETY_EVALUATION_SCHEMA,
                        "max_tokens": STRUCTURED_OUTPUT_MAX_TOKENS,
                    },
                    boundary="llm.generate",
                    coverage=(context or {})
                    .get("raw_task_sources", {})
                    .get("coverage", "unknown"),
                    witnesses=source_witnesses((context or {}).get("raw_task_sources")),
                )
                if actual_view["overflow"]:
                    raise ValueError("Final legacy LLM model input exceeds budget")
                from intaris.inference_attribution import llm_attribution
                from intaris.input_budget import located_source, source_sections

                with (
                    llm_attribution(operation="evaluation"),
                    source_sections(
                        located_source(
                            messages[1]["content"],
                            (context or {}).get("raw_task_sources"),
                            path=("messages", 1, "content"),
                        )
                    ),
                ):
                    raw = self._llm.generate(
                        payload["messages"],
                        json_schema=payload["json_schema"],
                        max_tokens=payload["max_tokens"],
                    )
                try:
                    result = parse_json_response(
                        raw,
                        expected_keys={"aligned", "risk", "reasoning", "decision"},
                    )
                except ValueError as exc:
                    from intaris.input_budget import ModelResponseError

                    raise ModelResponseError(
                        "Malformed evaluator model response"
                    ) from exc

                evaluation = EvaluationResult(
                    aligned=bool(result.get("aligned", False)),
                    risk=str(result.get("risk", "high")),
                    reasoning=str(result.get("reasoning", "No reasoning provided")),
                    decision=str(result.get("decision", "escalate")),
                )
            wire_view = getattr(
                jev if jev is not None else self._llm, "input_view", None
            )
            if isinstance(wire_view, dict):
                actual_view = {
                    **actual_view,
                    **wire_view,
                    "boundary": "provider_request",
                }
            evaluation = _apply_authoritative_user_precedent(
                evaluation,
                tool=tool,
                args_redacted=args_redacted,
                user_decisions=user_decisions,
            )
            if jev is not None and evaluation.decision == "escalate":
                return Decision(
                    decision="escalate",
                    risk=evaluation.risk,
                    reasoning=evaluation.reasoning,
                    path="llm",
                    metadata={**(evaluation.metadata or {}), "model_view": actual_view},
                )

            decision = apply_decision_matrix(evaluation)
            decision.metadata = {
                **(evaluation.metadata or {}),
                "model_view": actual_view,
            }
            return decision

        except Exception:
            # LLM failure → propagate as exception. The API endpoint
            # catches this and returns 500, letting the client retry
            # with exponential backoff. Making a safety decision
            # (approve/deny/escalate) on an infra failure is wrong.
            logger.exception("LLM safety evaluation failed")
            raise


def _compute_path_prefix(resolved_path: str, working_directory: str) -> str:
    """Compute the approved directory prefix for a path.

    Uses a depth-aware heuristic:
    - If the path shares a deep common ancestor with working_directory
      (depth >= len(wd_parts) - 1), uses the "sibling project" prefix
      (one level deeper than the common ancestor). This is convenient
      for sibling projects under the same parent directory.
    - If the common ancestor is shallow (distant paths like /var/log),
      uses the exact parent directory of the target file. This prevents
      over-broad approval (e.g., approving /var/ when only /var/log/
      was accessed).

    Examples:
        wd:     /Users/foo/src/mnemory
        target: /Users/foo/src/intaris/intaris/classifier.py
        common: /Users/foo/src  (depth 4, >= 4)  → /Users/foo/src/intaris

        wd:     /Users/foo/src/mnemory
        target: /var/log/app.log
        common: /  (depth 1, < 4)  → /var/log

    Args:
        resolved_path: Normalized absolute path of the accessed file.
        working_directory: Session's working directory.

    Returns:
        Normalized directory prefix string.
    """
    wd_parts = os.path.normpath(working_directory).split(os.sep)
    path_parts = os.path.normpath(resolved_path).split(os.sep)

    # Find common prefix length
    common_len = 0
    for a, b in zip(wd_parts, path_parts):
        if a != b:
            break
        common_len += 1

    # Depth threshold: sibling prefix only when paths share a deep ancestor
    min_depth = len(wd_parts) - 1
    if min_depth < 1:
        min_depth = 1

    if common_len >= min_depth and common_len < len(path_parts):
        # Sibling project prefix: common ancestor + one more component
        prefix_parts = path_parts[: common_len + 1]
    else:
        # Distant path: use exact parent directory
        parent = os.path.dirname(resolved_path)
        return os.path.normpath(parent)

    return os.sep.join(prefix_parts)


def _try_merge_prefix(new_prefix: str, prefixes: list[str]) -> bool:
    """Try to merge a new prefix with an existing one in the list.

    If the new prefix shares a deep common ancestor (>= ``_MIN_MERGE_DEPTH``
    path components) with an existing prefix, replaces the existing prefix
    with the common ancestor. This naturally broadens the cache as the agent
    explores related paths.

    Example:
        existing: ``/Users/foo/.cache/opencode/node_modules/@opencode-ai/sdk/dist``
        new:      ``/Users/foo/.cache/opencode/node_modules/@opencode-ai/plugin/dist``
        common:   ``/Users/foo/.cache/opencode/node_modules/@opencode-ai`` (depth 7)
        → merges to ``/Users/foo/.cache/opencode/node_modules/@opencode-ai``

    Modifies ``prefixes`` in place.

    Args:
        new_prefix: Normalized new prefix to add.
        prefixes: Existing prefix list (modified in place if merged).

    Returns:
        True if merged (caller should not add the new prefix separately).
    """
    new_parts = os.path.normpath(new_prefix).split(os.sep)

    for i, existing in enumerate(prefixes):
        existing_parts = os.path.normpath(existing).split(os.sep)

        # Find common prefix length
        common_len = 0
        for a, b in zip(new_parts, existing_parts):
            if a != b:
                break
            common_len += 1

        if common_len >= _MIN_MERGE_DEPTH:
            merged = os.sep.join(new_parts[:common_len])
            if merged != existing:
                logger.info(
                    "Merging path prefixes: %s + %s → %s",
                    existing,
                    new_prefix,
                    merged,
                )
            prefixes[i] = merged
            return True

    return False


def _compute_args_hash(args: dict[str, Any]) -> str:
    """Compute a deterministic SHA-256 hash of tool arguments.

    Used for escalation retry: if the same tool+args combination was
    previously approved, the approval can be reused within the TTL window.

    Falls back to hashing the repr() if args contain non-JSON-serializable
    values (e.g., bytes, datetime objects).

    Args:
        args: Tool arguments dict.

    Returns:
        Hex-encoded SHA-256 hash string.
    """
    try:
        canonical = json.dumps(args, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        canonical = repr(sorted(args.items()))
    return hashlib.sha256(canonical.encode()).hexdigest()
