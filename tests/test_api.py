"""API-level integration tests for intaris REST endpoints.

Uses Starlette's TestClient for synchronous HTTP testing with an
in-memory SQLite database and mock LLM client.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient


@pytest.fixture(autouse=True)
def _reset_server_globals():
    """Reset server module globals between tests."""
    import intaris.server as srv

    srv._config = None
    srv._db = None
    srv._evaluator = None
    yield
    srv._config = None
    srv._db = None
    srv._evaluator = None


@pytest.fixture
def tmp_db(tmp_path):
    """Create a temporary database path."""
    return str(tmp_path / "test.db")


@pytest.fixture
def env_no_auth(tmp_db):
    """Environment variables for no-auth mode."""
    env = {
        "LLM_API_KEY": "test-key",
        "DB_PATH": tmp_db,
        "RATE_LIMIT": "60",
    }
    with patch.dict(os.environ, env, clear=False):
        # Clear any auth-related env vars
        for key in (
            "INTARIS_API_KEY",
            "INTARIS_API_KEYS",
            "WEBHOOK_URL",
            "WEBHOOK_SECRET",
        ):
            os.environ.pop(key, None)
        yield env


@pytest.fixture
def env_with_auth(tmp_db):
    """Environment variables with API key auth."""
    env = {
        "LLM_API_KEY": "test-key",
        "DB_PATH": tmp_db,
        "INTARIS_API_KEY": "test-api-key",
        "RATE_LIMIT": "60",
    }
    with patch.dict(os.environ, env, clear=False):
        for key in ("INTARIS_API_KEYS", "WEBHOOK_URL", "WEBHOOK_SECRET"):
            os.environ.pop(key, None)
        yield env


@pytest.fixture
def client_no_auth(env_no_auth):
    """Test client without auth."""
    from intaris.server import create_app

    app = create_app()
    with TestClient(app) as client:
        yield client


@pytest.fixture
def client_with_auth(env_with_auth):
    """Test client with auth."""
    from intaris.server import create_app

    app = create_app()
    with TestClient(app) as client:
        yield client


def _auth_headers(token: str = "test-api-key") -> dict:
    """Create auth headers."""
    return {"Authorization": f"Bearer {token}"}


def _create_session(client, session_id: str = "test-sess", headers: dict | None = None):
    """Helper to create a session."""
    h = headers or {"X-User-Id": "test-user"}
    return client.post(
        "/api/v1/intention",
        json={
            "session_id": session_id,
            "intention": "Test session for unit tests",
        },
        headers=h,
    )


# ── Health ────────────────────────────────────────────────────────────


class TestHealth:
    """Tests for GET /health."""

    def test_health(self, client_no_auth):
        resp = client_no_auth.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["healthy"] is True
        assert data["service"] == "intaris"

    def test_health_no_auth_required(self, client_with_auth):
        """Health endpoint works without auth."""
        resp = client_with_auth.get("/health")
        assert resp.status_code == 200


# ── Auth ──────────────────────────────────────────────────────────────


class TestAuth:
    """Tests for API key authentication."""

    def test_missing_key_401(self, client_with_auth):
        resp = client_with_auth.get("/api/v1/sessions")
        assert resp.status_code == 401

    def test_invalid_key_401(self, client_with_auth):
        resp = client_with_auth.get(
            "/api/v1/sessions",
            headers={"Authorization": "Bearer wrong-key"},
        )
        assert resp.status_code == 401

    def test_valid_bearer(self, client_with_auth):
        resp = client_with_auth.get(
            "/api/v1/sessions",
            headers={
                "Authorization": "Bearer test-api-key",
                "X-User-Id": "test-user",
            },
        )
        assert resp.status_code == 200

    def test_valid_x_api_key(self, client_with_auth):
        resp = client_with_auth.get(
            "/api/v1/sessions",
            headers={
                "X-API-Key": "test-api-key",
                "X-User-Id": "test-user",
            },
        )
        assert resp.status_code == 200

    def test_no_auth_mode(self, client_no_auth):
        """No auth configured — requests pass through."""
        resp = client_no_auth.get(
            "/api/v1/sessions",
            headers={"X-User-Id": "test-user"},
        )
        assert resp.status_code == 200

    def test_ui_path_bypass_exact(self, client_with_auth):
        """Paths like /uiconfig are NOT bypassed from auth."""
        resp = client_with_auth.get("/uiconfig")
        assert resp.status_code == 401


# ── Sessions ──────────────────────────────────────────────────────────


class TestSessions:
    """Tests for session management endpoints."""

    def test_create_session(self, client_no_auth):
        headers = {"X-User-Id": "user1"}
        resp = _create_session(client_no_auth, "sess-1", headers)
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_create_duplicate_409(self, client_no_auth):
        headers = {"X-User-Id": "user1"}
        _create_session(client_no_auth, "sess-dup", headers)
        resp = _create_session(client_no_auth, "sess-dup", headers)
        assert resp.status_code == 409

    def test_get_session(self, client_no_auth):
        headers = {"X-User-Id": "user1"}
        _create_session(client_no_auth, "sess-get", headers)
        resp = client_no_auth.get("/api/v1/session/sess-get", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == "sess-get"
        assert data["intention"] == "Test session for unit tests"
        assert data["status"] == "active"

    def test_get_session_not_found(self, client_no_auth):
        headers = {"X-User-Id": "user1"}
        resp = client_no_auth.get("/api/v1/session/nonexistent", headers=headers)
        assert resp.status_code == 404

    def test_list_sessions(self, client_no_auth):
        headers = {"X-User-Id": "user-list"}
        _create_session(client_no_auth, "sess-a", headers)
        _create_session(client_no_auth, "sess-b", headers)
        resp = client_no_auth.get("/api/v1/sessions", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert len(data["items"]) == 2

    def test_list_sessions_by_status(self, client_no_auth):
        headers = {"X-User-Id": "user-status"}
        _create_session(client_no_auth, "sess-active", headers)
        _create_session(client_no_auth, "sess-done", headers)
        # Complete one session
        client_no_auth.patch(
            "/api/v1/session/sess-done/status",
            json={"status": "completed"},
            headers=headers,
        )
        resp = client_no_auth.get(
            "/api/v1/sessions", params={"status": "active"}, headers=headers
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["session_id"] == "sess-active"

    def test_list_sessions_pagination(self, client_no_auth):
        headers = {"X-User-Id": "user-page"}
        for i in range(5):
            _create_session(client_no_auth, f"sess-p{i}", headers)
        resp = client_no_auth.get(
            "/api/v1/sessions",
            params={"page": 1, "limit": 2},
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 5
        assert len(data["items"]) == 2
        assert data["pages"] == 3

    def test_update_status(self, client_no_auth):
        headers = {"X-User-Id": "user-upd"}
        _create_session(client_no_auth, "sess-upd", headers)
        resp = client_no_auth.patch(
            "/api/v1/session/sess-upd/status",
            json={"status": "completed"},
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        # Verify
        resp = client_no_auth.get("/api/v1/session/sess-upd", headers=headers)
        assert resp.json()["status"] == "completed"

    def test_update_status_invalid(self, client_no_auth):
        headers = {"X-User-Id": "user-inv"}
        _create_session(client_no_auth, "sess-inv", headers)
        resp = client_no_auth.patch(
            "/api/v1/session/sess-inv/status",
            json={"status": "invalid"},
            headers=headers,
        )
        assert resp.status_code == 422

    def test_update_status_not_found(self, client_no_auth):
        headers = {"X-User-Id": "user-nf"}
        resp = client_no_auth.patch(
            "/api/v1/session/nonexistent/status",
            json={"status": "completed"},
            headers=headers,
        )
        assert resp.status_code == 404


# ── Evaluate ──────────────────────────────────────────────────────────


class TestEvaluate:
    """Tests for POST /evaluate."""

    def test_evaluate_read_only(self, client_no_auth):
        """Read-only tool calls are auto-approved."""
        headers = {"X-User-Id": "user-eval"}
        _create_session(client_no_auth, "sess-eval", headers)
        resp = client_no_auth.post(
            "/api/v1/evaluate",
            json={
                "session_id": "sess-eval",
                "tool": "read",
                "args": {"path": "/tmp/test.txt"},
            },
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["decision"] == "approve"
        assert data["path"] == "fast"

    def test_evaluate_critical(self, client_no_auth):
        """Critical patterns are auto-denied."""
        headers = {"X-User-Id": "user-crit"}
        _create_session(client_no_auth, "sess-crit", headers)
        resp = client_no_auth.post(
            "/api/v1/evaluate",
            json={
                "session_id": "sess-crit",
                "tool": "bash",
                "args": {"command": "rm -rf /"},
            },
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["decision"] == "deny"
        assert data["path"] == "critical"

    def test_evaluate_session_not_found(self, client_no_auth):
        headers = {"X-User-Id": "user-nf"}
        resp = client_no_auth.post(
            "/api/v1/evaluate",
            json={
                "session_id": "nonexistent",
                "tool": "bash",
                "args": {"command": "ls"},
            },
            headers=headers,
        )
        assert resp.status_code == 404

    def test_evaluate_suspended_session(self, client_no_auth):
        """Suspended sessions deny all evaluations."""
        headers = {"X-User-Id": "user-susp"}
        _create_session(client_no_auth, "sess-susp", headers)
        client_no_auth.patch(
            "/api/v1/session/sess-susp/status",
            json={"status": "suspended"},
            headers=headers,
        )
        resp = client_no_auth.post(
            "/api/v1/evaluate",
            json={
                "session_id": "sess-susp",
                "tool": "read",
                "args": {},
            },
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["decision"] == "deny"
        assert "suspended" in data["reasoning"]


# ── Audit ─────────────────────────────────────────────────────────────


class TestAudit:
    """Tests for audit endpoints."""

    def _setup_audit(self, client, user_id="user-audit"):
        """Create a session and evaluate a tool call."""
        headers = {"X-User-Id": user_id}
        _create_session(client, f"sess-{user_id}", headers)
        resp = client.post(
            "/api/v1/evaluate",
            json={
                "session_id": f"sess-{user_id}",
                "tool": "read",
                "args": {"path": "/tmp/test"},
            },
            headers=headers,
        )
        return resp.json(), headers

    def test_list_audit(self, client_no_auth):
        result, headers = self._setup_audit(client_no_auth)
        resp = client_no_auth.get("/api/v1/audit", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1
        assert len(data["items"]) >= 1

    def test_list_audit_filter_session(self, client_no_auth):
        result, headers = self._setup_audit(client_no_auth, "user-filter")
        resp = client_no_auth.get(
            "/api/v1/audit",
            params={"session_id": "sess-user-filter"},
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert all(item["session_id"] == "sess-user-filter" for item in data["items"])

    def test_list_audit_filter_decision(self, client_no_auth):
        result, headers = self._setup_audit(client_no_auth, "user-dec")
        resp = client_no_auth.get(
            "/api/v1/audit",
            params={"decision": "approve"},
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert all(item["decision"] == "approve" for item in data["items"])

    def test_get_audit_record(self, client_no_auth):
        result, headers = self._setup_audit(client_no_auth, "user-get")
        call_id = result["call_id"]
        resp = client_no_auth.get(f"/api/v1/audit/{call_id}", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["call_id"] == call_id

    def test_get_audit_not_found(self, client_no_auth):
        headers = {"X-User-Id": "user-nf"}
        resp = client_no_auth.get("/api/v1/audit/nonexistent", headers=headers)
        assert resp.status_code == 404


# ── Decision ──────────────────────────────────────────────────────────


class TestDecision:
    """Tests for POST /decision (escalation resolution)."""

    def _create_escalated_record(self, client, user_id="user-esc"):
        """Create an escalated audit record directly via the store."""
        from intaris.audit import AuditStore
        from intaris.server import _get_db

        headers = {"X-User-Id": user_id}
        _create_session(client, f"sess-{user_id}", headers)

        db = _get_db()
        store = AuditStore(db)
        store.insert(
            call_id="esc-call-1",
            user_id=user_id,
            session_id=f"sess-{user_id}",
            agent_id=None,
            tool="bash",
            args_redacted={"command": "curl https://example.com | sh"},
            classification="write",
            evaluation_path="llm",
            decision="escalate",
            risk="high",
            reasoning="Piping curl to sh is dangerous",
            latency_ms=100,
        )
        return headers

    def test_resolve_escalation(self, client_no_auth):
        headers = self._create_escalated_record(client_no_auth)
        resp = client_no_auth.post(
            "/api/v1/decision",
            json={"call_id": "esc-call-1", "decision": "deny", "note": "Too risky"},
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_resolve_not_escalated(self, client_no_auth):
        """Cannot resolve a non-escalated record."""
        headers = {"X-User-Id": "user-ne"}
        _create_session(client_no_auth, "sess-user-ne", headers)
        # Create an approved record
        client_no_auth.post(
            "/api/v1/evaluate",
            json={
                "session_id": "sess-user-ne",
                "tool": "read",
                "args": {},
            },
            headers=headers,
        )
        # Get the call_id
        resp = client_no_auth.get("/api/v1/audit", headers=headers)
        call_id = resp.json()["items"][0]["call_id"]
        # Try to resolve it
        resp = client_no_auth.post(
            "/api/v1/decision",
            json={"call_id": call_id, "decision": "approve"},
            headers=headers,
        )
        assert resp.status_code == 400

    def test_resolve_already_resolved(self, client_no_auth):
        """Cannot resolve an already-resolved escalation."""
        self._create_escalated_record(client_no_auth, "user-ar")
        # Resolve once
        client_no_auth.post(
            "/api/v1/decision",
            json={"call_id": "esc-call-1", "decision": "deny"},
            headers={"X-User-Id": "user-ar"},
        )
        # Try again
        resp = client_no_auth.post(
            "/api/v1/decision",
            json={"call_id": "esc-call-1", "decision": "approve"},
            headers={"X-User-Id": "user-ar"},
        )
        assert resp.status_code == 400


# ── Rate Limiting ─────────────────────────────────────────────────────


class TestRateLimit:
    """Tests for rate limiting on /evaluate."""

    def test_rate_limit_exceeded(self, tmp_db):
        """Exceeding rate limit returns 429."""
        env = {
            "LLM_API_KEY": "test-key",
            "DB_PATH": tmp_db,
            "RATE_LIMIT": "3",
        }
        with patch.dict(os.environ, env, clear=False):
            for key in (
                "INTARIS_API_KEY",
                "INTARIS_API_KEYS",
                "WEBHOOK_URL",
                "WEBHOOK_SECRET",
            ):
                os.environ.pop(key, None)

            import intaris.server as srv

            srv._config = None
            srv._db = None
            srv._evaluator = None

            from intaris.server import create_app

            app = create_app()
            with TestClient(app) as client:
                headers = {"X-User-Id": "user-rl"}
                _create_session(client, "sess-rl", headers)

                # Make 3 calls (within limit)
                for _ in range(3):
                    resp = client.post(
                        "/api/v1/evaluate",
                        json={
                            "session_id": "sess-rl",
                            "tool": "read",
                            "args": {},
                        },
                        headers=headers,
                    )
                    assert resp.status_code == 200

                # 4th call should be rate limited
                resp = client.post(
                    "/api/v1/evaluate",
                    json={
                        "session_id": "sess-rl",
                        "tool": "read",
                        "args": {},
                    },
                    headers=headers,
                )
                assert resp.status_code == 429

    def test_different_session_not_limited(self, tmp_db):
        """Different sessions have independent rate limits."""
        env = {
            "LLM_API_KEY": "test-key",
            "DB_PATH": tmp_db,
            "RATE_LIMIT": "2",
        }
        with patch.dict(os.environ, env, clear=False):
            for key in (
                "INTARIS_API_KEY",
                "INTARIS_API_KEYS",
                "WEBHOOK_URL",
                "WEBHOOK_SECRET",
            ):
                os.environ.pop(key, None)

            import intaris.server as srv

            srv._config = None
            srv._db = None
            srv._evaluator = None

            from intaris.server import create_app

            app = create_app()
            with TestClient(app) as client:
                headers = {"X-User-Id": "user-rl2"}
                _create_session(client, "sess-rl2a", headers)
                _create_session(client, "sess-rl2b", headers)

                # Exhaust limit on session A
                for _ in range(2):
                    client.post(
                        "/api/v1/evaluate",
                        json={"session_id": "sess-rl2a", "tool": "read", "args": {}},
                        headers=headers,
                    )

                # Session B should still work
                resp = client.post(
                    "/api/v1/evaluate",
                    json={"session_id": "sess-rl2b", "tool": "read", "args": {}},
                    headers=headers,
                )
                assert resp.status_code == 200


# ── Config Validation ─────────────────────────────────────────────────


class TestConfigValidation:
    """Tests for config validation additions."""

    def test_webhook_url_without_secret(self, tmp_db):
        """WEBHOOK_URL without WEBHOOK_SECRET raises ValueError."""
        env = {
            "LLM_API_KEY": "test-key",
            "DB_PATH": tmp_db,
            "WEBHOOK_URL": "https://example.com/webhook",
        }
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("WEBHOOK_SECRET", None)
            from intaris.config import Config

            config = Config()
            with pytest.raises(ValueError, match="WEBHOOK_SECRET is required"):
                config.validate()


# ── Info Endpoints ────────────────────────────────────────────────────


class TestWhoami:
    """Tests for GET /whoami."""

    def test_whoami_basic(self, client_no_auth):
        resp = client_no_auth.get(
            "/api/v1/whoami",
            headers={"X-User-Id": "user-who"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["user_id"] == "user-who"
        assert data["can_switch_user"] is True

    def test_whoami_with_agent_id(self, client_no_auth):
        resp = client_no_auth.get(
            "/api/v1/whoami",
            headers={"X-User-Id": "user-who", "X-Agent-Id": "agent-1"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["user_id"] == "user-who"
        assert data["agent_id"] == "agent-1"

    def test_whoami_requires_user(self, client_no_auth):
        """Whoami without user identity returns 401."""
        resp = client_no_auth.get("/api/v1/whoami")
        assert resp.status_code == 401

    def test_whoami_auth_required(self, client_with_auth):
        """Whoami requires auth when configured."""
        resp = client_with_auth.get(
            "/api/v1/whoami",
            headers={"X-User-Id": "user-who"},
        )
        assert resp.status_code == 401

    def test_whoami_auth_valid(self, client_with_auth):
        resp = client_with_auth.get(
            "/api/v1/whoami",
            headers={**_auth_headers(), "X-User-Id": "user-who"},
        )
        assert resp.status_code == 200
        assert resp.json()["user_id"] == "user-who"

    def test_whoami_bound_user(self, tmp_db):
        """User bound via API key mapping has can_switch_user=False."""
        env = {
            "LLM_API_KEY": "test-key",
            "DB_PATH": tmp_db,
            "INTARIS_API_KEYS": '{"bound-key": "bound-user"}',
        }
        with patch.dict(os.environ, env, clear=False):
            for key in ("INTARIS_API_KEY", "WEBHOOK_URL", "WEBHOOK_SECRET"):
                os.environ.pop(key, None)

            import intaris.server as srv

            srv._config = None
            srv._db = None
            srv._evaluator = None

            from intaris.server import create_app

            app = create_app()
            with TestClient(app) as client:
                resp = client.get(
                    "/api/v1/whoami",
                    headers={"Authorization": "Bearer bound-key"},
                )
                assert resp.status_code == 200
                data = resp.json()
                assert data["user_id"] == "bound-user"
                assert data["can_switch_user"] is False


class TestStats:
    """Tests for GET /stats."""

    def test_stats_empty(self, client_no_auth):
        """Stats with no data returns zero counts."""
        resp = client_no_auth.get(
            "/api/v1/stats",
            headers={"X-User-Id": "user-stats-empty"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_sessions"] == 0
        assert data["total_evaluations"] == 0
        assert data["pending_approvals"] == 0
        assert data["approval_rate"] == 0.0
        assert data["avg_latency_ms"] == 0.0
        assert isinstance(data["users"], list)
        assert isinstance(data["sessions_by_status"], dict)
        assert isinstance(data["decisions"], dict)

    def test_stats_with_data(self, client_no_auth):
        """Stats reflect sessions and evaluations."""
        headers = {"X-User-Id": "user-stats"}
        _create_session(client_no_auth, "sess-stats-1", headers)
        _create_session(client_no_auth, "sess-stats-2", headers)

        # Create some evaluations
        client_no_auth.post(
            "/api/v1/evaluate",
            json={"session_id": "sess-stats-1", "tool": "read", "args": {}},
            headers=headers,
        )
        client_no_auth.post(
            "/api/v1/evaluate",
            json={
                "session_id": "sess-stats-2",
                "tool": "bash",
                "args": {"command": "rm -rf /"},
            },
            headers=headers,
        )

        resp = client_no_auth.get("/api/v1/stats", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_sessions"] == 2
        assert data["total_evaluations"] >= 2
        assert data["sessions_by_status"].get("active", 0) >= 2
        assert "approve" in data["decisions"] or "deny" in data["decisions"]
        assert data["avg_latency_ms"] >= 0

    def test_stats_pending_approvals(self, client_no_auth):
        """Stats counts pending escalations."""
        from intaris.audit import AuditStore
        from intaris.server import _get_db

        headers = {"X-User-Id": "user-stats-pend"}
        _create_session(client_no_auth, "sess-stats-pend", headers)

        # Insert an escalated record directly
        db = _get_db()
        store = AuditStore(db)
        store.insert(
            call_id="stats-esc-1",
            user_id="user-stats-pend",
            session_id="sess-stats-pend",
            agent_id=None,
            tool="bash",
            args_redacted={"command": "dangerous"},
            classification="write",
            evaluation_path="llm",
            decision="escalate",
            risk="high",
            reasoning="Needs review",
            latency_ms=50,
        )

        resp = client_no_auth.get("/api/v1/stats", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["pending_approvals"] >= 1

    def test_stats_users_list(self, client_no_auth):
        """Stats returns list of known users when user is unbound."""
        headers_a = {"X-User-Id": "user-stats-a"}
        headers_b = {"X-User-Id": "user-stats-b"}
        _create_session(client_no_auth, "sess-ua", headers_a)
        _create_session(client_no_auth, "sess-ub", headers_b)

        resp = client_no_auth.get("/api/v1/stats", headers=headers_a)
        assert resp.status_code == 200
        users = resp.json()["users"]
        assert "user-stats-a" in users
        assert "user-stats-b" in users

    def test_stats_users_list_bound(self, tmp_db):
        """Bound user only sees their own user_id in users list."""
        env = {
            "LLM_API_KEY": "test-key",
            "DB_PATH": tmp_db,
            "INTARIS_API_KEYS": '{"bound-key": "bound-user"}',
        }
        with patch.dict(os.environ, env, clear=False):
            for key in ("INTARIS_API_KEY", "WEBHOOK_URL", "WEBHOOK_SECRET"):
                os.environ.pop(key, None)

            import intaris.server as srv

            srv._config = None
            srv._db = None
            srv._evaluator = None

            from intaris.server import create_app

            app = create_app()
            with TestClient(app) as client:
                # Create sessions under two different users
                _create_session(
                    client,
                    "sess-bound",
                    {"Authorization": "Bearer bound-key"},
                )
                resp = client.get(
                    "/api/v1/stats",
                    headers={"Authorization": "Bearer bound-key"},
                )
                assert resp.status_code == 200
                users = resp.json()["users"]
                # Bound user should only see their own ID
                assert users == ["bound-user"]


class TestConfig:
    """Tests for GET /config."""

    def test_config_basic(self, client_no_auth):
        resp = client_no_auth.get(
            "/api/v1/config",
            headers={"X-User-Id": "user-cfg"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "version" in data
        assert "llm" in data
        assert "model" in data["llm"]
        assert "base_url" in data["llm"]
        assert "temperature" in data["llm"]
        assert "reasoning_effort" in data["llm"]
        assert "timeout_ms" in data["llm"]
        assert "rate_limit" in data
        assert "webhook_configured" in data
        assert "auth_configured" in data

    def test_config_masks_base_url(self, client_no_auth):
        """LLM base URL is masked, never shows internal URLs."""
        resp = client_no_auth.get(
            "/api/v1/config",
            headers={"X-User-Id": "user-cfg"},
        )
        assert resp.status_code == 200
        base_url = resp.json()["llm"]["base_url"]
        # Must be either "openai" or "custom", never a real URL
        assert base_url in ("openai", "custom")

    def test_config_no_auth_mode(self, client_no_auth):
        """Config shows auth_configured=False when no auth set."""
        resp = client_no_auth.get(
            "/api/v1/config",
            headers={"X-User-Id": "user-cfg"},
        )
        assert resp.status_code == 200
        assert resp.json()["auth_configured"] is False

    def test_config_auth_mode(self, client_with_auth):
        """Config shows auth_configured=True when auth is set."""
        resp = client_with_auth.get(
            "/api/v1/config",
            headers={**_auth_headers(), "X-User-Id": "user-cfg"},
        )
        assert resp.status_code == 200
        assert resp.json()["auth_configured"] is True

    def test_config_no_webhook(self, client_no_auth):
        """Config shows webhook_configured=False when no webhook."""
        resp = client_no_auth.get(
            "/api/v1/config",
            headers={"X-User-Id": "user-cfg"},
        )
        assert resp.status_code == 200
        assert resp.json()["webhook_configured"] is False


# ── Audit Resolved Filter ────────────────────────────────────────────


class TestAuditResolvedFilter:
    """Tests for the resolved filter on GET /audit."""

    def _setup_escalated(self, client, user_id="user-res"):
        """Create a session with an escalated audit record."""
        from intaris.audit import AuditStore
        from intaris.server import _get_db

        headers = {"X-User-Id": user_id}
        _create_session(client, f"sess-{user_id}", headers)

        db = _get_db()
        store = AuditStore(db)
        store.insert(
            call_id=f"res-call-{user_id}",
            user_id=user_id,
            session_id=f"sess-{user_id}",
            agent_id=None,
            tool="bash",
            args_redacted={"command": "test"},
            classification="write",
            evaluation_path="llm",
            decision="escalate",
            risk="high",
            reasoning="Needs review",
            latency_ms=50,
        )
        return headers, store

    def test_resolved_false_returns_unresolved(self, client_no_auth):
        """resolved=false returns only unresolved records."""
        headers, _ = self._setup_escalated(client_no_auth, "user-res-f")
        resp = client_no_auth.get(
            "/api/v1/audit",
            params={"resolved": "false"},
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        # The escalated record should appear (it's unresolved)
        assert data["total"] >= 1
        for item in data["items"]:
            assert item.get("user_decision") is None

    def test_resolved_true_returns_resolved(self, client_no_auth):
        """resolved=true returns only resolved records."""
        headers, store = self._setup_escalated(client_no_auth, "user-res-t")
        # Resolve the escalation
        store.resolve_escalation(
            "res-call-user-res-t",
            "deny",
            user_note="Denied",
            user_id="user-res-t",
        )
        resp = client_no_auth.get(
            "/api/v1/audit",
            params={"resolved": "true"},
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1
        for item in data["items"]:
            assert item.get("user_decision") is not None

    def test_resolved_none_returns_all(self, client_no_auth):
        """No resolved filter returns all records."""
        headers, store = self._setup_escalated(client_no_auth, "user-res-all")
        # Also create a normal evaluation
        client_no_auth.post(
            "/api/v1/evaluate",
            json={
                "session_id": "sess-user-res-all",
                "tool": "read",
                "args": {},
            },
            headers=headers,
        )
        resp = client_no_auth.get("/api/v1/audit", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        # Should have at least 2 records (escalated + approved)
        assert data["total"] >= 2
