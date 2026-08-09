"""Tests for mine_palace_lock — the per-palace non-blocking mine guard.

Covers the fix for the runaway mine fan-out described alongside issues
#974 and #965: if N copies of `mempalace mine` are spawned concurrently
against the same palace, they must collapse to a single runner rather
than queue as waiters that will drive parallel HNSW inserts. Mines
against *different* palaces must still be free to run in parallel.
"""

from __future__ import annotations

import hashlib
import importlib.util
import multiprocessing
import os
import sys
import time

import pytest

from mempalace.palace import (
    MineAlreadyRunning,
    mine_global_lock,
    mine_lock,
    mine_palace_lock,
)


def _get_mp_context():
    """Pick a start method that works on every CI runner.

    `fork` is cheaper (no re-import) but is unavailable on Windows, so we fall
    back to `spawn` there. `spawn` inherits ``os.environ`` (including the
    monkeypatched ``HOME``) and re-imports the ``mempalace`` package in the
    child, which is sufficient for the lock-file semantics exercised here.
    """
    start_method = "spawn" if os.name == "nt" else "fork"
    return multiprocessing.get_context(start_method)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hold_lock(palace_path: str, ready_flag: str, release_flag: str) -> int:
    """Acquire mine_palace_lock, signal readiness, wait for release flag.

    Returns 0 if we acquired the lock, 1 if MineAlreadyRunning was raised.
    Runs in a child process for true cross-process locking semantics.
    """
    try:
        with mine_palace_lock(palace_path):
            # Tell the parent we hold the lock
            open(ready_flag, "w").close()
            # Wait until parent tells us to release
            for _ in range(500):
                if os.path.exists(release_flag):
                    return 0
                time.sleep(0.01)
            return 0
    except MineAlreadyRunning:
        return 1


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_single_acquire_succeeds(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    with mine_palace_lock(str(tmp_path / "palace")):
        pass  # should not raise


def test_lock_reusable_after_release(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    palace = str(tmp_path / "palace")
    with mine_palace_lock(palace):
        pass
    # Re-acquire must succeed now that the previous holder released.
    with mine_palace_lock(palace):
        pass


def test_lock_path_sentinels_persist_after_release(tmp_path, monkeypatch):
    """Never unlink an advisory lock path while a contender could hold it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    source_key = "source-to-serialize"
    with mine_lock(source_key):
        pass
    source_lock = (
        tmp_path
        / ".mempalace"
        / "locks"
        / (hashlib.sha256(source_key.encode()).hexdigest()[:16] + ".lock")
    )
    assert source_lock.is_file()
    source_identity = (source_lock.stat().st_dev, source_lock.stat().st_ino)
    with mine_lock(source_key):
        pass
    assert (source_lock.stat().st_dev, source_lock.stat().st_ino) == source_identity

    palace = str(tmp_path / "palace")
    with mine_palace_lock(palace):
        pass
    normalized_palace = os.path.normcase(os.path.realpath(os.path.expanduser(palace)))
    palace_lock = (
        tmp_path
        / ".mempalace"
        / "locks"
        / ("mine_palace_" + hashlib.sha256(normalized_palace.encode()).hexdigest()[:16] + ".lock")
    )
    assert palace_lock.is_file()
    palace_identity = (palace_lock.stat().st_dev, palace_lock.stat().st_ino)
    with mine_palace_lock(palace):
        pass
    assert (palace_lock.stat().st_dev, palace_lock.stat().st_ino) == palace_identity


def test_slow_mine_lock_warning_uses_a_pseudonymous_key(tmp_path, monkeypatch, caplog):
    """Slow-lock diagnostics must not retain the caller's source path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr("mempalace.palace._MINE_LOCK_WARN_THRESHOLD_SECONDS", 0.0)
    source_key = "C:/private/source/with-sensitive-name.md"
    expected_key = hashlib.sha256(source_key.encode()).hexdigest()[:16]

    with caplog.at_level("WARNING", logger="mempalace.palace"):
        with mine_lock(source_key):
            pass

    assert source_key not in caplog.text
    assert expected_key in caplog.text


def test_failed_mine_lock_attempt_is_recorded_and_warned(tmp_path, monkeypatch, caplog):
    """The Windows retry-cliff error must remain visible to #41 diagnostics."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr("mempalace.palace._MINE_LOCK_WARN_THRESHOLD_SECONDS", 0.0)

    import mempalace.palace as palace_module

    def _raise_oserror(*_args):
        raise OSError("test")

    if os.name == "nt":
        import msvcrt

        monkeypatch.setattr(msvcrt, "locking", _raise_oserror)
    else:
        import fcntl

        monkeypatch.setattr(fcntl, "flock", _raise_oserror)

    palace_module.reset_mine_lock_stats()
    with caplog.at_level("WARNING", logger="mempalace.palace"):
        with pytest.raises(OSError, match="test"):
            with mine_lock("logical://synthetic/failed-lock"):
                pass

    stats = palace_module.get_mine_lock_stats()
    assert stats["attempts"] == 1
    assert stats["successful_acquisitions"] == 0
    assert stats["failed_acquisitions"] == 1
    assert stats["max_attempt_seconds"] >= 0.0
    assert stats["max_successful_acquire_seconds"] == 0.0
    assert "failed" in caplog.text


@pytest.mark.parametrize("raw", ["0", "-1", "nan", "inf", "1e300", "9.01", "not-a-number"])
def test_mine_lock_warn_threshold_rejects_invalid_environment_values(monkeypatch, raw):
    monkeypatch.setenv("MEMPALACE_MINE_LOCK_WARN_THRESHOLD_SECONDS", raw)

    import mempalace.palace as palace_module

    assert (
        palace_module._read_positive_finite_float_from_env(
            "MEMPALACE_MINE_LOCK_WARN_THRESHOLD_SECONDS", 2.0
        )
        == 2.0
    )


def test_mine_lock_warn_threshold_accepts_a_positive_finite_environment_value(monkeypatch):
    monkeypatch.setenv("MEMPALACE_MINE_LOCK_WARN_THRESHOLD_SECONDS", "3.5")

    import mempalace.palace as palace_module

    assert (
        palace_module._read_positive_finite_float_from_env(
            "MEMPALACE_MINE_LOCK_WARN_THRESHOLD_SECONDS", 2.0
        )
        == 3.5
    )


def test_mine_lock_warn_threshold_accepts_its_reviewed_upper_bound(monkeypatch):
    monkeypatch.setenv("MEMPALACE_MINE_LOCK_WARN_THRESHOLD_SECONDS", "9")

    import mempalace.palace as palace_module

    assert (
        palace_module._read_positive_finite_float_from_env(
            "MEMPALACE_MINE_LOCK_WARN_THRESHOLD_SECONDS", 2.0
        )
        == 9.0
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, 2.0),
        ("3.5", 3.5),
        ("0", 2.0),
        ("nan", 2.0),
        ("inf", 2.0),
        ("1e300", 2.0),
        ("not-a-number", 2.0),
    ],
)
def test_mine_lock_threshold_env_is_applied_during_a_fresh_module_import(
    monkeypatch, raw, expected
):
    """The import-time warning threshold must never make startup fragile."""
    import mempalace.palace as palace_module

    module_name = "mempalace._test_palace_import_config"
    spec = importlib.util.spec_from_file_location(module_name, palace_module.__file__)
    assert spec is not None
    assert spec.loader is not None
    fresh_module = importlib.util.module_from_spec(spec)

    with monkeypatch.context() as environment:
        if raw is None:
            environment.delenv("MEMPALACE_MINE_LOCK_WARN_THRESHOLD_SECONDS", raising=False)
        else:
            environment.setenv("MEMPALACE_MINE_LOCK_WARN_THRESHOLD_SECONDS", raw)
        sys.modules[module_name] = fresh_module
        try:
            spec.loader.exec_module(fresh_module)
            assert fresh_module._MINE_LOCK_WARN_THRESHOLD_SECONDS == expected
        finally:
            sys.modules.pop(module_name, None)


def test_same_palace_serializes_across_processes(tmp_path, monkeypatch):
    """Two processes contending for the same palace: second must be rejected."""
    monkeypatch.setenv("HOME", str(tmp_path))
    palace = str(tmp_path / "palace")
    ready = str(tmp_path / "ready")
    release = str(tmp_path / "release")

    ctx = _get_mp_context()
    holder = ctx.Process(target=_hold_lock, args=(palace, ready, release))
    holder.start()
    try:
        # Wait for the holder to acquire
        for _ in range(500):
            if os.path.exists(ready):
                break
            time.sleep(0.01)
        assert os.path.exists(ready), "holder failed to acquire lock in time"

        # From the parent, we must not be able to acquire the same palace lock
        with pytest.raises(MineAlreadyRunning):
            with mine_palace_lock(palace):
                pytest.fail("second acquire of same palace should have raised")
    finally:
        open(release, "w").close()
        holder.join(timeout=5)
        assert holder.exitcode == 0


def test_different_palaces_dont_conflict(tmp_path, monkeypatch):
    """Mines against different palaces must NOT block each other."""
    monkeypatch.setenv("HOME", str(tmp_path))
    palace_a = str(tmp_path / "palace_a")
    palace_b = str(tmp_path / "palace_b")
    ready = str(tmp_path / "ready_a")
    release = str(tmp_path / "release_a")

    ctx = _get_mp_context()
    holder = ctx.Process(target=_hold_lock, args=(palace_a, ready, release))
    holder.start()
    try:
        for _ in range(500):
            if os.path.exists(ready):
                break
            time.sleep(0.01)
        assert os.path.exists(ready), "holder failed to acquire lock in time"

        # Different palace — must succeed even while palace_a is held
        with mine_palace_lock(palace_b):
            pass  # no exception expected
    finally:
        open(release, "w").close()
        holder.join(timeout=5)


def test_palace_path_is_normalized(tmp_path, monkeypatch):
    """Relative and absolute forms of the same path must use the same lock."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    os.makedirs(tmp_path / "palace", exist_ok=True)
    absolute = str(tmp_path / "palace")
    relative = "palace"

    # Hold the lock with the absolute form; attempting to re-acquire with
    # the relative form (which resolves to the same absolute path) must fail.
    with mine_palace_lock(absolute):
        with pytest.raises(MineAlreadyRunning):
            with mine_palace_lock(relative):
                pytest.fail("normalized path collision should have raised")


def test_mine_global_lock_is_alias_for_back_compat(tmp_path, monkeypatch):
    """Old callers of `mine_global_lock` should still work."""
    monkeypatch.setenv("HOME", str(tmp_path))
    assert mine_global_lock is mine_palace_lock
    with mine_global_lock(str(tmp_path / "palace")):
        pass  # the alias accepts the same palace_path argument
