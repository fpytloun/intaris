"""FastAPI dependency injection for request identity.

Reads ContextVars set by the APIKeyMiddleware and provides a
SessionContext to all API endpoints.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException


@dataclass
class SessionContext:
    """Resolved identity for the current request.

    Attributes:
        user_id: Tenant identifier (always required for data operations).
        agent_id: Agent identifier (optional, from X-Agent-Id header).
        user_bound: True if user_id was bound from API key mapping
                    (prevents user switching in UI).
    """

    user_id: str
    agent_id: str | None = None
    user_bound: bool = False


def get_session_context() -> SessionContext:
    """Resolve session identity from ContextVars.

    Reads the ContextVars set by APIKeyMiddleware and returns a
    SessionContext. Raises 401 if user_id could not be resolved.

    Usage in endpoints:
        ctx: SessionContext = Depends(get_session_context)
    """
    from intaris.server import _session_agent_id, _session_user_bound, _session_user_id

    user_id = _session_user_id.get()
    if not user_id:
        raise HTTPException(
            status_code=401,
            detail=(
                "User identity required. Provide an API key mapped to a user_id "
                "(INTARIS_API_KEYS) or set the X-User-Id header."
            ),
        )

    return SessionContext(
        user_id=user_id,
        agent_id=_session_agent_id.get(),
        user_bound=_session_user_bound.get(),
    )
