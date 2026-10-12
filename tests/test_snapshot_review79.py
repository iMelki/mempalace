"""PR 79 regressions against the production verifier and both native CLIs."""

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from mempalace import backup_snapshot as backup
from mempalace import snapshot_verification as verification
from test_snapshot_quick_weekly import stage


@pytest.mark.parametrize("entrypoint", ["mempalace.cli", "mempalace.backup_snapshot"])
@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize(
    "status,exit_code",
    [("complete", 0), ("missing", 3), ("stale", 3), ("failed", 2), ("invalid", 2)],
)
def test_receipt_cli_exposes_weekly_outcome(tmp_path, entrypoint, as_json, status, exit_code):
    live, staging, _ = stage(tmp_path)
    if status != "missing":
        backup.verify_weekly_snapshot(staging.parent, palace_path=live, budget_seconds=30)
        sidecar = staging / verification.WEEKLY_FILENAME
        weekly = json.loads(sidecar.read_text())
        if status == "stale":
            weekly["finishedAt"] = "2000-01-01T00:00:00Z"
        elif status == "failed":
            weekly["status"] = "error"
            weekly["message"] = "row 1 missing from index by_value"
        elif status == "invalid":
            weekly["snapshotReceiptSha256"] = "sha256:" + "0" * 64
        sidecar.write_text(json.dumps(weekly))
    command = [sys.executable, "-B", "-m", entrypoint]
    if entrypoint == "mempalace.cli":
        command.append("backup-snapshot")
    command += ["--verify-receipt", str(staging / backup.SNAPSHOT_RECEIPT_FILENAME)]
    if as_json:
        command.append("--json")
    result = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == exit_code, result.stdout + result.stderr
    if as_json:
        payload = json.loads(result.stdout)
        assert payload["valid"] is True  # Nightly proof stays independently valid.
        assert payload["weeklyVerification"]["status"] == status
    else:
        assert f"Weekly full integrity: {status}" in result.stdout
        outcome = "complete" if exit_code == 0 else "warning" if exit_code == 3 else "failed"
        assert f"outcome={outcome}" in result.stdout
        if status == "failed":
            assert "row 1 missing from index by_value" in result.stdout


@pytest.mark.parametrize("location", ["root", "ancestor"])
def test_weekly_accepts_writer_backup_root_reparse_points(tmp_path, monkeypatch, location):
    live, staging, _ = stage(tmp_path)
    allowed = staging.parent if location == "root" else tmp_path
    original = backup._is_link_or_reparse
    monkeypatch.setattr(
        backup, "_is_link_or_reparse", lambda path: path == allowed or original(path)
    )
    result = backup.verify_weekly_snapshot(staging.parent, palace_path=live, budget_seconds=30)
    assert result["status"] == "complete"
    assert (
        backup.verify_snapshot_receipt(staging / backup.SNAPSHOT_RECEIPT_FILENAME)[
            "weeklyVerification"
        ]["status"]
        == "complete"
    )


@pytest.mark.parametrize("location", ["generation", "palace", "database", "receipt", "segment"])
def test_weekly_refuses_reparse_points_inside_generation(tmp_path, monkeypatch, location):
    live, staging, receipt = stage(tmp_path)
    segment = next(
        e["relativePath"] for e in receipt["contentIdentity"]["files"] if "/" in e["relativePath"]
    )
    rejected = {
        "generation": staging,
        "palace": staging / "palace",
        "database": staging / "palace/chroma.sqlite3",
        "receipt": staging / backup.SNAPSHOT_RECEIPT_FILENAME,
        "segment": staging / "palace" / segment,
    }[location]
    original = backup._is_link_or_reparse
    monkeypatch.setattr(
        backup, "_is_link_or_reparse", lambda path: path == rejected or original(path)
    )
    with pytest.raises(backup.PalaceSnapshotError, match="links or reparse points"):
        backup.verify_weekly_snapshot(staging.parent, palace_path=live, budget_seconds=30)


def test_corruption_near_deadline_keeps_sqlite_error(tmp_path, monkeypatch):
    class Reader:
        def set_progress_handler(self, *args):
            pass

        def execute(self, sql):
            raise sqlite3.DatabaseError("database disk image is malformed")

        def close(self):
            pass

    monkeypatch.setattr(verification.sqlite3, "connect", lambda *a, **k: Reader())
    monkeypatch.setattr(verification.time, "monotonic", lambda: 100)
    with pytest.raises(backup.PalaceSnapshotError, match="disk image is malformed"):
        verification.sqlite_check(tmp_path / "copy.sqlite3", "integrity_check", deadline=100)


def test_retired_integrity_helper_cannot_mask_production_checks():
    assert not hasattr(backup, "_sqlite_integrity_check")


@pytest.mark.parametrize("verdicts", [[], [("ok",), ("broken",)]])
def test_production_integrity_requires_exactly_one_ok(tmp_path, monkeypatch, verdicts):
    class Reader:
        def execute(self, sql):
            assert sql == "PRAGMA integrity_check"
            return self

        def fetchall(self):
            return verdicts

        def close(self):
            pass

    monkeypatch.setattr(verification.sqlite3, "connect", lambda *a, **k: Reader())
    with pytest.raises(backup.PalaceSnapshotError, match="integrity check did not return ok"):
        verification.sqlite_check(tmp_path / "copy.sqlite3", "integrity_check")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows directory junction contract")
def test_weekly_accepts_real_junction_backup_root(tmp_path):
    from test_backup_snapshot import _build_palace

    live = _build_palace(tmp_path / "live")
    target = tmp_path / "backup-target"
    target.mkdir()
    alias = tmp_path / "backup-alias"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(alias), str(target)],
        capture_output=True,
        text=True,
        timeout=10,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert backup._is_link_or_reparse(alias)
    receipt = backup.stage_palace_snapshot(live, alias / "one", use_maintenance_marker=False)
    assert receipt["status"] == "complete"
    result = backup.verify_weekly_snapshot(alias, palace_path=live, budget_seconds=30)
    assert result["status"] == "complete"
