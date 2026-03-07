"""Pydantic request/response models for the REST API."""

from __future__ import annotations

from typing import Any, Literal  # noqa: TC003

from pydantic import BaseModel, Field

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


# ── Intention / Session ───────────────────────────────────────────────


class IntentionRequest(BaseModel):
    """Request to declare a session intention."""

    session_id: str = Field(..., description="Session identifier")
    intention: str = Field(..., description="Declared session purpose")
    details: dict[str, Any] | None = Field(
        None, description="Session details (repo, branch, constraints)"
    )
    policy: dict[str, Any] | None = Field(
        None, description="Session policy (custom rules)"
    )
    parent_session_id: str | None = Field(
        None, description="Parent session ID for continuation chains"
    )


class IntentionResponse(BaseModel):
    """Response from intention declaration."""

    ok: bool = True


class SessionResponse(BaseModel):
    """Session details response."""

    session_id: str
    user_id: str
    intention: str
    details: dict[str, Any] | None = None
    policy: dict[str, Any] | None = None
    total_calls: int = 0
    approved_count: int = 0
    denied_count: int = 0
    escalated_count: int = 0
    status: str = "active"
    last_activity_at: str | None = None
    parent_session_id: str | None = None
    summary_count: int = 0
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
    user_decision: str | None = None
    user_note: str | None = None
    resolved_at: str | None = None


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


# ── Error ─────────────────────────────────────────────────────────────


class ErrorResponse(BaseModel):
    """Error response."""

    error: str
    detail: str | None = None


# ── Behavioral Analysis ───────────────────────────────────────────────


class ReasoningRequest(BaseModel):
    """Request to submit agent reasoning."""

    session_id: str = Field(..., description="Session identifier")
    content: str = Field(..., max_length=65536, description="Agent reasoning text")


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


class SessionSummaryRecord(BaseModel):
    """Intaris-generated session summary."""

    id: str
    session_id: str
    window_start: str
    window_end: str
    trigger: str
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
    analysis_type: str
    sessions_scope: list[str] | None = None
    risk_level: str
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
    """Behavioral risk profile for a user."""

    user_id: str
    risk_level: str = "low"
    active_alerts: list[dict[str, Any]] | None = None
    context_summary: str | None = None
    profile_version: int = 0
    updated_at: str | None = None
