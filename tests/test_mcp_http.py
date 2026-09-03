"""Native Streamable HTTP MCP tests using only disposable registries and palaces."""

from __future__ import annotations

import asyncio
import json
import math
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import AsyncExitStack
from importlib.metadata import version

import pytest

pytest.importorskip("mcp")

import anyio  # noqa: E402
import httpx  # noqa: E402
import uvicorn  # noqa: E402
from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

import mempalace.mcp_http as mcp_http_module  # noqa: E402
from mempalace.evaluation_identity import (  # noqa: E402
    EVALUATION_CORPUS_MANIFEST_SCHEMA,
    sha256_identity,
)
from mempalace.mcp_http import (  # noqa: E402
    DEFAULT_HOST,
    DEFAULT_MAX_CONCURRENCY,
    DEFAULT_MAX_SESSIONS,
    DEFAULT_SESSION_IDLE_SECONDS,
    DEFAULT_TERMINATION_TIMEOUT_SECONDS,
    BearerTokenMiddleware,
    _parse_args,
    create_http_app,
    resolve_auth_token,
)


AUTH_FIXTURE = "unit-test-loopback-token"
PROTOCOL_VERSION = "2025-11-25"
ASYNC_SCENARIO_TIMEOUT_SECONDS = 30
MCP_OPERATION_TIMEOUT_SECONDS = 5
SERVER_LIFECYCLE_TIMEOUT_SECONDS = 5


def _registry(handler):
    return {
        "test_tool": {
            "description": "Disposable test tool",
            "input_schema": {
                "type": "object",
                "properties": {"value": {"type": "integer"}},
            },
            "handler": handler,
        }
    }


def _evaluation_manifest(data_plane_id: str) -> dict[str, object]:
    material = {
        "schema": EVALUATION_CORPUS_MANIFEST_SCHEMA,
        "dataPlaneId": data_plane_id,
        "inventorySha256": "sha256:" + "a" * 64,
        "scopeSha256": "sha256:" + "b" * 64,
        "sourceRevision": "sha256:" + "c" * 64,
        "processingSourceRevision": "sha256:" + "d" * 64,
        "itemCount": 42,
    }
    return {
        **material,
        "capturedAtUtc": "2026-07-29T14:00:00Z",
        "corpusRevision": sha256_identity(material),
    }


def _run_async_bounded(factory, *, timeout=ASYNC_SCENARIO_TIMEOUT_SECONDS):
    outcome = {}

    def runner():
        try:
            outcome["result"] = asyncio.run(factory())
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=runner, name="mempalace-http-test-client", daemon=True)
    thread.start()
    thread.join(timeout=timeout)
    if thread.is_alive():
        raise TimeoutError(f"HTTP client scenario exceeded {timeout} seconds")
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("result")


def _start_test_server(app):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            log_level="warning",
            access_log=False,
            lifespan="on",
            ws="none",
            timeout_graceful_shutdown=2,
        )
    )
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [listener]},
        name="mempalace-http-test-server",
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + SERVER_LIFECYCLE_TIMEOUT_SECONDS
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=SERVER_LIFECYCLE_TIMEOUT_SECONDS)
        listener.close()
        raise TimeoutError("ephemeral HTTP server did not start within its deadline")
    return server, thread, listener, listener.getsockname()[1]


def _stop_test_server(server, thread, listener):
    server.should_exit = True
    thread.join(timeout=SERVER_LIFECYCLE_TIMEOUT_SECONDS)
    listener.close()
    if thread.is_alive():
        raise TimeoutError("ephemeral HTTP server did not stop within its deadline")


def _headers(session_id: str = "", *, token: str = AUTH_FIXTURE, origin: str = ""):
    headers = {
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
    }
    if session_id:
        headers["MCP-Session-Id"] = session_id
    if origin:
        headers["Origin"] = origin
    return headers


def _initialize(client: TestClient) -> str:
    response = client.post(
        "/mcp",
        headers=_headers(),
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "mempalace-test", "version": "1"},
            },
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["result"]["serverInfo"]["name"] == "mempalace"
    session_id = response.headers["mcp-session-id"]

    initialized = client.post(
        "/mcp",
        headers=_headers(session_id),
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    assert initialized.status_code == 202
    return session_id


def _post_call(client: TestClient, session_id: str, request_id: int, value: int):
    return client.post(
        "/mcp",
        headers=_headers(session_id),
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": "test_tool", "arguments": {"value": value}},
        },
    )


def test_initialize_list_and_call_over_streamable_http():
    app = create_http_app(
        auth_token=AUTH_FIXTURE,
        tools=_registry(lambda value=0: {"value": value}),
    )

    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        session_id = _initialize(client)

        listed = client.post(
            "/mcp",
            headers=_headers(session_id),
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        assert listed.status_code == 200
        assert [tool["name"] for tool in listed.json()["result"]["tools"]] == ["test_tool"]

        called = _post_call(client, session_id, 3, 7)
        assert called.status_code == 200
        content = json.loads(called.json()["result"]["content"][0]["text"])
        assert content == {"value": 7}


def test_authenticated_healthz_preserves_operator_probe_contract():
    app = create_http_app(
        auth_token=AUTH_FIXTURE,
        tools=_registry(lambda value=0: {"value": value}),
    )

    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        missing = client.get("/healthz")
        wrong = client.get("/healthz", headers={"Authorization": "Bearer wrong"})
        healthy = client.get("/healthz", headers={"Authorization": f"Bearer {AUTH_FIXTURE}"})

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert healthy.status_code == 200
    assert healthy.json()["status"] == "ok"
    assert healthy.json()["transport"] == "native-http"


def test_authenticated_memsys_identity_is_startup_bound_and_fail_closed_for_corpus():
    app = create_http_app(auth_token=AUTH_FIXTURE, tools=_registry(lambda _: {}))
    with TestClient(app) as client:
        missing = client.get("/__memsys/identity")
        identity = client.get(
            "/__memsys/identity", headers={"Authorization": f"Bearer {AUTH_FIXTURE}"}
        )

    assert missing.status_code == 401
    assert identity.status_code == 200
    payload = identity.json()
    assert payload["schema"] == "memsys-stack-identity/v1"
    assert payload["service"] == "mempalace"
    assert payload["revision"].startswith("sha256:")
    assert payload["serviceRevision"].startswith("version:")
    assert payload["corpusGeneration"] == {
        "schema": "mempalace-corpus-generation/v1",
        "status": "unavailable",
        "corpusRevision": None,
        "scope": "none",
        "capturedAtUtc": None,
    }
    serialized = json.dumps(payload)
    assert AUTH_FIXTURE not in serialized
    assert "mempalace\\" not in serialized.casefold()


def test_authenticated_memsys_identity_exposes_only_validated_evaluation_manifest(monkeypatch):
    data_plane_id = "sha256:" + "d" * 64
    monkeypatch.setenv("MEMSYS_MEMPALACE_DATA_PLANE_ID", data_plane_id)
    manifest = _evaluation_manifest(data_plane_id)
    app = create_http_app(
        auth_token=AUTH_FIXTURE,
        tools=_registry(lambda _: {}),
        evaluation_corpus_manifest=manifest,
    )

    with TestClient(app) as client:
        payload = client.get(
            "/__memsys/identity", headers={"Authorization": f"Bearer {AUTH_FIXTURE}"}
        ).json()

    assert payload["dataPlaneId"] == data_plane_id
    assert payload["corpusGeneration"] == {
        "schema": "mempalace-corpus-generation/v1",
        "status": "complete",
        "corpusRevision": manifest["corpusRevision"],
        "scope": "evaluation-manifest",
        "capturedAtUtc": "2026-07-29T14:00:00Z",
        "itemCount": 42,
        "inventorySha256": "sha256:" + "a" * 64,
    }
    serialized = json.dumps(payload)
    assert AUTH_FIXTURE not in serialized
    assert "sourceRevision" not in serialized


def test_evaluation_manifest_data_plane_or_digest_mismatch_fails_startup(monkeypatch):
    data_plane_id = "sha256:" + "d" * 64
    monkeypatch.setenv("MEMSYS_MEMPALACE_DATA_PLANE_ID", data_plane_id)
    manifest = _evaluation_manifest(data_plane_id)
    manifest["itemCount"] = 43

    with pytest.raises(ValueError, match="corpusRevision"):
        create_http_app(
            auth_token=AUTH_FIXTURE,
            tools=_registry(lambda _: {}),
            evaluation_corpus_manifest=manifest,
        )


def test_http_catalog_matches_stdio_catalog():
    app = create_http_app(auth_token=AUTH_FIXTURE)
    from mempalace.mcp_server import TOOLS

    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        session_id = _initialize(client)
        listed = client.post(
            "/mcp",
            headers=_headers(session_id),
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )

    assert listed.status_code == 200
    assert [tool["name"] for tool in listed.json()["result"]["tools"]] == list(TOOLS)


def test_official_client_over_ephemeral_loopback_socket():
    app = create_http_app(
        auth_token=AUTH_FIXTURE,
        tools=_registry(lambda value=0: {"value": value}),
    )
    server, thread, listener, port = _start_test_server(app)

    async def run_client():
        headers = {"Authorization": f"Bearer {AUTH_FIXTURE}"}
        async with httpx.AsyncClient(headers=headers) as http_client:
            async with streamable_http_client(
                f"http://127.0.0.1:{port}/mcp", http_client=http_client
            ) as streams:
                read_stream, write_stream, _ = streams
                async with ClientSession(read_stream, write_stream) as session:
                    initialized = await asyncio.wait_for(
                        session.initialize(), timeout=MCP_OPERATION_TIMEOUT_SECONDS
                    )
                    listed = await asyncio.wait_for(
                        session.list_tools(), timeout=MCP_OPERATION_TIMEOUT_SECONDS
                    )
                    called = await asyncio.wait_for(
                        session.call_tool("test_tool", {"value": 9}),
                        timeout=MCP_OPERATION_TIMEOUT_SECONDS,
                    )
                    return initialized, listed, called

    try:
        initialized, listed, called = _run_async_bounded(run_client)
        assert initialized.serverInfo.name == "mempalace"
        assert [tool.name for tool in listed.tools] == ["test_tool"]
        assert json.loads(called.content[0].text) == {"value": 9}
    finally:
        _stop_test_server(server, thread, listener)


def test_auth_and_origin_fail_closed():
    app = create_http_app(
        auth_token=AUTH_FIXTURE,
        tools=_registry(lambda value=0: {"value": value}),
    )
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "mempalace-test", "version": "1"},
        },
    }

    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        missing = client.post(
            "/mcp",
            headers={"Accept": "application/json, text/event-stream"},
            json=initialize,
        )
        assert missing.status_code == 401
        assert missing.headers["www-authenticate"] == "Bearer"

        wrong = client.post("/mcp", headers=_headers(token="wrong"), json=initialize)
        assert wrong.status_code == 401

        duplicate = client.post(
            "/mcp",
            headers=[
                ("Accept", "application/json, text/event-stream"),
                ("Authorization", f"Bearer {AUTH_FIXTURE}"),
                ("Authorization", f"Bearer {AUTH_FIXTURE}"),
            ],
            json=initialize,
        )
        assert duplicate.status_code == 401

        hostile_origin = client.post(
            "/mcp",
            headers=_headers(origin="https://attacker.example"),
            json=initialize,
        )
        assert hostile_origin.status_code == 403

        local_origin = client.post(
            "/mcp",
            headers=_headers(origin="http://127.0.0.1:3000"),
            json=initialize,
        )
        assert local_origin.status_code == 200

        hostile_host = client.post(
            "/mcp",
            headers={**_headers(), "Host": "attacker.example"},
            json=initialize,
        )
        assert hostile_host.status_code == 421


@pytest.mark.parametrize(
    "authorization",
    [b"Bearer \xff", b"Bearer token with spaces", b"Basic dGVzdA=="],
)
def test_raw_malformed_or_non_ascii_authorization_returns_401(authorization):
    downstream_called = False
    messages = []

    async def downstream(_scope, _receive, _send):
        nonlocal downstream_called
        downstream_called = True

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/mcp",
        "raw_path": b"/mcp",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"authorization", authorization)],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8787),
    }

    asyncio.run(BearerTokenMiddleware(downstream, AUTH_FIXTURE)(scope, receive, send))

    assert downstream_called is False
    start = next(message for message in messages if message["type"] == "http.response.start")
    assert start["status"] == 401


def test_origin_parser_rejects_every_malformed_or_ambiguous_value():
    app = create_http_app(
        auth_token=AUTH_FIXTURE,
        tools=_registry(lambda value=0: {"value": value}),
    )
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "mempalace-test", "version": "1"},
        },
    }
    malformed = [
        "",
        "null",
        "ftp://localhost",
        "http://user@localhost",
        "http://localhost:abc",
        "http://localhost:0",
        "http://localhost:65536",
        "http://localhost/path",
        "http://localhost/",
        "http://localhost?query=1",
        "http://localhost?",
        "http://localhost#fragment",
        "http://localhost#",
        "http://localhost:",
        "http://[::1]:",
        "http://user:password@localhost",
        "http://localhost\\attacker.example",
        "http://%6cocalhost",
        "http://127.0.0.1.evil.example",
        "http://2130706433",
        "http://localhost,http://attacker.example",
    ]

    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        for origin in malformed:
            headers = _headers(origin=origin)
            if origin == "":
                headers["Origin"] = ""
            response = client.post("/mcp", headers=headers, json=initialize)
            assert response.status_code == 403, origin
            assert response.json() == {"error": "invalid_origin"}

        duplicate = client.post(
            "/mcp",
            headers=[
                ("Accept", "application/json, text/event-stream"),
                ("Authorization", f"Bearer {AUTH_FIXTURE}"),
                ("MCP-Protocol-Version", PROTOCOL_VERSION),
                ("Origin", "http://localhost:3000"),
                ("Origin", "http://localhost:3000"),
            ],
            json=initialize,
        )
        assert duplicate.status_code == 403
        assert duplicate.json() == {"error": "invalid_origin"}

        secure_loopback = client.post(
            "/mcp",
            headers=_headers(origin="https://[::1]:8787"),
            json=initialize,
        )
        assert secure_loopback.status_code == 200


def test_session_delete_cleans_up_and_rejects_reuse():
    app = create_http_app(
        auth_token=AUTH_FIXTURE,
        tools=_registry(lambda value=0: {"value": value}),
    )

    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        session_id = _initialize(client)
        deleted = client.delete("/mcp", headers=_headers(session_id))
        assert deleted.status_code == 200
        manager = app.state.mcp_session_manager
        assert session_id not in manager._server_instances
        assert session_id not in manager._session_owners
        assert session_id not in manager._session_last_activity
        assert session_id not in manager._session_active_requests

        reused = client.post(
            "/mcp",
            headers=_headers(session_id),
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        assert reused.status_code == 404


def test_explicit_cancellation_reaches_in_flight_tool_handler():
    started = threading.Event()
    cancelled = threading.Event()

    async def wait_until_cancelled(_name, _arguments):
        started.set()
        try:
            await anyio.sleep_forever()
        finally:
            cancelled.set()

    app = create_http_app(
        auth_token=AUTH_FIXTURE,
        tools=_registry(lambda value=0: {"value": value}),
        runner=wait_until_cancelled,
    )

    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        session_id = _initialize(client)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(_post_call, client, session_id, 41, 1)
            assert started.wait(timeout=3)

            response = client.post(
                "/mcp",
                headers=_headers(session_id),
                json={
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {"requestId": 41, "reason": "unit test"},
                },
            )
            assert response.status_code == 202
            assert cancelled.wait(timeout=3)
            assert pending.result(timeout=3).status_code == 200


def test_cancelled_sync_handler_keeps_backend_gate_until_worker_exits():
    first_started = threading.Event()
    first_can_exit = threading.Event()
    second_started = threading.Event()
    lock = threading.Lock()
    side_effects = []
    active = 0
    maximum_active = 0

    def serial_handler(value=0):
        nonlocal active, maximum_active
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
            side_effects.append(f"{value}:start")

        try:
            if value == 1:
                first_started.set()
                if not first_can_exit.wait(timeout=5):
                    raise TimeoutError("test did not release the first handler")
            else:
                second_started.set()
            return {"value": value}
        finally:
            with lock:
                side_effects.append(f"{value}:end")
                active -= 1

    app = create_http_app(
        auth_token=AUTH_FIXTURE,
        tools=_registry(serial_handler),
        max_concurrency=1,
    )

    try:
        with TestClient(app, base_url="http://127.0.0.1:8787") as client:
            session_id = _initialize(client)
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(_post_call, client, session_id, 51, 1)
                assert first_started.wait(timeout=3)

                cancelled = client.post(
                    "/mcp",
                    headers=_headers(session_id),
                    json={
                        "jsonrpc": "2.0",
                        "method": "notifications/cancelled",
                        "params": {"requestId": 51, "reason": "unit test"},
                    },
                )
                assert cancelled.status_code == 202

                second = pool.submit(_post_call, client, session_id, 52, 2)
                assert not second_started.wait(timeout=0.5)

                first_can_exit.set()
                assert first.result(timeout=3).status_code == 200
                assert second.result(timeout=3).status_code == 200
    finally:
        first_can_exit.set()

    assert maximum_active == 1
    assert side_effects == ["1:start", "1:end", "2:start", "2:end"]


def test_concurrent_calls_are_bounded():
    lock = threading.Lock()
    active = 0
    maximum_active = 0

    def bounded_handler(value=0):
        nonlocal active, maximum_active
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
        try:
            time.sleep(0.08)
            return {"value": value}
        finally:
            with lock:
                active -= 1

    app = create_http_app(
        auth_token=AUTH_FIXTURE,
        tools=_registry(bounded_handler),
        max_concurrency=2,
    )

    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        session_id = _initialize(client)
        with ThreadPoolExecutor(max_workers=6) as pool:
            responses = list(
                pool.map(
                    lambda item: _post_call(client, session_id, item + 10, item),
                    range(6),
                )
            )

    assert all(response.status_code == 200 for response in responses)
    assert maximum_active == 2


def test_four_concurrent_http_status_calls_use_real_dispatch_and_single_probe(
    monkeypatch, config, kg
):
    from mempalace import mcp_server

    monkeypatch.setattr(mcp_server, "_config", config)
    monkeypatch.setattr(mcp_server, "_kg", kg)
    monkeypatch.setattr(mcp_server, "_vector_disabled", False)
    monkeypatch.setattr(mcp_server, "_vector_disabled_reason", "")
    monkeypatch.setattr(
        mcp_server,
        "hnsw_capacity_status",
        lambda *_args, **_kwargs: {
            "diverged": False,
            "status": "ok",
            "message": "warm disposable capacity",
            "sqlite_count": 0,
            "hnsw_count": 0,
            "divergence": 0,
        },
    )
    mcp_server._invalidate_hnsw_capacity_probe_cache()
    disposable_collection = mcp_server._get_collection(create=True, with_embedding=False)
    assert disposable_collection is not None
    assert disposable_collection.count() == 0

    calls = 0
    calls_lock = threading.Lock()
    dispatch_barrier = threading.Barrier(4)
    probe_started = threading.Event()
    release_probe = threading.Event()

    def slow_probe(*_args, **_kwargs):
        nonlocal calls
        with calls_lock:
            calls += 1
        probe_started.set()
        assert release_probe.wait(timeout=3)
        return {
            "diverged": False,
            "status": "ok",
            "message": "concurrent disposable capacity",
            "sqlite_count": 0,
            "hnsw_count": 0,
            "divergence": 0,
        }

    monkeypatch.setattr(mcp_server, "hnsw_capacity_status", slow_probe)
    monkeypatch.setattr(
        mcp_server,
        "_file_cache_identity",
        lambda path: ("stable-disposable-file", path, 0, 0),
    )
    mcp_server._invalidate_hnsw_capacity_probe_cache()

    def synchronized_real_status():
        dispatch_barrier.wait(timeout=3)
        return mcp_server.tool_status()

    tools = {name: dict(spec) for name, spec in mcp_server.TOOLS.items()}
    tools["mempalace_status"]["handler"] = synchronized_real_status
    app = create_http_app(
        auth_token=AUTH_FIXTURE,
        tools=tools,
        max_concurrency=4,
    )
    server, thread, listener, port = _start_test_server(app)

    async def run_clients():
        headers = {"Authorization": f"Bearer {AUTH_FIXTURE}"}
        async with httpx.AsyncClient(headers=headers) as http_client:
            async with AsyncExitStack() as stack:
                sessions = []
                for _ in range(4):
                    read_stream, write_stream, _ = await stack.enter_async_context(
                        streamable_http_client(
                            f"http://127.0.0.1:{port}/mcp",
                            http_client=http_client,
                        )
                    )
                    session = await stack.enter_async_context(
                        ClientSession(read_stream, write_stream)
                    )
                    await asyncio.wait_for(
                        session.initialize(), timeout=MCP_OPERATION_TIMEOUT_SECONDS
                    )
                    sessions.append(session)

                pending = [
                    asyncio.create_task(session.call_tool("mempalace_status", {}))
                    for session in sessions
                ]
                started = await asyncio.to_thread(probe_started.wait, 3)
                await asyncio.sleep(0.1)
                calls_before_release = calls
                release_probe.set()
                results = await asyncio.wait_for(
                    asyncio.gather(*pending),
                    timeout=MCP_OPERATION_TIMEOUT_SECONDS,
                )
                return started, calls_before_release, results

    try:
        started, calls_before_release, results = _run_async_bounded(run_clients)
    finally:
        release_probe.set()
        _stop_test_server(server, thread, listener)
        mcp_server._invalidate_hnsw_capacity_probe_cache()

    assert started
    assert calls_before_release == 1
    payloads = [json.loads(result.content[0].text) for result in results]
    assert calls == 1
    assert [payload["total_drawers"] for payload in payloads] == [0, 0, 0, 0]
    assert all("vector_disabled" not in payload for payload in payloads)


def test_low_level_sdk_repeats_30_sequential_sessions_without_deadlock():
    app = create_http_app(
        auth_token=AUTH_FIXTURE,
        tools=_registry(lambda value=0: {"value": value}),
    )

    server, thread, listener, port = _start_test_server(app)

    async def run_sessions():
        headers = {"Authorization": f"Bearer {AUTH_FIXTURE}"}
        async with httpx.AsyncClient(headers=headers) as http_client:

            async def run_one(index):
                async with streamable_http_client(
                    f"http://127.0.0.1:{port}/mcp",
                    http_client=http_client,
                    terminate_on_close=True,
                ) as streams:
                    read_stream, write_stream, get_session_id = streams
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        called = await session.call_tool("test_tool", {"value": index})
                        assert get_session_id()
                        assert json.loads(called.content[0].text) == {"value": index}

            for index in range(30):
                await asyncio.wait_for(
                    run_one(index),
                    timeout=MCP_OPERATION_TIMEOUT_SECONDS,
                )
                assert not app.state.mcp_session_manager._server_instances

    try:
        _run_async_bounded(run_sessions)
    finally:
        _stop_test_server(server, thread, listener)


def test_hard_session_cap_rejects_new_sessions_until_delete_releases_capacity():
    app = create_http_app(
        auth_token=AUTH_FIXTURE,
        tools=_registry(lambda value=0: {"value": value}),
        max_sessions=1,
        session_idle_seconds=60,
    )
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "mempalace-test", "version": "1"},
        },
    }

    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        first_session = _initialize(client)
        rejected = client.post("/mcp", headers=_headers(), json=initialize)
        assert rejected.status_code == 503
        assert rejected.json() == {"error": "session_capacity_reached"}

        assert client.delete("/mcp", headers=_headers(first_session)).status_code == 200
        second_session = _initialize(client)
        assert second_session != first_session


def test_active_aware_cleanup_expires_abandoned_but_never_active_calls():
    started = threading.Event()
    release = threading.Event()

    async def wait_for_release(_name, _arguments):
        started.set()
        while not release.is_set():
            await anyio.sleep(0.01)
        return {"released": True}

    app = create_http_app(
        auth_token=AUTH_FIXTURE,
        tools=_registry(lambda value=0: {"value": value}),
        runner=wait_for_release,
        session_idle_seconds=10,
    )
    manager = app.state.mcp_session_manager

    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        session_id = _initialize(client)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(_post_call, client, session_id, 71, 1)
            assert started.wait(timeout=3)
            client.portal.call(
                lambda: manager._session_last_activity.__setitem__(
                    session_id, time.monotonic() - 100
                )
            )
            assert client.portal.call(manager.cleanup_expired_sessions) == ()
            assert session_id in manager._server_instances
            release.set()
            assert pending.result(timeout=3).status_code == 200

        client.portal.call(
            lambda: manager._session_last_activity.__setitem__(session_id, time.monotonic() - 100)
        )
        assert client.portal.call(manager.cleanup_expired_sessions) == (session_id,)
        assert session_id not in manager._server_instances
        assert session_id not in manager._session_owners
        assert session_id not in manager._session_last_activity
        assert session_id not in manager._session_active_requests
        assert _post_call(client, session_id, 72, 2).status_code == 404


def test_background_reaper_expires_an_abandoned_session():
    app = create_http_app(
        auth_token=AUTH_FIXTURE,
        tools=_registry(lambda value=0: {"value": value}),
        session_idle_seconds=0.05,
    )
    manager = app.state.mcp_session_manager

    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        session_id = _initialize(client)
        deadline = time.monotonic() + 2
        while session_id in manager._server_instances and time.monotonic() < deadline:
            time.sleep(0.01)
        assert session_id not in manager._server_instances
        assert _post_call(client, session_id, 73, 3).status_code == 404


def test_failed_termination_tombstone_retries_after_bounded_cooldown():
    class FailingTransport:
        is_terminated = False

        def __init__(self):
            self.calls = 0

        async def terminate(self):
            self.calls += 1
            raise RuntimeError("simulated terminate failure")

    manager = object.__new__(mcp_http_module.ActiveAwareSessionManager)
    manager.max_sessions = 1
    manager.session_idle_seconds = 60
    manager.termination_max_attempts = 2
    manager.termination_retry_base_seconds = 60
    manager.termination_timeout_seconds = 1
    manager._server_instances = {}
    manager._session_owners = {}
    manager._session_last_activity = {}
    manager._session_active_requests = {}
    manager._termination_tombstones = {}
    manager._pending_session_creations = 0
    transport = FailingTransport()
    response_messages = []

    async def exercise():
        manager._session_lifecycle_lock = anyio.Lock()
        manager._server_instances["failed-termination"] = transport
        manager._session_last_activity["failed-termination"] = time.monotonic() - 100
        expired = await manager.cleanup_expired_sessions()

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            response_messages.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/mcp",
            "raw_path": b"/mcp",
            "query_string": b"",
            "root_path": "",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8787),
        }
        await manager._handle_new_session_with_capacity(scope, receive, send)
        manager._termination_tombstones["failed-termination"].next_retry_at = 0
        second_retry = await manager.cleanup_expired_sessions()
        manager._termination_tombstones["failed-termination"].next_retry_at = 0
        cooldown_retry = await manager.cleanup_expired_sessions()
        return expired, second_retry, cooldown_retry

    expired, second_retry, cooldown_retry = asyncio.run(exercise())

    assert expired == ("failed-termination",)
    assert second_retry == ()
    assert cooldown_retry == ()
    assert transport.calls == 3
    tombstone = manager._termination_tombstones["failed-termination"]
    assert tombstone.attempts == 3
    assert tombstone.next_retry_at > time.monotonic()
    assert manager._accounted_session_count_locked() == manager.max_sessions
    assert "failed-termination" not in manager._server_instances
    start = next(
        message for message in response_messages if message["type"] == "http.response.start"
    )
    assert start["status"] == 503
    body = next(message for message in response_messages if message["type"] == "http.response.body")
    assert json.loads(body["body"]) == {"error": "session_capacity_reached"}


def test_hanging_termination_attempts_timeout_concurrently_and_keep_capacity_closed():
    class HangingTransport:
        is_terminated = False

        def __init__(self):
            self.calls = 0

        async def terminate(self):
            self.calls += 1
            await anyio.sleep_forever()

    manager = object.__new__(mcp_http_module.ActiveAwareSessionManager)
    manager.max_sessions = 2
    manager.session_idle_seconds = 60
    manager.termination_max_attempts = 2
    manager.termination_retry_base_seconds = 60
    manager.termination_timeout_seconds = 0.05
    manager._server_instances = {}
    manager._session_owners = {}
    manager._session_last_activity = {}
    manager._session_active_requests = {}
    manager._termination_tombstones = {}
    manager._pending_session_creations = 0
    transports = {
        "hanging-a": HangingTransport(),
        "hanging-b": HangingTransport(),
    }

    async def exercise():
        manager._session_lifecycle_lock = anyio.Lock()
        for session_id, transport in transports.items():
            manager._server_instances[session_id] = transport
            manager._session_last_activity[session_id] = time.monotonic() - 100

        started = time.monotonic()
        expired = await asyncio.wait_for(manager.cleanup_expired_sessions(), timeout=1)
        first_elapsed = time.monotonic() - started
        for tombstone in manager._termination_tombstones.values():
            tombstone.next_retry_at = 0

        started = time.monotonic()
        retried = await asyncio.wait_for(manager.cleanup_expired_sessions(), timeout=1)
        second_elapsed = time.monotonic() - started
        for tombstone in manager._termination_tombstones.values():
            tombstone.next_retry_at = 0

        started = time.monotonic()
        cooldown_retried = await asyncio.wait_for(manager.cleanup_expired_sessions(), timeout=1)
        third_elapsed = time.monotonic() - started
        return expired, retried, cooldown_retried, first_elapsed, second_elapsed, third_elapsed

    expired, retried, cooldown_retried, first_elapsed, second_elapsed, third_elapsed = asyncio.run(
        exercise()
    )

    assert set(expired) == set(transports)
    assert retried == ()
    assert cooldown_retried == ()
    assert first_elapsed < 0.5
    assert second_elapsed < 0.5
    assert third_elapsed < 0.5
    assert manager._accounted_session_count_locked() == manager.max_sessions
    assert not manager._server_instances
    for session_id, transport in transports.items():
        assert transport.calls == 3
        tombstone = manager._termination_tombstones[session_id]
        assert tombstone.attempts == manager.termination_max_attempts + 1
        assert tombstone.in_flight is False
        assert math.isfinite(tombstone.next_retry_at)
        assert tombstone.next_retry_at > time.monotonic()


def test_shutdown_drain_retries_retained_tombstone_until_success():
    class EventuallyTerminates:
        is_terminated = False

        def __init__(self):
            self.calls = 0

        async def terminate(self):
            self.calls += 1
            if self.calls < 3:
                raise RuntimeError("not yet")
            self.is_terminated = True

    manager = object.__new__(mcp_http_module.ActiveAwareSessionManager)
    manager.termination_max_attempts = 3
    manager.termination_retry_base_seconds = 60
    manager.termination_timeout_seconds = 1
    manager._termination_tombstones = {}
    transport = EventuallyTerminates()

    async def exercise():
        manager._session_lifecycle_lock = anyio.Lock()
        manager._termination_tombstones["retained"] = mcp_http_module._TerminationTombstone(
            transport=transport
        )
        await manager._drain_termination_tombstones_on_shutdown()

    asyncio.run(exercise())

    assert transport.calls == 3
    assert manager._termination_tombstones == {}


def test_shutdown_drain_fails_closed_when_retained_tombstone_never_terminates():
    class NeverTerminates:
        is_terminated = False

        def __init__(self):
            self.calls = 0

        async def terminate(self):
            self.calls += 1
            await anyio.sleep_forever()

    manager = object.__new__(mcp_http_module.ActiveAwareSessionManager)
    manager.termination_max_attempts = 2
    manager.termination_retry_base_seconds = 60
    manager.termination_timeout_seconds = 0.01
    manager._termination_tombstones = {}
    transport = NeverTerminates()

    async def exercise():
        manager._session_lifecycle_lock = anyio.Lock()
        manager._termination_tombstones["retained"] = mcp_http_module._TerminationTombstone(
            transport=transport
        )
        with pytest.raises(RuntimeError, match="could not terminate 1 retained session"):
            await manager._drain_termination_tombstones_on_shutdown()

    asyncio.run(exercise())

    assert transport.calls == 2
    assert "retained" in manager._termination_tombstones


def test_cancelled_termination_clears_in_flight_before_shutdown_drain():
    class CancelThenTerminate:
        is_terminated = False

        def __init__(self):
            self.calls = 0

        async def terminate(self):
            self.calls += 1
            if self.calls == 1:
                await anyio.sleep_forever()
            self.is_terminated = True

    manager = object.__new__(mcp_http_module.ActiveAwareSessionManager)
    manager.termination_max_attempts = 2
    manager.termination_retry_base_seconds = 60
    manager.termination_timeout_seconds = 10
    manager._termination_tombstones = {}
    transport = CancelThenTerminate()

    async def exercise():
        manager._session_lifecycle_lock = anyio.Lock()
        manager._termination_tombstones["retained"] = mcp_http_module._TerminationTombstone(
            transport=transport,
            in_flight=True,
        )
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(manager._terminate_transport, "retained", transport)
            while transport.calls == 0:
                await anyio.sleep(0)
            task_group.cancel_scope.cancel()
        assert manager._termination_tombstones["retained"].in_flight is False
        await manager._drain_termination_tombstones_on_shutdown()

    asyncio.run(exercise())

    assert transport.calls == 2
    assert manager._termination_tombstones == {}


def test_token_resolution_accepts_either_name_or_matching_values():
    assert resolve_auth_token({"MEMPALACE_MCP_TOKEN": "native"}) == (
        "native",
        "MEMPALACE_MCP_TOKEN",
    )
    assert resolve_auth_token({"MEMSYS_MEMPALACE_TOKEN": "existing"}) == (
        "existing",
        "MEMSYS_MEMPALACE_TOKEN",
    )
    assert resolve_auth_token(
        {"MEMPALACE_MCP_TOKEN": "same", "MEMSYS_MEMPALACE_TOKEN": "same"}
    ) == ("same", "MEMPALACE_MCP_TOKEN")


def test_token_resolution_fails_closed_on_conflict_or_missing_token():
    with pytest.raises(RuntimeError, match="both set to different values"):
        resolve_auth_token({"MEMPALACE_MCP_TOKEN": "native", "MEMSYS_MEMPALACE_TOKEN": "old"})
    with pytest.raises(RuntimeError, match="MEMPALACE_MCP_TOKEN"):
        resolve_auth_token({})


@pytest.mark.parametrize("token", ["non-ascii-\u00e9", "contains a space", "\t"])
def test_token_resolution_rejects_non_header_safe_configured_values(token):
    with pytest.raises(RuntimeError, match="header-safe token68"):
        resolve_auth_token({"MEMPALACE_MCP_TOKEN": token})


def test_pinned_sdk_idle_deadline_is_replaced_by_active_aware_cleanup():
    app = create_http_app(
        auth_token=AUTH_FIXTURE,
        tools=_registry(lambda value=0: {"value": value}),
    )
    manager = app.state.mcp_session_manager

    assert version("mcp") == "1.28.1"
    assert DEFAULT_SESSION_IDLE_SECONDS == 300
    assert DEFAULT_MAX_SESSIONS == 64
    assert manager.stateless is False
    assert manager.session_idle_seconds == 300
    assert manager.max_sessions == 64
    assert manager.termination_timeout_seconds == DEFAULT_TERMINATION_TIMEOUT_SECONDS == 5
    assert manager.session_idle_timeout is None

    for invalid_timeout in (0, -1, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="must be positive"):
            create_http_app(
                auth_token=AUTH_FIXTURE,
                tools=_registry(lambda value=0: {"value": value}),
                session_idle_seconds=invalid_timeout,
            )


def test_sdk_runtime_gate_rejects_unreviewed_version(monkeypatch):
    monkeypatch.setattr(mcp_http_module, "package_version", lambda _name: "1.29.0")
    with pytest.raises(RuntimeError, match="mcp==1.28.1"):
        create_http_app(
            auth_token=AUTH_FIXTURE,
            tools=_registry(lambda value=0: {"value": value}),
        )


def test_pending_session_reservation_does_not_block_existing_requests(monkeypatch):
    app = create_http_app(
        auth_token=AUTH_FIXTURE,
        tools=_registry(lambda value=0: {"value": value}),
        max_sessions=2,
        session_idle_seconds=60,
    )
    manager = app.state.mcp_session_manager
    initialize = {
        "jsonrpc": "2.0",
        "id": 81,
        "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "pending-test", "version": "1"},
        },
    }

    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        existing_session = _initialize(client)
        original_handle_request = mcp_http_module.StreamableHTTPSessionManager.handle_request
        pending_started = threading.Event()
        release_pending = threading.Event()

        async def delayed_new_session(self, scope, receive, send):
            session_headers = [
                value for key, value in scope.get("headers", []) if key.lower() == b"mcp-session-id"
            ]
            if not session_headers:
                pending_started.set()
                await anyio.to_thread.run_sync(release_pending.wait)
            return await original_handle_request(self, scope, receive, send)

        monkeypatch.setattr(
            mcp_http_module.StreamableHTTPSessionManager,
            "handle_request",
            delayed_new_session,
        )
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(
                    client.post,
                    "/mcp",
                    headers=_headers(),
                    json=initialize,
                )
                assert pending_started.wait(timeout=3)
                assert manager._pending_session_creations == 1

                existing = client.post(
                    "/mcp",
                    headers=_headers(existing_session),
                    json={"jsonrpc": "2.0", "id": 82, "method": "tools/list", "params": {}},
                )
                assert existing.status_code == 200

                rejected = client.post("/mcp", headers=_headers(), json=initialize)
                assert rejected.status_code == 503
                assert rejected.json() == {"error": "session_capacity_reached"}

                release_pending.set()
                created = pending.result(timeout=5)
                assert created.status_code == 200
        finally:
            release_pending.set()
        assert manager._pending_session_creations == 0


def test_delete_racing_active_request_cannot_resurrect_session_maps():
    started = threading.Event()
    release = threading.Event()

    async def wait_for_release(_name, _arguments):
        started.set()
        while not release.is_set():
            await anyio.sleep(0.01)
        return {"released": True}

    app = create_http_app(
        auth_token=AUTH_FIXTURE,
        tools=_registry(lambda value=0: {"value": value}),
        runner=wait_for_release,
    )
    manager = app.state.mcp_session_manager

    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        session_id = _initialize(client)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(_post_call, client, session_id, 91, 1)
            assert started.wait(timeout=3)
            deleted = client.delete("/mcp", headers=_headers(session_id))
            assert deleted.status_code == 200
            release.set()
            try:
                pending.result(timeout=5)
            except Exception:
                pass

        assert session_id not in manager._server_instances
        assert session_id not in manager._session_owners
        assert session_id not in manager._session_last_activity
        assert session_id not in manager._session_active_requests
        assert _post_call(client, session_id, 92, 2).status_code == 404


def test_pending_initial_request_is_not_treated_as_idle():
    class PendingTransport:
        is_terminated = False

        async def terminate(self):
            self.is_terminated = True

    app = create_http_app(
        auth_token=AUTH_FIXTURE,
        tools=_registry(lambda value=0: {"value": value}),
        session_idle_seconds=60,
    )
    manager = app.state.mcp_session_manager
    transport = PendingTransport()

    async def exercise_pending_state():
        async with manager._session_lifecycle_lock:
            manager._pending_session_creations = 1
            manager._server_instances["pending-session"] = transport
        protected = await manager.cleanup_expired_sessions()
        async with manager._session_lifecycle_lock:
            manager._pending_session_creations = 0
            manager._session_last_activity["pending-session"] = time.monotonic() - 100
        expired = await manager.cleanup_expired_sessions()
        return protected, expired

    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        protected, expired = client.portal.call(exercise_pending_state)

    assert protected == ()
    assert expired == ("pending-session",)
    assert transport.is_terminated is True


def test_failed_precreation_cleanup_releases_session_reservation(monkeypatch):
    app = create_http_app(
        auth_token=AUTH_FIXTURE,
        tools=_registry(lambda value=0: {"value": value}),
    )
    manager = app.state.mcp_session_manager

    async def fail_cleanup(_sessions):
        raise RuntimeError("precreation cleanup failed")

    monkeypatch.setattr(manager, "_terminate_transports", fail_cleanup)
    initialize = {
        "jsonrpc": "2.0",
        "id": 101,
        "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "cleanup-test", "version": "1"},
        },
    }
    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        with pytest.raises(RuntimeError, match="precreation cleanup failed"):
            client.post("/mcp", headers=_headers(), json=initialize)
        assert manager._pending_session_creations == 0


def test_cli_defaults_to_loopback_and_serial_backend_calls():
    args = _parse_args([])
    assert args.host == DEFAULT_HOST == "127.0.0.1"
    assert args.max_concurrency == DEFAULT_MAX_CONCURRENCY == 1
    assert args.max_sessions == DEFAULT_MAX_SESSIONS == 64
    assert args.session_idle_seconds == DEFAULT_SESSION_IDLE_SECONDS == 300
    assert args.termination_timeout_seconds == DEFAULT_TERMINATION_TIMEOUT_SECONDS == 5
    with pytest.raises(SystemExit):
        _parse_args(["--host", "0.0.0.0"])
    assert _parse_args(["--session-idle-seconds", "5"]).session_idle_seconds == 5
    assert (
        _parse_args(["--termination-timeout-seconds", "0.25"]).termination_timeout_seconds == 0.25
    )


# --- memsys#458: hardened accept loop ---------------------------------------
#
# These never open a real listening socket or force a genuine WinError 64 --
# that would be flaky-by-construction and platform-specific. Instead they
# prove the two things that actually matter and are fully deterministic:
# (1) the classifier routes the exact asyncio accept-loop-death signature to
# a non-zero process exit and forwards every other exception context to
# asyncio's stock default handler unchanged, and (2) the handler is really
# the one installed on the loop `uvicorn.Server.serve()` runs on, not merely
# reachable in theory. `os._exit` is monkeypatched throughout so a matching
# test cannot terminate the pytest process itself.


class _RecordingLoop:
    """Minimal stand-in for asyncio.AbstractEventLoop's one method the
    handler calls on the non-matching path, so that path can be asserted
    without depending on a real event loop's internal state."""

    def __init__(self):
        self.default_handler_calls: list[dict] = []

    def default_exception_handler(self, context):
        self.default_handler_calls.append(context)


def test_accept_loop_handler_exits_nonzero_on_the_exact_accept_loop_death_signature(
    monkeypatch, caplog
):
    exit_calls = []
    monkeypatch.setattr(mcp_http_module.os, "_exit", lambda code: exit_calls.append(code))
    loop = _RecordingLoop()
    context = {
        "message": mcp_http_module.ACCEPT_LOOP_FAILURE_MESSAGE,
        "exception": OSError(64, "The specified network name is no longer available"),
        "socket": "fixture-socket-repr",
    }

    with caplog.at_level("CRITICAL", logger="mempalace_mcp_http"):
        mcp_http_module._handle_accept_loop_exception(loop, context)

    assert exit_calls == [1]
    assert loop.default_handler_calls == []
    assert any("memsys#458" in record.message for record in caplog.records)


def test_accept_loop_handler_forwards_every_other_exception_to_the_default_handler(monkeypatch):
    exit_calls = []
    monkeypatch.setattr(mcp_http_module.os, "_exit", lambda code: exit_calls.append(code))
    loop = _RecordingLoop()
    unrelated_context = {
        "message": "Exception in callback something_else()",
        "exception": RuntimeError("unrelated background task failure"),
    }

    mcp_http_module._handle_accept_loop_exception(loop, unrelated_context)

    assert exit_calls == []
    assert loop.default_handler_calls == [unrelated_context]


def test_accept_loop_handler_does_not_exit_when_message_is_merely_similar(monkeypatch):
    # Guards against a substring/fuzzy match accidentally widening the trigger
    # to something that merely mentions "accept" or "socket".
    exit_calls = []
    monkeypatch.setattr(mcp_http_module.os, "_exit", lambda code: exit_calls.append(code))
    loop = _RecordingLoop()
    almost_context = {"message": "Accept failed on a socket during shutdown"}

    mcp_http_module._handle_accept_loop_exception(loop, almost_context)

    assert exit_calls == []
    assert loop.default_handler_calls == [almost_context]


def test_serve_with_hardened_accept_loop_installs_the_real_handler_before_serving(monkeypatch):
    exit_calls = []
    monkeypatch.setattr(mcp_http_module.os, "_exit", lambda code: exit_calls.append(code))
    observed_handler = {}

    class _FakeServer:
        async def serve(self):
            loop = asyncio.get_running_loop()
            observed_handler["handler"] = loop.get_exception_handler()
            # Firing the handler THIS way (through the loop's own dispatch,
            # not by calling the function directly) proves the wiring, not
            # just the classifier logic already covered above.
            loop.call_exception_handler(
                {
                    "message": mcp_http_module.ACCEPT_LOOP_FAILURE_MESSAGE,
                    "exception": OSError(64, "fixture"),
                }
            )

    asyncio.run(mcp_http_module._serve_with_hardened_accept_loop(_FakeServer()))

    assert observed_handler["handler"] is mcp_http_module._handle_accept_loop_exception
    assert exit_calls == [1]


def test_run_uvicorn_with_hardened_accept_loop_wires_a_real_uvicorn_server(monkeypatch):
    import uvicorn

    served = []

    class _FakeServer:
        def __init__(self, config):
            served.append(config)

        async def serve(self):
            # Proves the handler is attached even through the full
            # uvicorn.Server(config) construction path, not only when a
            # server object is handed in directly.
            asyncio.get_running_loop().call_exception_handler(
                {
                    "message": mcp_http_module.ACCEPT_LOOP_FAILURE_MESSAGE,
                    "exception": OSError(64, "fixture"),
                }
            )

    exit_calls = []
    monkeypatch.setattr(mcp_http_module.os, "_exit", lambda code: exit_calls.append(code))
    monkeypatch.setattr(uvicorn, "Server", _FakeServer)

    fixture_config = object()
    mcp_http_module._run_uvicorn_with_hardened_accept_loop(fixture_config)

    assert served == [fixture_config]
    assert exit_calls == [1]


def test_healthz_does_not_recount_the_palace_on_every_probe(monkeypatch):
    """Regression: each probe ran a COUNT(*) over a 26.5 GB SQLite file.

    Measured live on 2026-09-01: 2.03s cold / 0.25s warm for 1,031,514
    drawers, against a 2.0s Router probe budget and a 5s bridge-watchdog
    budget. Both reported MemPalace down ("timed out" / "alive-timeout")
    while ``queryProof`` stayed ``proven`` -- the palace was serving queries
    the entire time. The count must still be served: the bridge watchdog
    downgrades a countless payload to ``alive-suspect``.
    """

    import mempalace.status as status_module

    calls = []

    def _counter(palace_path=None):
        calls.append(palace_path)
        return 4321

    monkeypatch.setattr(status_module, "get_fast_drawer_count", _counter)
    status_module.reset_drawer_count_cache()

    app = create_http_app(
        auth_token=AUTH_FIXTURE,
        tools=_registry(lambda value=0: {"value": value}),
    )
    headers = {"Authorization": f"Bearer {AUTH_FIXTURE}"}

    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        payloads = [client.get("/healthz", headers=headers) for _ in range(5)]

    status_module.reset_drawer_count_cache()

    assert [p.status_code for p in payloads] == [200] * 5
    for probe in payloads:
        body = probe.json()
        assert body["status"] == "ok"
        assert body["drawers"] == 4321
        assert body["drawerCount"] == 4321
    assert len(calls) == 1, f"expected one count across five probes, got {len(calls)}"
