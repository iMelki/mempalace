"""Deterministic native gate fixtures; no palace, network socket or external model."""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import anyio
import anyio._backends._asyncio as asyncio_backend
import pytest

import mempalace.mcp_backend_calls as backend_calls
from mempalace.mcp_backend_calls import BackendCallGate, UnobservedBackendCalls, _WorkerHandoff


def _registry(handler):
    return {
        "fixture": {
            "description": "Disposable handler",
            "input_schema": {"type": "object", "properties": {"value": {"type": "string"}}},
            "handler": handler,
        }
    }


def _scenario(function):
    async def bounded():
        with anyio.fail_after(10):
            await function()

    anyio.run(bounded)


async def _wait(gate, predicate):
    with anyio.fail_after(3):
        while True:
            snapshot = gate.snapshot()
            if predicate(snapshot):
                return snapshot
            await anyio.sleep(0)


async def _cancellable(gate, done, *, task_status=anyio.TASK_STATUS_IGNORED):
    try:
        with anyio.CancelScope() as scope:
            task_status.started(scope)
            await gate.run("fixture", {"value": "fixture"})
    finally:
        done.set()


class _HeldHandler:
    def __init__(self):
        self.release = threading.Event()
        self.calls = 0

    def __call__(self, value=None):
        self.calls += 1
        if not self.release.wait(5):
            raise AssertionError("Disposable worker was not released")
        return value


def _assert_quiescent(snapshot):
    assert snapshot["accounting"] == "complete"
    assert snapshot["workerState"] == "quiescent"
    assert all(value == 0 for value in snapshot["counts"].values())
    assert snapshot["semanticReadiness"] == "not-assessed"


def _assert_unknown(snapshot):
    assert snapshot["accounting"] == "unknown"
    assert snapshot["workerState"] == "unknown"
    assert all(value is None for value in snapshot["counts"].values())


def test_cold_snapshot_is_memory_only_and_generation_bound():
    async def check():
        gate = BackendCallGate(_registry(lambda: pytest.fail("Must not dispatch")), 1)
        first, second = gate.snapshot(), gate.snapshot()
        _assert_quiescent(first)
        assert first["appGenerationId"] == second["appGenerationId"]
        assert first["sequence"] < second["sequence"]
        assert first["appCreatedAtUtc"].endswith("Z")
        assert "processBirthUtc" not in first
        assert first["scope"] == "this-native-gate-only"

    _scenario(check)


def test_active_worker_then_actual_release_is_observed():
    async def check():
        handler = _HeldHandler()
        gate = BackendCallGate(_registry(handler), 1)
        try:
            async with anyio.create_task_group() as group:
                group.start_soon(gate.run, "fixture", {"value": "fixture"})
                state = await _wait(gate, lambda s: s["counts"]["activeDispatches"] == 1)
                assert state["workerState"] == "busy"
                assert state["counts"]["borrowedPermits"] == 1
                handler.release.set()
            final = gate.snapshot()
            _assert_quiescent(final)
            stages = [event["stage"] for event in final["recentEvents"]]
            assert stages == [
                "waiting",
                "permit-acquired",
                "dispatch-started",
                "dispatch-exited",
                "permit-returned",
            ]
        finally:
            handler.release.set()

    _scenario(check)


def test_queued_cancellation_never_releases_another_workers_permit():
    async def check():
        handler, done = _HeldHandler(), anyio.Event()
        gate = BackendCallGate(_registry(handler), 1)
        try:
            async with anyio.create_task_group() as group:
                group.start_soon(gate.run, "fixture", {})
                await _wait(gate, lambda s: s["counts"]["activeDispatches"] == 1)
                scope = await group.start(_cancellable, gate, done)
                queued = await _wait(gate, lambda s: s["counts"]["gateWaiters"] == 1)
                assert queued["counts"]["admissionsPending"] == 1
                scope.cancel()
                await done.wait()
                state = gate.snapshot()
                assert state["counts"]["borrowedPermits"] == 1
                assert state["counts"]["admissionsPending"] == 0
                assert handler.calls == 1
                assert "cancelled-before-admission" in [
                    event["stage"] for event in state["recentEvents"]
                ]
                handler.release.set()
            _assert_quiescent(gate.snapshot())
        finally:
            handler.release.set()

    _scenario(check)


def test_abandoned_worker_stays_busy_and_blocks_second_dispatch():
    async def check():
        handler, done = _HeldHandler(), anyio.Event()
        gate = BackendCallGate(_registry(handler), 1)
        try:
            async with anyio.create_task_group() as group:
                scope = await group.start(_cancellable, gate, done)
                await _wait(gate, lambda s: s["counts"]["activeDispatches"] == 1)
                scope.cancel()
                await done.wait()
                state = gate.snapshot()
                assert state["counts"]["abandonedWorkersActive"] == 1
                assert state["counts"]["borrowedPermits"] == 1
                group.start_soon(gate.run, "fixture", {})
                await _wait(gate, lambda s: s["counts"]["gateWaiters"] == 1)
                assert handler.calls == 1
                handler.release.set()
            assert handler.calls == 2
            _assert_quiescent(gate.snapshot())
        finally:
            handler.release.set()

    _scenario(check)


def test_cancel_after_acquire_before_worker_start_releases_exactly_once(monkeypatch):
    async def check():
        captured, entered, done = [], anyio.Event(), anyio.Event()

        async def delayed_start(worker, *, abandon_on_cancel):
            assert abandon_on_cancel is True
            captured.append(worker)
            entered.set()
            await anyio.sleep_forever()

        monkeypatch.setattr(anyio.to_thread, "run_sync", delayed_start)
        gate = BackendCallGate(_registry(lambda **_: pytest.fail("Late dispatch")), 1)
        async with anyio.create_task_group() as group:
            scope = await group.start(_cancellable, gate, done)
            await entered.wait()
            state = gate.snapshot()
            assert state["counts"]["workerStartsPending"] == 1
            scope.cancel()
            await done.wait()
        _assert_quiescent(gate.snapshot())
        assert captured[0]() is not None  # Actual late worker exits before dispatch/release.
        _assert_quiescent(gate.snapshot())

    _scenario(check)


def test_actual_default_thread_limiter_wait_is_not_a_started_worker():
    async def check():
        blocker, done = _HeldHandler(), anyio.Event()
        default = anyio.to_thread.current_default_thread_limiter()
        previous = default.total_tokens
        default.total_tokens = 1
        gate = BackendCallGate(_registry(lambda value=None: value), 1)
        try:
            async with anyio.create_task_group() as group:
                group.start_soon(anyio.to_thread.run_sync, blocker)
                with anyio.fail_after(3):
                    while blocker.calls == 0:
                        await anyio.sleep(0)
                scope = await group.start(_cancellable, gate, done)
                state = await _wait(gate, lambda s: s["counts"]["workerStartsPending"] == 1)
                assert state["counts"]["activeDispatches"] == 0
                scope.cancel()
                await done.wait()
                _assert_quiescent(gate.snapshot())
                blocker.release.set()
        finally:
            blocker.release.set()
            default.total_tokens = previous

    _scenario(check)


def test_handler_exception_is_not_cancelled_and_permit_is_returned():
    async def check():
        def fail(value=None):
            raise ValueError("private-exception-sentinel")

        gate = BackendCallGate(_registry(fail), 1)
        with pytest.raises(ValueError, match="private-exception-sentinel"):
            await gate.run("fixture", {})
        final = gate.snapshot()
        _assert_quiescent(final)
        assert final["recentEvents"][-1]["dispatchOutcome"] == "raised"
        assert "cancelled" not in json.dumps(final)
        assert "private-exception-sentinel" not in json.dumps(final)

    _scenario(check)


def test_non_cancellation_prestart_failure_keeps_its_own_classification(monkeypatch):
    async def check():
        async def fail_start(*args, **kwargs):
            raise RuntimeError("private-launch-sentinel")

        monkeypatch.setattr(anyio.to_thread, "run_sync", fail_start)
        gate = BackendCallGate(_registry(lambda: None), 1)
        with pytest.raises(RuntimeError, match="private-launch-sentinel"):
            await gate.run("fixture", {})
        final = gate.snapshot()
        _assert_quiescent(final)
        assert "dispatch-not-started" in [e["stage"] for e in final["recentEvents"]]
        assert "cancelled" not in json.dumps(final)

    _scenario(check)


def test_normal_completion_then_waiter_cancellation_does_not_resurrect_work(monkeypatch):
    async def check():
        actual = anyio.to_thread.run_sync
        returned, done = anyio.Event(), anyio.Event()

        async def after_completion(worker, *, abandon_on_cancel):
            await actual(worker, abandon_on_cancel=abandon_on_cancel)
            returned.set()
            await anyio.sleep_forever()

        monkeypatch.setattr(anyio.to_thread, "run_sync", after_completion)
        gate = BackendCallGate(_registry(lambda value=None: value), 1)
        async with anyio.create_task_group() as group:
            scope = await group.start(_cancellable, gate, done)
            await returned.wait()
            _assert_quiescent(gate.snapshot())
            scope.cancel()
            await done.wait()
        _assert_quiescent(gate.snapshot())

    _scenario(check)


@pytest.mark.parametrize("exception_class", [RuntimeError, BaseException])
def test_observer_failure_cannot_bypass_dispatch_or_permit_cleanup(monkeypatch, exception_class):
    async def check():
        gate = BackendCallGate(_registry(lambda value=None: "actual-result"), 1)

        def broken_observer(*args, **kwargs):
            raise exception_class("private-observer-sentinel")

        monkeypatch.setattr(gate._ledger, "advance", broken_observer)
        assert await gate.run("fixture", {}) == "actual-result"
        assert gate._limiter.statistics().borrowed_tokens == 0
        final = gate.snapshot()
        _assert_unknown(final)
        assert "private-observer-sentinel" not in json.dumps(final)

    _scenario(check)


def test_release_failure_is_unresolved_and_not_swallowed():
    async def check():
        gate = BackendCallGate(_registry(lambda value=None: "actual-result"), 1)
        actual = gate._limiter

        class FailingRelease:
            acquire_on_behalf_of = actual.acquire_on_behalf_of
            statistics = actual.statistics

            def release_on_behalf_of(self, borrower):
                raise RuntimeError("private-release-sentinel")

        gate._limiter = FailingRelease()
        try:
            with pytest.raises(RuntimeError, match="private-release-sentinel"):
                await gate.run("fixture", {})
            _assert_unknown(gate.snapshot())
            assert actual.statistics().borrowed_tokens == 1
            assert "permit-returned" not in [e["stage"] for e in gate.snapshot()["recentEvents"]]
        finally:
            for borrower in actual.statistics().borrowers:
                actual.release_on_behalf_of(borrower)

    _scenario(check)


def test_history_eviction_does_not_destroy_complete_active_coverage():
    async def check():
        gate = BackendCallGate(_registry(lambda value=None: value), 1, history_limit=2)
        await gate.run("fixture", {})
        final = gate.snapshot()
        _assert_quiescent(final)
        assert len(final["recentEvents"]) == 2
        assert final["historyDropped"] == 3

    _scenario(check)


def test_active_accounting_overflow_is_sticky_unknown_not_silent_eviction():
    async def check():
        handler = _HeldHandler()
        gate = BackendCallGate(_registry(handler), 1, active_limit=1)
        try:
            async with anyio.create_task_group() as group:
                group.start_soon(gate.run, "fixture", {})
                await _wait(gate, lambda s: s["counts"]["activeDispatches"] == 1)
                group.start_soon(gate.run, "fixture", {})
                await _wait(gate, lambda s: s["accounting"] == "unknown")
                assert len(gate._ledger._active) == 1
                handler.release.set()
            assert handler.calls == 2
            assert gate._limiter.statistics().borrowed_tokens == 0
            _assert_unknown(gate.snapshot())
        finally:
            handler.release.set()

    _scenario(check)


@pytest.mark.parametrize("borrowed,waiting", [(1, 0), (-1, 0), (True, 0), (0, None)])
def test_broken_public_statistics_never_manufacture_quiescence(monkeypatch, borrowed, waiting):
    async def check():
        gate = BackendCallGate(_registry(lambda: None), 1)
        monkeypatch.setattr(
            gate._limiter,
            "statistics",
            lambda: SimpleNamespace(
                borrowed_tokens=borrowed,
                tasks_waiting=waiting,
            ),
        )
        _assert_unknown(gate.snapshot())

    _scenario(check)


def test_snapshot_failure_returns_unknown_not_raw_error(monkeypatch):
    async def check():
        gate = BackendCallGate(_registry(lambda: None), 1)

        def broken_snapshot(*args):
            raise RuntimeError("private-snapshot-sentinel")

        monkeypatch.setattr(gate._ledger, "snapshot", broken_snapshot)
        state = gate.snapshot()
        _assert_unknown(state)
        assert state["sequence"] is None
        assert "private-snapshot-sentinel" not in json.dumps(state)

    _scenario(check)


def test_contradictory_abandoned_accounting_is_unknown_even_with_zero_permits(monkeypatch):
    async def check():
        gate = BackendCallGate(_registry(lambda: None), 1)
        original = gate._ledger._counts

        def broken_counts(borrowed, waiting):
            return {**original(borrowed, waiting), "abandonedWorkersActive": 1}

        monkeypatch.setattr(gate._ledger, "_counts", broken_counts)
        _assert_unknown(gate.snapshot())

    _scenario(check)


def test_unknown_runner_and_two_app_generations_never_imply_readiness():
    first = UnobservedBackendCalls("secret-path-sentinel", "secret-token-sentinel").snapshot()
    second = UnobservedBackendCalls(None, None).snapshot()
    assert first["accounting"] == "unsupported-runner"
    assert first["workerState"] == "unknown"
    assert all(value is None for value in first["counts"].values())
    assert first["appGenerationId"] != second["appGenerationId"]
    assert first["processId"] == second["processId"]
    assert first["instanceId"] is first["startupSourceRevision"] is None
    assert "secret" not in json.dumps(first)


def test_arguments_results_and_names_never_enter_observation_history():
    async def check():
        name, secret = "private-tool-sentinel", "private-query-result-sentinel"
        registry = _registry(lambda value=None: secret)
        registry[name] = registry.pop("fixture")
        gate = BackendCallGate(registry, 1)
        assert await gate.run(name, {"value": secret}) == secret
        saved = json.dumps(gate.snapshot())
        assert name not in saved and secret not in saved

    _scenario(check)


def test_handoff_host_and_worker_cannot_both_own_release():
    host = _WorkerHandoff()
    assert host.release_from_host() is True
    assert host.start() is False
    worker = _WorkerHandoff()
    assert worker.start() is True
    assert worker.release_from_host() is False


def test_actual_uncontended_acquire_checkpoint_is_complete_busy_not_sticky_unknown(monkeypatch):
    async def check():
        gate = BackendCallGate(_registry(lambda value=None: value), 1)
        captured = []
        backend = asyncio_backend.AsyncIOBackend
        actual_checkpoint = backend.cancel_shielded_checkpoint

        async def observe_checkpoint(cls):
            raw = gate._limiter.statistics()
            if (
                not captured
                and raw.borrowed_tokens == 1
                and all(item.stage == "waiting" for item in gate._ledger._active.values())
            ):
                captured.append(gate.snapshot())
            await actual_checkpoint()

        monkeypatch.setattr(backend, "cancel_shielded_checkpoint", classmethod(observe_checkpoint))
        await gate.run("fixture", {})
        assert len(captured) == 1
        state = captured[0]
        assert state["accounting"] == "complete"
        assert state["workerState"] == "busy"
        assert state["counts"]["borrowedPermits"] == 1
        assert state["counts"]["admissionsPending"] == 1
        assert state["counts"]["workerStartsPending"] == 0
        _assert_quiescent(gate.snapshot())

    _scenario(check)


def test_actual_notified_waiter_is_still_pending_work_before_it_resumes(monkeypatch):
    async def check():
        handler, captured = _HeldHandler(), []
        gate = BackendCallGate(_registry(handler), 1)
        actual_release = gate._release

        def release_then_observe(borrower, attempt):
            actual_release(borrower, attempt)
            raw = gate._limiter.statistics()
            pending = sum(item.stage == "waiting" for item in gate._ledger._active.values())
            if raw.borrowed_tokens == 0 and raw.tasks_waiting == 0 and pending == 1:
                captured.append(gate.snapshot())

        monkeypatch.setattr(gate, "_release", release_then_observe)
        try:
            async with anyio.create_task_group() as group:
                group.start_soon(gate.run, "fixture", {})
                await _wait(gate, lambda s: s["counts"]["activeDispatches"] == 1)
                group.start_soon(gate.run, "fixture", {})
                await _wait(gate, lambda s: s["counts"]["gateWaiters"] == 1)
                handler.release.set()
            assert len(captured) == 1
            state = captured[0]
            assert state["accounting"] == "complete"
            assert state["workerState"] == "busy"
            assert state["counts"]["borrowedPermits"] == state["counts"]["gateWaiters"] == 0
            assert state["counts"]["admissionsPending"] == 1
            _assert_quiescent(gate.snapshot())
        finally:
            handler.release.set()

    _scenario(check)


def test_pending_admission_cannot_explain_both_borrowed_and_queued_permits(monkeypatch):
    async def check():
        gate = BackendCallGate(_registry(lambda: None), 1)
        gate._ledger.begin()
        monkeypatch.setattr(
            gate._limiter,
            "statistics",
            lambda: SimpleNamespace(
                borrowed_tokens=1,
                tasks_waiting=1,
            ),
        )
        _assert_unknown(gate.snapshot())

    _scenario(check)


def test_attempt_nonce_generation_is_outside_bookkeeping_lock(monkeypatch):
    async def check():
        gate = BackendCallGate(_registry(lambda value=None: value), 1)
        actual_uuid4, calls = backend_calls.uuid.uuid4, []

        def lock_free_nonce():
            assert not gate._ledger._lock.locked()
            calls.append(True)
            return actual_uuid4()

        monkeypatch.setattr(backend_calls.uuid, "uuid4", lock_free_nonce)
        await gate.run("fixture", {})
        assert calls == [True]
        _assert_quiescent(gate.snapshot())

    _scenario(check)
