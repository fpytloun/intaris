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


class IntentionResponse(BaseModel):
    """Response from intention declaration."""

    ok: bool = True


class SessionResponse(BaseModel):
    """Session details response."""

    session_id: str
    intention: str
    details: dict[str, Any] | None = None
    policy: dict[str, Any] | None = None
    total_calls: int = 0
    approved_count: int = 0
    denied_count: int = 0
    escalated_count: int = 0
    status: str = "active"
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
    """Single audit log record."""

    id: str
    call_id: str
    session_id: str
    agent_id: str | None = None
    timestamp: str
    tool: str
    args_redacted: dict[str, Any]
    classification: str
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


# ── Error ─────────────────────────────────────────────────────────────


class ErrorResponse(BaseModel):
    """Error response."""

    error: str
    detail: str | None = None
