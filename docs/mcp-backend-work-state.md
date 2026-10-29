# Native backend work state

Fork-specific source candidate for [MemPalace #73](https://github.com/iMelki/mempalace/issues/73).
This document describes source behavior, not an installed or accepted runtime.

## At a glance

`GET /__memsys/work-state` observes the exact native application's backend gate
without calling a handler, counting drawers, accessing a database, initializing
an MCP session or warming a model. The existing bearer and Origin middleware
protect it just like the other native HTTP endpoints.

```text
caller waiting -> permit acquired -> synchronous dispatch -> dispatch exited
                                                      -> actual permit returned
caller cancelled -------------------> worker may still be running
```

An abandoned caller is not a completed worker. AnyIO's
[thread cancellation contract](https://anyio.readthedocs.io/en/stable/threads.html)
allows the host task to stop waiting while synchronous dispatch continues. The
existing gate keeps that worker's permit until dispatch exits. This candidate
preserves `abandon_on_cancel=True`, concurrency, time budgets and SDK behavior.

The MCP [cancellation specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/cancellation)
also permits completion/cancellation races. The route does not reinterpret a
cancel notification as worker termination.

## Response contract

The response schema is `mempalace-backend-work-state/v1`.

| Field | Meaning |
| --- | --- |
| `processId` | Current native process PID; not sufficient alone to bind a process lifetime. |
| `appGenerationId` | Random UUID created for this gate/app instance. Different apps in one process differ. |
| `appCreatedAtUtc` | Application/gate creation time, **not** native process birth time. |
| `instanceId` | Optional startup-supplied `sha256:` opaque identity; otherwise null. |
| `startupSourceRevision` | Startup package source digest, when the HTTP factory supplies it; otherwise null. |
| `capturedAtUtc` | Time of this memory-only snapshot. |
| `sequence` | Increasing event/snapshot sequence within this app generation; null if snapshot accounting itself fails. |
| `scope` | Always `this-native-gate-only`. |
| `semanticReadiness` | Always `not-assessed`. |
| `accounting` | `complete`, `unknown`, or `unsupported-runner`. |
| `workerState` | `busy`, `quiescent`, or `unknown`, conservatively derived from accounting. |
| `counts` | The seven counts below; all null when accounting is unknown/unsupported. |
| `recentEvents` | Bounded fixed-label observations with opaque attempt UUIDs and sequence numbers. |
| `historyDropped` | Number of historical events evicted; independent of active accounting. Null if snapshot accounting fails. |

The startup source digest is a startup filesystem observation, not proof that
an old running process has adopted newly published files. Native process birth,
listener ownership, selected source and deployment acceptance remain separate
runtime checks.

| Count | Exact scope |
| --- | --- |
| `borrowedPermits` | Public native limiter statistic: currently borrowed permits. |
| `gateWaiters` | Public native limiter statistic: tasks queued at that limiter. |
| `admissionsPending` | Attempts begun but not yet observed acquiring a permit, including admission checkpoint/handoff windows. |
| `workerStartsPending` | Acquired permit, synchronous dispatch not started yet; this includes waiting at AnyIO's separate default thread limiter. |
| `activeDispatches` | Synchronous dispatch started and has not exited. |
| `abandonedWorkersActive` | Active dispatches whose MCP host waiter was cancelled. |
| `unresolvedReleases` | Dispatch exited or was cancelled/failed before start, but successful permit return is not yet observed. |

These are overlapping lifecycle measures, not numbers to add into one workload
total. The implementation reads only the public
[limiter statistics](https://anyio.readthedocs.io/en/stable/api.html#anyio.CapacityLimiter.statistics),
not task/borrower representations. Statistics must agree with live accounting.
AnyIO's uncontended acquisition borrows before its shielded checkpoint resumes
the host. Pending admissions may explain exactly those not-yet-recorded held
permits; the same admission cannot explain both a borrowed token and a queued
waiter. A notified waiter remains pending work even after leaving the limiter's
wait queue. Both windows stay `busy`, not `quiescent` or permanently unknown.

- `busy`: accounting is complete and at least one relevant count is positive.
- `quiescent`: accounting is complete, every relevant count is known and zero,
  and limiter/ledger accounting agrees.
- `unknown`: a count cannot be established, accounting disagrees, observation
  fails, active accounting overflows, or an injected runner is unsupported.

`quiescent` means only no work observed in this gate. It is **not** useful
recall, semantic/vector readiness, database-wide quiet, mining permission,
maintenance handback, or proof that an old generation has finished.

## Lifecycle, boundedness and privacy

Events contain only `attemptId`, `stage`, `sequence`, and `dispatchOutcome`.
The outcome is null, `returned`, or `raised`; arbitrary exceptions are absent.
Stages are fixed: `waiting`, `permit-acquired`, `dispatch-started`,
`dispatch-exited`, `permit-returned`, `cancelled-before-admission`,
`cancelled-before-start`, `admission-failed`, `dispatch-not-started`, and
`waiter-abandoned`.

An attempt UUID is local to this gate's observation stream. It is not an MCP
session ID, JSON-RPC request ID, router request ID, or exact attribution to a
separate caller. Do not join it to a caller using only a similar timestamp.

The default active-accounting limit is 1,024 and historical limit is 128 events.
Historical eviction increments `historyDropped` and does not erase live records.
Active overflow does not evict a live record or block a valid dispatch: it makes
coverage permanently unknown for that app generation. Observation errors also
invalidate coverage permanently. A restored test fixture or a later empty gate
does not erase that uncertainty.

No query, arguments, result, tool name, task representation, path, credential,
session identifier or arbitrary exception text is recorded. Optional identity
fields accept only opaque SHA-256 labels. Nothing is written to a file or log by
the observer. The endpoint does not persist its observations.

The worker/host handoff lock still decides the original release owner. Ledger
locks protect only small memory updates. UUID generation, which reads OS
randomness, occurs before the ledger lock. No I/O, await, or thread-to-loop
callback occurs under either bookkeeping lock. The successful `permit-returned`
event is recorded in the same loop callback **after** real release succeeds.
An actual release failure remains an operation failure and makes accounting
unknown; it is not swallowed. Observation failures do not skip dispatch or
permit cleanup and do not replace normal handler results/errors.

## Qualification and adoption boundary

The candidate adds deterministic fixtures for cold/no-search observations,
held/queued workers, cancellation before admission/start and after start,
completion/cancellation races, dispatch/release/observer failures, history and
active limits, broken statistics, privacy, auth/Origin and multiple generations.
The existing real SDK cancellation fixture also reads the route while its
synchronous handler holds the exact application gate.

An actual isolated AnyIO 4.13.0 checkpoint control on 2026-10-09 reproduced the
first draft's incorrect sticky-unknown classification (source SHA
`9E54A13D5A3446DD519FC4F8D1E0EB76ECC84760597034072DB4A4C7927DA95F`).
Native exit 1 correctly identified `legitimate-admission-window-rejected` with
borrowed=1, pending admission=1 and recorded held=0; the worker still returned
its permit. Private immutable receipt SHA:
`C359E00241892676B295CCB93BCE1D5A707002CFD390844AE8E10AD1EB43638A`.
The final formatted source SHA
`E247D869D10F2ECA2DB0D3B8A3606F6753D82C52DA14F9F15502B53A027D421F`
passed the same actual checkpoint control: complete/busy at borrowed=1,
pending=1, held=0, then complete/quiescent with all seven counts zero after real
permit return. Native exit was 0. Restored receipt SHA:
`0E4DBE59F22F6D9DFF43A786D1F201350106E2D2D362D4CA97C651CA61CD97DA`.
The private probe is retained by hash rather than published with private paths;
the repository regression fixture is `tests/test_mcp_backend_calls.py`.

On 2026-10-09 the frozen source passed Ruff 0.15.9 check and format-check, and
the complete three-module fixture cohort passed **83/83**: 27 backend-call,
51 HTTP, and five dispatch cases, with zero failed, skipped or deselected.
Collection and terminal outcome inventories agree. This is not a full-repository
suite or live recall: HTTP fixtures use disposable synthetic stores and an
ephemeral loopback listener, not the configured service or palace.

The origin-checking private driver selected these whole modules without a node
filter: `tests/test_mcp_backend_calls.py`, `tests/test_mcp_http.py`, and
`tests/test_mcp_dispatch.py`. It ran under Python 3.13.2 with installed AnyIO
4.13.0, MCP 1.28.1 and pytest 9.0.3, selecting candidate package origins rather
than the live editable checkout. The bounded native test child exited 0 in
43,590 ms; fresh physical RAM admission was 79.7761% used at age 2.3476 seconds,
below the 90% ceiling. Both captures were complete/healthy with no dropped
bytes or reached deadline, and zero surviving job processes were proved.
The final lifecycle receipt records 83 completed tests and zero live clients.

| Saved evidence | SHA256 |
| --- | --- |
| Final Ruff check/format-check receipt | `21751AC675AC1D0B9C194C3D5F082E6D8A111B6700AC8243A7A7BF31B02CB922` |
| Three-module collection receipt | `0EEBD520F331B65C50591A1E487AE32BC94F4AA5B482CDDBB3BE894BA311B90C` |
| Collection inventory | `DBE9818A3DC8F8E71543D0F84733560E86C0D8C9515679C9D2B4B2F14E284AB9` |
| Final three-module test receipt | `D96B24B1B370599AB3BBB949202FE317A8453FCC48890EDD85E39346996993F1` |
| Actual terminal outcome inventory | `40C95602CF59854C21BC41BAB1370456DB19827BAE3024A27C0E1BB80B3EA813` |
| Final native lifecycle receipt | `1BC81ACED66F37E438CF5B317F75C5C1593B423C338ED85FE1D28B2307479429` |

One inherited `StarletteDeprecationWarning` remains at
`tests/test_mcp_http.py:24`: Starlette's TestClient deprecates its `httpx` path
in favor of `httpx2`. This cohort was green, not warning-free. Source-only
compatibility research is tracked under #73; no dependency change or install
was performed. Normal unbypassed commit/push hooks, full-repository acceptance,
and local Markdown link checks remain separate required publication checks.
The actual attributed red and restored pass are recorded in `.gate-evidence.json`.

Source ownership is isolated to native issue #73. Final independent non-author
acceptance, ordinary publication gates and CTO exact-head landing are still
pending; they precede any adoption. Source publication does not
install the package, restart a service, select the client or prove recall.
Actual native-runtime adoption and the existing mining/recovery handback remain
with the runtime owner under [MemSys #857](https://github.com/iMelki/memsys/issues/857).

The separate MemSys consumer belongs to its engineering parent under
[#730](https://github.com/iMelki/memsys/issues/730)/[#653](https://github.com/iMelki/memsys/issues/653).
It must bind current generation/time/count coverage, keep semantic readiness
separate, and reject stale, mismatched, incomplete or unsupported snapshots.
Installing this observer cannot retroactively prove what an older unobserved
worker did.
