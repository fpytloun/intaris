"""Token-based action endpoints for one-click approve/deny.

These endpoints are UNAUTHENTICATED — the Fernet token itself is the
authentication. The auth middleware skips /api/v1/action/ paths.

Security measures:
- Fernet-encrypted tokens with TTL (default 60 minutes)
- Confirmation page prevents accidental execution (GET shows, POST acts)
- CSRF token embedded in the action token, validated on POST
- IP-based rate limiting (10 requests/minute per IP)
- Minimal information on confirmation page (tool + call_id only)
- Resolution idempotency enforced by audit store's atomic WHERE clause
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from datetime import datetime
from datetime import timezone as tz
from html import escape

from starlette.requests import Request
from starlette.responses import HTMLResponse

logger = logging.getLogger(__name__)


# ── IP Rate Limiter ───────────────────────────────────────────────

_IP_RATE_LIMIT = 10  # requests per minute per IP
_IP_RATE_WINDOW = 60  # seconds
_ip_calls: dict[str, deque[float]] = {}
_ip_lock = threading.Lock()
_ip_last_sweep = time.monotonic()


def _check_ip_rate_limit(ip: str) -> bool:
    """Check if an IP is within the rate limit.

    Returns True if allowed, False if rate limit exceeded.
    """
    global _ip_last_sweep
    now = time.monotonic()
    cutoff = now - _IP_RATE_WINDOW

    with _ip_lock:
        # Periodic sweep
        if now - _ip_last_sweep > 300:
            _sweep_ip_entries(cutoff)
            _ip_last_sweep = now

        timestamps = _ip_calls.get(ip)
        if timestamps is None:
            timestamps = deque()
            _ip_calls[ip] = timestamps

        # Prune expired
        while timestamps and timestamps[0] < cutoff:
            timestamps.popleft()

        if len(timestamps) >= _IP_RATE_LIMIT:
            return False

        timestamps.append(now)
        return True


def _sweep_ip_entries(cutoff: float) -> None:
    """Remove entries for IPs with no recent calls."""
    empty = [ip for ip, ts in _ip_calls.items() if not ts or ts[-1] < cutoff]
    for ip in empty:
        del _ip_calls[ip]


# ── Confirmation Page HTML ────────────────────────────────────────

_CONFIRMATION_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Intaris — Confirm Action</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #0f172a; color: #e2e8f0;
    display: flex; justify-content: center; align-items: center;
    min-height: 100vh; padding: 1rem;
  }}
  .card {{
    background: #1e293b; border: 1px solid #334155;
    border-radius: 12px; padding: 2rem; max-width: 420px; width: 100%;
  }}
  h1 {{ font-size: 1.25rem; margin-bottom: 1.5rem; color: #f1f5f9; }}
  .field {{ margin-bottom: 1rem; }}
  .label {{ font-size: 0.75rem; color: #94a3b8; text-transform: uppercase;
            letter-spacing: 0.05em; margin-bottom: 0.25rem; }}
  .value {{ font-family: monospace; font-size: 0.875rem; color: #cbd5e1;
            word-break: break-all; }}
  .actions {{ display: flex; gap: 0.75rem; margin-top: 1.5rem; }}
  .btn {{
    flex: 1; padding: 0.75rem 1rem; border: none; border-radius: 8px;
    font-size: 0.875rem; font-weight: 600; cursor: pointer;
    text-decoration: none; text-align: center; display: inline-block;
  }}
  .btn-approve {{ background: #059669; color: white; }}
  .btn-approve:hover {{ background: #047857; }}
  .btn-deny {{ background: #dc2626; color: white; }}
  .btn-deny:hover {{ background: #b91c1c; }}
  .btn-cancel {{ background: #334155; color: #94a3b8; }}
  .btn-cancel:hover {{ background: #475569; }}
  .note {{ font-size: 0.75rem; color: #64748b; margin-top: 1rem;
           text-align: center; }}
</style>
</head>
<body>
<div class="card">
  <h1>{title}</h1>
  <div class="field">
    <div class="label">Tool</div>
    <div class="value">{tool}</div>
  </div>
  <div class="field">
    <div class="label">Call ID</div>
    <div class="value">{call_id}</div>
  </div>
  <form method="POST" action="{post_url}">
    <input type="hidden" name="csrf_token" value="{csrf_token}">
    <div class="actions">
      <button type="submit" class="btn {btn_class}">{btn_text}</button>
      <a href="{ui_url}" class="btn btn-cancel">Cancel</a>
    </div>
  </form>
  <div class="note">This link expires in {ttl_minutes} minutes.</div>
</div>
</body>
</html>"""

_RESULT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Intaris — {title}</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #0f172a; color: #e2e8f0;
    display: flex; justify-content: center; align-items: center;
    min-height: 100vh; padding: 1rem;
  }}
  .card {{
    background: #1e293b; border: 1px solid #334155;
    border-radius: 12px; padding: 2rem; max-width: 420px; width: 100%;
    text-align: center;
  }}
  h1 {{ font-size: 1.25rem; margin-bottom: 0.75rem; color: {color}; }}
  p {{ font-size: 0.875rem; color: #94a3b8; margin-bottom: 1.5rem; }}
  a {{
    display: inline-block; padding: 0.75rem 1.5rem; background: #334155;
    color: #e2e8f0; border-radius: 8px; text-decoration: none;
    font-size: 0.875rem;
  }}
  a:hover {{ background: #475569; }}
</style>
</head>
<body>
<div class="card">
  <h1>{title}</h1>
  <p>{message}</p>
  <a href="{ui_url}">Open Intaris</a>
</div>
</body>
</html>"""


# ── Endpoints ─────────────────────────────────────────────────────


async def action_get(request: Request) -> HTMLResponse:
    """Show confirmation page for an action token.

    GET /api/v1/action/{token}
    No authentication required — the token is the auth.
    """
    token = request.path_params.get("token", "")
    client_ip = _get_client_ip(request)

    if not _check_ip_rate_limit(client_ip):
        return HTMLResponse(
            content="<h1>Rate limit exceeded</h1>"
            "<p>Too many requests. Try again later.</p>",
            status_code=429,
        )

    from intaris.notifications.tokens import verify_action_token
    from intaris.server import _get_config

    cfg = _get_config()
    encryption_key = cfg.mcp.encryption_key
    ttl_seconds = cfg.notification.action_ttl_minutes * 60
    base_url = cfg.webhook.base_url or ""
    ui_url = f"{base_url}/ui/" if base_url else "/ui/"

    if not encryption_key:
        return HTMLResponse(
            content="<h1>Not configured</h1><p>Action tokens are not available.</p>",
            status_code=503,
        )

    try:
        payload = verify_action_token(
            token, encryption_key=encryption_key, ttl_seconds=ttl_seconds
        )
    except ValueError as e:
        return HTMLResponse(
            content=_RESULT_HTML.format(
                title="Invalid or Expired Link",
                color="#dc2626",
                message=escape(str(e)),
                ui_url=escape(ui_url),
            ),
            status_code=400,
        )

    # Look up the audit record to get tool name
    from intaris.audit import AuditStore
    from intaris.server import _get_db

    tool = "unknown"
    try:
        store = AuditStore(_get_db())
        record = store.get_by_call_id(payload.call_id, user_id=payload.user_id)
        tool = record.get("tool") or "unknown"
        # Check if already resolved
        if record.get("user_decision"):
            return HTMLResponse(
                content=_RESULT_HTML.format(
                    title="Already Resolved",
                    color="#94a3b8",
                    message=f"This escalation was already "
                    f"{escape(record['user_decision'])}d.",
                    ui_url=escape(ui_url),
                ),
                status_code=200,
            )
    except ValueError:
        pass  # Record not found — show generic page

    is_approve = payload.action == "approve"
    html = _CONFIRMATION_HTML.format(
        title=f"Confirm {'Approve' if is_approve else 'Deny'}",
        tool=escape(tool),
        call_id=escape(payload.call_id[:16] + "..."),
        post_url=f"/api/v1/action/{escape(token)}",
        csrf_token=escape(payload.csrf_token),
        btn_class="btn-approve" if is_approve else "btn-deny",
        btn_text=f"Confirm {'Approve' if is_approve else 'Deny'}",
        ui_url=escape(ui_url),
        ttl_minutes=cfg.notification.action_ttl_minutes,
    )

    return HTMLResponse(content=html, status_code=200)


async def action_post(request: Request) -> HTMLResponse:
    """Execute an action from the confirmation page.

    POST /api/v1/action/{token}
    No authentication required — the token + CSRF token are the auth.
    """
    token = request.path_params.get("token", "")
    client_ip = _get_client_ip(request)

    if not _check_ip_rate_limit(client_ip):
        return HTMLResponse(
            content="<h1>Rate limit exceeded</h1>"
            "<p>Too many requests. Try again later.</p>",
            status_code=429,
        )

    from intaris.notifications.tokens import verify_action_token
    from intaris.server import _get_config

    cfg = _get_config()
    encryption_key = cfg.mcp.encryption_key
    ttl_seconds = cfg.notification.action_ttl_minutes * 60
    base_url = cfg.webhook.base_url or ""
    ui_url = f"{base_url}/ui/" if base_url else "/ui/"

    if not encryption_key:
        return HTMLResponse(
            content="<h1>Not configured</h1>",
            status_code=503,
        )

    try:
        payload = verify_action_token(
            token, encryption_key=encryption_key, ttl_seconds=ttl_seconds
        )
    except ValueError as e:
        return HTMLResponse(
            content=_RESULT_HTML.format(
                title="Invalid or Expired Link",
                color="#dc2626",
                message=escape(str(e)),
                ui_url=escape(ui_url),
            ),
            status_code=400,
        )

    # Validate CSRF token from form data
    try:
        form = await request.form()
        submitted_csrf = form.get("csrf_token", "")
    except Exception:
        submitted_csrf = ""

    if not submitted_csrf or submitted_csrf != payload.csrf_token:
        return HTMLResponse(
            content=_RESULT_HTML.format(
                title="Invalid Request",
                color="#dc2626",
                message="CSRF validation failed. Please use the confirmation page.",
                ui_url=escape(ui_url),
            ),
            status_code=403,
        )

    # Execute the action using the token's user_id (not any header value).
    # NOTE: resolve_escalation() also accepts decision='deny' records
    # (for denial overrides), but notification action tokens are only
    # generated for escalations — so this path only handles escalations.
    # This calls resolve_escalation() directly rather than
    # resolve_with_side_effects() — EventBus publish, path learning,
    # and notification dispatch are intentionally skipped for action links.
    from intaris.audit import AuditStore
    from intaris.server import _get_db

    try:
        store = AuditStore(_get_db())
        store.resolve_escalation(
            call_id=payload.call_id,
            user_decision=payload.action,
            user_note="Resolved via notification action link",
            user_id=payload.user_id,
        )
    except ValueError as e:
        return HTMLResponse(
            content=_RESULT_HTML.format(
                title="Action Failed",
                color="#f59e0b",
                message=escape(str(e)),
                ui_url=escape(ui_url),
            ),
            status_code=400,
        )
    except Exception:
        logger.exception("Error executing action for call_id=%s", payload.call_id)
        return HTMLResponse(
            content=_RESULT_HTML.format(
                title="Error",
                color="#dc2626",
                message="An internal error occurred. Please try again.",
                ui_url=escape(ui_url),
            ),
            status_code=500,
        )

    # Fetch the audit record once for event publishing and notifications
    record = None
    try:
        record = store.get_by_call_id(payload.call_id, user_id=payload.user_id)
    except Exception:
        pass  # Best-effort — don't block the response

    # Publish event to EventBus (best-effort)
    _publish_decided_event(request, record, payload)

    # Send resolution notification (best-effort, fire-and-forget)
    _send_resolution_notification(request, record, payload, ui_url)

    is_approve = payload.action == "approve"
    return HTMLResponse(
        content=_RESULT_HTML.format(
            title="Approved" if is_approve else "Denied",
            color="#059669" if is_approve else "#dc2626",
            message=f"The escalation has been {payload.action}d successfully.",
            ui_url=escape(ui_url),
        ),
        status_code=200,
    )


def _publish_decided_event(request: Request, record: dict | None, payload) -> None:
    """Publish a 'decided' event to the EventBus."""
    try:
        event_bus = getattr(request.app.state, "event_bus", None)
        if event_bus is not None and record is not None:
            event_bus.publish(
                {
                    "type": "decided",
                    "call_id": payload.call_id,
                    "session_id": record.get("session_id"),
                    "user_id": payload.user_id,
                    "user_decision": payload.action,
                    "user_note": "Resolved via notification action link",
                }
            )
    except Exception:
        pass  # Best-effort


def _send_resolution_notification(
    request: Request, record: dict | None, payload, ui_url: str
) -> None:
    """Send resolution notification to user's channels."""
    try:
        dispatcher = getattr(request.app.state, "notification_dispatcher", None)
        if dispatcher is not None and record is not None:
            from intaris.notifications.providers import Notification

            notification = Notification(
                event_type="resolution",
                call_id=payload.call_id,
                session_id=record.get("session_id", ""),
                user_id=payload.user_id,
                agent_id=record.get("agent_id"),
                tool=record.get("tool"),
                args_redacted=None,
                risk=record.get("risk"),
                reasoning=record.get("reasoning"),
                ui_url=ui_url,
                approve_url=None,
                deny_url=None,
                timestamp=datetime.now(tz.utc).isoformat(),
                user_decision=payload.action,
                user_note="Resolved via notification action link",
            )
            asyncio.create_task(
                dispatcher.notify(
                    user_id=payload.user_id,
                    notification=notification,
                )
            )
    except Exception:
        pass  # Best-effort


def _get_client_ip(request: Request) -> str:
    """Extract client IP from request, respecting X-Forwarded-For.

    Note: X-Forwarded-For is trusted as-is. When deployed behind a reverse
    proxy, ensure the proxy strips/overwrites this header to prevent
    spoofing. The IP rate limiter is defense-in-depth — Fernet tokens
    are the primary security mechanism on action endpoints.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    client = request.client
    return client.host if client else "unknown"
