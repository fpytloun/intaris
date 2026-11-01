"""Audit log storage for intaris.

Records every tool call evaluation with decision, reasoning, redacted
args, and timing information. Supports filtered queries for the audit
log browser and session detail views. All operations are scoped by
user_id for multi-tenant isolation.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

from intaris.db import Database
from intaris.decision import UNATTENDED_JUDGE_REVIEW

logger = logging.getLogger(__name__)


class AuditStore:
    """Audit log CRUD backed by SQLite.

    All operations require user_id for tenant isolation.
    """

    VALID_RECORD_TYPES = {"tool_call", "reasoning", "checkpoint", "summary"}

    # Optional SearchService handle, set by the server lifespan once
    # both services are ready. When present, every insert fans out to
    # the search indexer (vector tier only — lexical search reads
    # canonical tables directly via tsvector). Best-effort: indexer
    # failures never block the canonical audit insert.
    _search_service: Any = None

    @classmethod
    def set_search_service(cls, service: Any) -> None:
        cls._search_service = service

    @classmethod
    def clear_search_service(cls, service: Any) -> None:
        """Clear the search hook only if the caller still owns it."""
        if cls._search_service is service:
            cls._search_service = None

    def __init__(self, db: Database):
        self._db = db

    def insert(
        self,
        *,
        call_id: str,
        user_id: str,
        session_id: str,
        agent_id: str | None,
        tool: str | None,
        args_redacted: dict[str, Any] | None,
        classification: str | None,
        evaluation_path: str,
        decision: str,
        risk: str | None,
        reasoning: str | None,
        latency_ms: int,
        record_type: str = "tool_call",
        content: str | None = None,
        args_hash: str | None = None,
        profile_version: int | None = None,
        intention: str | None = None,
        injection_detected: bool = False,
        raw_decision: str | None = None,
        maximum_outcome: str | None = None,
        minimum_outcome: str | None = None,
        outcome_override: str | None = None,
        evaluation_metadata: dict[str, Any] | None = None,
        admission_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Insert an audit record.

        Args:
            call_id: External correlation ID for the tool call.
            user_id: Tenant identifier.
            session_id: Session this call belongs to.
            agent_id: Agent that made the call (optional).
            tool: Tool name (e.g., "bash", "edit"). Null for reasoning checkpoints.
            args_redacted: Tool arguments with secrets redacted. For reasoning
                records, may store associated context metadata.
            classification: "read" or "write". Null for reasoning checkpoints.
            evaluation_path: "fast", "critical", "llm", or "reasoning".
            decision: "approve", "deny", or "escalate".
            risk: Risk level from LLM evaluation (null for fast path).
            reasoning: LLM reasoning or pattern match explanation.
            latency_ms: Time taken for the evaluation in milliseconds.
            record_type: Record type: "tool_call", "reasoning", "checkpoint",
                or "summary".
            content: Reasoning or checkpoint text (null for tool_call records).
            args_hash: SHA-256 hash of canonical args for escalation retry.
            profile_version: Behavioral profile version at time of evaluation.
            intention: Session intention at time of evaluation (for tracking).
            injection_detected: Whether prompt injection patterns were detected
                in the tool args or session intention.

        Returns:
            The created audit record as a dict.
        """
        if record_type not in self.VALID_RECORD_TYPES:
            raise ValueError(
                f"Invalid record_type '{record_type}'. "
                f"Must be one of: {self.VALID_RECORD_TYPES}"
            )

        record_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        with self._db.cursor() as cur:
            if record_type == "reasoning" and (content or "").startswith(
                "User message:"
            ):
                # Only user-role evidence changes authorization, not activity.
                role = (args_redacted or {}).get("context", args_redacted or {})
                is_user_source = (
                    not isinstance(role, dict) or role.get("role", "user") == "user"
                )
                cur.execute(
                    "UPDATE sessions SET state_revision = state_revision + ?, latest_user_evidence = COALESCE(?, latest_user_evidence) "
                    "WHERE user_id = ? AND session_id = ?",
                    (
                        int(is_user_source),
                        json.dumps(
                            {
                                "source": "reasoning_client",
                                "content": content,
                                "source_audit_call_id": call_id,
                                "recorded_context": args_redacted,
                            }
                        )
                        if is_user_source
                        else None,
                        user_id,
                        session_id,
                    ),
                )
            if admission_snapshot is not None:
                from intaris.policy_activation import HybridError
                from intaris.policy_inheritance import validate_parent_chain

                # Ancestors before descendants on both activation and admission:
                # parent updates cannot commit between validation and admission.
                validate_parent_chain(
                    cur, admission_snapshot, postgresql=self._db.backend == "postgresql"
                )
                # This no-op CAS takes the session write lock on both engines.
                # Every relevant writer increments state_revision on this row.
                metadata = evaluation_metadata or {}
                operator_bound = metadata.get("operator_bound") is True
                cur.execute(
                    "UPDATE sessions SET state_revision = state_revision "
                    "WHERE user_id = ? AND session_id = ? "
                    "AND policy_revision = ? "
                    + (
                        ""
                        if operator_bound
                        else "AND state_revision = ? AND status = 'active'"
                    ),
                    (
                        user_id,
                        session_id,
                        admission_snapshot["policy_revision"],
                        *(
                            ()
                            if operator_bound
                            else (admission_snapshot["state_revision"],)
                        ),
                    ),
                )
                if cur.rowcount != 1:
                    raise HybridError(
                        "stale_policy_revision"
                        if operator_bound
                        else "stale_admission",
                        "Session changed during evaluation",
                        409,
                    )
                workflow = metadata.get("workflow_scope")
                if workflow is not None:
                    from intaris.workflow_scope import registry_revision

                    if (
                        registry_revision(cur, user_id, session_id)
                        != workflow["registry_revision"]
                    ):
                        raise HybridError(
                            "workflow_step_conflict",
                            "Workflow declaration changed during admission",
                            409,
                        )
                if operator_bound:
                    # Resolve under the same lock as human/lifecycle/policy
                    # writers. No assessment or task context was used to grant.
                    cur.execute(
                        "SELECT status, policy, state_revision FROM sessions "
                        "WHERE user_id = ? AND session_id = ?",
                        (user_id, session_id),
                    )
                    current = cur.fetchone()
                    policy = json.loads(current["policy"] or "{}")
                    if policy.get("maximum_outcome") != "approve" or policy.get(
                        "policy_hash"
                    ) != metadata.get("policy_hash"):
                        raise HybridError("stale_policy_hash", "Policy changed", 409)
                    metadata["state_revision"] = current["state_revision"]
                    if current["status"] != "active":
                        decision, outcome_override = (
                            "deny",
                            "hard_stop.session_lifecycle",
                        )
                if (
                    operator_bound
                    or (workflow or {}).get("declared")
                    or (metadata.get("policy_result") or {}).get("permission_basis")
                    == "read_only_static"
                ):
                    cur.execute(
                        "SELECT call_id, user_decision, evaluation_metadata FROM audit_log "
                        "WHERE user_id = ? AND session_id = ? AND tool = ? AND args_hash = ? "
                        "AND resolved_by = 'user' AND user_decision IS NOT NULL "
                        "ORDER BY resolved_at DESC, id DESC",
                        (user_id, session_id, tool, args_hash),
                    )
                    for denied in cur.fetchall():
                        denial_meta = json.loads(denied["evaluation_metadata"] or "{}")
                        if denial_meta.get("mode_identity") == metadata.get(
                            "mode_identity"
                        ):
                            if denied["user_decision"] == "deny":
                                decision, outcome_override = "deny", "human_resolution"
                                metadata["human_denial_reference"] = denied["call_id"]
                            # A newer exact human approval clears a prior veto;
                            # it does not grant a reusable approval across policies.
                            break
                    if decision != "approve":
                        metadata["admission"] = None
                    final = metadata["final_policy_result"]
                    if decision != final["decision"]:
                        final.update(
                            decision=decision,
                            override=outcome_override,
                            disposition="blocked",
                            blocking_reasons=[
                                *final["blocking_reasons"],
                                outcome_override,
                            ],
                            waived_obligation_ids=[],
                        )
                        metadata["obligation_disposition"] = "blocked"
                        reasoning = f"Authorization blocked: {outcome_override}."
                if metadata.get("task_context"):
                    from intaris.task_context import validate_freshness

                    validate_freshness(cur, metadata, user_id, session_id)
                if metadata.get("approval_reference"):
                    cur.execute(
                        "SELECT user_decision, evaluation_metadata FROM audit_log WHERE call_id = ? "
                        "AND user_id = ? AND session_id = ? AND tool = ? AND args_hash = ?",
                        (
                            metadata["approval_reference"],
                            user_id,
                            session_id,
                            tool,
                            args_hash,
                        ),
                    )
                    approved = cur.fetchone()
                    approved_meta = (
                        json.loads(approved["evaluation_metadata"] or "{}")
                        if approved
                        else {}
                    )
                    if (
                        not approved
                        or approved["user_decision"] != "approve"
                        or approved_meta.get("state_revision")
                        != metadata["state_revision"]
                        or approved_meta.get("policy_hash")
                        != metadata.get("policy_hash")
                        or approved_meta.get("mode_identity")
                        != metadata.get("mode_identity")
                    ):
                        raise HybridError(
                            "stale_approval", "Approval changed before admission", 409
                        )
                cur.execute(
                    "SELECT action_digest, effective_decision, decision, call_id FROM audit_log "
                    "WHERE user_id = ? AND session_id = ? AND logical_call_id = ?",
                    (user_id, session_id, metadata.get("logical_call_id")),
                )
                for previous in cur.fetchall():
                    if previous["action_digest"] != metadata.get("action_digest"):
                        raise HybridError(
                            "logical_call_conflict",
                            "Logical call arguments changed",
                            409,
                        )
                    if previous["effective_decision"] == "approve" and not (
                        previous["decision"] == "escalate"
                        and previous["call_id"] == metadata.get("approval_reference")
                    ):
                        raise HybridError(
                            "duplicate_admission",
                            "Logical call already admitted; reconcile before retry",
                            409,
                        )
            cur.execute(
                """
                INSERT INTO audit_log
                    (id, call_id, record_type, user_id, session_id, agent_id,
                     timestamp, tool, args_redacted, content, classification,
                     evaluation_path, decision, risk, reasoning, latency_ms,
                     args_hash, profile_version, intention, injection_detected,
                     raw_decision, effective_decision, maximum_outcome,
                     minimum_outcome, outcome_override, evaluation_metadata,
                     logical_call_id, action_digest)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    call_id,
                    record_type,
                    user_id,
                    session_id,
                    agent_id,
                    now,
                    tool,
                    json.dumps(args_redacted) if args_redacted is not None else None,
                    content,
                    classification,
                    evaluation_path,
                    decision,
                    risk,
                    reasoning,
                    latency_ms,
                    args_hash,
                    profile_version,
                    intention,
                    injection_detected,
                    raw_decision or decision,
                    decision,
                    maximum_outcome,
                    minimum_outcome,
                    outcome_override,
                    json.dumps(evaluation_metadata)
                    if evaluation_metadata is not None
                    else None,
                    (evaluation_metadata or {}).get("logical_call_id"),
                    (evaluation_metadata or {}).get("action_digest"),
                ),
            )

        # Fan out to the search indexer (vector tier only). Lexical
        # search reads canonical tables directly via tsvector — no
        # write needed here for that.
        svc = AuditStore._search_service
        if svc is not None:
            try:
                if intention and intention.strip():
                    svc.enqueue_audit_intention(
                        user_id=user_id,
                        session_id=session_id,
                        agent_id=agent_id,
                        ref_id=record_id,
                        intention=intention,
                        ts=now,
                    )
                if (
                    record_type in ("reasoning", "checkpoint")
                    and content
                    and content.strip()
                ):
                    svc.enqueue_audit_reasoning(
                        user_id=user_id,
                        session_id=session_id,
                        agent_id=agent_id,
                        ref_id=record_id,
                        content=content,
                        ts=now,
                    )
            except Exception:
                logger.exception(
                    "Search indexer enqueue failed (audit_log insert "
                    "user=%s session=%s record_type=%s)",
                    user_id,
                    session_id,
                    record_type,
                )

        return {
            "id": record_id,
            "call_id": call_id,
            "record_type": record_type,
            "user_id": user_id,
            "session_id": session_id,
            "agent_id": agent_id,
            "timestamp": now,
            "tool": tool,
            "args_redacted": args_redacted,
            "content": content,
            "classification": classification,
            "evaluation_path": evaluation_path,
            "decision": decision,
            "raw_decision": raw_decision or decision,
            "effective_decision": decision,
            "maximum_outcome": maximum_outcome,
            "minimum_outcome": minimum_outcome,
            "outcome_override": outcome_override,
            "risk": risk,
            "reasoning": reasoning,
            "latency_ms": latency_ms,
            "user_decision": None,
            "user_note": None,
            "resolved_at": None,
            "resolved_by": None,
            "judge_reasoning": None,
            "judge_decision": None,
            "judge_risk": None,
            "args_hash": args_hash,
            "profile_version": profile_version,
            "intention": intention,
            "injection_detected": injection_detected,
            "evaluation_metadata": evaluation_metadata,
        }

    def get_by_call_id(self, call_id: str, *, user_id: str) -> dict[str, Any]:
        """Get an audit record by call_id, scoped to user.

        Raises:
            ValueError: If record not found.
        """
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT * FROM audit_log WHERE call_id = ? AND user_id = ?",
                (call_id, user_id),
            )
            row = cur.fetchone()

        if row is None:
            raise ValueError(f"Audit record with call_id={call_id} not found")

        return _row_to_dict(row)

    def get_by_id(self, record_id: str, *, user_id: str) -> dict[str, Any]:
        """Get an audit record by internal ID, scoped to user.

        Raises:
            ValueError: If record not found.
        """
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT * FROM audit_log WHERE id = ? AND user_id = ?",
                (record_id, user_id),
            )
            row = cur.fetchone()

        if row is None:
            raise ValueError(f"Audit record {record_id} not found")

        return _row_to_dict(row)

    def query(
        self,
        *,
        user_id: str,
        session_id: str | None = None,
        agent_id: str | None = None,
        record_type: str | None = None,
        tool: str | None = None,
        decision: str | None = None,
        risk: str | None = None,
        evaluation_path: str | None = None,
        from_ts: str | None = None,
        to_ts: str | None = None,
        resolved: bool | None = None,
        page: int = 1,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Query audit records with filters and pagination.

        Args:
            user_id: Tenant identifier (always required).
            record_type: Filter by record type (tool_call, reasoning, checkpoint).
            resolved: Filter by resolution status. ``True`` returns only
                records with a user_decision set, ``False`` returns only
                unresolved records (user_decision IS NULL). ``None`` (default)
                returns all records regardless of resolution status.

        Returns:
            Dict with 'items', 'total', 'page', 'pages'.
        """
        conditions: list[str] = ["user_id = ?"]
        params: list[Any] = [user_id]

        if session_id:
            conditions.append("session_id = ?")
            params.append(session_id)
        if agent_id:
            conditions.append("agent_id = ?")
            params.append(agent_id)
        if record_type:
            conditions.append("record_type = ?")
            params.append(record_type)
        if tool:
            if self._db.backend == "postgresql":
                conditions.append("tool ILIKE ?")
            else:
                conditions.append("tool LIKE ? COLLATE NOCASE")
            params.append(f"%{tool}%")
        if decision:
            conditions.append("decision = ?")
            params.append(decision)
        if risk:
            conditions.append("risk = ?")
            params.append(risk)
        if evaluation_path:
            conditions.append("evaluation_path = ?")
            params.append(evaluation_path)
        if from_ts:
            conditions.append("timestamp >= ?")
            params.append(from_ts)
        if to_ts:
            conditions.append("timestamp <= ?")
            params.append(to_ts)
        if resolved is True:
            conditions.append("user_decision IS NOT NULL")
        elif resolved is False:
            conditions.append("user_decision IS NULL")
            # Judge-only provisional calls must not appear in human approvals.
            conditions.append("(outcome_override IS NULL OR outcome_override != ?)")
            params.append(UNATTENDED_JUDGE_REVIEW)

        where = ""
        if conditions:
            where = "WHERE " + " AND ".join(conditions)

        with self._db.cursor() as cur:
            # Count total matching records
            cur.execute(f"SELECT COUNT(*) FROM audit_log {where}", params)
            total = cur.fetchone()[0]

            # Fetch page
            offset = (page - 1) * limit
            cur.execute(
                f"""
                SELECT * FROM audit_log
                {where}
                ORDER BY timestamp DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                params + [limit, offset],
            )
            rows = cur.fetchall()

        pages = max(1, (total + limit - 1) // limit)
        return {
            "items": [_row_to_dict(row) for row in rows],
            "total": total,
            "page": page,
            "pages": pages,
        }

    def get_recent(
        self,
        session_id: str,
        *,
        user_id: str,
        limit: int = 10,
        record_type: str | None = None,
        before: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get recent audit records for a session.

        Used to assemble context for LLM safety evaluation.

        Args:
            session_id: Session to query.
            user_id: Tenant identifier.
            limit: Max records to return.
            record_type: Filter by record type (e.g., "tool_call", "reasoning").
            before: ISO timestamp upper bound (inclusive). When set, only
                records with ``timestamp <= before`` are returned. Used
                for time-bounding parent session queries to a child
                session's creation time.

        Returns:
            List of audit record dicts, most recent first.
        """
        if record_type and record_type not in self.VALID_RECORD_TYPES:
            raise ValueError(
                f"Invalid record_type '{record_type}'. "
                f"Must be one of: {self.VALID_RECORD_TYPES}"
            )

        conditions = ["session_id = ?", "user_id = ?"]
        params: list[Any] = [session_id, user_id]

        if record_type:
            conditions.append("record_type = ?")
            params.append(record_type)

        if before:
            conditions.append("timestamp <= ?")
            params.append(before)

        params.append(limit)
        sql = (
            "SELECT * FROM audit_log"
            f" WHERE {' AND '.join(conditions)}"
            " ORDER BY timestamp DESC"
            " LIMIT ?"
        )

        with self._db.cursor() as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()

        return [_row_to_dict(row) for row in rows]

    def get_user_decisions(
        self,
        session_id: str,
        *,
        user_id: str,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Get recent final human decisions for a session.

        Only final human resolutions are returned. Judge-authored decisions
        are excluded so this data can be treated as authoritative human
        guidance in evaluator, judge, and intention prompts.

        Args:
            session_id: Session to query.
            user_id: Tenant identifier.
            limit: Max decision records to return.

        Returns:
            List of recent human decision records, newest first.
        """
        with self._db.cursor() as cur:
            cur.execute(
                """
                SELECT call_id, session_id, tool, args_redacted, decision,
                       user_decision, user_note, resolved_at, resolved_by,
                       risk, reasoning, evaluation_path, intention, args_hash,
                       timestamp
                FROM audit_log
                WHERE user_id = ? AND session_id = ?
                  AND record_type = 'tool_call'
                  AND user_decision IS NOT NULL
                  AND resolved_by = 'user'
                  AND (resolution_scope IS NULL OR resolution_scope != 'action')
                  AND COALESCE(user_note, '') != 'Resolved via notification action link'
                ORDER BY COALESCE(resolved_at, timestamp) DESC
                LIMIT ?
                """,
                (user_id, session_id, limit),
            )
            rows = cur.fetchall()

        return [_row_to_dict(row) for row in rows]

    def get_human_denials(
        self, session_id: str, *, user_id: str
    ) -> list[dict[str, Any]]:
        """Get every final human denial of a tool call in a session."""
        with self._db.cursor() as cur:
            cur.execute(
                """
                SELECT tool, args_redacted, args_hash, evaluation_metadata
                FROM audit_log
                WHERE user_id = ? AND session_id = ?
                  AND record_type = 'tool_call'
                  AND resolved_by = 'user' AND user_decision = 'deny'
                """,
                (user_id, session_id),
            )
            rows = cur.fetchall()
        return [_row_to_dict(row) for row in rows]

    def find_approved_escalation(
        self,
        *,
        user_id: str,
        tool: str,
        args_hash: str,
        cutoff: str,
    ) -> dict[str, Any] | None:
        """Find a prior approved override for the same tool+args.

        Used by the evaluator for retry: if the same tool+args
        combination was approved (via escalation resolution or denial
        override) within the TTL window, the approval can be reused.

        The query is decision-agnostic — it matches any audit record
        with ``user_decision='approve'`` regardless of the original
        ``decision`` column (escalate or deny).

        Args:
            user_id: Tenant identifier.
            tool: Tool name.
            args_hash: SHA-256 hash of canonical args.
            cutoff: ISO timestamp — only consider approvals after this time.

        Returns:
            Audit record dict if a matching approval is found, else None.
        """
        with self._db.cursor() as cur:
            cur.execute(
                """
                SELECT call_id, reasoning FROM audit_log
                WHERE user_id = ? AND tool = ?
                  AND args_hash = ? AND user_decision = 'approve'
                  AND (outcome_override IS NULL OR outcome_override != ?)
                  AND (
                      resolved_by = 'user'
                      OR COALESCE(raw_decision, '') != 'deny'
                      OR COALESCE(maximum_outcome, 'deny') = 'deny'
                  )
                  AND resolved_at >= ?
                ORDER BY resolved_at DESC
                LIMIT 1
                """,
                (user_id, tool, args_hash, UNATTENDED_JUDGE_REVIEW, cutoff),
            )
            row = cur.fetchone()

        if row is None:
            return None
        return dict(row)

    def resolve_escalation(
        self,
        call_id: str,
        user_decision: str,
        user_note: str | None = None,
        *,
        user_id: str,
        resolved_by: str = "user",
        judge_reasoning: str | None = None,
        judge_decision: str | None = None,
        judge_risk: str | None = None,
        hybrid_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record a user decision on an escalated or denied tool call.

        Supports:
        - Resolving unresolved escalations (first resolution)
        - Overriding judge decisions on escalations
        - Ex-post overriding L1 denials (critical auto-deny, LLM deny)

        Human decisions (``resolved_by="user"``) are final and cannot be
        overridden.

        Args:
            call_id: The escalated or denied call to resolve.
            user_decision: "approve" or "deny".
            user_note: Optional note from the resolver.
            user_id: Tenant identifier.
            resolved_by: Who resolved: "user" (human) or "judge" (LLM judge).
            judge_reasoning: Judge's reasoning (stored when judge resolves).
            judge_decision: Historical judge recommendation.
            judge_risk: Historical judge risk assessment.

        Returns:
            Updated audit record.

        Raises:
            ValueError: If record not found, not an escalation or denial,
                or already resolved by a human user.
        """
        if user_decision not in ("approve", "deny"):
            raise ValueError(
                f"Invalid user_decision '{user_decision}'. Must be 'approve' or 'deny'."
            )
        if resolved_by not in ("user", "judge"):
            raise ValueError(
                f"Invalid resolved_by '{resolved_by}'. Must be 'user' or 'judge'."
            )

        now = datetime.now(timezone.utc).isoformat()
        existing = self.get_by_call_id(call_id, user_id=user_id)
        existing_meta = existing.get("evaluation_metadata") or {}
        policy_result = existing_meta.get("policy_result") or {}
        outstanding = policy_result.get("outstanding_obligations") or existing_meta.get(
            "outstanding_obligations", []
        )
        has_alignment_obligation = any(
            item.get("id") == "alignment.compatibility" for item in outstanding
        )
        alignment_override = (
            user_decision == "approve"
            and resolved_by == "user"
            and (
                (
                    existing_meta.get("evaluation_policy_version") == "hybrid_v1"
                    and has_alignment_obligation
                )
                or existing.get("evaluation_path") == "alignment"
            )
        )
        from intaris import read_only
        from intaris.policy_activation import HybridError

        if user_decision == "approve" and (existing_meta.get("overlay") or {}).get(
            "read_only_required"
        ):
            candidate = (
                hybrid_metadata
                if resolved_by == "judge" and hybrid_metadata
                else existing_meta
            )
            mutation = candidate.get(
                "mutation", candidate.get("facts", {}).get("mutation", "unclear")
            )
            if existing_meta.get("static_facts", {}).get("mutation") == "mutating":
                mutation = "mutating"
            bounded, _ = read_only.enforce(
                "approve", existing_meta["overlay"], mutation
            )
            if bounded != "approve":
                raise HybridError(
                    "read_only_ceiling",
                    "This call has no read-only proof; change mode in a new request",
                    409,
                )
        if existing_meta.get("evaluation_policy_version") == "hybrid_v1":
            from intaris.policy_activation import HybridError

            if user_decision == "approve" and existing.get("minimum_outcome") == "deny":
                raise HybridError(
                    "mandatory_denial",
                    "The request denial floor cannot be approved",
                    409,
                )
            if (
                resolved_by == "judge"
                and user_decision == "approve"
                and (
                    (hybrid_metadata or existing_meta).get("human_approval_required")
                    or existing.get("minimum_outcome") in {"deny", "escalate"}
                )
            ):
                raise HybridError(
                    "human_approval_required", "Judge cannot satisfy a human floor", 409
                )

        # Atomic update: conditions in WHERE prevent TOCTOU races.
        # Matches unresolved escalations/denials OR records resolved by
        # the judge (allowing human override of judge decisions). Human
        # decisions are final — resolved_by='user' is NOT matched.
        # COALESCE preserves existing judge_reasoning when the caller
        # does not provide it (i.e., human override passes None).
        # Exact-action consent with no accompanying instruction changes only
        # this receipt, not the authorization state of sibling actions.
        action_local = (
            resolved_by == "user"
            and user_decision == "approve"
            and not user_note
            and existing.get("decision") == "escalate"
            and (
                existing.get("resolved_by") is None
                or (
                    existing.get("resolved_by") == "judge"
                    and existing.get("user_decision") == "escalate"
                    and existing.get("judge_decision") == "defer"
                )
            )
            and existing.get("evaluation_path") != "alignment"
            and not alignment_override
            and existing_meta.get("evaluation_policy_version") == "hybrid_v1"
            and bool(existing.get("action_digest"))
        )
        with self._db.cursor() as cur:
            if resolved_by == "user":
                # Lock first; bump only after successful resolution below.
                cur.execute(
                    "UPDATE sessions SET state_revision = state_revision "
                    "WHERE user_id = ? AND session_id = ?",
                    (user_id, existing["session_id"]),
                )
            if existing_meta.get(
                "evaluation_policy_version"
            ) == "hybrid_v1" or existing_meta.get("overlay"):
                cur.execute(
                    "UPDATE sessions SET state_revision = state_revision "
                    "WHERE user_id = ? AND session_id = ? AND policy_revision = ? "
                    "AND state_revision = ? AND status = 'active'",
                    (
                        user_id,
                        existing["session_id"],
                        existing_meta["policy_revision"],
                        existing_meta["state_revision"],
                    ),
                )
                if cur.rowcount != 1:
                    raise HybridError(
                        "stale_resolution", "Session changed before resolution", 409
                    )
                if existing_meta.get("task_context"):
                    from intaris.task_context import validate_freshness

                    validate_freshness(
                        cur,
                        existing_meta,
                        user_id,
                        existing["session_id"],
                        exclude_call_id=call_id,
                    )
                if (
                    resolved_by == "judge"
                    and hybrid_metadata is None
                    and existing_meta.get("evaluation_policy_version") == "hybrid_v1"
                ):
                    raise HybridError(
                        "invalid_judge_resolution",
                        "Hybrid classification required",
                        409,
                    )
            if resolved_by == "user" and existing_meta.get("policy_result"):
                from intaris.hybrid import PolicyResult
                from intaris.hybrid_runtime import finalize_result

                final = finalize_result(
                    PolicyResult.model_validate(existing_meta["policy_result"]),
                    maximum=existing.get("maximum_outcome"),
                    minimum=existing.get("minimum_outcome"),
                    human_resolution=user_decision,
                    overlay=existing_meta.get("overlay"),
                    mutation=existing_meta.get("facts", {}).get("mutation", "unclear"),
                )
                if final.decision != user_decision:
                    raise HybridError(
                        "protected_floor", "Resolution violates a protected floor", 409
                    )
                hybrid_metadata = {
                    **existing_meta,
                    "final_policy_result": final.model_dump(mode="json"),
                    "outstanding_obligations": [
                        item.model_dump(mode="json")
                        for item in final.outstanding_obligations
                    ],
                    "obligation_disposition": final.disposition,
                }
            if hybrid_metadata is not None:
                cur.execute(
                    "UPDATE audit_log SET evaluation_metadata = ? WHERE call_id = ? "
                    "AND user_id = ? AND (user_decision IS NULL OR resolved_by = 'judge') "
                    "AND evaluation_metadata = ?",
                    (
                        json.dumps(hybrid_metadata),
                        call_id,
                        user_id,
                        json.dumps(existing_meta),
                    ),
                )
                if cur.rowcount != 1:
                    raise HybridError(
                        "stale_resolution",
                        "Audit evidence changed before resolution",
                        409,
                    )
            cur.execute(
                """
                UPDATE audit_log
                SET user_decision = ?, user_note = ?, resolved_at = ?,
                    resolved_by = ?, resolution_scope = ?,
                    judge_reasoning = COALESCE(?, judge_reasoning),
                    judge_decision = COALESCE(?, judge_decision),
                    judge_risk = COALESCE(?, judge_risk),
                    effective_decision = ?
                WHERE call_id = ? AND user_id = ?
                  AND (decision IN ('escalate', 'deny') OR ?)
                  AND (user_decision IS NULL OR resolved_by = 'judge')
                  AND (? != 'user' OR outcome_override IS NULL OR outcome_override != ?)
                """,
                (
                    user_decision,
                    user_note,
                    now,
                    resolved_by,
                    "action" if action_local else "session",
                    judge_reasoning,
                    judge_decision,
                    judge_risk,
                    user_decision,
                    call_id,
                    user_id,
                    (resolved_by == "judge" or existing.get("resolved_by") == "judge")
                    and existing_meta.get("judge_required", False)
                    and existing_meta.get("evaluation_policy_version") == "hybrid_v1",
                    resolved_by,
                    UNATTENDED_JUDGE_REVIEW,
                ),
            )
            if cur.rowcount == 0:
                # Determine why the update failed for a clear error message
                record = self.get_by_call_id(call_id, user_id=user_id)
                if (
                    resolved_by == "user"
                    and record.get("outcome_override") == UNATTENDED_JUDGE_REVIEW
                ):
                    raise ValueError(
                        "Unattended Judge review cannot be resolved by a human"
                    )
                if record["decision"] not in ("escalate", "deny"):
                    raise ValueError(
                        f"Call {call_id} cannot be resolved "
                        f"(decision={record['decision']})"
                    )
                if record.get("user_decision"):
                    if record.get("resolved_by") == "user":
                        raise ValueError(
                            f"Call {call_id} already resolved by user "
                            f"(user_decision={record['user_decision']})"
                        )
                    raise ValueError(
                        f"Call {call_id} already resolved "
                        f"(user_decision={record['user_decision']})"
                    )
                # Shouldn't reach here, but raise generic error
                raise ValueError(f"Failed to resolve call {call_id}")
            if alignment_override:
                cur.execute(
                    "UPDATE sessions SET alignment_state = 'ready', alignment_overridden = ?, "
                    "updated_at = ? WHERE user_id = ? AND session_id = ?",
                    (True, now, user_id, existing["session_id"]),
                )
            if resolved_by == "user" and not action_local:
                cur.execute(
                    "UPDATE sessions SET state_revision = state_revision + 1 "
                    "WHERE user_id = ? AND session_id = ? "
                    "RETURNING policy_revision, state_revision",
                    (user_id, existing["session_id"]),
                )
                row = cur.fetchone()
                revision = row["state_revision"]
                resolution_revision = [row["policy_revision"], revision]
                if existing_meta:
                    # The receipt belongs to the post-decision authorization
                    # state; otherwise it would invalidate itself immediately.
                    receipt_meta = dict(hybrid_metadata or existing_meta)
                    receipt_meta["state_revision"] = revision
                    cur.execute(
                        "UPDATE audit_log SET evaluation_metadata = ? "
                        "WHERE call_id = ? AND user_id = ?",
                        (json.dumps(receipt_meta), call_id, user_id),
                    )
            else:
                cur.execute(
                    "SELECT policy_revision, state_revision FROM sessions "
                    "WHERE user_id = ? AND session_id = ?",
                    (user_id, existing["session_id"]),
                )
                row = cur.fetchone()
                resolution_revision = (
                    [row["policy_revision"], row["state_revision"]] if row else None
                )

        record = self.get_by_call_id(call_id, user_id=user_id)
        # Authorization revision at the moment of resolution (not persisted);
        # write-scope learning is valid only while it is still current.
        record["resolution_revision"] = resolution_revision
        return record

    def mark_execution(self, call_id: str, *, user_id: str, state: str) -> dict:
        """Durably claim dispatch once; a transport failure cannot reset the claim."""
        from intaris.policy_activation import HybridError

        record = self.get_by_call_id(call_id, user_id=user_id)
        original = record.get("evaluation_metadata") or {}
        expected = "not_dispatched" if state == "dispatched" else "dispatched"
        if (
            state not in {"dispatched", "completed", "uncertain"}
            or original.get("execution_state") != expected
        ):
            raise HybridError(
                "execution_state_conflict", "Execution already claimed or terminal", 409
            )
        if state == "dispatched":
            admission = original.get("admission")
            if not admission or datetime.now(timezone.utc) >= datetime.fromisoformat(
                admission["dispatch_deadline"]
            ):
                raise HybridError(
                    "expired_admission", "Admission expired before dispatch", 409
                )
        updated = {**original, "execution_state": state}
        with self._db.cursor() as cur:
            if state == "dispatched" and original.get("workflow_scope") is not None:
                from intaris.workflow_scope import registry_revision

                scope = original["workflow_scope"]
                cur.execute(
                    "UPDATE sessions SET state_revision = state_revision "
                    "WHERE user_id = ? AND session_id = ?",
                    (user_id, record["session_id"]),
                )
                if (
                    cur.rowcount != 1
                    or registry_revision(cur, user_id, record["session_id"])
                    != scope["registry_revision"]
                ):
                    raise HybridError(
                        "workflow_step_conflict",
                        "Workflow declaration changed before dispatch",
                        409,
                    )
            if state == "dispatched" and original.get("task_context"):
                from intaris.task_context import validate_freshness

                cur.execute(
                    "UPDATE sessions SET state_revision = state_revision WHERE user_id = ? "
                    "AND session_id = ? AND state_revision = ?",
                    (user_id, record["session_id"], original["state_revision"]),
                )
                if cur.rowcount != 1:
                    raise HybridError(
                        "stale_task_context", "Sources changed before dispatch", 409
                    )
                validate_freshness(
                    cur,
                    original,
                    user_id,
                    record["session_id"],
                    exclude_call_id=call_id,
                )
            cur.execute(
                "UPDATE audit_log SET evaluation_metadata = ? WHERE call_id = ? AND user_id = ? "
                "AND evaluation_metadata = ?",
                (json.dumps(updated), call_id, user_id, json.dumps(original)),
            )
            if cur.rowcount != 1:
                raise HybridError(
                    "execution_state_conflict", "Concurrent execution claim", 409
                )
        return updated

    def find_hybrid_approval(
        self,
        *,
        user_id: str,
        session_id: str,
        tool: str,
        args_hash: str,
        since: str,
    ) -> dict | None:
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT * FROM audit_log WHERE user_id = ? AND session_id = ? "
                "AND tool = ? AND args_hash = ? AND user_decision = 'approve' "
                "AND evaluation_path != 'alignment' "
                "AND resolved_at >= ? ORDER BY resolved_at DESC LIMIT 1",
                (user_id, session_id, tool, args_hash, since),
            )
            row = cur.fetchone()
        return _row_to_dict(row) if row else None

    def get_latest_user_message(self, session_id: str, *, user_id: str) -> dict | None:
        """Keep the latest user restriction independent of a rolling summary."""
        from intaris.source_capture import user_reasoning_role_sql

        role = user_reasoning_role_sql(self._db.backend)
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT * FROM audit_log WHERE user_id = ? AND session_id = ? "
                "AND record_type = 'reasoning' AND content LIKE 'User message:%' "
                "AND " + role + " = 'user' ORDER BY timestamp DESC, id DESC LIMIT 1",
                (user_id, session_id),
            )
            row = cur.fetchone()
        return _row_to_dict(row) if row else None

    def set_hybrid_review(
        self,
        call_id: str,
        *,
        user_id: str,
        metadata: dict,
        reasoning: str,
        risk: str | None,
    ) -> None:
        """Persist one unresolved classification review under the session CAS."""
        from intaris.policy_activation import HybridError

        record = self.get_by_call_id(call_id, user_id=user_id)
        with self._db.cursor() as cur:
            cur.execute(
                "UPDATE sessions SET state_revision = state_revision "
                "WHERE user_id = ? AND session_id = ? AND policy_revision = ? "
                "AND state_revision = ? AND status = 'active'",
                (
                    user_id,
                    record["session_id"],
                    metadata["policy_revision"],
                    metadata["state_revision"],
                ),
            )
            if cur.rowcount != 1:
                raise HybridError(
                    "stale_resolution", "Session changed before review", 409
                )
            if metadata.get("task_context"):
                from intaris.task_context import validate_freshness

                validate_freshness(
                    cur,
                    metadata,
                    user_id,
                    record["session_id"],
                    exclude_call_id=call_id,
                )
            cur.execute(
                "UPDATE audit_log SET evaluation_metadata = ?, judge_reasoning = ?, "
                "judge_decision = 'defer', judge_risk = ? "
                "WHERE call_id = ? AND user_id = ? AND user_decision IS NULL "
                "AND evaluation_metadata = ?",
                (
                    json.dumps(metadata),
                    reasoning,
                    risk,
                    call_id,
                    user_id,
                    json.dumps(record["evaluation_metadata"]),
                ),
            )
            if cur.rowcount != 1:
                raise HybridError("stale_review", "Call changed during review", 409)

    def record_model_view(
        self, call_id: str, *, user_id: str, stage: str, view: dict
    ) -> None:
        """Record bounded diagnostics without overwriting concurrent policy/audit metadata."""
        from intaris.policy_activation import HybridError

        with self._db.cursor() as cur:
            cur.execute(
                "SELECT evaluation_metadata FROM audit_log WHERE call_id = ? AND user_id = ?",
                (call_id, user_id),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError("Audit call not found")
            previous = row["evaluation_metadata"] or ""
            metadata = json.loads(previous) if previous else {}
            metadata[stage] = view
            cur.execute(
                "UPDATE audit_log SET evaluation_metadata = ? WHERE call_id = ? AND user_id = ? "
                "AND COALESCE(evaluation_metadata, '') = ?",
                (json.dumps(metadata), call_id, user_id, previous),
            )
            if cur.rowcount != 1:
                raise HybridError(
                    "stale_model_view",
                    "Audit metadata changed before model dispatch",
                    409,
                )

    def set_judge_reasoning(
        self,
        call_id: str,
        judge_reasoning: str,
        *,
        user_id: str,
        judge_decision: str | None = None,
        judge_risk: str | None = None,
        outcome_override: str | None = None,
    ) -> None:
        """Store judge reasoning on an unresolved escalation.

        Used in advisory mode when the judge defers to a human. The
        reasoning is stored so the human can see the judge's analysis
        when reviewing the escalation.

        Idempotent: multiple calls overwrite the previous reasoning.
        Only writes to unresolved escalations — if the escalation was
        resolved between the judge's check and this write, the WHERE
        clause prevents stale writes (rowcount=0, silently ignored).

        Args:
            call_id: The escalated call.
            judge_reasoning: Judge's reasoning text.
            user_id: Tenant identifier.
            judge_decision: Historical judge recommendation.
            judge_risk: Historical judge risk assessment.
        """
        with self._db.cursor() as cur:
            cur.execute(
                """
                UPDATE audit_log
                SET judge_reasoning = ?,
                    judge_decision = COALESCE(?, judge_decision),
                    judge_risk = COALESCE(?, judge_risk),
                    outcome_override = COALESCE(?, outcome_override)
                WHERE call_id = ? AND user_id = ?
                  AND decision = 'escalate'
                  AND user_decision IS NULL
                """,
                (
                    judge_reasoning,
                    judge_decision,
                    judge_risk,
                    outcome_override,
                    call_id,
                    user_id,
                ),
            )

    def get_latest_reasoning(
        self,
        session_id: str,
        *,
        user_id: str,
        before_ts: str,
        before_id: str,
    ) -> dict[str, Any] | None:
        """Get the latest reasoning record before a specific audit row."""
        with self._db.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM audit_log
                WHERE session_id = ? AND user_id = ?
                  AND record_type = 'reasoning'
                  AND (
                    timestamp < ?
                    OR (timestamp = ? AND id < ?)
                  )
                ORDER BY timestamp DESC, id DESC
                LIMIT 1
                """,
                (session_id, user_id, before_ts, before_ts, before_id),
            )
            row = cur.fetchone()

        if row is None:
            return None
        return _row_to_dict(row)

    def get_recent_judge_reviews(
        self,
        session_id: str,
        *,
        user_id: str,
        before_ts: str,
        before_id: str,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        """Get recent explicit judge approvals/denials before a review point."""
        with self._db.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM audit_log
                WHERE session_id = ? AND user_id = ?
                  AND record_type = 'tool_call'
                  AND resolved_by = 'judge'
                  AND judge_decision IN ('approve', 'deny')
                  AND (
                    timestamp < ?
                    OR (timestamp = ? AND id < ?)
                  )
                ORDER BY timestamp DESC, id DESC
                LIMIT ?
                """,
                (session_id, user_id, before_ts, before_ts, before_id, limit),
            )
            rows = cur.fetchall()

        return [_row_to_dict(row) for row in rows]

    def iter_analytics_revisions(
        self,
        session_id: str,
        *,
        user_id: str,
        from_ts: str,
        to_ts: str,
        page_size: int = 128,
    ) -> Iterator[list[dict[str, Any]]]:
        """Yield keyset pages of source identities, without payload hydration.

        Explicit LIMIT queries also bound libpq's client-side result allocation;
        fetchmany() on an unbounded ordinary cursor would not do that.
        """
        if not 1 <= page_size <= 128:
            raise ValueError("Analytics page size must be between 1 and 128")
        cursor = None
        while True:
            params: list[Any] = [user_id, session_id, from_ts, to_ts]
            after = ""
            if cursor is not None:
                after = " AND (timestamp > ? OR (timestamp = ? AND call_id > ?))"
                params.extend([cursor[0], cursor[0], cursor[1]])
            with self._db.cursor() as cur:
                cur.execute(
                    "SELECT call_id, timestamp, record_type, resolved_at, resolved_by, "
                    "user_decision, resolution_scope FROM audit_log "
                    "WHERE user_id = ? AND session_id = ? AND timestamp >= ? "
                    "AND timestamp <= ? AND record_type IN ('tool_call', 'reasoning')"
                    + after
                    + " ORDER BY timestamp, call_id LIMIT ?",
                    [*params, page_size],
                )
                rows = [dict(row) for row in cur.fetchall()]
            if not rows:
                return
            yield rows
            if len(rows) < page_size:
                return
            cursor = rows[-1]["timestamp"], rows[-1]["call_id"]

    def get_analytics_payloads(
        self, session_id: str, *, user_id: str, call_ids: list[str]
    ) -> list[dict[str, Any]]:
        """Hydrate one selected page, excluding evaluation snapshots entirely."""
        if not call_ids:
            return []
        if len(call_ids) > 128:
            raise ValueError("Analytics payload pages cannot exceed 128 records")
        placeholders = ", ".join("?" for _ in call_ids)
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT call_id, timestamp, record_type, tool, args_redacted, content, "
                "classification, decision, risk, reasoning, resolved_at, resolved_by, "
                "user_decision, resolution_scope, user_note, raw_decision, "
                "effective_decision, maximum_outcome, outcome_override, judge_decision "
                "FROM audit_log WHERE user_id = ? AND session_id = ? "
                f"AND call_id IN ({placeholders}) ORDER BY timestamp, call_id",
                [user_id, session_id, *call_ids],
            )
            return [_row_to_dict(row) for row in cur.fetchall()]

    def get_window(
        self,
        session_id: str,
        *,
        user_id: str,
        from_ts: str,
        to_ts: str,
        record_types: set[str] | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Get audit records within a time window for a session.

        Optimized for L2 session summary generation — fetches records
        within a specific time range, optionally filtered by record type.

        Args:
            session_id: Session to query.
            user_id: Tenant identifier.
            from_ts: Window start (inclusive) as ISO timestamp.
            to_ts: Window end (inclusive) as ISO timestamp.
            record_types: Optional set of record types to include
                (e.g., {"tool_call", "reasoning"}). None = all types.
            limit: Max records to return.

        Returns:
            List of audit record dicts, ordered by timestamp ASC
            (chronological order for analysis).
        """
        conditions = [
            "session_id = ?",
            "user_id = ?",
            "timestamp >= ?",
            "timestamp <= ?",
        ]
        params: list[Any] = [session_id, user_id, from_ts, to_ts]

        if record_types:
            valid = record_types & self.VALID_RECORD_TYPES
            if not valid:
                return []
            placeholders = ", ".join("?" for _ in valid)
            conditions.append(f"record_type IN ({placeholders})")
            params.extend(sorted(valid))

        where = "WHERE " + " AND ".join(conditions)
        limit_clause = f"LIMIT {limit}" if limit > 0 else ""

        with self._db.cursor() as cur:
            cur.execute(
                f"""
                SELECT * FROM audit_log
                {where}
                ORDER BY timestamp ASC
                {limit_clause}
                """,
                params,
            )
            rows = cur.fetchall()

        return [_row_to_dict(row) for row in rows]

    def get_session_stats(
        self,
        session_id: str,
        *,
        user_id: str,
        from_ts: str | None = None,
        to_ts: str | None = None,
    ) -> dict[str, int]:
        """Get aggregated stats for a session within an optional time window.

        Returns counts of approved, denied, escalated tool calls.

        Args:
            session_id: Session to query.
            user_id: Tenant identifier.
            from_ts: Optional window start (inclusive).
            to_ts: Optional window end (inclusive).

        Returns:
            Dict with approved_count, denied_count, escalated_count, total.
        """
        conditions = [
            "session_id = ?",
            "user_id = ?",
            "record_type = 'tool_call'",
        ]
        params: list[Any] = [session_id, user_id]

        if from_ts:
            conditions.append("timestamp >= ?")
            params.append(from_ts)
        if to_ts:
            conditions.append("timestamp <= ?")
            params.append(to_ts)

        where = "WHERE " + " AND ".join(conditions)

        with self._db.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN decision = 'approve' THEN 1 ELSE 0 END) as approved,
                    SUM(CASE WHEN decision = 'deny' THEN 1 ELSE 0 END) as denied,
                    SUM(CASE WHEN decision = 'escalate' THEN 1 ELSE 0 END) as escalated
                FROM audit_log
                {where}
                """,
                params,
            )
            row = cur.fetchone()

        if row is None:
            return {
                "approved_count": 0,
                "denied_count": 0,
                "escalated_count": 0,
                "total": 0,
            }
        return {
            "approved_count": row["approved"] or 0,
            "denied_count": row["denied"] or 0,
            "escalated_count": row["escalated"] or 0,
            "total": row["total"] or 0,
        }


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Convert a sqlite3.Row to a plain dict, parsing JSON fields."""
    d = dict(row)
    if d.get("args_redacted"):
        try:
            d["args_redacted"] = json.loads(d["args_redacted"])
        except (json.JSONDecodeError, TypeError):
            pass
    if d.get("evaluation_metadata"):
        d["evaluation_metadata"] = json.loads(d["evaluation_metadata"])
    return d
