"""Focused proof for the Chroma/ONNX thread lifecycle bound (mempalace#50)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import conftest as test_conftest

from mempalace.embedding import reset_embedding_function_cache
from mempalace.native_lifecycle import (
    BOUND_ENV,
    NATIVE_THREAD_BOUND_ENV,
    RECEIPT_ENV,
    RECEIPT_SCHEMA,
    NativeSessionRegistry,
    SyntheticNativePool,
    apply_native_thread_bounds,
    bound_native_lifecycle_enabled,
    close_chroma_client,
    dispose_onnx_owner,
    inspect_native_leak,
    release_native_sessions,
    sample_native_resources,
)


class _ClosableOwner:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class _OnnxOwner:
    def __init__(self) -> None:
        self.end_calls = 0
        self.session = self

    def end_session(self) -> None:
        self.end_calls += 1


def test_thread_bounds_are_applied_without_overwriting_operator_values(monkeypatch):
    monkeypatch.setenv("OMP_NUM_THREADS", "4")
    monkeypatch.delenv("OPENBLAS_NUM_THREADS", raising=False)

    applied = apply_native_thread_bounds()

    assert applied["OMP_NUM_THREADS"] == "4"
    assert applied["OPENBLAS_NUM_THREADS"] == "1"
    assert os.environ["OPENBLAS_NUM_THREADS"] == "1"


def test_suite_conftest_applied_the_native_thread_bounds():
    for key in NATIVE_THREAD_BOUND_ENV:
        assert os.environ.get(key), f"{key} must be set before chromadb imports"


def test_close_chroma_client_is_idempotent():
    owner = _ClosableOwner()

    assert close_chroma_client(owner) is True
    assert close_chroma_client(owner) is False
    assert owner.close_calls == 1


def test_dispose_onnx_owner_ends_session_and_clears_attribute():
    owner = _OnnxOwner()

    assert dispose_onnx_owner(owner) is True
    assert owner.end_calls >= 1
    assert owner.session is None


def test_reset_embedding_function_cache_disposes_cached_sessions(monkeypatch):
    import mempalace.embedding as embedding

    owner = _OnnxOwner()
    monkeypatch.setattr(embedding, "_EF_CACHE", {("CPUExecutionProvider",): owner})

    assert reset_embedding_function_cache() == 1
    assert embedding._EF_CACHE == {}
    assert owner.session is None


def test_synthetic_waiting_threads_return_to_baseline_after_release():
    baseline = sample_native_resources()
    registry = NativeSessionRegistry()
    pool = SyntheticNativePool(workers=8, name_prefix="proof-onnx")
    registry.track(pool)

    leaked = inspect_native_leak(registry, baseline)
    assert leaked["live_owners"] == 1
    assert leaked["python_thread_delta"] >= 8

    released = registry.release()
    after = sample_native_resources()

    assert released == 1
    assert registry.live_count() == 0
    assert after["python_threads"] <= baseline["python_threads"] + 1


def test_unreleased_owner_is_the_negative_leak_signal():
    registry = NativeSessionRegistry()
    registry.track(_ClosableOwner())
    report = inspect_native_leak(registry)

    assert report["live_owners"] == 1


def test_release_native_sessions_resets_default_backend(monkeypatch):
    from mempalace import palace

    calls = []
    monkeypatch.setattr(palace, "reset_default_backend", lambda: calls.append("reset"))
    registry = NativeSessionRegistry()
    owner = _ClosableOwner()
    registry.track(owner)

    result = release_native_sessions(registry)

    assert calls == ["reset"]
    assert owner.close_calls == 1
    assert result["live_clients"] == 0


def test_lifecycle_bound_is_enabled_for_the_suite_by_default():
    assert bound_native_lifecycle_enabled() is True
    assert os.environ.get(BOUND_ENV, "1") != "0"


def _run_pytest_child(fixture: Path, receipt: Path | None = None, extra_env=None):
    repo_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    # conftest redirects HOME into a session temp dir. A nested pytest must
    # see the real user site-packages (where pytest itself lives) and the
    # same HOME a protected pre-push caller would have.
    for var, original in test_conftest._original_env.items():
        if original is None:
            env.pop(var, None)
        else:
            env[var] = original
    env["PYTHONPATH"] = str(repo_root)
    if extra_env:
        env.update(extra_env)
    if receipt is not None:
        env[RECEIPT_ENV] = str(receipt)
    kwargs = {
        "capture_output": True,
        "cwd": str(repo_root),
        "env": env,
        "stdin": subprocess.DEVNULL,
        "text": True,
        "timeout": 120,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            str(fixture),
        ],
        **kwargs,
    )


def test_negative_fixture_fails_for_an_unreleased_native_pool():
    fixture = (
        Path(__file__).resolve().parents[1]
        / "tests"
        / "fixtures"
        / "native_lifecycle_unreleased.py"
    )
    completed = _run_pytest_child(fixture)

    assert completed.returncode == 1
    output = completed.stdout + completed.stderr
    assert "native-lifecycle-leak: unclosed chroma/onnx session" in output


def test_prepush_quiet_pytest_writes_lifecycle_receipt_and_releases(tmp_path):
    """Same quiet, cache-free invocation the protected pre-push caller uses."""
    fixture = (
        Path(__file__).resolve().parents[1]
        / "tests"
        / "fixtures"
        / "native_lifecycle_bound_child.py"
    )
    receipt = tmp_path / "native-lifecycle-receipt.json"
    completed = _run_pytest_child(fixture, receipt=receipt)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert payload["schema"] == RECEIPT_SCHEMA
    assert payload["final"] is True
    assert payload["live_clients"] == 0
    assert payload["lifecycle_bound"] is True
    assert payload["last_test"]
    assert payload["last_test_sha256"]
    assert payload["duration_seconds"] >= 0
    assert payload["peak"]["threads"] >= 1
    assert payload["peak"]["handles"] >= 0
    assert payload["peak"]["private_bytes"] >= 0
    assert payload["thread_bounds"]["OMP_NUM_THREADS"] == "1"
    output = completed.stdout + completed.stderr
    assert "mempalace native lifecycle (#50)" in output
    assert "peak_threads=" in output
