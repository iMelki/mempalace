"""Run actual nightly and weekly gates on retained, disposable copy fixtures."""

import argparse
import json
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from mempalace import backup_snapshot as backup
from mempalace.snapshot_verification import (
    WEEKLY_FILENAME,
    WEEKLY_SCHEMA,
    sqlite_check,
    utc_now,
    weekly_status,
)


def build_database(directory):
    database = directory / "chroma.sqlite3"
    with closing(sqlite3.connect(database)) as writer, writer:
        writer.executescript(
            "CREATE TABLE entries(id INTEGER PRIMARY KEY, value TEXT);"
            "INSERT INTO entries VALUES(1,'one'),(2,'two');"
            "CREATE INDEX by_value ON entries(value);"
        )
        page_size = writer.execute("PRAGMA page_size").fetchone()[0]
        page = writer.execute(
            "SELECT rootpage FROM sqlite_schema WHERE name='by_value'"
        ).fetchone()[0]
    return database, page_size, page


def quick_gate(directory, database, page_size, page, broken):
    if broken:
        with database.open("r+b") as stream:
            stream.seek((page - 1) * page_size)
            assert stream.read(1) == b"\x0a"
            stream.seek(-1, 1)
            stream.write(b"\x00")
            stream.seek(-1, 1)
            assert stream.read(1) == b"\x00"
    backup._sqlite_quick_check(database)


def weekly_gate(directory, database, page_size, page, broken):
    receipt_path = directory / backup.SNAPSHOT_RECEIPT_FILENAME
    digest = backup.sha256_file(database)
    receipt = {"contentIdentity": {"files": [{"relativePath": "chroma.sqlite3", "sha256": digest}]}}
    receipt_path.write_text(json.dumps(receipt))
    if not broken:
        weekly = {
            "schema": WEEKLY_SCHEMA,
            "status": "complete",
            "integrityCheck": "ok",
            "snapshotReceiptSha256": backup.sha256_file(receipt_path),
            "databaseSha256": digest,
            "finishedAt": utc_now(),
        }
        (directory / WEEKLY_FILENAME).write_text(json.dumps(weekly))
    else:
        assert not (directory / WEEKLY_FILENAME).exists()
    result = weekly_status(receipt_path, receipt)
    if broken:
        assert result == {"status": "missing", "problem": "weekly-integrity-receipt-missing"}
        raise backup.PalaceSnapshotError("weekly-integrity-receipt-missing")
    assert result["status"] == "complete"


def structure_gate(directory, database, page_size, page, broken):
    destination = directory / "copy.sqlite3"
    with closing(sqlite3.connect(database)) as reader:
        with closing(sqlite3.connect(destination)) as writer:
            reader.backup(writer)
    if broken:
        with closing(sqlite3.connect(destination)) as writer, writer:
            writer.execute("CREATE TABLE different(value TEXT)")
            assert writer.execute(
                "SELECT name FROM sqlite_schema WHERE name='different'"
            ).fetchone() == ("different",)
    backup._validate_sqlite_structure(database, destination)


def full_gate(directory, database, page_size, page, broken):
    if broken:
        with database.open("r+b") as stream:
            stream.seek((page - 1) * page_size + 3)
            stream.write(b"\x00\x00" + page_size.to_bytes(2, "big"))
        assert backup._sqlite_quick_check(database) == "ok"
        with closing(sqlite3.connect(database)) as reader:
            assert reader.execute(
                "SELECT COUNT(value) FROM entries INDEXED BY by_value"
            ).fetchone() == (0,)
            assert reader.execute("SELECT COUNT(*) FROM entries NOT INDEXED").fetchone() == (2,)
    sqlite_check(database, "integrity_check")


def main():
    gates = {
        "quick": quick_gate,
        "weekly": weekly_gate,
        "structure": structure_gate,
        "weekly-full": full_gate,
    }
    reasons = {
        "quick": "btreeInitPage",
        "weekly": "weekly-integrity-receipt-missing",
        "structure": "schema hash mismatch",
        "weekly-full": "row 1 missing from index by_value",
    }
    parser = argparse.ArgumentParser()
    parser.add_argument("--gate", choices=list(gates), required=True)
    parser.add_argument("--broken", action="store_true")
    args = parser.parse_args()
    directory = Path(tempfile.mkdtemp(prefix="snapshot-split-gate-"))
    database, page_size, page = build_database(directory)
    try:
        gates[args.gate](directory, database, page_size, page, args.broken)
    except backup.PalaceSnapshotError as exc:
        if not args.broken or reasons[args.gate] not in str(exc):
            print("UNEXPECTED:", exc)
            return 2
        print("PalaceSnapshotError:", exc)
        return 1
    if args.broken:
        print("FAIL: broken input accepted")
        return 2
    print("PASS:", args.gate, "restored input accepted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
