"""Second-review regressions through the production weekly and readback paths."""

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mempalace import backup_snapshot as backup
from mempalace import snapshot_verification as verification
from test_snapshot_quick_weekly import stage


@pytest.mark.parametrize(
    "damage",
    [
        "truncated",
        "not-object",
        "schema",
        "timestamp-missing",
        "timestamp-type",
        "timestamp-naive",
        "source-missing",
        "source-type",
        "reparse",
    ],
)
def test_bad_sibling_never_hides_good_copy(tmp_path, monkeypatch, damage):
    live, good, _ = stage(tmp_path)
    bad = good.parent / "bad"
    bad.mkdir()
    path = bad / backup.SNAPSHOT_RECEIPT_FILENAME
    receipt = json.loads((good / backup.SNAPSHOT_RECEIPT_FILENAME).read_text())
    if damage == "truncated":
        path.write_text("{", encoding="utf-8")
    else:
        if damage == "not-object":
            receipt = []
        elif damage == "schema":
            receipt["schema"] = "unknown"
        elif damage == "timestamp-missing":
            receipt.pop("generatedAt")
        elif damage == "timestamp-type":
            receipt["generatedAt"] = 42
        elif damage == "timestamp-naive":
            receipt["generatedAt"] = "2040-01-01T00:00:00"
        elif damage == "source-missing":
            receipt.pop("palaceRoot")
        elif damage == "source-type":
            receipt["palaceRoot"] = []
        path.write_text(json.dumps(receipt), encoding="utf-8")
    original = backup._is_link_or_reparse
    if damage == "reparse":
        monkeypatch.setattr(backup, "_is_link_or_reparse", lambda p: p == bad or original(p))
    # Pin the bad sibling first; filesystem enumeration order must not matter.
    original_iterdir = Path.iterdir
    monkeypatch.setattr(
        Path, "iterdir", lambda p: iter([bad, good]) if p == good.parent else original_iterdir(p)
    )
    result = backup.verify_weekly_snapshot(good.parent, palace_path=live, budget_seconds=30)
    assert result["status"] == "complete"
    assert result["integrityCheck"] == "ok"
    assert result["scanErrorCount"] == 1
    assert result["scanErrors"][0]["generation"] == "bad"
    assert result["scanErrors"][0]["message"]
    assert json.loads((good / verification.WEEKLY_FILENAME).read_text()) == result


def test_all_bad_generations_save_error_in_backup_root(tmp_path):
    live, generation, _ = stage(tmp_path)
    (generation / backup.SNAPSHOT_RECEIPT_FILENAME).write_text("{", encoding="utf-8")
    result = backup.verify_weekly_snapshot(generation.parent, palace_path=live, budget_seconds=30)
    assert result["status"] == "error"
    assert "no complete snapshot copy" in result["message"]
    assert result["scanErrorCount"] == 1
    assert result["inputBytes"] is None
    assert json.loads((generation.parent / verification.WEEKLY_FILENAME).read_text()) == result


def test_unsafe_sidecar_saves_failure_at_safe_backup_root(tmp_path, monkeypatch):
    live, generation, _ = stage(tmp_path)
    sidecar = generation / verification.WEEKLY_FILENAME
    original = backup._is_link_or_reparse
    monkeypatch.setattr(backup, "_is_link_or_reparse", lambda p: p == sidecar or original(p))
    result = backup.verify_weekly_snapshot(generation.parent, palace_path=live, budget_seconds=30)
    assert result["status"] == "error"
    assert "links or reparse points" in result["message"]
    assert result["receiptLocation"] == "backup-root"
    assert not sidecar.exists()
    assert json.loads((generation.parent / verification.WEEKLY_FILENAME).read_text()) == result


@pytest.mark.parametrize("damage", ["missing-database", "wrong-stage", "live-root"])
def test_selected_copy_precondition_overwrites_old_success(tmp_path, damage):
    live, generation, receipt = stage(tmp_path)
    backup.verify_weekly_snapshot(generation.parent, palace_path=live, budget_seconds=30)
    expected = "refuses live database"
    if damage == "missing-database":
        (generation / "palace/chroma.sqlite3").rename(generation / "retained.sqlite3")
        expected = "chroma.sqlite3"
    elif damage == "wrong-stage":
        receipt["stagedRoot"] = str(tmp_path / "wrong")
        (generation / backup.SNAPSHOT_RECEIPT_FILENAME).write_text(json.dumps(receipt))
        expected = "staged root does not match"
    else:
        live = generation.parent
        before = (generation / verification.WEEKLY_FILENAME).read_bytes()
        with pytest.raises(backup.PalaceSnapshotError, match="backup root.*live"):
            backup.verify_weekly_snapshot(generation.parent, palace_path=live, budget_seconds=30)
        assert (generation / verification.WEEKLY_FILENAME).read_bytes() == before
        assert not (generation.parent / verification.WEEKLY_FILENAME).exists()
        return
    result = backup.verify_weekly_snapshot(generation.parent, palace_path=live, budget_seconds=30)
    assert result["status"] == "error"
    assert expected in result["message"]
    assert json.loads((generation / verification.WEEKLY_FILENAME).read_text()) == result
    assert result["processedCount"] == 0
    assert result["errorCount"] == 1


@pytest.mark.parametrize("recorded_source_exists", [True, False])
def test_weekly_rejects_hard_linked_segments(tmp_path, recorded_source_exists):
    live, generation, receipt = stage(tmp_path)
    backup.verify_weekly_snapshot(generation.parent, palace_path=live, budget_seconds=30)
    relative = next(
        e["relativePath"]
        for e in receipt["contentIdentity"]["files"]
        if e["relativePath"].endswith("data_level0.bin")
    )
    copy = generation / "palace" / relative
    copy.rename(tmp_path / "retained-segment.bin")
    os.link(live / relative, copy)
    assert copy.stat().st_nlink == 2
    if not recorded_source_exists:
        live.rename(tmp_path / "relocated-live")
    result = backup.verify_weekly_snapshot(generation.parent, palace_path=live, budget_seconds=30)
    assert result["status"] == "error"
    assert "hard link" in result["message"]
    assert relative in result["message"]
    assert json.loads((generation / verification.WEEKLY_FILENAME).read_text()) == result


@pytest.mark.parametrize("location", ["generation", "receipt"])
def test_readback_checks_unresolved_receipt_before_reading(tmp_path, monkeypatch, location):
    live, generation, _ = stage(tmp_path)
    backup.verify_weekly_snapshot(generation.parent, palace_path=live, budget_seconds=30)
    path = generation / backup.SNAPSHOT_RECEIPT_FILENAME
    rejected = generation if location == "generation" else path
    original = backup._is_link_or_reparse
    monkeypatch.setattr(backup, "_is_link_or_reparse", lambda p: p == rejected or original(p))
    original_read = Path.read_text

    def read_text(p, *args, **kwargs):
        assert p != path, "redirected receipt was read before the link guard"
        return original_read(p, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    with pytest.raises(backup.PalaceSnapshotError, match="links or reparse points"):
        backup.verify_snapshot_receipt(path)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows directory junction contract")
@pytest.mark.parametrize("entrypoint", ["mempalace.cli", "mempalace.backup_snapshot"])
def test_native_receipt_cli_refuses_generation_junction(tmp_path, entrypoint):
    live, generation, _ = stage(tmp_path)
    backup.verify_weekly_snapshot(generation.parent, palace_path=live, budget_seconds=30)
    alias = generation.parent / "redirected"
    linked = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(alias), str(generation)],
        capture_output=True,
        text=True,
        timeout=10,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert linked.returncode == 0, linked.stdout + linked.stderr
    assert alias.resolve() == generation.resolve()
    command = [sys.executable, "-B", "-m", entrypoint]
    if entrypoint == "mempalace.cli":
        command.append("backup-snapshot")
    result = subprocess.run(
        command + ["--verify-receipt", str(alias / backup.SNAPSHOT_RECEIPT_FILENAME), "--json"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode != 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["status"] == "error"
    assert "links or reparse points" in payload["message"]
    assert "Traceback" not in result.stderr


def test_weekly_json_explicitly_requests_ascii(tmp_path, monkeypatch, capsys):
    live, generation, _ = stage(tmp_path)
    original = json.dumps
    calls = []

    def dumps(value, **kwargs):
        if isinstance(value, dict) and value.get("schema") == verification.WEEKLY_SCHEMA:
            calls.append(kwargs)
        return original(value, **kwargs)

    monkeypatch.setattr(backup.json, "dumps", dumps)
    assert (
        backup.main(
            ["--weekly-verify-root", str(generation.parent), "--palace-path", str(live), "--json"]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "complete"
    assert calls and all(k.get("ensure_ascii") is True for k in calls)


def test_weekly_unicode_error_is_saved_and_printed_on_cp1252(tmp_path, monkeypatch):
    live, generation, _ = stage(tmp_path)
    message = "IO failure: \u05d0\u05d1\u05d2/\u4e2d\u6587"

    def fail(*args):
        raise OSError(message)

    monkeypatch.setattr(verification, "full_copy_proof", fail)
    stream = io.BytesIO()
    output = io.TextIOWrapper(stream, encoding="cp1252", errors="strict")
    with monkeypatch.context() as context:
        context.setattr(sys, "stdout", output)
        assert (
            backup.main(
                [
                    "--weekly-verify-root",
                    str(generation.parent),
                    "--palace-path",
                    str(live),
                    "--json",
                ]
            )
            == 2
        )
        output.flush()
    printed = json.loads(stream.getvalue().decode("ascii"))
    saved = json.loads((generation / verification.WEEKLY_FILENAME).read_text())
    assert printed == saved
    assert printed["message"] == message
    assert printed["status"] == "error"
