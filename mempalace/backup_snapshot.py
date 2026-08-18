"""Clean-client lease + staged consistent palace snapshot + content-identity proof.

Why this module exists
----------------------
A palace backup used to be ``tar -czf`` over the live palace root.  That is not
crash-consistent: ``chroma.sqlite3`` is a multi-gigabyte live SQLite database and
the per-collection HNSW segment directories (``header.bin``, ``data_level0.bin``,
``link_lists.bin``, ``index_metadata.pickle``) are mutated as a group.  A
recursive archive of that tree can capture a torn database page set, or a
sqlite catalog that disagrees with the segment files copied moments later.

This module builds the three primitives a trustworthy palace backup needs, in
the order the ``agent-settings`` backup gate demands them:

1. **Clean-client lease** - :func:`clean_client_lease` takes the existing
   MemPalace-owned exclusive palace lock (:func:`mempalace.palace.mine_palace_lock`,
   the same lock every ``mempalace mine`` and every MCP managed write already
   acquires through ``managed_write_scope``) and, on Windows, also raises the
   shared ``%LOCALAPPDATA%\\MemSys\\.maintenance`` pause marker that the MemSys
   miners, watchdogs, and router launcher already honor.  Reuse, not invention.

2. **Staged consistent snapshot** - :func:`stage_palace_snapshot` copies
   ``chroma.sqlite3`` with the SQLite *online backup API* rather than a byte
   copy, then ``PRAGMA integrity_check`` s the result, then copies only the
   segment directories the *snapshot's own* catalog references.

3. **Content-identity proof** - every staged file is hashed, and the
   dependency-light ``repair-status`` drawer/closet counts are computed on both
   the live palace (under lease) and the staged snapshot and must agree.  The
   receipt is verifiable later with :func:`verify_snapshot_receipt` without
   unpacking or reopening Chroma.

Writer-quiescence proof
-----------------------
The lease alone is an *assertion*.  This module additionally *proves* quiescence
with SQLite's ``PRAGMA data_version``: that counter changes whenever a different
connection commits to the database, so a witness connection that observes the
same ``data_version`` before and after the snapshot window is direct evidence
that no other process committed mid-snapshot.  This matters because SQLite's
online backup restarts from scratch whenever an *external* process writes to the
source ("the entire backup operation must be restarted" -- sqlite.org/backup.html),
so an unnoticed writer means either an endlessly restarting backup or a snapshot
whose surrounding segment copies no longer match.

This module never mutates the live palace.  It only reads it, and writes into a
staging directory that must be outside the palace root.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
from contextlib import contextmanager

# datetime.UTC is a 3.11+ alias; CI runs 3.9, where only timezone.utc exists.
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .palace import MineAlreadyRunning, mine_palace_lock

SNAPSHOT_RECEIPT_SCHEMA = "mempalace-backup-snapshot-receipt/v1"
SNAPSHOT_RECEIPT_FILENAME = "backup-snapshot-receipt.json"
SNAPSHOT_METHOD = "sqlite-online-backup+catalog-scoped-segment-copy/v1"
LEASE_KIND = "mempalace-clean-client-lease/v1"

#: Interim status written by ``stage_palace_snapshot(..., verify=False)``. A
#: growing palace makes ``PRAGMA integrity_check`` plus full-tree hashing take
#: non-linearly longer (memsys#423: 26 minutes on a 25 GiB database), which can
#: blow a bounded copy-timeout budget even though the copy itself finished
#: cleanly. This status lets the copy phase finish and hand off the slow
#: proof work to a separately scheduled, separately timed verification pass
#: (:func:`verify_staged_snapshot`) without ever claiming a proof that was
#: never actually run.
SNAPSHOT_STATUS_AWAITING_VERIFICATION = "staged-awaiting-verification"

#: Sidecar written next to an awaiting-verification receipt. Carries exactly
#: what the deferred verification pass needs and cannot re-derive without the
#: lease: the source drawer/closet counts captured live, under lease, at copy
#: time. Re-reading the live palace later would compare against a
#: potentially-mutated palace state instead of the moment the snapshot was
#: actually taken.
STAGING_MANIFEST_SCHEMA = "mempalace-backup-snapshot-staging-manifest/v1"
STAGING_MANIFEST_FILENAME = "staging-manifest.json"

#: Live SQLite sidecars.  Their presence in the *staged* snapshot would mean the
#: snapshot was taken from a database with uncommitted or unmerged state.
LIVE_SQLITE_SIDECARS = ("chroma.sqlite3-wal", "chroma.sqlite3-shm", "chroma.sqlite3-journal")

#: Palace root entries that are part of the active restore set but are not
#: catalog-referenced vector segments.
STATIC_INCLUDES = (".blob_seq_ids_migrated",)

#: ``.mempalace`` metadata children that belong in a restore set.  Anything else
#: found there is unclassified and fails the snapshot closed.
KNOWN_METADATA_CHILDREN = (
    ".mempalace-directory-durable-v1",
    "repair-runs",
    "write-receipts",
)

FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_HASH_BLOCK = 8 * 1024 * 1024


class PalaceSnapshotError(RuntimeError):
    """A snapshot precondition, lease, or proof failed.  Always fail closed."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _is_link_or_reparse(path: Path) -> bool:
    try:
        stat_result = path.lstat()
    except OSError:
        return False
    return path.is_symlink() or bool(
        getattr(stat_result, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT
    )


def sha256_file(path: Path) -> str:
    """Stream a file into sha256 without loading it into memory."""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(_HASH_BLOCK), b""):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


# --------------------------------------------------------------------------
# 1. Clean-client lease
# --------------------------------------------------------------------------


def default_maintenance_marker() -> Path | None:
    """The shared MemSys pause marker, when this host has one.

    ``%LOCALAPPDATA%\\MemSys\\.maintenance`` is already honored by
    ``Mine-Drives.ps1``, ``Start-MemSysRouter.ps1``, and the ``Watch-MemSys*``
    watchdogs.  Reusing it means the snapshot pauses the same writers the rest
    of the fleet already knows how to pause.
    """

    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return None
    return Path(local_app_data) / "MemSys" / ".maintenance"


@contextmanager
def clean_client_lease(
    palace_path: Path,
    *,
    maintenance_marker: Path | None = None,
    use_maintenance_marker: bool = True,
) -> Iterator[dict[str, Any]]:
    """Hold exclusive write authority over one palace for the snapshot window.

    Layer 1 is the MemPalace-owned exclusive palace lock.  It is *non-blocking*:
    if a ``mempalace mine`` or an MCP managed write already holds it, this raises
    :class:`PalaceSnapshotError` rather than queueing behind a multi-hour miner.

    Layer 2 is the shared MemSys maintenance marker, which asks the schedulers
    and watchdogs not to start new work.  It is best effort: a marker that
    another operator already raised is left exactly as found on release.
    """

    resolved = Path(palace_path).expanduser().resolve()
    marker: Path | None = None
    if use_maintenance_marker:
        marker = (
            maintenance_marker if maintenance_marker is not None else default_maintenance_marker()
        )

    marker_created = False
    marker_pre_existing = False
    if marker is not None:
        marker_pre_existing = marker.exists()
        if not marker_pre_existing:
            try:
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text(
                    f"mempalace backup-snapshot lease {_utc_now()} pid={os.getpid()}\n",
                    encoding="utf-8",
                )
                marker_created = True
            except OSError:
                # Best effort only. The palace lock below is the hard boundary.
                marker = None

    try:
        with mine_palace_lock(str(resolved)):
            yield {
                "kind": LEASE_KIND,
                "palaceLockHeld": True,
                "palaceLockScope": "exclusive-cross-process",
                "maintenanceMarkerPath": str(marker) if marker is not None else "",
                "maintenanceMarkerRaisedByLease": marker_created,
                "maintenanceMarkerPreExisting": marker_pre_existing,
            }
    except MineAlreadyRunning as exc:
        raise PalaceSnapshotError(
            "clean-client lease unavailable: another mempalace writer "
            f"(mine or MCP managed write) holds the palace lock for {resolved}"
        ) from exc
    finally:
        if marker_created and marker is not None:
            try:
                marker.unlink(missing_ok=True)
            except OSError:
                pass


# --------------------------------------------------------------------------
# Writer-quiescence witness
# --------------------------------------------------------------------------


class _DataVersionWitness:
    """Detect commits made by *other* connections across the snapshot window.

    ``PRAGMA data_version`` is documented to change when the database file is
    modified by any connection other than this one.  The value is only refreshed
    when this connection reads, so the witness keeps one long-lived read-only
    connection in autocommit mode and re-reads at the end.
    """

    def __init__(self, source: Path) -> None:
        self._connection = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True, timeout=5)
        self.journal_mode = str(self._connection.execute("PRAGMA journal_mode").fetchone()[0])
        self.page_size = int(self._connection.execute("PRAGMA page_size").fetchone()[0])
        self.page_count = int(self._connection.execute("PRAGMA page_count").fetchone()[0])
        self.initial = self._read()

    def _read(self) -> int:
        return int(self._connection.execute("PRAGMA data_version").fetchone()[0])

    def observe(self) -> int:
        return self._read()

    def close(self) -> None:
        try:
            self._connection.close()
        except sqlite3.Error:
            pass


# --------------------------------------------------------------------------
# 2. Staged consistent snapshot
# --------------------------------------------------------------------------


def _snapshot_sqlite(source: Path, destination: Path, *, progress: bool = False) -> dict[str, Any]:
    """Copy a live SQLite database with the online backup API.

    A byte copy (``tar``, ``robocopy``, ``Copy-Item``) of a database that any
    process may write is not a snapshot.  ``sqlite3.Connection.backup`` takes a
    series of small locks, copies pages, and re-copies pages dirtied underneath
    it, so the destination is a consistent point-in-time image when it finishes.
    """

    if not source.is_file():
        raise PalaceSnapshotError(f"palace SQLite catalog is unavailable: {source}")

    pages_done = {"copied": 0, "total": 0, "callbacks": 0}

    def _progress(status: int, remaining: int, total: int) -> None:
        pages_done["copied"] = total - remaining
        pages_done["total"] = total
        pages_done["callbacks"] += 1
        if progress and pages_done["callbacks"] % 20 == 0:
            pct = 100.0 * (total - remaining) / max(total, 1)
            print(f"  sqlite online backup: {pct:5.1f}%", file=sys.stderr, flush=True)

    started = time.monotonic()
    try:
        reader = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True, timeout=5)
        try:
            writer = sqlite3.connect(str(destination))
            try:
                reader.backup(writer, pages=4096, progress=_progress)
            finally:
                writer.close()
        finally:
            reader.close()
    except sqlite3.Error as exc:
        raise PalaceSnapshotError(f"palace SQLite online backup failed: {exc}") from exc
    return {
        "method": "sqlite-online-backup",
        "pagesCopied": pages_done["copied"],
        "pageTotal": pages_done["total"],
        "durationSeconds": round(time.monotonic() - started, 3),
    }


def _sqlite_integrity_check(snapshot: Path) -> str:
    try:
        connection = sqlite3.connect(f"file:{snapshot.as_posix()}?mode=ro", uri=True, timeout=5)
        try:
            result = connection.execute("PRAGMA integrity_check").fetchone()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise PalaceSnapshotError(f"snapshot SQLite integrity check failed: {exc}") from exc
    verdict = str(result[0]) if result else "missing"
    if verdict != "ok":
        raise PalaceSnapshotError(f"snapshot SQLite integrity check did not return ok: {verdict}")
    return verdict


def read_catalog(sqlite_path: Path) -> list[dict[str, Any]]:
    """Return the collections and their currently referenced segment ids."""

    connection = sqlite3.connect(f"file:{sqlite_path.as_posix()}?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT
                c.id   AS collection_id,
                c.name AS collection_name,
                v.id   AS vector_segment_id,
                m.id   AS metadata_segment_id,
                COALESCE(ec.embedding_count, 0) AS embedding_count
            FROM collections AS c
            LEFT JOIN segments AS v ON v.collection = c.id AND v.scope = 'VECTOR'
            LEFT JOIN segments AS m ON m.collection = c.id AND m.scope = 'METADATA'
            LEFT JOIN (
                SELECT segment_id, COUNT(*) AS embedding_count
                FROM embeddings GROUP BY segment_id
            ) AS ec ON ec.segment_id = m.id
            ORDER BY c.name, c.id
            """
        ).fetchall()
    finally:
        connection.close()
    return [
        {
            "collectionId": row["collection_id"],
            "collectionName": row["collection_name"],
            "vectorSegmentId": row["vector_segment_id"] or "",
            "metadataSegmentId": row["metadata_segment_id"] or "",
            "embeddingCount": int(row["embedding_count"]),
        }
        for row in rows
    ]


def _copy_tree_no_links(source: Path, destination: Path) -> tuple[int, int]:
    """Copy a directory, refusing reparse points, returning (bytes, files)."""

    if _is_link_or_reparse(source):
        raise PalaceSnapshotError(f"refusing to copy a reparse point: {source}")
    destination.mkdir(parents=True, exist_ok=False)
    total_bytes = 0
    total_files = 0
    for entry in sorted(source.iterdir(), key=lambda p: p.name.lower()):
        if _is_link_or_reparse(entry):
            raise PalaceSnapshotError(f"refusing to copy a reparse point: {entry}")
        if entry.is_dir():
            child_bytes, child_files = _copy_tree_no_links(entry, destination / entry.name)
            total_bytes += child_bytes
            total_files += child_files
        elif entry.is_file():
            shutil.copy2(entry, destination / entry.name)
            total_bytes += (destination / entry.name).stat().st_size
            total_files += 1
        else:
            raise PalaceSnapshotError(f"unsupported filesystem entry: {entry}")
    return total_bytes, total_files


# --------------------------------------------------------------------------
# 3. Content-identity proof
# --------------------------------------------------------------------------


def _counts_payload(palace_root: Path) -> dict[str, Any]:
    """Drawer/closet SQLite-vs-HNSW counts from the dependency-light probe.

    Reuses ``mempalace.repair.status_payload`` -- the same code behind
    ``mempalace repair-status --json`` -- so the snapshot's identity numbers are
    the numbers operators already read, not a second private definition.
    """

    from .repair import status_payload

    payload = status_payload(str(palace_root))
    trimmed: dict[str, Any] = {}
    for label in ("drawers", "closets"):
        entry = payload.get(label)
        if not isinstance(entry, dict):
            continue
        trimmed[label] = {
            "segmentId": entry.get("segment_id"),
            "sqliteCount": entry.get("sqlite_count"),
            "hnswCount": entry.get("hnsw_count"),
            "divergence": entry.get("divergence"),
            "diverged": bool(entry.get("diverged")),
            "status": entry.get("status"),
        }
    return trimmed


def _identity_comparable(counts: dict[str, Any]) -> dict[str, Any]:
    """The subset of the counts that must be byte-identical across the copy."""

    return {
        label: {
            "segmentId": entry.get("segmentId"),
            "sqliteCount": entry.get("sqliteCount"),
            "hnswCount": entry.get("hnswCount"),
        }
        for label, entry in sorted(counts.items())
    }


def _hash_staged_tree(staged_root: Path) -> tuple[list[dict[str, Any]], int]:
    files: list[dict[str, Any]] = []
    total_bytes = 0
    for path in sorted(staged_root.rglob("*"), key=lambda p: p.as_posix().casefold()):
        if not path.is_file():
            continue
        size = path.stat().st_size
        total_bytes += size
        files.append(
            {
                "relativePath": path.relative_to(staged_root).as_posix(),
                "bytes": size,
                "sha256": sha256_file(path),
            }
        )
    return files, total_bytes


def canonical_identity_digest(payload: Any) -> str:
    canonical = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def _assert_staging_outside_palace(staging_dir: Path, palace_root: Path) -> None:
    if staging_dir == palace_root or palace_root in staging_dir.parents:
        raise PalaceSnapshotError("staging directory must not live inside the palace root")


def stage_palace_snapshot(
    palace_path: str | os.PathLike[str],
    staging_dir: str | os.PathLike[str],
    *,
    use_maintenance_marker: bool = True,
    maintenance_marker: Path | None = None,
    progress: bool = False,
    verify: bool = True,
) -> dict[str, Any]:
    """Produce a staged, consistent palace snapshot, optionally deferring proof.

    Returns the receipt dict, which is also written to
    ``<staging_dir>/backup-snapshot-receipt.json``.  The staged restore set
    itself lives in ``<staging_dir>/palace``.

    ``verify=True`` (default) is the original, self-contained behavior: the
    sqlite ``PRAGMA integrity_check`` and the full-tree content-identity hash
    both run inline, and the receipt is written with ``status="complete"``
    and all three proof flags true.

    ``verify=False`` performs only the clean-client-lease copy -- the sqlite
    online-backup copy, catalog-scoped segment copy, and writer-quiescence
    witness -- then returns and writes an interim receipt with
    ``status=SNAPSHOT_STATUS_AWAITING_VERIFICATION`` and
    ``snapshotConsistencyProven=False`` / ``contentIdentityProven=False``
    (never a false claim of proof). A sibling ``staging-manifest.json`` is
    written alongside it carrying everything :func:`verify_staged_snapshot`
    needs to finish the proof later, out of process, without re-touching the
    live palace or re-acquiring the lease.
    """

    started = time.monotonic()
    palace_root = Path(palace_path).expanduser().resolve(strict=True)
    staging = Path(staging_dir).expanduser().resolve()
    _assert_staging_outside_palace(staging, palace_root)

    if staging.exists() and any(staging.iterdir()):
        raise PalaceSnapshotError(f"staging directory is not empty: {staging}")

    source_sqlite = palace_root / "chroma.sqlite3"
    if _is_link_or_reparse(source_sqlite):
        raise PalaceSnapshotError("palace SQLite catalog is a link; refusing to snapshot")

    staged_root = staging / "palace"
    receipt_path = staging / SNAPSHOT_RECEIPT_FILENAME

    with clean_client_lease(
        palace_root,
        maintenance_marker=maintenance_marker,
        use_maintenance_marker=use_maintenance_marker,
    ) as lease:
        # Under lease: a live sidecar means a client is still attached, or a
        # previous process died mid-transaction. Either way, do not snapshot.
        present_sidecars = [name for name in LIVE_SQLITE_SIDECARS if (palace_root / name).exists()]
        if present_sidecars:
            raise PalaceSnapshotError(
                "live SQLite sidecars present under the clean-client lease: "
                + ",".join(present_sidecars)
            )

        source_counts = _counts_payload(palace_root)
        witness = _DataVersionWitness(source_sqlite)
        try:
            staged_root.mkdir(parents=True, exist_ok=False)
            sqlite_result = _snapshot_sqlite(
                source_sqlite, staged_root / "chroma.sqlite3", progress=progress
            )
            # A growing palace makes this non-linearly slower (memsys#423: 26
            # minutes on a 25 GiB database) and is exactly what verify=False
            # defers to the separate Verify-PalaceSnapshotStaging lane.
            integrity = (
                _sqlite_integrity_check(staged_root / "chroma.sqlite3") if verify else "deferred"
            )

            # The snapshot's own catalog decides what else belongs in the
            # restore set. Reading the live catalog instead would let a
            # post-snapshot commit widen the copy set.
            collections = read_catalog(staged_root / "chroma.sqlite3")
            referenced = sorted(
                {entry["vectorSegmentId"] for entry in collections if entry["vectorSegmentId"]}
            )

            staged_entries: list[dict[str, Any]] = []
            for segment_id in referenced:
                segment_source = palace_root / segment_id
                if not segment_source.is_dir():
                    matching = [c for c in collections if c["vectorSegmentId"] == segment_id]
                    if all(entry["embeddingCount"] == 0 for entry in matching):
                        continue
                    raise PalaceSnapshotError(
                        f"catalog references a missing vector segment directory: {segment_id}"
                    )
                seg_bytes, seg_files = _copy_tree_no_links(segment_source, staged_root / segment_id)
                staged_entries.append(
                    {
                        "relativePath": segment_id,
                        "kind": "vector-segment",
                        "bytes": seg_bytes,
                        "fileCount": seg_files,
                    }
                )

            for name in STATIC_INCLUDES:
                candidate = palace_root / name
                if candidate.is_file() and not _is_link_or_reparse(candidate):
                    shutil.copy2(candidate, staged_root / name)
                    staged_entries.append(
                        {
                            "relativePath": name,
                            "kind": "chroma-marker",
                            "bytes": (staged_root / name).stat().st_size,
                            "fileCount": 1,
                        }
                    )

            metadata_root = palace_root / ".mempalace"
            if metadata_root.is_dir() and not _is_link_or_reparse(metadata_root):
                unclassified = [
                    child.name
                    for child in metadata_root.iterdir()
                    if child.name not in KNOWN_METADATA_CHILDREN
                ]
                if unclassified:
                    raise PalaceSnapshotError(
                        "unclassified .mempalace metadata entries: "
                        + ",".join(sorted(unclassified))
                    )
                staged_metadata = staged_root / ".mempalace"
                staged_metadata.mkdir(parents=True, exist_ok=False)
                for child in sorted(metadata_root.iterdir(), key=lambda p: p.name.lower()):
                    if child.is_dir():
                        child_bytes, child_files = _copy_tree_no_links(
                            child, staged_metadata / child.name
                        )
                    else:
                        shutil.copy2(child, staged_metadata / child.name)
                        child_bytes = (staged_metadata / child.name).stat().st_size
                        child_files = 1
                    staged_entries.append(
                        {
                            "relativePath": f".mempalace/{child.name}",
                            "kind": "palace-metadata",
                            "bytes": child_bytes,
                            "fileCount": child_files,
                        }
                    )

            # Quiescence proof: no other connection committed across the window.
            final_data_version = witness.observe()
            if final_data_version != witness.initial:
                raise PalaceSnapshotError(
                    "clean-client lease was violated: the palace SQLite data_version changed "
                    f"during the snapshot window ({witness.initial} -> {final_data_version})"
                )
        finally:
            witness.close()

        snapshot_counts = _counts_payload(staged_root) if verify else None

    # Lease released. Everything below reads only the staged copy, so none of
    # it needs to happen while the exclusive lease is held.
    lease_record = {
        **lease,
        "sourceJournalMode": witness.journal_mode,
        "sourcePageSize": witness.page_size,
        "sourcePageCount": witness.page_count,
        "initialDataVersion": witness.initial,
        "finalDataVersion": final_data_version,
        "writerQuiescenceProven": True,
    }

    if not verify:
        staging.mkdir(parents=True, exist_ok=True)
        generated_at = _utc_now()
        copy_duration = round(time.monotonic() - started, 3)
        manifest: dict[str, Any] = {
            "schema": STAGING_MANIFEST_SCHEMA,
            "generatedAt": generated_at,
            "palaceRoot": str(palace_root),
            "stagedRoot": str(staged_root),
            "snapshotMethod": SNAPSHOT_METHOD,
            # Captured live, under lease, at copy time. The deferred verify
            # pass compares against THIS value, never a fresh live re-read,
            # because the palace may have moved on by the time it runs.
            "sourceCounts": source_counts,
            "collections": collections,
            "lease": lease_record,
            "sqliteSnapshot": sqlite_result,
            "stagedEntries": staged_entries,
            "copyDurationSeconds": copy_duration,
        }
        manifest_path = staging / STAGING_MANIFEST_FILENAME
        with manifest_path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(manifest, stream, ensure_ascii=True, indent=2, sort_keys=True)
            stream.write("\n")

        interim_receipt: dict[str, Any] = {
            "schema": SNAPSHOT_RECEIPT_SCHEMA,
            "status": SNAPSHOT_STATUS_AWAITING_VERIFICATION,
            "generatedAt": generated_at,
            "snapshotMethod": SNAPSHOT_METHOD,
            "palaceRoot": str(palace_root),
            "stagedRoot": str(staged_root),
            "lease": lease_record,
            "sqliteSnapshot": {**sqlite_result, "integrityCheck": "deferred"},
            "stagedEntries": staged_entries,
            "contentIdentity": None,
            "contentIdentityDigest": None,
            # The copy phase DID prove the lease (reaching this line means the
            # clean-client lease context manager completed without raising).
            # It deliberately did NOT run integrity_check or hashing.
            "leaseProven": True,
            "snapshotConsistencyProven": False,
            "contentIdentityProven": False,
            "verification": {
                "status": "pending",
                "manifestPath": str(manifest_path),
            },
            "durationSeconds": copy_duration,
        }
        with receipt_path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(interim_receipt, stream, ensure_ascii=True, indent=2, sort_keys=True)
            stream.write("\n")
        interim_receipt["receiptPath"] = str(receipt_path)
        interim_receipt["manifestPath"] = str(manifest_path)
        return interim_receipt

    if _identity_comparable(source_counts) != _identity_comparable(snapshot_counts):
        raise PalaceSnapshotError(
            "content identity failed: staged snapshot drawer/closet counts differ from the source"
        )

    staged_files, staged_bytes = _hash_staged_tree(staged_root)
    content_identity = {
        "algorithm": "sha256",
        "canonicalization": "json-sort-keys-ascii-v1",
        "fileCount": len(staged_files),
        "totalBytes": staged_bytes,
        "files": staged_files,
        "counts": snapshot_counts,
        "collections": collections,
    }
    identity_digest = canonical_identity_digest(
        {
            "files": [
                {"relativePath": f["relativePath"], "sha256": f["sha256"]} for f in staged_files
            ],
            "counts": _identity_comparable(snapshot_counts),
        }
    )

    receipt: dict[str, Any] = {
        "schema": SNAPSHOT_RECEIPT_SCHEMA,
        "status": "complete",
        "generatedAt": _utc_now(),
        "snapshotMethod": SNAPSHOT_METHOD,
        "palaceRoot": str(palace_root),
        "stagedRoot": str(staged_root),
        "lease": lease_record,
        "sqliteSnapshot": {
            **sqlite_result,
            "integrityCheck": integrity,
        },
        "stagedEntries": staged_entries,
        "contentIdentity": content_identity,
        "contentIdentityDigest": identity_digest,
        "leaseProven": True,
        "snapshotConsistencyProven": True,
        "contentIdentityProven": True,
        "durationSeconds": round(time.monotonic() - started, 3),
    }
    staging.mkdir(parents=True, exist_ok=True)
    with receipt_path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(receipt, stream, ensure_ascii=True, indent=2, sort_keys=True)
        stream.write("\n")
    receipt["receiptPath"] = str(receipt_path)
    return receipt


def verify_staged_snapshot(staging_dir: str | os.PathLike[str]) -> dict[str, Any]:
    """Finish a deferred verification: sqlite integrity_check + first hashing.

    Companion to ``stage_palace_snapshot(..., verify=False)``. That call
    copies the palace and writes an interim receipt with
    ``status=SNAPSHOT_STATUS_AWAITING_VERIFICATION`` plus a sibling
    ``staging-manifest.json`` carrying everything this function needs to
    finish the proof without re-touching the live palace or re-acquiring the
    lease: the source drawer/closet counts captured live, under lease, at
    copy time, the lease record, and the sqlite copy result.

    This function reads that manifest, runs the sqlite integrity check and
    the full-tree content-identity hash the copy phase deliberately skipped,
    and rewrites the SAME receipt file in place with the final verdict --
    ``status="complete"`` with all three proof flags true on success,
    ``status="error"`` with the proof flags false on failure. Either way the
    generation is left with a receipt that says what happened: never left
    silently unverified forever, and never silently marked proven.

    Precondition failures -- a missing manifest, a missing staged sqlite file
    -- raise :class:`PalaceSnapshotError` without touching whatever receipt is
    already on disk, because they mean verification could not even be
    attempted, which is a different fact than "attempted and failed".
    """

    staging = Path(staging_dir).expanduser().resolve(strict=True)
    staged_root = staging / "palace"
    manifest_path = staging / STAGING_MANIFEST_FILENAME
    receipt_path = staging / SNAPSHOT_RECEIPT_FILENAME

    if not manifest_path.is_file():
        raise PalaceSnapshotError(
            f"staging manifest is missing; cannot verify (was verify=False ever run here?): {manifest_path}"
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PalaceSnapshotError(
            f"staging manifest is unreadable: {manifest_path}: {exc}"
        ) from exc
    if manifest.get("schema") != STAGING_MANIFEST_SCHEMA:
        raise PalaceSnapshotError(f"unexpected staging manifest schema: {manifest.get('schema')}")

    staged_sqlite = staged_root / "chroma.sqlite3"
    if not staged_sqlite.is_file():
        raise PalaceSnapshotError(f"staged SQLite catalog is unavailable: {staged_sqlite}")

    started = time.monotonic()
    source_counts = manifest.get("sourceCounts") or {}
    lease_record = manifest.get("lease") or {}
    sqlite_result = manifest.get("sqliteSnapshot") or {}
    staged_entries = manifest.get("stagedEntries") or []
    palace_root_text = str(manifest.get("palaceRoot", ""))
    staged_root_text = str(manifest.get("stagedRoot", str(staged_root)))

    def _write_failure(message: str, *, integrity_result: str) -> dict[str, Any]:
        failure: dict[str, Any] = {
            "schema": SNAPSHOT_RECEIPT_SCHEMA,
            "status": "error",
            "generatedAt": _utc_now(),
            "snapshotMethod": SNAPSHOT_METHOD,
            "palaceRoot": palace_root_text,
            "stagedRoot": staged_root_text,
            "lease": lease_record,
            "sqliteSnapshot": {**sqlite_result, "integrityCheck": integrity_result},
            "stagedEntries": staged_entries,
            "contentIdentity": None,
            "contentIdentityDigest": None,
            # A valid manifest existing at all is itself proof the copy-phase
            # lease succeeded; only the two slow proofs are what verification
            # decides here.
            "leaseProven": True,
            "snapshotConsistencyProven": False,
            "contentIdentityProven": False,
            "verification": {
                "status": "failed",
                "message": message,
                "manifestPath": str(manifest_path),
            },
            "durationSeconds": round(time.monotonic() - started, 3),
        }
        with receipt_path.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(failure, stream, ensure_ascii=True, indent=2, sort_keys=True)
            stream.write("\n")
        failure["receiptPath"] = str(receipt_path)
        return failure

    try:
        integrity = _sqlite_integrity_check(staged_sqlite)
    except PalaceSnapshotError as exc:
        return _write_failure(str(exc), integrity_result="failed")

    snapshot_counts = _counts_payload(staged_root)
    if _identity_comparable(source_counts) != _identity_comparable(snapshot_counts):
        return _write_failure(
            "content identity failed: staged snapshot drawer/closet counts differ from the "
            "source counts captured at copy time",
            integrity_result=integrity,
        )

    staged_files, staged_bytes = _hash_staged_tree(staged_root)
    collections = manifest.get("collections") or read_catalog(staged_sqlite)
    content_identity = {
        "algorithm": "sha256",
        "canonicalization": "json-sort-keys-ascii-v1",
        "fileCount": len(staged_files),
        "totalBytes": staged_bytes,
        "files": staged_files,
        "counts": snapshot_counts,
        "collections": collections,
    }
    identity_digest = canonical_identity_digest(
        {
            "files": [
                {"relativePath": f["relativePath"], "sha256": f["sha256"]} for f in staged_files
            ],
            "counts": _identity_comparable(snapshot_counts),
        }
    )

    receipt: dict[str, Any] = {
        "schema": SNAPSHOT_RECEIPT_SCHEMA,
        "status": "complete",
        "generatedAt": _utc_now(),
        "snapshotMethod": SNAPSHOT_METHOD,
        "palaceRoot": palace_root_text,
        "stagedRoot": staged_root_text,
        "lease": lease_record,
        "sqliteSnapshot": {**sqlite_result, "integrityCheck": integrity},
        "stagedEntries": staged_entries,
        "contentIdentity": content_identity,
        "contentIdentityDigest": identity_digest,
        "leaseProven": True,
        "snapshotConsistencyProven": True,
        "contentIdentityProven": True,
        "verification": {
            "status": "complete",
            "manifestPath": str(manifest_path),
            "hashesComputed": len(staged_files),
        },
        "durationSeconds": round(time.monotonic() - started, 3),
    }
    with receipt_path.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(receipt, stream, ensure_ascii=True, indent=2, sort_keys=True)
        stream.write("\n")
    receipt["receiptPath"] = str(receipt_path)
    return receipt


def verify_snapshot_receipt(
    receipt_path: str | os.PathLike[str],
    *,
    staged_root: str | os.PathLike[str] | None = None,
    verify_hashes: bool = True,
) -> dict[str, Any]:
    """Re-check a snapshot receipt against the staged tree it describes.

    ``verify_hashes=False`` performs the cheap structural check (schema, proof
    flags, file inventory, sizes, recomputed identity digest) without re-reading
    tens of gigabytes -- enough to confirm an archive's provenance later without
    unpacking every file.
    """

    path = Path(receipt_path).expanduser().resolve(strict=True)
    receipt = json.loads(path.read_text(encoding="utf-8"))
    problems: list[str] = []

    if receipt.get("schema") != SNAPSHOT_RECEIPT_SCHEMA:
        problems.append(f"unexpected-schema:{receipt.get('schema')}")
    if receipt.get("status") != "complete":
        problems.append(f"snapshot-not-complete:{receipt.get('status')}")
    for flag in ("leaseProven", "snapshotConsistencyProven", "contentIdentityProven"):
        if not receipt.get(flag):
            problems.append(f"proof-flag-not-set:{flag}")

    identity = receipt.get("contentIdentity") or {}
    files = identity.get("files") or []
    counts = identity.get("counts") or {}
    recomputed = canonical_identity_digest(
        {
            "files": [{"relativePath": f["relativePath"], "sha256": f["sha256"]} for f in files],
            "counts": _identity_comparable(counts),
        }
    )
    if recomputed != receipt.get("contentIdentityDigest"):
        problems.append("content-identity-digest-mismatch")

    root = (
        Path(staged_root).expanduser().resolve()
        if staged_root
        else Path(receipt.get("stagedRoot", ""))
    )
    checked = 0
    if root and root.is_dir():
        present = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
        expected = {f["relativePath"] for f in files}
        for missing in sorted(expected - present):
            problems.append(f"staged-file-missing:{missing}")
        for extra in sorted(present - expected):
            problems.append(f"staged-file-unexpected:{extra}")
        for entry in files:
            candidate = root / entry["relativePath"]
            if not candidate.is_file():
                continue
            if candidate.stat().st_size != entry["bytes"]:
                problems.append(f"staged-file-size-mismatch:{entry['relativePath']}")
                continue
            if verify_hashes:
                if sha256_file(candidate) != entry["sha256"]:
                    problems.append(f"staged-file-hash-mismatch:{entry['relativePath']}")
                checked += 1
    else:
        problems.append("staged-root-unavailable")

    return {
        "schema": "mempalace-backup-snapshot-verification/v1",
        "generatedAt": _utc_now(),
        "receiptPath": str(path),
        "stagedRoot": str(root),
        "hashesVerified": checked,
        "hashVerificationRequested": verify_hashes,
        "fileCount": len(files),
        "valid": not problems,
        "problems": problems,
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _print(payload: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True))
        return
    if payload.get("schema") == SNAPSHOT_RECEIPT_SCHEMA:
        status = payload.get("status")
        if status == SNAPSHOT_STATUS_AWAITING_VERIFICATION:
            print("MemPalace staged backup snapshot: copied, awaiting verification")
            print(f"  Staged root:    {payload.get('stagedRoot')}")
            print(f"  Receipt:        {payload.get('receiptPath')}")
            print(f"  Manifest:       {payload.get('manifestPath')}")
            print(f"  Duration:       {payload.get('durationSeconds')}s")
            return
        if status == "error" and payload.get("verification"):
            verification = payload.get("verification") or {}
            print("MemPalace staged backup snapshot verification: FAILED")
            print(f"  Staged root:    {payload.get('stagedRoot')}")
            print(f"  Message:        {verification.get('message')}")
            return
        identity = payload.get("contentIdentity") or {}
        counts = identity.get("counts", {})
        print("MemPalace staged backup snapshot: complete")
        print(f"  Staged root:    {payload.get('stagedRoot')}")
        print(f"  Receipt:        {payload.get('receiptPath')}")
        print(f"  Files staged:   {identity.get('fileCount')}")
        print(f"  Bytes staged:   {identity.get('totalBytes'):,}")
        for label, entry in sorted(counts.items()):
            print(f"  {label:<14} sqlite={entry.get('sqliteCount')} hnsw={entry.get('hnswCount')}")
        print(f"  Identity:       {payload.get('contentIdentityDigest')}")
        print(f"  Duration:       {payload.get('durationSeconds')}s")
        return
    print(f"MemPalace snapshot verification: valid={payload.get('valid')}")
    print(f"  Files:    {payload.get('fileCount')}  hashed={payload.get('hashesVerified')}")
    for problem in payload.get("problems", []):
        print(f"  PROBLEM   {problem}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mempalace backup-snapshot")
    parser.add_argument("--palace-path", default=str(Path.home() / ".mempalace" / "palace"))
    parser.add_argument("--staging-dir", default="")
    parser.add_argument("--verify-receipt", default="", help="Verify an existing receipt instead")
    parser.add_argument(
        "--staged-root", default="", help="Override staged root during verification"
    )
    parser.add_argument(
        "--verify-staged-dir",
        default="",
        help=(
            "Finish a deferred verification (integrity_check + first hashing) for a "
            "generation staged earlier with --defer-verification"
        ),
    )
    parser.add_argument("--skip-hash-verification", action="store_true")
    parser.add_argument("--no-maintenance-marker", action="store_true")
    parser.add_argument("--progress", action="store_true")
    parser.add_argument(
        "--defer-verification",
        action="store_true",
        help=(
            "Copy the palace only; skip PRAGMA integrity_check and content-identity "
            "hashing so a separately scheduled, separately timed lane can run "
            "--verify-staged-dir against this generation afterward"
        ),
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        if args.verify_receipt:
            result = verify_snapshot_receipt(
                args.verify_receipt,
                staged_root=args.staged_root or None,
                verify_hashes=not args.skip_hash_verification,
            )
            _print(result, args.json)
            return 0 if result["valid"] else 2
        if args.verify_staged_dir:
            receipt = verify_staged_snapshot(args.verify_staged_dir)
            _print(receipt, args.json)
            return 0 if receipt.get("status") == "complete" else 2
        if not args.staging_dir:
            raise PalaceSnapshotError("--staging-dir is required when creating a snapshot")
        receipt = stage_palace_snapshot(
            args.palace_path,
            args.staging_dir,
            use_maintenance_marker=not args.no_maintenance_marker,
            progress=args.progress,
            verify=not args.defer_verification,
        )
        _print(receipt, args.json)
        return 0
    except (PalaceSnapshotError, OSError, ValueError) as exc:
        failure = {
            "schema": SNAPSHOT_RECEIPT_SCHEMA,
            "status": "error",
            "generatedAt": _utc_now(),
            "leaseProven": False,
            "snapshotConsistencyProven": False,
            "contentIdentityProven": False,
            "message": str(exc),
        }
        if args.json:
            print(json.dumps(failure, ensure_ascii=True, separators=(",", ":"), sort_keys=True))
        else:
            print(f"MemPalace snapshot failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
