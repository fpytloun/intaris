"""Upstream MCP connection manager for intaris.

Manages connections to upstream MCP servers with lazy initialization,
per-session isolation, idle timeout, and graceful cleanup.

Connection lifecycle:
- Connections are established lazily on first tool call to a server.
- Each MCP proxy session gets its own upstream connections (no sharing).
- Connection key: (mcp_session_id, server_name).
- Idle connections are cleaned up after 30 minutes.
- All connections are closed when the MCP session ends or on shutdown.

Transport support:
- stdio: subprocess via mcp.client.stdio.stdio_client
- streamable-http: via mcp.client.streamable_http.streamablehttp_client
- sse: via mcp.client.sse.sse_client
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, TextIO, cast

from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import Implementation

logger = logging.getLogger(__name__)

# Idle timeout: close connections unused for this long.
_IDLE_TIMEOUT_SECONDS = 30 * 60  # 30 minutes

# Max connections per user across all sessions.
_MAX_CONNECTIONS_PER_USER = 10

# Background sweep interval for idle connection cleanup.
_SWEEP_INTERVAL_SECONDS = 60


@dataclass
class _Connection:
    """A live connection to an upstream MCP server."""

    session: ClientSession
    exit_stack: AsyncExitStack
    server_name: str
    user_id: str
    server_instructions: str | None = None
    last_used: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        """Update last-used timestamp."""
        self.last_used = time.monotonic()


class MCPConnectionManager:
    """Manages upstream MCP server connections.

    Thread-safe for concurrent async access. Each connection is keyed
    by (mcp_session_id, server_name) for per-session isolation.

    Args:
        upstream_timeout_ms: Timeout for upstream MCP calls in milliseconds.
        allow_stdio: Whether stdio transport is allowed.
    """

    def __init__(
        self,
        *,
        upstream_timeout_ms: int = 30000,
        allow_stdio: bool = True,
    ):
        self._timeout_ms = upstream_timeout_ms
        self._allow_stdio = allow_stdio
        # Key: (mcp_session_id, server_name) → _Connection
        self._connections: dict[tuple[str, str], _Connection] = {}
        self._lock = asyncio.Lock()
        self._sweep_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the background idle sweep task."""
        if self._sweep_task is None:
            self._sweep_task = asyncio.create_task(self._sweep_loop())
            logger.info(
                "MCPConnectionManager started (sweep interval=%ds)",
                _SWEEP_INTERVAL_SECONDS,
            )

    async def shutdown(self) -> None:
        """Close all connections and stop the sweep task."""
        if self._sweep_task is not None:
            self._sweep_task.cancel()
            try:
                await self._sweep_task
            except asyncio.CancelledError:
                pass
            self._sweep_task = None

        async with self._lock:
            keys = list(self._connections.keys())
            for key in keys:
                await self._close_connection(key)
            logger.info(
                "MCPConnectionManager shutdown: closed %d connections", len(keys)
            )

    async def get_or_connect(
        self,
        *,
        mcp_session_id: str,
        server_config: dict[str, Any],
        user_id: str = "",
    ) -> ClientSession:
        """Get an existing connection or create a new one.

        Args:
            mcp_session_id: The MCP proxy session ID.
            server_config: Server config dict from MCPServerStore.get_server()
                with decrypt_secrets=True. Must include: name, transport,
                and transport-specific fields.
            user_id: User who owns this connection (for per-user limits).

        Returns:
            An initialized ClientSession ready for tool calls.

        Raises:
            ValueError: If transport is not supported, stdio is disabled,
                or the per-user connection limit is exceeded.
            ConnectionError: If upstream connection fails.
        """
        server_name = server_config["name"]
        key = (mcp_session_id, server_name)

        async with self._lock:
            conn = self._connections.get(key)
            if conn is not None:
                conn.touch()
                return conn.session

            # Enforce per-user connection limit before releasing the lock.
            if user_id:
                user_count = sum(
                    1 for c in self._connections.values() if c.user_id == user_id
                )
                if user_count >= _MAX_CONNECTIONS_PER_USER:
                    raise ValueError(
                        f"Connection limit exceeded: user '{user_id}' has "
                        f"{user_count}/{_MAX_CONNECTIONS_PER_USER} connections. "
                        f"Close unused connections or increase the limit."
                    )

        # Connect outside the lock to avoid blocking other operations.
        # Re-check after acquiring lock in case of concurrent connect.
        new_conn = await self._connect(server_config, user_id=user_id)

        async with self._lock:
            # Double-check: another coroutine may have connected while we waited.
            existing = self._connections.get(key)
            if existing is not None:
                # Close the duplicate we just created.
                await new_conn.exit_stack.aclose()
                existing.touch()
                return existing.session

            self._connections[key] = new_conn
            logger.info(
                "Connected to upstream MCP server '%s' for session %s",
                server_name,
                mcp_session_id,
            )
            return new_conn.session

    async def close_session(self, mcp_session_id: str) -> int:
        """Close all connections for a specific MCP session.

        Args:
            mcp_session_id: The MCP proxy session ID.

        Returns:
            Number of connections closed.
        """
        async with self._lock:
            keys_to_close = [
                key for key in self._connections if key[0] == mcp_session_id
            ]
            for key in keys_to_close:
                await self._close_connection(key)
            return len(keys_to_close)

    def get_server_instructions(
        self, mcp_session_id: str, server_name: str
    ) -> str | None:
        """Get cached server instructions for a connection."""
        conn = self._connections.get((mcp_session_id, server_name))
        return conn.server_instructions if conn else None

    def connection_count(self, *, user_id: str | None = None) -> int:
        """Count active connections, optionally filtered by user_id."""
        if user_id is None:
            return len(self._connections)
        return sum(1 for c in self._connections.values() if c.user_id == user_id)

    async def _connect(
        self, server_config: dict[str, Any], *, user_id: str = ""
    ) -> _Connection:
        """Establish a new connection to an upstream MCP server.

        Raises:
            ValueError: If transport is unsupported or stdio is disabled.
            ConnectionError: If the upstream server is unreachable.
        """
        transport = server_config["transport"]
        server_name = server_config["name"]
        timeout_seconds = self._timeout_ms / 1000.0

        exit_stack = AsyncExitStack()
        try:
            if transport == "stdio":
                if not self._allow_stdio:
                    raise ValueError(
                        f"stdio transport is disabled (MCP_ALLOW_STDIO=false). "
                        f"Cannot connect to server '{server_name}'."
                    )
                read_stream, write_stream = await self._connect_stdio(
                    exit_stack, server_config
                )
            elif transport == "streamable-http":
                read_stream, write_stream = await self._connect_streamable_http(
                    exit_stack, server_config, timeout_seconds
                )
            elif transport == "sse":
                read_stream, write_stream = await self._connect_sse(
                    exit_stack, server_config, timeout_seconds
                )
            else:
                raise ValueError(
                    f"Unsupported transport '{transport}' for server '{server_name}'"
                )

            # Create and initialize the MCP client session.
            session = await exit_stack.enter_async_context(
                ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=timedelta(seconds=timeout_seconds),
                    client_info=Implementation(
                        name="intaris-proxy",
                        version="0.1.0",
                    ),
                )
            )

            init_result = await session.initialize()
            server_instructions = getattr(init_result, "instructions", None)

            logger.debug(
                "Initialized upstream '%s' (%s): %d tools, instructions=%s",
                server_name,
                transport,
                "deferred",
                bool(server_instructions),
            )

            return _Connection(
                session=session,
                exit_stack=exit_stack,
                server_name=server_name,
                user_id=user_id,
                server_instructions=server_instructions,
            )

        except Exception:
            # Clean up the exit stack on any failure.
            await exit_stack.aclose()
            raise

    async def _connect_stdio(
        self,
        exit_stack: AsyncExitStack,
        server_config: dict[str, Any],
    ) -> tuple[Any, Any]:
        """Connect via stdio transport (subprocess)."""
        params = StdioServerParameters(
            command=server_config["command"],
            args=server_config.get("args") or [],
            env=server_config.get("env"),
            cwd=server_config.get("cwd"),
        )

        # Capture stderr to logging.
        stderr_log = cast(TextIO, _StderrLogger(server_config["name"]))

        read_stream, write_stream = await exit_stack.enter_async_context(
            stdio_client(params, errlog=stderr_log)
        )
        return read_stream, write_stream

    async def _connect_streamable_http(
        self,
        exit_stack: AsyncExitStack,
        server_config: dict[str, Any],
        timeout_seconds: float,
    ) -> tuple[Any, Any]:
        """Connect via streamable HTTP transport."""
        url = server_config["url"]
        headers = server_config.get("headers")

        (
            read_stream,
            write_stream,
            _get_session_id,
        ) = await exit_stack.enter_async_context(
            streamablehttp_client(
                url=url,
                headers=headers,
                timeout=timedelta(seconds=timeout_seconds),
            )
        )
        return read_stream, write_stream

    async def _connect_sse(
        self,
        exit_stack: AsyncExitStack,
        server_config: dict[str, Any],
        timeout_seconds: float,
    ) -> tuple[Any, Any]:
        """Connect via SSE transport."""
        url = server_config["url"]
        headers = server_config.get("headers")

        read_stream, write_stream = await exit_stack.enter_async_context(
            sse_client(
                url=url,
                headers=headers,
                timeout=timeout_seconds,
            )
        )
        return read_stream, write_stream

    async def _close_connection(self, key: tuple[str, str]) -> None:
        """Close and remove a connection by key. Must be called under lock."""
        conn = self._connections.pop(key, None)
        if conn is None:
            return
        try:
            await conn.exit_stack.aclose()
            logger.debug("Closed connection to '%s' (session %s)", key[1], key[0])
        except Exception:
            logger.exception("Error closing connection to '%s'", key[1])

    async def _sweep_loop(self) -> None:
        """Background task: close idle connections periodically."""
        while True:
            try:
                await asyncio.sleep(_SWEEP_INTERVAL_SECONDS)
                await self._sweep_idle()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("Error in connection sweep")

    async def _sweep_idle(self) -> None:
        """Close connections that have been idle beyond the timeout."""
        now = time.monotonic()
        async with self._lock:
            idle_keys = [
                key
                for key, conn in self._connections.items()
                if (now - conn.last_used) > _IDLE_TIMEOUT_SECONDS
            ]
            for key in idle_keys:
                logger.info(
                    "Closing idle connection to '%s' (session %s, idle %.0fs)",
                    key[1],
                    key[0],
                    now - self._connections[key].last_used,
                )
                await self._close_connection(key)


class _StderrLogger(io.StringIO):
    """Captures subprocess stderr and routes it to Python logging.

    Used as the errlog parameter for stdio_client to capture upstream
    server error output with proper server name prefixing.

    Inherits from StringIO to satisfy the TextIO protocol expected
    by stdio_client.
    """

    def __init__(self, server_name: str):
        super().__init__()
        self._server_name = server_name
        self._line_buffer = ""

    def write(self, s: str) -> int:
        """Buffer and log complete lines from stderr."""
        self._line_buffer += s
        while "\n" in self._line_buffer:
            line, self._line_buffer = self._line_buffer.split("\n", 1)
            if line.strip():
                logger.warning("[%s stderr] %s", self._server_name, line.rstrip())
        return len(s)

    def flush(self) -> None:
        """Flush any remaining buffered content."""
        if self._line_buffer.strip():
            logger.warning(
                "[%s stderr] %s", self._server_name, self._line_buffer.rstrip()
            )
            self._line_buffer = ""
        super().flush()
