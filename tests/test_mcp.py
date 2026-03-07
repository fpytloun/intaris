"""Tests for MCP proxy modules: crypto, store, config, classifier extensions, evaluator extensions."""

from __future__ import annotations

import json
import os
import tempfile

import pytest

from intaris.audit import AuditStore
from intaris.classifier import Classification, classify
from intaris.config import DBConfig
from intaris.db import Database
from intaris.decision import make_fast_decision
from intaris.mcp.store import MCPServerStore
from intaris.session import SessionStore

TEST_USER = "test-user"
OTHER_USER = "other-user"


@pytest.fixture
def db(tmp_path):
    """Create a test database."""
    config = DBConfig()
    config.path = str(tmp_path / "test.db")
    return Database(config)


@pytest.fixture
def session_store(db):
    return SessionStore(db)


@pytest.fixture
def audit_store(db):
    return AuditStore(db)


# ── Crypto Tests ─────────────────────────────────────────────────────


class TestCrypto:
    """Test Fernet encryption/decryption module."""

    def test_generate_key(self):
        from intaris.crypto import generate_key

        key = generate_key()
        assert isinstance(key, str)
        assert len(key) > 20

    def test_encrypt_decrypt_roundtrip(self):
        from intaris.crypto import decrypt, encrypt, generate_key

        key = generate_key()
        plaintext = "hello world"
        ciphertext = encrypt(plaintext, key)
        assert ciphertext != plaintext
        assert decrypt(ciphertext, key) == plaintext

    def test_encrypt_requires_key(self):
        from intaris.crypto import encrypt

        with pytest.raises(ValueError, match="Invalid encryption key"):
            encrypt("test", "")

    def test_decrypt_requires_key(self):
        from intaris.crypto import decrypt

        with pytest.raises(ValueError, match="Invalid encryption key"):
            decrypt("test", "")

    def test_decrypt_invalid_ciphertext(self):
        from intaris.crypto import generate_key

        key = generate_key()
        from intaris.crypto import decrypt

        with pytest.raises(ValueError, match="Decryption failed"):
            decrypt("not-valid-ciphertext", key)

    def test_validate_key_valid(self):
        from intaris.crypto import generate_key, validate_key

        key = generate_key()
        assert validate_key(key) is True

    def test_validate_key_invalid(self):
        from intaris.crypto import validate_key

        assert validate_key("not-a-valid-key") is False

    def test_validate_key_empty(self):
        from intaris.crypto import validate_key

        assert validate_key("") is False


# ── MCPServerStore Tests ─────────────────────────────────────────────


@pytest.fixture
def encryption_key():
    from intaris.crypto import generate_key

    return generate_key()


@pytest.fixture
def server_store(db, encryption_key):
    return MCPServerStore(db, encryption_key)


@pytest.fixture
def server_store_no_key(db):
    return MCPServerStore(db, "")


class TestMCPServerStore:
    """Test MCP server CRUD operations."""

    def test_upsert_http_server(self, server_store):
        server = server_store.upsert_server(
            user_id=TEST_USER,
            name="tavily",
            transport="streamable-http",
            url="https://mcp.tavily.com/mcp",
            headers={"Authorization": "Bearer test-key"},
        )
        assert server["name"] == "tavily"
        assert server["transport"] == "streamable-http"
        assert server["url"] == "https://mcp.tavily.com/mcp"
        assert server["has_headers"] is True
        assert server["enabled"] is True
        # Encrypted fields should not be in output
        assert "env_encrypted" not in server
        assert "headers_encrypted" not in server

    def test_upsert_stdio_server(self, server_store):
        server = server_store.upsert_server(
            user_id=TEST_USER,
            name="mcp-tool",
            transport="stdio",
            command="npx",
            args=["-y", "mcp-tool"],
            env={"API_KEY": "secret"},
            cwd="/tmp",
        )
        assert server["name"] == "mcp-tool"
        assert server["transport"] == "stdio"
        assert server["command"] == "npx"
        assert server["args"] == ["-y", "mcp-tool"]
        assert server["has_env"] is True
        assert server["cwd"] == "/tmp"

    def test_upsert_updates_existing(self, server_store):
        server_store.upsert_server(
            user_id=TEST_USER,
            name="tavily",
            transport="streamable-http",
            url="https://old.url/mcp",
        )
        updated = server_store.upsert_server(
            user_id=TEST_USER,
            name="tavily",
            transport="streamable-http",
            url="https://new.url/mcp",
        )
        assert updated["url"] == "https://new.url/mcp"

    def test_get_server(self, server_store):
        server_store.upsert_server(
            user_id=TEST_USER,
            name="test-server",
            transport="sse",
            url="https://example.com/sse",
        )
        server = server_store.get_server(user_id=TEST_USER, name="test-server")
        assert server["name"] == "test-server"
        assert server["transport"] == "sse"

    def test_get_server_not_found(self, server_store):
        with pytest.raises(ValueError, match="not found"):
            server_store.get_server(user_id=TEST_USER, name="nonexistent")

    def test_get_server_with_decryption(self, server_store):
        server_store.upsert_server(
            user_id=TEST_USER,
            name="secret-server",
            transport="streamable-http",
            url="https://example.com/mcp",
            headers={"Authorization": "Bearer my-secret"},
        )
        server = server_store.get_server(
            user_id=TEST_USER, name="secret-server", decrypt_secrets=True
        )
        assert server["headers"] == {"Authorization": "Bearer my-secret"}

    def test_list_servers(self, server_store):
        server_store.upsert_server(
            user_id=TEST_USER, name="a-server", transport="stdio", command="cmd"
        )
        server_store.upsert_server(
            user_id=TEST_USER, name="b-server", transport="sse", url="https://b.com"
        )
        servers = server_store.list_servers(user_id=TEST_USER)
        assert len(servers) == 2
        assert servers[0]["name"] == "a-server"
        assert servers[1]["name"] == "b-server"

    def test_list_servers_enabled_only(self, server_store):
        server_store.upsert_server(
            user_id=TEST_USER, name="enabled", transport="stdio", command="cmd"
        )
        server_store.upsert_server(
            user_id=TEST_USER,
            name="disabled",
            transport="stdio",
            command="cmd",
            enabled=False,
        )
        servers = server_store.list_servers(user_id=TEST_USER, enabled_only=True)
        assert len(servers) == 1
        assert servers[0]["name"] == "enabled"

    def test_delete_server(self, server_store):
        server_store.upsert_server(
            user_id=TEST_USER, name="to-delete", transport="stdio", command="cmd"
        )
        server_store.delete_server(user_id=TEST_USER, name="to-delete")
        with pytest.raises(ValueError, match="not found"):
            server_store.get_server(user_id=TEST_USER, name="to-delete")

    def test_delete_server_not_found(self, server_store):
        with pytest.raises(ValueError, match="not found"):
            server_store.delete_server(user_id=TEST_USER, name="nonexistent")

    def test_user_isolation(self, server_store):
        server_store.upsert_server(
            user_id=TEST_USER, name="my-server", transport="stdio", command="cmd"
        )
        servers = server_store.list_servers(user_id=OTHER_USER)
        assert len(servers) == 0

    def test_secrets_require_encryption_key(self, server_store_no_key):
        with pytest.raises(ValueError, match="INTARIS_ENCRYPTION_KEY"):
            server_store_no_key.upsert_server(
                user_id=TEST_USER,
                name="test",
                transport="streamable-http",
                url="https://example.com",
                headers={"Authorization": "Bearer secret"},
            )

    def test_no_secrets_without_key_ok(self, server_store_no_key):
        """Servers without secrets don't need encryption key."""
        server = server_store_no_key.upsert_server(
            user_id=TEST_USER,
            name="plain",
            transport="streamable-http",
            url="https://example.com",
        )
        assert server["name"] == "plain"


class TestServerNameValidation:
    """Test server name validation rules."""

    def test_valid_names(self, server_store):
        for name in ["tavily", "my-server", "server_1", "A123"]:
            server_store.upsert_server(
                user_id=TEST_USER, name=name, transport="stdio", command="cmd"
            )

    def test_empty_name(self, server_store):
        with pytest.raises(ValueError, match="required"):
            server_store.upsert_server(
                user_id=TEST_USER, name="", transport="stdio", command="cmd"
            )

    def test_name_too_long(self, server_store):
        with pytest.raises(ValueError, match="too long"):
            server_store.upsert_server(
                user_id=TEST_USER, name="a" * 65, transport="stdio", command="cmd"
            )

    def test_name_with_colon(self, server_store):
        with pytest.raises(ValueError, match="Invalid server name"):
            server_store.upsert_server(
                user_id=TEST_USER, name="bad:name", transport="stdio", command="cmd"
            )

    def test_name_with_space(self, server_store):
        with pytest.raises(ValueError, match="Invalid server name"):
            server_store.upsert_server(
                user_id=TEST_USER, name="bad name", transport="stdio", command="cmd"
            )

    def test_name_starting_with_hyphen(self, server_store):
        with pytest.raises(ValueError, match="Invalid server name"):
            server_store.upsert_server(
                user_id=TEST_USER, name="-bad", transport="stdio", command="cmd"
            )

    def test_invalid_transport(self, server_store):
        with pytest.raises(ValueError, match="Invalid transport"):
            server_store.upsert_server(
                user_id=TEST_USER, name="test", transport="invalid", command="cmd"
            )


class TestToolPreferences:
    """Test per-tool preference overrides."""

    def test_set_and_get_preference(self, server_store):
        server_store.upsert_server(
            user_id=TEST_USER, name="srv", transport="stdio", command="cmd"
        )
        server_store.set_tool_preference(
            user_id=TEST_USER,
            server_name="srv",
            tool_name="search",
            preference="auto-approve",
        )
        prefs = server_store.get_tool_preferences(user_id=TEST_USER, server_name="srv")
        assert prefs == {"search": "auto-approve"}

    def test_get_all_tool_preferences(self, server_store):
        server_store.upsert_server(
            user_id=TEST_USER, name="srv1", transport="stdio", command="cmd"
        )
        server_store.upsert_server(
            user_id=TEST_USER, name="srv2", transport="stdio", command="cmd"
        )
        server_store.set_tool_preference(
            user_id=TEST_USER,
            server_name="srv1",
            tool_name="tool_a",
            preference="deny",
        )
        server_store.set_tool_preference(
            user_id=TEST_USER,
            server_name="srv2",
            tool_name="tool_b",
            preference="escalate",
        )
        all_prefs = server_store.get_all_tool_preferences(user_id=TEST_USER)
        assert all_prefs == {"srv1:tool_a": "deny", "srv2:tool_b": "escalate"}

    def test_update_preference(self, server_store):
        server_store.upsert_server(
            user_id=TEST_USER, name="srv", transport="stdio", command="cmd"
        )
        server_store.set_tool_preference(
            user_id=TEST_USER,
            server_name="srv",
            tool_name="tool",
            preference="deny",
        )
        server_store.set_tool_preference(
            user_id=TEST_USER,
            server_name="srv",
            tool_name="tool",
            preference="auto-approve",
        )
        prefs = server_store.get_tool_preferences(user_id=TEST_USER, server_name="srv")
        assert prefs["tool"] == "auto-approve"

    def test_delete_preference(self, server_store):
        server_store.upsert_server(
            user_id=TEST_USER, name="srv", transport="stdio", command="cmd"
        )
        server_store.set_tool_preference(
            user_id=TEST_USER,
            server_name="srv",
            tool_name="tool",
            preference="deny",
        )
        server_store.delete_tool_preference(
            user_id=TEST_USER, server_name="srv", tool_name="tool"
        )
        prefs = server_store.get_tool_preferences(user_id=TEST_USER, server_name="srv")
        assert prefs == {}

    def test_invalid_preference(self, server_store):
        server_store.upsert_server(
            user_id=TEST_USER, name="srv", transport="stdio", command="cmd"
        )
        with pytest.raises(ValueError, match="Invalid preference"):
            server_store.set_tool_preference(
                user_id=TEST_USER,
                server_name="srv",
                tool_name="tool",
                preference="invalid",
            )

    def test_cascade_delete(self, server_store):
        """Deleting a server cascades to its tool preferences."""
        server_store.upsert_server(
            user_id=TEST_USER, name="srv", transport="stdio", command="cmd"
        )
        server_store.set_tool_preference(
            user_id=TEST_USER,
            server_name="srv",
            tool_name="tool",
            preference="deny",
        )
        server_store.delete_server(user_id=TEST_USER, name="srv")
        # Preferences should be gone
        prefs = server_store.get_tool_preferences(user_id=TEST_USER, server_name="srv")
        assert prefs == {}


class TestToolsCache:
    """Test tools cache operations."""

    def test_update_tools_cache(self, server_store):
        server_store.upsert_server(
            user_id=TEST_USER, name="srv", transport="stdio", command="cmd"
        )
        tools = [
            {"name": "search", "description": "Search the web", "inputSchema": {}},
            {"name": "extract", "description": "Extract content", "inputSchema": {}},
        ]
        server_store.update_tools_cache(
            user_id=TEST_USER,
            name="srv",
            tools=tools,
            server_instructions="Use these tools wisely",
        )
        server = server_store.get_server(user_id=TEST_USER, name="srv")
        assert len(server["tools_cache"]) == 2
        assert server["tools_cache"][0]["name"] == "search"
        assert server["server_instructions"] == "Use these tools wisely"
        assert server["tools_cache_at"] is not None


# ── Classifier Extension Tests ───────────────────────────────────────


class TestClassifierWithPreferences:
    """Test classifier with tool_preferences parameter."""

    def test_preference_deny_overrides_read_only(self):
        """Deny preference overrides even read-only tools."""
        result = classify(
            "read",
            {},
            tool_preferences={"read": "deny"},
        )
        assert result == Classification.CRITICAL

    def test_preference_escalate(self):
        result = classify(
            "tavily:search",
            {},
            tool_preferences={"tavily:search": "escalate"},
        )
        assert result == Classification.ESCALATE

    def test_preference_auto_approve(self):
        """Auto-approve makes a write tool read-only."""
        result = classify(
            "bash",
            {"command": "npm install"},
            tool_preferences={"bash": "auto-approve"},
        )
        assert result == Classification.READ

    def test_preference_evaluate_is_default(self):
        """Evaluate preference doesn't change classification."""
        result = classify(
            "bash",
            {"command": "npm install"},
            tool_preferences={"bash": "evaluate"},
        )
        assert result == Classification.WRITE

    def test_preference_namespaced_lookup(self):
        """Preferences work with server:tool namespacing."""
        result = classify(
            "tavily:search",
            {},
            tool_preferences={"tavily:search": "deny"},
        )
        assert result == Classification.CRITICAL

    def test_preference_fallback_to_tool_name(self):
        """Falls back to just tool name if namespaced not found."""
        result = classify(
            "tavily:search",
            {},
            tool_preferences={"search": "escalate"},
        )
        assert result == Classification.ESCALATE

    def test_session_policy_deny_beats_preference(self):
        """Session policy deny takes priority over preference."""
        result = classify(
            "bash",
            {"command": "rm -rf /"},
            session_policy={"deny_tools": ["bash"]},
            tool_preferences={"bash": "auto-approve"},
        )
        assert result == Classification.CRITICAL

    def test_preference_deny_beats_session_allow(self):
        """Tool preference deny beats session policy allow."""
        result = classify(
            "bash",
            {"command": "ls"},
            session_policy={"allow_tools": ["bash"]},
            tool_preferences={"bash": "deny"},
        )
        assert result == Classification.CRITICAL

    def test_no_preferences_is_normal(self):
        """No preferences = normal classification."""
        result = classify("read", {})
        assert result == Classification.READ

        result = classify("bash", {"command": "npm install"})
        assert result == Classification.WRITE


# ── Decision Extension Tests ─────────────────────────────────────────


class TestDecisionEscalate:
    """Test the escalate fast path in decision.py."""

    def test_escalate_fast_decision(self):
        decision = make_fast_decision("escalate", "Tool requires escalation")
        assert decision.decision == "escalate"
        assert decision.risk == "high"
        assert decision.path == "fast"
        assert "escalation" in decision.reasoning


# ── Evaluator Extension Tests ────────────────────────────────────────


class TestArgsHash:
    """Test args_hash computation."""

    def test_deterministic_hash(self):
        from intaris.evaluator import _compute_args_hash

        args = {"command": "ls -la", "workdir": "/tmp"}
        hash1 = _compute_args_hash(args)
        hash2 = _compute_args_hash(args)
        assert hash1 == hash2

    def test_different_args_different_hash(self):
        from intaris.evaluator import _compute_args_hash

        hash1 = _compute_args_hash({"command": "ls"})
        hash2 = _compute_args_hash({"command": "rm"})
        assert hash1 != hash2

    def test_key_order_independent(self):
        from intaris.evaluator import _compute_args_hash

        hash1 = _compute_args_hash({"a": 1, "b": 2})
        hash2 = _compute_args_hash({"b": 2, "a": 1})
        assert hash1 == hash2

    def test_empty_args(self):
        from intaris.evaluator import _compute_args_hash

        h = _compute_args_hash({})
        assert isinstance(h, str)
        assert len(h) == 64  # SHA-256 hex


# ── Audit args_hash Tests ────────────────────────────────────────────


class TestAuditArgsHash:
    """Test that args_hash is stored and queryable in audit records."""

    def test_insert_with_args_hash(self, session_store, audit_store):
        session_store.create(user_id=TEST_USER, session_id="sess1", intention="test")
        record = audit_store.insert(
            call_id="call-1",
            user_id=TEST_USER,
            session_id="sess1",
            agent_id=None,
            tool="bash",
            args_redacted={"command": "ls"},
            classification="read",
            evaluation_path="fast",
            decision="approve",
            risk="low",
            reasoning="test",
            latency_ms=10,
            args_hash="abc123hash",
        )
        assert record["args_hash"] == "abc123hash"

    def test_insert_without_args_hash(self, session_store, audit_store):
        session_store.create(user_id=TEST_USER, session_id="sess1", intention="test")
        record = audit_store.insert(
            call_id="call-2",
            user_id=TEST_USER,
            session_id="sess1",
            agent_id=None,
            tool="bash",
            args_redacted={"command": "ls"},
            classification="read",
            evaluation_path="fast",
            decision="approve",
            risk="low",
            reasoning="test",
            latency_ms=10,
        )
        assert record.get("args_hash") is None


# ── File Config Tests ────────────────────────────────────────────────


class TestFileConfig:
    """Test file-based MCP config loading."""

    def test_load_config_file(self):
        from intaris.mcp.config import load_config_file

        config = {
            "users": {
                "user1": {
                    "mcpServers": {
                        "tavily": {
                            "type": "streamable-http",
                            "url": "https://mcp.tavily.com/mcp",
                            "headers": {"Authorization": "Bearer key"},
                        }
                    }
                }
            }
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(config, f)
            f.flush()
            result = load_config_file(f.name)

        os.unlink(f.name)
        assert "user1" in result
        assert len(result["user1"]) == 1
        assert result["user1"][0]["name"] == "tavily"
        assert result["user1"][0]["transport"] == "streamable-http"

    def test_sync_file_configs(self, server_store, encryption_key):
        from intaris.mcp.config import sync_file_configs

        config = {
            "users": {
                TEST_USER: {
                    "mcpServers": {
                        "file-server": {
                            "type": "streamable-http",
                            "url": "https://example.com/mcp",
                        }
                    }
                }
            }
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(config, f)
            f.flush()
            count = sync_file_configs(server_store, f.name)

        os.unlink(f.name)
        assert count == 1
        server = server_store.get_server(user_id=TEST_USER, name="file-server")
        assert server["source"] == "file"

    def test_sync_removes_orphans(self, server_store, encryption_key):
        from intaris.mcp.config import sync_file_configs

        # First sync: create a file-sourced server
        config1 = {
            "users": {
                TEST_USER: {
                    "mcpServers": {
                        "old-server": {
                            "type": "stdio",
                            "command": "cmd",
                        }
                    }
                }
            }
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(config1, f)
            f.flush()
            sync_file_configs(server_store, f.name)

        # Second sync: remove the server from config
        config2 = {"users": {TEST_USER: {"mcpServers": {}}}}
        with open(f.name, "w") as f2:
            json.dump(config2, f2)
        sync_file_configs(server_store, f.name)

        os.unlink(f.name)
        servers = server_store.list_servers(user_id=TEST_USER)
        assert len(servers) == 0


# ── DB Migration Tests ───────────────────────────────────────────────


class TestDBMigration:
    """Test schema migration for args_hash column."""

    def test_args_hash_column_exists(self, db):
        """The args_hash column should exist in audit_log."""
        with db.cursor() as cur:
            cur.execute("PRAGMA table_info(audit_log)")
            columns = {row[1] for row in cur.fetchall()}
        assert "args_hash" in columns

    def test_mcp_servers_table_exists(self, db):
        """The mcp_servers table should exist."""
        with db.cursor() as cur:
            cur.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='mcp_servers'"
            )
            assert cur.fetchone() is not None

    def test_mcp_tool_preferences_table_exists(self, db):
        """The mcp_tool_preferences table should exist."""
        with db.cursor() as cur:
            cur.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='mcp_tool_preferences'"
            )
            assert cur.fetchone() is not None

    def test_escalation_retry_index_exists(self, db):
        """The escalation retry index should exist."""
        with db.cursor() as cur:
            cur.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND name='idx_audit_escalation_retry'"
            )
            assert cur.fetchone() is not None
