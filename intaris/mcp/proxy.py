"""MCP proxy server for intaris.

Implements the MCP protocol server that sits between LLM clients and
upstream MCP servers. Aggregates tools from all configured servers,
evaluates every tool call through the safety pipeline, and forwards
approved calls to the upstream server.

Uses the low-level mcp.server.Server class (not FastMCP) for full
control over per-session tool lists and dynamic server instructions.

The MCP server is mounted at /mcp in the Starlette router. Auth
middleware runs before MCP handlers, setting ContextVars for identity.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import time
import uuid
from datetime import timedelta
from typing import Any

from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import (
    AudioContent,
    CallToolResult,
    ImageContent,
    TextContent,
    Tool,
)

from intaris.mcp.client import MCPConnectionManager

logger = logging.getLogger(__name__)

# Default intention for auto-created MCP proxy sessions.
_DEFAULT_INTENTION = "MCP proxy session — evaluate all tool calls for safety"

# Proxy server instructions prepended to aggregated upstream instructions.
_PROXY_INSTRUCTIONS_HEADER = (
    "You are using tools through the Intaris safety proxy. All tool calls "
    "are evaluated for safety before execution. If a tool call is escalated, "
    "wait for human approval and retry."
)


class MCPProxy:
    """MCP proxy server that aggregates and evaluates tool calls.

    Manages the MCP server instance, session manager, and coordinates
    with the evaluator and connection manager.

    Args:
        connection_manager: Manages upstream MCP connections.
        evaluator: Safety evaluation pipeline.
        session_store: Session CRUD.
        audit_store: Audit log storage.
        server_store: MCP server config CRUD.
        upstream_timeout_ms: Timeout for upstream calls.
    """

    def __init__(
        self,
        *,
        connection_manager: MCPConnectionManager,
        evaluator: Any,  # intaris.evaluator.Evaluator (avoid circular import)
        session_store: Any,  # intaris.session.SessionStore
        audit_store: Any,  # intaris.audit.AuditStore
        server_store: Any,  # intaris.mcp.store.MCPServerStore
        upstream_timeout_ms: int = 30000,
    ):
        self._conn_mgr = connection_manager
        self._evaluator = evaluator
        self._sessions = session_store
        self._audit = audit_store
        self._server_store = server_store
        self._timeout_ms = upstream_timeout_ms

        # Track MCP session → Intaris session mapping.
        # Key: mcp_session_id → {user_id, session_id, agent_id}
        self._session_map: dict[str, dict[str, Any]] = {}
        # Reverse index: (user_id, agent_id) → mcp_session_id for O(1) lookup.
        self._user_session_index: dict[tuple[str, str | None], str] = {}
        self._session_lock = asyncio.Lock()

        # Optional judge reviewer for auto-resolving escalations.
        # Set via set_judge_reviewer() after initialization.
        self._judge_reviewer: Any | None = None

        # Create the MCP server and register handlers.
        self._server = Server("intaris-proxy")
        self._register_handlers()

        # Create the session manager (ASGI handler).
        self._session_manager = StreamableHTTPSessionManager(
            app=self._server,
            json_response=False,
            stateless=False,
        )

    @property
    def session_manager(self) -> StreamableHTTPSessionManager:
        """ASGI handler for mounting at /mcp."""
        return self._session_manager

    @property
    def server(self) -> Server:
        """The underlying MCP server instance."""
        return self._server

    def set_judge_reviewer(self, reviewer: Any) -> None:
        """Set the judge reviewer for auto-resolving escalations.

        Called from the lifespan after the judge reviewer is initialized.
        """
        self._judge_reviewer = reviewer

    @property
    def active_sessions(self) -> int:
        """Number of active MCP proxy sessions."""
        return len(self._session_map)

    @property
    def connection_count(self) -> int:
        """Number of active upstream connections."""
        return self._conn_mgr.connection_count()

    async def start(self) -> None:
        """Start the proxy (connection manager, etc.)."""
        await self._conn_mgr.start()

    async def shutdown(self) -> None:
        """Shutdown the proxy and close all connections."""
        await self._conn_mgr.shutdown()
        self._session_map.clear()
        self._user_session_index.clear()

    async def refresh_server_tools(
        self,
        *,
        user_id: str,
        server_name: str,
    ) -> list[dict[str, Any]]:
        """Connect to an upstream server, fetch tools, and update the cache.

        Used by the REST API to force-refresh the tools cache for a
        specific server. Raises ValueError if the server is not found.

        Returns:
            List of tool definitions (name, description, inputSchema).
        """
        server_cfg = await asyncio.to_thread(
            self._server_store.get_server,
            user_id=user_id,
            name=server_name,
            decrypt_secrets=True,
        )
        if server_cfg is None:
            raise ValueError(f"Server not found: {server_name}")

        client = await self._conn_mgr.get_or_connect(
            server_config=server_cfg,
            user_id=user_id,
        )
        result = await client.list_tools()
        tools_cache = [
            {
                "name": t.name,
                "description": t.description,
                "inputSchema": t.inputSchema,
            }
            for t in result.tools
        ]

        # Cache the tools and server instructions.
        instructions = self._conn_mgr.get_server_instructions(user_id, server_name)
        await asyncio.to_thread(
            self._server_store.update_tools_cache,
            user_id=user_id,
            name=server_name,
            tools=tools_cache,
            server_instructions=instructions,
        )

        logger.info(
            "Refreshed tools for server '%s' (user=%s): %d tools",
            server_name,
            user_id,
            len(tools_cache),
        )
        return tools_cache

    def _register_handlers(self) -> None:
        """Register MCP protocol handlers on the server."""

        @self._server.list_tools()
        async def handle_list_tools() -> list[Tool]:
            return await self._handle_list_tools()

        @self._server.call_tool()
        async def handle_call_tool(
            name: str, arguments: dict[str, Any] | None
        ) -> list[TextContent]:
            return await self._handle_call_tool(name, arguments or {})

    async def _handle_list_tools(self) -> list[Tool]:
        """Aggregate tools from all configured upstream servers.

        Reads user_id/agent_id from ContextVars (set by auth middleware).
        Filters servers by agent_pattern and enabled status.
        Excludes tools with 'deny' preference.
        Namespaces tools as server_name:tool_name.
        """
        from intaris.server import _session_agent_id, _session_user_id

        user_id = _session_user_id.get()
        agent_id = _session_agent_id.get()

        if not user_id:
            logger.warning("list_tools called without user_id")
            return []

        # Get all enabled servers for this user.
        servers = await asyncio.to_thread(
            self._server_store.list_servers,
            user_id=user_id,
            enabled_only=True,
        )

        # Filter by agent_pattern.
        if agent_id:
            servers = [
                s
                for s in servers
                if fnmatch.fnmatch(agent_id, s.get("agent_pattern", "*"))
            ]

        # Get tool preferences for this user.
        tool_prefs = await asyncio.to_thread(
            self._server_store.get_all_tool_preferences,
            user_id=user_id,
        )

        aggregated_tools: list[Tool] = []

        for server_cfg in servers:
            server_name = server_cfg["name"]
            tools_cache = server_cfg.get("tools_cache")

            if not tools_cache:
                # No cached tools — try to connect and fetch.
                try:
                    server_with_secrets = await asyncio.to_thread(
                        self._server_store.get_server,
                        user_id=user_id,
                        name=server_name,
                        decrypt_secrets=True,
                    )
                    client = await self._conn_mgr.get_or_connect(
                        server_config=server_with_secrets,
                        user_id=user_id,
                    )
                    result = await client.list_tools()
                    tools_cache = [
                        {
                            "name": t.name,
                            "description": t.description,
                            "inputSchema": t.inputSchema,
                        }
                        for t in result.tools
                    ]
                    # Cache the tools and server instructions.
                    instructions = self._conn_mgr.get_server_instructions(
                        user_id, server_name
                    )
                    await asyncio.to_thread(
                        self._server_store.update_tools_cache,
                        user_id=user_id,
                        name=server_name,
                        tools=tools_cache,
                        server_instructions=instructions,
                    )
                except Exception:
                    logger.exception(
                        "Failed to fetch tools from upstream '%s'", server_name
                    )
                    continue

            # Add tools with namespacing, excluding denied tools.
            for tool_def in tools_cache:
                tool_name = tool_def["name"]
                namespaced = f"{server_name}:{tool_name}"

                # Skip denied tools.
                pref = tool_prefs.get(namespaced) or tool_prefs.get(tool_name)
                if pref == "deny":
                    continue

                aggregated_tools.append(
                    Tool(
                        name=namespaced,
                        description=tool_def.get("description"),
                        inputSchema=tool_def.get("inputSchema", {"type": "object"}),
                    )
                )

        return aggregated_tools

    async def _handle_call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> list[TextContent]:
        """Evaluate and forward a tool call to the upstream server.

        Flow:
        1. Parse server_name:tool_name
        2. Ensure Intaris session exists (auto-create if needed)
        3. Get tool preferences
        4. Run through evaluator (with tool_preferences)
        5. If approved: forward to upstream, return result
        6. If denied: return error
        7. If escalated: return error with retry instructions
        """
        from intaris.server import _session_agent_id, _session_user_id

        user_id = _session_user_id.get()
        agent_id = _session_agent_id.get()

        if not user_id:
            return [TextContent(type="text", text="Error: user identity not resolved")]

        # Parse namespaced tool name.
        if ":" not in name:
            return [
                TextContent(
                    type="text",
                    text=f"Error: invalid tool name '{name}' — expected 'server:tool' format",
                )
            ]

        server_name, tool_name = name.split(":", 1)

        # Ensure Intaris session exists.
        session_id = await self._ensure_session(user_id, agent_id)

        # Get tool preferences for the classifier.
        tool_prefs = await asyncio.to_thread(
            self._server_store.get_all_tool_preferences,
            user_id=user_id,
        )

        # Run safety evaluation (sync evaluator wrapped in to_thread).
        eval_result = await asyncio.to_thread(
            self._evaluator.evaluate,
            user_id=user_id,
            session_id=session_id,
            agent_id=agent_id,
            tool=name,
            args=arguments,
            tool_preferences=tool_prefs,
        )

        decision = eval_result["decision"]
        call_id = eval_result["call_id"]

        if decision == "deny":
            return [
                TextContent(
                    type="text",
                    text=(
                        f"Tool call denied by Intaris safety evaluation.\n"
                        f"Reason: {eval_result.get('reasoning', 'No reason provided')}\n"
                        f"Call ID: {call_id}"
                    ),
                )
            ]

        if decision == "escalate":
            # Launch judge review if enabled (fire-and-forget)
            if self._judge_reviewer is not None and self._judge_reviewer.is_enabled:
                asyncio.create_task(
                    self._judge_reviewer.review_and_resolve(
                        call_id=call_id,
                        user_id=user_id,
                        session_id=session_id,
                        agent_id=agent_id,
                    )
                )

            reviewer_label = "review" if self._judge_reviewer else "human approval"
            return [
                TextContent(
                    type="text",
                    text=(
                        f"This tool call has been escalated for {reviewer_label} "
                        f"(call_id: {call_id}).\n"
                        f"Please wait for the approval to be resolved in the "
                        f"Intaris UI, then retry this exact tool call. "
                        f"Do not attempt alternative approaches until resolved."
                    ),
                )
            ]

        # Decision is "approve" — forward to upstream.
        try:
            server_with_secrets = await asyncio.to_thread(
                self._server_store.get_server,
                user_id=user_id,
                name=server_name,
                decrypt_secrets=True,
            )
        except ValueError:
            return [
                TextContent(
                    type="text",
                    text=f"Error: MCP server '{server_name}' not found",
                )
            ]

        try:
            client = await self._conn_mgr.get_or_connect(
                server_config=server_with_secrets,
                user_id=user_id,
            )

            result = await client.call_tool(
                name=tool_name,
                arguments=arguments,
                read_timeout_seconds=timedelta(milliseconds=self._timeout_ms),
            )

            # Convert CallToolResult content to TextContent list.
            return self._convert_result(result)

        except Exception as exc:
            logger.exception("Upstream call to '%s:%s' failed", server_name, tool_name)
            # Evict dead connection so next call reconnects.
            await self._conn_mgr.evict(user_id, server_name)
            return [
                TextContent(
                    type="text",
                    text=(
                        f"Error calling upstream server '{server_name}': {exc}\n"
                        f"The tool call was approved but the upstream server "
                        f"returned an error."
                    ),
                )
            ]

    async def _ensure_session(self, user_id: str, agent_id: str | None) -> str:
        """Ensure an Intaris session exists for this MCP proxy connection.

        Auto-creates a session if one doesn't exist. Uses a deterministic
        session ID based on user_id to reuse sessions across reconnects.
        Protected by _session_lock to prevent duplicate session creation.

        Returns:
            The Intaris session_id.
        """
        async with self._session_lock:
            mcp_session_id = self._get_or_create_mcp_session_id(user_id, agent_id)

            # Check if we already have a session mapping.
            mapping = self._session_map.get(mcp_session_id)
            if mapping:
                return mapping["session_id"]

            # Auto-create a new Intaris session.
            session_id = f"mcp-{uuid.uuid4()}"

            # Determine intention: from X-Intaris-Intention header (via ContextVar),
            # or from server_instructions of configured servers, or default.
            from intaris.server import _session_intention

            intention = _session_intention.get() or _DEFAULT_INTENTION

            try:
                await asyncio.to_thread(
                    self._sessions.create,
                    user_id=user_id,
                    session_id=session_id,
                    intention=intention,
                )
            except ValueError:
                # Session might already exist (race condition) — that's OK.
                pass

            self._session_map[mcp_session_id] = {
                "user_id": user_id,
                "session_id": session_id,
                "agent_id": agent_id,
            }
            self._user_session_index[(user_id, agent_id)] = mcp_session_id

            logger.info(
                "Auto-created Intaris session %s for MCP proxy (user=%s)",
                session_id,
                user_id,
            )
            return session_id

    def _get_or_create_mcp_session_id(self, user_id: str, agent_id: str | None) -> str:
        """Get or create a stable MCP session ID for this user+agent.

        Uses a reverse index for O(1) lookup. Must be called under
        _session_lock when creating new entries.
        """
        key = (user_id, agent_id)
        existing = self._user_session_index.get(key)
        if existing is not None:
            return existing

        # Create a new one.
        new_id = f"mcp-{user_id}-{agent_id or 'default'}-{uuid.uuid4().hex[:8]}"
        return new_id

    @staticmethod
    def _convert_result(result: CallToolResult) -> list[TextContent]:
        """Convert a CallToolResult to a list of TextContent.

        Handles text, image, and other content types by converting
        non-text content to descriptive text.
        """
        contents: list[TextContent] = []
        for item in result.content:
            if isinstance(item, TextContent):
                contents.append(item)
            elif isinstance(item, ImageContent):
                contents.append(
                    TextContent(
                        type="text",
                        text=f"[image/{item.mimeType}, {len(item.data)} bytes]",
                    )
                )
            elif isinstance(item, AudioContent):
                contents.append(
                    TextContent(
                        type="text",
                        text=f"[audio/{item.mimeType}, {len(item.data)} bytes]",
                    )
                )
            else:
                # ResourceLink, EmbeddedResource, or unknown types.
                contents.append(TextContent(type="text", text=str(item)))

        if result.isError:
            # Prepend error indicator.
            error_text = "\n".join(c.text for c in contents)
            return [
                TextContent(
                    type="text",
                    text=f"Upstream server returned an error:\n{error_text}",
                )
            ]

        return contents or [TextContent(type="text", text="(empty response)")]

    async def call_tool_rest(
        self,
        *,
        user_id: str,
        agent_id: str | None,
        session_id: str,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Call an upstream MCP tool via REST API with full safety evaluation.

        Unlike ``_handle_call_tool`` (which reads identity from ContextVars
        and auto-creates sessions), this method takes explicit parameters
        and uses the caller-provided ``session_id``.  It is designed for
        the ``POST /api/v1/mcp/call`` REST endpoint where the OpenClaw
        plugin has already created an Intaris session.

        The full evaluation pipeline runs (tool preferences + LLM safety
        evaluation + audit logging), exactly like a local tool call.

        Returns:
            Dict with: content, isError, decision, call_id, reasoning,
            latency_ms.
        """
        start = time.monotonic()
        namespaced = f"{server_name}:{tool_name}"

        # Fetch MCP tool preferences for the classifier.
        tool_prefs = await asyncio.to_thread(
            self._server_store.get_all_tool_preferences,
            user_id=user_id,
        )

        # Run safety evaluation (sync evaluator wrapped in to_thread).
        eval_result = await asyncio.to_thread(
            self._evaluator.evaluate,
            user_id=user_id,
            session_id=session_id,
            agent_id=agent_id,
            tool=namespaced,
            args=arguments,
            tool_preferences=tool_prefs,
        )

        decision = eval_result["decision"]
        call_id = eval_result.get("call_id")
        reasoning = eval_result.get("reasoning")

        if decision == "deny":
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"Tool call denied by Intaris safety evaluation.\n"
                            f"Reason: {reasoning or 'No reason provided'}\n"
                            f"Call ID: {call_id}"
                        ),
                    }
                ],
                "isError": True,
                "decision": "deny",
                "call_id": call_id,
                "reasoning": reasoning,
                "latency_ms": round((time.monotonic() - start) * 1000),
            }

        if decision == "escalate":
            # Launch judge review if enabled (fire-and-forget)
            if self._judge_reviewer is not None and self._judge_reviewer.is_enabled:
                asyncio.create_task(
                    self._judge_reviewer.review_and_resolve(
                        call_id=call_id,
                        user_id=user_id,
                        session_id=session_id,
                        agent_id=agent_id,
                    )
                )

            reviewer_label = "review" if self._judge_reviewer else "human approval"
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"This tool call has been escalated for "
                            f"{reviewer_label} (call_id: {call_id}).\n"
                            f"Please wait for the approval to be resolved in "
                            f"the Intaris UI, then retry this exact tool call. "
                            f"Do not attempt alternative approaches until "
                            f"resolved."
                        ),
                    }
                ],
                "isError": True,
                "decision": "escalate",
                "call_id": call_id,
                "reasoning": reasoning,
                "latency_ms": round((time.monotonic() - start) * 1000),
            }

        # Guard against unexpected decision values.
        if decision != "approve":
            logger.warning(
                "Unexpected decision '%s' for %s:%s; treating as deny",
                decision,
                server_name,
                tool_name,
            )
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"Unexpected evaluation decision: {decision}",
                    }
                ],
                "isError": True,
                "decision": decision,
                "call_id": call_id,
                "reasoning": reasoning,
                "latency_ms": round((time.monotonic() - start) * 1000),
            }

        # Decision is "approve" — forward to upstream.
        try:
            server_with_secrets = await asyncio.to_thread(
                self._server_store.get_server,
                user_id=user_id,
                name=server_name,
                decrypt_secrets=True,
            )
        except ValueError:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"Error: MCP server '{server_name}' not found",
                    }
                ],
                "isError": True,
                "decision": "approve",
                "call_id": call_id,
                "reasoning": reasoning,
                "latency_ms": round((time.monotonic() - start) * 1000),
            }

        # Ensure session mapping exists for this user+agent pair.
        async with self._session_lock:
            mcp_session_id = self._get_or_create_mcp_session_id(user_id, agent_id)
            key = (user_id, agent_id)
            if key not in self._user_session_index:
                self._user_session_index[key] = mcp_session_id
                self._session_map[mcp_session_id] = {
                    "user_id": user_id,
                    "session_id": session_id,
                    "agent_id": agent_id,
                }

        try:
            client = await self._conn_mgr.get_or_connect(
                server_config=server_with_secrets,
                user_id=user_id,
            )

            result = await client.call_tool(
                name=tool_name,
                arguments=arguments,
                read_timeout_seconds=timedelta(milliseconds=self._timeout_ms),
            )

            # Convert CallToolResult content to serializable dicts.
            text_contents = self._convert_result(result)
            content = [{"type": "text", "text": c.text} for c in text_contents]
            is_error = result.isError or False

            return {
                "content": content,
                "isError": is_error,
                "decision": "approve",
                "call_id": call_id,
                "reasoning": reasoning,
                "latency_ms": round((time.monotonic() - start) * 1000),
            }

        except Exception as exc:
            logger.exception("Upstream call to '%s:%s' failed", server_name, tool_name)
            # Evict dead connection so next call reconnects.
            await self._conn_mgr.evict(user_id, server_name)
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"Error calling upstream server "
                            f"'{server_name}': {exc}\n"
                            f"The tool call was approved but the upstream "
                            f"server returned an error."
                        ),
                    }
                ],
                "isError": True,
                "decision": "approve",
                "call_id": call_id,
                "reasoning": reasoning,
                "latency_ms": round((time.monotonic() - start) * 1000),
            }

    def build_instructions(self, user_id: str) -> str:
        """Build aggregated server instructions for a user.

        Combines the proxy header with per-server instructions from
        all configured upstream servers.
        """
        servers = self._server_store.list_servers(user_id=user_id, enabled_only=True)

        parts = [_PROXY_INSTRUCTIONS_HEADER]
        for server_cfg in servers:
            instructions = server_cfg.get("server_instructions")
            if instructions:
                parts.append(f"\n## {server_cfg['name']}\n{instructions}")

        return "\n".join(parts)
