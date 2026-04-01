"""HTTP client for the Intaris REST API.

Provides typed wrappers around urllib.request with retry logic, exponential
backoff, and proper error handling.  Uses only Python stdlib — no external
HTTP library dependencies.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)


class ApiResult:
    """Result of an API call."""

    __slots__ = ("data", "error", "status")

    def __init__(
        self,
        data: dict[str, Any] | None = None,
        error: str | None = None,
        status: int | None = None,
    ):
        self.data = data
        self.error = error
        self.status = status


class IntarisClient:
    """HTTP client for the Intaris REST API."""

    def __init__(
        self,
        url: str,
        api_key: str,
        user_id: str = "",
        agent_id: str = "",
    ):
        self._base_url = url.rstrip("/")
        self._api_key = api_key
        self._user_id = user_id
        self._agent_id = agent_id

    # -- Low-level API -------------------------------------------------------

    def call_api(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | list[Any] | None = None,
        timeout_s: float = 5.0,
        extra_headers: dict[str, str] | None = None,
        agent_id: str | None = None,
    ) -> ApiResult:
        """Make an HTTP request to the Intaris API."""
        url = f"{self._base_url}{path}"
        headers: dict[str, str] = {}
        if extra_headers:
            headers.update(extra_headers)

        aid = agent_id or self._agent_id
        if aid:
            headers["X-Agent-Id"] = aid
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        if self._user_id:
            headers["X-User-Id"] = self._user_id

        body: bytes | None = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode("utf-8")

        req = urllib.request.Request(url, data=body, headers=headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return ApiResult(data=data, status=resp.status)
        except urllib.error.HTTPError as exc:
            raw = ""
            try:
                raw = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            detail = f"HTTP {exc.code}"
            try:
                parsed = json.loads(raw)
                detail = str(parsed.get("detail") or parsed.get("error") or detail)
            except Exception:
                if raw:
                    detail = raw[:200]
            logger.warning("API %s %s returned %s: %s", method, path, exc.code, detail)
            return ApiResult(error=detail, status=exc.code)
        except Exception as exc:
            logger.warning("API %s %s failed: %s", method, path, exc)
            return ApiResult(error=str(exc))

    def call_api_with_retry(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | list[Any] | None = None,
        timeout_s: float = 30.0,
        max_retries: int = 3,
        agent_id: str | None = None,
    ) -> ApiResult:
        """Call API with retries and exponential backoff.

        Retries on network errors and 5xx responses.  Does NOT retry 4xx.
        """
        backoff = [1.0, 2.0, 4.0]
        last: ApiResult = ApiResult(error="no attempts")

        for attempt in range(max_retries + 1):
            last = self.call_api(method, path, payload, timeout_s, agent_id=agent_id)

            # Success
            if last.data is not None:
                return last

            # 4xx client errors — do not retry
            if last.status is not None and 400 <= last.status < 500:
                return last

            # 5xx or network error — retry with backoff (unless last attempt)
            if attempt < max_retries:
                delay = backoff[min(attempt, len(backoff) - 1)]
                logger.warning(
                    "API %s %s failed (attempt %d/%d), retrying in %.0fs",
                    method,
                    path,
                    attempt + 1,
                    max_retries + 1,
                    delay,
                )
                time.sleep(delay)

        return last

    # -- High-level API ------------------------------------------------------

    def create_intention(
        self,
        session_id: str,
        intention: str,
        details: dict[str, Any],
        policy: dict[str, Any] | None = None,
        parent_session_id: str | None = None,
        agent_id: str | None = None,
    ) -> ApiResult:
        body: dict[str, Any] = {
            "session_id": session_id,
            "intention": intention,
            "details": details,
        }
        if policy:
            body["policy"] = policy
        if parent_session_id:
            body["parent_session_id"] = parent_session_id
        return self.call_api_with_retry(
            "POST",
            "/api/v1/intention",
            body,
            timeout_s=5.0,
            max_retries=2,
            agent_id=agent_id,
        )

    def evaluate(
        self,
        session_id: str,
        tool: str,
        args: dict[str, Any],
        intention_pending: bool = False,
        agent_id: str | None = None,
    ) -> ApiResult:
        body: dict[str, Any] = {
            "session_id": session_id,
            "tool": tool,
            "args": args,
        }
        if intention_pending:
            body["intention_pending"] = True
        return self.call_api_with_retry(
            "POST",
            "/api/v1/evaluate",
            body,
            timeout_s=30.0,
            max_retries=2,
            agent_id=agent_id,
        )

    def update_status(
        self, session_id: str, status: str, agent_id: str | None = None
    ) -> ApiResult:
        return self.call_api(
            "PATCH",
            f"/api/v1/session/{urllib.parse.quote(session_id, safe='')}/status",
            {"status": status},
            timeout_s=2.0,
            agent_id=agent_id,
        )

    def update_session(
        self,
        session_id: str,
        intention: str,
        details: dict[str, Any],
        agent_id: str | None = None,
    ) -> ApiResult:
        return self.call_api(
            "PATCH",
            f"/api/v1/session/{urllib.parse.quote(session_id, safe='')}",
            {"intention": intention, "details": details},
            timeout_s=2.0,
            agent_id=agent_id,
        )

    def get_session(self, session_id: str, agent_id: str | None = None) -> ApiResult:
        return self.call_api(
            "GET",
            f"/api/v1/session/{urllib.parse.quote(session_id, safe='')}",
            timeout_s=5.0,
            agent_id=agent_id,
        )

    def get_audit(self, call_id: str, agent_id: str | None = None) -> ApiResult:
        return self.call_api(
            "GET",
            f"/api/v1/audit/{urllib.parse.quote(call_id, safe='')}",
            timeout_s=5.0,
            agent_id=agent_id,
        )

    def submit_agent_summary(
        self, session_id: str, summary: str, agent_id: str | None = None
    ) -> ApiResult:
        return self.call_api(
            "POST",
            f"/api/v1/session/{urllib.parse.quote(session_id, safe='')}/agent-summary",
            {"summary": summary},
            timeout_s=2.0,
            agent_id=agent_id,
        )

    def submit_checkpoint(
        self, session_id: str, content: str, agent_id: str | None = None
    ) -> ApiResult:
        return self.call_api(
            "POST",
            "/api/v1/checkpoint",
            {"session_id": session_id, "content": content},
            timeout_s=2.0,
            agent_id=agent_id,
        )

    def submit_reasoning(
        self,
        session_id: str,
        content: str,
        agent_id: str | None = None,
        context: str | None = None,
        from_events: bool = False,
    ) -> ApiResult:
        body: dict[str, Any] = {"session_id": session_id, "content": content}
        if context:
            body["context"] = context
        if from_events:
            body["from_events"] = True
        return self.call_api(
            "POST", "/api/v1/reasoning", body, timeout_s=2.0, agent_id=agent_id
        )

    def append_events(
        self,
        session_id: str,
        events: list[dict[str, Any]],
        agent_id: str | None = None,
    ) -> ApiResult:
        return self.call_api(
            "POST",
            f"/api/v1/session/{urllib.parse.quote(session_id, safe='')}/events",
            events,
            timeout_s=5.0,
            extra_headers={"X-Intaris-Source": "hermes"},
            agent_id=agent_id,
        )

    # -- MCP API -------------------------------------------------------------

    def list_mcp_tools(self, agent_id: str | None = None) -> list[dict[str, Any]]:
        """Fetch available MCP tools.  Returns empty list on failure."""
        result = self.call_api(
            "GET", "/api/v1/mcp/tools", timeout_s=10.0, agent_id=agent_id
        )
        if not result.data:
            return []
        tools = result.data.get("tools")
        if not isinstance(tools, list):
            logger.warning("MCP tools response missing 'tools' array")
            return []
        return tools

    def call_mcp_tool(
        self,
        session_id: str,
        server: str,
        tool: str,
        args: dict[str, Any],
        agent_id: str | None = None,
    ) -> ApiResult:
        """Proxy a tool call to an MCP server via Intaris."""
        return self.call_api_with_retry(
            "POST",
            "/api/v1/mcp/call",
            {
                "session_id": session_id,
                "server": server,
                "tool": tool,
                "arguments": args,
            },
            timeout_s=60.0,
            max_retries=2,
            agent_id=agent_id,
        )
