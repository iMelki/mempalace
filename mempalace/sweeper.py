#!/usr/bin/env python3
"""
sweeper.py - Message-granular managed ingestion for Claude JSONL files.

Each physical JSONL file owns a separate sweeper materialization lane. The
lane does not claim the physical ``source_file`` identity used by the primary
miners, so message-level safety-net rows can coexist with their chunked rows.
Within the sweeper lane, one exact source version replaces the complete prior
message set under a managed receipt and durable rollback snapshot.

Properties:

  - Exact source identity: SHA-256 of the complete JSONL bytes.
  - Idempotent replay: unchanged bytes reuse the represented output manifest.
  - Complete replacement: removed or changed messages cannot linger silently.
  - Interruption recovery: partial replacement restores predecessor documents,
    metadata, and vectors through the common receipt recovery journal.
  - Source-stability check: a file that changes during extraction fails closed
    and rolls back instead of publishing a mixed-version receipt.
  - Legacy safety: unmanaged rows that collide with deterministic sweeper IDs
    are not silently claimed; they require an explicit provenance migration.

The legacy timestamp cursor helper remains public for compatibility and
readback, but it is no longer deletion/completeness authority.

Usage:
    from mempalace.sweeper import sweep
    result = sweep("/path/to/session.jsonl", "/path/to/palace")
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from .palace import get_collection, mine_lock, mine_palace_lock
from .provenance import managed_adapter_ingest
from .sources.base import AdapterSchema, BaseSourceAdapter, SourceItemMetadata, SourceRef
from .sources.context import PalaceContext
from .write_receipts import (
    META_SOURCE_IDENTITY,
    ReceiptConflictError,
    ReceiptIdentityError,
    ReceiptStore,
    canonical_source_locator,
    managed_write_scope,
)

logger = logging.getLogger(__name__)

_SWEEPER_CONTRACT = "mempalace-sweeper-jsonl-managed-write/v1"
_SWEEPER_ADAPTER_VERSION = "1.0.0"
_SWEEP_BATCH_SIZE = 64
_READ_BATCH_SIZE = 1000
_SWEEPER_SEMANTIC_METADATA_HASH = "sweeper_semantic_metadata_hash"
_SWEEPER_VOLATILE_METADATA = frozenset(
    {
        "source_file",
        "filed_at",
        _SWEEPER_SEMANTIC_METADATA_HASH,
    }
)


# ── JSONL parsing ────────────────────────────────────────────────────


def _flatten_content(content) -> str:
    """Normalize Claude Code's message content to a plain string.

    User messages are strings already; assistant messages are a list of
    content blocks like [{"type": "text", "text": "..."}, {"type":
    "tool_use", ...}]. All blocks are preserved verbatim — the design
    principle is "verbatim always", so tool inputs and results are
    serialized in full, never truncated.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                parts.append(json.dumps(block, ensure_ascii=False, sort_keys=True))
                continue
            btype = block.get("type", "")
            if btype == "text":
                parts.append(block.get("text", ""))
            elif btype == "tool_use":
                parts.append(
                    f"[tool_use: {block.get('name', '?')} "
                    f"input={json.dumps(block.get('input', {}), default=str)}]"
                )
            elif btype == "tool_result":
                parts.append(f"[tool_result: {json.dumps(block.get('content', ''), default=str)}]")
            else:
                parts.append(f"[{btype}: {json.dumps(block, default=str)}]")
        return "\n".join(p for p in parts if p)
    return str(content)


def parse_claude_jsonl(path: str) -> Iterator[dict]:
    """Yield user/assistant records from a Claude Code .jsonl file.

    Each yield is:
        {
          "session_id": str,
          "uuid":       str,   # per-message UUID
          "timestamp":  str,   # ISO 8601
          "role":       "user" | "assistant",
          "content":    str,   # flattened text
        }

    Non-message records (progress, file-history-snapshot, system,
    queue-operation, last-prompt) are filtered out. Malformed lines are
    skipped silently — data quality is the transcript writer's problem,
    not ours.
    """
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            rtype = record.get("type")
            if rtype not in ("user", "assistant"):
                continue
            msg = record.get("message") or {}
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            if role not in ("user", "assistant"):
                continue
            timestamp = record.get("timestamp")
            if not timestamp:
                continue
            uuid = record.get("uuid")
            if not uuid:
                continue
            session_id = record.get("sessionId") or record.get("session_id")
            if not session_id:
                continue
            content = _flatten_content(msg.get("content", ""))
            if not content.strip():
                continue
            yield {
                "session_id": session_id,
                "uuid": uuid,
                "timestamp": timestamp,
                "role": role,
                "content": content,
            }


def _parse_managed_claude_jsonl(path: Path) -> Iterator[dict]:
    """Parse replacement-authoritative input and reject ambiguous message loss."""
    with path.open("rb") as handle:
        for line_number, raw_bytes in enumerate(handle, start=1):
            try:
                raw_line = raw_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ReceiptIdentityError(
                    f"managed sweeper source is not valid UTF-8 at line {line_number}"
                ) from exc
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ReceiptIdentityError(
                    f"managed sweeper source has malformed JSON at line {line_number}"
                ) from exc
            if not isinstance(record, dict):
                raise ReceiptIdentityError(
                    f"managed sweeper source has a non-object record at line {line_number}"
                )
            record_type = record.get("type")
            if record_type not in {"user", "assistant"}:
                continue
            message = record.get("message")
            if not isinstance(message, dict):
                raise ReceiptIdentityError(
                    f"managed sweeper message is missing its object at line {line_number}"
                )
            role = message.get("role")
            timestamp = record.get("timestamp")
            message_uuid = record.get("uuid")
            session_id = record.get("sessionId") or record.get("session_id")
            if role not in {"user", "assistant"}:
                raise ReceiptIdentityError(
                    f"managed sweeper message has an invalid role at line {line_number}"
                )
            if not isinstance(timestamp, str) or not timestamp:
                raise ReceiptIdentityError(
                    f"managed sweeper message has no timestamp at line {line_number}"
                )
            if not isinstance(message_uuid, str) or not message_uuid:
                raise ReceiptIdentityError(
                    f"managed sweeper message has no UUID at line {line_number}"
                )
            if not isinstance(session_id, str) or not session_id:
                raise ReceiptIdentityError(
                    f"managed sweeper message has no session ID at line {line_number}"
                )
            content = _flatten_content(message.get("content", ""))
            if not content.strip():
                raise ReceiptIdentityError(
                    f"managed sweeper message has empty content at line {line_number}"
                )
            yield {
                "session_id": session_id,
                "uuid": message_uuid,
                "timestamp": timestamp,
                "role": role,
                "content": content,
            }


# ── Cursor resolution ────────────────────────────────────────────────


def get_palace_cursor(collection, session_id: str) -> Optional[str]:
    """Return the max timestamp of drawers for this session_id, or None.

    ISO-8601 strings compare lexically in the right order, so we don't
    need to parse them. Query scans metadatas for the session via the
    backend's where-filter, then reduces.

    Backend errors are logged at WARNING and surface as a `None` cursor —
    which makes the caller treat the session as empty and ingest every
    message. That's intentional: a no-cursor sweep is recovered from on
    the next run by deterministic drawer IDs, so a degraded cursor never
    causes silent data loss.
    """
    try:
        data = collection.get(
            where={"session_id": session_id},
            include=["metadatas"],
        )
    except Exception as exc:
        logger.warning(
            "sweeper: cursor lookup failed for session_id=%s (%s); "
            "treating as empty — drawers will be re-upserted idempotently.",
            session_id,
            exc,
        )
        return None
    metas = data.get("metadatas") or []
    timestamps = [m.get("timestamp") for m in metas if m and m.get("timestamp")]
    if not timestamps:
        return None
    return max(timestamps)


# ── Sweep ────────────────────────────────────────────────────────────


def _drawer_id_for_message(
    session_id: str,
    message_uuid: str,
    *,
    source_uri: Optional[str] = None,
) -> str:
    """Return the legacy or path-namespaced deterministic message ID.

    Old direct sweeps used ``sweep_<session>_<uuid>``. Managed sweeps add a
    source namespace so a copied or renamed transcript cannot collide with a
    retained lane from its previous path. Omitting ``source_uri`` intentionally
    returns the legacy ID for migration detection.
    """
    if source_uri is None:
        return f"sweep_{session_id}_{message_uuid}"
    namespace = hashlib.sha256(source_uri.encode("utf-8")).hexdigest()[:16]
    return f"sweep_{namespace}_{session_id}_{message_uuid}"


def _file_fingerprint(path: Path) -> tuple[str, int]:
    """Return an exact tagged digest and byte count without loading the file."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return f"sha256:{digest.hexdigest()}", size


def _safe_source_name(path: Path) -> str:
    safe = "".join(
        character if character.isascii() and (character.isalnum() or character in "._-") else "_"
        for character in path.name
    ).strip("._")
    return (safe or "session.jsonl")[:96]


def _sweeper_source_uri(path: Path) -> str:
    """Give the sweeper its own lane without exposing the full path in receipts."""
    canonical = canonical_source_locator(path, local_path=True)
    token = hashlib.sha256(os.path.normcase(canonical).encode("utf-8")).hexdigest()
    safe_name = _safe_source_name(path)
    if os.name == "nt":
        safe_name = safe_name.lower()
    return f"mempalace://sweeper/jsonl/{token}/{safe_name}"


def _result_rows(result: dict) -> dict[str, tuple[Optional[str], dict]]:
    ids = list(result.get("ids") or [])
    documents = result.get("documents") or [None] * len(ids)
    metadatas = result.get("metadatas") or [{} for _ in ids]
    if len(documents) != len(ids) or len(metadatas) != len(ids):
        raise ReceiptIdentityError("sweeper readback returned misaligned row fields")
    if len(set(ids)) != len(ids):
        raise ReceiptIdentityError("sweeper readback returned duplicate row IDs")
    return {
        item_id: (documents[index], dict(metadatas[index] or {}))
        for index, item_id in enumerate(ids)
    }


def _rows_for_ids(collection, ids: list[str]) -> dict[str, tuple[Optional[str], dict]]:
    rows: dict[str, tuple[Optional[str], dict]] = {}
    for offset in range(0, len(ids), _READ_BATCH_SIZE):
        batch = ids[offset : offset + _READ_BATCH_SIZE]
        result = collection.get(ids=batch, include=["documents", "metadatas"])
        batch_rows = _result_rows(result)
        overlap = set(rows).intersection(batch_rows)
        if overlap:
            raise ReceiptIdentityError("sweeper readback repeated row IDs across batches")
        rows.update(batch_rows)
    return rows


def _rows_for_where(collection, where: dict) -> dict[str, tuple[Optional[str], dict]]:
    rows: dict[str, tuple[Optional[str], dict]] = {}
    offset = 0
    while True:
        result = collection.get(
            where=where,
            include=["metadatas"],
            limit=_READ_BATCH_SIZE,
            offset=offset,
        )
        batch_rows = _result_rows(result)
        overlap = set(rows).intersection(batch_rows)
        if overlap:
            raise ReceiptIdentityError("sweeper filtered readback repeated row IDs")
        rows.update(batch_rows)
        batch_size = len(batch_rows)
        if batch_size < _READ_BATCH_SIZE:
            return rows
        offset += batch_size


def _legacy_source_may_match(raw_source_file: object, input_path: Path) -> bool:
    if not isinstance(raw_source_file, str) or not raw_source_file or "://" in raw_source_file:
        return False
    expanded = os.path.expanduser(raw_source_file)
    target = os.path.normcase(os.path.normpath(str(input_path)))
    if os.path.isabs(expanded):
        try:
            candidate = canonical_source_locator(expanded, local_path=True)
        except (OSError, TypeError, ValueError):
            return False
        return os.path.normcase(os.path.normpath(candidate)) == target

    relative = os.path.normcase(os.path.normpath(expanded))
    if relative in {"", "."}:
        return False
    return target.endswith(os.sep + relative) or os.path.basename(relative) == os.path.basename(
        target
    )


def _legacy_message_ids(path: Path) -> set[str]:
    ids: set[str] = set()
    for record in _parse_managed_claude_jsonl(path):
        item_id = _drawer_id_for_message(record["session_id"], record["uuid"])
        if item_id in ids:
            raise ReceiptIdentityError(
                f"sweeper source contains a duplicate session/message identity: {item_id}"
            )
        ids.add(item_id)
    return ids


def _preflight_legacy_unmanaged(
    collection,
    *,
    input_path: Path,
    legacy_source_files: tuple[str, ...],
    legacy_ids: set[str],
) -> None:
    """Reject known direct-sweeper rows before receipt storage is initialized."""
    candidates: dict[str, tuple[Optional[str], dict]] = {}
    for legacy_source_file in legacy_source_files:
        candidates.update(
            _result_rows(
                collection.get(
                    where={"source_file": legacy_source_file},
                    include=["metadatas"],
                )
            )
        )
    candidates.update(_rows_for_ids(collection, sorted(legacy_ids)))
    candidates.update(_rows_for_where(collection, {"ingest_mode": "sweep"}))
    for item_id, (_, metadata) in candidates.items():
        if (
            metadata.get(META_SOURCE_IDENTITY) is None
            and metadata.get("ingest_mode") == "sweep"
            and (
                item_id in legacy_ids
                or metadata.get("source_file") in legacy_source_files
                or _legacy_source_may_match(metadata.get("source_file"), input_path)
            )
        ):
            raise ReceiptConflictError(
                "legacy unmanaged sweeper rows require an explicit provenance migration "
                f"before managed sweeping (first legacy row: {item_id})"
            )


def _semantic_metadata(metadata: dict) -> dict:
    return {
        str(key): value
        for key, value in metadata.items()
        if not str(key).startswith("write_") and str(key) not in _SWEEPER_VOLATILE_METADATA
    }


def _semantic_row_hash(item_id: str, document: str, metadata: dict) -> str:
    payload = json.dumps(
        {
            "id": item_id,
            "document": document,
            "metadata": _semantic_metadata(metadata),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


class _SweeperJsonlAdapter(BaseSourceAdapter):
    """Materialize one physical JSONL file in an isolated receipt-owned lane."""

    name = "sweeper-jsonl"
    adapter_version = _SWEEPER_ADAPTER_VERSION
    capabilities = frozenset({"supports_incremental"})
    supported_modes = frozenset({"whole_record"})
    declared_transformations = frozenset(
        {"claude-jsonl-filter", "message-content-flatten", "role-prefix"}
    )
    empty_output_disposition = "ZERO_OUTPUT"

    def __init__(
        self,
        *,
        input_path: Path,
        source_uri: str,
        source_identity: str,
        source_label: Optional[str],
        legacy_source_files: tuple[str, ...],
        allow_zero_output: bool,
    ) -> None:
        self.input_path = input_path
        self.origin_source_file = os.path.normcase(str(input_path))
        self.source_uri = source_uri
        self.source_identity = source_identity
        self.source_label = source_label
        self.legacy_source_files = legacy_source_files
        self.allow_zero_output = allow_zero_output
        self.source_content_hash: Optional[str] = None
        self.source_size = 0
        self.generated_ids: set[str] = set()
        self.previous_ids: set[str] = set()
        self.expected_row_hashes: dict[str, str] = {}
        self.cursor_by_session: dict[str, Optional[str]] = {}
        self.semantic_updated_ids: set[str] = set()
        self.semantic_unchanged_ids: set[str] = set()
        self.drawers_written = 0
        self.exact_current = False

    def _record_metadata(self, record: dict, *, filed_at: Optional[str] = None) -> dict:
        metadata = {
            "session_id": record["session_id"],
            "timestamp": record["timestamp"],
            "message_uuid": record["uuid"],
            "role": record["role"],
            "origin_source_file": self.origin_source_file,
            "ingest_mode": "sweep",
            "adapter_name": self.name,
            "adapter_version": self.adapter_version,
        }
        if filed_at is not None:
            metadata["filed_at"] = filed_at
        if self.source_label:
            metadata["source_label"] = self.source_label
        return metadata

    def _scan_source(self) -> tuple[dict[str, str], set[str], set[str]]:
        expected_row_hashes: dict[str, str] = {}
        session_ids: set[str] = set()
        legacy_ids: set[str] = set()
        for record in _parse_managed_claude_jsonl(self.input_path):
            item_id = _drawer_id_for_message(
                record["session_id"],
                record["uuid"],
                source_uri=self.source_uri,
            )
            if item_id in expected_row_hashes:
                raise ReceiptIdentityError(
                    f"sweeper source contains a duplicate session/message identity: {item_id}"
                )
            document = f"{record['role'].upper()}: {record['content']}"
            expected_row_hashes[item_id] = _semantic_row_hash(
                item_id,
                document,
                self._record_metadata(record),
            )
            session_ids.add(record["session_id"])
            legacy_ids.add(_drawer_id_for_message(record["session_id"], record["uuid"]))
        return expected_row_hashes, session_ids, legacy_ids

    def _preflight_existing(
        self,
        collection,
        expected_row_hashes: dict[str, str],
        session_ids: set[str],
        legacy_ids: set[str],
    ) -> None:
        _preflight_legacy_unmanaged(
            collection,
            input_path=self.input_path,
            legacy_source_files=self.legacy_source_files,
            legacy_ids=legacy_ids,
        )

        current = _result_rows(
            collection.get(
                where={"source_file": self.source_uri},
                include=["documents", "metadatas"],
            )
        )
        for item_id, (_, metadata) in current.items():
            if (
                metadata.get(META_SOURCE_IDENTITY) != self.source_identity
                or metadata.get("source_file") != self.source_uri
            ):
                raise ReceiptConflictError(
                    f"managed sweeper lane has contradictory ownership: {item_id}"
                )
        if current and not expected_row_hashes and not self.allow_zero_output:
            raise ReceiptConflictError(
                "managed sweep would remove every represented message; rerun with "
                "allow_zero_output=True only after reviewing the source"
            )

        collisions = _rows_for_ids(collection, sorted(expected_row_hashes))
        for item_id, (_, metadata) in collisions.items():
            if item_id in current:
                continue
            if (
                metadata.get(META_SOURCE_IDENTITY) is None
                and metadata.get("ingest_mode") == "sweep"
            ):
                raise ReceiptConflictError(
                    "legacy unmanaged sweeper rows collide with the managed lane; "
                    "run an explicit provenance migration before sweeping this source "
                    f"(first collision: {item_id})"
                )
            raise ReceiptConflictError(
                f"sweeper output ID is already owned by another source: {item_id}"
            )

        self.previous_ids = set(current)
        overlap = set(current).intersection(expected_row_hashes)
        self.semantic_updated_ids = {
            item_id
            for item_id in overlap
            if not (
                isinstance(current[item_id][0], str)
                and current[item_id][1].get(_SWEEPER_SEMANTIC_METADATA_HASH)
                == expected_row_hashes[item_id]
                and _semantic_row_hash(item_id, current[item_id][0], current[item_id][1])
                == expected_row_hashes[item_id]
            )
        }
        self.semantic_unchanged_ids = overlap - self.semantic_updated_ids
        self.exact_current = (
            set(current) == set(expected_row_hashes) and not self.semantic_updated_ids
        )
        cursors: dict[str, Optional[str]] = {session_id: None for session_id in session_ids}
        for _, metadata in current.values():
            session_id = metadata.get("session_id")
            timestamp = metadata.get("timestamp")
            if not isinstance(session_id, str) or not isinstance(timestamp, str):
                continue
            previous = cursors.get(session_id)
            if previous is None or timestamp > previous:
                cursors[session_id] = timestamp
        self.cursor_by_session = cursors

    def ingest(self, *, source: SourceRef, palace: PalaceContext):
        if source.uri != self.source_uri or source.local_path is not None:
            raise ReceiptIdentityError("sweeper adapter source URI changed before ingestion")

        physical_lock = os.path.normcase(str(self.input_path))
        with mine_lock(physical_lock):
            content_hash, source_size = _file_fingerprint(self.input_path)
            expected_row_hashes, session_ids, legacy_ids = self._scan_source()
            self._preflight_existing(
                palace.drawer_collection,
                expected_row_hashes,
                session_ids,
                legacy_ids,
            )
            self.source_content_hash = content_hash
            self.source_size = source_size
            self.generated_ids = set(expected_row_hashes)
            self.expected_row_hashes = expected_row_hashes

            yield SourceItemMetadata(
                source_file=self.source_uri,
                version=content_hash,
                size_hint=source_size,
                content_hash=content_hash,
            )
            if palace._skip_requested:
                return

            filed_at = datetime.now(timezone.utc).isoformat()
            batch_ids: list[str] = []
            batch_documents: list[str] = []
            batch_metadatas: list[dict] = []
            written_ids: set[str] = set()

            def flush() -> None:
                if not batch_ids:
                    return
                palace.drawer_collection.upsert(
                    ids=list(batch_ids),
                    documents=list(batch_documents),
                    metadatas=list(batch_metadatas),
                )
                self.drawers_written += len(batch_ids)
                batch_ids.clear()
                batch_documents.clear()
                batch_metadatas.clear()

            for record in _parse_managed_claude_jsonl(self.input_path):
                item_id = _drawer_id_for_message(
                    record["session_id"],
                    record["uuid"],
                    source_uri=self.source_uri,
                )
                if item_id in written_ids:
                    raise ReceiptIdentityError(
                        "sweeper source changed to contain duplicate message IDs during ingestion"
                    )
                written_ids.add(item_id)
                document = f"{record['role'].upper()}: {record['content']}"
                metadata = self._record_metadata(record, filed_at=filed_at)
                semantic_hash = _semantic_row_hash(item_id, document, metadata)
                if semantic_hash != expected_row_hashes.get(item_id):
                    raise ReceiptConflictError("sweeper source semantics changed during ingestion")
                metadata[_SWEEPER_SEMANTIC_METADATA_HASH] = semantic_hash
                batch_ids.append(item_id)
                batch_documents.append(document)
                batch_metadatas.append(metadata)
                if len(batch_ids) >= _SWEEP_BATCH_SIZE:
                    flush()
            flush()

            final_hash, final_size = _file_fingerprint(self.input_path)
            if (
                final_hash != content_hash
                or final_size != source_size
                or written_ids != set(expected_row_hashes)
            ):
                raise ReceiptConflictError(
                    "sweeper source changed during ingestion; rolled back mixed-version output"
                )
            self.assert_exact_representation(palace.drawer_collection)

    def describe_schema(self) -> AdapterSchema:
        return AdapterSchema(fields={}, version="1")

    def is_current(self, *, item: SourceItemMetadata, existing_metadata: Optional[dict]) -> bool:
        del item, existing_metadata
        return self.exact_current

    def assert_exact_representation(self, collection) -> None:
        rows = _result_rows(
            collection.get(
                where={"source_file": self.source_uri},
                include=["documents", "metadatas"],
            )
        )
        if set(rows) != set(self.expected_row_hashes):
            raise ReceiptConflictError("sweeper exact row set does not match the source")
        for item_id, (document, metadata) in rows.items():
            expected = self.expected_row_hashes[item_id]
            if (
                not isinstance(document, str)
                or metadata.get(META_SOURCE_IDENTITY) != self.source_identity
                or metadata.get("source_file") != self.source_uri
                or metadata.get(_SWEEPER_SEMANTIC_METADATA_HASH) != expected
                or _semantic_row_hash(item_id, document, metadata) != expected
            ):
                raise ReceiptConflictError(
                    f"sweeper semantic row readback does not match the source: {item_id}"
                )


def sweep(
    jsonl_path: str,
    palace_path: str,
    source_label: Optional[str] = None,
    *,
    allow_zero_output: bool = False,
) -> dict:
    """Replace one JSONL file's complete sweeper representation safely."""
    input_path = Path(jsonl_path).expanduser().resolve()
    if not input_path.is_file():
        raise FileNotFoundError(f"sweeper source is not a file: {input_path}")
    resolved_palace = str(Path(palace_path).expanduser().resolve())
    source_uri = _sweeper_source_uri(input_path)
    legacy_source_files = tuple(
        sorted(
            {
                str(jsonl_path),
                str(input_path),
                *([source_label] if source_label else []),
            }
            - {source_uri}
        )
    )
    legacy_ids = _legacy_message_ids(input_path)
    with managed_write_scope(resolved_palace, lock_factory=mine_palace_lock):
        return _sweep_under_palace_lock(
            input_path=input_path,
            resolved_palace=resolved_palace,
            source_uri=source_uri,
            source_label=source_label,
            legacy_source_files=legacy_source_files,
            legacy_ids=legacy_ids,
            allow_zero_output=allow_zero_output,
        )


def _sweep_under_palace_lock(
    *,
    input_path: Path,
    resolved_palace: str,
    source_uri: str,
    source_label: Optional[str],
    legacy_source_files: tuple[str, ...],
    legacy_ids: set[str],
    allow_zero_output: bool,
) -> dict:
    """Run all palace access inside an already-held managed write scope."""
    collection = get_collection(resolved_palace, create=True)
    _preflight_legacy_unmanaged(
        collection,
        input_path=input_path,
        legacy_source_files=legacy_source_files,
        legacy_ids=legacy_ids,
    )
    receipt_store = ReceiptStore(resolved_palace)
    source_identity = receipt_store.source_identity(source_uri)
    adapter = _SweeperJsonlAdapter(
        input_path=input_path,
        source_uri=source_uri,
        source_identity=source_identity,
        source_label=source_label,
        legacy_source_files=legacy_source_files,
        allow_zero_output=allow_zero_output,
    )
    context = PalaceContext(
        drawer_collection=collection,
        closet_collection=None,
        knowledge_graph=None,
        palace_path=resolved_palace,
        adapter_name=adapter.name,
        adapter_version=adapter.adapter_version,
    )
    managed = managed_adapter_ingest(
        adapter=adapter,
        source=SourceRef(uri=source_uri),
        palace=context,
        receipt_store=receipt_store,
        caller="sweeper",
        config={
            "contract": _SWEEPER_CONTRACT,
            "materialization": "complete-sweeper-lane",
            "source_label": source_label or "",
            "allow_zero_output": allow_zero_output,
        },
        committed_error_policy="report",
    )

    validation_errors: list[str] = []
    current_candidate = managed.receipt_events[0] if len(managed.receipt_events) == 1 else {}
    current = current_candidate if isinstance(current_candidate, dict) else {}
    receipt_id = managed.receipt_ids[0] if len(managed.receipt_ids) == 1 else None
    driver_status = (
        managed.receipt_verification_statuses[0]
        if len(managed.receipt_verification_statuses) == 1
        else None
    )
    driver_error = (
        managed.receipt_validation_errors[0]
        if len(managed.receipt_validation_errors) == 1
        else None
    )
    if driver_status is None:
        validation_errors.append("managed driver returned no single verification status")
    elif driver_status != "represented":
        validation_errors.append(driver_error or f"managed driver reported {driver_status}")
    if not current or current.get("state") != "COMPLETE":
        validation_errors.append("managed driver returned no single terminal COMPLETE")
    if current and current.get("receipt_id") != receipt_id:
        validation_errors.append("terminal receipt event does not match the managed result")
    source_payload = current.get("source")
    if not isinstance(source_payload, dict):
        source_payload = {}
    if adapter.source_content_hash is None:
        validation_errors.append("adapter did not retain exact source content identity")
    elif current and source_payload.get("content_hash") != adapter.source_content_hash:
        validation_errors.append("terminal receipt source hash does not match the processed source")
    outputs_payload = current.get("outputs")
    if not isinstance(outputs_payload, dict):
        outputs_payload = {}
    identities = outputs_payload.get("identities")
    if not isinstance(identities, list):
        identities = []
    represented_ids = {
        item.get("id")
        for item in identities
        if isinstance(item, dict)
        and item.get("collection") == "drawers"
        and isinstance(item.get("id"), str)
    }
    if current and represented_ids != adapter.generated_ids:
        validation_errors.append("terminal receipt output manifest does not match the source")

    added_ids = adapter.generated_ids - adapter.previous_ids
    overlapping_ids = adapter.generated_ids & adapter.previous_ids
    rewritten_ids = overlapping_ids if adapter.drawers_written else set()
    rebound_count = len(adapter.generated_ids) if managed.sources_unchanged == 1 else 0
    verification_status = "represented" if not validation_errors else "committed-unverified"
    expected_count = len(adapter.generated_ids)
    represented_count = expected_count if verification_status == "represented" else 0

    return {
        "drawers_added": len(added_ids),
        "drawers_already_present": len(overlapping_ids),
        "drawers_updated": len(adapter.semantic_updated_ids),
        "drawers_semantically_unchanged": len(adapter.semantic_unchanged_ids),
        "drawers_rewritten": len(rewritten_ids),
        "drawers_rebound": rebound_count,
        "drawers_removed": len(adapter.previous_ids - adapter.generated_ids),
        "drawers_expected": expected_count,
        "drawers_verifier_confirmed": represented_count,
        "drawers_represented": represented_count,
        "drawers_upserted": adapter.drawers_written,
        "drawers_physical_mutations": adapter.drawers_written + rebound_count,
        "drawers_skipped": 0,
        "cursor_by_session": adapter.cursor_by_session,
        "source_uri": source_uri,
        "source_content_hash": adapter.source_content_hash,
        "source_size_bytes": adapter.source_size,
        "receipt_id": receipt_id,
        "run_id": managed.run_id,
        "disposition": current.get("disposition", "COMMITTED_UNVERIFIED"),
        "committed": True,
        "unchanged": managed.sources_unchanged == 1,
        "verification_status": verification_status,
        "verification_error": "; ".join(validation_errors) or None,
    }


def sweep_directory(
    dir_path: str,
    palace_path: str,
    *,
    allow_zero_output: bool = False,
) -> dict:
    """Sweep every .jsonl file in a directory (recursive).

    Returns aggregated summary across all files. ``files_attempted``
    includes files that raised, so the count reflects discovery rather
    than only successes; ``files_succeeded`` is the subset that
    completed without error.
    """
    dir_p = Path(dir_path).expanduser().resolve()
    files = sorted(dir_p.rglob("*.jsonl"))

    total_added = 0
    total_already_present = 0
    total_updated = 0
    total_semantically_unchanged = 0
    total_rewritten = 0
    total_rebound = 0
    total_removed = 0
    total_upserted = 0
    total_physical_mutations = 0
    total_expected = 0
    total_verifier_confirmed = 0
    committed_unverified = 0
    per_file = []

    failures: list[dict] = []
    for f in files:
        try:
            result = sweep(
                str(f),
                palace_path,
                source_label=str(f),
                allow_zero_output=allow_zero_output,
            )
        except Exception as exc:
            logger.error("sweeper: sweep failed on %s: %s", f, exc)
            print(f"  WARNING: sweep failed on {f}: {exc}", file=sys.stderr)
            failures.append({"file": str(f), "error": str(exc)})
            continue
        total_added += result["drawers_added"]
        total_already_present += result.get("drawers_already_present", 0)
        total_updated += result.get("drawers_updated", 0)
        total_semantically_unchanged += result.get("drawers_semantically_unchanged", 0)
        total_rewritten += result.get("drawers_rewritten", 0)
        total_rebound += result.get("drawers_rebound", 0)
        total_removed += result.get("drawers_removed", 0)
        total_upserted += result.get("drawers_upserted", 0)
        total_physical_mutations += result.get("drawers_physical_mutations", 0)
        total_expected += result.get("drawers_expected", 0)
        total_verifier_confirmed += result.get("drawers_verifier_confirmed", 0)
        if result.get("verification_status") != "represented":
            committed_unverified += 1
        per_file.append(
            {
                "file": str(f),
                "added": result["drawers_added"],
                "already_present": result.get("drawers_already_present", 0),
                "updated": result.get("drawers_updated", 0),
                "semantically_unchanged": result.get("drawers_semantically_unchanged", 0),
                "rewritten": result.get("drawers_rewritten", 0),
                "rebound": result.get("drawers_rebound", 0),
                "removed": result.get("drawers_removed", 0),
                "expected": result.get("drawers_expected", 0),
                "represented": result.get("drawers_represented", 0),
                "upserted": result.get("drawers_upserted", 0),
                "physical_mutations": result.get("drawers_physical_mutations", 0),
                "receipt_id": result.get("receipt_id"),
                "verification_status": result.get("verification_status"),
                "verification_error": result.get("verification_error"),
            }
        )

    whole_run_represented = (
        total_verifier_confirmed if not failures and committed_unverified == 0 else 0
    )
    return {
        "files_attempted": len(files),
        "files_succeeded": len(per_file),
        "drawers_added": total_added,
        "drawers_already_present": total_already_present,
        "drawers_updated": total_updated,
        "drawers_semantically_unchanged": total_semantically_unchanged,
        "drawers_rewritten": total_rewritten,
        "drawers_rebound": total_rebound,
        "drawers_removed": total_removed,
        "drawers_expected": total_expected,
        "drawers_verifier_confirmed": total_verifier_confirmed,
        "drawers_represented": whole_run_represented,
        "drawers_upserted": total_upserted,
        "drawers_physical_mutations": total_physical_mutations,
        "files_committed_unverified": committed_unverified,
        "drawers_skipped": 0,
        "per_file": per_file,
        "failures": failures,
    }
