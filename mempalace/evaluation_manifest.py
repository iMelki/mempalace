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
import json
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
from .repair import COLLECTION_NAME, iter_drawers_from_sqlite
from .write_receipts import _package_source_digest


INVENTORY_SCHEMA = "mempalace-evaluation-logical-inventory/v1"
ATTESTATION_SCHEMA = "mempalace-evaluation-corpus-attestation/v1"
SNAPSHOT_METHOD = "sqlite-online-backup/v1"


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


def _logical_inventory(snapshot: Path, *, collection_name: str, batch_size: int) -> dict[str, object]:
    rows: list[dict[str, str]] = []
    for batch in iter_drawers_from_sqlite(
        str(snapshot), collection_name=collection_name, batch_size=batch_size
    ):
        for record in batch:
            row = _canonical_row(record)
            rows.append({"id": str(row["id"]), "rowSha256": sha256_identity(row)})
    rows.sort(key=lambda value: value["id"])
    if len({value["id"] for value in rows}) != len(rows):
        raise EvaluationCorpusManifestError("MemPalace inventory contains duplicate drawer ids")
    return {
        "schema": INVENTORY_SCHEMA,
        "backend": "chroma-sqlite",
        "collection": collection_name,
        "eligibility": "metadata-segment with non-empty chroma:document",
        "rows": rows,
    }


def build_evaluation_corpus_manifest(
    palace_path: Path,
    *,
    data_plane_id: str,
    collection_name: str = COLLECTION_NAME,
    batch_size: int = 1000,
    captured_at_utc: str | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    """Return a strict startup manifest and a provenance attestation.

    The attestation is intentionally separate from the strict startup
    contract, so the HTTP endpoint never has to expose paths or source rows.
    """

    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    if not isinstance(collection_name, str) or not collection_name:
        raise ValueError("collection_name must be non-empty")
    captured = captured_at_utc or _utc_now()
    source = palace_path.expanduser().resolve() / "chroma.sqlite3"
    with tempfile.TemporaryDirectory(prefix="mempalace-evaluation-manifest-") as directory:
        snapshot = Path(directory) / "chroma.sqlite3"
        _snapshot_sqlite(source, snapshot)
        inventory = _logical_inventory(snapshot, collection_name=collection_name, batch_size=batch_size)
        snapshot_sha256 = _sha256_file(snapshot)

    scope = {
        "schema": INVENTORY_SCHEMA,
        "backend": inventory["backend"],
        "collection": inventory["collection"],
        "eligibility": inventory["eligibility"],
        "snapshotMethod": SNAPSHOT_METHOD,
    }
    source_revision = _package_source_digest(Path(__file__).resolve().parent)
    inventory_sha256 = sha256_identity(inventory)
    scope_sha256 = sha256_identity(scope)
    material = {
        "schema": EVALUATION_CORPUS_MANIFEST_SCHEMA,
        "dataPlaneId": data_plane_id,
        "inventorySha256": inventory_sha256,
        "scopeSha256": scope_sha256,
        "sourceRevision": source_revision,
        "itemCount": len(inventory["rows"]),
    }
    manifest: dict[str, object] = {
        **material,
        "capturedAtUtc": captured,
        "corpusRevision": sha256_identity(material),
    }
    # Keep the producer and service-side validation identical.
    validate_evaluation_corpus_manifest(manifest, expected_data_plane_id=data_plane_id)
    attestation: dict[str, object] = {
        "schema": ATTESTATION_SCHEMA,
        "status": "complete",
        "capturedAtUtc": captured,
        "manifestSha256": sha256_identity(manifest),
        "inventorySha256": inventory_sha256,
        "sourceSnapshotSha256": snapshot_sha256,
        "snapshotMethod": SNAPSHOT_METHOD,
        "sourceRevision": source_revision,
        "itemCount": len(inventory["rows"]),
    }
    return manifest, attestation


def _write_new_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=True, indent=2, sort_keys=True)
            stream.write("\n")
    except FileExistsError as exc:
        raise EvaluationCorpusManifestError(f"refusing to overwrite {path.name}") from exc


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a MemPalace evaluation corpus manifest")
    parser.add_argument("--palace", required=True, metavar="PATH")
    parser.add_argument("--data-plane-id", required=True, metavar="SHA256")
    parser.add_argument("--manifest-out", required=True, metavar="PATH")
    parser.add_argument("--attestation-out", required=True, metavar="PATH")
    parser.add_argument("--collection", default=COLLECTION_NAME)
    parser.add_argument("--batch-size", type=int, default=1000)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    if args.batch_size < 1:
        raise SystemExit("--batch-size must be positive")
    manifest_path = Path(args.manifest_out).expanduser().resolve()
    attestation_path = Path(args.attestation_out).expanduser().resolve()
    if manifest_path == attestation_path:
        raise SystemExit("--manifest-out and --attestation-out must differ")
    try:
        manifest, attestation = build_evaluation_corpus_manifest(
            Path(args.palace),
            data_plane_id=args.data_plane_id,
            collection_name=args.collection,
            batch_size=args.batch_size,
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
