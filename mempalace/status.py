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
from collections import defaultdict
from typing import DefaultDict


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


def get_fast_drawer_count(palace_path: str | None = None) -> int | None:
    """Fast O(1) total drawer count for health checks and status probes."""
    if not palace_path:
        palace_path = (
            os.environ.get("MEMPALACE_PATH")
            or os.environ.get("MEMPALACE_PALACE_PATH")
            or os.path.expanduser("~/.mempalace")
        )
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
