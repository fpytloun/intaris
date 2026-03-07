"""File-based MCP server configuration loader.

Loads upstream MCP server configs from a JSON file and syncs them
into the database. Supports multi-user format for shared deployments.

File format (JSON):

.. code-block:: json

    {
      "users": {
        "user@example.com": {
          "mcpServers": {
            "server-name": {
              "type": "streamable-http",
              "url": "https://...",
              "headers": {"Authorization": "Bearer ..."},
              "agent_pattern": "*"
            }
          }
        }
      }
    }
"""

from __future__ import annotations

import json
import logging
from typing import Any

from intaris.mcp.store import MCPServerStore

logger = logging.getLogger(__name__)


def load_config_file(path: str) -> dict[str, list[dict[str, Any]]]:
    """Load and parse a multi-user MCP config file.

    Args:
        path: Path to the JSON config file.

    Returns:
        Dict mapping user_id → list of server config dicts.

    Raises:
        ValueError: If the file is malformed.
    """
    with open(path) as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise ValueError("MCP config file must be a JSON object")

    users = data.get("users", {})
    if not isinstance(users, dict):
        raise ValueError("'users' must be a JSON object")

    result: dict[str, list[dict[str, Any]]] = {}

    for user_id, user_config in users.items():
        if not isinstance(user_config, dict):
            logger.warning("Skipping invalid config for user %s", user_id)
            continue

        servers = user_config.get("mcpServers", {})
        if not isinstance(servers, dict):
            logger.warning("Skipping invalid mcpServers for user %s", user_id)
            continue

        server_list = []
        for name, server in servers.items():
            if not isinstance(server, dict):
                logger.warning(
                    "Skipping invalid server config %s for user %s", name, user_id
                )
                continue

            # Map file format to internal format
            transport = server.get("type", "stdio")
            config: dict[str, Any] = {
                "name": name,
                "transport": transport,
                "agent_pattern": server.get("agent_pattern", "*"),
                "enabled": server.get("enabled", True),
            }

            # Transport-specific fields
            if transport in ("streamable-http", "sse"):
                config["url"] = server.get("url")
                if server.get("headers"):
                    config["headers"] = server["headers"]
            elif transport == "stdio":
                config["command"] = server.get("command")
                config["args"] = server.get("args", [])
                if server.get("env"):
                    config["env"] = server["env"]
                config["cwd"] = server.get("cwd")

            server_list.append(config)

        if server_list:
            result[user_id] = server_list

    return result


def sync_file_configs(store: MCPServerStore, path: str) -> int:
    """Sync file-based configs into the database.

    - Upserts all servers from the file with source="file"
    - Deletes DB entries with source="file" that are no longer in the file
    - API-created entries (source="api") are never touched

    Args:
        store: MCPServerStore instance.
        path: Path to the config file.

    Returns:
        Number of servers synced.
    """
    configs = load_config_file(path)
    synced = 0

    # Track which (user_id, name) pairs are in the file
    file_entries: set[tuple[str, str]] = set()

    for user_id, servers in configs.items():
        for server in servers:
            name = server["name"]
            file_entries.add((user_id, name))

            try:
                store.upsert_server(
                    user_id=user_id,
                    name=name,
                    transport=server["transport"],
                    command=server.get("command"),
                    args=server.get("args"),
                    env=server.get("env"),
                    cwd=server.get("cwd"),
                    url=server.get("url"),
                    headers=server.get("headers"),
                    agent_pattern=server.get("agent_pattern", "*"),
                    enabled=server.get("enabled", True),
                    source="file",
                )
                synced += 1
            except ValueError as e:
                logger.warning(
                    "Failed to sync server %s for user %s: %s",
                    name,
                    user_id,
                    e,
                )

    # Delete orphaned file-sourced entries
    _delete_orphaned_file_entries(store, file_entries)

    return synced


def _delete_orphaned_file_entries(
    store: MCPServerStore,
    file_entries: set[tuple[str, str]],
) -> None:
    """Delete DB entries with source='file' that are not in the config file."""
    db_entries = store.list_file_sourced_servers()

    for user_id, name in db_entries:
        if (user_id, name) not in file_entries:
            try:
                store.delete_server(user_id=user_id, name=name)
                logger.info(
                    "Deleted orphaned file-sourced server %s for user %s",
                    name,
                    user_id,
                )
            except ValueError:
                pass
