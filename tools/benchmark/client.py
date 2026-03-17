"""Intaris HTTP client for the benchmark system.

Synchronous httpx client wrapping all Intaris API endpoints needed by
the benchmark runner, evaluator, and analysis phases.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class IntarisError(Exception):
    """Raised when an Intaris API call returns a non-2xx status."""

    def __init__(self, status_code: int, detail: str, url: str) -> None:
        self.status_code = status_code
        self.detail = detail
        self.url = url
        super().__init__(f"HTTP {status_code} from {url}: {detail}")


class IntarisClient:
    """Synchronous HTTP client for the Intaris guardrails API.

    Wraps session lifecycle, evaluation, behavioral analysis, audit,
    and event recording endpoints.  All methods return parsed JSON
    dicts or raise ``IntarisError`` on non-2xx responses.

    Usage::

        with IntarisClient(base_url, api_key, user_id, agent_id) as client:
            client.create_session("s1", "Fix CSS bugs")
            result = client.evaluate("s1", "edit", {"filePath": "a.css"})
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        user_id: str,
        agent_id: str,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.user_id = user_id
        self.agent_id = agent_id
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=30.0,
            headers={
                "X-API-Key": api_key,
                "X-User-Id": user_id,
                "X-Agent-Id": agent_id,
                "X-Intaris-Source": "benchmark",
                "Content-Type": "application/json",
            },
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | list[dict[str, Any]] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send an HTTP request and return the parsed JSON body.

        Raises ``IntarisError`` for any non-2xx response.
        """
        # Strip None values from query params
        if params:
            params = {k: v for k, v in params.items() if v is not None}

        url = path  # httpx resolves relative to base_url
        try:
            resp = self._client.request(method, url, json=json, params=params)
        except httpx.TransportError as exc:
            raise IntarisError(0, str(exc), f"{self.base_url}{path}") from exc

        if resp.status_code >= 400:
            # Try to extract structured error detail
            try:
                body = resp.json()
                detail = body.get("detail") or body.get("error") or resp.text
            except Exception:
                detail = resp.text
            raise IntarisError(
                resp.status_code,
                str(detail),
                str(resp.url),
            )

        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def create_session(
        self,
        session_id: str,
        intention: str,
        *,
        details: dict[str, Any] | None = None,
        policy: dict[str, Any] | None = None,
        parent_session_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a session with the declared intention.

        POST /api/v1/intention
        """
        payload: dict[str, Any] = {
            "session_id": session_id,
            "intention": intention,
        }
        if details is not None:
            payload["details"] = details
        if policy is not None:
            payload["policy"] = policy
        if parent_session_id is not None:
            payload["parent_session_id"] = parent_session_id

        logger.debug("Creating session %s: %s", session_id, intention[:80])
        return self._request("POST", "/api/v1/intention", json=payload)

    def get_session(self, session_id: str) -> dict[str, Any]:
        """Get session details.

        GET /api/v1/session/{id}
        """
        return self._request("GET", f"/api/v1/session/{session_id}")

    def update_session(
        self,
        session_id: str,
        *,
        intention: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Update session intention and/or details.

        PATCH /api/v1/session/{id}
        """
        payload: dict[str, Any] = {}
        if intention is not None:
            payload["intention"] = intention
        if details is not None:
            payload["details"] = details

        return self._request("PATCH", f"/api/v1/session/{session_id}", json=payload)

    def update_status(self, session_id: str, status: str) -> dict[str, Any]:
        """Update session status (active, idle, completed, suspended, terminated).

        PATCH /api/v1/session/{id}/status
        """
        return self._request(
            "PATCH",
            f"/api/v1/session/{session_id}/status",
            json={"status": status},
        )

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------

    def evaluate(
        self,
        session_id: str,
        tool: str,
        args: dict[str, Any],
        *,
        context: dict[str, Any] | None = None,
        intention_pending: bool = False,
    ) -> dict[str, Any]:
        """Evaluate a tool call for safety.

        POST /api/v1/evaluate
        """
        payload: dict[str, Any] = {
            "session_id": session_id,
            "tool": tool,
            "args": args,
        }
        if context is not None:
            payload["context"] = context
        if intention_pending:
            payload["intention_pending"] = True

        return self._request("POST", "/api/v1/evaluate", json=payload)

    # ------------------------------------------------------------------
    # Behavioral data (L1)
    # ------------------------------------------------------------------

    def submit_reasoning(
        self,
        session_id: str,
        content: str,
        *,
        context: str | None = None,
    ) -> dict[str, Any]:
        """Submit agent reasoning or user message text.

        POST /api/v1/reasoning
        """
        payload: dict[str, Any] = {
            "session_id": session_id,
            "content": content,
        }
        if context is not None:
            payload["context"] = context

        return self._request("POST", "/api/v1/reasoning", json=payload)

    def submit_checkpoint(
        self,
        session_id: str,
        content: str,
    ) -> dict[str, Any]:
        """Submit agent behavioral checkpoint.

        POST /api/v1/checkpoint
        """
        return self._request(
            "POST",
            "/api/v1/checkpoint",
            json={
                "session_id": session_id,
                "content": content,
            },
        )

    def submit_agent_summary(
        self,
        session_id: str,
        summary: str,
    ) -> dict[str, Any]:
        """Submit agent-reported session summary.

        POST /api/v1/session/{id}/agent-summary
        """
        return self._request(
            "POST",
            f"/api/v1/session/{session_id}/agent-summary",
            json={"summary": summary},
        )

    # ------------------------------------------------------------------
    # Analysis (L2/L3)
    # ------------------------------------------------------------------

    def trigger_summary(self, session_id: str) -> dict[str, Any]:
        """Manually trigger Intaris summary generation for a session.

        POST /api/v1/session/{id}/summary/trigger
        """
        return self._request("POST", f"/api/v1/session/{session_id}/summary/trigger")

    def get_summary(self, session_id: str) -> dict[str, Any]:
        """Get all summaries for a session (Intaris + agent).

        GET /api/v1/session/{id}/summary
        """
        return self._request("GET", f"/api/v1/session/{session_id}/summary")

    def trigger_analysis(self, *, agent_id: str | None = None) -> dict[str, Any]:
        """Trigger cross-session behavioral analysis.

        POST /api/v1/analysis/trigger
        """
        params: dict[str, Any] = {}
        if agent_id is not None:
            params["agent_id"] = agent_id

        return self._request("POST", "/api/v1/analysis/trigger", params=params)

    def get_analysis(self, *, agent_id: str | None = None) -> dict[str, Any]:
        """List behavioral analyses.

        GET /api/v1/analysis
        """
        params: dict[str, Any] = {}
        if agent_id is not None:
            params["agent_id"] = agent_id

        return self._request("GET", "/api/v1/analysis", params=params)

    def get_profile(self, *, agent_id: str | None = None) -> dict[str, Any]:
        """Get the behavioral risk profile.

        GET /api/v1/profile
        """
        params: dict[str, Any] = {}
        if agent_id is not None:
            params["agent_id"] = agent_id

        return self._request("GET", "/api/v1/profile", params=params)

    def get_task_status(self, *, task_type: str | None = None) -> dict[str, Any]:
        """Get task queue status counts.

        GET /api/v1/tasks/status
        """
        params: dict[str, Any] = {}
        if task_type is not None:
            params["task_type"] = task_type

        return self._request("GET", "/api/v1/tasks/status", params=params)

    # ------------------------------------------------------------------
    # Audit / escalation
    # ------------------------------------------------------------------

    def get_audit_record(self, call_id: str) -> dict[str, Any]:
        """Get a single audit record by call_id.

        GET /api/v1/audit/{call_id}
        """
        return self._request("GET", f"/api/v1/audit/{call_id}")

    def query_audit(
        self,
        *,
        session_id: str | None = None,
        **params: Any,
    ) -> dict[str, Any]:
        """Query the audit log with filters and pagination.

        GET /api/v1/audit

        Accepted keyword arguments match the API query parameters:
        ``agent_id``, ``record_type``, ``tool``, ``decision``, ``risk``,
        ``path``, ``resolved``, ``page``, ``limit``.
        """
        query: dict[str, Any] = {}
        if session_id is not None:
            query["session_id"] = session_id
        # Pass through any additional query params
        for key, value in params.items():
            if value is not None:
                query[key] = value

        return self._request("GET", "/api/v1/audit", params=query)

    def resolve_escalation(
        self,
        call_id: str,
        decision: str,
        *,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Resolve an escalated tool call (approve or deny).

        POST /api/v1/decision
        """
        payload: dict[str, Any] = {
            "call_id": call_id,
            "decision": decision,
        }
        if note is not None:
            payload["note"] = note

        logger.debug("Resolving escalation %s -> %s", call_id, decision)
        return self._request("POST", "/api/v1/decision", json=payload)

    # ------------------------------------------------------------------
    # Events (session recording)
    # ------------------------------------------------------------------

    def append_events(
        self,
        session_id: str,
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Append events to session recording.

        POST /api/v1/session/{id}/events

        Each event dict must have ``type`` (str) and ``data`` (dict).
        """
        return self._request(
            "POST",
            f"/api/v1/session/{session_id}/events",
            json=events,
        )

    # ------------------------------------------------------------------
    # Info
    # ------------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """Check server health.

        GET /health
        """
        return self._request("GET", "/health")

    def whoami(self) -> dict[str, Any]:
        """Get authenticated identity info.

        GET /api/v1/whoami
        """
        return self._request("GET", "/api/v1/whoami")

    def stats(self, *, agent_id: str | None = None) -> dict[str, Any]:
        """Get aggregated dashboard statistics.

        GET /api/v1/stats
        """
        params: dict[str, Any] = {}
        if agent_id is not None:
            params["agent_id"] = agent_id

        return self._request("GET", "/api/v1/stats", params=params)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the underlying httpx client."""
        self._client.close()

    def __enter__(self) -> IntarisClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
