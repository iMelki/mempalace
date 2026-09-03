import os
import sqlite3
import threading
import time

import mempalace.status as status_module
from mempalace.status import (
    _default_palace_path,
    _sqlite_status_counts,
    get_fast_drawer_count,
    status,
)


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


def test_get_fast_drawer_count_matches_metadata_segment(tmp_path):
    palace_path = tmp_path / "palace"
    _seed_sqlite_status_palace(palace_path)

    # 3 embeddings exist but one is vector-segment-only; only METADATA counts.
    assert get_fast_drawer_count(str(palace_path)) == 2


def test_default_palace_path_points_at_the_palace_not_the_config_dir(monkeypatch):
    """Regression: /healthz served `drawers: 0` for a 1,003,935-drawer palace.

    ``status.py`` defaulted to ``~/.mempalace`` -- the CONFIG directory --
    while ``config.DEFAULT_PALACE_PATH`` and every other module use
    ``~/.mempalace/palace``. Both directories contain a ``chroma.sqlite3``, so
    the wrong one opened cleanly and returned a confident 0 rather than
    failing. Served next to ``status: ok`` it read as "the palace is empty".
    """

    monkeypatch.delenv("MEMPALACE_PATH", raising=False)
    monkeypatch.delenv("MEMPALACE_PALACE_PATH", raising=False)

    resolved = _default_palace_path()

    assert os.path.basename(os.path.normpath(resolved)) == "palace"

    from mempalace.config import DEFAULT_PALACE_PATH

    assert os.path.normpath(resolved) == os.path.normpath(DEFAULT_PALACE_PATH)


def test_explicit_env_overrides_still_win(monkeypatch, tmp_path):
    monkeypatch.delenv("MEMPALACE_PATH", raising=False)
    monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(tmp_path / "custom"))
    assert _default_palace_path() == str(tmp_path / "custom")

    monkeypatch.setenv("MEMPALACE_PATH", str(tmp_path / "winner"))
    assert _default_palace_path() == str(tmp_path / "winner")


def test_fast_count_returns_none_when_no_palace_exists(tmp_path):
    # Must not report a confident 0 for a path that has no palace at all --
    # None means "cannot assert", which callers treat differently from empty.
    assert get_fast_drawer_count(str(tmp_path / "nonexistent")) is None


def test_cached_drawer_count_does_not_recount_within_ttl(monkeypatch):
    """Regression: ``/healthz`` recounted a 26.5 GB SQLite file on every probe.

    ``get_fast_drawer_count`` is documented as "Fast O(1)" but runs a
    two-JOIN ``COUNT(*)`` over the whole ``embeddings`` table. Measured on the
    live palace (1,031,514 drawers, 26.5 GB ``chroma.sqlite3``): 2.03s cold,
    0.25s warm. The Router health probe budget is 2.0s and the MemPalace
    bridge watchdog's is 5s, so both flapped against a service that was
    healthy the whole time.
    """

    calls = []

    def _counter(palace_path=None):
        calls.append(palace_path)
        return 1234

    monkeypatch.setattr(status_module, "get_fast_drawer_count", _counter)
    status_module.reset_drawer_count_cache()

    first = status_module.get_cached_drawer_count("/palace", ttl_seconds=60.0, now=1000.0)
    second = status_module.get_cached_drawer_count("/palace", ttl_seconds=60.0, now=1010.0)
    third = status_module.get_cached_drawer_count("/palace", ttl_seconds=60.0, now=1059.0)

    assert (first, second, third) == (1234, 1234, 1234)
    assert len(calls) == 1, f"expected one recount within the TTL, got {len(calls)}"


def test_cached_drawer_count_serves_stale_value_instead_of_blocking(monkeypatch):
    """A stale count must be served immediately; the refresh happens off-probe.

    Blocking the probe to refresh is what produced ``alive-timeout``. The
    cache refreshes in the background and the caller keeps the last known
    good number.
    """

    release = threading.Event()
    counts = iter([111, 222])

    def _slow_counter(palace_path=None):
        value = next(counts)
        if value == 222:
            assert release.wait(timeout=10), "refresh thread never ran"
        return value

    monkeypatch.setattr(status_module, "get_fast_drawer_count", _slow_counter)
    status_module.reset_drawer_count_cache()

    assert status_module.get_cached_drawer_count("/palace", ttl_seconds=30.0, now=1000.0) == 111

    started = time.monotonic()
    stale = status_module.get_cached_drawer_count("/palace", ttl_seconds=30.0, now=1100.0)
    elapsed = time.monotonic() - started

    assert stale == 111, "a stale-but-known count must be served, not None"
    assert elapsed < 1.0, f"stale read blocked for {elapsed:.2f}s on the refresh"

    release.set()
    status_module.wait_for_drawer_count_refresh(timeout=10)
    assert status_module.get_cached_drawer_count("/palace", ttl_seconds=30.0, now=1101.0) == 222
