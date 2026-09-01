"""
Dependency-light MemPalace status reporting.

The CLI status command must stay usable when the vector backend is broken or
when the local Python runtime can read ``chroma.sqlite3`` but does not have
``chromadb`` installed. Keep the SQLite path in this module so importing the
CLI command does not eagerly import the mining/vector stack.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from collections import defaultdict
from typing import Any, DefaultDict


COLLECTION_NAME = "mempalace_drawers"


def _sqlite_fast_drawer_count(palace_path: str) -> int | None:
    """Return fast total drawer count from SQLite without wing/room metadata JOINs."""
    sqlite_path = os.path.join(palace_path, "chroma.sqlite3")
    if not os.path.exists(sqlite_path):
        return None

    try:
        conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
        try:
            row = conn.execute(
                """
                SELECT COUNT(*)
                FROM embeddings e
                JOIN segments s ON e.segment_id = s.id
                JOIN collections c ON s.collection = c.id
                WHERE c.name = ? AND s.scope = 'METADATA'
                """,
                (COLLECTION_NAME,),
            ).fetchone()
            return int(row[0]) if row and row[0] is not None else 0
        finally:
            conn.close()
    except Exception:
        return None


def _default_palace_path() -> str:
    """Resolve the palace path the same way every other module does.

    This module previously fell back to ``~/.mempalace`` -- the CONFIG
    directory -- while ``config.DEFAULT_PALACE_PATH`` and every other caller
    use ``~/.mempalace/palace``. Both directories contain a ``chroma.sqlite3``,
    so the wrong one opened cleanly and returned a confident **0** for a palace
    holding 1,003,935 drawers. That zero was served on ``/healthz`` next to
    ``status: ok``, which reads as "the palace is empty" rather than "I read
    the wrong file".

    Imported lazily so this module stays dependency-light: the CLI status
    command must keep working when the vector backend is broken.
    """

    explicit = os.environ.get("MEMPALACE_PATH") or os.environ.get("MEMPALACE_PALACE_PATH")
    if explicit:
        return explicit

    try:
        from .config import DEFAULT_PALACE_PATH

        return DEFAULT_PALACE_PATH
    except Exception:
        return os.path.join(os.path.expanduser("~"), ".mempalace", "palace")


# ---------------------------------------------------------------------------
# Cached drawer count for health probes
#
# ``get_fast_drawer_count`` is only "fast" relative to a wing/room GROUP BY --
# it still runs a two-JOIN ``COUNT(*)`` across the whole ``embeddings`` table.
# Measured on the live palace (1,031,514 drawers, 26.5 GB ``chroma.sqlite3``):
# 2.03s cold, 0.25s warm. ``/healthz`` called it on *every* probe, so the
# Router's 2.0s probe budget and the MemPalace bridge watchdog's 5s budget both
# timed out against a service that was answering queries the whole time. The
# Router reported ``mempalace: fail / "timed out"`` and the watchdog reported
# ``alive-timeout``, while ``queryProof`` stayed ``proven``.
#
# Serving a slightly stale count is correct here: the probe needs "is this
# palace populated", not a transactional number. Never let a failed refresh
# replace a known-good count with ``None`` -- a confident wrong "empty palace"
# is the exact failure this module already carries a regression test for.
# ---------------------------------------------------------------------------

DRAWER_COUNT_TTL_SECONDS = 30.0

_drawer_count_lock = threading.Lock()
_drawer_count_state: dict[str, Any] = {"value": None, "at": None, "path": None}
_drawer_count_refresh_thread: threading.Thread | None = None


def reset_drawer_count_cache() -> None:
    """Drop the cached count. For tests and for an explicit operator refresh."""

    global _drawer_count_refresh_thread
    with _drawer_count_lock:
        _drawer_count_state.update({"value": None, "at": None, "path": None})
        _drawer_count_refresh_thread = None


def wait_for_drawer_count_refresh(timeout: float | None = None) -> bool:
    """Join an in-flight background refresh. Returns False if it is still running."""

    with _drawer_count_lock:
        thread = _drawer_count_refresh_thread
    if thread is None:
        return True
    thread.join(timeout)
    return not thread.is_alive()


def _refresh_drawer_count(palace_path: str | None, stamp: float) -> None:
    value = get_fast_drawer_count(palace_path)
    with _drawer_count_lock:
        # A failed read must not erase a known-good count.
        if value is not None or _drawer_count_state.get("value") is None:
            _drawer_count_state["value"] = value
        _drawer_count_state["path"] = palace_path
        # Stamp the moment the refresh was *requested*, not finished, so a slow
        # read cannot silently extend the TTL past what the caller asked for.
        _drawer_count_state["at"] = stamp


def get_cached_drawer_count(
    palace_path: str | None = None,
    *,
    ttl_seconds: float | None = None,
    now: float | None = None,
) -> int | None:
    """Return the drawer count for a health probe on every probe.

    Fresh cache -> returned directly. Stale cache -> the last known value is
    returned *immediately* and a single background thread refreshes it. Cold
    cache -> counted synchronously once, because a probe that reports no count
    at all reads as an empty palace to the bridge watchdog.
    """

    global _drawer_count_refresh_thread

    if ttl_seconds is None:
        ttl_seconds = DRAWER_COUNT_TTL_SECONDS
    if now is None:
        now = time.monotonic()

    with _drawer_count_lock:
        cached_at = _drawer_count_state.get("at")
        cached_value = _drawer_count_state.get("value")
        cached_path = _drawer_count_state.get("path")
        is_cold = cached_at is None or cached_path != palace_path
        if not is_cold and (now - cached_at) < ttl_seconds:
            return cached_value

        if is_cold:
            # Hold the lock: one cold caller counts, the rest wait for it once.
            value = get_fast_drawer_count(palace_path)
            _drawer_count_state.update({"value": value, "at": now, "path": palace_path})
            return value

        thread = _drawer_count_refresh_thread
        if thread is None or not thread.is_alive():
            thread = threading.Thread(
                target=_refresh_drawer_count,
                args=(palace_path, now),
                name="mempalace-drawer-count-refresh",
                daemon=True,
            )
            _drawer_count_refresh_thread = thread
            thread.start()
        return cached_value


def get_fast_drawer_count(palace_path: str | None = None) -> int | None:
    """Fast O(1) total drawer count for health checks and status probes."""
    if not palace_path:
        palace_path = _default_palace_path()
    count = _sqlite_fast_drawer_count(palace_path)
    if count is not None:
        return count

    try:
        from .palace import get_collection

        col = get_collection(palace_path, create=False)
        return col.count()
    except Exception:
        return None


def _sqlite_status_counts(palace_path: str):
    """Return ``(total, wing_rooms)`` from Chroma SQLite metadata when available."""

    sqlite_path = os.path.join(palace_path, "chroma.sqlite3")
    if not os.path.exists(sqlite_path):
        return None

    try:
        conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                """
                SELECT
                    COALESCE(w.string_value, '?') AS wing,
                    COALESCE(r.string_value, '?') AS room,
                    COUNT(*) AS drawer_count
                FROM embeddings e
                JOIN segments s ON e.segment_id = s.id
                JOIN collections c ON s.collection = c.id
                LEFT JOIN embedding_metadata w ON w.id = e.id AND w.key = 'wing'
                LEFT JOIN embedding_metadata r ON r.id = e.id AND r.key = 'room'
                WHERE c.name = ?
                  AND s.scope = 'METADATA'
                GROUP BY COALESCE(w.string_value, '?'), COALESCE(r.string_value, '?')
                """,
                (COLLECTION_NAME,),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return None

    wing_rooms: DefaultDict[str, DefaultDict[str, int]] = defaultdict(lambda: defaultdict(int))
    total = 0
    for wing, room, count in rows:
        count = int(count or 0)
        wing_rooms[wing or "?"][room or "?"] += count
        total += count
    return total, wing_rooms


def _collection_status_counts(palace_path: str):
    from .palace import get_collection

    try:
        col = get_collection(palace_path, create=False)
    except Exception:
        return None

    total = col.count()
    wing_rooms: DefaultDict[str, DefaultDict[str, int]] = defaultdict(lambda: defaultdict(int))
    batch_size = 5000
    offset = 0
    while offset < total:
        result = col.get(limit=batch_size, offset=offset, include=["metadatas"])
        batch = result["metadatas"]
        if not batch:
            break
        for metadata in batch:
            metadata = metadata or {}
            wing_rooms[metadata.get("wing", "?")][metadata.get("room", "?")] += 1
        offset += len(batch)
    return total, wing_rooms


def status(palace_path: str):
    """Show what's been filed in the palace without opening HNSW when SQLite works."""

    counts = _sqlite_status_counts(palace_path)
    if counts is None:
        counts = _collection_status_counts(palace_path)

    if counts is None:
        print(f"\n  No palace found at {palace_path}")
        print("  Run: mempalace init <dir> then mempalace mine <dir>")
        return

    total, wing_rooms = counts
    print(f"\n{'=' * 55}")
    print(f"  MemPalace Status — {total} drawers")
    print(f"{'=' * 55}\n")
    for wing, rooms in sorted(wing_rooms.items()):
        print(f"  WING: {wing}")
        for room, count in sorted(rooms.items(), key=lambda item: item[1], reverse=True):
            print(f"    ROOM: {room:20} {count:5} drawers")
        print()
    print(f"{'=' * 55}\n")
