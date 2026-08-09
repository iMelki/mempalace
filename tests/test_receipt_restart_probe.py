import json
from pathlib import Path

import mempalace.receipt_restart_probe as probe_module
from mempalace.receipt_restart_probe import EXPECTED_INTERRUPT_EXIT, run_probe


def test_real_chroma_hard_exit_is_restored_across_fresh_processes(tmp_path):
    result = run_probe(
        artifact_root=tmp_path / "artifacts",
        scratch_parent=tmp_path / "scratch",
        phase_timeout_seconds=90,
    )

    assert result["status"] == "ok"
    assert result["scope"] == {
        "mutationTarget": "disposable-synthetic-chroma",
        "livePalaceTouched": False,
        "historicalSourcesRead": False,
        "providerCalls": False,
        "networkRequired": False,
        "railwayAccessed": False,
        "disposableWorkspaceMarkerValidated": True,
    }
    assert result["processBoundary"]["expectedHardExitCode"] == EXPECTED_INTERRUPT_EXIT
    assert result["processBoundary"]["expectedHardExitObserved"] is True
    assert [item["exitCode"] for item in result["phases"]] == [
        0,
        EXPECTED_INTERRUPT_EXIT,
        0,
        0,
    ]
    assert result["recovery"] == {
        "action": "restore",
        "restoredRows": 1,
        "remainingRecoveries": 0,
        "vectorQueryTopId": "restart-probe-baseline",
        "sqliteIntegrity": "ok",
    }
    assert result["cleanup"] == {
        "attempted": True,
        "succeeded": True,
        "workspacePreserved": False,
        "workspacePath": None,
    }

    artifact = json.loads(Path(result["artifactPath"]).read_text(encoding="utf-8"))
    assert artifact["status"] == "ok"
    assert artifact["failure"] is None


def test_orchestrator_stops_and_preserves_evidence_on_unexpected_child_exit(tmp_path, monkeypatch):
    calls = []

    def fail_seed(phase, *, workspace, artifact_dir, phase_token, timeout_seconds):
        calls.append((phase, workspace, artifact_dir, phase_token, timeout_seconds))
        return {
            "phase": phase,
            "exitCode": 9,
            "timedOut": False,
            "durationSeconds": 0.01,
            "stdoutLog": str(artifact_dir / "phase-seed.stdout.log"),
            "stderrLog": str(artifact_dir / "phase-seed.stderr.log"),
        }

    monkeypatch.setattr(probe_module, "_run_child", fail_seed)
    result = run_probe(
        artifact_root=tmp_path / "artifacts",
        scratch_parent=tmp_path / "scratch",
        phase_timeout_seconds=12,
    )

    assert result["status"] == "failed"
    assert result["failure"] == "phase seed did not reach expected exit boundary"
    assert [call[0] for call in calls] == ["seed"]
    assert result["phases"][0]["exitMatched"] is False
    assert result["cleanup"]["attempted"] is False
    assert result["cleanup"]["succeeded"] is False
    assert result["cleanup"]["workspacePreserved"] is True
    assert Path(result["cleanup"]["workspacePath"]).is_dir()
    artifact = json.loads(Path(result["artifactPath"]).read_text(encoding="utf-8"))
    assert artifact["status"] == "failed"
    assert artifact["artifactPath"] == result["artifactPath"]


def test_public_help_hides_internal_child_phase_controls():
    help_text = probe_module._parser().format_help()
    assert "--phase {" not in help_text
    assert "--workspace" not in help_text
    assert "--phase-artifact-dir" not in help_text
    assert "--phase-token" not in help_text
