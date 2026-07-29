"""Build a secret-free immutable corpus manifest for MemSys evaluation.

The MemPalace HTTP identity endpoint consumes a manifest at process startup.
This module is the matching *producer*: it takes a consistent SQLite online
backup, derives a canonical inventory of the retrievable drawer rows, and
emits a strict manifest plus a separate attestation.  It never changes the
source palace, opens Chroma/HNSW, or exposes drawer content in its output.
"""

from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import os
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from .evaluation_identity import (
    EVALUATION_CORPUS_MANIFEST_SCHEMA,
    EvaluationCorpusManifestError,
    sha256_identity,
    validate_evaluation_corpus_manifest,
)
from .repair import COLLECTION_NAME
from .write_receipts import _package_source_digest


INVENTORY_SCHEMA = "mempalace-evaluation-logical-inventory/v1"
ATTESTATION_SCHEMA = "mempalace-evaluation-corpus-attestation/v1"
SNAPSHOT_METHOD = "sqlite-online-backup/v1"
SNAPSHOT_RECEIPT_SCHEMA = "mempalace-evaluation-snapshot-receipt/v1"
SCAN_CHECKPOINT_SCHEMA = "mempalace-evaluation-inventory-checkpoint/v1"
SHARD_SCHEMA = "mempalace-evaluation-inventory-shard/v1"


def _canonical_bytes(value: object) -> bytes:
    """Return the exact bytes used by :func:`sha256_identity`."""

    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


def _snapshot_sqlite(source: Path, destination: Path) -> None:
    """Use SQLite's backup API; byte-copying a WAL database is not a snapshot."""

    if not source.is_file():
        raise EvaluationCorpusManifestError("MemPalace chroma.sqlite3 is unavailable")
    try:
        reader = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
        try:
            writer = sqlite3.connect(destination)
            try:
                reader.backup(writer)
            finally:
                writer.close()
        finally:
            reader.close()
    except sqlite3.Error as exc:
        raise EvaluationCorpusManifestError("MemPalace SQLite snapshot failed") from exc


def _sqlite_integrity_check(snapshot: Path) -> None:
    try:
        connection = sqlite3.connect(f"file:{snapshot.as_posix()}?mode=ro", uri=True)
        try:
            result = connection.execute("PRAGMA integrity_check").fetchone()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise EvaluationCorpusManifestError("MemPalace SQLite snapshot integrity check failed") from exc
    if result != ("ok",):
        raise EvaluationCorpusManifestError("MemPalace SQLite snapshot integrity check did not return ok")


def _canonical_row(record: Mapping[str, Any]) -> dict[str, object]:
    identifier = record.get("id")
    document = record.get("document")
    metadata = record.get("metadata")
    if not isinstance(identifier, str) or not identifier:
        raise EvaluationCorpusManifestError("MemPalace inventory contains an invalid drawer id")
    if not isinstance(document, str) or not document:
        raise EvaluationCorpusManifestError("MemPalace inventory contains an invalid drawer document")
    if not isinstance(metadata, Mapping):
        raise EvaluationCorpusManifestError("MemPalace inventory contains invalid drawer metadata")
    try:
        # This both verifies JSON-safe metadata and normalizes mapping order.
        normalized_metadata = json.loads(json.dumps(metadata, ensure_ascii=True, sort_keys=True))
    except (TypeError, ValueError) as exc:
        raise EvaluationCorpusManifestError("MemPalace inventory metadata is not JSON-safe") from exc
    return {"id": identifier, "document": document, "metadata": normalized_metadata}


def _write_new_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=True, indent=2, sort_keys=True)
            stream.write("\n")
    except FileExistsError as exc:
        raise EvaluationCorpusManifestError(f"refusing to overwrite {path.name}") from exc


def _read_json(path: Path, *, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvaluationCorpusManifestError(f"{label} is unreadable") from exc
    if not isinstance(value, dict):
        raise EvaluationCorpusManifestError(f"{label} must be an object")
    return value


def _snapshot_paths(staging_dir: Path) -> tuple[Path, Path, Path]:
    return (
        staging_dir / "chroma.sqlite3",
        staging_dir / "snapshot-receipt.json",
        staging_dir / "inventory-checkpoint.json",
    )


def _stage_identity(
    *, snapshot_sha256: str, data_plane_id: str, collection_name: str, source_revision: str
) -> dict[str, object]:
    return {
        "snapshotSha256": snapshot_sha256,
        "dataPlaneId": data_plane_id,
        "collection": collection_name,
        "sourceRevision": source_revision,
    }


def prepare_evaluation_snapshot(
    palace_path: Path,
    *,
    staging_dir: Path,
    data_plane_id: str,
    collection_name: str = COLLECTION_NAME,
    captured_at_utc: str | None = None,
) -> dict[str, object]:
    """Create one durable, integrity-checked source snapshot.

    A completed staging snapshot is deliberately reusable.  An interrupted
    backup has no receipt and is never eligible for scanning or publication.
    This is a completion boundary, not a resumable SQLite backup protocol.
    """

    staging = staging_dir.expanduser().resolve()
    source = palace_path.expanduser().resolve() / "chroma.sqlite3"
    snapshot, receipt_path, checkpoint_path = _snapshot_paths(staging)
    if receipt_path.exists() or checkpoint_path.exists() or snapshot.exists():
        raise EvaluationCorpusManifestError("evaluation staging directory is not empty")
    if not isinstance(collection_name, str) or not collection_name:
        raise ValueError("collection_name must be non-empty")
    source_revision = _package_source_digest(Path(__file__).resolve().parent)
    staging.mkdir(parents=True, exist_ok=False)
    _snapshot_sqlite(source, snapshot)
    _sqlite_integrity_check(snapshot)
    snapshot_sha256 = _sha256_file(snapshot)
    captured = captured_at_utc or _utc_now()
    identity = _stage_identity(
        snapshot_sha256=snapshot_sha256,
        data_plane_id=data_plane_id,
        collection_name=collection_name,
        source_revision=source_revision,
    )
    receipt: dict[str, object] = {
        "schema": SNAPSHOT_RECEIPT_SCHEMA,
        "status": "complete",
        "capturedAtUtc": captured,
        "snapshotMethod": SNAPSHOT_METHOD,
        **identity,
    }
    checkpoint: dict[str, object] = {
        "schema": SCAN_CHECKPOINT_SCHEMA,
        "status": "scanning",
        "batchSize": None,
        "lastSourceRowId": 0,
        "nextShard": 1,
        "itemCount": 0,
        "previousShardSha256": None,
        **identity,
    }
    _write_new_json(receipt_path, receipt)
    _write_new_json(checkpoint_path, checkpoint)
    return receipt


def _load_stage(
    staging_dir: Path, *, data_plane_id: str, collection_name: str | None = None
) -> tuple[Path, dict[str, object], dict[str, object]]:
    staging = staging_dir.expanduser().resolve()
    snapshot, receipt_path, checkpoint_path = _snapshot_paths(staging)
    receipt = _read_json(receipt_path, label="evaluation snapshot receipt")
    checkpoint = _read_json(checkpoint_path, label="evaluation inventory checkpoint")
    if receipt.get("schema") != SNAPSHOT_RECEIPT_SCHEMA or receipt.get("status") != "complete":
        raise EvaluationCorpusManifestError("evaluation snapshot receipt is not complete")
    if checkpoint.get("schema") != SCAN_CHECKPOINT_SCHEMA:
        raise EvaluationCorpusManifestError("evaluation inventory checkpoint schema is unsupported")
    if not snapshot.is_file() or _sha256_file(snapshot) != receipt.get("snapshotSha256"):
        raise EvaluationCorpusManifestError("evaluation snapshot is missing or does not match its receipt")
    required = ("snapshotSha256", "dataPlaneId", "collection", "sourceRevision")
    if any(receipt.get(key) != checkpoint.get(key) for key in required):
        raise EvaluationCorpusManifestError("evaluation scan checkpoint is not bound to the snapshot receipt")
    if receipt.get("dataPlaneId") != data_plane_id:
        raise EvaluationCorpusManifestError("evaluation snapshot dataPlaneId is unbound")
    if collection_name is not None and receipt.get("collection") != collection_name:
        raise EvaluationCorpusManifestError("evaluation snapshot collection is unbound")
    return snapshot, receipt, checkpoint


def _replace_checkpoint(path: Path, checkpoint: Mapping[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.new")
    if temporary.exists():
        raise EvaluationCorpusManifestError("evaluation checkpoint temporary path already exists")
    _write_new_json(temporary, checkpoint)
    os.replace(temporary, path)


def _iter_drawer_batches_after(
    snapshot: Path, *, collection_name: str, batch_size: int, after_row_id: int
):
    """Return cursor-bearing batches without materializing the full inventory."""

    connection = sqlite3.connect(f"file:{snapshot.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        last_row_id = after_row_id
        while True:
            rows = connection.execute(
                """
                SELECT e.id, e.embedding_id
                FROM embeddings e
                JOIN segments s ON e.segment_id = s.id
                JOIN collections c ON s.collection = c.id
                WHERE c.name = ? AND s.scope = 'METADATA' AND e.id > ?
                ORDER BY e.id ASC LIMIT ?
                """,
                (collection_name, last_row_id, batch_size),
            ).fetchall()
            if not rows:
                return
            row_ids = [int(row["id"]) for row in rows]
            placeholders = ",".join("?" for _ in row_ids)
            metadata_rows = connection.execute(
                f"""
                SELECT id, key, string_value, int_value, float_value, bool_value
                FROM embedding_metadata WHERE id IN ({placeholders}) ORDER BY id ASC, key ASC
                """,
                row_ids,
            ).fetchall()
            records: dict[int, dict[str, Any]] = {
                int(row["id"]): {"id": row["embedding_id"], "document": None, "metadata": {}}
                for row in rows
            }
            for metadata in metadata_rows:
                record = records.get(int(metadata["id"]))
                if record is None:
                    continue
                key = str(metadata["key"])
                if metadata["string_value"] is not None:
                    value: Any = metadata["string_value"]
                elif metadata["int_value"] is not None:
                    value = metadata["int_value"]
                elif metadata["float_value"] is not None:
                    value = metadata["float_value"]
                elif metadata["bool_value"] is not None:
                    value = bool(metadata["bool_value"])
                else:
                    value = None
                if key == "chroma:document":
                    record["document"] = value if isinstance(value, str) else ""
                elif not key.startswith("chroma:") and value is not None:
                    record["metadata"][key] = value
            yield row_ids[-1], [
                record for record in records.values() if isinstance(record.get("document"), str) and record["document"]
            ]
            last_row_id = row_ids[-1]
    finally:
        connection.close()


def _write_shard(staging: Path, *, number: int, rows: list[dict[str, str]], checkpoint: Mapping[str, object]) -> str:
    rows.sort(key=lambda value: value["id"])
    if any(left["id"] == right["id"] for left, right in zip(rows, rows[1:])):
        raise EvaluationCorpusManifestError("MemPalace inventory contains duplicate drawer ids")
    value: dict[str, object] = {
        "schema": SHARD_SCHEMA,
        "number": number,
        "previousShardSha256": checkpoint["previousShardSha256"],
        "rows": rows,
    }
    shard = staging / "shards" / f"shard-{number:06d}.json"
    _write_new_json(shard, value)
    return sha256_identity(value)


def scan_evaluation_snapshot(
    staging_dir: Path,
    *,
    data_plane_id: str,
    collection_name: str = COLLECTION_NAME,
    batch_size: int = 1000,
    shard_batches: int = 25,
    max_batches: int | None = None,
) -> dict[str, object]:
    """Resume an inventory scan from an immutable completed snapshot.

    The staging directory contains private id/hash shards only.  It cannot
    activate a service: a public manifest is unavailable until finalization.
    ``max_batches`` is a test/operations bound which intentionally leaves a
    resumable ``scanning`` checkpoint.
    """

    if batch_size < 1 or shard_batches < 1 or (max_batches is not None and max_batches < 1):
        raise ValueError("batch_size, shard_batches, and max_batches must be positive")
    snapshot, receipt, checkpoint = _load_stage(
        staging_dir, data_plane_id=data_plane_id, collection_name=collection_name
    )
    if checkpoint.get("status") == "complete":
        return checkpoint
    if checkpoint.get("status") != "scanning":
        raise EvaluationCorpusManifestError("evaluation inventory checkpoint status is unsupported")
    processing_source_revision = _package_source_digest(Path(__file__).resolve().parent)
    recorded_processing_revision = checkpoint.get("processingSourceRevision")
    if recorded_processing_revision is None:
        checkpoint = {**checkpoint, "processingSourceRevision": processing_source_revision}
        _replace_checkpoint(staging_dir.expanduser().resolve() / "inventory-checkpoint.json", checkpoint)
    elif recorded_processing_revision != processing_source_revision:
        raise EvaluationCorpusManifestError("evaluation scan processing source revision is stale")
    recorded_batch_size = checkpoint.get("batchSize")
    if recorded_batch_size not in (None, batch_size):
        raise EvaluationCorpusManifestError("evaluation inventory batch size cannot change during resume")
    staging = staging_dir.expanduser().resolve()
    current_rows: list[dict[str, str]] = []
    processed = 0
    last_cursor = int(checkpoint["lastSourceRowId"])
    for cursor, records in _iter_drawer_batches_after(
        snapshot, collection_name=collection_name, batch_size=batch_size, after_row_id=last_cursor
    ):
        current_rows.extend(
            {"id": str(row["id"]), "rowSha256": sha256_identity(_canonical_row(row))}
            for row in records
        )
        processed += 1
        last_cursor = cursor
        if processed % shard_batches != 0 and (max_batches is None or processed < max_batches):
            continue
        if current_rows:
            shard_sha256 = _write_shard(
                staging, number=int(checkpoint["nextShard"]), rows=current_rows, checkpoint=checkpoint
            )
            checkpoint = {**checkpoint, "previousShardSha256": shard_sha256, "nextShard": int(checkpoint["nextShard"]) + 1, "itemCount": int(checkpoint["itemCount"]) + len(current_rows)}
            current_rows = []
        checkpoint = {**checkpoint, "batchSize": batch_size, "lastSourceRowId": last_cursor}
        _replace_checkpoint(staging / "inventory-checkpoint.json", checkpoint)
        if max_batches is not None and processed >= max_batches:
            return checkpoint
    if current_rows:
        shard_sha256 = _write_shard(
            staging, number=int(checkpoint["nextShard"]), rows=current_rows, checkpoint=checkpoint
        )
        checkpoint = {**checkpoint, "previousShardSha256": shard_sha256, "nextShard": int(checkpoint["nextShard"]) + 1, "itemCount": int(checkpoint["itemCount"]) + len(current_rows)}
    checkpoint = {**checkpoint, "batchSize": batch_size, "lastSourceRowId": last_cursor, "status": "complete"}
    _replace_checkpoint(staging / "inventory-checkpoint.json", checkpoint)
    return checkpoint


def _iter_sorted_shard_rows(staging: Path, checkpoint: Mapping[str, object]):
    previous: object = None
    iterators = []
    for number in range(1, int(checkpoint["nextShard"])):
        shard = _read_json(staging / "shards" / f"shard-{number:06d}.json", label="evaluation inventory shard")
        if shard.get("schema") != SHARD_SCHEMA or shard.get("number") != number:
            raise EvaluationCorpusManifestError("evaluation inventory shard is invalid")
        if shard.get("previousShardSha256") != previous:
            raise EvaluationCorpusManifestError("evaluation inventory shard chain is invalid")
        rows = shard.get("rows")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise EvaluationCorpusManifestError("evaluation inventory shard rows are invalid")
        previous = sha256_identity(shard)
        iterators.append(iter(rows))
    if previous != checkpoint.get("previousShardSha256"):
        raise EvaluationCorpusManifestError("evaluation inventory shard chain does not match checkpoint")
    for row in heapq.merge(*iterators, key=lambda value: value["id"]):
        identifier = row.get("id")
        row_sha256 = row.get("rowSha256")
        if not isinstance(identifier, str) or not identifier or not isinstance(row_sha256, str):
            raise EvaluationCorpusManifestError("evaluation inventory shard row is invalid")
        yield {"id": identifier, "rowSha256": row_sha256}


def _stream_inventory_identity(staging: Path, checkpoint: Mapping[str, object], *, collection_name: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    digest.update(_canonical_bytes({"backend": "chroma-sqlite", "collection": collection_name, "eligibility": "metadata-segment with non-empty chroma:document", "rows": []})[:-2])
    # The preceding canonical object ends in ``[]}``; turn the empty array into
    # a streamed array while retaining byte-for-byte compatibility with
    # ``sha256_identity`` over the historical inventory object.
    previous_id: str | None = None
    count = 0
    for row in _iter_sorted_shard_rows(staging, checkpoint):
        if row["id"] == previous_id:
            raise EvaluationCorpusManifestError("MemPalace inventory contains duplicate drawer ids")
        if count:
            digest.update(b",")
        digest.update(_canonical_bytes(row))
        previous_id = row["id"]
        count += 1
    digest.update(b'],"schema":"' + INVENTORY_SCHEMA.encode("ascii") + b'"}')
    if count != checkpoint.get("itemCount"):
        raise EvaluationCorpusManifestError("evaluation inventory item count does not match checkpoint")
    return f"sha256:{digest.hexdigest()}", count


def finalize_evaluation_corpus_manifest(
    staging_dir: Path,
    *,
    data_plane_id: str,
    collection_name: str = COLLECTION_NAME,
    captured_at_utc: str | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    """Build public identity material only from a completed immutable scan."""

    _, snapshot_receipt, checkpoint = _load_stage(
        staging_dir, data_plane_id=data_plane_id, collection_name=collection_name
    )
    if checkpoint.get("status") != "complete":
        raise EvaluationCorpusManifestError("evaluation inventory scan is incomplete")
    processing_source_revision = _package_source_digest(Path(__file__).resolve().parent)
    if checkpoint.get("processingSourceRevision") != processing_source_revision:
        raise EvaluationCorpusManifestError("evaluation finalization processing source revision is stale")
    staging = staging_dir.expanduser().resolve()
    inventory_sha256, item_count = _stream_inventory_identity(
        staging, checkpoint, collection_name=collection_name
    )
    scope = {
        "schema": INVENTORY_SCHEMA,
        "backend": "chroma-sqlite",
        "collection": collection_name,
        "eligibility": "metadata-segment with non-empty chroma:document",
        "snapshotMethod": SNAPSHOT_METHOD,
    }
    source_revision = str(snapshot_receipt["sourceRevision"])
    material = {
        "schema": EVALUATION_CORPUS_MANIFEST_SCHEMA,
        "dataPlaneId": data_plane_id,
        "inventorySha256": inventory_sha256,
        "scopeSha256": sha256_identity(scope),
        "sourceRevision": source_revision,
        "processingSourceRevision": processing_source_revision,
        "itemCount": item_count,
    }
    manifest: dict[str, object] = {
        **material,
        "capturedAtUtc": captured_at_utc or str(snapshot_receipt["capturedAtUtc"]),
        "corpusRevision": sha256_identity(material),
    }
    validate_evaluation_corpus_manifest(manifest, expected_data_plane_id=data_plane_id)
    attestation: dict[str, object] = {
        "schema": ATTESTATION_SCHEMA,
        "status": "complete",
        "capturedAtUtc": manifest["capturedAtUtc"],
        "manifestSha256": sha256_identity(manifest),
        "inventorySha256": inventory_sha256,
        "sourceSnapshotSha256": snapshot_receipt["snapshotSha256"],
        "snapshotMethod": SNAPSHOT_METHOD,
        "sourceRevision": source_revision,
        "processingSourceRevision": processing_source_revision,
        "itemCount": item_count,
    }
    return manifest, attestation


def build_evaluation_corpus_manifest(
    palace_path: Path,
    *,
    data_plane_id: str,
    collection_name: str = COLLECTION_NAME,
    batch_size: int = 1000,
    captured_at_utc: str | None = None,
    staging_dir: Path | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    """Return a strict startup manifest and a provenance attestation.

    The attestation is intentionally separate from the strict startup
    contract, so the HTTP endpoint never has to expose paths or source rows.
    """

    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    if not isinstance(collection_name, str) or not collection_name:
        raise ValueError("collection_name must be non-empty")
    if staging_dir is not None:
        prepare_evaluation_snapshot(
            palace_path,
            staging_dir=staging_dir,
            data_plane_id=data_plane_id,
            collection_name=collection_name,
            captured_at_utc=captured_at_utc,
        )
        scan_evaluation_snapshot(
            staging_dir,
            data_plane_id=data_plane_id,
            collection_name=collection_name,
            batch_size=batch_size,
        )
        return finalize_evaluation_corpus_manifest(
            staging_dir,
            data_plane_id=data_plane_id,
            collection_name=collection_name,
            captured_at_utc=captured_at_utc,
        )
    with tempfile.TemporaryDirectory(prefix="mempalace-evaluation-manifest-") as directory:
        ephemeral_stage = Path(directory) / "stage"
        prepare_evaluation_snapshot(
            palace_path,
            staging_dir=ephemeral_stage,
            data_plane_id=data_plane_id,
            collection_name=collection_name,
            captured_at_utc=captured_at_utc,
        )
        scan_evaluation_snapshot(
            ephemeral_stage,
            data_plane_id=data_plane_id,
            collection_name=collection_name,
            batch_size=batch_size,
        )
        return finalize_evaluation_corpus_manifest(
            ephemeral_stage,
            data_plane_id=data_plane_id,
            collection_name=collection_name,
            captured_at_utc=captured_at_utc,
        )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a MemPalace evaluation corpus manifest")
    parser.add_argument("--palace", required=True, metavar="PATH")
    parser.add_argument("--data-plane-id", required=True, metavar="SHA256")
    parser.add_argument("--manifest-out", metavar="PATH")
    parser.add_argument("--attestation-out", metavar="PATH")
    parser.add_argument("--collection", default=COLLECTION_NAME)
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--staging-dir", metavar="PATH")
    parser.add_argument(
        "--phase", choices=("all", "snapshot", "scan", "finalize"), default="all",
        help="Use snapshot/scan/finalize to resume a durable staging directory.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    if args.batch_size < 1:
        raise SystemExit("--batch-size must be positive")
    if args.phase in ("snapshot", "scan", "finalize") and not args.staging_dir:
        raise SystemExit(f"--phase {args.phase} requires --staging-dir")
    if args.phase in ("all", "finalize") and (not args.manifest_out or not args.attestation_out):
        raise SystemExit("--manifest-out and --attestation-out are required for manifest publication")
    manifest_path = Path(args.manifest_out).expanduser().resolve() if args.manifest_out else None
    attestation_path = Path(args.attestation_out).expanduser().resolve() if args.attestation_out else None
    if manifest_path is not None and manifest_path == attestation_path:
        raise SystemExit("--manifest-out and --attestation-out must differ")
    try:
        staging = Path(args.staging_dir) if args.staging_dir else None
        if args.phase == "snapshot":
            receipt = prepare_evaluation_snapshot(
                Path(args.palace), staging_dir=staging, data_plane_id=args.data_plane_id,
                collection_name=args.collection,
            )
            print(json.dumps({"status": "snapshot-complete", "snapshotSha256": receipt["snapshotSha256"]}, sort_keys=True))
            return
        if args.phase == "scan":
            checkpoint = scan_evaluation_snapshot(
                staging, data_plane_id=args.data_plane_id, collection_name=args.collection, batch_size=args.batch_size
            )
            print(json.dumps({"status": checkpoint["status"], "itemCount": checkpoint["itemCount"]}, sort_keys=True))
            return
        if args.phase == "finalize":
            manifest, attestation = finalize_evaluation_corpus_manifest(
                staging, data_plane_id=args.data_plane_id, collection_name=args.collection
            )
        else:
            manifest, attestation = build_evaluation_corpus_manifest(
                Path(args.palace),
                data_plane_id=args.data_plane_id,
                collection_name=args.collection,
                batch_size=args.batch_size,
                staging_dir=staging,
            )
        # Publish the provenance receipt first.  If the manifest write then
        # fails, the harmless orphaned attestation cannot activate a service;
        # conversely, no active manifest can ever lack its receipt.
        _write_new_json(attestation_path, attestation)
        _write_new_json(manifest_path, manifest)
    except (EvaluationCorpusManifestError, OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(
        json.dumps(
            {
                "status": "complete",
                "manifestSha256": attestation["manifestSha256"],
                "inventorySha256": attestation["inventorySha256"],
                "itemCount": attestation["itemCount"],
            },
            ensure_ascii=True,
            sort_keys=True,
        )
    )


if __name__ == "__main__":  # pragma: no cover
    main()
