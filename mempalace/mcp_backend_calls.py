"""Bounded native dispatch and memory-only, generation-bound work observations.

These observations describe this gate, not semantic readiness or database quiet.
The synchronous worker, not an abandoned MCP waiter, owns an acquired permit.
"""

from __future__ import annotations

import functools
import os
import re
import threading
import uuid
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import anyio

from .mcp_dispatch import dispatch_tool

ToolRegistry = Mapping[str, Mapping[str, Any]]
WORK_STATE_SCHEMA = "mempalace-backend-work-state/v1"
_OPAQUE_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z", re.ASCII)
_COUNT_NAMES = (
    "borrowedPermits",
    "gateWaiters",
    "admissionsPending",
    "workerStartsPending",
    "activeDispatches",
    "abandonedWorkersActive",
    "unresolvedReleases",
)
_TRANSITIONS = {
    "waiting": {"permit-acquired", "cancelled-before-admission", "admission-failed"},
    "permit-acquired": {"dispatch-started", "cancelled-before-start", "dispatch-not-started"},
    "dispatch-started": {"dispatch-exited"},
    "dispatch-exited": {"permit-returned"},
    "cancelled-before-start": {"permit-returned"},
    "dispatch-not-started": {"permit-returned"},
}
_TERMINAL = {"permit-returned", "cancelled-before-admission", "admission-failed"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _opaque_identity(value: str | None) -> str | None:
    return value if isinstance(value, str) and _OPAQUE_SHA256.fullmatch(value) else None


class _WorkIdentity:
    def __init__(self, instance_id: str | None, startup_source_revision: str | None):
        self._identity = {
            "schema": WORK_STATE_SCHEMA,
            "processId": os.getpid(),
            "appGenerationId": str(uuid.uuid4()),
            "appCreatedAtUtc": _utc_now(),
            "instanceId": _opaque_identity(instance_id),
            "startupSourceRevision": _opaque_identity(startup_source_revision),
            "scope": "this-native-gate-only",
            "semanticReadiness": "not-assessed",
        }
        self._sequence = 0

    def capture(self) -> dict[str, Any]:
        self._sequence += 1
        return {**self._identity, "capturedAtUtc": _utc_now(), "sequence": self._sequence}

    def unknown_capture(self) -> dict[str, Any]:
        return {
            **self._identity,
            "capturedAtUtc": _utc_now(),
            "sequence": None,
            "accounting": "unknown",
            "workerState": "unknown",
            "counts": {name: None for name in _COUNT_NAMES},
            "historyDropped": None,
            "recentEvents": [],
        }


@dataclass
class _Attempt:
    attempt_id: str
    stage: str = "waiting"
    abandoned: bool = False
    dispatch_outcome: str | None = None


class _WorkLedger:
    """Bound history separately from live accounting; overflow invalidates coverage."""

    def __init__(self, identity: _WorkIdentity, active_limit: int, history_limit: int):
        if (
            type(active_limit) is not int
            or active_limit < 1
            or type(history_limit) is not int
            or history_limit < 0
        ):
            raise ValueError("active_limit must be positive and history_limit nonnegative")
        self._identity = identity
        self._lock = threading.Lock()
        self._active: dict[str, _Attempt] = {}
        self._history: deque[dict[str, Any]] = deque(maxlen=history_limit)
        self._active_limit = active_limit
        self._history_dropped = 0
        self._coverage_failed = False

    def failed(self) -> None:
        # A sticky assignment also works when the failed observer was the lock itself.
        self._coverage_failed = True

    def _record(self, attempt: _Attempt, stage: str) -> None:
        self._identity._sequence += 1
        if len(self._history) == self._history.maxlen:
            self._history_dropped += 1
        self._history.append(
            {
                "attemptId": attempt.attempt_id,
                "stage": stage,
                "sequence": self._identity._sequence,
                "dispatchOutcome": attempt.dispatch_outcome,
            }
        )

    def begin(self) -> _Attempt | None:
        # uuid4 reads OS randomness: never perform that I/O under the ledger lock.
        attempt = _Attempt(str(uuid.uuid4()))
        with self._lock:
            if len(self._active) >= self._active_limit:
                self.failed()
                return None
            self._active[attempt.attempt_id] = attempt
            self._record(attempt, "waiting")
            return attempt

    def advance(self, attempt: _Attempt | None, stage: str, outcome: str | None = None) -> None:
        if attempt is None:
            return
        with self._lock:
            if stage not in _TRANSITIONS.get(attempt.stage, set()):
                raise RuntimeError("Invalid work-accounting transition")
            if outcome not in {None, "returned", "raised"}:
                raise ValueError("Invalid work-accounting outcome")
            attempt.stage = stage
            if outcome is not None:
                attempt.dispatch_outcome = outcome
            self._record(attempt, stage)
            if stage in _TERMINAL:
                del self._active[attempt.attempt_id]

    def abandoned(self, attempt: _Attempt | None) -> None:
        if attempt is None:
            return
        with self._lock:
            # Cancellation can race a completed release; never resurrect that worker.
            attempt.abandoned = True
            self._record(attempt, "waiter-abandoned")

    def _counts(self, borrowed: int | None, waiting: int | None) -> dict[str, int | None]:
        records = tuple(self._active.values())
        return {
            "borrowedPermits": borrowed,
            "gateWaiters": waiting,
            "admissionsPending": sum(item.stage == "waiting" for item in records),
            "workerStartsPending": sum(item.stage == "permit-acquired" for item in records),
            "activeDispatches": sum(item.stage == "dispatch-started" for item in records),
            "abandonedWorkersActive": sum(
                item.stage == "dispatch-started" and item.abandoned for item in records
            ),
            "unresolvedReleases": sum(
                item.stage
                in {
                    "dispatch-exited",
                    "cancelled-before-start",
                    "dispatch-not-started",
                }
                for item in records
            ),
        }

    def snapshot(self, borrowed: int | None, waiting: int | None) -> dict[str, Any]:
        with self._lock:
            counts = self._counts(borrowed, waiting)
            known = set(counts) == set(_COUNT_NAMES) and all(
                type(value) is int and value >= 0 for value in counts.values()
            )
            held = sum(
                counts[name]
                for name in (
                    "workerStartsPending",
                    "activeDispatches",
                    "unresolvedReleases",
                )
            )
            represented = counts["admissionsPending"] + held
            valid_records = all(
                item.stage in _TRANSITIONS
                and type(item.abandoned) is bool
                and item.dispatch_outcome in {None, "returned", "raised"}
                for item in self._active.values()
            )
            # Fast-path acquisition borrows before a shielded checkpoint resumes
            # the host. Only pending admissions can explain those extra permits.
            acquired_pending = borrowed - held if known else 0
            agreed = (
                known
                and 0 <= acquired_pending <= counts["admissionsPending"]
                and waiting <= counts["admissionsPending"] - acquired_pending
                and represented == len(self._active)
                and valid_records
                and counts["abandonedWorkersActive"] <= counts["activeDispatches"]
            )
            if not agreed:
                self.failed()
            complete = not self._coverage_failed
            state = "unknown" if not complete else ("busy" if any(counts.values()) else "quiescent")
            return {
                **self._identity.capture(),
                "accounting": "complete" if complete else "unknown",
                "workerState": state,
                "counts": counts if complete else {name: None for name in _COUNT_NAMES},
                "historyDropped": self._history_dropped,
                "recentEvents": [dict(event) for event in self._history],
            }


class _WorkerHandoff:
    """Keep the original host/worker ownership decision independent of telemetry."""

    def __init__(self):
        self._lock = threading.Lock()
        self._started = False
        self._host_released = False
        self.abandoned_before_start = object()

    def start(self) -> bool:
        with self._lock:
            if self._host_released:
                return False
            self._started = True
            return True

    def release_from_host(self) -> bool:
        with self._lock:
            if self._started:
                return False
            self._host_released = True
            return True


class BackendCallGate:
    """Run synchronous handlers off-loop; observe, never control, their lifecycle."""

    def __init__(
        self,
        tools: ToolRegistry,
        max_concurrency: int,
        *,
        instance_id: str | None = None,
        startup_source_revision: str | None = None,
        active_limit: int = 1024,
        history_limit: int = 128,
    ):
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        self.tools = tools
        self._limiter = anyio.CapacityLimiter(max_concurrency)
        self._ledger = _WorkLedger(
            _WorkIdentity(instance_id, startup_source_revision),
            active_limit,
            history_limit,
        )

    def _observe(self, method: str, *args: Any) -> Any:
        try:
            return getattr(self._ledger, method)(*args)
        except BaseException:
            self._ledger.failed()
            return None

    def snapshot(self) -> dict[str, Any]:
        """Read on the app event loop; no await, I/O, handler or borrower disclosure."""
        try:
            statistics = self._limiter.statistics()
            borrowed, waiting = statistics.borrowed_tokens, statistics.tasks_waiting
        except BaseException:
            self._ledger.failed()
            borrowed = waiting = None
        try:
            return self._ledger.snapshot(borrowed, waiting)
        except BaseException:
            self._ledger.failed()
            return self._ledger._identity.unknown_capture()

    def _release(self, borrower: object, attempt: _Attempt | None) -> None:
        try:
            self._limiter.release_on_behalf_of(borrower)
        except BaseException:
            self._ledger.failed()
            raise
        # Runs in the same event-loop callback, after the actual release succeeds.
        self._observe("advance", attempt, "permit-returned")

    def _worker(
        self, call: Any, handoff: _WorkerHandoff, borrower: object, attempt: _Attempt | None
    ) -> Any:
        if not handoff.start():
            return handoff.abandoned_before_start
        self._observe("advance", attempt, "dispatch-started")
        try:
            result = call()
        except BaseException:
            self._observe("advance", attempt, "dispatch-exited", "raised")
            raise
        else:
            self._observe("advance", attempt, "dispatch-exited", "returned")
            return result
        finally:
            anyio.from_thread.run_sync(self._release, borrower, attempt)

    async def _acquire(self, borrower: object, attempt: _Attempt | None) -> None:
        try:
            await self._limiter.acquire_on_behalf_of(borrower)
        except BaseException as exc:
            stage = (
                "cancelled-before-admission"
                if isinstance(exc, anyio.get_cancelled_exc_class())
                else "admission-failed"
            )
            self._observe("advance", attempt, stage)
            raise
        self._observe("advance", attempt, "permit-acquired")

    async def run(self, tool_name: str, arguments: Mapping[str, Any] | None) -> Any:
        call = functools.partial(dispatch_tool, self.tools, tool_name, arguments)
        borrower, handoff = object(), _WorkerHandoff()
        attempt = self._observe("begin")
        await self._acquire(borrower, attempt)
        worker = functools.partial(self._worker, call, handoff, borrower, attempt)
        try:
            result = await anyio.to_thread.run_sync(worker, abandon_on_cancel=True)
        except BaseException as exc:
            cancelled = isinstance(exc, anyio.get_cancelled_exc_class())
            if handoff.release_from_host():
                stage = "cancelled-before-start" if cancelled else "dispatch-not-started"
                self._observe("advance", attempt, stage)
                self._release(borrower, attempt)
            elif cancelled:
                self._observe("abandoned", attempt)
            raise
        if result is handoff.abandoned_before_start:  # pragma: no cover
            raise RuntimeError("Backend worker was abandoned before dispatch")
        return result


class UnobservedBackendCalls:
    """An injected runner is not evidence about the native gate or worker lifecycle."""

    def __init__(self, instance_id: str | None, startup_source_revision: str | None):
        self._identity = _WorkIdentity(instance_id, startup_source_revision)

    def snapshot(self) -> dict[str, Any]:
        return {
            **self._identity.capture(),
            "accounting": "unsupported-runner",
            "workerState": "unknown",
            "counts": {name: None for name in _COUNT_NAMES},
            "historyDropped": 0,
            "recentEvents": [],
        }
