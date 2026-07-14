# Native loopback Streamable HTTP MCP transport

## Decision

MemPalace has a native Streamable HTTP entry point built on the MCP Python SDK
1.x low-level `Server` and `StreamableHTTPSessionManager` APIs. The existing
stdio entry point remains supported, and both transports use the same tool
registry and argument dispatcher.

The HTTP command is `mempalace-mcp-http`. It requires Python 3.10+, binds to
`127.0.0.1:8787` by default, and has no literal bearer-token default. Startup
token resolution is deliberately unambiguous:

- `MEMPALACE_MCP_TOKEN` alone works.
- `MEMSYS_MEMPALACE_TOKEN` alone works for compatibility.
- If both are set to the same non-empty value, that value is accepted and the
  native variable name is reported as the source.
- If both are set to different non-empty values, startup fails closed.
- If neither contains a non-empty value, startup fails closed.
- Configured values must be non-empty ASCII `token68` values safe for an HTTP
  Authorization field. Whitespace, controls, non-ASCII text, and malformed
  values fail startup before an ASGI app is created.

The same bearer boundary protects `/mcp` and `/healthz`. The health endpoint
only proves that the authenticated native process is serving requests. MCP
initialize, list, and tool calls remain the functional readiness proof.

## Specification and local policy

The MCP 2025-11-25 Streamable HTTP specification uses different requirement
levels for its three local-server protections:

- Servers **MUST** validate `Origin`. If an `Origin` is present and invalid,
  the response must be HTTP 403.
- Local servers **SHOULD** bind only to localhost rather than all interfaces.
- Servers **SHOULD** authenticate all connections.

Those are protocol requirements and recommendations. MemPalace local policy is
intentionally stricter than the two recommendations:

- Raw ASGI middleware accepts at most one supplied `Origin`, requires an exact
  `http` or `https` URI with no userinfo/path/query/fragment, validates a
  numeric port in range when present, and accepts only `127.0.0.1`,
  `localhost`, or `::1`. Every duplicate, malformed, ambiguous, or non-loopback
  supplied value is rejected with HTTP 403 before SDK routing.
- SDK transport security independently retains loopback Host and Origin
  allowlists as defense in depth.
- SDK transport security also enforces a loopback `Host` allowlist as a local
  DNS-rebinding defense.
- The CLI accepts only `127.0.0.1`, `localhost`, or `::1` as bind hosts.
- Raw ASGI bearer middleware authenticates every HTTP route, including
  `/healthz`, and rejects missing, wrong, or duplicate Authorization headers.
  It compares validated expected bytes against the raw ASGI header bytes with
  `hmac.compare_digest`; malformed and non-ASCII wire values return HTTP 401
  rather than entering text decoding or raising a type error.

Loopback binding and bearer authentication are therefore enforced MemPalace
policy, not claims that the MCP specification labels them as MUST requirements.

## Synchronous cancellation boundary

Python cannot pre-empt a synchronous worker thread. AnyIO documents that
`to_thread.run_sync()` shields its waiter from cancellation by default, while
`abandon_on_cancel=True` releases the waiter and leaves the worker running with
its result ignored.

Using `abandon_on_cancel=False` directly is not compatible with the SDK 1.28.1
request lifecycle. The SDK marks the request completed when cancellation is
processed; when the shielded call later returns, the SDK can attempt a second
response and crash that session with `Request already responded to`.

MemPalace therefore uses the separately held permit design. It acquires a
backend capacity permit before starting the worker, transfers permit ownership
to that worker, and uses `abandon_on_cancel=True` only for the MCP waiter. The
worker releases the backend permit through the AnyIO event loop after the
synchronous handler exits. A cancellation racing before worker startup releases
the permit from the host and prevents dispatch. At the default
`max_concurrency=1`, a second palace call cannot begin while cancelled backend
side effects are still running. The protocol result may be discarded, but
backend work is not represented as rolled back or stopped. Higher concurrency
remains an explicit opt-in for a backend whose parallel behavior has been
separately proven.

## MCP SDK 1.28.1 session-lifecycle boundary

The project pins `mcp==1.28.1` in the HTTP extra and development dependencies,
and `create_http_app()` performs the same exact runtime compatibility check.
This is required because MemPalace deliberately coordinates the SDK's private
`_server_instances`, `_session_owners`, and `_session_creation_lock` state. An
unreviewed SDK version fails before an ASGI app is created. The test environment
uses MCP 1.28.1 and AnyIO 4.13.0.

In MCP SDK 1.28.1, `session_idle_timeout` does not distinguish an active request
from an idle session:

1. The SDK sets one deadline on a cancel scope around the session's entire
   `Server.run()` call.
2. An existing-session HTTP request moves that deadline when the request
   arrives, before `transport.handle_request()` completes.
3. There is no active-request counter and no completion-time deadline reset.
4. If a handler runs past the deadline, the session cancel scope can expire,
   remove the session, and terminate its transport while that request is still
   active.

The MCP specification requires a client receiving HTTP 404 for an expired
session to initialize a new session. That can create a retry window while a
non-preemptible synchronous handler from the expired session is still running.

MemPalace therefore keeps SDK idle expiry disabled by passing
`session_idle_timeout=None`, then owns lifecycle policy in
`ActiveAwareSessionManager`:

- each existing-session HTTP request increments an active counter before SDK
  dispatch and decrements it after the response path exits;
- a background reaper expires only sessions with zero active requests and no
  completed activity for five minutes by default;
- a hard default cap of 64 retained sessions rejects new initialization with
  HTTP 503 until expiry or explicit deletion releases capacity;
- pending initialization reservations count against that cap, while the local
  lifecycle lock is released before SDK request I/O so an initializing client
  cannot head-of-line block an established session;
- a successful MCP DELETE removes the terminated transport from the SDK
  `_server_instances` and `_session_owners` maps plus MemPalace activity maps
  after the response, and a concurrent request completion cannot recreate them;
- expiry moves a transport into a termination tombstone before removing its SDK
  maps. Failed termination remains counted against the hard session cap, retries
  in bounded exponential-backoff waves, enters a longer cooldown after each
  configured wave, and remains fail-closed while later cleanup cycles continue
  retrying rather than silently releasing capacity;
- termination attempts run concurrently and each supported SDK transport gets
  a five-second cooperative AnyIO deadline by default. A timeout is recorded as
  a failed attempt, leaves the tombstone counted against capacity, and follows
  the same bounded retry policy. This bounds DELETE, expiry, and pre-creation
  cleanup for the pinned cooperative SDK transport instead of allowing one
  stuck termination to block every new session indefinitely;
- expired or unknown session IDs continue to receive HTTP 404, requiring client
  reinitialization as specified by MCP.

The five-minute threshold, hard cap, and per-attempt termination deadline are
CLI-configurable with positive values. The timeout is a cooperative async
boundary under exact MCP SDK 1.28.1; it is not a claim that Python can forcibly
kill arbitrary cancellation-shielded third-party coroutine code. The shipped
CLI starts one Uvicorn process; session limits and maps are process-local, so an
externally embedded multi-worker ASGI deployment would multiply the cap and has
not been proven. This policy does not claim to cancel a synchronous backend
thread: backend permits remain owned until that worker exits, as described
above.

## FastMCP Windows deadlock control

MemPalace intentionally remains on the low-level SDK surface. The open upstream
[python-sdk #2653](https://github.com/modelcontextprotocol/python-sdk/issues/2653)
reports that `mcp.server.fastmcp.FastMCP` Streamable HTTP deadlocks after about
five sequential Windows sessions, while the raw `mcp.server.lowlevel.Server` +
`StreamableHTTPSessionManager` control completed 30/30 on the same workload.
The report attributes the observed stuck tasks to the FastMCP wrapper's
`sse_starlette.EventSourceResponse` integration; the low-level SDK writes the
event stream directly and did not reproduce the defect.

That evidence reinforces the existing architecture rather than proving the
upstream bug fixed. A local disposable regression now drives 30 sequential
initialize + tool-call + DELETE sessions through MemPalace's low-level app and
asserts that every SDK and local session map is released before the next
session. The four-concurrent-status regression is separate and exercises the
real MemPalace dispatcher and disposable Chroma status path.

## Sources

- MCP Streamable HTTP requirements and session behavior:
  https://modelcontextprotocol.io/specification/2025-11-25/basic/transports
- MCP cancellation behavior:
  https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/cancellation
- MCP SDK 1.28.1 session-manager source:
  https://github.com/modelcontextprotocol/python-sdk/blob/v1.28.1/src/mcp/server/streamable_http_manager.py
- MCP SDK v1 stability and v2 prerelease guidance:
  https://github.com/modelcontextprotocol/python-sdk/blob/v1.28.1/README.md
- FastMCP Windows sequential-session deadlock and passing low-level control:
  https://github.com/modelcontextprotocol/python-sdk/issues/2653
- AnyIO worker-thread cancellation semantics:
  https://anyio.readthedocs.io/en/stable/threads.html
- AnyIO 4.13.0 asyncio worker implementation:
  https://github.com/agronholm/anyio/blob/4.13.0/src/anyio/_backends/_asyncio.py

## Scope and validation

The repository validation used disposable tool registries and an ephemeral
loopback test socket. The later attended integration cutover restarted only the
exact managed MemPalace bridge process; Router, QMD, Meili, Hindsight, Honcho,
and code search were recorded as `not-touched`. Live concurrency proof called
only read-only `mempalace_status`. It did not mine, add, edit, or delete palace
data and did not access a hosted endpoint or Railway resource.

Focused validation updated 2026-07-14 after release-audit remediation:

- `.venv/Scripts/python.exe -m pytest tests/test_mcp_http.py -q`: 32 passed
  in 5.38 seconds with one Starlette `TestClient` deprecation warning. This
  includes four official-client sessions concurrently dispatching real status
  calls through an ephemeral Uvicorn socket and one HNSW probe, plus 30
  sequential initialize/tool-call/DELETE sessions with empty session maps after
  every iteration. Two cancellation-cooperative hanging transports also time
  out concurrently under internal deadlines, retain both capacity tombstones,
  and enter a bounded cooldown before the next retry wave. Graceful shutdown
  performs its own bounded drain and fails closed if any tombstone remains.
- `.venv/Scripts/python.exe -m ruff check mempalace/mcp_http.py
  tests/test_mcp_http.py`: all checks passed as part of the scoped Python lint.
- `.venv/Scripts/python.exe -m ruff format --check mempalace/mcp_http.py
  tests/test_mcp_http.py`: both files formatted and the scoped check passes.

The exact MCP SDK 1.28.1 environment also passes raw non-ASCII/malformed
Authorization cases, configured-token validation, direct bounded
termination-tombstone/cap accounting, and bounded client/server teardown. The
three order/load-sensitive real-Chroma receipt-readback failures found by an
earlier clean run were repaired by canonical six-digit mtime handling, bounded
stale-read retry, and supported-API-only exact vector readback that refuses a
live SQLite/WAL fallback. The final repository-wide release gate passes `1,673`
tests with `7` skips, `106` intentional deselections, and `191` warnings in
`136.68s` of pytest time (`139.526s` wall time). The run record is
`%LOCALAPPDATA%\MemSys\eval-artifacts\mempalace-finish-line\full-suite-final-latest-run.json`.

Independent transport review found no remaining implementation blocker.
MemPalace commit `04f5bf3` and the committed agent-settings launcher now run
the native HTTP path with backend concurrency serialized at one. An exact
attended bridge-only restart succeeded. The four-client live gate passed, then
`mempalace-attended-native-sustained-burnin-20260714T060927Z.json` passed six
four-client waves over `132.39s`: `24/24` authenticated read-only status calls,
`24/24` cleanup, zero lingering workers, and one stable exact bridge identity.
Fresh decision `mempalace-bridge-transport-readiness-20260714T061355Z` is `ok`
with `decision=native-transport-ready`. Supergateway remains an explicit
rollback path; hosted deployment and Railway remain outside scope.
