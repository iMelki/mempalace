"""Nightly structural proof and separately bounded copy-only full proof."""

import hashlib
import json
import sqlite3
from contextlib import closing

import pytest

from mempalace import backup_snapshot as backup
from test_backup_snapshot import _build_palace


def damaged_page(database, target):
    with closing(sqlite3.connect(database)) as reader:
        page_size = reader.execute("PRAGMA page_size").fetchone()[0]
        page = reader.execute(
            "SELECT rootpage FROM sqlite_schema WHERE name=?", (target,)
        ).fetchone()[0]
    with database.open("r+b") as stream:
        stream.seek((page - 1) * page_size)
        original = stream.read(1)
        assert original in (b"\x0a", b"\x0d")
        stream.seek(-1, 1)
        stream.write(b"\x00")


@pytest.mark.parametrize("target", ["entries", "by_value"])
def test_nightly_refuses_corrupted_table_and_index_pages(tmp_path, target):
    database = tmp_path / "catalog.sqlite3"
    with closing(sqlite3.connect(database)) as writer, writer:
        writer.executescript(
            "CREATE TABLE entries(id INTEGER PRIMARY KEY, value TEXT);"
            "INSERT INTO entries VALUES(1,'one');"
            "CREATE INDEX by_value ON entries(value);"
        )
    damaged_page(database, target)
    with pytest.raises(backup.PalaceSnapshotError, match="quick check"):
        backup._sqlite_quick_check(database)


def stage(tmp_path):
    live = _build_palace(tmp_path / "live")
    staging = tmp_path / "generations" / "20261011T000000Z"
    receipt = backup.stage_palace_snapshot(live, staging, use_maintenance_marker=False)
    return live, staging, receipt


def test_nightly_records_structural_check_and_missing_weekly_receipt(tmp_path):
    _, staging, receipt = stage(tmp_path)
    sql = receipt["sqliteSnapshot"]
    assert sql["quickCheck"] == "ok"
    assert sql["integrityCheck"] == "deferred-weekly"
    assert sql["structure"]["pageCount"] > 0
    assert sql["structure"]["freelistCount"] >= 0
    assert sql["structure"]["schemaHash"].startswith("sha256:")
    result = backup.verify_snapshot_receipt(staging / backup.SNAPSHOT_RECEIPT_FILENAME)
    assert result["valid"]
    assert result["weeklyVerification"]["status"] == "missing"


def test_weekly_selects_latest_copy_and_preserves_nightly_receipt(tmp_path):
    live, staging, _ = stage(tmp_path)
    path = staging / backup.SNAPSHOT_RECEIPT_FILENAME
    before = path.read_bytes()
    result = backup.verify_weekly_snapshot(staging.parent, palace_path=live, budget_seconds=30)
    assert result["status"] == "complete"
    assert result["integrityCheck"] == "ok"
    assert result["snapshotReceiptSha256"] == "sha256:" + hashlib.sha256(before).hexdigest()
    assert path.read_bytes() == before
    saved = json.loads((staging / "weekly-integrity-receipt.json").read_text())
    assert saved["status"] == "complete"
    assert backup.verify_snapshot_receipt(path)["weeklyVerification"]["status"] == "complete"


def test_weekly_refuses_live_database_even_when_receipt_points_to_it(tmp_path):
    _, staging, receipt = stage(tmp_path)
    assert receipt["stagedRoot"] == str(staging / "palace")
    with pytest.raises(backup.PalaceSnapshotError, match="refuses live database$"):
        backup.verify_weekly_snapshot(staging.parent, palace_path=staging.parent, budget_seconds=30)


def test_weekly_reports_timeout_without_nightly_success_rewrite(tmp_path):
    live, staging, _ = stage(tmp_path)
    result = backup.verify_weekly_snapshot(
        staging.parent, palace_path=live, budget_seconds=0.000001
    )
    assert result["status"] == "error"
    assert "budget" in result["message"]
    assert (
        json.loads((staging / backup.SNAPSHOT_RECEIPT_FILENAME).read_text())["status"] == "complete"
    )


def test_weekly_full_check_rejects_missing_index_rows_that_nightly_accepts(tmp_path, capsys):
    live = _build_palace(tmp_path / "live")
    database = live / "chroma.sqlite3"
    with closing(sqlite3.connect(database)) as writer, writer:
        writer.execute("CREATE INDEX by_embedding_id ON embeddings(embedding_id)")
        page_size = writer.execute("PRAGMA page_size").fetchone()[0]
        page = writer.execute(
            "SELECT rootpage FROM sqlite_schema WHERE name='by_embedding_id'"
        ).fetchone()[0]
    with database.open("r+b") as stream:
        stream.seek((page - 1) * page_size + 3)
        stream.write(b"\x00\x00" + page_size.to_bytes(2, "big"))
    staging = tmp_path / "generations" / "one"
    receipt = backup.stage_palace_snapshot(live, staging, use_maintenance_marker=False)
    assert receipt["sqliteSnapshot"]["quickCheck"] == "ok"
    result = backup.verify_weekly_snapshot(staging.parent, palace_path=live, budget_seconds=30)
    assert result["status"] == "error"
    assert "missing from index by_embedding_id" in result["message"]
    assert backup.main(["--verify-receipt", str(staging / backup.SNAPSHOT_RECEIPT_FILENAME)]) == 2
    output = capsys.readouterr().out
    assert "outcome=failed" in output
    assert "missing from index by_embedding_id" in output


@pytest.mark.parametrize("breakage", ["stale", "identity", "failed", "malformed"])
def test_weekly_status_never_accepts_bad_receipt(tmp_path, breakage):
    from datetime import datetime, timedelta, timezone

    live, staging, _ = stage(tmp_path)
    backup.verify_weekly_snapshot(staging.parent, palace_path=live, budget_seconds=30)
    path = staging / "weekly-integrity-receipt.json"
    weekly = json.loads(path.read_text())
    if breakage == "stale":
        weekly["finishedAt"] = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    elif breakage == "identity":
        weekly["snapshotReceiptSha256"] = "sha256:" + "0" * 64
    elif breakage == "failed":
        weekly["status"] = "error"
    else:
        weekly = []
    path.write_text(json.dumps(weekly))
    result = backup.verify_snapshot_receipt(staging / backup.SNAPSHOT_RECEIPT_FILENAME)
    assert result["weeklyVerification"]["status"] != "complete"


@pytest.mark.parametrize("producer_now", ["2026-10-13T00:00:00Z", "2040-01-01T00:00:00Z"])
def test_weekly_checks_newest_generation_and_ignores_pending_copy(
    tmp_path, monkeypatch, producer_now
):
    monkeypatch.setattr(backup, "_utc_now", lambda: producer_now)
    live, first, _ = stage(tmp_path)
    first_path = first / backup.SNAPSHOT_RECEIPT_FILENAME
    first_snapshot = json.loads(first_path.read_text())
    first_snapshot["generatedAt"] = "2026-10-11T00:00:00Z"
    first_path.write_text(json.dumps(first_snapshot))
    newer = first.parent / "newer"
    backup.stage_palace_snapshot(live, newer, use_maintenance_marker=False)
    path = newer / backup.SNAPSHOT_RECEIPT_FILENAME
    snapshot = json.loads(path.read_text())
    snapshot["generatedAt"] = "2026-10-12T00:00:00Z"
    path.write_text(json.dumps(snapshot))
    backup.stage_palace_snapshot(
        live, first.parent / "pending", use_maintenance_marker=False, verify=False
    )
    result = backup.verify_weekly_snapshot(first.parent, palace_path=live, budget_seconds=30)
    assert result["snapshotGeneratedAt"] == "2026-10-12T00:00:00Z"
    assert (newer / "weekly-integrity-receipt.json").is_file()
    assert not (first / "weekly-integrity-receipt.json").exists()


def test_weekly_refuses_a_hard_link_to_live_sqlite(tmp_path):
    import os

    live = _build_palace(tmp_path / "live")
    staging = tmp_path / "generations" / "one"
    (staging / "palace").mkdir(parents=True)
    os.link(live / "chroma.sqlite3", staging / "palace" / "chroma.sqlite3")
    receipt = {
        "schema": backup.SNAPSHOT_RECEIPT_SCHEMA,
        "status": "complete",
        "generatedAt": "2026-10-11T00:00:00Z",
        "palaceRoot": str(live),
        "stagedRoot": str(staging / "palace"),
    }
    (staging / backup.SNAPSHOT_RECEIPT_FILENAME).write_text(json.dumps(receipt))
    with pytest.raises(backup.PalaceSnapshotError, match="hard link"):
        backup.verify_weekly_snapshot(staging.parent, palace_path=live, budget_seconds=30)


def test_weekly_cli_reports_result_and_nonzero_timeout(tmp_path, capsys):
    live, staging, _ = stage(tmp_path)
    args = ["--weekly-verify-root", str(staging.parent), "--palace-path", str(live), "--json"]
    assert backup.main(args) == 0
    assert json.loads(capsys.readouterr().out)["integrityCheck"] == "ok"
    assert backup.main(args + ["--weekly-budget-seconds", "0.000001"]) == 2
    assert "budget" in json.loads(capsys.readouterr().out)["message"]


def test_structure_mismatch_fails_snapshot_closed(tmp_path, monkeypatch):
    from mempalace import snapshot_verification

    original = snapshot_verification.sqlite_structure

    def mismatched(database):
        structure = original(database)
        if "generations" in database.parts:
            structure["pageCount"] += 1
        return structure

    monkeypatch.setattr(snapshot_verification, "sqlite_structure", mismatched)
    with pytest.raises(backup.PalaceSnapshotError, match="schema hash mismatch"):
        stage(tmp_path)


def test_weekly_sqlite_vm_deadline_is_attributed_as_budget_failure(tmp_path):
    import time
    from mempalace.snapshot_verification import sqlite_check

    database = tmp_path / "large.sqlite3"
    with closing(sqlite3.connect(database)) as writer, writer:
        writer.execute("CREATE TABLE rows(value TEXT UNIQUE)")
        writer.executemany("INSERT INTO rows VALUES(?)", ((str(i),) for i in range(20000)))
    with pytest.raises(backup.PalaceSnapshotError, match="budget exceeded"):
        sqlite_check(database, "integrity_check", deadline=time.monotonic() + 0.000001)


@pytest.mark.parametrize("verdicts", [[], [("ok",), ("broken",)]])
def test_nightly_requires_exactly_one_ok_verdict(tmp_path, monkeypatch, verdicts):
    from mempalace import snapshot_verification

    class Reader:
        def execute(self, sql):
            assert sql == "PRAGMA quick_check"
            return self

        def fetchall(self):
            return verdicts

        def close(self):
            pass

    monkeypatch.setattr(snapshot_verification.sqlite3, "connect", lambda *a, **k: Reader())
    with pytest.raises(backup.PalaceSnapshotError, match="quick check did not return ok"):
        backup._sqlite_quick_check(tmp_path / "copy.sqlite3")


def test_weekly_never_connects_to_live_or_acquires_a_lease(tmp_path, monkeypatch):
    live, staging, _ = stage(tmp_path)
    real_connect = sqlite3.connect
    connected = []

    def connect(database_uri, *args, **kwargs):
        connected.append(database_uri)
        assert "/live/" not in database_uri
        assert "mode=ro" in database_uri
        return real_connect(database_uri, *args, **kwargs)

    def forbidden_lease(*args, **kwargs):
        raise AssertionError("weekly job acquired a lease")

    monkeypatch.setattr(sqlite3, "connect", connect)
    monkeypatch.setattr(backup, "clean_client_lease", forbidden_lease)
    result = backup.verify_weekly_snapshot(staging.parent, palace_path=live, budget_seconds=30)
    assert result["status"] == "complete"
    assert connected


def test_weekly_no_complete_copy_is_a_clear_refusal(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    with pytest.raises(backup.PalaceSnapshotError, match="no complete snapshot copy"):
        backup.verify_weekly_snapshot(root, palace_path=tmp_path / "live", budget_seconds=30)


@pytest.mark.parametrize("budget", [0, -1, float("inf"), float("nan")])
def test_weekly_invalid_budget_is_refused(tmp_path, budget):
    with pytest.raises(backup.PalaceSnapshotError, match="finite and positive"):
        backup.verify_weekly_snapshot(
            tmp_path, palace_path=tmp_path / "live", budget_seconds=budget
        )


def test_cli_readback_reports_missing_weekly_receipt_in_plain_output(tmp_path, capsys):
    _, staging, _ = stage(tmp_path)
    assert backup.main(["--verify-receipt", str(staging / backup.SNAPSHOT_RECEIPT_FILENAME)]) == 3
    output = capsys.readouterr().out
    assert "Weekly full integrity: missing" in output
    assert "weekly-integrity-receipt-missing" in output


@pytest.mark.skipif(
    sqlite3.sqlite_version_info < (3, 45, 1), reason="read-only FTS5 needs SQLite 3.45.1"
)
def test_nightly_quick_check_retains_fts5_corruption_detection(tmp_path):
    database = tmp_path / "fts.sqlite3"
    with closing(sqlite3.connect(database)) as writer, writer:
        writer.executescript(
            "CREATE VIRTUAL TABLE ft USING fts5(value, tokenize=trigram);"
            "INSERT INTO ft(value) VALUES('abcdef');"
            "UPDATE ft_content SET c0='xyz';"
        )
    with pytest.raises(backup.PalaceSnapshotError, match="malformed inverted index"):
        backup._sqlite_quick_check(database)


def test_weekly_cli_precondition_error_has_weekly_schema(tmp_path, capsys):
    from mempalace.snapshot_verification import WEEKLY_SCHEMA

    assert (
        backup.main(
            ["--weekly-verify-root", str(tmp_path), "--weekly-budget-seconds", "0", "--json"]
        )
        == 1
    )
    result = json.loads(capsys.readouterr().out)
    assert result["schema"] == WEEKLY_SCHEMA
    assert result["status"] == "error"
    assert "finite and positive" in result["message"]
    assert "leaseProven" not in result


def test_real_module_cli_returns_structured_weekly_success_and_refusal(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    from mempalace.snapshot_verification import WEEKLY_SCHEMA

    live, staging, _ = stage(tmp_path)
    before = (staging / backup.SNAPSHOT_RECEIPT_FILENAME).read_bytes()
    command = [
        sys.executable,
        "-B",
        "-m",
        "mempalace.backup_snapshot",
        "--weekly-verify-root",
        str(staging.parent),
        "--palace-path",
        str(live),
        "--json",
    ]
    launch = dict(
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    refused = subprocess.run(command + ["--weekly-budget-seconds", "0"], **launch)
    assert refused.returncode == 1
    error = json.loads(refused.stdout)
    assert error["schema"] == WEEKLY_SCHEMA
    assert "finite and positive" in error["message"]
    assert "Traceback" not in refused.stderr
    passed = subprocess.run(command, **launch)
    assert passed.returncode == 0, passed.stderr
    assert json.loads(passed.stdout)["status"] == "complete"
    saved = json.loads((staging / "weekly-integrity-receipt.json").read_text())
    assert saved["schema"] == WEEKLY_SCHEMA
    assert saved["status"] == "complete"
    assert (staging / backup.SNAPSHOT_RECEIPT_FILENAME).read_bytes() == before
