"""Round-three review reproductions against copy-only snapshot entrypoints."""

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from mempalace import backup_snapshot as backup
from mempalace import snapshot_verification as verification
from test_backup_snapshot import _build_palace
from test_snapshot_quick_weekly import stage


def segment(receipt):
    return next(
        e["relativePath"]
        for e in receipt["contentIdentity"]["files"]
        if e["relativePath"].endswith("data_level0.bin")
    )


def hardlink(source, target, retained):
    target.rename(retained)
    os.link(source, target)
    assert target.stat().st_nlink == 2
    assert os.path.samefile(source, target)


def junction(source, target):
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(target), str(source)],
        capture_output=True,
        text=True,
        timeout=10,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert target.resolve() == source.resolve()


@pytest.mark.parametrize("entrypoint", ["mempalace.cli", "mempalace.backup_snapshot"])
@pytest.mark.parametrize("damage", ["hardlink", "junction"])
def test_readback_refuses_live_segment_links(tmp_path, entrypoint, damage):
    if damage == "junction" and sys.platform != "win32":
        pytest.skip("Windows directory junction contract")
    live, generation, receipt = stage(tmp_path)
    assert (
        backup.verify_weekly_snapshot(generation.parent, palace_path=live)["status"] == "complete"
    )
    relative = segment(receipt)
    copy = generation / "palace" / relative
    if damage == "hardlink":
        hardlink(live / relative, copy, tmp_path / "retained.bin")
    else:
        copy.parent.rename(tmp_path / "retained-segment")
        junction((live / relative).parent, copy.parent)
    command = [sys.executable, "-B", "-m", entrypoint]
    if entrypoint == "mempalace.cli":
        command.append("backup-snapshot")
    result = subprocess.run(
        command
        + ["--verify-receipt", str(generation / backup.SNAPSHOT_RECEIPT_FILENAME), "--json"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    assert result.returncode == 2, result.stdout
    payload = json.loads(result.stdout)
    diagnostic = json.dumps(payload)
    assert ("hard link" if damage == "hardlink" else "reparse points") in diagnostic
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("location", ["root", "metadata", "junction"])
def test_weekly_never_writes_fallback_inside_live(tmp_path, location):
    if location == "junction" and sys.platform != "win32":
        pytest.skip("Windows directory junction contract")
    live, _, _ = stage(tmp_path)
    root = live
    if location == "metadata":
        root = live / ".mempalace"
    elif location == "junction":
        root = tmp_path / "live-alias"
        junction(live, root)
    before = {p.relative_to(live): p.read_bytes() for p in live.rglob("*") if p.is_file()}
    with pytest.raises(backup.PalaceSnapshotError, match="backup root.*live"):
        backup.verify_weekly_snapshot(root, palace_path=live)
    after = {p.relative_to(live): p.read_bytes() for p in live.rglob("*") if p.is_file()}
    assert before == after


def deferred(tmp_path):
    live = _build_palace(tmp_path / "live")
    generation = tmp_path / "generations" / "one"
    backup.stage_palace_snapshot(live, generation, use_maintenance_marker=False, verify=False)
    return live, generation


def test_deferred_refuses_live_catalog_before_quick_check(tmp_path, monkeypatch):
    live, generation = deferred(tmp_path)
    database = generation / "palace" / "chroma.sqlite3"
    hardlink(live / "chroma.sqlite3", database, tmp_path / "retained.sqlite3")
    opened = []
    original = backup._sqlite_quick_check

    def quick(path):
        opened.append(path)
        return original(path)

    monkeypatch.setattr(backup, "_sqlite_quick_check", quick)
    try:
        result = backup.verify_staged_snapshot(generation)
    except backup.PalaceSnapshotError as exc:
        assert "hard link" in str(exc)
    else:
        assert result["status"] == "error", "linked live catalog was certified complete"
        assert "hard link" in result["verification"]["message"]
    assert not opened, "quick_check opened the live inode before isolation"


@pytest.mark.parametrize("failure", [False, True])
def test_deferred_receipt_publication_preserves_linked_outside_file(tmp_path, failure):
    _, generation = deferred(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    receipt = generation / backup.SNAPSHOT_RECEIPT_FILENAME
    hardlink(outside, receipt, tmp_path / "retained-receipt.json")
    if failure:
        (generation / "palace" / "chroma.sqlite3").write_bytes(b"corrupt database")
    try:
        backup.verify_staged_snapshot(generation)
    except backup.PalaceSnapshotError as exc:
        assert "hard link" in str(exc)
    assert outside.read_text(encoding="utf-8") == "keep", (
        "receipt publisher truncated outside inode"
    )


@pytest.mark.skipif(sys.platform != "win32", reason="Windows read-only replacement contract")
@pytest.mark.parametrize("failure", ["hardlink", "full-check"])
def test_readonly_sidecar_cannot_leave_accepted_old_success(tmp_path, monkeypatch, failure):
    live, generation, receipt = stage(tmp_path)
    assert (
        backup.verify_weekly_snapshot(generation.parent, palace_path=live)["status"] == "complete"
    )
    sidecar = generation / verification.WEEKLY_FILENAME
    old = sidecar.read_bytes()
    if failure == "hardlink":
        relative = segment(receipt)
        hardlink(live / relative, generation / "palace" / relative, tmp_path / "retained.bin")
    else:

        def fail(*args):
            raise backup.PalaceSnapshotError("forced full integrity failure")

        monkeypatch.setattr(verification, "full_copy_proof", fail)
    os.chmod(sidecar, stat.S_IREAD)
    try:
        result = backup.verify_weekly_snapshot(generation.parent, palace_path=live)
        assert result["status"] == "error"
        assert result["receiptLocation"] == "backup-root"
        assert result["receiptWriteError"]
        assert sidecar.read_bytes() == old
        assert json.loads((generation.parent / verification.WEEKLY_FILENAME).read_text()) == result
        payload = backup.verify_snapshot_receipt(generation / backup.SNAPSHOT_RECEIPT_FILENAME)
        assert verification.receipt_verification_exit_code(payload) == 2, payload
    finally:
        os.chmod(sidecar, stat.S_IWRITE | stat.S_IREAD)


@pytest.mark.parametrize("good_copy", [False, True])
def test_deep_sibling_is_recorded_without_aborting_scan(tmp_path, good_copy):
    live, generation, _ = stage(tmp_path)
    bad = generation.parent / "bad"
    bad.mkdir()
    (bad / backup.SNAPSHOT_RECEIPT_FILENAME).write_text("[" * 20000 + "]" * 20000)
    if not good_copy:
        (generation / backup.SNAPSHOT_RECEIPT_FILENAME).write_text("{")
    result = backup.verify_weekly_snapshot(generation.parent, palace_path=live)
    assert result["status"] == ("complete" if good_copy else "error")
    assert any(
        e["generation"] == "bad" and "recursion" in e["message"] for e in result["scanErrors"]
    )
    destination = generation if good_copy else generation.parent
    assert json.loads((destination / verification.WEEKLY_FILENAME).read_text()) == result


@pytest.mark.parametrize("verify_hashes", [False, True])
def test_readback_isolation_precedes_hash_reads_and_survives_source_move(
    tmp_path, monkeypatch, verify_hashes
):
    live, generation, receipt = stage(tmp_path)
    relative = segment(receipt)
    hardlink(live / relative, generation / "palace" / relative, tmp_path / "retained.bin")
    live.rename(tmp_path / "moved-live")

    def forbidden(path):
        pytest.fail("isolation guard allowed staged hash access")

    monkeypatch.setattr(backup, "sha256_file", forbidden)
    result = backup.verify_snapshot_receipt(
        generation / backup.SNAPSHOT_RECEIPT_FILENAME, verify_hashes=verify_hashes
    )
    assert not result["valid"]
    assert result["hashesVerified"] == 0
    assert any("hard link" in p for p in result["problems"])


def test_independent_relocated_archive_override_still_valid(tmp_path):
    live, generation, _ = stage(tmp_path)
    assert (
        backup.verify_weekly_snapshot(generation.parent, palace_path=live)["status"] == "complete"
    )
    relocated = tmp_path / "archive"
    shutil.copytree(generation, relocated)
    result = backup.verify_snapshot_receipt(
        relocated / backup.SNAPSHOT_RECEIPT_FILENAME, staged_root=relocated / "palace"
    )
    assert result["valid"]
    assert verification.receipt_verification_exit_code(result) == 0


@pytest.mark.skipif(sys.platform != "win32", reason="Windows directory junction contract")
def test_staged_root_override_cannot_resolve_away_junction(tmp_path):
    live, generation, _ = stage(tmp_path)
    alias = tmp_path / "linked-copy"
    junction(generation / "palace", alias)
    result = backup.verify_snapshot_receipt(
        generation / backup.SNAPSHOT_RECEIPT_FILENAME, staged_root=alias
    )
    assert not result["valid"]
    assert any("reparse points" in p for p in result["problems"])


@pytest.mark.skipif(sys.platform != "win32", reason="Windows read-only replacement contract")
def test_generation_failure_survives_other_root_error_and_recovers(tmp_path, monkeypatch):
    live, generation, _ = stage(tmp_path)
    backup.verify_weekly_snapshot(generation.parent, palace_path=live)
    sidecar = generation / verification.WEEKLY_FILENAME
    os.chmod(sidecar, stat.S_IREAD)
    try:
        with monkeypatch.context() as context:

            def fail(*args):
                raise backup.PalaceSnapshotError("forced full integrity failure")

            context.setattr(verification, "full_copy_proof", fail)
            failure = backup.verify_weekly_snapshot(generation.parent, palace_path=live)
        assert (
            json.loads((generation / verification.WEEKLY_INVALIDATION_FILENAME).read_text())
            == failure
        )
        unrelated = {**failure, "snapshotReceiptSha256": "sha256:unrelated"}
        verification.write_weekly_receipt(
            generation.parent / verification.WEEKLY_FILENAME, unrelated
        )
        path = generation / backup.SNAPSHOT_RECEIPT_FILENAME
        assert (
            verification.receipt_verification_exit_code(backup.verify_snapshot_receipt(path)) == 2
        )
    finally:
        os.chmod(sidecar, stat.S_IWRITE | stat.S_IREAD)
    assert (
        backup.verify_weekly_snapshot(generation.parent, palace_path=live)["status"] == "complete"
    )
    assert verification.receipt_verification_exit_code(backup.verify_snapshot_receipt(path)) == 0


@pytest.mark.parametrize("error", [RecursionError, TypeError, RuntimeError])
def test_unexpected_sibling_parser_errors_remain_isolated(tmp_path, monkeypatch, error):
    live, generation, _ = stage(tmp_path)
    bad = generation.parent / "bad"
    bad.mkdir()
    original = verification.read_generation

    def read(path, root):
        if path.parent == bad:
            raise error("injected sibling parser failure")
        return original(path, root)

    monkeypatch.setattr(verification, "read_generation", read)
    result = backup.verify_weekly_snapshot(generation.parent, palace_path=live)
    assert result["status"] == "complete"
    assert result["scanErrors"] == [
        {"generation": "bad", "message": "injected sibling parser failure"}
    ]


def test_recorded_live_root_blocks_fallback_even_with_wrong_caller_root(tmp_path, monkeypatch):
    live, generation, _ = stage(tmp_path)
    unsafe_generation = live / "copied-generation"
    shutil.copytree(generation, unsafe_generation)
    sidecar = unsafe_generation / verification.WEEKLY_FILENAME
    original = backup._is_link_or_reparse
    monkeypatch.setattr(backup, "_is_link_or_reparse", lambda p: p == sidecar or original(p))
    before = {p.relative_to(live): p.read_bytes() for p in live.rglob("*") if p.is_file()}
    with pytest.raises(backup.PalaceSnapshotError, match="backup root.*live"):
        backup.verify_weekly_snapshot(live, palace_path=tmp_path / "wrong-live")
    assert before == {p.relative_to(live): p.read_bytes() for p in live.rglob("*") if p.is_file()}
