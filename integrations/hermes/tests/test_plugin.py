"""Unit tests for the Hermes Intaris plugin."""

from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Stub the Hermes-internal ``tools.registry`` module so the plugin can be
# imported without a full Hermes installation.
# ---------------------------------------------------------------------------

_fake_tools: dict[str, object] = {}


class _FakeToolEntry:
    __slots__ = (
        "name",
        "toolset",
        "schema",
        "handler",
        "check_fn",
        "requires_env",
        "is_async",
        "description",
        "emoji",
    )

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class _FakeRegistry:
    def __init__(self):
        self._tools: dict[str, _FakeToolEntry] = {}

    def register(self, **kw):
        entry = _FakeToolEntry(**kw)
        self._tools[kw["name"]] = entry


_registry = _FakeRegistry()

# Inject the stub into sys.modules BEFORE importing the plugin.
_tools_mod = types.ModuleType("tools")
_registry_mod = types.ModuleType("tools.registry")
_registry_mod.registry = _registry  # type: ignore[attr-defined]
sys.modules["tools"] = _tools_mod
sys.modules["tools.registry"] = _registry_mod

# Now we can import the plugin package.
from hermes_intaris import (  # noqa: E402
    Config,
    SessionState,
    _build_agent_summary,
    _build_checkpoint_content,
    _build_policy,
    _get_or_create_state,
    _make_guarded_handler,
    _resolve_config,
    _sessions,
    register,
)
from hermes_intaris.client import ApiResult, IntarisClient  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_state():
    """Reset module-level state between tests."""
    import hermes_intaris as mod

    _sessions.clear()
    mod._cfg = None
    mod._client = None
    mod._mcp_tool_cache = None
    mod._mcp_tool_cache_at = 0.0
    mod._flush_timer = None
    _registry._tools.clear()
    yield
    _sessions.clear()


@pytest.fixture
def env_vars(monkeypatch):
    """Set required environment variables."""
    monkeypatch.setenv("INTARIS_URL", "http://test:8060")
    monkeypatch.setenv("INTARIS_API_KEY", "test-key")
    monkeypatch.setenv("INTARIS_USER_ID", "test-user")


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------


class TestConfig:
    def test_defaults(self, monkeypatch):
        # Clear all INTARIS env vars to test true defaults
        for key in list(os.environ):
            if key.startswith("INTARIS_"):
                monkeypatch.delenv(key, raising=False)
        cfg = _resolve_config()
        assert cfg.url == "http://localhost:8060"
        assert cfg.api_key == ""
        assert cfg.fail_open is False
        assert cfg.checkpoint_interval == 25
        assert cfg.recording is False
        assert cfg.mcp_tools is True  # default is True (enabled by default)

    def test_from_env(self, monkeypatch):
        monkeypatch.setenv("INTARIS_URL", "http://custom:9090")
        monkeypatch.setenv("INTARIS_API_KEY", "my-key")
        monkeypatch.setenv("INTARIS_FAIL_OPEN", "true")
        monkeypatch.setenv("INTARIS_CHECKPOINT_INTERVAL", "50")
        monkeypatch.setenv("INTARIS_SESSION_RECORDING", "1")
        monkeypatch.setenv("INTARIS_MCP_TOOLS", "true")
        cfg = _resolve_config()
        assert cfg.url == "http://custom:9090"
        assert cfg.api_key == "my-key"
        assert cfg.fail_open is True
        assert cfg.checkpoint_interval == 50
        assert cfg.recording is True
        assert cfg.mcp_tools is True


# ---------------------------------------------------------------------------
# Helper tests
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_build_policy_defaults(self):
        policy = _build_policy("")
        assert "/tmp/*" in policy["allow_paths"]
        assert "/var/tmp/*" in policy["allow_paths"]

    def test_build_policy_with_paths(self, monkeypatch):
        monkeypatch.setenv("HOME", "/home/user")
        policy = _build_policy("~/src, /opt/data")
        paths = policy["allow_paths"]
        assert "/home/user/src/*" in paths
        assert "/opt/data/*" in paths

    def test_build_checkpoint_content(self):
        import hermes_intaris as mod

        mod._cfg = Config(checkpoint_interval=25)
        state = SessionState(
            call_count=50,
            approved_count=45,
            denied_count=3,
            escalated_count=2,
            recent_tools=["read_file", "terminal"],
        )
        content = _build_checkpoint_content(state)
        assert "Checkpoint #2" in content
        assert "50 calls" in content
        assert "45 approved" in content
        assert "read_file, terminal" in content

    def test_build_agent_summary(self):
        state = SessionState(
            call_count=10,
            approved_count=8,
            denied_count=1,
            escalated_count=1,
        )
        summary = _build_agent_summary(state)
        assert "10 tool calls" in summary
        assert "8 approved" in summary

    def test_get_or_create_state(self):
        state1 = _get_or_create_state("task-1")
        state2 = _get_or_create_state("task-1")
        assert state1 is state2
        assert state1.call_count == 0


# ---------------------------------------------------------------------------
# Client tests
# ---------------------------------------------------------------------------


class TestClient:
    def test_call_api_success(self):
        client = IntarisClient("http://test:8060", "key", "user")
        response_data = json.dumps({"session_id": "s1"}).encode()

        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = mock.MagicMock()
            mock_resp.read.return_value = response_data
            mock_resp.status = 200
            mock_resp.__enter__ = mock.MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = mock.MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp

            result = client.call_api("POST", "/api/v1/intention", {"session_id": "s1"})
            assert result.data == {"session_id": "s1"}
            assert result.status == 200
            assert result.error is None

    def test_call_api_http_error(self):
        import urllib.error

        client = IntarisClient("http://test:8060", "key", "user")

        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            error = urllib.error.HTTPError(
                "http://test:8060/api/v1/intention",
                401,
                "Unauthorized",
                {},
                None,
            )
            error.read = mock.MagicMock(return_value=b'{"detail": "Invalid API key"}')
            mock_urlopen.side_effect = error

            result = client.call_api("POST", "/api/v1/intention", {})
            assert result.status == 401
            assert "Invalid API key" in (result.error or "")

    def test_call_api_network_error(self):
        client = IntarisClient("http://test:8060", "key", "user")

        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = ConnectionError("Connection refused")

            result = client.call_api("POST", "/api/v1/intention", {})
            assert result.status is None
            assert "Connection refused" in (result.error or "")


# ---------------------------------------------------------------------------
# Tool wrapper tests
# ---------------------------------------------------------------------------


class TestToolWrapper:
    def _setup_plugin(self, monkeypatch):
        """Set up the plugin with mocked client."""
        import hermes_intaris as mod

        monkeypatch.setenv("INTARIS_URL", "http://test:8060")
        monkeypatch.setenv("INTARIS_API_KEY", "test-key")
        monkeypatch.setenv("INTARIS_MCP_TOOLS", "false")

        mod._cfg = _resolve_config()
        mod._client = IntarisClient("http://test:8060", "test-key")

        # Create a session
        state = _get_or_create_state("task-1")
        state.intaris_session_id = "hm-test-session"
        return state

    def test_approve_calls_original(self, monkeypatch):
        state = self._setup_plugin(monkeypatch)
        original = mock.MagicMock(return_value='{"result": "ok"}')

        handler = _make_guarded_handler(original, "read_file", "file-tools", False)

        with mock.patch.object(IntarisClient, "evaluate") as mock_eval:
            mock_eval.return_value = ApiResult(
                data={
                    "call_id": "c1",
                    "decision": "approve",
                    "reasoning": "",
                    "risk": "low",
                    "path": "fast",
                    "latency_ms": 1,
                },
                status=200,
            )
            result = handler({"path": "/tmp/test.txt"})

        assert result == '{"result": "ok"}'
        original.assert_called_once_with({"path": "/tmp/test.txt"})

    def test_deny_blocks_execution(self, monkeypatch):
        state = self._setup_plugin(monkeypatch)
        original = mock.MagicMock(return_value='{"result": "ok"}')

        handler = _make_guarded_handler(original, "terminal", "terminal-tools", False)

        with mock.patch.object(IntarisClient, "evaluate") as mock_eval:
            mock_eval.return_value = ApiResult(
                data={
                    "call_id": "c2",
                    "decision": "deny",
                    "reasoning": "Dangerous command",
                    "risk": "critical",
                    "path": "critical",
                    "latency_ms": 1,
                },
                status=200,
            )
            result = handler({"command": "rm -rf /"})

        parsed = json.loads(result)
        assert "error" in parsed
        assert "DENIED" in parsed["error"]
        assert "Dangerous command" in parsed["error"]
        original.assert_not_called()

    def test_fail_open_on_network_error(self, monkeypatch):
        import hermes_intaris as mod

        monkeypatch.setenv("INTARIS_FAIL_OPEN", "true")
        state = self._setup_plugin(monkeypatch)
        mod._cfg = _resolve_config()  # Re-resolve with fail_open=true
        original = mock.MagicMock(return_value='{"result": "ok"}')

        handler = _make_guarded_handler(original, "read_file", "file-tools", False)

        with mock.patch.object(IntarisClient, "evaluate") as mock_eval:
            mock_eval.return_value = ApiResult(error="Connection refused")
            result = handler({"path": "/tmp/test.txt"})

        assert result == '{"result": "ok"}'
        original.assert_called_once()

    def test_fail_closed_on_network_error(self, monkeypatch):
        state = self._setup_plugin(monkeypatch)
        original = mock.MagicMock(return_value='{"result": "ok"}')

        handler = _make_guarded_handler(original, "terminal", "terminal-tools", False)

        with mock.patch.object(IntarisClient, "evaluate") as mock_eval:
            mock_eval.return_value = ApiResult(error="Connection refused")
            result = handler({"command": "ls"})

        parsed = json.loads(result)
        assert "error" in parsed
        assert "INTARIS_FAIL_OPEN=false" in parsed["error"]
        original.assert_not_called()

    def test_4xx_always_blocks(self, monkeypatch):
        """4xx errors block even with fail_open=true."""
        import hermes_intaris as mod

        monkeypatch.setenv("INTARIS_FAIL_OPEN", "true")
        state = self._setup_plugin(monkeypatch)
        mod._cfg = _resolve_config()
        original = mock.MagicMock(return_value='{"result": "ok"}')

        handler = _make_guarded_handler(original, "terminal", "terminal-tools", False)

        with mock.patch.object(IntarisClient, "evaluate") as mock_eval:
            mock_eval.return_value = ApiResult(error="Unauthorized", status=401)
            result = handler({"command": "ls"})

        parsed = json.loads(result)
        assert "error" in parsed
        assert "rejected" in parsed["error"].lower()
        original.assert_not_called()

    def test_statistics_tracking(self, monkeypatch):
        state = self._setup_plugin(monkeypatch)
        original = mock.MagicMock(return_value='{"result": "ok"}')

        handler = _make_guarded_handler(original, "read_file", "file-tools", False)

        with mock.patch.object(IntarisClient, "evaluate") as mock_eval:
            mock_eval.return_value = ApiResult(
                data={
                    "call_id": "c1",
                    "decision": "approve",
                    "reasoning": "",
                    "risk": "low",
                    "path": "fast",
                    "latency_ms": 1,
                },
                status=200,
            )
            handler({"path": "/tmp/test.txt"})

        assert state.call_count == 1
        assert state.approved_count == 1
        assert state.recent_tools == ["read_file"]

    def test_no_session_fail_closed(self, monkeypatch):
        """No active session with fail_open=false returns error."""
        import hermes_intaris as mod

        monkeypatch.setenv("INTARIS_URL", "http://test:8060")
        monkeypatch.setenv("INTARIS_API_KEY", "test-key")
        monkeypatch.setenv("INTARIS_MCP_TOOLS", "false")
        mod._cfg = _resolve_config()
        mod._client = IntarisClient("http://test:8060", "test-key")

        original = mock.MagicMock(return_value='{"result": "ok"}')
        handler = _make_guarded_handler(original, "terminal", "terminal-tools", False)

        result = handler({"command": "ls"})
        parsed = json.loads(result)
        assert "error" in parsed
        assert "No active Intaris session" in parsed["error"]
        original.assert_not_called()


# ---------------------------------------------------------------------------
# Hook tests
# ---------------------------------------------------------------------------


class TestHooks:
    def test_on_session_start_creates_session(self, monkeypatch):
        import hermes_intaris as mod

        monkeypatch.setenv("INTARIS_URL", "http://test:8060")
        monkeypatch.setenv("INTARIS_API_KEY", "test-key")
        monkeypatch.setenv("INTARIS_MCP_TOOLS", "false")
        mod._cfg = _resolve_config()
        mod._client = IntarisClient("http://test:8060", "test-key")

        with mock.patch.object(IntarisClient, "create_intention") as mock_create:
            mock_create.return_value = ApiResult(
                data={"session_id": "hm-123"}, status=200
            )
            _on_session_start = mod._on_session_start
            _on_session_start(session_id="task-1", model="gpt-5", platform="cli")

        state = _sessions.get("task-1")
        assert state is not None
        assert state.intaris_session_id is not None
        assert state.intaris_session_id.startswith("hm-")

    def test_on_session_end_signals_completion(self, monkeypatch):
        import hermes_intaris as mod

        monkeypatch.setenv("INTARIS_URL", "http://test:8060")
        monkeypatch.setenv("INTARIS_API_KEY", "test-key")
        monkeypatch.setenv("INTARIS_MCP_TOOLS", "false")
        mod._cfg = _resolve_config()
        mod._client = IntarisClient("http://test:8060", "test-key")

        state = _get_or_create_state("task-1")
        state.intaris_session_id = "hm-test"
        state.call_count = 5
        state.approved_count = 4
        state.denied_count = 1

        with (
            mock.patch.object(IntarisClient, "update_status") as mock_status,
            mock.patch.object(IntarisClient, "submit_agent_summary") as mock_summary,
        ):
            mock_status.return_value = ApiResult(data={}, status=200)
            mock_summary.return_value = ApiResult(data={}, status=200)

            mod._on_session_end(session_id="task-1", completed=True)

        mock_status.assert_called_once_with("hm-test", "completed")
        mock_summary.assert_called_once()
        summary_text = mock_summary.call_args[0][1]
        assert "5 tool calls" in summary_text

        # Session should be removed
        assert "task-1" not in _sessions

    def test_pre_llm_call_forwards_reasoning(self, monkeypatch):
        import hermes_intaris as mod

        monkeypatch.setenv("INTARIS_URL", "http://test:8060")
        monkeypatch.setenv("INTARIS_API_KEY", "test-key")
        monkeypatch.setenv("INTARIS_MCP_TOOLS", "false")
        monkeypatch.setenv("INTARIS_SESSION_RECORDING", "false")
        mod._cfg = _resolve_config()
        mod._client = IntarisClient("http://test:8060", "test-key")

        state = _get_or_create_state("task-1")
        state.intaris_session_id = "hm-test"

        with mock.patch.object(IntarisClient, "submit_reasoning") as mock_reason:
            mock_reason.return_value = ApiResult(data={}, status=200)

            mod._on_pre_llm_call(
                session_id="task-1",
                user_message="Fix the bug in main.py",
            )

        mock_reason.assert_called_once()
        content = mock_reason.call_args[0][1]
        assert "Fix the bug in main.py" in content
        assert state.intention_pending is True


# ---------------------------------------------------------------------------
# Register tests
# ---------------------------------------------------------------------------


class TestRegister:
    def test_register_wraps_tools(self, monkeypatch):
        """register() should wrap all existing tools in the registry."""
        monkeypatch.setenv("INTARIS_URL", "http://test:8060")
        monkeypatch.setenv("INTARIS_API_KEY", "test-key")
        monkeypatch.setenv("INTARIS_MCP_TOOLS", "false")

        # Pre-populate the fake registry with a tool
        original_handler = mock.MagicMock(return_value='{"ok": true}')
        _registry.register(
            name="test_tool",
            toolset="test",
            schema={"name": "test_tool", "parameters": {}},
            handler=original_handler,
            check_fn=None,
            requires_env=[],
            is_async=False,
            description="A test tool",
            emoji="",
        )

        # Create a mock plugin context
        ctx = mock.MagicMock()

        with mock.patch.object(IntarisClient, "list_mcp_tools", return_value=[]):
            register(ctx)

        # The tool should have been re-registered with a different handler
        entry = _registry._tools.get("test_tool")
        assert entry is not None
        assert entry.handler is not original_handler  # Wrapped

        # Hooks should be registered
        assert ctx.register_hook.call_count == 6
        hook_names = [call[0][0] for call in ctx.register_hook.call_args_list]
        assert "on_session_start" in hook_names
        assert "on_session_end" in hook_names
        assert "pre_llm_call" in hook_names
        assert "pre_tool_call" in hook_names
        assert "post_tool_call" in hook_names
        assert "post_llm_call" in hook_names
