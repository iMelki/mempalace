"""Native authenticated loopback Streamable HTTP transport for MemPalace MCP."""

from __future__ import annotations

import argparse
import functools
import hmac
import json
import logging
import math
import os
import re
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version as package_version
from typing import Any

try:
    import anyio
    from mcp import types
    from mcp.server.lowlevel import Server
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from starlette.types import ASGIApp, Receive, Scope, Send
except ModuleNotFoundError as exc:  # pragma: no cover - exercised without the optional extra
    raise ModuleNotFoundError(
        "Native HTTP MCP support requires Python 3.10+ and `pip install 'mempalace[mcp-http]'`."
    ) from exc

from .mcp_dispatch import ToolDispatchError, dispatch_tool, list_tool_specs
from .version import __version__

logger = logging.getLogger("mempalace_mcp_http")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
DEFAULT_MAX_CONCURRENCY = 1
DEFAULT_SESSION_IDLE_SECONDS: float | None = 300.0
DEFAULT_MAX_SESSIONS = 64
DEFAULT_TERMINATION_MAX_ATTEMPTS = 5
DEFAULT_TERMINATION_RETRY_BASE_SECONDS = 0.25
DEFAULT_TERMINATION_TIMEOUT_SECONDS = 5.0
AUTH_ENVIRONMENT_NAMES = ("MEMPALACE_MCP_TOKEN", "MEMSYS_MEMPALACE_TOKEN")
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
SUPPORTED_MCP_SDK_VERSION = "1.28.1"
_LOOPBACK_ORIGIN_RE = re.compile(
    r"(?P<scheme>https?)://(?P<host>localhost|127\.0\.0\.1|\[::1\])" r"(?::(?P<port>[0-9]+))?",
    re.ASCII | re.IGNORECASE,
)
_BEARER_TOKEN_RE = re.compile(rb"[A-Za-z0-9\-._~+/]+=*", re.ASCII)

ToolRegistry = Mapping[str, Mapping[str, Any]]
ToolRunner = Callable[[str, Mapping[str, Any] | None], Awaitable[Any]]


@dataclass
class _TerminationTombstone:
    transport: Any
    attempts: int = 0
    next_retry_at: float = 0.0
    in_flight: bool = False


def _validated_auth_token(value: str) -> bytes:
    """Return one RFC 6750 token68 value as safe ASCII header bytes."""
    if not isinstance(value, str):
        raise ValueError("Bearer token must be text")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("Bearer token must contain only ASCII token68 characters") from exc
    if len(encoded) > 4096 or _BEARER_TOKEN_RE.fullmatch(encoded) is None:
        raise ValueError("Bearer token must contain only ASCII token68 characters")
    return encoded


class BearerTokenMiddleware:
    """Raw ASGI bearer authentication that does not buffer MCP streams."""

    def __init__(self, app: ASGIApp, token: str):
        self.app = app
        self._expected = b"Bearer " + _validated_auth_token(token)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":  # pragma: no cover - HTTP-only application
            await self.app(scope, receive, send)
            return

        authorization_values = []
        for key, value in scope.get("headers", []):
            if key.lower() == b"authorization":
                authorization_values.append(value)

        if len(authorization_values) != 1 or not hmac.compare_digest(
            authorization_values[0], self._expected
        ):
            response = JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


class StrictOriginMiddleware:
    """Reject malformed, duplicate, or non-loopback Origin values."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":  # pragma: no cover - HTTP-only application
            await self.app(scope, receive, send)
            return

        origins = [
            value.decode("latin-1")
            for key, value in scope.get("headers", [])
            if key.lower() == b"origin"
        ]
        if len(origins) > 1 or (origins and not _valid_loopback_origin(origins[0])):
            response = JSONResponse({"error": "invalid_origin"}, status_code=403)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class BackendCallGate:
    """Run synchronous palace handlers off-loop with bounded parallelism."""

    def __init__(self, tools: ToolRegistry, max_concurrency: int):
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        self.tools = tools
        self._limiter = anyio.CapacityLimiter(max_concurrency)

    async def run(self, tool_name: str, arguments: Mapping[str, Any] | None) -> Any:
        call = functools.partial(dispatch_tool, self.tools, tool_name, arguments)
        # The MCP waiter may be abandoned, so the worker owns a separate
        # backend permit and releases it only after dispatch has exited.
        borrower = object()
        await self._limiter.acquire_on_behalf_of(borrower)

        handoff_lock = threading.Lock()
        worker_started = False
        host_released = False
        abandoned_before_start = object()

        def call_with_permit() -> Any:
            nonlocal worker_started
            with handoff_lock:
                if host_released:
                    return abandoned_before_start
                worker_started = True

            try:
                return call()
            finally:
                anyio.from_thread.run_sync(
                    self._limiter.release_on_behalf_of,
                    borrower,
                )

        try:
            result = await anyio.to_thread.run_sync(
                call_with_permit,
                abandon_on_cancel=True,
            )
        except BaseException:
            release_from_host = False
            with handoff_lock:
                if not worker_started:
                    host_released = True
                    release_from_host = True
            if release_from_host:
                self._limiter.release_on_behalf_of(borrower)
            raise

        if result is abandoned_before_start:  # pragma: no cover - cancelled waiter ignores it
            raise RuntimeError("Backend worker was abandoned before dispatch")
        return result


class ActiveAwareSessionManager(StreamableHTTPSessionManager):
    """Bound SDK sessions without cancelling requests that are still active."""

    def __init__(
        self,
        *args: Any,
        max_sessions: int,
        session_idle_seconds: float | None,
        termination_max_attempts: int = DEFAULT_TERMINATION_MAX_ATTEMPTS,
        termination_retry_base_seconds: float = DEFAULT_TERMINATION_RETRY_BASE_SECONDS,
        termination_timeout_seconds: float = DEFAULT_TERMINATION_TIMEOUT_SECONDS,
        **kwargs: Any,
    ):
        if max_sessions < 1:
            raise ValueError("max_sessions must be at least 1")
        if session_idle_seconds is not None and (
            not math.isfinite(session_idle_seconds) or session_idle_seconds <= 0
        ):
            raise ValueError("session_idle_seconds must be positive")
        if termination_max_attempts < 1:
            raise ValueError("termination_max_attempts must be at least 1")
        if not math.isfinite(termination_retry_base_seconds) or termination_retry_base_seconds <= 0:
            raise ValueError("termination_retry_base_seconds must be positive")
        if not math.isfinite(termination_timeout_seconds) or termination_timeout_seconds <= 0:
            raise ValueError("termination_timeout_seconds must be positive")
        # SDK 1.28.1's deadline wraps Server.run() and can cancel an active
        # request. MemPalace owns expiry and leaves that SDK mechanism disabled.
        kwargs["session_idle_timeout"] = None
        super().__init__(*args, **kwargs)
        required_sdk_state = ("_server_instances", "_session_owners", "_session_creation_lock")
        if any(not hasattr(self, name) for name in required_sdk_state):
            raise RuntimeError("MCP SDK session-manager internals are incompatible with MemPalace")
        self.max_sessions = max_sessions
        self.session_idle_seconds = session_idle_seconds
        self.termination_max_attempts = termination_max_attempts
        self.termination_retry_base_seconds = termination_retry_base_seconds
        self.termination_timeout_seconds = termination_timeout_seconds
        self._session_last_activity: dict[str, float] = {}
        self._session_active_requests: dict[str, int] = {}
        self._termination_tombstones: dict[str, _TerminationTombstone] = {}
        self._session_lifecycle_lock = anyio.Lock()
        self._pending_session_creations = 0

    @asynccontextmanager
    async def run(self) -> AsyncIterator[None]:
        try:
            async with super().run():
                try:
                    async with anyio.create_task_group() as cleanup_tasks:
                        cleanup_tasks.start_soon(self._cleanup_loop)
                        try:
                            yield
                        finally:
                            cleanup_tasks.cancel_scope.cancel()
                finally:
                    with anyio.CancelScope(shield=True):
                        await self._drain_termination_tombstones_on_shutdown()
        finally:
            self._session_last_activity.clear()
            self._session_active_requests.clear()
            self._termination_tombstones.clear()
            self._pending_session_creations = 0

    async def handle_request(self, scope: Scope, receive: Receive, send: Send) -> None:
        session_ids = _raw_header_values(scope, b"mcp-session-id")
        if len(session_ids) > 1:
            response = JSONResponse({"error": "invalid_session_header"}, status_code=400)
            await response(scope, receive, send)
            return
        session_id = session_ids[0] if session_ids else None
        if session_id is None:
            await self._handle_new_session_with_capacity(scope, receive, send)
            return

        await self.cleanup_expired_sessions()
        async with self._session_lifecycle_lock:
            transport = self._server_instances.get(session_id)
            if transport is not None and not transport.is_terminated:
                self._session_active_requests[session_id] = (
                    self._session_active_requests.get(session_id, 0) + 1
                )
            else:
                transport = None

        if transport is None:
            await super().handle_request(scope, receive, send)
            return

        response_status: int | None = None

        async def tracked_send(message: dict[str, Any]) -> None:
            nonlocal response_status
            if message.get("type") == "http.response.start":
                response_status = message.get("status")
            await send(message)

        try:
            await super().handle_request(scope, receive, tracked_send)
        finally:
            async with self._session_lifecycle_lock:
                active = max(0, self._session_active_requests.get(session_id, 1) - 1)
                if active:
                    self._session_active_requests[session_id] = active
                else:
                    self._session_active_requests.pop(session_id, None)
                current_transport = self._server_instances.get(session_id)
                deleted = (
                    scope.get("method") == "DELETE"
                    and response_status == 200
                    and transport.is_terminated
                )
                if current_transport is transport and deleted:
                    self._remove_session_locked(session_id)
                elif current_transport is transport and transport.is_terminated:
                    self._termination_tombstones[session_id] = _TerminationTombstone(
                        transport=transport,
                        next_retry_at=time.monotonic(),
                    )
                    self._remove_session_locked(session_id)
                elif current_transport is transport:
                    self._session_last_activity[session_id] = time.monotonic()
                else:
                    self._session_last_activity.pop(session_id, None)
                    self._session_active_requests.pop(session_id, None)

    async def cleanup_expired_sessions(self) -> tuple[str, ...]:
        """Terminate expired inactive sessions and prune terminated instances."""
        now = time.monotonic()
        async with self._session_lifecycle_lock:
            retrying = self._take_retryable_tombstones_locked(now)
            expired = self._pop_expired_sessions_locked(now)
        await self._terminate_transports(retrying + expired)
        return tuple(session_id for session_id, _ in expired)

    async def _handle_new_session_with_capacity(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        at_capacity = False
        async with self._session_lifecycle_lock:
            now = time.monotonic()
            retrying = self._take_retryable_tombstones_locked(now)
            expired = self._pop_expired_sessions_locked(now)
            terminating = retrying + expired
            if self._accounted_session_count_locked() >= self.max_sessions:
                at_capacity = True
            else:
                self._pending_session_creations += 1
        if at_capacity:
            await self._terminate_transports(terminating)
            response = JSONResponse({"error": "session_capacity_reached"}, status_code=503)
            await response(scope, receive, send)
            return

        before = set(self._server_instances)
        try:
            await self._terminate_transports(terminating)
            await super().handle_request(scope, receive, send)
        finally:
            now = time.monotonic()
            async with self._session_lifecycle_lock:
                self._pending_session_creations = max(0, self._pending_session_creations - 1)
                for session_id in set(self._server_instances) - before:
                    transport = self._server_instances.get(session_id)
                    if transport is not None and not transport.is_terminated:
                        self._session_last_activity[session_id] = now

    def _pop_expired_sessions_locked(self, now: float) -> list[tuple[str, Any]]:
        expired: list[tuple[str, Any]] = []
        current_ids = set(self._server_instances)
        for session_id in set(self._session_owners) - current_ids:
            self._session_owners.pop(session_id, None)
        for session_id in set(self._session_last_activity) - current_ids:
            self._session_last_activity.pop(session_id, None)
            self._session_active_requests.pop(session_id, None)

        for session_id, transport in list(self._server_instances.items()):
            if self._session_active_requests.get(session_id, 0) > 0:
                continue
            if (
                self._pending_session_creations > 0
                and session_id not in self._session_last_activity
                and not transport.is_terminated
            ):
                continue
            last_activity = self._session_last_activity.setdefault(session_id, now)
            is_expired = (
                self.session_idle_seconds is not None
                and now - last_activity >= self.session_idle_seconds
            )
            if transport.is_terminated or is_expired:
                expired.append((session_id, transport))
                self._termination_tombstones[session_id] = _TerminationTombstone(
                    transport=transport,
                    next_retry_at=now,
                    in_flight=True,
                )
                self._remove_session_locked(session_id)
        return expired

    def _take_retryable_tombstones_locked(self, now: float) -> list[tuple[str, Any]]:
        retrying = []
        for session_id, tombstone in self._termination_tombstones.items():
            if not tombstone.in_flight and tombstone.next_retry_at <= now:
                tombstone.in_flight = True
                retrying.append((session_id, tombstone.transport))
        return retrying

    def _accounted_session_count_locked(self) -> int:
        return (
            len(self._server_instances)
            + len(self._termination_tombstones)
            + self._pending_session_creations
        )

    def _remove_session_locked(self, session_id: str) -> None:
        self._server_instances.pop(session_id, None)
        self._session_owners.pop(session_id, None)
        self._session_last_activity.pop(session_id, None)
        self._session_active_requests.pop(session_id, None)

    async def _terminate_transports(self, sessions: list[tuple[str, Any]]) -> None:
        if not sessions:
            return
        async with anyio.create_task_group() as task_group:
            for session_id, transport in sessions:
                task_group.start_soon(self._terminate_transport, session_id, transport)

    async def _terminate_transport(self, session_id: str, transport: Any) -> None:
        failure: BaseException | None = None
        try:
            with anyio.fail_after(self.termination_timeout_seconds):
                await transport.terminate()
        except BaseException as exc:
            failure = exc
            with anyio.CancelScope(shield=True):
                async with self._session_lifecycle_lock:
                    tombstone = self._termination_tombstones.get(session_id)
                    if tombstone is not None and tombstone.transport is transport:
                        tombstone.attempts += 1
                        tombstone.in_flight = False
                        attempt_in_wave = (tombstone.attempts - 1) % self.termination_max_attempts
                        if attempt_in_wave < self.termination_max_attempts - 1:
                            delay = min(
                                30.0,
                                self.termination_retry_base_seconds * (2**attempt_in_wave),
                            )
                        else:
                            delay = max(
                                30.0,
                                min(
                                    300.0,
                                    self.termination_retry_base_seconds * (2**attempt_in_wave),
                                ),
                            )
                            logger.critical(
                                "MCP session %s termination retry wave exhausted; "
                                "capacity remains closed and cleanup will retry after %.3f seconds",
                                session_id[:64],
                                delay,
                            )
                        tombstone.next_retry_at = time.monotonic() + delay
            if not isinstance(exc, Exception):
                raise
        else:
            with anyio.CancelScope(shield=True):
                async with self._session_lifecycle_lock:
                    tombstone = self._termination_tombstones.get(session_id)
                    if tombstone is not None and tombstone.transport is transport:
                        self._termination_tombstones.pop(session_id, None)
        if failure is not None:
            if isinstance(failure, TimeoutError):
                logger.error(
                    "Timed out terminating MCP session %s after %.3f seconds",
                    session_id[:64],
                    self.termination_timeout_seconds,
                )
            else:
                logger.error(
                    "Failed to terminate MCP session %s: %s",
                    session_id[:64],
                    type(failure).__name__,
                    exc_info=(type(failure), failure, failure.__traceback__),
                )

    async def _drain_termination_tombstones_on_shutdown(self) -> None:
        """Retry retained sessions and fail shutdown if any cannot terminate."""
        async with self._session_lifecycle_lock:
            for tombstone in self._termination_tombstones.values():
                tombstone.in_flight = False
        for _ in range(self.termination_max_attempts):
            async with self._session_lifecycle_lock:
                if not self._termination_tombstones:
                    return
                sessions = []
                for session_id, tombstone in self._termination_tombstones.items():
                    if tombstone.in_flight:
                        continue
                    tombstone.in_flight = True
                    sessions.append((session_id, tombstone.transport))
            await self._terminate_transports(sessions)
            async with self._session_lifecycle_lock:
                if not self._termination_tombstones:
                    return

        async with self._session_lifecycle_lock:
            unresolved = tuple(sorted(self._termination_tombstones))
        logger.critical(
            "MCP shutdown could not terminate %d retained session(s): %s",
            len(unresolved),
            ", ".join(session_id[:64] for session_id in unresolved),
        )
        raise RuntimeError(
            f"MCP shutdown could not terminate {len(unresolved)} retained session(s)"
        )

    async def _cleanup_loop(self) -> None:
        interval = (
            max(0.05, min(30.0, self.session_idle_seconds / 2))
            if self.session_idle_seconds is not None
            else min(30.0, self.termination_retry_base_seconds)
        )
        while True:
            await anyio.sleep(interval)
            await self.cleanup_expired_sessions()


def _tool_result(payload: Any, *, is_error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload, indent=2))],
        isError=is_error,
    )


def build_mcp_server(
    tools: ToolRegistry,
    *,
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    runner: ToolRunner | None = None,
) -> Server:
    """Build the protocol server around the transport-neutral tool registry."""
    server = Server("mempalace", version=__version__)
    bounded_runner = BackendCallGate(tools, max_concurrency).run if runner is None else runner

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [types.Tool(**spec) for spec in list_tool_specs(tools)]

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict[str, Any] | None) -> types.CallToolResult:
        try:
            return _tool_result(await bounded_runner(name, arguments))
        except ToolDispatchError as exc:
            return _tool_result({"error": str(exc)}, is_error=True)
        except Exception:
            logger.exception("Tool error in %s", name)
            return _tool_result({"error": "Internal tool error"}, is_error=True)

    return server


class _StreamableHttpEndpoint:
    def __init__(self, manager: StreamableHTTPSessionManager):
        self.manager = manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.manager.handle_request(scope, receive, send)


def _raw_header_values(scope: Scope, name: bytes) -> list[str]:
    return [
        value.decode("latin-1") for key, value in scope.get("headers", []) if key.lower() == name
    ]


def _valid_loopback_origin(value: str) -> bool:
    if (
        not value
        or value != value.strip()
        or any(ord(char) <= 32 or ord(char) == 127 for char in value)
    ):
        return False
    match = _LOOPBACK_ORIGIN_RE.fullmatch(value)
    if match is None:
        return False
    port = match.group("port")
    return port is None or 1 <= int(port) <= 65535


def _require_supported_mcp_sdk() -> None:
    try:
        installed = package_version("mcp")
    except PackageNotFoundError as exc:  # pragma: no cover - import guard normally wins first
        raise RuntimeError("The MCP SDK is not installed") from exc
    if installed != SUPPORTED_MCP_SDK_VERSION:
        raise RuntimeError(
            f"MemPalace native MCP requires mcp=={SUPPORTED_MCP_SDK_VERSION}; found {installed}."
        )


def _transport_security() -> TransportSecuritySettings:
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            "127.0.0.1",
            "127.0.0.1:*",
            "localhost",
            "localhost:*",
            "[::1]",
            "[::1]:*",
        ],
        allowed_origins=[
            "http://127.0.0.1",
            "http://127.0.0.1:*",
            "http://localhost",
            "http://localhost:*",
            "http://[::1]",
            "http://[::1]:*",
            "https://127.0.0.1",
            "https://127.0.0.1:*",
            "https://localhost",
            "https://localhost:*",
            "https://[::1]",
            "https://[::1]:*",
        ],
    )


def create_http_app(
    *,
    auth_token: str,
    tools: ToolRegistry | None = None,
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    session_idle_seconds: float | None = DEFAULT_SESSION_IDLE_SECONDS,
    max_sessions: int = DEFAULT_MAX_SESSIONS,
    termination_max_attempts: int = DEFAULT_TERMINATION_MAX_ATTEMPTS,
    termination_retry_base_seconds: float = DEFAULT_TERMINATION_RETRY_BASE_SECONDS,
    termination_timeout_seconds: float = DEFAULT_TERMINATION_TIMEOUT_SECONDS,
    runner: ToolRunner | None = None,
) -> Starlette:
    """Create the authenticated `/mcp` ASGI app without opening a socket."""
    _require_supported_mcp_sdk()
    if session_idle_seconds is not None and (
        not math.isfinite(session_idle_seconds) or session_idle_seconds <= 0
    ):
        raise ValueError("session_idle_seconds must be positive")
    if max_sessions < 1:
        raise ValueError("max_sessions must be at least 1")
    if tools is None:
        from . import mcp_server

        # mcp_server redirects fd 1 while importing Chroma dependencies so the
        # stdio protocol stays clean. HTTP shares its registry, then restores
        # the embedding process's stdout before returning the ASGI app.
        mcp_server._restore_stdout()
        tools = mcp_server.TOOLS

    server = build_mcp_server(tools, max_concurrency=max_concurrency, runner=runner)
    manager = ActiveAwareSessionManager(
        app=server,
        json_response=True,
        stateless=False,
        security_settings=_transport_security(),
        session_idle_seconds=session_idle_seconds,
        max_sessions=max_sessions,
        termination_max_attempts=termination_max_attempts,
        termination_retry_base_seconds=termination_retry_base_seconds,
        termination_timeout_seconds=termination_timeout_seconds,
    )

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        async with manager.run():
            yield

    from .status import get_fast_drawer_count

    async def healthz(_request: Any) -> JSONResponse:
        drawers = get_fast_drawer_count()
        payload = {
            "status": "ok",
            "service": "mempalace-mcp",
            "transport": "native-http",
            "version": __version__,
        }
        if drawers is not None:
            payload["drawers"] = drawers
            payload["drawerCount"] = drawers
        return JSONResponse(payload)

    app = Starlette(
        routes=[
            Route("/healthz", endpoint=healthz, methods=["GET"]),
            Route("/mcp", endpoint=_StreamableHttpEndpoint(manager)),
        ],
        middleware=[
            Middleware(BearerTokenMiddleware, token=auth_token),
            Middleware(StrictOriginMiddleware),
        ],
        lifespan=lifespan,
    )
    app.state.mcp_session_manager = manager
    return app


def resolve_auth_token(environ: Mapping[str, str] | None = None) -> tuple[str, str]:
    """Resolve the static local bearer token without accepting CLI secrets."""
    values = os.environ if environ is None else environ
    native_name, legacy_name = AUTH_ENVIRONMENT_NAMES
    native = values.get(native_name, "")
    legacy = values.get(legacy_name, "")
    native_is_set = native != ""
    legacy_is_set = legacy != ""

    try:
        native_bytes = _validated_auth_token(native) if native_is_set else None
        legacy_bytes = _validated_auth_token(legacy) if legacy_is_set else None
    except ValueError as exc:
        raise RuntimeError("Configured MemPalace bearer token is not header-safe token68") from exc

    if (
        native_bytes is not None
        and legacy_bytes is not None
        and not hmac.compare_digest(native_bytes, legacy_bytes)
    ):
        raise RuntimeError(
            f"{native_name} and {legacy_name} are both set to different values; "
            "refusing to choose a bearer token."
        )
    if native_bytes is not None:
        return native, native_name
    if legacy_bytes is not None:
        return legacy, legacy_name
    raise RuntimeError(
        "Set MEMPALACE_MCP_TOKEN or MEMSYS_MEMPALACE_TOKEN before starting "
        "the native HTTP MCP server."
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MemPalace native loopback HTTP MCP server")
    parser.add_argument("--palace", metavar="PATH", help="Path to a disposable or managed palace")
    parser.add_argument("--host", choices=LOOPBACK_HOSTS, default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--max-concurrency", type=int, default=DEFAULT_MAX_CONCURRENCY)
    parser.add_argument("--max-http-connections", type=int, default=64)
    parser.add_argument("--max-sessions", type=int, default=DEFAULT_MAX_SESSIONS)
    parser.add_argument(
        "--session-idle-seconds",
        type=float,
        default=DEFAULT_SESSION_IDLE_SECONDS,
    )
    parser.add_argument(
        "--termination-timeout-seconds",
        type=float,
        default=DEFAULT_TERMINATION_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--log-level",
        choices=("critical", "error", "warning", "info", "debug"),
        default="info",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    if not 1 <= args.port <= 65535:
        raise SystemExit("--port must be between 1 and 65535")
    if args.max_concurrency < 1:
        raise SystemExit("--max-concurrency must be at least 1")
    if args.max_http_connections < 1:
        raise SystemExit("--max-http-connections must be at least 1")
    if args.max_sessions < 1:
        raise SystemExit("--max-sessions must be at least 1")
    if not math.isfinite(args.session_idle_seconds) or args.session_idle_seconds <= 0:
        raise SystemExit("--session-idle-seconds must be positive")
    if not math.isfinite(args.termination_timeout_seconds) or args.termination_timeout_seconds <= 0:
        raise SystemExit("--termination-timeout-seconds must be positive")

    if args.palace:
        os.environ["MEMPALACE_PALACE_PATH"] = os.path.abspath(args.palace)

    token, token_source = resolve_auth_token()
    from . import mcp_server

    mcp_server._restore_stdout()
    mcp_server._refresh_vector_disabled_flag()
    app = create_http_app(
        auth_token=token,
        tools=mcp_server.TOOLS,
        max_concurrency=args.max_concurrency,
        max_sessions=args.max_sessions,
        session_idle_seconds=args.session_idle_seconds,
        termination_timeout_seconds=args.termination_timeout_seconds,
    )

    import uvicorn

    logger.info(
        "Starting MemPalace MCP on http://%s:%s/mcp (auth=%s)",
        args.host,
        args.port,
        token_source,
    )
    config = uvicorn.Config(
        app,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        access_log=False,
        limit_concurrency=args.max_http_connections,
        timeout_graceful_shutdown=10,
        ws="none",
    )
    uvicorn.Server(config).run()


if __name__ == "__main__":  # pragma: no cover
    main()
