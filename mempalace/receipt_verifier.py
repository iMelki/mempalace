"""Read-only verification for ``mempalace-source-write-receipt/v1``."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Union

from .write_receipts import (
    COMPLETE_PUBLICATION_SCHEMA,
    META_OUTPUT_CONTENT_HASH,
    META_RECEIPT_ID,
    META_SOURCE_CONTENT_HASH,
    META_SOURCE_IDENTITY,
    META_SOURCE_VERSION_HASH,
    RECEIPT_SCHEMA,
    RECEIPT_STATES,
    ReceiptIdentityError,
    ReceiptStore,
    manifest_digest,
    receipt_event_id,
    sha256_bytes,
    source_size_bucket,
    validate_receipt_relations,
)

_VERIFY_BATCH_SIZE = 1000


class ReceiptVerificationError(RuntimeError):
    """Raised when current-store verification cannot be completed safely."""


@dataclass(frozen=True)
class VerificationResult:
    """Exact local verification outcomes for one COMPLETE receipt."""

    receipt_id: str
    status: str
    represented: tuple[str, ...]
    missing: tuple[str, ...]
    excess: tuple[str, ...]
    conflict: tuple[str, ...]
    stale: tuple[str, ...]

    @property
    def outcomes(self) -> dict[str, int]:
        return {
            "represented": len(self.represented),
            "missing": len(self.missing),
            "excess": len(self.excess),
            "conflict": len(self.conflict),
            "stale": len(self.stale),
        }

    def as_dict(self, *, include_identities: bool = True) -> dict:
        value = {
            "receipt_id": self.receipt_id,
            "status": self.status,
            "outcomes": self.outcomes,
        }
        if include_identities:
            value.update(
                {
                    "represented": list(self.represented),
                    "missing": list(self.missing),
                    "excess": list(self.excess),
                    "conflict": list(self.conflict),
                    "stale": list(self.stale),
                }
            )
        return value


def verify_receipt(
    receipt: Union[Mapping[str, Any], str, Path],
    drawer_collection: Optional[Any] = None,
    *,
    collections: Optional[Mapping[str, Any]] = None,
    current_source_content_hash: Optional[str] = None,
    store: Optional[ReceiptStore] = None,
) -> VerificationResult:
    """Compare exact receipt identities with current collection contents.

    Missing identity fields, shared projections, non-COMPLETE events, and
    unreadable collections raise instead of producing a false represented
    result. The function never mutates a collection or receipt journal.
    """
    event = load_and_validate_receipt(receipt, require_complete=True)
    resolved_collections = dict(collections or {})
    if drawer_collection is not None:
        resolved_collections.setdefault("drawers", drawer_collection)
    if not resolved_collections:
        raise ReceiptVerificationError("at least one current collection is required")

    expected = event["outputs"]["identities"]
    source = event["source"]
    represented: list[str] = []
    missing: list[str] = []
    excess: list[str] = []
    conflict: list[str] = []
    stale: list[str] = []

    expected_by_collection: dict[str, list[dict]] = {}
    for item in expected:
        expected_by_collection.setdefault(item["collection"], []).append(item)

    for collection_name, items in expected_by_collection.items():
        collection = resolved_collections.get(collection_name)
        if collection is None:
            conflict.extend(_qualified(collection_name, item["id"]) for item in items)
            continue
        current = _rows_for_ids(collection, [item["id"] for item in items])
        for item in items:
            qualified = _qualified(collection_name, item["id"])
            row = current.get(item["id"])
            if row is None:
                missing.append(qualified)
                continue
            document, metadata = row
            if _row_conflicts(item, document, metadata, source):
                conflict.append(qualified)
            else:
                represented.append(qualified)

    # Scan every supplied managed collection, including collections with no
    # expected outputs. Otherwise a ZERO_OUTPUT receipt could look represented
    # while stale stamped closets or adapter rows still survive elsewhere.
    collections_to_scan = set(resolved_collections)
    for collection_name in sorted(collections_to_scan):
        collection = resolved_collections.get(collection_name)
        if collection is None:
            continue
        expected_ids = {item["id"] for item in expected_by_collection.get(collection_name, [])}
        current_rows = _scan_rows(
            collection,
            where={META_SOURCE_IDENTITY: source["identity"]},
        )
        for item_id, (_, metadata) in current_rows.items():
            row_version = metadata.get(META_SOURCE_VERSION_HASH)
            if row_version == source["version_hash"] and item_id not in expected_ids:
                excess.append(_qualified(collection_name, item_id))
            elif row_version and row_version != source["version_hash"]:
                stale.append(event["receipt_id"])

    if current_source_content_hash is not None:
        _require_sha256(current_source_content_hash, "current source content hash")
        if current_source_content_hash != source["content_hash"]:
            stale.append(event["receipt_id"])

    if store is not None:
        current = store.find_current_read_only(source["identity"])
        is_current = current is not None and current["receipt_id"] == event["receipt_id"]
        if not is_current and store.invalidations_for(event["receipt_id"]):
            stale.append(event["receipt_id"])
        if current is not None and not is_current:
            current_source = current.get("source", {})
            if (
                current_source.get("content_hash") != source["content_hash"]
                or current_source.get("version_hash") != source["version_hash"]
                or current.get("relations", {}).get("supersedes", {}).get("receipt_id")
                == event["receipt_id"]
            ):
                stale.append(event["receipt_id"])

    represented = sorted(set(represented))
    missing = sorted(set(missing))
    excess = sorted(set(excess))
    conflict = sorted(set(conflict))
    stale = sorted(set(stale))
    status = _overall_status(missing, excess, conflict, stale)
    return VerificationResult(
        receipt_id=event["receipt_id"],
        status=status,
        represented=tuple(represented),
        missing=tuple(missing),
        excess=tuple(excess),
        conflict=tuple(conflict),
        stale=tuple(stale),
    )


def load_and_validate_receipt(
    receipt: Union[Mapping[str, Any], str, Path],
    *,
    require_complete: bool = False,
) -> dict:
    """Load a receipt and fail closed on absent provenance identity."""
    if isinstance(receipt, Mapping):
        event = dict(receipt)
    else:
        path = Path(receipt)
        try:
            event = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ReceiptIdentityError("receipt is unreadable") from exc
        if not isinstance(event, dict):
            raise ReceiptIdentityError("receipt must be a JSON object")

    if event.get("schema") != RECEIPT_SCHEMA:
        raise ReceiptIdentityError("unsupported or missing receipt schema")
    receipt_id = _require_uuid(event.get("receipt_id"), "receipt id")
    event_id = _require_uuid(event.get("event_id"), "event id")
    sequence = event.get("sequence")
    if not isinstance(sequence, int) or sequence < 0:
        raise ReceiptIdentityError("receipt sequence is required")
    if event_id != receipt_event_id(receipt_id, sequence):
        raise ReceiptIdentityError("event id is not bound to its receipt sequence")
    state = event.get("state")
    if state not in RECEIPT_STATES:
        raise ReceiptIdentityError("receipt state is missing or invalid")
    if require_complete and state != "COMPLETE":
        raise ReceiptIdentityError("only COMPLETE receipts assert a complete output manifest")
    _require_text(event.get("event_time"), "event time")
    _require_text(event.get("stage"), "receipt stage")
    _require_text(event.get("disposition"), "receipt disposition")

    run = _require_mapping(event.get("run"), "run")
    _require_uuid(run.get("id"), "run id")
    _require_hmac(
        _require_mapping(run.get("caller"), "run caller").get("identity"),
        "caller identity",
    )
    _require_text(run.get("mode"), "run mode")

    producer = _require_mapping(event.get("producer"), "producer")
    package = _require_mapping(producer.get("package"), "package identity")
    _require_text(package.get("name"), "package name")
    _require_text(package.get("version"), "package version")
    _require_sha256(package.get("source_digest"), "package source digest")
    git = _require_mapping(producer.get("git"), "git identity")
    git_state = _require_text(git.get("state"), "git state")
    if git_state not in {"available", "build-metadata", "unavailable"}:
        raise ReceiptIdentityError("git state is invalid")
    if git_state == "available":
        commit = _require_text(git.get("commit"), "git commit")
        if len(commit) not in {40, 64} or any(
            character not in "0123456789abcdef" for character in commit.lower()
        ):
            raise ReceiptIdentityError("git commit is invalid")
    elif git_state == "build-metadata":
        _require_text(git.get("commit"), "git commit")
    config = _require_mapping(producer.get("config"), "config identity")
    _require_sha256(config.get("digest"), "config digest")

    _validate_source_identity(event.get("source"))

    outputs = _require_mapping(event.get("outputs"), "output manifest")
    identities = outputs.get("identities")
    if not isinstance(identities, list):
        raise ReceiptIdentityError("exact output identities are required for verification")
    _validate_output_identities(
        identities,
        producer_receipt_id=_terminal_producer_receipt_id(state, receipt_id),
    )
    count = outputs.get("count")
    if not isinstance(count, int) or count != len(identities):
        raise ReceiptIdentityError("output manifest count does not match identities")
    digest = outputs.get("manifest_digest")
    _require_sha256(digest, "output manifest digest")
    if manifest_digest(identities) != digest:
        raise ReceiptIdentityError("output manifest digest does not match identities")

    counts = _require_mapping(event.get("counts"), "receipt counts")
    for field in (
        "source_bytes",
        "items_expected",
        "items_written",
        "items_unchanged",
        "items_invalidated",
        "drawers_expected",
        "drawers_written",
        "drawers_unchanged",
        "sentinels_written",
        "batches",
        "errors",
    ):
        value = counts.get(field)
        if not isinstance(value, int) or value < 0:
            raise ReceiptIdentityError(f"receipt count {field!r} is required")
    _validate_errors(event.get("errors"), expected_count=counts["errors"])
    if state == "COMPLETE":
        _validate_complete_receipt(event, counts, identities, count)
    validate_receipt_relations(event)
    return event


def _validate_output_identities(
    identities: list[dict],
    *,
    producer_receipt_id: Optional[str] = None,
) -> None:
    seen = set()
    for item in identities:
        if not isinstance(item, Mapping):
            raise ReceiptIdentityError("output identities must be objects")
        collection = _require_text(item.get("collection"), "output collection")
        item_id = _require_text(item.get("id"), "output id")
        _require_text(item.get("kind"), "output kind")
        _require_sha256(item.get("content_hash"), "output content hash")
        producer = _require_uuid(item.get("producer_receipt_id"), "producer receipt id")
        if producer_receipt_id is not None and producer != producer_receipt_id:
            raise ReceiptIdentityError("COMPLETE manifest contains a foreign producer receipt")
        key = (collection, item_id)
        if key in seen:
            raise ReceiptIdentityError("output manifest contains duplicate identities")
        seen.add(key)


def _terminal_producer_receipt_id(state: str, receipt_id: str) -> Optional[str]:
    """Bind terminal manifests without adding policy branches to the loader."""
    return receipt_id if state == "COMPLETE" else None


def _validate_complete_receipt(
    event: Mapping[str, Any],
    counts: Mapping[str, Any],
    identities: list[dict],
    count: int,
) -> None:
    publication = _require_mapping(event.get("publication"), "COMPLETE publication")
    if publication.get("schema") != COMPLETE_PUBLICATION_SCHEMA:
        raise ReceiptIdentityError("COMPLETE durable publication schema is invalid")
    if publication.get("policy") != "durable-file-and-parent-proof-required":
        raise ReceiptIdentityError("COMPLETE durable publication policy is invalid")
    if counts["items_written"] + counts["items_unchanged"] != count:
        raise ReceiptIdentityError("complete item counts do not match the manifest")
    if counts["items_expected"] != count:
        raise ReceiptIdentityError("expected item count does not match the manifest")
    drawer_count = sum(1 for item in identities if item["kind"] == "drawer")
    if counts["drawers_written"] + counts["drawers_unchanged"] != drawer_count:
        raise ReceiptIdentityError("complete drawer counts do not match the manifest")
    if counts["drawers_expected"] != drawer_count:
        raise ReceiptIdentityError("expected drawer count does not match the manifest")


def _validate_source_identity(value: Any) -> None:
    source = _require_mapping(value, "source identity")
    _require_hmac(source.get("identity"), "source identity")
    _require_sha256(source.get("content_hash"), "source content hash")
    _require_sha256(source.get("version_hash"), "source version hash")
    _require_hmac(source.get("shared_content_identity"), "shared source content identity")
    _require_hmac(source.get("shared_version_identity"), "shared source version identity")
    size = source.get("size_bytes")
    if not isinstance(size, int) or size < 0:
        raise ReceiptIdentityError("source size is required")
    if source.get("size_bucket") != source_size_bucket(size):
        raise ReceiptIdentityError("source size bucket does not match exact source size")
    adapter = _require_mapping(source.get("adapter"), "adapter identity")
    _require_text(adapter.get("name"), "adapter name")
    _require_text(adapter.get("version"), "adapter version")


def _validate_errors(value: Any, *, expected_count: int) -> None:
    if not isinstance(value, list) or expected_count != len(value):
        raise ReceiptIdentityError("error count does not match error records")
    for error in value:
        error = _require_mapping(error, "receipt error")
        _require_text(error.get("type"), "error type")
        _require_text(error.get("stage"), "error stage")
        _require_sha256(error.get("message_digest"), "error message digest")
        _require_hmac(error.get("shared_message_identity"), "shared error message identity")


def _collection_get(collection: Any, **kwargs: Any) -> Any:
    try:
        return collection.get(**kwargs)
    except Exception as exc:
        raise ReceiptVerificationError("current collection could not be read") from exc


def _rows_for_ids(collection: Any, ids: list[str]) -> dict[str, tuple[str, dict]]:
    rows = {}
    for start in range(0, len(ids), _VERIFY_BATCH_SIZE):
        result = _collection_get(
            collection,
            ids=ids[start : start + _VERIFY_BATCH_SIZE],
            include=["documents", "metadatas"],
        )
        rows.update(_rows_by_id(result))
    return rows


def _scan_rows(collection: Any, *, where: dict) -> dict[str, tuple[str, dict]]:
    rows = {}
    offset = 0
    while True:
        result = _collection_get(
            collection,
            where=where,
            limit=_VERIFY_BATCH_SIZE,
            offset=offset,
            include=["documents", "metadatas"],
        )
        page = _rows_by_id(result)
        if not page:
            break
        previous_size = len(rows)
        rows.update(page)
        if len(rows) == previous_size:
            raise ReceiptVerificationError("collection pagination did not advance")
        offset += len(page)
        if len(page) < _VERIFY_BATCH_SIZE:
            break
    return rows


def _rows_by_id(result: Any) -> dict[str, tuple[str, dict]]:
    try:
        ids = list(result.get("ids") or [])
        documents = list(result.get("documents") or [])
        metadatas = list(result.get("metadatas") or [])
    except (AttributeError, TypeError, ValueError) as exc:
        raise ReceiptVerificationError("collection returned an invalid result shape") from exc
    documents += [""] * max(0, len(ids) - len(documents))
    metadatas += [{}] * max(0, len(ids) - len(metadatas))
    return {
        item_id: (documents[index], metadatas[index] or {}) for index, item_id in enumerate(ids)
    }


def _row_conflicts(item: dict, document: str, metadata: dict, source: dict) -> bool:
    if sha256_bytes(document.encode("utf-8")) != item["content_hash"]:
        return True
    required = {
        META_RECEIPT_ID: item["producer_receipt_id"],
        META_SOURCE_IDENTITY: source["identity"],
        META_SOURCE_CONTENT_HASH: source["content_hash"],
        META_SOURCE_VERSION_HASH: source["version_hash"],
        META_OUTPUT_CONTENT_HASH: item["content_hash"],
    }
    return any(metadata.get(key) != value for key, value in required.items())


def _overall_status(missing: list, excess: list, conflict: list, stale: list) -> str:
    if conflict:
        return "conflict"
    if missing:
        return "missing"
    if excess:
        return "excess"
    if stale:
        return "stale"
    return "represented"


def _qualified(collection: str, item_id: str) -> str:
    return f"{collection}:{item_id}"


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ReceiptIdentityError(f"{label} object is required")
    return value


def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReceiptIdentityError(f"{label} is required")
    return value


def _require_uuid(value: Any, label: str) -> str:
    text = _require_text(value, label)
    try:
        parsed = uuid.UUID(text)
    except ValueError as exc:
        raise ReceiptIdentityError(f"{label} must be a UUID") from exc
    if str(parsed) != text.lower():
        raise ReceiptIdentityError(f"{label} must be a canonical UUID")
    return text


def _require_sha256(value: Any, label: str) -> str:
    return _require_digest(value, label, prefix="sha256")


def _require_hmac(value: Any, label: str) -> str:
    return _require_digest(value, label, prefix="hmac-sha256")


def _require_digest(value: Any, label: str, *, prefix: str) -> str:
    if not isinstance(value, str):
        raise ReceiptIdentityError(f"{label} is required")
    actual_prefix, separator, digest = value.partition(":")
    if separator != ":" or actual_prefix != prefix:
        raise ReceiptIdentityError(f"{label} must use {prefix}")
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ReceiptIdentityError(f"{label} must use {prefix}")
    return value


__all__ = [
    "ReceiptVerificationError",
    "VerificationResult",
    "load_and_validate_receipt",
    "verify_receipt",
]
