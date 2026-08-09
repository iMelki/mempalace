"""Privacy tests for the #41 pytest-report failure metrics."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import conftest as test_conftest


def _sample_stats():
    return {
        "collection_rows_for_ids_calls": 3,
        "collection_rows_for_ids_elapsed_max": 0.125,
        "verify_managed_write_readback_calls": 2,
        "verify_managed_write_readback_elapsed_max": 0.25,
    }


def test_failure_diagnostic_is_bounded_pseudonymous_and_metrics_only():
    private_nodeid = "tests/test_private.py::test_token[super-secret-value]"

    artifact_text = test_conftest._build_sanitized_failure_diagnostics(
        nodeid=private_nodeid,
        phase="call",
        stats=_sample_stats(),
        mine_lock_stats={
            "attempts": 4,
            "successful_acquisitions": 3,
            "failed_acquisitions": 1,
            "max_attempt_seconds": 1.5,
            "max_successful_acquire_seconds": 1.0,
        },
    )

    artifact = json.loads(artifact_text)
    assert len(artifact_text.encode("utf-8")) <= test_conftest._DIAGNOSTICS_MAX_BYTES
    assert private_nodeid not in artifact_text
    assert "super-secret-value" not in artifact_text
    assert artifact == {
        "schema": "mempalace-test-failure-diagnostics/v1",
        "phase": "call",
        "test_id_sha256": hashlib.sha256(private_nodeid.encode("utf-8")).hexdigest(),
        "readback": _sample_stats(),
        "mine_lock": {
            "attempts": 4,
            "successful_acquisitions": 3,
            "failed_acquisitions": 1,
            "max_attempt_seconds": 1.5,
            "max_successful_acquire_seconds": 1.0,
        },
    }


def test_failure_diagnostic_omits_an_unexpectedly_large_record(monkeypatch):
    monkeypatch.setattr(test_conftest, "_DIAGNOSTICS_MAX_BYTES", 1)

    assert json.loads(
        test_conftest._build_sanitized_failure_diagnostics(
            nodeid="tests/test_one.py::test_one",
            phase="call",
            stats=_sample_stats(),
            mine_lock_stats={
                "attempts": 0,
                "successful_acquisitions": 0,
                "failed_acquisitions": 0,
                "max_attempt_seconds": 0.0,
                "max_successful_acquire_seconds": 0.0,
            },
        )
    ) == {
        "schema": "mempalace-test-failure-diagnostics/v1",
        "status": "omitted-size-cap",
    }


def test_failed_child_pytest_run_emits_the_safe_diagnostics_section():
    """Exercise the report hook at the same point that terminal reporting uses."""
    repo_root = Path(__file__).resolve().parents[1]
    fixture = repo_root / "tests" / "fixtures" / "failure_diagnostics_intentional_failure.py"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root)
    kwargs = {
        "capture_output": True,
        "cwd": repo_root,
        "env": env,
        "stdin": subprocess.DEVNULL,
        "text": True,
        "timeout": 30,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(fixture)],
        **kwargs,
    )

    assert completed.returncode == 1
    output = completed.stdout + completed.stderr
    section_start = output.index("mempalace safe diagnostics")
    payload_start = output.index("{", section_start)
    payload_end = output.index("\n", payload_start)
    payload = json.loads(output[payload_start:payload_end])
    assert payload["schema"] == "mempalace-test-failure-diagnostics/v1"
    assert payload["phase"] == "call"
    assert payload["mine_lock"]["attempts"] == 1
    assert payload["mine_lock"]["successful_acquisitions"] == 1
    assert "synthetic-private-fixture-value" not in json.dumps(payload)
