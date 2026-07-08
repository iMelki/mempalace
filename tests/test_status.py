import sqlite3

from mempalace.status import _sqlite_status_counts, status


def _seed_sqlite_status_palace(palace_path):
    palace_path.mkdir(parents=True, exist_ok=True)
    db_path = palace_path / "chroma.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE collections (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL
            );
            CREATE TABLE segments (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                scope TEXT NOT NULL,
                collection TEXT NOT NULL
            );
            CREATE TABLE embeddings (
                id INTEGER PRIMARY KEY,
                segment_id TEXT NOT NULL,
                embedding_id TEXT NOT NULL,
                seq_id BLOB NOT NULL,
                created_at TIMESTAMP
            );
            CREATE TABLE embedding_metadata (
                id INTEGER,
                key TEXT NOT NULL,
                string_value TEXT,
                int_value INTEGER,
                float_value REAL,
                bool_value INTEGER,
                PRIMARY KEY (id, key)
            );
            INSERT INTO collections (id, name) VALUES ('c1', 'mempalace_drawers');
            INSERT INTO segments (id, type, scope, collection)
                VALUES ('m1', 'urn:chroma:segment/metadata/sqlite', 'METADATA', 'c1');
            INSERT INTO segments (id, type, scope, collection)
                VALUES ('v1', 'urn:chroma:segment/vector/hnsw-local-persisted', 'VECTOR', 'c1');
            INSERT INTO embeddings (id, segment_id, embedding_id, seq_id)
                VALUES (1, 'm1', 'drawer-1', x'0001');
            INSERT INTO embeddings (id, segment_id, embedding_id, seq_id)
                VALUES (2, 'm1', 'drawer-2', x'0002');
            INSERT INTO embeddings (id, segment_id, embedding_id, seq_id)
                VALUES (3, 'v1', 'stale-vector-only', x'0003');
            INSERT INTO embedding_metadata (id, key, string_value)
                VALUES (1, 'wing', 'agents');
            INSERT INTO embedding_metadata (id, key, string_value)
                VALUES (1, 'room', 'general');
            INSERT INTO embedding_metadata (id, key, string_value)
                VALUES (2, 'wing', 'coding');
            INSERT INTO embedding_metadata (id, key, string_value)
                VALUES (2, 'room', 'tools');
            """
        )
    return db_path


def test_sqlite_status_counts_metadata_segment_only(tmp_path):
    palace_path = tmp_path / "palace"
    _seed_sqlite_status_palace(palace_path)

    total, wing_rooms = _sqlite_status_counts(str(palace_path))

    assert total == 2
    assert wing_rooms["agents"]["general"] == 1
    assert wing_rooms["coding"]["tools"] == 1
    assert "stale-vector-only" not in wing_rooms


def test_status_uses_sqlite_without_opening_collection(tmp_path, monkeypatch, capsys):
    palace_path = tmp_path / "palace"
    _seed_sqlite_status_palace(palace_path)

    def fail_open(_palace_path):
        raise AssertionError("should not open Chroma collection when SQLite status works")

    monkeypatch.setattr("mempalace.status._collection_status_counts", fail_open)

    status(str(palace_path))

    out = capsys.readouterr().out
    assert "MemPalace Status — 2 drawers" in out
    assert "WING: agents" in out
    assert "ROOM: tools" in out
