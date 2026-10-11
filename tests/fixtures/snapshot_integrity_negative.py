"""Exercise the real snapshot verifier on an index mismatch and its restored input."""

import argparse
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from mempalace.backup_snapshot import PalaceSnapshotError, _sqlite_integrity_check


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--broken", action="store_true")
    args = parser.parse_args()
    # Keep the disposable inputs for readback; cleanup needs the operator's recycle tool.
    directory = Path(tempfile.mkdtemp(prefix="snapshot-integrity-gate-"))
    database = directory / "catalog.sqlite3"
    with closing(sqlite3.connect(database)) as writer, writer:
        writer.executescript(
            "CREATE TABLE entries(id INTEGER PRIMARY KEY, value TEXT);"
            "INSERT INTO entries VALUES (1, 'one'), (2, 'two');"
            "CREATE INDEX by_value ON entries(value);"
        )
        page_size = writer.execute("PRAGMA page_size").fetchone()[0]
        root_page = writer.execute(
            "SELECT rootpage FROM sqlite_schema WHERE name='by_value'"
        ).fetchone()[0]
    if args.broken:
        with database.open("r+b") as stream:
            stream.seek((root_page - 1) * page_size + 3)
            stream.write(b"\x00\x00" + page_size.to_bytes(2, "big"))
        with closing(sqlite3.connect(database)) as reader:
            assert reader.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    try:
        assert _sqlite_integrity_check(database) == "ok"
    except PalaceSnapshotError as error:
        if not args.broken or "row 1 missing from index by_value" not in str(error):
            print(f"UNEXPECTED: {error}")
            return 2
        print(f"PalaceSnapshotError: {error}")
        return 1
    if args.broken:
        print("FAIL: the full verifier admitted missing index rows")
        return 2
    print("PASS: full snapshot integrity check accepted the restored index")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
