"""Characterize strict read-only SQLite verification on real damaged databases."""

import hashlib
import sqlite3
from contextlib import closing

import pytest

from mempalace import backup_snapshot
from mempalace import snapshot_verification
from mempalace.snapshot_verification import sqlite_check


def test_integrity_reader_preserves_settings_and_database_bytes(tmp_path, monkeypatch):
    database = tmp_path / "catalog.sqlite3"
    with closing(sqlite3.connect(database)) as writer, writer:
        writer.execute("CREATE TABLE entries (id INTEGER PRIMARY KEY, value TEXT UNIQUE)")
        writer.execute("INSERT INTO entries VALUES (1, 'one')")
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    real_connect = sqlite3.connect
    observed = []
    with closing(real_connect(database)) as reader:
        default_cache = reader.execute("PRAGMA cache_size").fetchone()[0]
        default_mmap = reader.execute("PRAGMA mmap_size").fetchone()[0]

    class Reader(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql == "PRAGMA integrity_check":
                observed.append(
                    (
                        super().execute("PRAGMA cache_size").fetchone()[0],
                        super().execute("PRAGMA mmap_size").fetchone()[0],
                    )
                )
            return super().execute(sql, *args)

    def connect(*args, **kwargs):
        assert "mode=ro" in args[0]
        return real_connect(*args, factory=Reader, **kwargs)

    monkeypatch.setattr(snapshot_verification.sqlite3, "connect", connect)
    assert sqlite_check(database, "integrity_check") == "ok"
    assert observed == [(default_cache, default_mmap)]
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    assert not database.with_name(database.name + "-wal").exists()
    with closing(real_connect(database)) as reader:
        assert reader.execute("PRAGMA cache_size").fetchone()[0] == default_cache


def test_full_integrity_rejects_index_damage_that_quick_check_accepts(tmp_path):
    database = tmp_path / "damaged.sqlite3"
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
    # Overwrite one index key in place. The entry count stays the same, so
    # quick_check accepts the leaf on SQLite builds that count index entries.
    # integrity_check still reports the mismatched table row.
    with database.open("r+b") as stream:
        stream.seek((root_page - 1) * page_size)
        page = bytearray(stream.read(page_size))
        offset = page.find(b"one")
        assert offset >= 0
        assert page.find(b"one", offset + 1) < 0
        page[offset : offset + 3] = b"onf"
        stream.seek((root_page - 1) * page_size)
        stream.write(page)
    with closing(sqlite3.connect(database)) as reader:
        assert reader.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    with pytest.raises(
        backup_snapshot.PalaceSnapshotError, match="integrity check did not return ok"
    ):
        sqlite_check(database, "integrity_check")


def test_integrity_sqlite_error_is_a_refusal(tmp_path):
    database = tmp_path / "broken.sqlite3"
    database.write_bytes(b"not a SQLite database" * 300)
    with pytest.raises(backup_snapshot.PalaceSnapshotError, match="integrity check failed"):
        sqlite_check(database, "integrity_check")


@pytest.mark.parametrize("damage_content", [False, True])
@pytest.mark.skipif(
    sqlite3.sqlite_version_info < (3, 45, 1),
    reason="read-only FTS5 integrity verification needs SQLite 3.45.1 or newer",
)
def test_integrity_verifies_fts5_trigram_content(tmp_path, damage_content):
    database = tmp_path / "fts.sqlite3"
    with closing(sqlite3.connect(database)) as writer, writer:
        writer.executescript(
            "CREATE VIRTUAL TABLE ft USING fts5(value, tokenize=trigram);"
            "INSERT INTO ft(value) VALUES ('abcdef');"
        )
        if damage_content:
            # Bypass the virtual table: the trigram index still describes the old content.
            writer.execute("UPDATE ft_content SET c0='xyz'")
    if damage_content:
        with pytest.raises(backup_snapshot.PalaceSnapshotError, match="malformed inverted index"):
            sqlite_check(database, "integrity_check")
    else:
        assert sqlite_check(database, "integrity_check") == "ok"
