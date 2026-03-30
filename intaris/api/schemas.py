"""Pydantic request/response models for the REST API."""

from __future__ import annotations

from typing import Any, Literal  # noqa: TC003

from pydantic import BaseModel, Field, model_validator

# ── Evaluate ──────────────────────────────────────────────────────────


class EvaluateRequest(BaseModel):
    """Request to evaluate a tool call for safety."""

    session_id: str = Field(..., description="Session identifier")
    agent_id: str | None = Field(None, description="Agent identifier")
    tool: str = Field(..., description="Tool name (e.g., 'bash', 'edit')")
    args: dict[str, Any] = Field(default_factory=dict, description="Tool arguments")
    context: dict[str, Any] | None = Field(
        None, description="Optional additional context"
    )
    intention_pending: bool = Field(
        False,
        description=(
            "Deprecated — no longer used. The server now tracks user "
            "message arrival times internally. Kept for backward "
            "compatibility with existing clients."
        ),
    )

    @model_validator(mode="after")
    def _validate_payload_sizes(self) -> EvaluateRequest:
        """Reject oversized args and context dicts to limit injection surface."""
        import json

        if self.args:
            raw_args = json.dumps(self.args, separators=(",", ":"), default=str)
            if len(raw_args) > 65536:
                raise ValueError(
                    f"args dict exceeds 64 KB limit ({len(raw_args)} bytes)"
                )
        if self.context is not None:
            raw_ctx = json.dumps(self.context, separators=(",", ":"), default=str)
            if len(raw_ctx) > 8192:
                raise ValueError(
                    f"context dict exceeds 8 KB limit ({len(raw_ctx)} bytes)"
                )
        return self


class EvaluateResponse(BaseModel):
    """Response from tool call evaluation."""

    call_id: str = Field(..., description="Unique call identifier")
    decision: Literal["approve", "deny", "escalate"] = Field(
        ..., description="Evaluation decision"
    )
    reasoning: str | None = Field(None, description="Decision reasoning")
    risk: str | None = Field(None, description="Risk level")
    path: str = Field(..., description="Evaluation path: fast, critical, or llm")
    latency_ms: int = Field(..., description="Evaluation latency in ms")
    injection_detected: bool = Field(
        False,
        description=(
            "Whether prompt injection patterns were detected in the tool "
            "arguments or session intention during evaluation."
        ),
    )
    session_status: str | None = Field(
        None,
        description=(
            "Session status when evaluation is denied due to session state "
            "(suspended, terminated, completed). Allows clients to "
            "distinguish deny reasons and react accordingly."
        ),
    )
    status_reason: str | None = Field(
        None,
        description="Reason for session suspension/termination (if applicable)",
    )


# ── Intention / Session ───────────────────────────────────────────────


class SessionPolicy(BaseModel):
    """Typed session policy with known keys only.

    Prevents arbitrary JSON keys from being serialized into LLM prompts,
    which could be used for prompt injection attacks.
    """

    allow_tools: list[str] | None = Field(
        None, description="Glob patterns for tools to auto-allow"
    )
    deny_tools: list[str] | None = Field(
        None, description="Glob patterns for tools to auto-deny"
    )
    allow_commands: list[str] | None = Field(
        None, description="Glob patterns for bash commands to auto-allow"
    )
    deny_commands: list[str] | None = Field(
        None, description="Glob patterns for bash commands to auto-deny"
    )
    allow_paths: list[str] | None = Field(
        None, description="Glob patterns for filesystem paths to allow"
    )
    deny_paths: list[str] | None = Field(
        None, description="Glob patterns for filesystem paths to deny"
    )

    model_config = {"extra": "ignore"}


class IntentionRequest(BaseModel):
    """Request to declare a session intention."""

    session_id: str = Field(..., description="Session identifier")
    intention: str = Field(..., max_length=500, description="Declared session purpose")
    details: dict[str, Any] | None = Field(
        None, description="Session details (repo, branch, constraints)"
    )
    policy: SessionPolicy | dict[str, Any] | None = Field(
        None, description="Session policy (custom rules)"
    )
    parent_session_id: str | None = Field(
        None, description="Parent session ID for continuation chains"
    )

    @model_validator(mode="after")
    def _normalize_policy(self) -> IntentionRequest:
        """Validate and normalize policy to a plain dict with known keys.

        Accepts either a SessionPolicy or a raw dict. Unknown keys are
        silently dropped (SessionPolicy has ``extra="ignore"``). The
        result is always stored as a plain dict for downstream
        compatibility with the classifier and evaluator.
        """
        if isinstance(self.policy, dict):
            validated = SessionPolicy(**self.policy)
            self.policy = validated.model_dump(exclude_none=True)
        elif isinstance(self.policy, SessionPolicy):
            self.policy = self.policy.model_dump(exclude_none=True)
        return self


class IntentionResponse(BaseModel):
    """Response from intention declaration."""

    ok: bool = True


class SessionResponse(BaseModel):
    """Session details response."""

    session_id: str
    user_id: str
    agent_id: str | None = None
    intention: str
    details: dict[str, Any] | None = None
    policy: dict[str, Any] | None = None
    total_calls: int = 0
    approved_count: int = 0
    denied_count: int = 0
    escalated_count: int = 0
    status: str = "active"
    status_reason: str | None = None
    last_activity_at: str | None = None
    parent_session_id: str | None = None
    summary_count: int = 0
    last_alignment: str | None = None
    created_at: str
    updated_at: str


# ── Decision ──────────────────────────────────────────────────────────


class DecisionRequest(BaseModel):
    """Request to resolve an escalated tool call."""

    call_id: str = Field(..., description="Call ID to resolve")
    decision: Literal["approve", "deny"] = Field(..., description="User decision")
    note: str | None = Field(None, description="Optional user note")


class DecisionResponse(BaseModel):
    """Response from decision resolution."""

    ok: bool = True


# ── Audit ─────────────────────────────────────────────────────────────


class AuditRecord(BaseModel):
    """Single audit log record.

    Supports multiple record types:
    - tool_call: Standard tool call evaluation (tool, args_redacted, classification set).
    - reasoning: Agent reasoning checkpoint (content set, tool/args null).
    - checkpoint: Periodic agent state checkpoint (content set, tool/args null).
    """

    id: str
    call_id: str
    record_type: str = "tool_call"
    user_id: str
    session_id: str
    agent_id: str | None = None
    timestamp: str
    tool: str | None = None
    args_redacted: dict[str, Any] | None = None
    content: str | None = None
    classification: str | None = None
    evaluation_path: str
    decision: str
    risk: str | None = None
    reasoning: str | None = None
    latency_ms: int
    intention: str | None = None
    injection_detected: bool = False
    user_decision: str | None = None
    user_note: str | None = None
    resolved_at: str | None = None
    resolved_by: str | None = None
    judge_reasoning: str | None = None


class AuditListResponse(BaseModel):
    """Paginated audit log response."""

    items: list[AuditRecord]
    total: int
    page: int
    pages: int


# ── Session List ──────────────────────────────────────────────────────


class SessionListResponse(BaseModel):
    """Paginated session list response."""

    items: list[SessionResponse]
    total: int
    page: int
    pages: int


class StatusUpdateRequest(BaseModel):
    """Request to update session status."""

    status: Literal["active", "idle", "completed", "suspended", "terminated"] = Field(
        ..., description="New session status"
    )


class StatusUpdateResponse(BaseModel):
    """Response from session status update."""

    ok: bool = True


class SessionUpdateRequest(BaseModel):
    """Request to update session fields (intention, details)."""

    intention: str | None = Field(None, max_length=500, description="Updated intention")
    details: dict[str, Any] | None = Field(None, description="Updated session details")


# ── Error ─────────────────────────────────────────────────────────────


class ErrorResponse(BaseModel):
    """Error response."""

    error: str
    detail: str | None = None


# ── Behavioral Analysis ───────────────────────────────────────────────


class ReasoningRequest(BaseModel):
    """Request to submit agent reasoning."""

    session_id: str = Field(..., description="Session identifier")
    content: str = Field(
        "",
        max_length=65536,
        description="Agent reasoning text. Required when from_events is false.",
    )
    context: str | None = Field(
        None,
        max_length=65536,
        description=(
            "Optional conversational context (e.g., assistant's last response) "
            "to help interpret short user replies like 'ok, do it'. "
            "Used for intention generation and stored in the audit log "
            "for judge evaluation context."
        ),
    )
    from_events: bool = Field(
        False,
        description=(
            "Resolve content and context from the session's event store "
            "instead of the request body. When true, reads the latest "
            "user_message as content and the latest assistant_message as "
            "context. Useful when the client already recorded events and "
            "wants to trigger intention update without re-sending data."
        ),
    )

    @model_validator(mode="after")
    def _validate_from_events(self) -> ReasoningRequest:
        if self.from_events and self.content:
            raise ValueError("content must be empty when from_events is true")
        if not self.from_events and not self.content:
            raise ValueError("content is required when from_events is false")
        return self


class ReasoningResponse(BaseModel):
    """Response from reasoning submission."""

    ok: bool = True
    call_id: str = Field(..., description="Audit record call_id")


class CheckpointRequest(BaseModel):
    """Request to submit agent checkpoint."""

    session_id: str = Field(..., description="Session identifier")
    content: str = Field(..., max_length=65536, description="Agent checkpoint text")


class CheckpointResponse(BaseModel):
    """Response from checkpoint submission."""

    ok: bool = True
    call_id: str = Field(..., description="Audit record call_id")


class AgentSummaryRequest(BaseModel):
    """Request to submit agent-reported summary."""

    summary: str = Field(
        ..., max_length=65536, description="Agent's summary of the session"
    )


class AgentSummaryResponse(BaseModel):
    """Response from agent summary submission."""

    ok: bool = True


class SummaryTriggerResponse(BaseModel):
    """Response from manual summary trigger."""

    ok: bool = True
    task_id: str | None = Field(None, description="Enqueued task ID")


class BackfillSummariesResponse(BaseModel):
    """Response from summary backfill trigger."""

    enqueued: int = Field(description="Number of summary tasks enqueued")
    skipped: int = Field(description="Number of sessions skipped (duplicate pending)")


class TaskStatusResponse(BaseModel):
    """Task queue status counts."""

    pending: int = 0
    running: int = 0
    completed: int = 0
    failed: int = 0
    cancelled: int = 0


class SessionSummaryRecord(BaseModel):
    """Intaris-generated session summary."""

    id: str
    session_id: str
    window_start: str
    window_end: str
    trigger: str
    summary_type: str = "window"
    summary: str
    tools_used: list[str] | None = None
    intent_alignment: str
    risk_indicators: list[dict[str, Any]] | None = None
    call_count: int
    approved_count: int = 0
    denied_count: int = 0
    escalated_count: int = 0
    created_at: str


class AgentSummaryRecord(BaseModel):
    """Agent-reported summary."""

    id: str
    session_id: str
    summary: str
    created_at: str


class SessionSummariesResponse(BaseModel):
    """Combined summaries for a session."""

    intaris_summaries: list[SessionSummaryRecord]
    agent_summaries: list[AgentSummaryRecord]


class AnalysisTriggerResponse(BaseModel):
    """Response from manual analysis trigger."""

    ok: bool = True
    task_id: str | None = Field(None, description="Enqueued task ID")


class AnalysisRecord(BaseModel):
    """Cross-session behavioral analysis result."""

    id: str
    agent_id: str | None = None
    analysis_type: str
    sessions_scope: list[str] | None = None
    risk_level: int
    findings: list[dict[str, Any]]
    recommendations: list[dict[str, Any]] | None = None
    created_at: str


class AnalysisListResponse(BaseModel):
    """Paginated analysis list response."""

    items: list[AnalysisRecord]
    total: int
    page: int
    pages: int


class ProfileResponse(BaseModel):
    """Behavioral risk profile for a user+agent."""

    user_id: str
    agent_id: str | None = None
    risk_level: int = 1
    active_alerts: list[dict[str, Any]] | None = None
    context_summary: str | None = None
    profile_version: int = 0
    updated_at: str | None = None


# ── Session Events (Recording) ────────────────────────────────────────


class SessionEvent(BaseModel):
    """A single session event for recording."""

    type: str = Field(
        ...,
        description=(
            "Canonical event type: message, user_message, assistant_message, "
            "tool_call, tool_result, evaluation, delegation, "
            "compaction_summary, part, lifecycle, checkpoint, reasoning, "
            "transcript"
        ),
    )
    data: dict[str, Any] = Field(
        default_factory=dict,
        description="Event payload (client-native for reconstruction)",
        json_schema_extra={"maxProperties": 200},
    )

    @model_validator(mode="after")
    def _validate_data_size(self) -> SessionEvent:
        """Reject oversized event payloads to prevent memory exhaustion."""
        import json

        raw = json.dumps(self.data, separators=(",", ":"))
        if len(raw) > 1_048_576:  # 1 MB
            raise ValueError("Event data payload exceeds 1 MB limit")
        return self


class EventAppendResponse(BaseModel):
    """Response from appending events."""

    ok: bool = True
    count: int = Field(..., description="Number of events appended")
    first_seq: int = Field(..., description="First assigned sequence number")
    last_seq: int = Field(..., description="Last assigned sequence number")


class EventReadResponse(BaseModel):
    """Response from reading events."""

    events: list[dict[str, Any]] = Field(
        default_factory=list, description="Events ordered by seq"
    )
    last_seq: int = Field(0, description="Last sequence number in response")
    has_more: bool = Field(False, description="Whether more events exist")
