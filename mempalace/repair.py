"""
repair.py — Scan, prune corrupt entries, and rebuild HNSW index
================================================================

When ChromaDB's HNSW index accumulates duplicate entries (from repeated
add() calls with the same ID), link_lists.bin can grow unbounded —
terabytes on large palaces — eventually causing segfaults.

This module provides four operations:

  status  — compare sqlite vs HNSW element counts (read-only health check)
  scan    — find every corrupt/unfetchable ID in the palace
  prune   — delete only the corrupt IDs (surgical)
  rebuild — extract all drawers, delete the collection, recreate with
            correct HNSW settings, and upsert everything back

The rebuild backs up ONLY chroma.sqlite3 (the source of truth), not the
full palace directory — so it works even when link_lists.bin is bloated.

Usage (standalone):
    python -m mempalace.repair status
    python -m mempalace.repair scan [--wing X]
    python -m mempalace.repair prune --confirm
    python -m mempalace.repair rebuild

Usage (from CLI):
    mempalace repair
    mempalace repair-scan [--wing X]
    mempalace repair-prune --confirm
"""

import argparse
import json
import os
import pickle
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

COLLECTION_NAME = "mempalace_drawers"
ChromaBackend = None
hnsw_capacity_status = None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_json_file(path: str, payload: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")


def _append_jsonl(path: str, payload: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, sort_keys=True))
        f.write("\n")


def _sqlite_replay_artifact_paths(
    palace_path: str, *, artifact_dir: Optional[str], run_id: str
) -> dict[str, str]:
    if artifact_dir:
        resolved_dir = os.path.abspath(os.path.expanduser(artifact_dir))
    else:
        resolved_dir = os.path.join(
            palace_path,
            ".mempalace",
            "repair-runs",
            f"sqlite-replay-{run_id}",
        )
    return {
        "artifact_dir": resolved_dir,
        "result_json": os.path.join(resolved_dir, "result.json"),
        "events_log": os.path.join(resolved_dir, "events.jsonl"),
    }


def _planned_batches(count: int, batch_size: int) -> int:
    if count <= 0:
        return 0
    return (count + batch_size - 1) // batch_size


def _get_chroma_backend_cls():
    """Import Chroma lazily so SQLite-only repair modes stay dependency-light."""
    global ChromaBackend
    if ChromaBackend is None:
        from .backends.chroma import ChromaBackend as _ChromaBackend

        ChromaBackend = _ChromaBackend
    return ChromaBackend


def _get_hnsw_capacity_status():
    """Return the HNSW probe without importing Chroma.

    ``repair-status`` must work in lean Python environments that can read
    ``chroma.sqlite3`` but do not have ``chromadb`` installed. Importing
    ``mempalace.backends.chroma`` walks through the package initializer and
    requires Chroma, so keep the dependency-free probe local to this module.
    """
    global hnsw_capacity_status
    if hnsw_capacity_status is None:
        hnsw_capacity_status = _hnsw_capacity_status_local
    return hnsw_capacity_status


class _PersistentDataStub:
    """Minimal stand-in for Chroma's PersistentData during safe unpickling."""

    def __init__(self, *args, **kwargs):
        pass

    def __setstate__(self, state):
        if isinstance(state, dict):
            self.__dict__.update(state)
        elif isinstance(state, tuple) and len(state) == 2 and isinstance(state[1], dict):
            self.__dict__.update(state[1])


class _SafePersistentDataUnpickler:
    _ALLOWED = frozenset(
        {
            (
                "chromadb.segment.impl.vector.local_persistent_hnsw",
                "PersistentData",
            ),
        }
    )

    @classmethod
    def load(cls, path: str):
        class _Restricted(pickle.Unpickler):
            def find_class(self, module: str, name: str):
                if (module, name) in cls._ALLOWED:
                    return _PersistentDataStub
                raise pickle.UnpicklingError(f"disallowed class: {module}.{name}")

        with open(path, "rb") as f:
            return _Restricted(f).load()


def _hnsw_element_count(palace_path: str, segment_id: str) -> Optional[int]:
    pickle_path = os.path.join(palace_path, segment_id, "index_metadata.pickle")
    if not os.path.isfile(pickle_path):
        return None
    try:
        persistent_data = _SafePersistentDataUnpickler.load(pickle_path)
        if isinstance(persistent_data, dict):
            id_to_label = persistent_data.get("id_to_label")
        else:
            id_to_label = getattr(persistent_data, "id_to_label", None)
        if isinstance(id_to_label, dict):
            return len(id_to_label)
        return None
    except Exception:
        return None


_HNSW_DIVERGENCE_FALLBACK_FLOOR = 2000
_HNSW_DIVERGENCE_FRACTION = 0.10


def _read_sync_threshold(palace_path: str, collection_name: str) -> int:
    db_path = os.path.join(palace_path, "chroma.sqlite3")
    if not os.path.isfile(db_path):
        return 1000
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT cm.int_value
                FROM collection_metadata cm
                JOIN collections c ON cm.collection_id = c.id
                WHERE c.name = ? AND cm.key = 'hnsw:sync_threshold'
                """,
                (collection_name,),
            )
            row = cur.fetchone()
            if row and row[0] is not None:
                return int(row[0])
            return 1000
        finally:
            conn.close()
    except Exception:
        return 1000


def _hnsw_capacity_status_local(palace_path: str, collection_name: str = COLLECTION_NAME) -> dict:
    out: dict[str, Any] = {
        "segment_id": None,
        "sqlite_count": None,
        "hnsw_count": None,
        "divergence": None,
        "diverged": False,
        "status": "unknown",
        "message": "",
    }

    try:
        seg_id = _vector_segment_id(palace_path, collection_name)
        out["segment_id"] = seg_id

        sqlite_count = _sqlite_embedding_count(palace_path, collection_name)
        out["sqlite_count"] = sqlite_count

        if seg_id is None or sqlite_count is None:
            out["message"] = "palace state unreadable; skipping HNSW capacity check"
            return out

        hnsw_count = _hnsw_element_count(palace_path, seg_id)
        out["hnsw_count"] = hnsw_count

        sync_threshold = _read_sync_threshold(palace_path, collection_name)
        divergence_floor = max(_HNSW_DIVERGENCE_FALLBACK_FLOOR, 2 * sync_threshold)

        if hnsw_count is None:
            if sqlite_count > divergence_floor:
                out["status"] = "diverged"
                out["diverged"] = True
                out["divergence"] = sqlite_count
                out["message"] = (
                    f"sqlite holds {sqlite_count:,} embeddings but the HNSW segment "
                    "has never flushed metadata — vector search will return nothing "
                    "until the segment is rebuilt. Run `mempalace repair`."
                )
            else:
                out["message"] = "HNSW segment metadata not yet flushed; skipping"
            return out

        divergence = sqlite_count - hnsw_count
        out["divergence"] = divergence
        threshold = max(divergence_floor, int(sqlite_count * _HNSW_DIVERGENCE_FRACTION))
        if divergence > threshold:
            out["status"] = "diverged"
            out["diverged"] = True
            pct = 100.0 * divergence / max(sqlite_count, 1)
            out["message"] = (
                f"HNSW index holds {hnsw_count:,} elements but sqlite has "
                f"{sqlite_count:,} embeddings — {divergence:,} drawers ({pct:.0f}%) "
                "are invisible to vector search. Run `mempalace repair` to rebuild."
            )
        else:
            out["status"] = "ok"
            out["message"] = (
                f"HNSW {hnsw_count:,} / sqlite {sqlite_count:,} (within flush-lag tolerance)"
            )
    except Exception:
        out["message"] = "HNSW capacity probe raised; skipping"
    return out


def _get_palace_path():
    """Resolve palace path from config."""
    try:
        from .config import MempalaceConfig

        return MempalaceConfig().palace_path
    except Exception:
        default = os.path.join(os.path.expanduser("~"), ".mempalace", "palace")
        return default


def _paginate_ids(col, where=None):
    """Pull all IDs in a collection using pagination."""
    ids = []
    page = 1000
    offset = 0
    while True:
        try:
            r = col.get(where=where, include=[], limit=page, offset=offset)
        except Exception:
            try:
                r = col.get(where=where, include=[], limit=page)
                new_ids = [i for i in r["ids"] if i not in set(ids)]
                if not new_ids:
                    break
                ids.extend(new_ids)
                offset += len(new_ids)
                continue
            except Exception:
                break
        n = len(r["ids"]) if r["ids"] else 0
        if n == 0:
            break
        ids.extend(r["ids"])
        offset += n
        if n < page:
            break
    return ids


def scan_palace(palace_path=None, only_wing=None):
    """Scan the palace for corrupt/unfetchable IDs.

    Probes in batches of 100, falls back to per-ID on failure.
    Writes corrupt_ids.txt to the palace directory for the prune step.

    Returns (good_set, bad_set).
    """
    palace_path = palace_path or _get_palace_path()
    print(f"\n  Palace: {palace_path}")
    print("  Loading...")

    col = _get_chroma_backend_cls()().get_collection(palace_path, COLLECTION_NAME)

    where = {"wing": only_wing} if only_wing else None
    total = col.count()
    print(f"  Collection: {COLLECTION_NAME}, total: {total:,}")
    if only_wing:
        print(f"  Scanning wing: {only_wing}")

    print("\n  Step 1: listing all IDs...")
    t0 = time.time()
    all_ids = _paginate_ids(col, where=where)
    print(f"  Found {len(all_ids):,} IDs in {time.time() - t0:.1f}s\n")

    if not all_ids:
        print("  Nothing to scan.")
        return set(), set()

    print("  Step 2: probing each ID (batches of 100)...")
    t0 = time.time()
    good_set = set()
    bad_set = set()
    batch = 100

    for i in range(0, len(all_ids), batch):
        chunk = all_ids[i : i + batch]
        try:
            r = col.get(ids=chunk, include=["documents"])
            for got in r["ids"]:
                good_set.add(got)
            for mid in chunk:
                if mid not in good_set:
                    bad_set.add(mid)
        except Exception:
            for sid in chunk:
                try:
                    r = col.get(ids=[sid], include=["documents"])
                    if r["ids"]:
                        good_set.add(sid)
                    else:
                        bad_set.add(sid)
                except Exception:
                    bad_set.add(sid)

        if (i // batch) % 50 == 0:
            elapsed = time.time() - t0
            rate = (i + batch) / max(elapsed, 0.01)
            eta = (len(all_ids) - i - batch) / max(rate, 0.01)
            print(
                f"    {i + batch:>6}/{len(all_ids):>6}  "
                f"good={len(good_set):>6}  bad={len(bad_set):>6}  "
                f"eta={eta:.0f}s"
            )

    print(f"\n  Scan complete in {time.time() - t0:.1f}s")
    print(f"  GOOD: {len(good_set):,}")
    print(f"  BAD:  {len(bad_set):,}  ({len(bad_set) / max(len(all_ids), 1) * 100:.1f}%)")

    bad_file = os.path.join(palace_path, "corrupt_ids.txt")
    with open(bad_file, "w") as f:
        for bid in sorted(bad_set):
            f.write(bid + "\n")
    print(f"\n  Bad IDs written to: {bad_file}")
    return good_set, bad_set


def prune_corrupt(palace_path=None, confirm=False):
    """Delete corrupt IDs listed in corrupt_ids.txt."""
    palace_path = palace_path or _get_palace_path()
    bad_file = os.path.join(palace_path, "corrupt_ids.txt")

    if not os.path.exists(bad_file):
        print("  No corrupt_ids.txt found — run scan first.")
        return

    with open(bad_file) as f:
        bad_ids = [line.strip() for line in f if line.strip()]
    print(f"  {len(bad_ids):,} corrupt IDs queued for deletion")

    if not confirm:
        print("\n  DRY RUN — no deletions performed.")
        print("  Re-run with --confirm to actually delete.")
        return

    col = _get_chroma_backend_cls()().get_collection(palace_path, COLLECTION_NAME)
    before = col.count()
    print(f"  Collection size before: {before:,}")

    batch = 100
    deleted = 0
    failed = 0
    for i in range(0, len(bad_ids), batch):
        chunk = bad_ids[i : i + batch]
        try:
            col.delete(ids=chunk)
            deleted += len(chunk)
        except Exception:
            for sid in chunk:
                try:
                    col.delete(ids=[sid])
                    deleted += 1
                except Exception:
                    failed += 1
        if (i // batch) % 20 == 0:
            print(f"    deleted {deleted}/{len(bad_ids)}  (failed: {failed})")

    after = col.count()
    print(f"\n  Deleted: {deleted:,}")
    print(f"  Failed:  {failed:,}")
    print(f"  Collection size: {before:,} → {after:,}")


# ChromaDB's ``collection.get()`` enforces an internal default ``limit``
# of 10 000 rows when the caller does not pass one. We pass an explicit
# ``limit=batch_size`` below, but the underlying segment also caps reads
# during stale/quarantined-HNSW recovery flows: extraction silently stops
# at exactly 10 000 even on palaces with many more rows. Refusing to
# overwrite when this exact value comes back is the simplest signal we
# can detect without depending on chromadb internals.
CHROMADB_DEFAULT_GET_LIMIT = 10_000
SQLITE_REPLAY_DRY_RUN_EXACT_DOC_LIMIT = 100_000
SQLITE_REPLAY_LARGE_REEMBED_LIMIT = 100_000


class TruncationDetected(Exception):
    """Raised by :func:`check_extraction_safety` when extraction looks short.

    Carries the human-readable abort message so callers (CLI ``cmd_repair``,
    ``rebuild_index``) can print and exit consistently without re-deriving
    the wording.
    """

    def __init__(self, message: str, sqlite_count: "int | None", extracted: int):
        super().__init__(message)
        self.message = message
        self.sqlite_count = sqlite_count
        self.extracted = extracted


def check_extraction_safety(
    palace_path: str, extracted: int, confirm_truncation_ok: bool = False
) -> None:
    """Cross-check that ``extracted`` matches the SQLite ground truth.

    Two signals trip the guard:

    1. **Strong** — ``chroma.sqlite3`` reports more drawers than were
       extracted. This is the user-reported #1208 case: 67 580 on disk,
       10 000 came back through the chromadb collection layer, repair
       would have destroyed the difference.
    2. **Weak** — extracted count equals exactly ``CHROMADB_DEFAULT_GET_LIMIT``
       AND the SQLite check couldn't run (schema drift, locked file).
       Hitting the chromadb default ``get()`` cap exactly is suspicious
       enough to refuse without explicit acknowledgement.

    Raises :class:`TruncationDetected` with a printable message when the
    guard fires. Does nothing on safe extractions or when
    ``confirm_truncation_ok`` is set.
    """
    if confirm_truncation_ok:
        return

    sqlite_count = sqlite_drawer_count(palace_path)
    cap_signal = extracted == CHROMADB_DEFAULT_GET_LIMIT

    if sqlite_count is not None and sqlite_count > extracted:
        loss = sqlite_count - extracted
        pct = 100 * loss / sqlite_count
        message = (
            f"\n  ABORT: chroma.sqlite3 reports {sqlite_count:,} drawers but only {extracted:,}\n"
            "  came back through the chromadb collection layer. The segment metadata is\n"
            "  stale (often after manual HNSW quarantine) — proceeding would silently\n"
            f"  destroy {loss:,} drawers (~{pct:.0f}%).\n"
            "\n"
            "  Recovery options:\n"
            "    1. Restore from your most recent palace backup, then re-mine.\n"
            "    2. Direct-extract from chroma.sqlite3 (rows are still on disk) and\n"
            "       rebuild the palace from source files.\n"
            "    3. If you have independently confirmed the palace really contains only\n"
            f"       {extracted:,} drawers, re-run with --confirm-truncation-ok.\n"
        )
        raise TruncationDetected(message, sqlite_count, extracted)

    if cap_signal and sqlite_count is None:
        message = (
            f"\n  ABORT: extracted exactly {CHROMADB_DEFAULT_GET_LIMIT:,} drawers, which matches\n"
            "  ChromaDB's internal default get() limit. The on-disk SQLite count couldn't\n"
            "  be cross-checked from this Python context, so we can't tell whether the\n"
            f"  palace genuinely holds {CHROMADB_DEFAULT_GET_LIMIT:,} rows or whether extraction was\n"
            "  silently capped. Refusing to overwrite the palace.\n"
            "\n"
            "  If you have independently confirmed (e.g. via direct sqlite3 query) that\n"
            f"  the palace really contains exactly {CHROMADB_DEFAULT_GET_LIMIT:,} drawers, re-run with\n"
            "  --confirm-truncation-ok.\n"
        )
        raise TruncationDetected(message, sqlite_count, extracted)


def sqlite_drawer_count(palace_path: str) -> "int | None":
    """Count rows in ``chroma.sqlite3.embeddings`` for the drawers collection.

    Used as an independent ground-truth check against the chromadb
    collection-layer ``count()`` / ``get()``: when the on-disk SQLite
    row count exceeds the extraction count, the segment metadata is
    stale and repair would destroy the difference.

    Returns ``None`` when the schema isn't readable (chromadb version
    drift, missing tables, locked file). Callers treat ``None`` as
    "unknown" and fall back to the cap-detection check.
    """
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
                WHERE c.name = ?
                """,
                (COLLECTION_NAME,),
            ).fetchone()
            return int(row[0]) if row and row[0] is not None else None
        finally:
            conn.close()
    except Exception:
        # chromadb schema differs by version (segments / collections column
        # names occasionally rename). Silent fallback is correct here —
        # the cap-detection check still catches the user-reported case.
        return None


def _sqlite_embedding_count(palace_path: str, collection_name: str) -> "int | None":
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
                WHERE c.name = ?
                """,
                (collection_name,),
            ).fetchone()
            return int(row[0]) if row and row[0] is not None else None
        finally:
            conn.close()
    except Exception:
        return None


def _vector_segment_id(palace_path: str, collection_name: str) -> "str | None":
    sqlite_path = os.path.join(palace_path, "chroma.sqlite3")
    if not os.path.exists(sqlite_path):
        return None
    try:
        conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
        try:
            row = conn.execute(
                """
                SELECT s.id
                FROM segments s
                JOIN collections c ON s.collection = c.id
                WHERE c.name = ? AND s.scope = 'VECTOR'
                LIMIT 1
                """,
                (collection_name,),
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def _sqlite_collection_count(db_path: str, collection_name: str) -> int:
    """Count metadata-segment rows for a Chroma collection.

    ChromaDB 1.x stores documents and user metadata in the METADATA
    segment. The VECTOR segment may be stale, quarantined, or missing in
    the recovery scenarios this module handles, so the direct-SQL replay
    path deliberately ignores it.
    """
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*)
            FROM embeddings e
            JOIN segments s ON e.segment_id = s.id
            JOIN collections c ON s.collection = c.id
            WHERE c.name = ?
              AND s.scope = 'METADATA'
            """,
            (collection_name,),
        ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _sqlite_document_count(db_path: str, collection_name: str) -> int:
    """Count rows with a non-empty ``chroma:document`` payload."""
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        row = conn.execute(
            """
            SELECT COUNT(DISTINCT e.id)
            FROM embeddings e
            JOIN segments s ON e.segment_id = s.id
            JOIN collections c ON s.collection = c.id
            JOIN embedding_metadata em ON em.id = e.id
            WHERE c.name = ?
              AND s.scope = 'METADATA'
              AND em.key = 'chroma:document'
              AND em.string_value IS NOT NULL
              AND em.string_value != ''
            """,
            (collection_name,),
        ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _metadata_value(row: sqlite3.Row) -> Any:
    """Return the typed metadata value from a Chroma ``embedding_metadata`` row."""
    if row["string_value"] is not None:
        return row["string_value"]
    if row["int_value"] is not None:
        return int(row["int_value"])
    if row["float_value"] is not None:
        return float(row["float_value"])
    if row["bool_value"] is not None:
        return bool(row["bool_value"])
    return None


def iter_drawers_from_sqlite(
    db_path: str,
    *,
    collection_name: str = COLLECTION_NAME,
    batch_size: int = 1000,
) -> Iterator[list[dict[str, Any]]]:
    """Yield drawer batches reconstructed directly from Chroma SQLite.

    This bypasses the Chroma collection layer, which is exactly what can
    be truncated or unsafe after HNSW quarantine. Each yielded record has
    ``id``, ``document``, and ``metadata`` keys suitable for Chroma
    ``upsert``.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be positive")

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        last_row_id = 0
        while True:
            rows = conn.execute(
                """
                SELECT e.id, e.embedding_id
                FROM embeddings e
                JOIN segments s ON e.segment_id = s.id
                JOIN collections c ON s.collection = c.id
                WHERE c.name = ?
                  AND s.scope = 'METADATA'
                  AND e.id > ?
                ORDER BY e.id ASC
                LIMIT ?
                """,
                (collection_name, last_row_id, batch_size),
            ).fetchall()
            if not rows:
                break

            row_ids = [int(row["id"]) for row in rows]
            placeholders = ",".join("?" for _ in row_ids)
            meta_rows = conn.execute(
                f"""
                SELECT id, key, string_value, int_value, float_value, bool_value
                FROM embedding_metadata
                WHERE id IN ({placeholders})
                ORDER BY id ASC, key ASC
                """,
                row_ids,
            ).fetchall()

            by_row_id: dict[int, dict[str, Any]] = {
                int(row["id"]): {"id": row["embedding_id"], "document": None, "metadata": {}}
                for row in rows
            }
            for meta in meta_rows:
                row_id = int(meta["id"])
                record = by_row_id.get(row_id)
                if record is None:
                    continue
                key = str(meta["key"])
                value = _metadata_value(meta)
                if key == "chroma:document":
                    record["document"] = value if isinstance(value, str) else ""
                elif not key.startswith("chroma:") and value is not None:
                    record["metadata"][key] = value

            batch = [
                record
                for row_id, record in by_row_id.items()
                if isinstance(record.get("document"), str) and record["document"]
            ]
            if batch:
                yield batch
            last_row_id = row_ids[-1]
    finally:
        conn.close()


def repair_sqlite_replay(  # noqa: C901
    palace_path: str,
    *,
    dry_run: bool = False,
    assume_yes: bool = False,
    backup: bool = True,
    batch_size: int = 1000,
    confirm_large_reembed: bool = False,
    max_rows: Optional[int] = None,
    max_batches: Optional[int] = None,
    artifact_dir: Optional[str] = None,
    collection_name: str = COLLECTION_NAME,
) -> dict[str, Any]:
    """Rebuild the drawers vector index from SQLite metadata rows.

    Use this when ``repair-status`` reports a large SQLite-vs-HNSW
    divergence and the legacy Chroma extraction path would only see the
    truncated vector segment. The source SQLite database is copied first;
    replay streams from that snapshot so deleting the live collection
    cannot destroy the recovery source.
    """
    from .migrate import confirm_destructive_action, contains_palace_database

    palace_path = os.path.abspath(os.path.expanduser(palace_path))
    db_path = os.path.join(palace_path, "chroma.sqlite3")
    started_monotonic = time.time()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    result: dict[str, Any] = {
        "operation": "sqlite-replay",
        "run_id": run_id,
        "palace_path": palace_path,
        "source_collection": collection_name,
        "target_collection": collection_name,
        "collection": collection_name,
        "dry_run": dry_run,
        "aborted": False,
        "status": "running",
        "started_at": _utc_now_iso(),
        "finished_at": None,
        "duration_seconds": None,
        "source_count": 0,
        "document_count": 0,
        "document_count_exact": True,
        "planned_reembed_count": 0,
        "planned_batches": 0,
        "replayed": 0,
        "batches_replayed": 0,
        "backup": None,
        "backup_requested": backup,
        "backup_note": None,
        "source_snapshot": None,
        "artifact_dir": None,
        "result_json": None,
        "events_log": None,
        "max_rows": max_rows,
        "max_batches": max_batches,
        "batch_size": batch_size,
        "bounded": max_rows is not None or max_batches is not None,
        "partial_live_collection": False,
        "live_collection_state": "unchanged",
        "rollback_attempted": False,
        "rollback_succeeded": False,
        "verified_count": None,
        "resume_supported": False,
        "warnings": [],
        "events": [],
    }

    def ensure_artifacts() -> None:
        if result["artifact_dir"]:
            return
        paths = _sqlite_replay_artifact_paths(palace_path, artifact_dir=artifact_dir, run_id=run_id)
        result.update(paths)
        os.makedirs(paths["artifact_dir"], exist_ok=True)

    def write_result() -> None:
        result_json = result.get("result_json")
        if result_json:
            _write_json_file(str(result_json), result)

    def event(name: str, **fields: Any) -> None:
        payload = {"ts": _utc_now_iso(), "event": name, **fields}
        result["events"].append(payload)
        events_log = result.get("events_log")
        if events_log:
            _append_jsonl(str(events_log), payload)

    def finish(
        status: str,
        *,
        reason: Optional[str] = None,
        aborted: Optional[bool] = None,
        error: Optional[str] = None,
    ) -> dict[str, Any]:
        result["status"] = status
        if reason is not None:
            result["reason"] = reason
        if aborted is not None:
            result["aborted"] = aborted
        if error is not None:
            result["error"] = error
        result["finished_at"] = _utc_now_iso()
        result["duration_seconds"] = round(max(time.time() - started_monotonic, 0), 3)
        write_result()
        return result

    print(f"\n{'=' * 55}")
    print("  MemPalace Repair — SQLite Replay")
    print(f"{'=' * 55}\n")
    print(f"  Palace:     {palace_path}")
    print(f"  Source:     {collection_name}")
    print(f"  Target:     {collection_name}")

    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    if max_rows is not None and max_rows < 1:
        raise ValueError("max_rows must be positive")
    if max_batches is not None and max_batches < 1:
        raise ValueError("max_batches must be positive")

    if not os.path.isdir(palace_path):
        print(f"  No palace found at {palace_path}")
        if artifact_dir:
            ensure_artifacts()
            event("aborted", reason="palace-missing")
        return finish("aborted", reason="palace-missing", aborted=True)
    if not contains_palace_database(palace_path):
        print(f"  No palace database found at {db_path}")
        if artifact_dir:
            ensure_artifacts()
            event("aborted", reason="db-missing")
        return finish("aborted", reason="db-missing", aborted=True)

    ensure_artifacts()
    event("started")

    source_count = _sqlite_collection_count(db_path, collection_name)
    skip_exact_document_count = source_count > SQLITE_REPLAY_DRY_RUN_EXACT_DOC_LIMIT and (
        dry_run or (source_count > SQLITE_REPLAY_LARGE_REEMBED_LIMIT and not confirm_large_reembed)
    )
    if skip_exact_document_count:
        document_count = source_count
        result["document_count_exact"] = False
    else:
        document_count = _sqlite_document_count(db_path, collection_name)
    result["source_count"] = source_count
    result["document_count"] = document_count
    planned_reembed_count = document_count if result["document_count_exact"] else source_count
    result["planned_reembed_count"] = planned_reembed_count
    result["planned_batches"] = _planned_batches(planned_reembed_count, batch_size)
    event(
        "planned",
        source_count=source_count,
        document_count=document_count,
        document_count_exact=result["document_count_exact"],
        planned_reembed_count=planned_reembed_count,
        planned_batches=result["planned_batches"],
    )
    print(f"  SQLite rows: {source_count:,}")
    if result["document_count_exact"]:
        print(f"  Documents:   {document_count:,}")
    else:
        print("  Documents:   (exact count deferred; large dry run)")

    if source_count == 0:
        print("  Nothing to replay.")
        event("completed", reason="empty-source")
        return finish("completed", reason="empty-source")
    if document_count < source_count:
        missing = source_count - document_count
        print(f"  Warning: {missing:,} row(s) have no document payload and will be skipped.")
        result["warnings"].append(f"{missing} row(s) have no document payload and will be skipped")

    if max_rows is not None and planned_reembed_count > max_rows:
        print()
        print(
            f"  ABORT: replay would re-embed {planned_reembed_count:,} documents, "
            f"above --max-rows {max_rows:,}."
        )
        print("  No collection was deleted or rewritten.")
        event(
            "aborted",
            reason="max-rows-exceeded",
            planned_reembed_count=planned_reembed_count,
            max_rows=max_rows,
        )
        print(f"\n{'=' * 55}\n")
        return finish("aborted", reason="max-rows-exceeded", aborted=True)

    if max_batches is not None and result["planned_batches"] > max_batches:
        print()
        print(
            f"  ABORT: replay would need {result['planned_batches']:,} batches, "
            f"above --max-batches {max_batches:,}."
        )
        print("  No collection was deleted or rewritten.")
        event(
            "aborted",
            reason="max-batches-exceeded",
            planned_batches=result["planned_batches"],
            max_batches=max_batches,
        )
        print(f"\n{'=' * 55}\n")
        return finish("aborted", reason="max-batches-exceeded", aborted=True)

    if dry_run:
        print("\n  DRY RUN - no collection was deleted or rewritten.")
        if result["document_count_exact"]:
            replay_label = f"{document_count:,}"
        else:
            replay_label = f"up to {source_count:,}"
        print(f"  Would replay {replay_label} drawers from SQLite in batches of {batch_size:,}.")
        print(f"  Planned batches: {result['planned_batches']:,}")
        print(f"  Artifacts: {result['artifact_dir']}")
        if source_count > SQLITE_REPLAY_LARGE_REEMBED_LIMIT:
            print(
                "  Note: non-dry-run replay currently re-embeds documents; "
                "large palaces require --confirm-large-reembed."
            )
        event("dry-run")
        print(f"\n{'=' * 55}\n")
        return finish("dry-run")

    if planned_reembed_count > SQLITE_REPLAY_LARGE_REEMBED_LIMIT and not confirm_large_reembed:
        print()
        print(
            f"  ABORT: replay would re-embed up to {planned_reembed_count:,} documents. "
            "That is intentionally separate from --yes because it can take hours "
            "and consume substantial CPU/GPU."
        )
        print(
            "  Re-run with --confirm-large-reembed only when a long rebuild window is acceptable."
        )
        event(
            "aborted",
            reason="large-reembed-not-confirmed",
            planned_reembed_count=planned_reembed_count,
        )
        print(f"\n{'=' * 55}\n")
        return finish("aborted", reason="large-reembed-not-confirmed", aborted=True)

    if not confirm_destructive_action(
        "Repair from SQLite replay", palace_path, assume_yes=assume_yes
    ):
        event("aborted", reason="user-aborted")
        return finish("aborted", reason="user-aborted", aborted=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%fZ")
    source_db = os.path.join(palace_path, f"chroma.sqlite3.sqlite-replay-source-{timestamp}")
    print(f"  Snapshot: {source_db}")
    if not backup:
        warning = "--no-backup requested; source snapshot is still required for safe replay"
        result["warnings"].append(warning)
        result["backup_note"] = warning
        print(f"  Warning: {warning}.")
    try:
        shutil.copy2(db_path, source_db)
    except Exception as exc:
        print(f"  ERROR creating source snapshot: {exc}")
        event("failed", reason="snapshot-failed", error=str(exc))
        return finish("failed", reason="snapshot-failed", aborted=True, error=str(exc))
    result["backup"] = source_db
    result["source_snapshot"] = source_db
    event("snapshot-created", source_snapshot=source_db)

    _close_chroma_handles(palace_path)
    backend = _get_chroma_backend_cls()()
    print("  Rebuilding collection from SQLite snapshot...")
    try:
        backend.delete_collection(palace_path, collection_name)
    except Exception as exc:
        print(f"  ERROR deleting collection: {exc}")
        print("  No replay performed; source snapshot is intact.")
        event("failed", reason="delete-failed", error=str(exc))
        return finish("failed", reason="delete-failed", aborted=True, error=str(exc))
    result["live_collection_state"] = "deleted"
    event("collection-deleted")

    try:
        new_col = backend.create_collection(palace_path, collection_name)
    except Exception as exc:
        result["partial_live_collection"] = True
        result["live_collection_state"] = "absent-or-partial"
        print(f"  ERROR creating replacement collection: {exc}")
        print(f"  Recovery source remains at: {source_db}")
        event("failed", reason="create-failed", error=str(exc))
        return finish("failed", reason="create-failed", aborted=True, error=str(exc))
    result["live_collection_state"] = "empty-replacement"
    event("collection-created")

    replayed = 0
    batches_replayed = 0
    t0 = time.time()
    try:
        for batch in iter_drawers_from_sqlite(
            source_db, collection_name=collection_name, batch_size=batch_size
        ):
            new_col.upsert(
                ids=[record["id"] for record in batch],
                documents=[record["document"] for record in batch],
                metadatas=[record["metadata"] for record in batch],
            )
            replayed += len(batch)
            batches_replayed += 1
            result["replayed"] = replayed
            result["batches_replayed"] = batches_replayed
            elapsed = max(time.time() - t0, 0.001)
            rate = replayed / elapsed
            eta = (planned_reembed_count - replayed) / max(rate, 0.001)
            event(
                "batch-replayed",
                batch_rows=len(batch),
                replayed=replayed,
                batches_replayed=batches_replayed,
                rate_per_second=round(rate, 3),
                eta_seconds=round(max(eta, 0), 3),
            )
            print(
                f"  Replayed {replayed:,}/{planned_reembed_count:,} drawers "
                f"({rate:.1f}/s, eta {eta / 60:.1f}m)..."
            )
    except Exception as exc:
        result["partial_live_collection"] = True
        result["live_collection_state"] = "partial"
        print(f"\n  ERROR during SQLite replay after {replayed:,} drawers: {exc}")
        print(f"  Recovery source remains at: {source_db}")
        event("failed", reason="replay-failed", error=str(exc), replayed=replayed)
        return finish("failed", reason="replay-failed", aborted=True, error=str(exc))

    result["replayed"] = replayed
    result["batches_replayed"] = batches_replayed
    result["live_collection_state"] = "replayed"

    try:
        verified_count = new_col.count()
    except Exception as exc:
        result["verification_error"] = str(exc)
        event("verification-unavailable", error=str(exc))
    else:
        if isinstance(verified_count, int):
            result["verified_count"] = verified_count
            event("verified", verified_count=verified_count)
            if verified_count != replayed:
                result["partial_live_collection"] = True
                result["live_collection_state"] = "verification-mismatch"
                print(
                    f"  ERROR: verification count {verified_count:,} does not match "
                    f"replayed drawers {replayed:,}."
                )
                return finish("failed", reason="verification-mismatch", aborted=True)

    print(f"\n  SQLite replay complete. {replayed:,} drawers rebuilt.")
    if result["backup"]:
        print(f"  Source snapshot: {result['backup']}")
    print(f"  Artifacts: {result['artifact_dir']}")
    event("completed", replayed=replayed, batches_replayed=batches_replayed)
    print(f"\n{'=' * 55}\n")
    return finish("completed")


def rebuild_index(palace_path=None, confirm_truncation_ok: bool = False):
    """Rebuild the HNSW index from scratch.

    1. Extract all drawers via ChromaDB get()
    2. Cross-check against the SQLite ground truth (#1208 guard)
    3. Back up ONLY chroma.sqlite3 (not the bloated HNSW files)
    4. Delete and recreate the collection with hnsw:space=cosine
    5. Upsert all drawers back

    ``confirm_truncation_ok`` overrides the safety guard from step 2.
    Set to ``True`` only when you have independently verified that the
    palace genuinely contains exactly the extracted number of drawers
    (typically only a concern for palaces sized at exactly 10 000 rows).
    """
    palace_path = palace_path or _get_palace_path()

    if not os.path.isdir(palace_path):
        print(f"\n  No palace found at {palace_path}")
        return

    print(f"\n{'=' * 55}")
    print("  MemPalace Repair — Index Rebuild")
    print(f"{'=' * 55}\n")
    print(f"  Palace: {palace_path}")

    backend = _get_chroma_backend_cls()()
    try:
        col = backend.get_collection(palace_path, COLLECTION_NAME)
        total = col.count()
    except Exception as e:
        print(f"  Error reading palace: {e}")
        print("  Palace may need to be re-mined from source files.")
        return

    print(f"  Drawers found: {total}")

    if total == 0:
        print("  Nothing to repair.")
        return

    # Extract all drawers in batches
    print("\n  Extracting drawers...")
    batch_size = 5000
    all_ids = []
    all_docs = []
    all_metas = []
    offset = 0
    while offset < total:
        batch = col.get(limit=batch_size, offset=offset, include=["documents", "metadatas"])
        if not batch["ids"]:
            break
        all_ids.extend(batch["ids"])
        all_docs.extend(batch["documents"])
        all_metas.extend(batch["metadatas"])
        offset += len(batch["ids"])
    print(f"  Extracted {len(all_ids)} drawers")

    # ── #1208 guard ──────────────────────────────────────────────────
    # Refuse to ``delete_collection`` + rebuild when extraction looks
    # short of the SQLite ground truth (or when extraction == chromadb
    # default get() cap and the SQLite check couldn't run).
    try:
        check_extraction_safety(palace_path, len(all_ids), confirm_truncation_ok)
    except TruncationDetected as e:
        print(e.message)
        return

    # Back up ONLY the SQLite database, not the bloated HNSW files
    sqlite_path = os.path.join(palace_path, "chroma.sqlite3")
    backup_path = sqlite_path + ".backup"
    if os.path.exists(sqlite_path):
        print(f"  Backing up chroma.sqlite3 ({os.path.getsize(sqlite_path) / 1e6:.0f} MB)...")
        shutil.copy2(sqlite_path, backup_path)
        print(f"  Backup: {backup_path}")

    # Rebuild with correct HNSW settings
    print("  Rebuilding collection with hnsw:space=cosine...")
    backend.delete_collection(palace_path, COLLECTION_NAME)
    new_col = backend.create_collection(palace_path, COLLECTION_NAME)

    filed = 0
    try:
        for i in range(0, len(all_ids), batch_size):
            batch_ids = all_ids[i : i + batch_size]
            batch_docs = all_docs[i : i + batch_size]
            batch_metas = all_metas[i : i + batch_size]
            new_col.upsert(documents=batch_docs, ids=batch_ids, metadatas=batch_metas)
            filed += len(batch_ids)
            print(f"  Re-filed {filed}/{len(all_ids)} drawers...")
    except Exception as e:
        print(f"\n  ERROR during rebuild: {e}")
        print(f"  Only {filed}/{len(all_ids)} drawers were re-filed.")
        if os.path.exists(backup_path):
            print(f"  Restoring from backup: {backup_path}")
            backend.delete_collection(palace_path, COLLECTION_NAME)
            shutil.copy2(backup_path, sqlite_path)
            print("  Backup restored. Palace is back to pre-repair state.")
        else:
            print("  No backup available. Re-mine from source files to recover.")
        raise

    print(f"\n  Repair complete. {filed} drawers rebuilt.")
    print("  HNSW index is now clean with cosine distance metric.")
    print(f"\n{'=' * 55}\n")


def status(palace_path=None) -> dict:
    """Read-only health check: compare sqlite vs HNSW element counts.

    Catches the #1222 failure mode where chromadb's HNSW segment freezes
    at a stale ``max_elements`` while sqlite keeps accumulating rows.
    Once the divergence is large enough, every tool call segfaults when
    chromadb tries to load the undersized HNSW. Running ``mempalace
    repair-status`` *before* opening the segment lets the operator
    discover the problem without crashing the MCP server.

    The check itself never opens a chromadb client and never imports
    hnswlib — it reads ``chroma.sqlite3`` and ``index_metadata.pickle``
    directly via :func:`mempalace.backends.chroma.hnsw_capacity_status`.

    Returns the capacity-status dict (also printed). Returns a dict with
    ``status="unknown"`` when no palace exists at the given path.
    """
    palace_path = palace_path or _get_palace_path()
    print(f"\n{'=' * 55}")
    print("  MemPalace Repair — Status")
    print(f"{'=' * 55}\n")
    print(f"  Palace: {palace_path}")

    if not os.path.isdir(palace_path):
        print("  No palace found.\n")
        return {"status": "unknown", "message": "no palace at path"}

    capacity_status = _get_hnsw_capacity_status()
    drawers = capacity_status(palace_path, "mempalace_drawers")
    closets = capacity_status(palace_path, "mempalace_closets")

    for label, info in (("drawers", drawers), ("closets", closets)):
        print(f"\n  [{label}]")
        if info["sqlite_count"] is None:
            print("    sqlite count:   (unreadable)")
        else:
            print(f"    sqlite count:   {info['sqlite_count']:,}")
        if info["hnsw_count"] is None:
            print("    hnsw count:     (no flushed metadata yet)")
        else:
            print(f"    hnsw count:     {info['hnsw_count']:,}")
        if info["divergence"] is not None:
            print(f"    divergence:     {info['divergence']:,}")
        marker = "DIVERGED" if info["diverged"] else info["status"].upper()
        print(f"    status:         {marker}")
        if info["message"]:
            print(f"    note:           {info['message']}")

    if drawers["diverged"] or closets["diverged"]:
        print(
            "\n  Recommended: run `mempalace repair --mode sqlite-replay --dry-run` first. "
            "Large palaces require an explicit `--confirm-large-reembed` rebuild window."
        )
    print()
    return {"drawers": drawers, "closets": closets}


# ---------------------------------------------------------------------------
# max-seq-id mode: un-poison max_seq_id rows corrupted by the old shim
# ---------------------------------------------------------------------------


def _close_chroma_handles(palace_path: str) -> None:
    """Drop ChromaBackend + chromadb singleton caches so OS mmap handles release."""
    import gc

    try:
        _get_chroma_backend_cls()().close_palace(palace_path)
    except Exception:
        pass
    try:
        from chromadb.api.client import SharedSystemClient

        SharedSystemClient.clear_system_cache()
    except Exception:
        pass
    gc.collect()


class MaxSeqIdVerificationError(RuntimeError):
    """Raised when post-repair detection still sees poisoned rows."""


#: Any ``max_seq_id.seq_id`` above this is unreachable by a real palace.
#: Clean values are bounded by the embeddings_queue's monotonic counter (<1e10
#: in practice), and 2**53 is the float64 exact-integer ceiling. Poisoned
#: values from the 0.6.x shim misinterpreting chromadb 1.5.x's
#: ``b'\x11\x11' + 6 ASCII digits`` format start at ~1.23e18, so anything
#: above the threshold is confidently a shim-poisoning artefact.
MAX_SEQ_ID_SANITY_THRESHOLD = 1 << 53


def _detect_poisoned_max_seq_ids(
    db_path: str,
    *,
    segment: Optional[str] = None,
    threshold: int = MAX_SEQ_ID_SANITY_THRESHOLD,
) -> list[tuple[str, int]]:
    """Return ``[(segment_id, poisoned_seq_id), ...]`` for rows above threshold.

    If ``segment`` is given, the detection is restricted to that segment id
    (still only returning it if it actually exceeds the threshold).
    """
    with sqlite3.connect(db_path) as conn:
        if segment is not None:
            rows = conn.execute(
                "SELECT segment_id, seq_id FROM max_seq_id WHERE segment_id = ? AND seq_id > ?",
                (segment, threshold),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT segment_id, seq_id FROM max_seq_id WHERE seq_id > ?",
                (threshold,),
            ).fetchall()
    return [(str(sid), int(val)) for sid, val in rows]


def _compute_heuristic_seq_id(cur: sqlite3.Cursor, segment_id: str) -> int:
    """Return ``MAX(embeddings.seq_id)`` over the collection owning ``segment_id``.

    Matches the METADATA segment's pre-poison value exactly (its max equals
    the collection-wide embeddings max). For the sibling VECTOR segment the
    value is a few seq_ids ahead of its own pre-poison max; the queue
    treats that as "already consumed", skipping a small window of
    already-indexed embeddings on next subscribe. That is an acceptable
    loss vs. resetting to 0 (which would re-process the entire queue and
    risk HNSW bloat from issue #1046).

    ``embeddings.seq_id`` rows can be BLOB-typed on palaces where
    chromadb 1.5.x has been writing seq_ids natively (8-byte big-endian
    uint64). When SQLite's ``MAX`` returns such a row, decode it back to
    an integer rather than crashing on ``int(bytes)``.
    """
    row = cur.execute(
        """
        SELECT MAX(e.seq_id)
        FROM embeddings e
        JOIN segments s ON e.segment_id = s.id
        WHERE s.collection = (
            SELECT collection FROM segments WHERE id = ?
        )
        """,
        (segment_id,),
    ).fetchone()
    if row is None or row[0] is None:
        return 0
    val = row[0]
    if isinstance(val, (bytes, bytearray)):
        return int.from_bytes(val, "big")
    return int(val)


def _read_sidecar_seq_ids(sidecar_path: str) -> dict[str, int]:
    """Load ``{segment_id: seq_id}`` from a sidecar DB's ``max_seq_id`` table.

    Rejects sidecar files whose ``max_seq_id.seq_id`` is itself BLOB-typed
    — a sidecar that old predates chromadb's type normalisation and is not
    a trustworthy restoration source.
    """
    if not os.path.isfile(sidecar_path):
        raise FileNotFoundError(f"Sidecar database not found: {sidecar_path}")
    out: dict[str, int] = {}
    with sqlite3.connect(sidecar_path) as conn:
        rows = conn.execute("SELECT segment_id, seq_id, typeof(seq_id) FROM max_seq_id").fetchall()
    for segment_id, seq_id, kind in rows:
        if kind == "blob":
            raise ValueError(
                f"Sidecar has BLOB-typed seq_id for {segment_id}; refusing to use it. "
                "Pass a sidecar that was already migrated to INTEGER rows."
            )
        out[str(segment_id)] = int(seq_id)
    return out


def repair_max_seq_id(
    palace_path: str,
    *,
    segment: Optional[str] = None,
    from_sidecar: Optional[str] = None,
    threshold: int = MAX_SEQ_ID_SANITY_THRESHOLD,
    backup: bool = True,
    dry_run: bool = False,
    assume_yes: bool = False,
) -> dict:
    """Un-poison ``max_seq_id`` rows corrupted by ``_fix_blob_seq_ids`` misfire.

    The old shim ran ``int.from_bytes(blob, 'big')`` across every BLOB
    ``max_seq_id.seq_id`` row, including chromadb 1.5.x's native
    ``b'\\x11\\x11' + ASCII digits`` format. That conversion yields a
    ~1.23e18 integer that silently suppresses every subsequent
    ``embeddings_queue`` write for the affected segment. This command
    restores clean values either from a pre-corruption sidecar DB
    (exact) or heuristically (``MAX(embeddings.seq_id)`` over the owning
    collection).
    """
    from .migrate import confirm_destructive_action, contains_palace_database

    palace_path = os.path.abspath(os.path.expanduser(palace_path))
    db_path = os.path.join(palace_path, "chroma.sqlite3")

    result: dict = {
        "palace_path": palace_path,
        "dry_run": dry_run,
        "aborted": False,
        "segment_repaired": [],
        "before": {},
        "after": {},
        "backup": None,
    }

    print(f"\n{'=' * 55}")
    print("  MemPalace Repair — max_seq_id Un-poison")
    print(f"{'=' * 55}\n")
    print(f"  Palace:  {palace_path}")
    if segment:
        print(f"  Segment: {segment}")
    if from_sidecar:
        print(f"  Sidecar: {from_sidecar}")

    if not os.path.isdir(palace_path):
        print(f"  No palace found at {palace_path}")
        result["aborted"] = True
        result["reason"] = "palace-missing"
        return result
    if not contains_palace_database(palace_path):
        print(f"  No palace database at {palace_path}")
        result["aborted"] = True
        result["reason"] = "db-missing"
        return result

    poisoned = _detect_poisoned_max_seq_ids(db_path, segment=segment, threshold=threshold)
    if not poisoned:
        print("  No poisoned max_seq_id rows detected. Nothing to do.")
        print(f"\n{'=' * 55}\n")
        return result

    sidecar_map: dict[str, int] = {}
    if from_sidecar:
        sidecar_map = _read_sidecar_seq_ids(from_sidecar)

    plan: list[tuple[str, int, int]] = []
    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()
        for seg_id, old_val in poisoned:
            if from_sidecar:
                if seg_id not in sidecar_map:
                    print(f"  Skipped segment {seg_id}: no sidecar entry")
                    continue
                new_val = sidecar_map[seg_id]
            else:
                new_val = _compute_heuristic_seq_id(cur, seg_id)
            plan.append((seg_id, old_val, new_val))
            result["before"][seg_id] = old_val
            result["after"][seg_id] = new_val

    print()
    print("  Report")
    print(f"    poisoned rows        {len(poisoned):>6}")
    print(f"    planned repairs      {len(plan):>6}")
    source = "sidecar" if from_sidecar else "heuristic (collection MAX)"
    print(f"    clean-value source   {source}")
    for seg_id, old_val, new_val in plan:
        print(f"    {seg_id}  {old_val}  →  {new_val}")

    if dry_run:
        print("\n  DRY RUN — no rows modified.\n" + "=" * 55 + "\n")
        return result

    if not plan:
        print("  No actionable repairs.")
        print(f"\n{'=' * 55}\n")
        return result

    if not confirm_destructive_action("Repair max_seq_id", palace_path, assume_yes=assume_yes):
        result["aborted"] = True
        result["reason"] = "user-aborted"
        return result

    if backup:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = os.path.join(palace_path, f"chroma.sqlite3.max-seq-id-backup-{timestamp}")
        shutil.copy2(db_path, backup_path)
        result["backup"] = backup_path
        print(f"  Backup:  {backup_path}")

    _close_chroma_handles(palace_path)

    with sqlite3.connect(db_path) as conn:
        conn.execute("BEGIN")
        try:
            conn.executemany(
                "UPDATE max_seq_id SET seq_id = ? WHERE segment_id = ?",
                [(new_val, seg_id) for seg_id, _old, new_val in plan],
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    remaining = _detect_poisoned_max_seq_ids(db_path, segment=segment, threshold=threshold)
    if remaining:
        raise MaxSeqIdVerificationError(
            f"Post-repair detection still found {len(remaining)} poisoned row(s): "
            f"{[sid for sid, _ in remaining]}. Backup at {result['backup']}."
        )

    result["segment_repaired"] = [seg_id for seg_id, _old, _new in plan]
    print(f"\n  Repair complete. {len(plan)} row(s) restored.")
    print(f"  Backup:  {result['backup'] or '(skipped)'}")
    print(f"\n{'=' * 55}\n")
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="MemPalace repair tools")
    p.add_argument("command", choices=["status", "scan", "prune", "rebuild"])
    p.add_argument("--palace", default=None, help="Palace directory path")
    p.add_argument("--wing", default=None, help="Scan only this wing")
    p.add_argument("--confirm", action="store_true", help="Actually delete corrupt IDs")
    args = p.parse_args()

    path = os.path.expanduser(args.palace) if args.palace else None

    if args.command == "status":
        status(palace_path=path)
    elif args.command == "scan":
        scan_palace(palace_path=path, only_wing=args.wing)
    elif args.command == "prune":
        prune_corrupt(palace_path=path, confirm=args.confirm)
    elif args.command == "rebuild":
        rebuild_index(palace_path=path)
