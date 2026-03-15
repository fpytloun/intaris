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

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from typing import Any

from intaris.audit import AuditStore
from intaris.classifier import (
    Classification,
    classify,
    extract_bash_paths,
    extract_paths,
    is_path_within,
    is_read_only,
    resolve_path,
)
from intaris.config import AnalysisConfig
from intaris.db import Database
from intaris.decision import (
    Decision,
    EvaluationResult,
    apply_decision_matrix,
    make_fast_decision,
)
from intaris.llm import LLMClient, parse_json_response
from intaris.prompts import (
    SAFETY_EVALUATION_SCHEMA,
    SAFETY_EVALUATION_SYSTEM_PROMPT,
    build_evaluation_user_prompt,
)
from intaris.redactor import redact
from intaris.sanitize import ANTI_INJECTION_PREAMBLE
from intaris.session import SessionStore

# Escalation retry: reuse approval if same tool+args approved within this window.
_ESCALATION_RETRY_TTL_MINUTES = 10

# Maximum number of approved path prefixes per session (FIFO eviction).
_MAX_APPROVED_PATHS_PER_SESSION = 50

# Minimum common ancestor depth (in path components) for prefix merging.
# Prevents merging to overly broad prefixes like /Users or /home.
# Example: /Users/foo/.cache/opencode = 4 components (excluding root).
_MIN_MERGE_DEPTH = 4

logger = logging.getLogger(__name__)


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
    ):
        self._llm = llm
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
        self._approved_paths_lock = threading.Lock()

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
    ) -> dict[str, Any]:
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
        call_id = str(uuid.uuid4())

        # Get session for intention and policy (verifies ownership)
        session = self._sessions.get(session_id, user_id=user_id)

        # Check session status — deny evaluation for inactive sessions
        session_status = session.get("status", "active")

        # Auto-resume idle sessions silently (behavioral guardrails)
        if session_status == "idle":
            try:
                self._sessions.update_status(session_id, "active", user_id=user_id)
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
            self._audit.insert(
                call_id=call_id,
                user_id=user_id,
                session_id=session_id,
                agent_id=agent_id,
                tool=tool,
                args_redacted=redact(args),
                classification="write",
                evaluation_path="fast",
                decision="deny",
                risk="low",
                reasoning=reasoning,
                latency_ms=latency_ms,
            )
            try:
                self._sessions.increment_counter(session_id, "deny", user_id=user_id)
            except ValueError:
                pass
            return {
                "call_id": call_id,
                "decision": "deny",
                "reasoning": reasoning,
                "risk": "low",
                "path": "fast",
                "latency_ms": latency_ms,
                "args_redacted": redact(args),
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
        if self._alignment_barrier is not None:
            misalignment_reason = self._alignment_barrier.is_misaligned(
                user_id, session_id
            )
            if misalignment_reason:
                latency_ms = int((time.monotonic() - start_time) * 1000)
                args_redacted = redact(args)
                args_hash = _compute_args_hash(args)
                self._audit.insert(
                    call_id=call_id,
                    user_id=user_id,
                    session_id=session_id,
                    agent_id=agent_id,
                    tool=tool,
                    args_redacted=args_redacted,
                    classification="write",
                    evaluation_path="alignment",
                    decision="escalate",
                    risk="high",
                    reasoning=misalignment_reason,
                    latency_ms=latency_ms,
                    args_hash=args_hash,
                    intention=session.get("intention"),
                )
                try:
                    self._sessions.increment_counter(
                        session_id, "escalate", user_id=user_id
                    )
                except ValueError:
                    pass
                return {
                    "call_id": call_id,
                    "decision": "escalate",
                    "reasoning": misalignment_reason,
                    "risk": "high",
                    "path": "alignment",
                    "latency_ms": latency_ms,
                    "args_redacted": args_redacted,
                    "classification": "write",
                }

        # Lookup behavioral profile for context injection
        profile_version: int | None = None
        if self._analysis_enabled and self._db is not None:
            profile = self._get_behavioral_context(user_id, agent_id)
            if profile:
                profile_version = profile.get("profile_version")
                # Inject context for high/critical risk profiles
                if profile.get("risk_level") in ("high", "critical"):
                    context = dict(context) if context else {}
                    context["behavioral_alert"] = profile.get("context_summary", "")

        # Redact secrets from args
        args_redacted = redact(args)

        # Scan for prompt injection patterns in tool args (log-only).
        # This runs AFTER redaction so we scan what the LLM will see.
        from intaris.sanitize import (
            detect_injection_patterns,
            log_injection_warning,
        )

        _injection_detected = False
        args_text = json.dumps(args_redacted, default=str)
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

        # Get session policy for classifier
        session_policy = session.get("policy")

        # Extract working directory for path-aware classification
        details = session.get("details") or {}
        working_directory: str | None = details.get("working_directory") or None

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
                parent_intention = parent_session.get("intention")

                # Parent lifecycle cascade: if parent is terminated or
                # suspended, auto-suspend the child session. This prevents
                # orphaned children from operating under a dead parent.
                parent_status = parent_session.get("status", "active")
                if parent_status in ("terminated", "suspended"):
                    status_reason = f"Parent session is {parent_status}"
                    try:
                        self._sessions.update_status(
                            session_id,
                            "suspended",
                            user_id=user_id,
                            status_reason=status_reason,
                        )
                    except ValueError:
                        pass
                    latency_ms = int((time.monotonic() - start_time) * 1000)
                    cascade_args_redacted = redact(args)
                    cascade_reasoning = (
                        f"Session suspended — parent session is {parent_status}"
                    )
                    self._audit.insert(
                        call_id=call_id,
                        user_id=user_id,
                        session_id=session_id,
                        agent_id=agent_id,
                        tool=tool,
                        args_redacted=cascade_args_redacted,
                        classification="write",
                        evaluation_path="fast",
                        decision="deny",
                        risk="low",
                        reasoning=cascade_reasoning,
                        latency_ms=latency_ms,
                    )
                    try:
                        self._sessions.increment_counter(
                            session_id, "deny", user_id=user_id
                        )
                    except ValueError:
                        pass
                    return {
                        "call_id": call_id,
                        "decision": "deny",
                        "reasoning": cascade_reasoning,
                        "risk": "low",
                        "path": "fast",
                        "latency_ms": latency_ms,
                        "args_redacted": cascade_args_redacted,
                        "classification": "write",
                        "session_status": "suspended",
                        "status_reason": status_reason,
                    }
            except ValueError:
                logger.debug(
                    "Parent session %s not found for sub-session %s",
                    parent_session_id,
                    session_id,
                )

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
        ):
            raw_paths = extract_paths(args)
            # Also extract absolute paths from bash commands for consistency
            # with the classifier's _resolve_tool_paths().
            if tool == "bash":
                command = args.get("command", "") or args.get("cmd", "")
                if command:
                    raw_paths.extend(extract_bash_paths(str(command)))
            if raw_paths:
                resolved_paths = [resolve_path(p, working_directory) for p in raw_paths]
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

        # Inject project_path into LLM context for WRITE evaluations
        if working_directory and classification == Classification.WRITE:
            context = dict(context) if context else {}
            context["project_path"] = working_directory

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
            decision = make_fast_decision(
                "critical",
                f"Critical pattern detected in {tool} call",
            )
        elif classification == Classification.ESCALATE:
            # Check escalation retry: reuse prior approval if same
            # tool+args was approved within the TTL window.
            retry_decision = self._check_escalation_retry(
                user_id=user_id,
                tool=tool,
                args_hash=args_hash,
            )
            if retry_decision is not None:
                decision = retry_decision
            else:
                decision = make_fast_decision(
                    "escalate",
                    f"Tool preference requires escalation for {tool}",
                )
        else:
            # Step 6: LLM safety evaluation
            decision = self._llm_evaluate(
                session=session,
                tool=tool,
                args_redacted=args_redacted,
                agent_id=agent_id,
                context=context,
                parent_intention=parent_intention,
            )

        # Learn from LLM approvals: cache path prefixes for path-reclassified
        # calls so subsequent reads to the same directory are fast-pathed.
        # User-approved escalations are handled separately via
        # learn_from_approved_escalation() called from POST /decision.
        if path_reclassified and decision.decision == "approve" and working_directory:
            for rp in resolved_paths:
                if not is_path_within(rp, working_directory):
                    prefix = _compute_path_prefix(rp, working_directory)
                    self._approve_path_prefix(user_id, session_id, prefix)
                    logger.info(
                        "Learned approved path prefix: %s (session %s)",
                        prefix,
                        session_id,
                    )

        # Calculate latency
        latency_ms = int((time.monotonic() - start_time) * 1000)

        # Step 7: Audit (with args_hash for escalation retry, profile_version)
        self._audit.insert(
            call_id=call_id,
            user_id=user_id,
            session_id=session_id,
            agent_id=agent_id,
            tool=tool,
            args_redacted=args_redacted,
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
        # messages have been received (intention_source is still "initial"),
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
                if not tq.cancel_duplicate("intention_update", user_id, session_id):
                    tq.enqueue(
                        "intention_update",
                        user_id,
                        session_id=session_id,
                        payload={"trigger": "bootstrap"},
                        priority=1,
                    )
            except Exception:
                logger.debug(
                    "Failed to enqueue bootstrap intention update",
                    exc_info=True,
                )

        return {
            "call_id": call_id,
            "decision": decision.decision,
            "reasoning": decision.reasoning,
            "risk": decision.risk,
            "path": decision.path,
            "latency_ms": latency_ms,
            "args_redacted": args_redacted,
            "classification": classification.value,
            "injection_detected": _injection_detected,
        }

    def _get_behavioral_context(
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
        if self._db is None:
            return None

        try:
            with self._db.cursor() as cur:
                # Try agent-specific profile first
                if agent_id:
                    cur.execute(
                        "SELECT risk_level, context_summary, profile_version "
                        "FROM behavioral_profiles "
                        "WHERE user_id = ? AND agent_id = ?",
                        (user_id, agent_id),
                    )
                    row = cur.fetchone()
                    if row:
                        return dict(row)

                # Fall back to user-level profile (agent_id='')
                cur.execute(
                    "SELECT risk_level, context_summary, profile_version "
                    "FROM behavioral_profiles "
                    "WHERE user_id = ? AND agent_id = ''",
                    (user_id,),
                )
                row = cur.fetchone()
            return dict(row) if row else None
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
        """Check if a prior escalation for the same tool+args was approved.

        Looks for an audit record within the retry TTL window where:
        - Same user, tool, and args_hash (session-independent so approvals
          survive MCP proxy reconnects)
        - The escalation was resolved with user_decision='approve'

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
        raw_paths = extract_paths(args_redacted)
        if tool == "bash" or tool == "Bash":
            command = args_redacted.get("command", "") or args_redacted.get("cmd", "")
            if command:
                raw_paths.extend(extract_bash_paths(str(command)))
        if not raw_paths:
            return

        for p in raw_paths:
            resolved = resolve_path(p, working_directory)
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
        return len(stale)

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

        user_prompt = build_evaluation_user_prompt(
            intention=session["intention"],
            policy=session.get("policy"),
            recent_history=recent_history,
            session_stats={
                "total_calls": session.get("total_calls", 0),
                "approved_count": session.get("approved_count", 0),
                "denied_count": session.get("denied_count", 0),
                "escalated_count": session.get("escalated_count", 0),
            },
            tool=tool,
            args=args_redacted,
            agent_id=agent_id,
            context=context,
            parent_intention=parent_intention,
        )

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
            raw = self._llm.generate(
                messages,
                json_schema=SAFETY_EVALUATION_SCHEMA,
                max_tokens=1024,
            )
            result = parse_json_response(
                raw,
                expected_keys={"aligned", "risk", "reasoning", "decision"},
            )

            evaluation = EvaluationResult(
                aligned=bool(result.get("aligned", False)),
                risk=str(result.get("risk", "high")),
                reasoning=str(result.get("reasoning", "No reasoning provided")),
                decision=str(result.get("decision", "escalate")),
            )

            return apply_decision_matrix(evaluation)

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
