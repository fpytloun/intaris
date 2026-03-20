"""CRUD operations for MCP server configurations and tool preferences.

All operations are scoped by user_id for multi-tenant isolation.
Secrets (env vars, HTTP headers) are encrypted at rest using Fernet.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from intaris.db import Database

logger = logging.getLogger(__name__)

# Server name validation: alphanumeric start, then alphanumeric/hyphen/underscore.
_SERVER_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
_SERVER_NAME_MAX_LEN = 64

_VALID_TRANSPORTS = {"stdio", "streamable-http", "sse"}
_VALID_PREFERENCES = {"auto-approve", "evaluate", "escalate", "deny"}


class MCPServerStore:
    """CRUD for MCP server configs and tool preferences.

    Encrypts env/headers before storage, decrypts on internal read.
    API responses should NEVER include decrypted secrets — use
    `list_servers()` and `get_server()` which return redacted views.
    """

    def __init__(self, db: Database, encryption_key: str = ""):
        self._db = db
        self._encryption_key = encryption_key

    def _encrypt(self, plaintext: str) -> str:
        """Encrypt a JSON string for storage."""
        from intaris.crypto import encrypt

        return encrypt(plaintext, self._encryption_key)

    def _decrypt(self, ciphertext: str) -> str:
        """Decrypt a stored JSON string."""
        from intaris.crypto import decrypt

        return decrypt(ciphertext, self._encryption_key)

    @staticmethod
    def _validate_name(name: str) -> None:
        """Validate server name format."""
        if not name:
            raise ValueError("Server name is required")
        if len(name) > _SERVER_NAME_MAX_LEN:
            raise ValueError(
                f"Server name too long ({len(name)} chars, max {_SERVER_NAME_MAX_LEN})"
            )
        if not _SERVER_NAME_PATTERN.match(name):
            raise ValueError(
                f"Invalid server name '{name}'. Must match pattern: "
                "alphanumeric start, then alphanumeric/hyphen/underscore. "
                "No colons, dots, slashes, or spaces."
            )

    def upsert_server(
        self,
        *,
        user_id: str,
        name: str,
        transport: str,
        command: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        url: str | None = None,
        headers: dict[str, str] | None = None,
        agent_pattern: str = "*",
        enabled: bool = True,
        source: str = "api",
        clear_env: bool = False,
        clear_headers: bool = False,
    ) -> dict[str, Any]:
        """Create or update an MCP server configuration.

        Args:
            clear_env: If True, explicitly remove stored env vars.
            clear_headers: If True, explicitly remove stored headers.

        Raises:
            ValueError: If name is invalid, transport is unknown, or
                secrets are provided without an encryption key.
        """
        self._validate_name(name)

        if transport not in _VALID_TRANSPORTS:
            raise ValueError(
                f"Invalid transport '{transport}'. "
                f"Must be one of: {', '.join(sorted(_VALID_TRANSPORTS))}"
            )

        # Encrypt secrets if provided, or set sentinel for clearing.
        # _CLEAR_SENTINEL is used in the UPSERT to distinguish "not provided"
        # (None → COALESCE preserves existing) from "explicitly cleared".
        env_encrypted = None
        if clear_env:
            env_encrypted = ""  # Empty string → cleared in UPSERT
        elif env:
            if not self._encryption_key:
                raise ValueError(
                    "INTARIS_ENCRYPTION_KEY is required to store server secrets. "
                    "Set the environment variable and restart."
                )
            env_encrypted = self._encrypt(json.dumps(env))

        headers_encrypted = None
        if clear_headers:
            headers_encrypted = ""  # Empty string → cleared in UPSERT
        elif headers:
            if not self._encryption_key:
                raise ValueError(
                    "INTARIS_ENCRYPTION_KEY is required to store server secrets. "
                    "Set the environment variable and restart."
                )
            headers_encrypted = self._encrypt(json.dumps(headers))

        now = datetime.now(timezone.utc).isoformat()
        args_json = json.dumps(args) if args else None

        with self._db.cursor() as cur:
            cur.execute(
                """
                INSERT INTO mcp_servers
                    (user_id, name, transport, command, args, env_encrypted,
                     cwd, url, headers_encrypted, agent_pattern, enabled,
                     source, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (user_id, name) DO UPDATE SET
                    transport = excluded.transport,
                    command = excluded.command,
                    args = excluded.args,
                    env_encrypted = CASE
                        WHEN excluded.env_encrypted = '' THEN NULL
                        ELSE COALESCE(excluded.env_encrypted, mcp_servers.env_encrypted)
                    END,
                    cwd = excluded.cwd,
                    url = excluded.url,
                    headers_encrypted = CASE
                        WHEN excluded.headers_encrypted = '' THEN NULL
                        ELSE COALESCE(excluded.headers_encrypted, mcp_servers.headers_encrypted)
                    END,
                    agent_pattern = excluded.agent_pattern,
                    enabled = excluded.enabled,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                (
                    user_id,
                    name,
                    transport,
                    command,
                    args_json,
                    env_encrypted,
                    cwd,
                    url,
                    headers_encrypted,
                    agent_pattern,
                    enabled,
                    source,
                    now,
                    now,
                ),
            )

        return self.get_server(user_id=user_id, name=name)

    def get_server(
        self, *, user_id: str, name: str, decrypt_secrets: bool = False
    ) -> dict[str, Any]:
        """Get a server config by name.

        Args:
            user_id: Tenant identifier.
            name: Server name.
            decrypt_secrets: If True, decrypt env/headers (internal use only).

        Raises:
            ValueError: If server not found.
        """
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT * FROM mcp_servers WHERE user_id = ? AND name = ?",
                (user_id, name),
            )
            row = cur.fetchone()

        if row is None:
            raise ValueError(f"MCP server '{name}' not found")

        return self._row_to_dict(row, decrypt_secrets=decrypt_secrets)

    def list_servers(
        self,
        *,
        user_id: str,
        enabled_only: bool = False,
    ) -> list[dict[str, Any]]:
        """List all MCP servers for a user.

        Returns redacted view (no decrypted secrets).
        """
        with self._db.cursor() as cur:
            if enabled_only:
                cur.execute(
                    "SELECT * FROM mcp_servers "
                    "WHERE user_id = ? AND enabled = ? "
                    "ORDER BY name",
                    (user_id, True),
                )
            else:
                cur.execute(
                    "SELECT * FROM mcp_servers WHERE user_id = ? ORDER BY name",
                    (user_id,),
                )
            rows = cur.fetchall()

        return [self._row_to_dict(row) for row in rows]

    def delete_server(self, *, user_id: str, name: str) -> None:
        """Delete a server config (cascades to tool preferences).

        Raises:
            ValueError: If server not found.
        """
        with self._db.cursor() as cur:
            cur.execute(
                "DELETE FROM mcp_servers WHERE user_id = ? AND name = ?",
                (user_id, name),
            )
            if cur.rowcount == 0:
                raise ValueError(f"MCP server '{name}' not found")

    def update_tools_cache(
        self,
        *,
        user_id: str,
        name: str,
        tools: list[dict[str, Any]],
        server_instructions: str | None = None,
    ) -> None:
        """Update cached tool list and server instructions."""
        now = datetime.now(timezone.utc).isoformat()
        with self._db.cursor() as cur:
            cur.execute(
                """
                UPDATE mcp_servers
                SET tools_cache = ?, tools_cache_at = ?,
                    server_instructions = COALESCE(?, server_instructions),
                    updated_at = ?
                WHERE user_id = ? AND name = ?
                """,
                (
                    json.dumps(tools),
                    now,
                    server_instructions,
                    now,
                    user_id,
                    name,
                ),
            )

    # ── Tool Preferences ──────────────────────────────────────────

    def get_tool_preferences(self, *, user_id: str, server_name: str) -> dict[str, str]:
        """Get all tool preferences for a server.

        Returns:
            Dict mapping tool_name → preference.
        """
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT tool_name, preference FROM mcp_tool_preferences "
                "WHERE user_id = ? AND server_name = ?",
                (user_id, server_name),
            )
            return {row["tool_name"]: row["preference"] for row in cur.fetchall()}

    def get_all_tool_preferences(self, *, user_id: str) -> dict[str, str]:
        """Get all tool preferences for a user across all servers.

        Returns:
            Dict mapping 'server_name:tool_name' → preference.
        """
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT server_name, tool_name, preference "
                "FROM mcp_tool_preferences WHERE user_id = ?",
                (user_id,),
            )
            return {
                f"{row['server_name']}:{row['tool_name']}": row["preference"]
                for row in cur.fetchall()
            }

    def set_tool_preference(
        self,
        *,
        user_id: str,
        server_name: str,
        tool_name: str,
        preference: str,
    ) -> None:
        """Set a tool preference override.

        Raises:
            ValueError: If preference is invalid.
        """
        if preference not in _VALID_PREFERENCES:
            raise ValueError(
                f"Invalid preference '{preference}'. "
                f"Must be one of: {', '.join(sorted(_VALID_PREFERENCES))}"
            )
        now = datetime.now(timezone.utc).isoformat()
        with self._db.cursor() as cur:
            cur.execute(
                """
                INSERT INTO mcp_tool_preferences
                    (user_id, server_name, tool_name, preference, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (user_id, server_name, tool_name) DO UPDATE SET
                    preference = excluded.preference,
                    updated_at = excluded.updated_at
                """,
                (user_id, server_name, tool_name, preference, now, now),
            )

    def delete_tool_preference(
        self, *, user_id: str, server_name: str, tool_name: str
    ) -> None:
        """Reset a tool preference to default (evaluate)."""
        with self._db.cursor() as cur:
            cur.execute(
                "DELETE FROM mcp_tool_preferences "
                "WHERE user_id = ? AND server_name = ? AND tool_name = ?",
                (user_id, server_name, tool_name),
            )

    def list_all_enabled_servers(self) -> list[dict[str, Any]]:
        """List all enabled servers across all users.

        Returns redacted view (no decrypted secrets). Used by the
        connection manager for eager startup — secrets are decrypted
        per-server during the connection loop.
        """
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT * FROM mcp_servers WHERE enabled = ? ORDER BY user_id, name",
                (True,),
            )
            rows = cur.fetchall()

        return [self._row_to_dict(row) for row in rows]

    def list_file_sourced_servers(self) -> list[tuple[str, str]]:
        """List all (user_id, name) pairs with source='file'.

        Used by the config loader for orphan reconciliation.
        """
        with self._db.cursor() as cur:
            cur.execute("SELECT user_id, name FROM mcp_servers WHERE source = 'file'")
            return [(row["user_id"], row["name"]) for row in cur.fetchall()]

    # ── Internal Helpers ──────────────────────────────────────────

    def _row_to_dict(
        self, row: Any, *, decrypt_secrets: bool = False
    ) -> dict[str, Any]:
        """Convert a sqlite3.Row to a dict with optional secret decryption."""
        d = dict(row)

        # Parse JSON fields
        if d.get("args"):
            try:
                d["args"] = json.loads(d["args"])
            except (json.JSONDecodeError, TypeError):
                pass

        if d.get("tools_cache"):
            try:
                d["tools_cache"] = json.loads(d["tools_cache"])
            except (json.JSONDecodeError, TypeError):
                pass

        # Handle secrets: decrypt or redact
        if decrypt_secrets and self._encryption_key:
            if d.get("env_encrypted"):
                try:
                    d["env"] = json.loads(self._decrypt(d["env_encrypted"]))
                except (ValueError, json.JSONDecodeError):
                    logger.warning("Failed to decrypt env for server %s", d.get("name"))
                    d["env"] = None
            else:
                d["env"] = None

            if d.get("headers_encrypted"):
                try:
                    d["headers"] = json.loads(self._decrypt(d["headers_encrypted"]))
                except (ValueError, json.JSONDecodeError):
                    logger.warning(
                        "Failed to decrypt headers for server %s", d.get("name")
                    )
                    d["headers"] = None
            else:
                d["headers"] = None
        else:
            # Redacted view: show boolean flags
            d["has_env"] = bool(d.get("env_encrypted"))
            d["has_headers"] = bool(d.get("headers_encrypted"))

        # Remove encrypted fields from output
        d.pop("env_encrypted", None)
        d.pop("headers_encrypted", None)

        # Convert enabled from int to bool
        d["enabled"] = bool(d.get("enabled", 0))

        return d
