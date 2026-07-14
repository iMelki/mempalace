"""Durable, pseudonymized receipts for managed MemPalace source writes.

The journal is deliberately local and append-only. Each event is a complete,
cumulative observation of one source-write attempt, while a small mutable index
points at the latest COMPLETE receipt for a pseudonymous source identity.
"""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import logging
import math
import os
import re
import secrets
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Union

from .version import __version__

RECEIPT_SCHEMA = "mempalace-source-write-receipt/v1"
INVALIDATION_SCHEMA = "mempalace-source-write-invalidation/v1"
IDENTITY_KEY_SCHEMA = "mempalace-receipt-identity-key/v1"
RECOVERY_SCHEMA = "mempalace-managed-rewrite-recovery/v2"
LEGACY_RECOVERY_SCHEMA = "mempalace-managed-rewrite-recovery/v1"
CURRENT_INDEX_SCHEMA = "mempalace-source-write-receipt-index/v1"
COMPLETE_PUBLICATION_SCHEMA = "mempalace-durable-complete-publication/v1"
LEGACY_MISSING_PREDECESSOR_COMPATIBILITY = "mempalace-explicit-legacy-missing-predecessor/v1"
RECEIPT_STATES = frozenset({"START", "RUNNING", "COMPLETE", "ABORT", "FAIL"})
TERMINAL_STATES = frozenset({"COMPLETE", "ABORT", "FAIL"})

META_RECEIPT_ID = "write_receipt_id"
META_SOURCE_IDENTITY = "write_source_identity"
META_SOURCE_CONTENT_HASH = "write_source_content_hash"
META_SOURCE_VERSION_HASH = "write_source_version_hash"
META_OUTPUT_CONTENT_HASH = "write_output_content_hash"

_HASH_RE = re.compile(r"^(?:sha256|hmac-sha256):[0-9a-f]{64}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:+-]{1,160}$")
_LOGGER = logging.getLogger(__name__)
_MOVEFILE_REPLACE_EXISTING = 0x1
_MOVEFILE_WRITE_THROUGH = 0x8
_WINDOWS_ALREADY_EXISTS_ERRORS = frozenset({80, 183})
_DIRECTORY_DURABILITY_MARKER = ".mempalace-directory-durable-v1"
_DIRECTORY_DURABILITY_BYTES = b"mempalace-directory-durability/v1\n"
_DIRECTORY_SYNC_BARRIER = ".mempalace-directory-sync-barrier-v1"
_PURGE_AUTHORITY = object()
_MANAGED_WRITE_AUTHORITY = object()
_MANAGED_WRITE_READBACK_TIMEOUT_SECONDS = 2.0
_MANAGED_WRITE_READBACK_INITIAL_RETRY_SECONDS = 0.01
_MANAGED_WRITE_READBACK_MAX_RETRY_SECONDS = 0.25
_MANAGED_WRITE_READBACK_BACKOFF = 2.0
_MANAGED_WRITE_SCOPE: ContextVar[Optional["_ManagedWriteScope"]] = ContextVar(
    "mempalace_managed_write_scope",
    default=None,
)


class ReceiptError(RuntimeError):
    """Base error for receipt creation, storage, and validation."""


class ReceiptIdentityError(ReceiptError, ValueError):
    """Raised when required provenance identity is absent or malformed."""


class ReceiptStateError(ReceiptError):
    """Raised when a receipt lifecycle transition is invalid."""


class ReceiptConflictError(ReceiptError):
    """Raised when immutable receipt content would be overwritten."""


class ReceiptRecoveryError(ReceiptError):
    """Raised when a durable managed rewrite cannot be reconciled exactly."""


class ReceiptDurabilityError(ReceiptRecoveryError):
    """Raised when a recovery record cannot be proven durably published."""


@contextmanager
def managed_write_scope(
    palace_path: Union[str, os.PathLike],
    *,
    lock_factory: Callable[[str], Any],
):
    """Hold the exclusive palace lock and expose private mutation authority.

    This is an in-process trusted-adapter boundary, not a Chroma transaction.
    Nested scopes for the same palace reuse the already-held cross-process lock.
    """
    resolved = Path(palace_path).expanduser().resolve()
    current = _MANAGED_WRITE_SCOPE.get()
    if current is not None:
        if current.palace_path != resolved:
            raise ReceiptRecoveryError(
                "one process context cannot mutate two palaces under one managed lock"
            )
        yield current
        return

    with lock_factory(str(resolved)):
        state = _ManagedWriteScope(
            authority=_MANAGED_WRITE_AUTHORITY,
            palace_path=resolved,
            nonce=object(),
        )
        token = _MANAGED_WRITE_SCOPE.set(state)
        try:
            yield state
        finally:
            _MANAGED_WRITE_SCOPE.reset(token)


def _require_managed_write_scope(palace_path: Union[str, os.PathLike]) -> _ManagedWriteScope:
    resolved = Path(palace_path).expanduser().resolve()
    current = _MANAGED_WRITE_SCOPE.get()
    if (
        current is None
        or current.authority is not _MANAGED_WRITE_AUTHORITY
        or current.palace_path != resolved
    ):
        raise ReceiptRecoveryError(
            "managed collection mutation requires the exclusive palace write scope"
        )
    return current


def utc_now() -> str:
    """Return an RFC 3339 UTC timestamp with an explicit ``Z`` suffix."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_bytes(data: bytes) -> str:
    """Return a tagged SHA-256 digest."""
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def sha256_text(value: str) -> str:
    """Hash UTF-8 text with an unambiguous SHA-256 tag."""
    return sha256_bytes(value.encode("utf-8"))


def version_hash(version: str) -> str:
    """Hash an adapter's opaque source-side version token."""
    if not isinstance(version, str) or not version:
        raise ReceiptIdentityError("source version must be a non-empty string")
    return sha256_text(version)


def canonical_source_locator(locator: Union[str, os.PathLike], *, local_path: bool) -> str:
    """Return the one locator spelling used by identities, locks, and row metadata."""
    value = os.fspath(locator)
    _require_text(value, "source locator")
    return _canonical_local_path(value) if local_path else value.strip()


def source_size_bucket(size_bytes: int) -> str:
    """Return a coarse power-of-two byte bucket for pseudonymized projections."""
    if not isinstance(size_bytes, int) or size_bytes < 0:
        raise ReceiptIdentityError("source size must be a non-negative integer")
    if size_bytes < 1024:
        return "0-1023"
    lower = 1 << (size_bytes.bit_length() - 1)
    return f"{lower}-{(lower << 1) - 1}"


def config_hash(config: Any) -> str:
    """Hash output-affecting configuration without retaining its raw values."""
    return sha256_bytes(_canonical_json_bytes(_jsonable(config)))


def output_identity(
    item_id: str,
    content: Union[str, bytes],
    *,
    collection: str = "drawers",
    kind: str = "drawer",
    producer_receipt_id: str,
) -> dict:
    """Build one content-bound output identity for a receipt manifest."""
    _require_text(item_id, "output id")
    _require_text(collection, "output collection")
    _require_text(kind, "output kind")
    _require_uuid(producer_receipt_id, "producer receipt id")
    raw = content if isinstance(content, bytes) else content.encode("utf-8")
    return {
        "collection": collection,
        "id": item_id,
        "kind": kind,
        "content_hash": sha256_bytes(raw),
        "producer_receipt_id": producer_receipt_id,
    }


def receipt_event_id(receipt_id: str, sequence: int) -> str:
    """Derive the only valid event identity for a receipt sequence."""
    canonical_receipt_id = _require_uuid(receipt_id, "receipt id")
    if not isinstance(sequence, int) or sequence < 0:
        raise ReceiptIdentityError("receipt sequence must be a non-negative integer")
    return str(uuid.uuid5(uuid.UUID(canonical_receipt_id), str(sequence)))


def manifest_digest(outputs: Iterable[Mapping[str, Any]]) -> str:
    """Digest a deterministic, content-bound output manifest."""
    normalized = _normalize_outputs(outputs)
    return sha256_bytes(_canonical_json_bytes(normalized))


def stamp_output_metadata(
    metadata: Optional[Mapping[str, Any]],
    session: "SourceWriteReceiptSession",
    content: Union[str, bytes],
) -> dict:
    """Attach receipt/source/content identity fields to Chroma metadata."""
    stamped = dict(metadata or {})
    stamped[META_RECEIPT_ID] = session.receipt_id
    stamped[META_SOURCE_IDENTITY] = session.source["identity"]
    stamped[META_SOURCE_CONTENT_HASH] = session.source["content_hash"]
    stamped[META_SOURCE_VERSION_HASH] = session.source["version_hash"]
    raw = content if isinstance(content, bytes) else content.encode("utf-8")
    stamped[META_OUTPUT_CONTENT_HASH] = sha256_bytes(raw)
    return stamped


@dataclass(frozen=True)
class ManagedRunIdentity:
    """Identity shared by all per-source receipts in one managed mine run."""

    run_id: str
    caller_identity: str
    mode: str
    config_digest: str
    producer: dict

    def as_dict(self) -> dict:
        return {
            "id": self.run_id,
            "caller": {"identity": self.caller_identity},
            "mode": self.mode,
        }


@dataclass(frozen=True)
class ManagedSourceSnapshot:
    """Exact restorable rows selected for one managed source rewrite."""

    ids: tuple[str, ...]
    documents: tuple[str, ...]
    metadatas: tuple[dict, ...]
    embeddings: Optional[tuple[tuple[Any, ...], ...]] = None


@dataclass(frozen=True)
class ManagedSourceSelectors:
    """Private selectors that define every row represented by one source."""

    source_files: tuple[str, ...]


@dataclass(frozen=True)
class DurablePublicationProof:
    """Verified OS-level publication evidence for one recovery record."""

    path: Path
    content_sha256: str
    size_bytes: int
    primitive: str


@dataclass(frozen=True)
class ManagedRewriteRecovery:
    """Validated durable recovery state for one not-yet-committed rewrite."""

    path: Path
    receipt_id: str
    source_identity: str
    previous_receipt_id: Optional[str]
    selectors: ManagedSourceSelectors
    selector_coverage_complete: bool
    snapshots: Mapping[str, ManagedSourceSnapshot]


@dataclass(frozen=True)
class _ManagedWriteScope:
    """Unforgeable in-process evidence that the palace lock is held."""

    authority: object
    palace_path: Path
    nonce: object


@dataclass(frozen=True)
class _ValidatedCollectionRow:
    """One exact collection row authorized for conditional deletion."""

    item_id: str
    document: str
    metadata: Mapping[str, Any]
    embedding: Optional[tuple[Any, ...]]


@dataclass(frozen=True)
class _ManagedPurgeCapability:
    """Private, recovery-bound authority for one exact destructive step."""

    authority: object
    lock_nonce: object
    palace_path: Path
    collection_identity: int
    recovery_path: Path
    receipt_id: str
    source_identity: str
    collection_name: str
    snapshot_digest: str
    rows: tuple[_ValidatedCollectionRow, ...]


class ReceiptStore:
    """Atomic local storage for source-write receipt events."""

    def __init__(
        self,
        palace_path: Union[str, os.PathLike],
        *,
        receipt_root: Optional[Union[str, os.PathLike]] = None,
        clock: Callable[[], str] = utc_now,
    ):
        self.palace_path = Path(palace_path).expanduser().resolve()
        self.root = (
            Path(receipt_root).expanduser().resolve()
            if receipt_root is not None
            else self.palace_path / ".mempalace" / "write-receipts" / "v1"
        )
        self.clock = clock
        self.events_dir = self.root / "events"
        self.sources_dir = self.root / "sources"
        self.invalidations_dir = self.root / "invalidations"
        self.recoveries_dir = self.root / "recoveries"
        self.identity_key_path = self.root / "identity.key"
        self.identity_metadata_path = self.root / "identity-key.json"
        receipt_state_exists = _receipt_state_exists(self.root)
        for directory in (
            self.root,
            self.events_dir,
            self.sources_dir,
            self.invalidations_dir,
            self.recoveries_dir,
        ):
            _ensure_private_dir(directory)
        self._identity_key = _load_or_create_identity_key(
            self.identity_key_path,
            require_existing=receipt_state_exists or self.identity_metadata_path.exists(),
        )
        _ensure_identity_key_metadata(self.identity_metadata_path, self._identity_key)

    def create_run(self, *, caller: str, mode: str, config: Any) -> ManagedRunIdentity:
        """Create a run identity without persisting raw caller/config values."""
        _require_text(caller, "caller")
        _require_text(mode, "run mode")
        return ManagedRunIdentity(
            run_id=str(uuid.uuid4()),
            caller_identity=self._pseudonym("caller", caller),
            mode=mode,
            config_digest=config_hash(config),
            producer=_producer_identity(),
        )

    def source_identity(self, locator: str, *, local_path: bool = False) -> str:
        """Return a stable per-palace HMAC identity for a source locator."""
        canonical = canonical_source_locator(locator, local_path=local_path)
        if local_path:
            canonical = os.path.normcase(canonical)
        return self._pseudonym("source", canonical)

    def begin_source(
        self,
        *,
        run: ManagedRunIdentity,
        source_locator: str,
        source_content_hash: str,
        source_version_hash: str,
        source_size_bytes: int,
        adapter_name: str,
        adapter_version: str,
        local_path: bool = False,
    ) -> "SourceWriteReceiptSession":
        """Persist START and return the mutable in-process receipt session."""
        _require_sha256(source_content_hash, "source content hash")
        _require_sha256(source_version_hash, "source version hash")
        _require_text(adapter_name, "adapter name")
        _require_text(adapter_version, "adapter version")
        if not isinstance(source_size_bytes, int) or source_size_bytes < 0:
            raise ReceiptIdentityError("source size must be a non-negative integer")

        source_identity = self.source_identity(source_locator, local_path=local_path)
        if self._pending_recovery_paths(source_identity):
            raise ReceiptRecoveryError(
                "pending managed rewrite recovery must be reconciled before a new source write"
            )
        source = {
            "identity": source_identity,
            "content_hash": source_content_hash,
            "version_hash": source_version_hash,
            "size_bytes": source_size_bytes,
            "shared_content_identity": self._pseudonym(
                "shared-source-content",
                f"{source_identity}\0{source_content_hash}",
            ),
            "shared_version_identity": self._pseudonym(
                "shared-source-version",
                f"{source_identity}\0{source_version_hash}",
            ),
            "size_bucket": source_size_bucket(source_size_bytes),
            "adapter": {"name": adapter_name, "version": adapter_version},
        }
        receipt_id = str(
            uuid.uuid5(
                uuid.UUID(run.run_id),
                "|".join(
                    (
                        source_identity,
                        source_version_hash,
                        run.config_digest,
                        adapter_name,
                        adapter_version,
                    )
                ),
            )
        )
        return SourceWriteReceiptSession(
            store=self,
            run=run,
            source=source,
            receipt_id=receipt_id,
            previous_complete=self.find_current(source_identity),
        )

    def find_current(
        self,
        source_identity: str,
        *,
        content_hash: Optional[str] = None,
        version_digest: Optional[str] = None,
        config_digest: Optional[str] = None,
    ) -> Optional[dict]:
        """Return the atomic current-index head, repairing only an unambiguous lineage."""
        return self._find_current(
            source_identity,
            content_hash=content_hash,
            version_digest=version_digest,
            config_digest=config_digest,
            repair_index=True,
        )

    def find_current_read_only(
        self,
        source_identity: str,
        *,
        content_hash: Optional[str] = None,
        version_digest: Optional[str] = None,
        config_digest: Optional[str] = None,
    ) -> Optional[dict]:
        """Resolve the authoritative lineage head without repairing journal state."""
        return self._find_current(
            source_identity,
            content_hash=content_hash,
            version_digest=version_digest,
            config_digest=config_digest,
            repair_index=False,
        )

    def _find_current(
        self,
        source_identity: str,
        *,
        content_hash: Optional[str],
        version_digest: Optional[str],
        config_digest: Optional[str],
        repair_index: bool,
    ) -> Optional[dict]:
        _require_hmac(source_identity, "source identity")
        if content_hash is not None:
            _require_sha256(content_hash, "source content hash")
        if version_digest is not None:
            _require_sha256(version_digest, "source version hash")
        if config_digest is not None:
            _require_sha256(config_digest, "config digest")
        index_path = self.sources_dir / f"{_digest_value(source_identity)}.json"
        indexed: Optional[tuple[dict, Path]] = None
        if index_path.exists():
            try:
                index = _read_json(index_path)
                if index.get("schema") != CURRENT_INDEX_SCHEMA:
                    raise ValueError("receipt index schema is invalid")
                if index.get("source_identity") != source_identity:
                    raise ValueError("receipt index source identity does not match its key")
                event_path = (self.root / index["event_path"]).resolve()
                if self.root != event_path and self.root not in event_path.parents:
                    raise ValueError("receipt index path escapes the journal root")
                event = _read_json(event_path)
                if not _index_matches_event(index, event, source_identity):
                    raise ValueError("receipt index does not match its journal event")
                indexed = (event, event_path)
            except (KeyError, OSError, ValueError, json.JSONDecodeError) as exc:
                raise ReceiptConflictError("current receipt index is inconsistent") from exc

        candidates = self._complete_events_for_source(source_identity)
        if indexed is not None:
            candidates.setdefault(indexed[0]["receipt_id"], indexed)
            current, current_path = _resolve_receipt_lineage(
                candidates,
                indexed_receipt_id=indexed[0]["receipt_id"],
            )
        elif candidates:
            current, current_path = _resolve_receipt_lineage(candidates)
        else:
            return None

        if repair_index and (indexed is None or indexed[0]["receipt_id"] != current["receipt_id"]):
            self.set_current(current, current_path)

        source = current["source"]
        producer = current["producer"]
        if content_hash is not None and source["content_hash"] != content_hash:
            return None
        if version_digest is not None and source["version_hash"] != version_digest:
            return None
        if config_digest is not None and producer["config"]["digest"] != config_digest:
            return None
        return current

    def _complete_events_for_source(self, source_identity: str) -> dict[str, tuple[dict, Path]]:
        """Load exact COMPLETE candidates, failing on corruption in this source partition."""
        candidates: dict[str, tuple[dict, Path]] = {}
        source_events = self.events_dir / _digest_value(source_identity)
        partition_paths = (
            list(source_events.rglob("*-complete.json")) if source_events.exists() else []
        )
        for path in partition_paths:
            try:
                event = _read_json(path)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise ReceiptConflictError("source receipt journal is unreadable") from exc
            if not _is_complete_event_for_source(event, source_identity):
                raise ReceiptConflictError("source receipt journal contains an invalid COMPLETE")
            _add_receipt_candidate(candidates, event, path)

        # Compatibility for COMPLETE events created before source partitioning.
        for path in self.events_dir.rglob("*-complete.json"):
            if path in partition_paths:
                continue
            try:
                event = _read_json(path)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise ReceiptConflictError("legacy receipt journal is unreadable") from exc
            try:
                event_source = _require_hmac(
                    _require_mapping(event.get("source"), "source identity").get("identity"),
                    "source identity",
                )
            except ReceiptIdentityError as exc:
                raise ReceiptConflictError(
                    "legacy receipt journal has no trustworthy source identity"
                ) from exc
            if event_source != source_identity:
                continue
            if not _is_complete_event_for_source(event, source_identity):
                raise ReceiptConflictError("legacy receipt journal contains an invalid COMPLETE")
            _add_receipt_candidate(candidates, event, path)
        return candidates

    def invalidations_for(self, receipt_id: str) -> list[dict]:
        """Read every durable invalidation record for a receipt."""
        _require_uuid(receipt_id, "receipt id")
        directory = self.invalidations_dir / receipt_id
        records: dict[str, dict] = {}
        if directory.exists():
            for path in sorted(directory.glob("*.json")):
                try:
                    record = _validate_invalidation_record(
                        _read_json(path), invalidated_receipt_id=receipt_id
                    )
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    raise ReceiptIdentityError("invalidation record is unreadable") from exc
                records[record["invalidation_id"]] = record

        if not records:
            # Compatibility for the pre-fix layout that accidentally keyed the
            # directory by invalidation ID instead of invalidated receipt ID.
            for path in self.invalidations_dir.glob("*/*.json"):
                if path.parent == directory:
                    continue
                try:
                    record = _validate_invalidation_record(
                        _read_json(path), invalidated_receipt_id=receipt_id
                    )
                except (OSError, ValueError, json.JSONDecodeError, ReceiptIdentityError):
                    continue
                records[record["invalidation_id"]] = record

        if records:
            return [records[item] for item in sorted(records)]

        # COMPLETE is authoritative. Reconstruct a hook from the receipt when
        # a crash happened after COMPLETE publication but before the redundant
        # invalidation file was linked into place.
        for path in self.events_dir.rglob("*-complete.json"):
            try:
                event = _read_json(path)
                source_identity = event.get("source", {}).get("identity")
                if not isinstance(source_identity, str) or not _is_complete_event_for_source(
                    event, source_identity
                ):
                    continue
                for record in _planned_invalidation_records(event):
                    if record["invalidated_receipt_id"] == receipt_id:
                        records.setdefault(record["invalidation_id"], record)
            except (OSError, ValueError, json.JSONDecodeError, ReceiptIdentityError):
                continue
        return [records[item] for item in sorted(records)]

    def prepare_invalidation(
        self,
        *,
        invalidated_receipt: Mapping[str, Any],
        by_receipt_id: str,
        reason: str,
    ) -> dict:
        """Build a deterministic invalidation hook without publishing it."""
        invalidated_id = _require_uuid(invalidated_receipt.get("receipt_id"), "receipt id")
        _require_uuid(by_receipt_id, "successor receipt id")
        _require_text(reason, "invalidation reason")
        output_manifest = invalidated_receipt.get("outputs", {}).get("manifest_digest")
        _require_sha256(output_manifest, "invalidated manifest digest")
        invalidation_id = _invalidation_id(
            invalidated_id,
            by_receipt_id,
            reason,
            output_manifest,
        )
        return {
            "schema": INVALIDATION_SCHEMA,
            "invalidation_id": invalidation_id,
            "event_time": self.clock(),
            "invalidated_receipt_id": invalidated_id,
            "invalidated_manifest_digest": output_manifest,
            "by_receipt_id": by_receipt_id,
            "reason": reason,
        }

    def publish_invalidation(self, value: Mapping[str, Any]) -> dict:
        """Publish one prepared invalidation with create-only semantics."""
        payload = _validate_invalidation_record(value)
        invalidated_id = payload["invalidated_receipt_id"]
        invalidation_id = payload["invalidation_id"]
        path = self.invalidations_dir / invalidated_id / f"{invalidation_id}.json"
        try:
            _atomic_write_json(path, payload, immutable=True)
        except ReceiptConflictError:
            try:
                existing = _validate_invalidation_record(_read_json(path))
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise ReceiptIdentityError("invalidation record is unreadable") from exc
            if _same_invalidation(existing, payload):
                return existing
            raise
        return payload

    def record_invalidation(
        self,
        *,
        invalidated_receipt: Mapping[str, Any],
        by_receipt_id: str,
        reason: str,
    ) -> dict:
        """Append a path-redacted invalidation/supersession hook record."""
        payload = self.prepare_invalidation(
            invalidated_receipt=invalidated_receipt,
            by_receipt_id=by_receipt_id,
            reason=reason,
        )
        return self.publish_invalidation(payload)

    def write_event(self, event: Mapping[str, Any]) -> Path:
        """Write one immutable lifecycle event and return its path."""
        run_id = _require_uuid(event.get("run", {}).get("id"), "run id")
        receipt_id = _require_uuid(event.get("receipt_id"), "receipt id")
        source_identity = _require_hmac(
            event.get("source", {}).get("identity"),
            "source identity",
        )
        sequence = event.get("sequence")
        state = event.get("state")
        if not isinstance(sequence, int) or sequence < 0:
            raise ReceiptIdentityError("receipt sequence must be a non-negative integer")
        event_id = _require_uuid(event.get("event_id"), "event id")
        if event_id != receipt_event_id(receipt_id, sequence):
            raise ReceiptIdentityError("event id is not bound to its receipt sequence")
        if state not in RECEIPT_STATES:
            raise ReceiptIdentityError(f"unsupported receipt state: {state!r}")
        path = (
            self.events_dir
            / _digest_value(source_identity)
            / run_id
            / receipt_id
            / f"{sequence:06d}-{state.lower()}.json"
        )
        if state == "COMPLETE":
            _validate_complete_publication_marker(event)
            _validate_terminal_output_binding(event)
            proof = _atomic_write_json(
                path,
                event,
                immutable=True,
                durable=True,
                durability_anchor=self.palace_path / ".mempalace",
            )
            _validate_durable_publication_proof(path, event, proof)
        else:
            if "publication" in event:
                raise ReceiptIdentityError("durable publication metadata is reserved for COMPLETE")
            _atomic_write_json(path, event, immutable=True)
        return path

    def _prove_complete_durable(self, event: Mapping[str, Any], path: Path) -> None:
        """Re-flush and re-read a marked COMPLETE before recovery removal."""
        _validate_complete_publication_marker(event)
        expected = _canonical_json_bytes(_jsonable(event)) + b"\n"
        try:
            _ensure_private_dir_durable_chain(
                path.parent,
                anchor=self.palace_path / ".mempalace",
            )
            digest = _flush_and_verify_published_file(path, expected)
            if os.name == "nt":
                _publish_windows_directory_sync_barrier(path.parent)
            else:
                _fsync_directory(path.parent)
        except (OSError, ValueError, ReceiptDurabilityError) as exc:
            raise ReceiptDurabilityError(
                "COMPLETE durability could not be re-proven; recovery is retained"
            ) from exc
        if digest != sha256_bytes(expected):
            raise ReceiptDurabilityError(
                "COMPLETE durability readback did not match; recovery is retained"
            )

    def set_current(self, event: Mapping[str, Any], event_path: Path) -> None:
        """Atomically refresh the reconstructable current-receipt index."""
        source_identity = event.get("source", {}).get("identity")
        _require_hmac(source_identity, "source identity")
        relative_path = event_path.relative_to(self.root).as_posix()
        payload = {
            "schema": CURRENT_INDEX_SCHEMA,
            "source_identity": source_identity,
            "receipt_id": event["receipt_id"],
            "event_path": relative_path,
            "source_content_hash": event["source"]["content_hash"],
            "source_version_hash": event["source"]["version_hash"],
            "config_digest": event["producer"]["config"]["digest"],
            "updated_at": event["event_time"],
        }
        path = self.sources_dir / f"{_digest_value(source_identity)}.json"
        _atomic_write_json(path, payload, immutable=False)

    def prepare_rewrite_recovery(
        self,
        *,
        session: "SourceWriteReceiptSession",
        snapshots: Mapping[str, ManagedSourceSnapshot],
        source_file: str,
        local_path: bool,
        source_aliases: Iterable[Union[str, os.PathLike]] = (),
        previous_receipt: Optional[Mapping[str, Any]] = None,
    ) -> Path:
        """Durably publish exact pre-purge rows before any destructive mutation."""
        if session.store is not self:
            raise ReceiptRecoveryError("rewrite recovery session belongs to another store")
        session._ensure_open()
        if not snapshots:
            raise ReceiptRecoveryError("rewrite recovery requires at least one collection")
        if self._pending_recovery_paths(session.source["identity"]):
            raise ReceiptRecoveryError(
                "another rewrite recovery is already pending for this source"
            )

        previous = previous_receipt if previous_receipt is not None else session.previous_complete
        previous_id = None
        if previous is not None:
            previous_id = _require_uuid(previous.get("receipt_id"), "previous receipt id")
            if previous.get("source", {}).get("identity") != session.source["identity"]:
                raise ReceiptRecoveryError("rewrite predecessor belongs to another source")

        collection_payloads = {
            _require_token(name, "recovery collection name"): _snapshot_payload(snapshot)
            for name, snapshot in sorted(snapshots.items())
        }
        selectors = ManagedSourceSelectors(
            source_files=_validated_source_aliases(
                source_file,
                local_path=local_path,
                source_aliases=source_aliases,
            )
        )
        core = {
            "schema": RECOVERY_SCHEMA,
            "receipt_id": session.receipt_id,
            "source_identity": session.source["identity"],
            "previous_receipt_id": previous_id,
            "created_at": self.clock(),
            "selectors": _source_selector_payload(selectors),
            "collections": collection_payloads,
        }
        payload = {**core, "manifest_digest": sha256_bytes(_canonical_json_bytes(core))}
        path = self._recovery_path(session.source["identity"], session.receipt_id)
        proof = _atomic_write_json(
            path,
            payload,
            immutable=True,
            durable=True,
            durability_anchor=self.palace_path / ".mempalace",
        )
        expected_bytes = _canonical_json_bytes(_jsonable(payload)) + b"\n"
        if (
            proof is None
            or proof.path.resolve() != path.resolve()
            or proof.content_sha256 != sha256_bytes(expected_bytes)
            or proof.size_bytes != len(expected_bytes)
            or not proof.primitive
        ):
            raise ReceiptDurabilityError(
                "managed rewrite recovery publication did not return verified evidence"
            )
        return path

    def finalize_rewrite_recovery(
        self,
        source_identity: str,
        receipt_id: str,
        *,
        collections: Mapping[str, Any],
    ) -> bool:
        """Remove recovery only after durable authoritative COMPLETE readback."""
        _require_managed_write_scope(self.palace_path)
        path = self._recovery_path(source_identity, receipt_id)
        if not path.exists():
            return False
        recovery = _load_rewrite_recovery(path, expected_source_identity=source_identity)
        current = self.find_current(source_identity)
        if current is None or current.get("receipt_id") != receipt_id:
            raise ReceiptRecoveryError(
                "cannot finalize rewrite recovery unless its COMPLETE is the "
                "unique authoritative lineage head"
            )
        candidate = self._complete_events_for_source(source_identity).get(receipt_id)
        if candidate is None:
            raise ReceiptRecoveryError("authoritative COMPLETE journal event is missing")
        complete, complete_path = candidate
        self._prove_complete_durable(complete, complete_path)
        _verify_committed_rewrite_state(
            complete,
            recovery,
            collections=collections,
        )
        self._delete_recovery(recovery.path)
        return True

    def discard_rewrite_recovery(
        self,
        source_identity: str,
        receipt_id: str,
        *,
        collections: Mapping[str, Any],
    ) -> bool:
        """Remove rollback state only after a fresh exact restoration readback."""
        _require_managed_write_scope(self.palace_path)
        path = self._recovery_path(source_identity, receipt_id)
        if not path.exists():
            return False
        recovery = _load_rewrite_recovery(path, expected_source_identity=source_identity)
        if self._complete_events_for_source(source_identity).get(receipt_id) is not None:
            raise ReceiptRecoveryError("COMPLETE rewrite recovery must be finalized, not discarded")
        missing = sorted(set(recovery.snapshots) - set(collections))
        if missing:
            raise ReceiptRecoveryError(
                f"recovery collections are unavailable: {', '.join(missing)}"
            )
        current = self.find_current(source_identity)
        current_id = current.get("receipt_id") if current is not None else None
        if current_id != recovery.previous_receipt_id:
            raise ReceiptRecoveryError(
                "restored rewrite baseline no longer matches the authoritative current index"
            )
        for name, snapshot in recovery.snapshots.items():
            _verify_restored_snapshot(
                collections[name],
                snapshot,
                recovery=recovery,
            )
        self._delete_recovery(recovery.path)
        return True

    def reconcile_pending_rewrites(
        self,
        collections: Mapping[str, Any],
        *,
        source_identity: Optional[str] = None,
    ) -> tuple[dict, ...]:
        """Commit or exactly restore every durable rewrite record in scope."""
        _require_managed_write_scope(self.palace_path)
        if source_identity is not None:
            _require_hmac(source_identity, "source identity")
        recoveries = [
            _load_rewrite_recovery(path, expected_source_identity=source_identity)
            for path in self._pending_recovery_paths(source_identity)
        ]
        missing = sorted(
            {
                name
                for recovery in recoveries
                for name in recovery.snapshots
                if name not in collections
            }
        )
        if missing:
            raise ReceiptRecoveryError(
                f"recovery collections are unavailable: {', '.join(missing)}"
            )

        plans = []
        for recovery in recoveries:
            complete = self._complete_events_for_source(recovery.source_identity).get(
                recovery.receipt_id
            )
            current = self.find_current(recovery.source_identity)
            current_id = current.get("receipt_id") if current is not None else None
            if complete is not None:
                if current_id != recovery.receipt_id:
                    raise ReceiptRecoveryError(
                        "completed rewrite is not the unique authoritative lineage head"
                    )
                self._prove_complete_durable(complete[0], complete[1])
                _verify_committed_rewrite_state(
                    complete[0],
                    recovery,
                    collections=collections,
                )
                plans.append(("commit", recovery))
                continue
            if current_id != recovery.previous_receipt_id:
                raise ReceiptRecoveryError(
                    "pending rewrite baseline no longer matches the authoritative current index"
                )
            plans.append(("restore", recovery))

        restore_capabilities = {}
        for action, recovery in plans:
            if action != "restore":
                continue
            for name, snapshot in recovery.snapshots.items():
                restore_capabilities[(recovery.path, name)] = _validate_recovery_collection_state(
                    collections[name],
                    snapshot,
                    recovery=recovery,
                    collection_name=name,
                )

        outcomes = []
        for action, recovery in plans:
            if action == "commit":
                self._delete_recovery(recovery.path)
            else:
                for name, snapshot in recovery.snapshots.items():
                    collection = collections[name]
                    capability = restore_capabilities[(recovery.path, name)]
                    _delete_validated_collection_rows(collection, capability)
                    _restore_missing_managed_source_rows(collection, snapshot)
                    _verify_restored_snapshot(
                        collection,
                        snapshot,
                        recovery=recovery,
                    )
                self._delete_recovery(recovery.path)
            outcomes.append(
                {
                    "receipt_id": recovery.receipt_id,
                    "source_identity": recovery.source_identity,
                    "action": action,
                }
            )
        return tuple(outcomes)

    def _pending_recovery_paths(self, source_identity: Optional[str] = None) -> list[Path]:
        if source_identity is None:
            return sorted(self.recoveries_dir.glob("*/*.json"))
        _require_hmac(source_identity, "source identity")
        directory = self.recoveries_dir / _digest_value(source_identity)
        return sorted(directory.glob("*.json")) if directory.exists() else []

    def _recovery_path(self, source_identity: str, receipt_id: str) -> Path:
        _require_hmac(source_identity, "source identity")
        _require_uuid(receipt_id, "receipt id")
        return self.recoveries_dir / _digest_value(source_identity) / f"{receipt_id}.json"

    def _delete_recovery(self, path: Path) -> None:
        payload = _read_json(path)
        try:
            path.unlink()
            if os.name == "nt":
                _publish_windows_directory_sync_barrier(path.parent)
            else:
                _fsync_directory(path.parent)
        except FileNotFoundError:
            return
        except (OSError, ReceiptDurabilityError) as exc:
            try:
                _atomic_write_json(
                    path,
                    payload,
                    immutable=True,
                    durable=True,
                    durability_anchor=self.palace_path / ".mempalace",
                )
            except Exception:
                _LOGGER.exception("failed to republish recovery after delete-sync uncertainty")
            raise ReceiptRecoveryError("durable rewrite recovery could not be removed") from exc
        try:
            path.parent.rmdir()
        except OSError:
            pass

    def _pseudonym(self, namespace: str, value: str) -> str:
        digest = hmac.new(
            self._identity_key,
            f"{namespace}\0{value}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"hmac-sha256:{digest}"


class SourceWriteReceiptSession:
    """In-process builder for one source version's receipt event sequence."""

    def __init__(
        self,
        *,
        store: ReceiptStore,
        run: ManagedRunIdentity,
        source: dict,
        receipt_id: str,
        previous_complete: Optional[dict],
    ):
        self.store = store
        self.run = run
        self.source = source
        self.receipt_id = receipt_id
        self.previous_complete = previous_complete
        self.state: Optional[str] = None
        self.sequence = 0
        self.stage = "discovered"
        self.disposition = "WRITE"
        self._outputs: dict[tuple[str, str], dict] = {}
        self._pending_invalidations: list[dict] = []
        self.errors: list[dict] = []
        self.relations: dict[str, Any] = {}
        if previous_complete is not None:
            self.relations["predecessor_receipt_id"] = _require_uuid(
                previous_complete.get("receipt_id"),
                "previous receipt id",
            )
        self.counts = {
            "source_bytes": source["size_bytes"],
            "items_expected": 0,
            "items_written": 0,
            "items_unchanged": 0,
            "items_invalidated": 0,
            "drawers_expected": 0,
            "drawers_written": 0,
            "drawers_unchanged": 0,
            "sentinels_written": 0,
            "batches": 0,
            "errors": 0,
        }
        self.last_event: Optional[dict] = None
        self.last_event_path: Optional[Path] = None
        self._emit("START")

    @property
    def outputs(self) -> list[dict]:
        return _normalize_outputs(self._outputs.values())

    def set_expected(self, *, drawers: int, items: Optional[int] = None) -> None:
        """Record expected drawer/item counts before writes begin."""
        if not isinstance(drawers, int) or drawers < 0:
            raise ReceiptIdentityError("expected drawer count must be non-negative")
        resolved_items = drawers if items is None else items
        if not isinstance(resolved_items, int) or resolved_items < 0:
            raise ReceiptIdentityError("expected item count must be non-negative")
        self.counts["drawers_expected"] = drawers
        self.counts["items_expected"] = resolved_items

    def running(self, stage: str) -> dict:
        """Append a cumulative RUNNING event."""
        _require_text(stage, "receipt stage")
        self.stage = stage
        return self._emit("RUNNING")

    def record_batch(self) -> None:
        self._ensure_open()
        self.counts["batches"] += 1

    def record_output(
        self,
        item_id: str,
        content: Union[str, bytes],
        *,
        collection: str = "drawers",
        kind: str = "drawer",
    ) -> dict:
        """Add one successfully persisted item to the cumulative manifest."""
        item = self.validate_output(
            item_id,
            content,
            collection=collection,
            kind=kind,
        )
        key = (collection, item_id)
        previous = self._outputs.get(key)
        if previous is None:
            self._outputs[key] = item
            self.counts["items_written"] += 1
            if kind == "drawer":
                self.counts["drawers_written"] += 1
            elif kind == "sentinel":
                self.counts["sentinels_written"] += 1
        return item

    def validate_output(
        self,
        item_id: str,
        content: Union[str, bytes],
        *,
        collection: str = "drawers",
        kind: str = "drawer",
    ) -> dict:
        """Validate a prospective item before its collection mutation."""
        self._ensure_open()
        item = output_identity(
            item_id,
            content,
            collection=collection,
            kind=kind,
            producer_receipt_id=self.receipt_id,
        )
        previous = self._outputs.get((collection, item_id))
        if previous is not None and previous != item:
            raise ReceiptConflictError(f"output identity changed within receipt: {item_id}")
        return item

    def reuse(self, prior: Mapping[str, Any]) -> None:
        """Bind unchanged content to this receipt's producer identity."""
        self._ensure_open()
        if prior.get("state") != "COMPLETE":
            raise ReceiptStateError("only COMPLETE receipts can be reused")
        prior_id = _require_uuid(prior.get("receipt_id"), "reused receipt id")
        if prior.get("source", {}).get("identity") != self.source["identity"]:
            raise ReceiptIdentityError("reused receipt has a different source identity")
        if prior.get("source", {}).get("content_hash") != self.source["content_hash"]:
            raise ReceiptIdentityError("reused receipt has a different source content hash")
        identities = prior.get("outputs", {}).get("identities")
        if not isinstance(identities, list):
            raise ReceiptIdentityError("reused receipt is missing exact output identities")
        normalized = _normalize_outputs(identities)
        if any(item["producer_receipt_id"] != prior_id for item in normalized):
            raise ReceiptIdentityError("reused manifest contains a foreign producer receipt")
        if self.relations.get("predecessor_receipt_id") not in {None, prior_id}:
            raise ReceiptConflictError("reused receipt does not match the indexed predecessor")
        self._outputs = {
            (item["collection"], item["id"]): {
                **item,
                "producer_receipt_id": self.receipt_id,
            }
            for item in normalized
        }
        self.counts["items_unchanged"] = len(self._outputs)
        self.counts["drawers_unchanged"] = sum(
            1 for item in self._outputs.values() if item["kind"] == "drawer"
        )
        self.counts["items_expected"] = len(self._outputs)
        self.counts["drawers_expected"] = self.counts["drawers_unchanged"]
        self.disposition = "UNCHANGED"
        self.relations["reuses_receipt_id"] = prior_id

    def supersede(self, prior: Mapping[str, Any], *, reason: str) -> None:
        """Declare that this attempt replaces a prior source receipt."""
        self._ensure_open()
        _require_text(reason, "supersession reason")
        prior_id = _require_uuid(prior.get("receipt_id"), "superseded receipt id")
        if self.relations.get("predecessor_receipt_id") not in {None, prior_id}:
            raise ReceiptConflictError("superseded receipt does not match the indexed predecessor")
        prior_manifest = prior.get("outputs", {}).get("manifest_digest")
        _require_sha256(prior_manifest, "superseded manifest digest")
        self.relations["supersedes"] = {
            "receipt_id": prior_id,
            "manifest_digest": prior_manifest,
            "reason": reason,
        }

    def record_invalidation(self, prior: Mapping[str, Any], *, reason: str) -> dict:
        """Queue a source-purge hook for publication after COMPLETE."""
        self._ensure_open()
        record = self.store.prepare_invalidation(
            invalidated_receipt=prior,
            by_receipt_id=self.receipt_id,
            reason=reason,
        )
        for pending in self._pending_invalidations:
            if pending["invalidation_id"] == record["invalidation_id"]:
                return pending
        self.relations.setdefault("invalidations", []).append(record["invalidation_id"])
        self.relations.setdefault("invalidation_records", []).append(record)
        self._pending_invalidations.append(record)
        prior_count = prior.get("outputs", {}).get("count", 0)
        if isinstance(prior_count, int) and prior_count >= 0:
            self.counts["items_invalidated"] = prior_count
        return record

    def discard_pending_invalidations(self) -> None:
        """Remove unpublished hooks after the corresponding store rollback."""
        self._ensure_open()
        self._pending_invalidations.clear()
        self.relations.pop("invalidations", None)
        self.relations.pop("invalidation_records", None)
        self.counts["items_invalidated"] = 0

    def record_error(self, error: BaseException, *, stage: str) -> dict:
        """Record a non-secret error type and message digest."""
        self._ensure_open()
        _require_text(stage, "error stage")
        error_type = f"{type(error).__module__}.{type(error).__name__}"
        record = {
            "type": error_type,
            "stage": stage,
            "message_digest": sha256_text(str(error)),
            "shared_message_identity": self.store._pseudonym(
                "shared-error-message",
                f"{self.receipt_id}\0{stage}\0{error_type}\0{error}",
            ),
        }
        self.errors.append(record)
        self.counts["errors"] = len(self.errors)
        return record

    def complete(self, *, disposition: Optional[str] = None) -> dict:
        """Append COMPLETE and advance the current-source index."""
        if disposition is not None:
            self.disposition = _require_text(disposition, "receipt disposition")
        self._validate_complete_counts()
        if any(item["producer_receipt_id"] != self.receipt_id for item in self.outputs):
            raise ReceiptIdentityError("COMPLETE manifest contains a foreign producer receipt")
        self.stage = "complete"
        event = self._emit("COMPLETE")
        for invalidation in self._pending_invalidations:
            try:
                self.store.publish_invalidation(invalidation)
            except Exception:
                _LOGGER.exception(
                    "receipt invalidation hook publication failed; COMPLETE remains authoritative"
                )
        try:
            self.store.set_current(event, self.last_event_path)
        except Exception:
            _LOGGER.exception("receipt index refresh failed; journal remains authoritative")
        return event

    def abort(self, error: Optional[BaseException] = None, *, stage: str = "interrupted") -> dict:
        """Append ABORT for an abnormal external interruption."""
        if error is not None:
            self.record_error(error, stage=stage)
        self.stage = stage
        return self._emit("ABORT")

    def fail(self, error: BaseException, *, stage: str = "write") -> dict:
        """Append FAIL for a source processing or persistence error."""
        self.record_error(error, stage=stage)
        self.stage = stage
        return self._emit("FAIL")

    def _emit(self, state: str) -> dict:
        self._check_transition(state)
        outputs = self.outputs
        event = {
            "schema": RECEIPT_SCHEMA,
            "receipt_id": self.receipt_id,
            "event_id": receipt_event_id(self.receipt_id, self.sequence),
            "sequence": self.sequence,
            "state": state,
            "event_time": self.store.clock(),
            "stage": self.stage,
            "disposition": self.disposition,
            "run": self.run.as_dict(),
            "producer": {
                **self.run.producer,
                "config": {"digest": self.run.config_digest},
            },
            "source": dict(self.source),
            "outputs": {
                "count": len(outputs),
                "manifest_digest": manifest_digest(outputs),
                "identities": outputs,
            },
            "counts": dict(self.counts),
            "errors": list(self.errors),
            "relations": _jsonable(self.relations),
        }
        if state == "COMPLETE":
            event["publication"] = {
                "schema": COMPLETE_PUBLICATION_SCHEMA,
                "policy": "durable-file-and-parent-proof-required",
            }
        path = self.store.write_event(event)
        self.last_event = event
        self.last_event_path = path
        self.state = state
        self.sequence += 1
        return event

    def _check_transition(self, next_state: str) -> None:
        if next_state not in RECEIPT_STATES:
            raise ReceiptStateError(f"unknown receipt state: {next_state}")
        if self.state in TERMINAL_STATES:
            raise ReceiptStateError(f"receipt is already terminal: {self.state}")
        if self.state is None and next_state != "START":
            raise ReceiptStateError("the first receipt event must be START")
        if self.state is not None and next_state == "START":
            raise ReceiptStateError("START can only be emitted once")

    def _ensure_open(self) -> None:
        if self.state in TERMINAL_STATES:
            raise ReceiptStateError(f"receipt is already terminal: {self.state}")

    def _validate_complete_counts(self) -> None:
        outputs = self.outputs
        drawers = sum(1 for item in outputs if item["kind"] == "drawer")
        if self.counts["items_written"] + self.counts["items_unchanged"] != len(outputs):
            raise ReceiptIdentityError("complete item counts do not match the manifest")
        if self.counts["items_expected"] != len(outputs):
            raise ReceiptIdentityError("expected item count does not match the manifest")
        if self.counts["drawers_written"] + self.counts["drawers_unchanged"] != drawers:
            raise ReceiptIdentityError("complete drawer counts do not match the manifest")
        if self.counts["drawers_expected"] != drawers:
            raise ReceiptIdentityError("expected drawer count does not match the manifest")


def snapshot_managed_source_rows(
    collection: Any,
    *,
    source_file: str,
    source_identity: str,
    local_path: bool = True,
    source_aliases: Iterable[Union[str, os.PathLike]] = (),
) -> ManagedSourceSnapshot:
    """Read an exact rollback snapshot before a managed purge."""
    item_ids = _managed_source_row_ids(
        collection,
        source_file=source_file,
        source_identity=source_identity,
        local_path=local_path,
        source_aliases=source_aliases,
    )
    rows = _collection_rows_for_ids(collection, item_ids, include_embeddings=True)
    row_embeddings = tuple(rows[item_id][2] for item_id in item_ids)
    if any(item is not None for item in row_embeddings) and not all(
        item is not None for item in row_embeddings
    ):
        raise ReceiptIdentityError("collection snapshot returned incomplete embeddings")
    embeddings = (
        tuple(item for item in row_embeddings if item is not None)
        if row_embeddings and all(item is not None for item in row_embeddings)
        else None
    )
    return ManagedSourceSnapshot(
        ids=tuple(item_ids),
        documents=tuple(rows[item_id][0] for item_id in item_ids),
        metadatas=tuple(rows[item_id][1] for item_id in item_ids),
        embeddings=embeddings,
    )


def _validate_managed_source_snapshot_current(
    collection: Any,
    snapshot: ManagedSourceSnapshot,
    *,
    recovery_path: Union[str, os.PathLike],
    collection_name: str,
    source_file: str,
    source_identity: str,
    local_path: bool = True,
    source_aliases: Iterable[Union[str, os.PathLike]] = (),
) -> _ManagedPurgeCapability:
    """Issue private purge authority only for an exact durable snapshot."""
    recovery = _load_rewrite_recovery(
        Path(recovery_path),
        expected_source_identity=source_identity,
    )
    stored_snapshot = _recovery_snapshot_for_collection(
        recovery,
        collection_name,
        snapshot,
    )
    requested_selectors = ManagedSourceSelectors(
        source_files=_validated_source_aliases(
            source_file,
            local_path=local_path,
            source_aliases=source_aliases,
        )
    )
    if requested_selectors != recovery.selectors or not recovery.selector_coverage_complete:
        raise ReceiptRecoveryError(
            "purge selectors do not exactly match durable recovery authority"
        )
    palace_path = _palace_path_from_recovery_path(recovery.path)
    write_scope = _require_managed_write_scope(palace_path)
    expected = tuple(sorted(snapshot.ids))
    current = tuple(
        _managed_source_row_ids(
            collection,
            source_file=source_file,
            source_identity=source_identity,
            local_path=local_path,
            source_aliases=source_aliases,
        )
    )
    if current != expected:
        added = len(set(current) - set(expected))
        removed = len(set(expected) - set(current))
        raise ReceiptRecoveryError(
            "managed source row set changed after durable snapshot publication; "
            f"refusing purge (added={added}, removed={removed})"
        )
    rows = _collection_rows_for_ids(collection, list(current), include_embeddings=True)
    validated_rows = []
    snapshot_indexes = {item_id: index for index, item_id in enumerate(snapshot.ids)}
    for item_id in current:
        index = snapshot_indexes[item_id]
        row = rows[item_id]
        if not _row_matches_snapshot(row, stored_snapshot, index):
            raise ReceiptRecoveryError(
                "managed source row content changed after durable snapshot publication; "
                "refusing purge"
            )
        validated_rows.append(_validated_collection_row(item_id, row))
    return _ManagedPurgeCapability(
        authority=_PURGE_AUTHORITY,
        lock_nonce=write_scope.nonce,
        palace_path=palace_path,
        collection_identity=id(collection),
        recovery_path=recovery.path,
        receipt_id=recovery.receipt_id,
        source_identity=recovery.source_identity,
        collection_name=collection_name,
        snapshot_digest=_snapshot_payload(stored_snapshot)["manifest_digest"],
        rows=tuple(validated_rows),
    )


def purge_managed_source_snapshot(
    collection: Any,
    snapshot: ManagedSourceSnapshot,
    *,
    recovery_path: Union[str, os.PathLike],
    collection_name: str,
    source_file: str,
    source_identity: str,
    local_path: bool = True,
    source_aliases: Iterable[Union[str, os.PathLike]] = (),
) -> list[str]:
    """Conditionally delete one exact snapshot under durable recovery authority."""
    capability = _validate_managed_source_snapshot_current(
        collection,
        snapshot,
        recovery_path=recovery_path,
        collection_name=collection_name,
        source_file=source_file,
        source_identity=source_identity,
        local_path=local_path,
        source_aliases=source_aliases,
    )
    deleted = _delete_validated_collection_rows(collection, capability)
    remaining = _managed_source_row_ids(
        collection,
        source_file=source_file,
        source_identity=source_identity,
        local_path=local_path,
        source_aliases=source_aliases,
    )
    if remaining:
        raise ReceiptRecoveryError(
            "managed source changed during conditional purge; recovery remains required "
            f"(remaining={len(remaining)})"
        )
    return deleted


def rollback_managed_source_rows(
    collection: Any,
    snapshot: ManagedSourceSnapshot,
    *,
    recovery_path: Union[str, os.PathLike],
    collection_name: str,
    source_identity: str,
    receipt_id: str,
) -> None:
    """Remove only the validated interrupted attempt and exactly restore baseline."""
    _require_managed_write_scope(_palace_path_from_recovery_path(Path(recovery_path)))
    recovery = _load_rewrite_recovery(
        Path(recovery_path),
        expected_source_identity=source_identity,
    )
    if recovery.receipt_id != receipt_id:
        raise ReceiptRecoveryError("rollback receipt does not match durable recovery")
    stored_snapshot = _recovery_snapshot_for_collection(
        recovery,
        collection_name,
        snapshot,
    )
    capability = _validate_recovery_collection_state(
        collection,
        stored_snapshot,
        recovery=recovery,
        collection_name=collection_name,
    )
    _delete_validated_collection_rows(collection, capability)
    _restore_missing_managed_source_rows(collection, stored_snapshot)
    _verify_restored_snapshot(
        collection,
        stored_snapshot,
        recovery=recovery,
    )


def write_receipted_collection_batch(
    collection: Any,
    method: str,
    kwargs: Mapping[str, Any],
    *,
    session: SourceWriteReceiptSession,
    source_file: str,
    collection_name: str = "drawers",
    kind: str = "drawer",
    local_path: bool = False,
) -> Any:
    """Validate, stamp, persist, and manifest one managed collection batch."""
    _require_managed_write_scope(session.store.palace_path)
    if method not in {"add", "upsert", "update"}:
        raise ReceiptIdentityError(f"unsupported managed collection method: {method!r}")
    canonical = canonical_source_locator(source_file, local_path=local_path)
    if (
        session.store.source_identity(canonical, local_path=local_path)
        != session.source["identity"]
    ):
        raise ReceiptIdentityError("managed collection source does not match its active receipt")

    ids = kwargs.get("ids")
    documents = kwargs.get("documents")
    metadatas = kwargs.get("metadatas")
    if not isinstance(ids, (list, tuple)) or not isinstance(documents, (list, tuple)):
        raise ReceiptIdentityError("managed collection writes require ids and documents")
    if not ids or len(ids) != len(documents):
        raise ReceiptIdentityError("managed collection ids and documents must align")
    if any(not isinstance(item_id, str) for item_id in ids) or any(
        not isinstance(document, str) for document in documents
    ):
        raise ReceiptIdentityError("managed collection ids and documents must be text")
    if len(set(ids)) != len(ids):
        raise ReceiptIdentityError("managed collection ids must be unique within a batch")
    if metadatas is None:
        resolved_metadatas = [{} for _ in ids]
    elif isinstance(metadatas, (list, tuple)) and len(metadatas) == len(ids):
        try:
            resolved_metadatas = [dict(metadata or {}) for metadata in metadatas]
        except (TypeError, ValueError) as exc:
            raise ReceiptIdentityError("managed collection metadata must be mappings") from exc
    else:
        raise ReceiptIdentityError("managed collection metadata must align with ids")
    embeddings = kwargs.get("embeddings")
    if embeddings is not None and (
        not isinstance(embeddings, (list, tuple)) or len(embeddings) != len(ids)
    ):
        raise ReceiptIdentityError("managed collection embeddings must align with ids")

    existing_rows = _assert_managed_write_ids_owned_by_source(
        collection,
        list(ids),
        canonical_source=canonical,
        source_identity=session.source["identity"],
        local_path=local_path,
    )

    stamped = []
    for item_id, document, metadata in zip(ids, documents, resolved_metadatas):
        session.validate_output(
            item_id,
            document,
            collection=collection_name,
            kind=kind,
        )
        metadata["source_file"] = canonical
        stamped.append(stamp_output_metadata(metadata, session, document))

    call_kwargs = dict(kwargs)
    call_kwargs["ids"] = list(ids)
    call_kwargs["documents"] = list(documents)
    call_kwargs["metadatas"] = stamped
    result = _write_managed_collection_batch(
        collection,
        method,
        call_kwargs,
        existing_rows=existing_rows,
    )
    _verify_managed_write_readback(collection, call_kwargs)
    for item_id, document in zip(ids, documents):
        session.record_output(
            item_id,
            document,
            collection=collection_name,
            kind=kind,
        )
    session.record_batch()
    return result


def complete_reused_receipt(
    session: SourceWriteReceiptSession,
    prior: Mapping[str, Any],
    *,
    collections: Mapping[str, Any],
    source_file: str,
    local_path: bool,
    source_aliases: Iterable[Union[str, os.PathLike]] = (),
) -> dict:
    """Restamp unchanged rows to the new terminal producer under recovery."""
    _require_managed_write_scope(session.store.palace_path)
    if not collections:
        raise ReceiptIdentityError("reused receipt requires managed collections")
    prior_id = _require_uuid(prior.get("receipt_id"), "reused receipt id")
    if not _is_complete_event_for_source(prior, session.source["identity"]):
        raise ReceiptIdentityError("reused receipt is not a valid COMPLETE predecessor")

    output_collections = {
        item["collection"] for item in _normalize_outputs(prior["outputs"]["identities"])
    }
    unknown = sorted(output_collections - set(collections))
    if unknown:
        raise ReceiptIdentityError(
            f"reused receipt collections are unavailable: {', '.join(unknown)}"
        )

    canonical = canonical_source_locator(source_file, local_path=local_path)
    if (
        session.store.source_identity(canonical, local_path=local_path)
        != session.source["identity"]
    ):
        raise ReceiptIdentityError("reused receipt source does not match the active session")

    recovery_path: Optional[Path] = None
    attempted: list[str] = []
    snapshots: dict[str, ManagedSourceSnapshot] = {}
    try:
        session.reuse(prior)
        session.running("snapshotting-unchanged")
        snapshots = {
            name: snapshot_managed_source_rows(
                collection,
                source_file=canonical,
                source_identity=session.source["identity"],
                local_path=local_path,
                source_aliases=source_aliases,
            )
            for name, collection in sorted(collections.items())
        }
        recovery_path = session.store.prepare_rewrite_recovery(
            session=session,
            snapshots=snapshots,
            source_file=canonical,
            local_path=local_path,
            source_aliases=source_aliases,
            previous_receipt=prior,
        )
        session.running("recovery-prepared")
        session.running("rebinding-unchanged-producer")
        for name, collection in sorted(collections.items()):
            attempted.append(name)
            _rebind_managed_source_snapshot(
                collection,
                snapshots[name],
                session=session,
                prior_receipt_id=prior_id,
                recovery_path=recovery_path,
                collection_name=name,
            )
        event = session.complete()
        session.store.finalize_rewrite_recovery(
            session.source["identity"],
            session.receipt_id,
            collections=collections,
        )
        return event
    except BaseException as exc:
        if session.state not in TERMINAL_STATES and recovery_path is not None:
            rollback_failures = []
            for name in reversed(attempted):
                try:
                    rollback_managed_source_rows(
                        collections[name],
                        snapshots[name],
                        recovery_path=recovery_path,
                        collection_name=name,
                        source_identity=session.source["identity"],
                        receipt_id=session.receipt_id,
                    )
                except Exception as rollback_exc:
                    rollback_failures.append(rollback_exc)
            if not rollback_failures:
                try:
                    session.store.discard_rewrite_recovery(
                        session.source["identity"],
                        session.receipt_id,
                        collections=collections,
                    )
                except Exception as rollback_exc:
                    rollback_failures.append(rollback_exc)
            if rollback_failures:
                error = ReceiptError("unchanged producer rebind rollback failed")
                session.fail(error, stage="reuse-producer-rollback")
                raise error from rollback_failures[0]
        if session.state not in TERMINAL_STATES:
            if isinstance(exc, Exception):
                session.fail(exc, stage="reuse-producer-rebind")
            else:
                session.abort(exc, stage="reuse-producer-rebind")
        raise


def _rebind_managed_source_snapshot(
    collection: Any,
    snapshot: ManagedSourceSnapshot,
    *,
    session: SourceWriteReceiptSession,
    prior_receipt_id: str,
    recovery_path: Union[str, os.PathLike],
    collection_name: str,
) -> None:
    """Change only producer metadata and prove all row fields after update."""
    _require_managed_write_scope(session.store.palace_path)
    prior_id = _require_uuid(prior_receipt_id, "reused receipt id")
    recovery = _load_rewrite_recovery(
        Path(recovery_path),
        expected_source_identity=session.source["identity"],
    )
    if recovery.receipt_id != session.receipt_id:
        raise ReceiptRecoveryError("producer rebind receipt does not match durable recovery")
    stored_snapshot = _recovery_snapshot_for_collection(recovery, collection_name, snapshot)
    expected_outputs = {
        item["id"]: item for item in session.outputs if item["collection"] == collection_name
    }
    if set(expected_outputs) != set(stored_snapshot.ids):
        raise ReceiptRecoveryError("reused manifest does not exactly match current source rows")
    if not stored_snapshot.ids:
        return

    current = _collection_rows_for_ids(
        collection,
        list(stored_snapshot.ids),
        include_embeddings=True,
    )
    updated_metadatas = []
    for index, item_id in enumerate(stored_snapshot.ids):
        if not _row_matches_snapshot(current[item_id], stored_snapshot, index):
            raise ReceiptRecoveryError("unchanged source row changed before producer rebind")
        document, metadata, _ = current[item_id]
        output = expected_outputs[item_id]
        required = {
            META_RECEIPT_ID: prior_id,
            META_SOURCE_IDENTITY: session.source["identity"],
            META_SOURCE_CONTENT_HASH: session.source["content_hash"],
            META_SOURCE_VERSION_HASH: session.source["version_hash"],
            META_OUTPUT_CONTENT_HASH: output["content_hash"],
        }
        if sha256_bytes(document.encode("utf-8")) != output["content_hash"] or any(
            metadata.get(key) != value for key, value in required.items()
        ):
            raise ReceiptRecoveryError("unchanged source row does not match reused receipt")
        rebound = dict(metadata)
        rebound[META_RECEIPT_ID] = session.receipt_id
        updated_metadatas.append(rebound)

    collection.update(ids=list(stored_snapshot.ids), metadatas=updated_metadatas)
    readback = {
        "ids": list(stored_snapshot.ids),
        "documents": list(stored_snapshot.documents),
        "metadatas": updated_metadatas,
    }
    if stored_snapshot.embeddings is not None:
        readback["embeddings"] = [list(item) for item in stored_snapshot.embeddings]
    _verify_managed_write_readback(collection, readback)


def shared_receipt_projection(receipt: Mapping[str, Any]) -> dict:
    """Return a pseudonymized projection with exact store IDs removed.

    The projection is suitable for bounded sharing with trusted reviewers, not
    anonymous publication: timestamps, UUIDs, producer identity, and counts can
    still correlate activity within an artifact set.
    """
    if receipt.get("schema") != RECEIPT_SCHEMA:
        raise ReceiptIdentityError("unsupported or missing receipt schema")
    receipt_id = _require_uuid(receipt.get("receipt_id"), "receipt id")
    event_id = _require_uuid(receipt.get("event_id"), "event id")
    sequence = receipt.get("sequence")
    if not isinstance(sequence, int) or sequence < 0:
        raise ReceiptIdentityError("receipt sequence is required")
    if event_id != receipt_event_id(receipt_id, sequence):
        raise ReceiptIdentityError("event id is not bound to its receipt sequence")
    state = receipt.get("state")
    if state not in RECEIPT_STATES:
        raise ReceiptIdentityError("receipt state is missing or invalid")
    event_time = _require_timestamp(receipt.get("event_time"), "event time")
    stage = _require_token(receipt.get("stage"), "receipt stage")
    disposition = _require_token(receipt.get("disposition"), "receipt disposition")

    run = _require_mapping(receipt.get("run"), "run")
    run_id = _require_uuid(run.get("id"), "run id")
    caller = _require_mapping(run.get("caller"), "run caller")
    caller_identity = _require_hmac(caller.get("identity"), "caller identity")
    mode = _require_token(run.get("mode"), "run mode")

    producer = _require_mapping(receipt.get("producer"), "producer")
    package = _require_mapping(producer.get("package"), "package identity")
    package_name = _require_token(package.get("name"), "package name")
    package_version = _require_token(package.get("version"), "package version")
    package_digest = _require_sha256(package.get("source_digest"), "package source digest")
    git = _require_mapping(producer.get("git"), "git identity")
    git_state = _require_token(git.get("state"), "git state")
    if git_state not in {"available", "build-metadata", "unavailable"}:
        raise ReceiptIdentityError("git state is invalid")
    git_commit = git.get("commit")
    if git_state != "unavailable":
        git_commit = _require_token(git_commit, "git commit")
    elif git_commit is not None:
        raise ReceiptIdentityError("unavailable git identity cannot contain a commit")
    git_dirty = git.get("dirty")
    if git_dirty is not None and not isinstance(git_dirty, bool):
        raise ReceiptIdentityError("git dirty state is invalid")
    config = _require_mapping(producer.get("config"), "config identity")
    config_digest = _require_sha256(config.get("digest"), "config digest")

    source = _require_mapping(receipt.get("source"), "source identity")
    _require_hmac(source.get("identity"), "source identity")
    _require_sha256(source.get("content_hash"), "source content hash")
    _require_sha256(source.get("version_hash"), "source version hash")
    shared_content_identity = _require_hmac(
        source.get("shared_content_identity"), "shared source content identity"
    )
    shared_version_identity = _require_hmac(
        source.get("shared_version_identity"), "shared source version identity"
    )
    source_size = source.get("size_bytes")
    if not isinstance(source_size, int) or source_size < 0:
        raise ReceiptIdentityError("source size is required")
    size_bucket = _require_token(source.get("size_bucket"), "source size bucket")
    if size_bucket != source_size_bucket(source_size):
        raise ReceiptIdentityError("source size bucket does not match exact source size")
    adapter = _require_mapping(source.get("adapter"), "adapter identity")
    adapter_name = _require_token(adapter.get("name"), "adapter name")
    adapter_version = _require_token(adapter.get("version"), "adapter version")

    outputs = _require_mapping(receipt.get("outputs"), "output manifest")
    identities = outputs.get("identities")
    if not isinstance(identities, list):
        raise ReceiptIdentityError("exact output identities are required for projection")
    normalized_outputs = _normalize_outputs(identities)
    if receipt.get("state") == "COMPLETE" and any(
        item["producer_receipt_id"] != receipt_id for item in normalized_outputs
    ):
        raise ReceiptIdentityError("COMPLETE manifest contains a foreign producer receipt")
    output_count = outputs.get("count")
    if not isinstance(output_count, int) or output_count != len(normalized_outputs):
        raise ReceiptIdentityError("output manifest count does not match identities")
    _require_sha256(outputs.get("manifest_digest"), "manifest digest")
    if manifest_digest(normalized_outputs) != outputs["manifest_digest"]:
        raise ReceiptIdentityError("output manifest digest does not match identities")

    counts = _shared_counts(receipt.get("counts"))
    errors = _shared_errors(receipt.get("errors"))
    if counts.get("errors") != len(errors):
        raise ReceiptIdentityError("error count does not match error records")
    relations = validate_receipt_relations(receipt)
    return {
        "schema": RECEIPT_SCHEMA,
        "projection": "pseudonymized-shared",
        "receipt_id": receipt_id,
        "event_id": event_id,
        "sequence": sequence,
        "state": state,
        "event_time": event_time,
        "stage": stage,
        "disposition": disposition,
        "run": {
            "id": run_id,
            "caller": {"identity": caller_identity},
            "mode": mode,
        },
        "producer": {
            "package": {
                "name": package_name,
                "version": package_version,
                "source_digest": package_digest,
            },
            "git": {"state": git_state, "commit": git_commit, "dirty": git_dirty},
            "config": {"digest": config_digest},
        },
        "source": {
            "identity": source["identity"],
            "content_identity": shared_content_identity,
            "version_identity": shared_version_identity,
            "size_bucket": size_bucket,
            "adapter": {"name": adapter_name, "version": adapter_version},
        },
        "outputs": {
            "count": output_count,
            "manifest_digest": outputs["manifest_digest"],
        },
        "counts": counts,
        "errors": errors,
        "relations": relations,
    }


def _shared_counts(value: Any) -> dict:
    counts = _require_mapping(value, "receipt counts")
    safe = {}
    for key, count in counts.items():
        safe_key = _require_token(key, "receipt count name")
        if not isinstance(count, int) or count < 0:
            raise ReceiptIdentityError(f"receipt count {safe_key!r} is invalid")
        if safe_key == "source_bytes":
            continue
        safe[safe_key] = count
    return safe


def _shared_errors(value: Any) -> list[dict]:
    if not isinstance(value, list):
        raise ReceiptIdentityError("receipt errors must be a list")
    safe = []
    for error in value:
        error = _require_mapping(error, "receipt error")
        safe.append(
            {
                "type": _require_token(error.get("type"), "error type"),
                "stage": _require_token(error.get("stage"), "error stage"),
                "message_identity": _require_hmac(
                    error.get("shared_message_identity"), "shared error message identity"
                ),
            }
        )
    return safe


def validate_receipt_relations(event: Mapping[str, Any]) -> dict:
    """Validate receipt lineage and return its privacy-safe projection."""
    receipt_id = _require_uuid(event.get("receipt_id"), "receipt id")
    state = event.get("state")
    if state not in RECEIPT_STATES:
        raise ReceiptIdentityError("receipt state is missing or invalid")
    disposition = _require_token(event.get("disposition"), "receipt disposition")
    relations = _require_mapping(event.get("relations"), "receipt relations")
    allowed = {
        "predecessor_receipt_id",
        "reuses_receipt_id",
        "supersedes",
        "invalidations",
        "invalidation_records",
        "legacy_missing_predecessor_compatibility",
    }
    unknown = set(relations) - allowed
    if unknown:
        raise ReceiptIdentityError(
            f"receipt relations contain unsupported fields: {sorted(unknown)!r}"
        )

    predecessor_ids = _receipt_predecessor_ids(event)
    if receipt_id in predecessor_ids:
        raise ReceiptIdentityError("receipt cannot be its own predecessor")
    safe = {}
    if "predecessor_receipt_id" in relations:
        safe["predecessor_receipt_id"] = _require_uuid(
            relations["predecessor_receipt_id"], "predecessor receipt id"
        )
    if "reuses_receipt_id" in relations:
        safe["reuses_receipt_id"] = _require_uuid(
            relations["reuses_receipt_id"], "reused receipt id"
        )
    if "supersedes" in relations:
        supersedes = _require_mapping(relations["supersedes"], "supersession")
        safe["supersedes"] = {
            "receipt_id": _require_uuid(supersedes.get("receipt_id"), "superseded receipt id"),
            "manifest_digest": _require_sha256(
                supersedes.get("manifest_digest"), "superseded manifest digest"
            ),
            "reason": _require_text(supersedes.get("reason"), "supersession reason"),
        }
    if "invalidations" in relations:
        invalidations = relations["invalidations"]
        if not isinstance(invalidations, list):
            raise ReceiptIdentityError("receipt invalidations must be a list")
        safe["invalidations"] = [_require_uuid(item, "invalidation id") for item in invalidations]
    if "legacy_missing_predecessor_compatibility" in relations:
        compatibility = _require_text(
            relations["legacy_missing_predecessor_compatibility"],
            "legacy predecessor compatibility",
        )
        if compatibility != LEGACY_MISSING_PREDECESSOR_COMPATIBILITY:
            raise ReceiptIdentityError("legacy predecessor compatibility is unsupported")
        safe["legacy_missing_predecessor_compatibility"] = compatibility
    records = _planned_invalidation_records(event)
    record_ids = [record["invalidation_id"] for record in records]
    if len(record_ids) != len(set(record_ids)):
        raise ReceiptIdentityError("receipt invalidation records contain duplicate identities")
    if set(record_ids) != set(safe.get("invalidations", [])):
        raise ReceiptIdentityError("receipt invalidation identities do not match their records")

    reuses_id = safe.get("reuses_receipt_id")
    supersedes = safe.get("supersedes")
    if disposition == "UNCHANGED":
        if reuses_id is None:
            raise ReceiptIdentityError("UNCHANGED receipt must identify the reused predecessor")
        if supersedes is not None or records:
            raise ReceiptIdentityError("UNCHANGED receipt cannot supersede or invalidate output")
    elif reuses_id is not None:
        raise ReceiptIdentityError("only UNCHANGED receipts can declare reuse")

    if supersedes is None and records:
        raise ReceiptIdentityError("receipt invalidation records require a superseded receipt")
    if supersedes is not None:
        superseded_id = supersedes["receipt_id"]
        superseded_manifest = supersedes["manifest_digest"]
        for record in records:
            if (
                record["invalidated_receipt_id"] != superseded_id
                or record["invalidated_manifest_digest"] != superseded_manifest
                or record["by_receipt_id"] != receipt_id
            ):
                raise ReceiptIdentityError(
                    "receipt invalidation record does not match its supersession"
                )
    if "legacy_missing_predecessor_compatibility" in safe and not predecessor_ids:
        raise ReceiptIdentityError("legacy predecessor compatibility requires a predecessor")
    return safe


def _managed_source_row_ids(
    collection: Any,
    *,
    source_file: str,
    source_identity: str,
    local_path: bool,
    source_aliases: Iterable[Union[str, os.PathLike]],
) -> list[str]:
    _require_hmac(source_identity, "source identity")
    aliases = _validated_source_aliases(
        source_file,
        local_path=local_path,
        source_aliases=source_aliases,
    )
    return _managed_source_row_ids_for_selectors(
        collection,
        selectors=ManagedSourceSelectors(source_files=aliases),
        source_identity=source_identity,
    )


def _managed_source_row_ids_for_selectors(
    collection: Any,
    *,
    selectors: ManagedSourceSelectors,
    source_identity: str,
) -> list[str]:
    """Resolve exact source selectors and reject contradictory ownership."""
    _require_hmac(source_identity, "source identity")
    alias_ids: set[str] = set()
    for alias in selectors.source_files:
        _require_text(alias, "recovery source-file selector")
        alias_ids.update(_collection_ids_for_where(collection, {"source_file": alias}))
    identity_ids = set(
        _collection_ids_for_where(collection, {META_SOURCE_IDENTITY: source_identity})
    )
    item_ids = alias_ids | identity_ids
    if item_ids:
        rows = _collection_rows_for_ids(collection, sorted(item_ids), require_all=False)
        if set(rows) != item_ids:
            raise ReceiptIdentityError("source-selected rows changed during ownership validation")
        allowed_source_files = set(selectors.source_files)
        for item_id, (_, metadata, _) in rows.items():
            owner = metadata.get(META_SOURCE_IDENTITY)
            if owner is not None:
                try:
                    _require_hmac(owner, "source-selected row identity")
                except ReceiptIdentityError as exc:
                    raise ReceiptConflictError(
                        f"source-selected row has invalid ownership: {item_id}"
                    ) from exc
                if owner != source_identity:
                    raise ReceiptConflictError(
                        f"source-selected row is owned by another source: {item_id}"
                    )
            if item_id in identity_ids:
                source_file = metadata.get("source_file")
                if owner != source_identity or not _source_file_matches_selectors(
                    source_file,
                    allowed_source_files,
                ):
                    raise ReceiptConflictError(
                        f"identity-selected row is owned by another source file: {item_id}"
                    )
    return sorted(item_ids)


def _source_file_matches_selectors(source_file: Any, selectors: set[str]) -> bool:
    if not isinstance(source_file, str) or not source_file:
        return False
    if source_file in selectors:
        return True
    if not os.path.isabs(source_file):
        return False
    try:
        canonical = os.path.normcase(canonical_source_locator(source_file, local_path=True))
        return any(
            os.path.isabs(selector)
            and os.path.normcase(canonical_source_locator(selector, local_path=True)) == canonical
            for selector in selectors
        )
    except (OSError, ReceiptIdentityError, TypeError, ValueError):
        return False


def _validated_source_aliases(
    source_file: str,
    *,
    local_path: bool,
    source_aliases: Iterable[Union[str, os.PathLike]],
) -> tuple[str, ...]:
    canonical = canonical_source_locator(source_file, local_path=local_path)
    aliases = {canonical}
    for raw_alias in source_aliases:
        alias = os.fspath(raw_alias)
        _require_text(alias, "source alias")
        if local_path:
            alias_canonical = canonical_source_locator(alias, local_path=True)
            if os.path.normcase(alias_canonical) != os.path.normcase(canonical):
                raise ReceiptIdentityError("source alias resolves to a different local path")
            expanded = os.path.expanduser(alias)
            aliases.update(
                {
                    alias,
                    os.path.normpath(expanded),
                    os.path.abspath(expanded),
                    os.path.normcase(os.path.abspath(expanded)),
                }
            )
        else:
            if alias.strip() != canonical:
                raise ReceiptIdentityError("source alias does not match the canonical locator")
            aliases.add(alias)
    return tuple(sorted(aliases))


def _collection_rows_for_ids(
    collection: Any,
    item_ids: list[str],
    *,
    require_all: bool = True,
    include_embeddings: bool = False,
) -> dict[str, tuple[str, dict, Optional[tuple[Any, ...]]]]:
    """Read stable row identity, fetching embeddings only when explicitly required."""
    rows: dict[str, tuple[str, dict, Optional[tuple[Any, ...]]]] = {}
    for start in range(0, len(item_ids), 1000):
        expected = item_ids[start : start + 1000]
        result = collection.get(ids=expected, include=["documents", "metadatas"])
        try:
            ids = list(result.get("ids") or [])
            documents = list(result.get("documents") or [])
            metadatas = list(result.get("metadatas") or [])
        except (AttributeError, TypeError, ValueError) as exc:
            raise ReceiptIdentityError("collection returned an invalid snapshot result") from exc
        if (
            len(documents) != len(ids)
            or len(metadatas) != len(ids)
            or len(set(ids)) != len(ids)
            or not set(ids).issubset(expected)
            or (require_all and set(ids) != set(expected))
        ):
            raise ReceiptIdentityError("managed source changed while its snapshot was read")
        embeddings = (
            _collection_embeddings_for_ids(collection, ids) if include_embeddings and ids else {}
        )
        for item_id, document, metadata in zip(ids, documents, metadatas):
            if not isinstance(item_id, str) or not isinstance(document, str):
                raise ReceiptIdentityError("collection snapshot ids and documents must be text")
            try:
                rows[item_id] = (document, dict(metadata or {}), embeddings.get(item_id))
            except (TypeError, ValueError) as exc:
                raise ReceiptIdentityError(
                    "collection snapshot metadata or embeddings are invalid"
                ) from exc
    return rows


def _collection_embeddings_for_ids(
    collection: Any,
    item_ids: list[str],
) -> dict[str, Optional[tuple[Any, ...]]]:
    """Read exact embeddings separately from deterministic document/metadata proof."""
    exact_reader = getattr(collection, "get_exact_embeddings", None)
    if callable(exact_reader):
        from .backends.base import EmbeddingVisibilityError

        try:
            return exact_reader(item_ids)
        except EmbeddingVisibilityError as exc:
            raise ReceiptIdentityError(
                "collection exact embeddings are not yet available through its supported API"
            ) from exc
    result = collection.get(ids=item_ids, include=["embeddings"])
    try:
        ids = list(result.get("ids") or [])
        raw_embeddings = result.get("embeddings")
        embeddings = [None for _ in ids] if raw_embeddings is None else list(raw_embeddings)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ReceiptIdentityError("collection returned an invalid embedding result") from exc
    if len(embeddings) != len(ids) or len(set(ids)) != len(ids) or set(ids) != set(item_ids):
        raise ReceiptIdentityError("managed source changed while its embeddings were read")
    try:
        return {
            item_id: None if embedding is None else tuple(embedding)
            for item_id, embedding in zip(ids, embeddings)
        }
    except (TypeError, ValueError) as exc:
        raise ReceiptIdentityError("collection snapshot embeddings are invalid") from exc


def _assert_managed_write_ids_owned_by_source(
    collection: Any,
    item_ids: list[str],
    *,
    canonical_source: str,
    source_identity: str,
    local_path: bool,
) -> dict[str, tuple[str, dict, Optional[tuple[Any, ...]]]]:
    """Reject a managed write before it can overwrite another source's row."""
    existing = _collection_rows_for_ids(
        collection,
        item_ids,
        require_all=False,
        include_embeddings=True,
    )
    for item_id, (_, metadata, _) in existing.items():
        owner = metadata.get(META_SOURCE_IDENTITY)
        raw_source = metadata.get("source_file")
        try:
            if owner is not None:
                _require_hmac(owner, "existing row source identity")
                if owner != source_identity:
                    raise ReceiptConflictError(
                        f"managed write ID is already owned by another source: {item_id}"
                    )
            if not isinstance(raw_source, str) or not raw_source:
                raise ReceiptConflictError(
                    f"managed write ID has no verifiable source owner: {item_id}"
                )
            existing_source = canonical_source_locator(raw_source, local_path=local_path)
            if local_path:
                same_source = os.path.normcase(existing_source) == os.path.normcase(
                    canonical_source
                )
            else:
                same_source = existing_source == canonical_source
            if not same_source:
                raise ReceiptConflictError(
                    f"managed write ID is already owned by another source: {item_id}"
                )
        except ReceiptConflictError:
            raise
        except (OSError, ReceiptIdentityError, TypeError, ValueError) as exc:
            raise ReceiptConflictError(f"managed write ID ownership is invalid: {item_id}") from exc
    return existing


def _write_managed_collection_batch(
    collection: Any,
    method: str,
    kwargs: Mapping[str, Any],
    *,
    existing_rows: Mapping[str, tuple[str, dict, Optional[tuple[Any, ...]]]],
) -> Any:
    """Recheck exact existing identity and avoid unconditional upsert overwrites."""
    ids = list(kwargs["ids"])
    existing_ids = set(existing_rows)
    if method == "add" and existing_ids:
        raise ReceiptConflictError("managed add cannot replace an existing ID")
    if method == "update" and existing_ids != set(ids):
        raise ReceiptConflictError("managed update requires every ID to exist")

    current_rows = _collection_rows_for_ids(
        collection,
        ids,
        require_all=False,
        include_embeddings=True,
    )
    if set(current_rows) != existing_ids or any(
        current_rows[item_id] != existing_rows[item_id] for item_id in existing_ids
    ):
        raise ReceiptConflictError(
            "managed write target identity changed before mutation; refusing overwrite"
        )

    if method != "upsert":
        return getattr(collection, method)(**dict(kwargs))

    results = []
    for selected_method, selected_ids in (
        ("update", existing_ids),
        ("add", set(ids) - existing_ids),
    ):
        if not selected_ids:
            continue
        indexes = [index for index, item_id in enumerate(ids) if item_id in selected_ids]
        selected = {
            key: [value[index] for index in indexes]
            for key, value in kwargs.items()
            if isinstance(value, (list, tuple)) and len(value) == len(ids)
        }
        for key, value in kwargs.items():
            if key not in selected and key not in {"ids", "documents", "metadatas", "embeddings"}:
                selected[key] = value
        results.append(getattr(collection, selected_method)(**selected))
    return results[-1] if len(results) == 1 else tuple(results)


def _verify_managed_write_readback(collection: Any, kwargs: Mapping[str, Any]) -> None:
    ids = list(kwargs["ids"])
    documents = list(kwargs["documents"])
    metadatas = [dict(item) for item in kwargs["metadatas"]]
    embeddings = kwargs.get("embeddings")
    started = time.monotonic()
    deadline = started + _MANAGED_WRITE_READBACK_TIMEOUT_SECONDS
    retry_seconds = _MANAGED_WRITE_READBACK_INITIAL_RETRY_SECONDS
    attempts = 0
    last_error: Optional[BaseException] = None
    while True:
        attempts += 1
        try:
            rows = _collection_rows_for_ids(collection, ids)
            actual_embeddings = (
                _collection_embeddings_for_ids(collection, ids) if embeddings is not None else {}
            )
            mismatch = None
            for index, item_id in enumerate(ids):
                document, metadata, _ = rows[item_id]
                if document != documents[index] or metadata != metadatas[index]:
                    mismatch = "managed write exact readback did not match"
                    break
                if embeddings is not None:
                    expected_embedding = tuple(embeddings[index])
                    if actual_embeddings[item_id] != expected_embedding:
                        mismatch = "managed write embedding readback did not match"
                        break
            if mismatch is None:
                return
            last_error = ReceiptRecoveryError(mismatch)
        except ReceiptIdentityError as exc:
            last_error = exc

        if time.monotonic() >= deadline:
            message = (
                str(last_error)
                if isinstance(last_error, ReceiptRecoveryError)
                else "managed write exact readback did not stabilize"
            )
            elapsed = time.monotonic() - started
            raise ReceiptRecoveryError(
                f"{message} after {attempts} attempts in {elapsed:.3f}s"
            ) from last_error
        remaining = max(0.0, deadline - time.monotonic())
        time.sleep(min(retry_seconds, remaining))
        retry_seconds = min(
            retry_seconds * _MANAGED_WRITE_READBACK_BACKOFF,
            _MANAGED_WRITE_READBACK_MAX_RETRY_SECONDS,
        )


def _recovery_snapshot_for_collection(
    recovery: ManagedRewriteRecovery,
    collection_name: str,
    snapshot: ManagedSourceSnapshot,
) -> ManagedSourceSnapshot:
    safe_name = _require_token(collection_name, "recovery collection name")
    stored = recovery.snapshots.get(safe_name)
    if stored is None:
        raise ReceiptRecoveryError("durable recovery does not authorize this collection")
    if _canonical_json_bytes(_snapshot_payload(stored)) != _canonical_json_bytes(
        _snapshot_payload(snapshot)
    ):
        raise ReceiptRecoveryError("durable recovery snapshot does not match purge request")
    return stored


def _row_matches_snapshot(
    row: tuple[str, dict, Optional[tuple[Any, ...]]],
    snapshot: ManagedSourceSnapshot,
    index: int,
) -> bool:
    document, metadata, embedding = row
    expected_embedding = snapshot.embeddings[index] if snapshot.embeddings is not None else None
    return (
        document == snapshot.documents[index]
        and metadata == snapshot.metadatas[index]
        and embedding == expected_embedding
    )


def _validated_collection_row(
    item_id: str,
    row: tuple[str, dict, Optional[tuple[Any, ...]]],
) -> _ValidatedCollectionRow:
    document, metadata, embedding = row
    return _ValidatedCollectionRow(
        item_id=item_id,
        document=document,
        metadata=dict(metadata),
        embedding=embedding,
    )


def _delete_filters_for_validated_row(
    row: _ValidatedCollectionRow,
) -> tuple[dict, Optional[dict]]:
    metadata = dict(row.metadata)
    conditions = []
    source_identity = metadata.get(META_SOURCE_IDENTITY)
    source_file = metadata.get("source_file")
    if source_identity is not None:
        try:
            _require_hmac(source_identity, "validated row source identity")
        except ReceiptIdentityError as exc:
            raise ReceiptRecoveryError("validated row source ownership is invalid") from exc
        conditions.append({META_SOURCE_IDENTITY: source_identity})
    if isinstance(source_file, str) and source_file:
        conditions.append({"source_file": source_file})
    if not conditions:
        raise ReceiptRecoveryError("validated row has no conditional source ownership filter")

    receipt_id = metadata.get(META_RECEIPT_ID)
    if receipt_id is not None:
        try:
            _require_uuid(receipt_id, "validated row receipt id")
        except ReceiptIdentityError as exc:
            raise ReceiptRecoveryError("validated row receipt ownership is invalid") from exc
        conditions.append({META_RECEIPT_ID: receipt_id})

    content_hash = metadata.get(META_OUTPUT_CONTENT_HASH)
    if content_hash is not None:
        try:
            _require_sha256(content_hash, "validated row content hash")
        except ReceiptIdentityError as exc:
            raise ReceiptRecoveryError("validated row content identity is invalid") from exc
        conditions.append({META_OUTPUT_CONTENT_HASH: content_hash})
    elif not row.document:
        raise ReceiptRecoveryError(
            "legacy empty row cannot be content-bound for conditional deletion"
        )

    where = conditions[0] if len(conditions) == 1 else {"$and": conditions}
    where_document = {"$regex": f"(?s)^{re.escape(row.document)}$"} if row.document else None
    return where, where_document


def _delete_validated_collection_rows(
    collection: Any,
    capability: _ManagedPurgeCapability,
) -> list[str]:
    """Consume private recovery authority using ID+ownership+content filters."""
    if (
        not isinstance(capability, _ManagedPurgeCapability)
        or capability.authority is not _PURGE_AUTHORITY
        or capability.collection_identity != id(collection)
    ):
        raise ReceiptRecoveryError("private durable-recovery purge capability is required")
    write_scope = _require_managed_write_scope(capability.palace_path)
    if write_scope.nonce is not capability.lock_nonce:
        raise ReceiptRecoveryError("purge capability escaped the exclusive validation scope")
    recovery = _load_rewrite_recovery(
        capability.recovery_path,
        expected_source_identity=capability.source_identity,
    )
    if recovery.receipt_id != capability.receipt_id:
        raise ReceiptRecoveryError("purge capability no longer matches durable recovery")
    snapshot = recovery.snapshots.get(capability.collection_name)
    if (
        snapshot is None
        or _snapshot_payload(snapshot)["manifest_digest"] != capability.snapshot_digest
    ):
        raise ReceiptRecoveryError("purge capability snapshot authority is invalid")

    deleted = []
    for expected in capability.rows:
        current = _collection_rows_for_ids(
            collection,
            [expected.item_id],
            require_all=False,
            include_embeddings=True,
        )
        if expected.item_id not in current:
            raise ReceiptRecoveryError(
                "validated row disappeared before conditional deletion; recovery remains required"
            )
        if _validated_collection_row(expected.item_id, current[expected.item_id]) != expected:
            raise ReceiptRecoveryError(
                "validated row changed before conditional deletion; recovery remains required"
            )
        where, where_document = _delete_filters_for_validated_row(expected)
        delete_kwargs = {"ids": [expected.item_id], "where": where}
        if where_document is not None:
            delete_kwargs["where_document"] = where_document
        _delete_collection_row_with_exact_filters(collection, delete_kwargs)
        survivor = _collection_rows_for_ids(
            collection,
            [expected.item_id],
            require_all=False,
        )
        if expected.item_id in survivor:
            raise ReceiptRecoveryError(
                "conditional delete did not remove the validated row; "
                "a concurrent replacement may have survived"
            )
        deleted.append(expected.item_id)
    return deleted


def _delete_collection_row_with_exact_filters(
    collection: Any,
    delete_kwargs: Mapping[str, Any],
) -> None:
    """Use an exact-filter-capable delete surface or fail closed."""
    method = getattr(collection, "delete", None)
    if not callable(method):
        raise ReceiptRecoveryError("collection has no conditional delete surface")
    if not _callable_accepts_keyword(method, "where_document"):
        raw_collection = getattr(collection, "_collection", None)
        method = getattr(raw_collection, "delete", None)
        if not callable(method) or not _callable_accepts_keyword(method, "where_document"):
            raise ReceiptRecoveryError(
                "collection cannot enforce content-bound conditional deletion"
            )
    result = method(**dict(delete_kwargs))
    if isinstance(result, Mapping) and "deleted" in result:
        deleted = result.get("deleted")
        if not isinstance(deleted, int) or isinstance(deleted, bool) or deleted != 1:
            raise ReceiptRecoveryError("conditional delete did not report exactly one removed row")


def _callable_accepts_keyword(method: Callable[..., Any], keyword: str) -> bool:
    try:
        parameters = inspect.signature(method).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD or parameter.name == keyword
        for parameter in parameters
    )


def _restore_missing_managed_source_rows(
    collection: Any,
    snapshot: ManagedSourceSnapshot,
) -> None:
    """Create only absent baseline rows; never overwrite a concurrent survivor."""
    _snapshot_payload(snapshot)
    current = _collection_rows_for_ids(
        collection,
        list(snapshot.ids),
        require_all=False,
        include_embeddings=True,
    )
    missing_indexes = []
    for index, item_id in enumerate(snapshot.ids):
        row = current.get(item_id)
        if row is None:
            missing_indexes.append(index)
        elif not _row_matches_snapshot(row, snapshot, index):
            raise ReceiptRecoveryError(
                "rollback found a non-baseline row at a baseline ID; refusing overwrite"
            )
    for start in range(0, len(missing_indexes), 1000):
        indexes = missing_indexes[start : start + 1000]
        kwargs = {
            "ids": [snapshot.ids[index] for index in indexes],
            "documents": [snapshot.documents[index] for index in indexes],
            "metadatas": [dict(snapshot.metadatas[index]) for index in indexes],
        }
        if snapshot.embeddings is not None:
            kwargs["embeddings"] = [list(snapshot.embeddings[index]) for index in indexes]
        if indexes:
            collection.add(**kwargs)


def _snapshot_source_files(snapshot: ManagedSourceSnapshot) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                value
                for metadata in snapshot.metadatas
                for value in [metadata.get("source_file")]
                if isinstance(value, str) and value
            }
        )
    )


def _represented_snapshot_source_ids(
    collection: Any,
    *,
    recovery: ManagedRewriteRecovery,
) -> set[str]:
    if not recovery.selector_coverage_complete:
        raise ReceiptRecoveryError(
            "legacy recovery lacks complete source selectors; manual reconciliation is required"
        )
    return set(
        _managed_source_row_ids_for_selectors(
            collection,
            selectors=recovery.selectors,
            source_identity=recovery.source_identity,
        )
    )


def _verify_committed_rewrite_state(
    complete: Mapping[str, Any],
    recovery: ManagedRewriteRecovery,
    *,
    collections: Mapping[str, Any],
) -> None:
    """Prove the COMPLETE manifest is the exact current source representation."""
    if complete.get("receipt_id") != recovery.receipt_id:
        raise ReceiptRecoveryError("COMPLETE does not match rewrite recovery")
    missing = sorted(set(recovery.snapshots) - set(collections))
    if missing:
        raise ReceiptRecoveryError(f"recovery collections are unavailable: {', '.join(missing)}")
    outputs = _normalize_outputs(complete.get("outputs", {}).get("identities", []))
    unknown = sorted({item["collection"] for item in outputs} - set(recovery.snapshots))
    if unknown:
        raise ReceiptRecoveryError(
            f"COMPLETE contains unscoped recovery collections: {', '.join(unknown)}"
        )
    for name in recovery.snapshots:
        collection = collections[name]
        expected = {item["id"]: item for item in outputs if item["collection"] == name}
        represented = _represented_snapshot_source_ids(collection, recovery=recovery)
        if represented != set(expected):
            raise ReceiptRecoveryError(
                "committed source representation does not exactly match COMPLETE"
            )
        rows = _collection_rows_for_ids(collection, sorted(expected))
        for item_id, identity in expected.items():
            document, metadata, _ = rows[item_id]
            if (
                metadata.get(META_RECEIPT_ID) != recovery.receipt_id
                or metadata.get(META_SOURCE_IDENTITY) != recovery.source_identity
                or metadata.get(META_OUTPUT_CONTENT_HASH) != identity["content_hash"]
                or metadata.get("source_file") not in recovery.selectors.source_files
                or sha256_text(document) != identity["content_hash"]
            ):
                raise ReceiptRecoveryError("committed source row does not match COMPLETE identity")


def _palace_path_from_recovery_path(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    for parent in resolved.parents:
        if parent.name == ".mempalace":
            return parent.parent
    raise ReceiptRecoveryError("recovery path is outside a palace journal")


def _collection_ids_for_where(collection: Any, where: Mapping[str, Any]) -> list[str]:
    item_ids: list[str] = []
    seen: set[str] = set()
    offset = 0
    while True:
        result = collection.get(where=dict(where), limit=1000, offset=offset, include=[])
        try:
            page = result.get("ids") or []
        except (AttributeError, TypeError, ValueError) as exc:
            raise ReceiptIdentityError("collection returned an invalid purge result") from exc
        if isinstance(page, str):
            raise ReceiptIdentityError("collection returned an invalid purge result")
        if not isinstance(page, (list, tuple)) or any(not isinstance(item, str) for item in page):
            raise ReceiptIdentityError("collection returned invalid purge identities")
        if not page:
            break
        if len(set(page)) != len(page) or set(page) & seen:
            raise ReceiptIdentityError("collection returned duplicate purge identities")
        item_ids.extend(page)
        seen.update(page)
        offset += len(page)
        if len(page) < 1000:
            break
    return item_ids


def _add_receipt_candidate(
    candidates: dict[str, tuple[dict, Path]],
    event: dict,
    path: Path,
) -> None:
    receipt_id = _require_uuid(event.get("receipt_id"), "receipt id")
    existing = candidates.get(receipt_id)
    if existing is not None and _canonical_json_bytes(existing[0]) != _canonical_json_bytes(event):
        raise ReceiptConflictError("receipt journal contains conflicting COMPLETE events")
    candidates[receipt_id] = (event, path)


def _receipt_predecessor_ids(event: Mapping[str, Any]) -> set[str]:
    relations = _require_mapping(event.get("relations"), "receipt relations")
    values = []
    if "predecessor_receipt_id" in relations:
        values.append(_require_uuid(relations["predecessor_receipt_id"], "predecessor id"))
    if "reuses_receipt_id" in relations:
        values.append(_require_uuid(relations["reuses_receipt_id"], "reused receipt id"))
    if "supersedes" in relations:
        supersedes = _require_mapping(relations["supersedes"], "supersession")
        values.append(_require_uuid(supersedes.get("receipt_id"), "superseded receipt id"))
    resolved = set(values)
    if len(resolved) > 1:
        raise ReceiptConflictError("receipt declares contradictory predecessor identities")
    return resolved


def _resolve_receipt_lineage(
    candidates: Mapping[str, tuple[dict, Path]],
    *,
    indexed_receipt_id: Optional[str] = None,
) -> tuple[dict, Path]:
    """Resolve one lineage head without using wall-clock timestamps."""
    if not candidates:
        raise ReceiptConflictError("receipt lineage is empty")
    children: dict[str, set[str]] = {}
    for receipt_id, (event, _) in candidates.items():
        for predecessor in _receipt_predecessor_ids(event):
            if predecessor not in candidates:
                compatibility = event.get("relations", {}).get(
                    "legacy_missing_predecessor_compatibility"
                )
                if compatibility != LEGACY_MISSING_PREDECESSOR_COMPATIBILITY:
                    raise ReceiptConflictError("receipt lineage declares a missing predecessor")
                continue
            children.setdefault(predecessor, set()).add(receipt_id)
    forks = [receipt_id for receipt_id, values in children.items() if len(values) > 1]
    if forks:
        raise ReceiptConflictError("receipt lineage contains multiple successor branches")

    def terminal(start: str) -> str:
        seen = set()
        current = start
        while True:
            if current in seen:
                raise ReceiptConflictError("receipt lineage contains a cycle")
            seen.add(current)
            successors = children.get(current, set())
            if not successors:
                return current
            current = next(iter(successors))

    if indexed_receipt_id is not None and indexed_receipt_id not in candidates:
        raise ReceiptConflictError("current receipt index is absent from its journal")

    heads = {terminal(receipt_id) for receipt_id in candidates}
    if len(heads) != 1:
        raise ReceiptConflictError(
            "receipt lineage contains disconnected or ambiguous COMPLETE branches"
        )
    head_id = next(iter(heads))
    if indexed_receipt_id is not None and terminal(indexed_receipt_id) != head_id:
        raise ReceiptConflictError("current receipt index is disconnected from its journal head")
    return candidates[head_id]


def _snapshot_payload(snapshot: ManagedSourceSnapshot) -> dict:
    if not isinstance(snapshot, ManagedSourceSnapshot):
        raise ReceiptRecoveryError("managed rewrite snapshot is invalid")
    if (
        len(snapshot.ids) != len(snapshot.documents)
        or len(snapshot.ids) != len(snapshot.metadatas)
        or (snapshot.embeddings is not None and len(snapshot.ids) != len(snapshot.embeddings))
        or len(set(snapshot.ids)) != len(snapshot.ids)
    ):
        raise ReceiptRecoveryError("managed rewrite snapshot is internally inconsistent")
    if any(not isinstance(item, str) or not item for item in snapshot.ids):
        raise ReceiptRecoveryError("managed rewrite snapshot IDs must be non-empty text")
    if any(not isinstance(item, str) for item in snapshot.documents):
        raise ReceiptRecoveryError("managed rewrite snapshot documents must be text")

    metadatas = [_recovery_json_value(dict(item)) for item in snapshot.metadatas]
    embeddings = None
    if snapshot.embeddings is not None:
        embeddings = []
        for embedding in snapshot.embeddings:
            values = []
            for value in embedding:
                if isinstance(value, bool):
                    raise ReceiptRecoveryError("managed rewrite embedding values must be numeric")
                try:
                    number = float(value)
                except (TypeError, ValueError) as exc:
                    raise ReceiptRecoveryError(
                        "managed rewrite embedding values must be numeric"
                    ) from exc
                if not math.isfinite(number):
                    raise ReceiptRecoveryError("managed rewrite embedding values must be finite")
                values.append(number)
            embeddings.append(values)
    core = {
        "ids": list(snapshot.ids),
        "documents": list(snapshot.documents),
        "metadatas": metadatas,
        "embeddings": embeddings,
    }
    return {**core, "manifest_digest": sha256_bytes(_canonical_json_bytes(core))}


def _source_selector_payload(selectors: ManagedSourceSelectors) -> dict:
    if not isinstance(selectors, ManagedSourceSelectors) or not selectors.source_files:
        raise ReceiptRecoveryError("managed rewrite requires explicit source-file selectors")
    source_files = []
    for source_file in selectors.source_files:
        source_files.append(_require_text(source_file, "recovery source-file selector"))
    if len(set(source_files)) != len(source_files):
        raise ReceiptRecoveryError("managed rewrite source selectors must be unique")
    return {
        "schema": "mempalace-managed-source-selectors/v1",
        "source_files": sorted(source_files),
    }


def _load_source_selectors(value: Any) -> ManagedSourceSelectors:
    payload = _require_mapping(value, "recovery source selectors")
    if payload.get("schema") != "mempalace-managed-source-selectors/v1":
        raise ReceiptRecoveryError("managed rewrite selector schema is invalid")
    source_files = payload.get("source_files")
    if (
        not isinstance(source_files, list)
        or not source_files
        or any(not isinstance(item, str) or not item for item in source_files)
        or len(set(source_files)) != len(source_files)
        or source_files != sorted(source_files)
    ):
        raise ReceiptRecoveryError("managed rewrite source selectors are invalid")
    return ManagedSourceSelectors(source_files=tuple(source_files))


def _load_rewrite_recovery(
    path: Path,
    *,
    expected_source_identity: Optional[str] = None,
) -> ManagedRewriteRecovery:
    try:
        value = _read_json(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ReceiptRecoveryError("managed rewrite recovery is unreadable") from exc
    schema = value.get("schema")
    if schema not in {RECOVERY_SCHEMA, LEGACY_RECOVERY_SCHEMA}:
        raise ReceiptRecoveryError("managed rewrite recovery schema is invalid")
    receipt_id = _require_uuid(value.get("receipt_id"), "recovery receipt id")
    source_identity = _require_hmac(value.get("source_identity"), "recovery source identity")
    if expected_source_identity is not None and source_identity != expected_source_identity:
        raise ReceiptRecoveryError("managed rewrite recovery belongs to another source")
    if path.stem != receipt_id or path.parent.name != _digest_value(source_identity):
        raise ReceiptRecoveryError("managed rewrite recovery path does not match its identity")
    _require_timestamp(value.get("created_at"), "recovery creation time")
    previous = value.get("previous_receipt_id")
    if previous is not None:
        previous = _require_uuid(previous, "previous receipt id")
    collections = _require_mapping(value.get("collections"), "recovery collections")
    if not collections:
        raise ReceiptRecoveryError("managed rewrite recovery has no collections")

    snapshots = {}
    for name, raw_snapshot in collections.items():
        safe_name = _require_token(name, "recovery collection name")
        payload = dict(_require_mapping(raw_snapshot, "recovery collection snapshot"))
        digest = _require_sha256(payload.pop("manifest_digest", None), "snapshot manifest digest")
        if sha256_bytes(_canonical_json_bytes(payload)) != digest:
            raise ReceiptRecoveryError("managed rewrite snapshot digest does not match")
        ids = payload.get("ids")
        documents = payload.get("documents")
        metadatas = payload.get("metadatas")
        embeddings = payload.get("embeddings")
        if (
            not isinstance(ids, list)
            or not isinstance(documents, list)
            or not isinstance(metadatas, list)
            or len(ids) != len(documents)
            or len(ids) != len(metadatas)
            or len(set(ids)) != len(ids)
            or any(not isinstance(item, str) or not item for item in ids)
            or any(not isinstance(item, str) for item in documents)
            or any(not isinstance(item, Mapping) for item in metadatas)
        ):
            raise ReceiptRecoveryError("managed rewrite snapshot rows are invalid")
        resolved_embeddings = None
        if embeddings is not None:
            if not isinstance(embeddings, list) or len(embeddings) != len(ids):
                raise ReceiptRecoveryError("managed rewrite snapshot embeddings are invalid")
            resolved_embeddings = []
            for embedding in embeddings:
                if not isinstance(embedding, list) or any(
                    isinstance(item, bool)
                    or not isinstance(item, (int, float))
                    or not math.isfinite(float(item))
                    for item in embedding
                ):
                    raise ReceiptRecoveryError("managed rewrite snapshot embeddings are invalid")
                resolved_embeddings.append(tuple(float(item) for item in embedding))
        snapshots[safe_name] = ManagedSourceSnapshot(
            ids=tuple(ids),
            documents=tuple(documents),
            metadatas=tuple(dict(item) for item in metadatas),
            embeddings=(tuple(resolved_embeddings) if resolved_embeddings is not None else None),
        )

    selector_coverage_complete = schema == RECOVERY_SCHEMA
    if selector_coverage_complete:
        selectors = _load_source_selectors(value.get("selectors"))
    else:
        legacy_source_files = tuple(
            sorted(
                {
                    source_file
                    for snapshot in snapshots.values()
                    for source_file in _snapshot_source_files(snapshot)
                }
            )
        )
        selectors = ManagedSourceSelectors(source_files=legacy_source_files)
        selector_coverage_complete = bool(legacy_source_files)

    manifest = _require_sha256(value.get("manifest_digest"), "recovery manifest digest")
    core = dict(value)
    core.pop("manifest_digest", None)
    if sha256_bytes(_canonical_json_bytes(core)) != manifest:
        raise ReceiptRecoveryError("managed rewrite recovery digest does not match")
    return ManagedRewriteRecovery(
        path=path,
        receipt_id=receipt_id,
        source_identity=source_identity,
        previous_receipt_id=previous,
        selectors=selectors,
        selector_coverage_complete=selector_coverage_complete,
        snapshots=snapshots,
    )


def _recovery_json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ReceiptRecoveryError("recovery metadata numbers must be finite")
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ReceiptRecoveryError("recovery metadata keys must be text")
        return {key: _recovery_json_value(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_recovery_json_value(item) for item in value]
    raise ReceiptRecoveryError("recovery metadata must be JSON-compatible")


def _verify_restored_snapshot(
    collection: Any,
    snapshot: ManagedSourceSnapshot,
    *,
    recovery: ManagedRewriteRecovery,
) -> None:
    _require_hmac(recovery.source_identity, "source identity")
    _require_uuid(recovery.receipt_id, "interrupted receipt id")
    rows = _collection_rows_for_ids(
        collection,
        list(snapshot.ids),
        include_embeddings=True,
    )
    for index, item_id in enumerate(snapshot.ids):
        if not _row_matches_snapshot(rows[item_id], snapshot, index):
            raise ReceiptRecoveryError("restored managed rewrite rows do not match snapshot")
    represented = _represented_snapshot_source_ids(
        collection,
        recovery=recovery,
    )
    if represented != set(snapshot.ids):
        raise ReceiptRecoveryError("restored source retains replacement rows outside snapshot")
    interrupted = set(_collection_ids_for_where(collection, {META_RECEIPT_ID: recovery.receipt_id}))
    if interrupted:
        raise ReceiptRecoveryError("restored source retains interrupted-attempt rows")


def _validate_recovery_collection_state(
    collection: Any,
    snapshot: ManagedSourceSnapshot,
    *,
    recovery: ManagedRewriteRecovery,
    collection_name: str,
) -> _ManagedPurgeCapability:
    palace_path = _palace_path_from_recovery_path(recovery.path)
    write_scope = _require_managed_write_scope(palace_path)
    stored_snapshot = _recovery_snapshot_for_collection(
        recovery,
        collection_name,
        snapshot,
    )
    candidate_ids = set(snapshot.ids)
    candidate_ids.update(
        _represented_snapshot_source_ids(
            collection,
            recovery=recovery,
        )
    )
    rows = _collection_rows_for_ids(
        collection,
        sorted(candidate_ids),
        require_all=False,
        include_embeddings=True,
    )
    snapshot_indexes = {item_id: index for index, item_id in enumerate(snapshot.ids)}
    interrupted_rows = []
    for item_id, (document, metadata, embedding) in rows.items():
        baseline_index = snapshot_indexes.get(item_id)
        row = (document, metadata, embedding)
        baseline_match = baseline_index is not None and _row_matches_snapshot(
            row,
            stored_snapshot,
            baseline_index,
        )
        interrupted_attempt = (
            metadata.get(META_SOURCE_IDENTITY) == recovery.source_identity
            and metadata.get(META_RECEIPT_ID) == recovery.receipt_id
        )
        if not baseline_match and not interrupted_attempt:
            raise ReceiptRecoveryError(
                "pending rewrite encountered an unexpected managed row; refusing restoration"
            )
        if interrupted_attempt:
            validated = _validated_collection_row(item_id, row)
            _delete_filters_for_validated_row(validated)
            interrupted_rows.append(validated)
    return _ManagedPurgeCapability(
        authority=_PURGE_AUTHORITY,
        lock_nonce=write_scope.nonce,
        palace_path=palace_path,
        collection_identity=id(collection),
        recovery_path=recovery.path,
        receipt_id=recovery.receipt_id,
        source_identity=recovery.source_identity,
        collection_name=collection_name,
        snapshot_digest=_snapshot_payload(stored_snapshot)["manifest_digest"],
        rows=tuple(sorted(interrupted_rows, key=lambda row: row.item_id)),
    )


def _matching_receipt_candidates(
    candidates: Iterable[tuple[dict, Path]],
    *,
    source_identity: str,
    content_hash: Optional[str],
    version_digest: Optional[str],
    config_digest: Optional[str],
) -> list[tuple[dict, Path]]:
    matching = []
    for event, path in candidates:
        if not _is_complete_event_for_source(event, source_identity):
            continue
        source = event["source"]
        producer = event["producer"]
        if content_hash is not None and source["content_hash"] != content_hash:
            continue
        if version_digest is not None and source["version_hash"] != version_digest:
            continue
        if config_digest is not None and producer["config"]["digest"] != config_digest:
            continue
        matching.append((event, path))
    return matching


def _is_complete_event_for_source(event: Mapping[str, Any], source_identity: str) -> bool:
    try:
        if event.get("schema") != RECEIPT_SCHEMA or event.get("state") != "COMPLETE":
            return False
        _validate_complete_publication_marker(event)
        receipt_id = _require_uuid(event.get("receipt_id"), "receipt id")
        sequence = event.get("sequence")
        if not isinstance(sequence, int) or sequence < 0:
            return False
        if _require_uuid(event.get("event_id"), "event id") != receipt_event_id(
            receipt_id, sequence
        ):
            return False
        _require_timestamp(event.get("event_time"), "event time")
        source = _require_mapping(event.get("source"), "source identity")
        if _require_hmac(source.get("identity"), "source identity") != source_identity:
            return False
        shared_receipt_projection(event)
        outputs = _normalize_outputs(event["outputs"]["identities"])
        if event.get("disposition") == "UNCHANGED":
            _require_uuid(
                event.get("relations", {}).get("reuses_receipt_id"),
                "reused receipt id",
            )
        if any(item["producer_receipt_id"] != receipt_id for item in outputs):
            return False
        counts = event["counts"]
        drawers = sum(1 for item in outputs if item["kind"] == "drawer")
        return (
            counts.get("items_written", 0) + counts.get("items_unchanged", 0) == len(outputs)
            and counts.get("items_expected") == len(outputs)
            and counts.get("drawers_written", 0) + counts.get("drawers_unchanged", 0) == drawers
            and counts.get("drawers_expected") == drawers
        )
    except (KeyError, TypeError, ReceiptIdentityError):
        return False


def _validate_complete_publication_marker(event: Mapping[str, Any]) -> None:
    publication = _require_mapping(event.get("publication"), "COMPLETE publication")
    if publication.get("schema") != COMPLETE_PUBLICATION_SCHEMA:
        raise ReceiptIdentityError("COMPLETE durable publication schema is invalid")
    if publication.get("policy") != "durable-file-and-parent-proof-required":
        raise ReceiptIdentityError("COMPLETE durable publication policy is invalid")


def _validate_terminal_output_binding(event: Mapping[str, Any]) -> None:
    receipt_id = _require_uuid(event.get("receipt_id"), "receipt id")
    outputs = _require_mapping(event.get("outputs"), "output manifest").get("identities")
    if not isinstance(outputs, list):
        raise ReceiptIdentityError("exact output identities are required")
    normalized = _normalize_outputs(outputs)
    if any(item["producer_receipt_id"] != receipt_id for item in normalized):
        raise ReceiptIdentityError("COMPLETE manifest contains a foreign producer receipt")


def _validate_durable_publication_proof(
    path: Path,
    value: Mapping[str, Any],
    proof: Optional[DurablePublicationProof],
) -> None:
    expected = _canonical_json_bytes(_jsonable(value)) + b"\n"
    if (
        proof is None
        or proof.path.resolve() != path.resolve()
        or proof.content_sha256 != sha256_bytes(expected)
        or proof.size_bytes != len(expected)
        or not proof.primitive
    ):
        raise ReceiptDurabilityError("durable publication did not return verified evidence")


def _invalidation_id(
    invalidated_receipt_id: str,
    by_receipt_id: str,
    reason: str,
    manifest: str,
) -> str:
    return str(
        uuid.uuid5(
            uuid.UUID(by_receipt_id),
            f"{invalidated_receipt_id}|{reason}|{manifest}",
        )
    )


def _validate_invalidation_record(
    value: Mapping[str, Any],
    *,
    invalidated_receipt_id: Optional[str] = None,
) -> dict:
    record = _require_mapping(value, "invalidation record")
    if record.get("schema") != INVALIDATION_SCHEMA:
        raise ReceiptIdentityError("invalidation record schema is invalid")
    invalidation_id = _require_uuid(record.get("invalidation_id"), "invalidation id")
    event_time = _require_timestamp(record.get("event_time"), "invalidation time")
    invalidated_id = _require_uuid(record.get("invalidated_receipt_id"), "invalidated receipt id")
    if invalidated_receipt_id is not None and invalidated_id != invalidated_receipt_id:
        raise ReceiptIdentityError("invalidation record is stored under the wrong receipt")
    manifest = _require_sha256(
        record.get("invalidated_manifest_digest"), "invalidated manifest digest"
    )
    by_receipt_id = _require_uuid(record.get("by_receipt_id"), "successor receipt id")
    reason = _require_text(record.get("reason"), "invalidation reason")
    expected_id = _invalidation_id(invalidated_id, by_receipt_id, reason, manifest)
    if invalidation_id != expected_id:
        raise ReceiptIdentityError("invalidation identity does not match its contents")
    return {
        "schema": INVALIDATION_SCHEMA,
        "invalidation_id": invalidation_id,
        "event_time": event_time,
        "invalidated_receipt_id": invalidated_id,
        "invalidated_manifest_digest": manifest,
        "by_receipt_id": by_receipt_id,
        "reason": reason,
    }


def _planned_invalidation_records(event: Mapping[str, Any]) -> list[dict]:
    relations = event.get("relations", {})
    if not isinstance(relations, Mapping):
        raise ReceiptIdentityError("receipt relations object is required")
    values = relations.get("invalidation_records", [])
    if not isinstance(values, list):
        raise ReceiptIdentityError("receipt invalidation records must be a list")
    return [_validate_invalidation_record(value) for value in values]


def _same_invalidation(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    keys = {
        "schema",
        "invalidation_id",
        "invalidated_receipt_id",
        "invalidated_manifest_digest",
        "by_receipt_id",
        "reason",
    }
    return all(left.get(key) == right.get(key) for key in keys)


def _index_matches_event(
    index: Mapping[str, Any],
    event: Mapping[str, Any],
    source_identity: str,
) -> bool:
    if not _is_complete_event_for_source(event, source_identity):
        return False
    source = event["source"]
    producer = event.get("producer", {})
    return (
        index.get("receipt_id") == event.get("receipt_id")
        and index.get("source_content_hash") == source.get("content_hash")
        and index.get("source_version_hash") == source.get("version_hash")
        and index.get("config_digest") == producer.get("config", {}).get("digest")
    )


def _event_freshness_key(event: Mapping[str, Any], path: Path) -> tuple[float, int, str]:
    try:
        event_time = datetime.fromisoformat(str(event["event_time"]).replace("Z", "+00:00"))
        timestamp = event_time.timestamp()
    except (KeyError, TypeError, ValueError, OSError):
        timestamp = float("-inf")
    try:
        modified_ns = path.stat().st_mtime_ns
    except OSError:
        modified_ns = -1
    return timestamp, modified_ns, str(event.get("receipt_id", ""))


def _producer_identity() -> dict:
    package_dir = Path(__file__).resolve().parent
    git = _git_identity(package_dir.parent)
    return {
        "package": {
            "name": "mempalace",
            "version": __version__,
            "source_digest": _package_source_digest(package_dir),
        },
        "git": git,
    }


def _package_source_digest(package_dir: Path) -> str:
    digest = hashlib.sha256()
    package_files = (
        path
        for path in package_dir.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix.lower() not in {".pyc", ".pyo"}
    )
    for path in sorted(package_files, key=lambda item: item.as_posix()):
        relative = path.relative_to(package_dir).as_posix().encode("utf-8")
        try:
            content = path.read_bytes()
        except OSError:
            continue
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return f"sha256:{digest.hexdigest()}"


def _git_identity(repo_root: Path) -> dict:
    env_commit = os.environ.get("MEMPALACE_BUILD_GIT_SHA", "").strip()
    if env_commit:
        commit_identity = (
            env_commit.lower()
            if len(env_commit) in {40, 64}
            and all(character in "0123456789abcdef" for character in env_commit.lower())
            else sha256_text(env_commit)
        )
        return {"state": "build-metadata", "commit": commit_identity, "dirty": None}
    try:
        commit = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            check=True,
            text=True,
            timeout=3,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(repo_root), "status", "--porcelain", "--untracked-files=all"],
            capture_output=True,
            check=True,
            text=True,
            timeout=3,
        ).stdout
        return {"state": "available", "commit": commit, "dirty": bool(status.strip())}
    except (OSError, subprocess.SubprocessError):
        return {"state": "unavailable", "commit": None, "dirty": None}


def _canonical_local_path(value: str) -> str:
    return os.path.realpath(os.path.expanduser(value))


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {
            str(key): _jsonable(item)
            for key, item in sorted(value.items(), key=lambda x: str(x[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_jsonable(item) for item in value), key=lambda item: repr(item))
    return repr(value)


def _normalize_outputs(outputs: Iterable[Mapping[str, Any]]) -> list[dict]:
    normalized = []
    seen = set()
    for raw in outputs:
        if not isinstance(raw, Mapping):
            raise ReceiptIdentityError("output identities must be objects")
        item = {
            "collection": _require_text(raw.get("collection"), "output collection"),
            "id": _require_text(raw.get("id"), "output id"),
            "kind": _require_text(raw.get("kind"), "output kind"),
            "content_hash": raw.get("content_hash"),
            "producer_receipt_id": _require_uuid(
                raw.get("producer_receipt_id"), "producer receipt id"
            ),
        }
        _require_sha256(item["content_hash"], "output content hash")
        key = (item["collection"], item["id"])
        if key in seen:
            raise ReceiptConflictError(f"duplicate output identity: {key}")
        seen.add(key)
        normalized.append(item)
    return sorted(normalized, key=lambda item: (item["collection"], item["id"]))


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _receipt_state_exists(root: Path) -> bool:
    for name in ("events", "sources", "invalidations", "recoveries"):
        directory = root / name
        if directory.exists() and any(path.is_file() for path in directory.rglob("*")):
            return True
    return False


def _load_or_create_identity_key(path: Path, *, require_existing: bool) -> bytes:
    try:
        key = path.read_bytes()
    except FileNotFoundError:
        if require_existing:
            raise ReceiptIdentityError(
                "receipt identity key is missing while receipt state already exists"
            )
        key = secrets.token_bytes(32)
        _ensure_private_dir(path.parent)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            fd = os.open(str(path), flags, 0o600)
        except FileExistsError:
            key = path.read_bytes()
        else:
            with os.fdopen(fd, "wb") as handle:
                handle.write(key)
                handle.flush()
                os.fsync(handle.fileno())
            _fsync_directory(path.parent)
    if len(key) != 32:
        raise ReceiptIdentityError("receipt identity key must contain exactly 32 bytes")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return key


def _ensure_identity_key_metadata(path: Path, key: bytes) -> None:
    expected = {
        "schema": IDENTITY_KEY_SCHEMA,
        "key_fingerprint": sha256_bytes(key),
    }
    if path.exists():
        try:
            current = _read_json(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ReceiptIdentityError("receipt identity key metadata is unreadable") from exc
        if current.get("schema") != IDENTITY_KEY_SCHEMA:
            raise ReceiptIdentityError("receipt identity key metadata schema is invalid")
        if current.get("key_fingerprint") != expected["key_fingerprint"]:
            raise ReceiptIdentityError(
                "receipt identity key does not match its recorded fingerprint"
            )
        return
    _atomic_write_json(path, expected, immutable=True)


def _atomic_write_json(
    path: Path,
    value: Mapping[str, Any],
    *,
    immutable: bool,
    durable: bool = False,
    durability_anchor: Optional[Path] = None,
) -> Optional[DurablePublicationProof]:
    data = _canonical_json_bytes(_jsonable(value)) + b"\n"
    if durable:
        _ensure_private_dir_durable_chain(
            path.parent,
            anchor=durability_anchor or path.parent,
        )
    else:
        _ensure_private_dir(path.parent)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        try:
            os.chmod(temp_path, 0o600)
        except OSError:
            pass
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if durable:
            return _publish_durable_file(temp_path, path, data, immutable=immutable)
        if immutable:
            try:
                os.link(str(temp_path), str(path))
            except FileExistsError:
                if path.read_bytes() != data:
                    raise ReceiptConflictError(f"immutable receipt already exists: {path.name}")
            except OSError as exc:
                raise ReceiptError(f"create-only receipt publication failed: {path.name}") from exc
        else:
            os.replace(str(temp_path), str(path))
        try:
            _fsync_directory(path.parent)
        except OSError:
            # Publication already succeeded. Raising here could make callers
            # roll back rows while the authoritative COMPLETE remains linked.
            _LOGGER.warning("receipt directory sync failed after publication")
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            _LOGGER.warning("temporary receipt file cleanup failed")
    return None


def _publish_durable_file(
    temp_path: Path,
    path: Path,
    data: bytes,
    *,
    immutable: bool,
) -> DurablePublicationProof:
    """Publish one same-directory file and fail unless OS durability checks pass."""
    primitive = "posix-link-fsync" if immutable else "posix-replace-fsync"
    try:
        if os.name == "nt":
            primitive = "windows-directory-marker+movefileex-write-through"
            try:
                _windows_move_file_write_through(
                    temp_path,
                    path,
                    replace_existing=not immutable,
                )
            except OSError as exc:
                error_code = getattr(exc, "winerror", None) or exc.errno
                if (
                    not immutable
                    or error_code not in _WINDOWS_ALREADY_EXISTS_ERRORS
                    or not path.exists()
                ):
                    raise
                if path.read_bytes() != data:
                    raise ReceiptConflictError(
                        f"immutable receipt already exists: {path.name}"
                    ) from exc
        elif immutable:
            try:
                os.link(str(temp_path), str(path))
            except FileExistsError:
                if path.read_bytes() != data:
                    raise ReceiptConflictError(f"immutable receipt already exists: {path.name}")
        else:
            os.replace(str(temp_path), str(path))

        content_sha256 = _flush_and_verify_published_file(path, data)
        if os.name != "nt":
            _fsync_directory(path.parent)
    except ReceiptConflictError:
        raise
    except (OSError, ValueError) as exc:
        raise ReceiptDurabilityError(f"durable publication failed for {path.name}") from exc

    return DurablePublicationProof(
        path=path.resolve(),
        content_sha256=content_sha256,
        size_bytes=len(data),
        primitive=primitive,
    )


def _ensure_private_dir_durable_chain(
    path: Path,
    *,
    anchor: Path,
    _platform_name: Optional[str] = None,
) -> None:
    """Create and durability-prove every journal directory through ``path``."""
    target = path.resolve()
    root = anchor.resolve()
    if target != root and root not in target.parents:
        raise ReceiptDurabilityError("durability directory escapes its journal anchor")
    relative = target.relative_to(root)
    directories = [root]
    current = root
    for part in relative.parts:
        current = current / part
        directories.append(current)
    platform_name = _platform_name or os.name
    try:
        for directory in directories:
            directory.mkdir(exist_ok=True, mode=0o700)
            try:
                directory.chmod(0o700)
            except OSError:
                pass
            if platform_name == "nt":
                _publish_windows_directory_marker(directory)
            else:
                _fsync_directory(directory)
                _fsync_directory(directory.parent)
    except ReceiptDurabilityError:
        raise
    except OSError as exc:
        raise ReceiptDurabilityError(
            f"durable directory publication failed for {target.name}"
        ) from exc


def _publish_windows_directory_marker(directory: Path) -> None:
    """Use documented write-through file primitives as Windows directory evidence."""
    marker = directory / _DIRECTORY_DURABILITY_MARKER
    if marker.exists():
        if marker.read_bytes() != _DIRECTORY_DURABILITY_BYTES:
            raise ReceiptDurabilityError(
                f"directory durability marker is inconsistent for {directory.name}"
            )
        _flush_and_verify_published_file(marker, _DIRECTORY_DURABILITY_BYTES)
        return

    fd, temp_name = tempfile.mkstemp(
        prefix=f".{_DIRECTORY_DURABILITY_MARKER}.",
        suffix=".tmp",
        dir=str(directory),
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(_DIRECTORY_DURABILITY_BYTES)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            _windows_move_file_write_through(
                temp_path,
                marker,
                replace_existing=False,
            )
        except OSError as exc:
            error_code = getattr(exc, "winerror", None) or exc.errno
            if (
                error_code not in _WINDOWS_ALREADY_EXISTS_ERRORS
                or not marker.exists()
                or marker.read_bytes() != _DIRECTORY_DURABILITY_BYTES
            ):
                raise
        _flush_and_verify_published_file(marker, _DIRECTORY_DURABILITY_BYTES)
    except (OSError, ValueError) as exc:
        raise ReceiptDurabilityError(
            f"Windows directory durability proof failed for {directory.name}"
        ) from exc
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            _LOGGER.warning("temporary directory durability marker cleanup failed")


def _publish_windows_directory_sync_barrier(directory: Path) -> None:
    """Best available Windows ordering proof after directory-entry mutation.

    Win32 exposes no supported directory ``fsync`` equivalent here. A fresh
    same-directory file is therefore replaced with ``MOVEFILE_WRITE_THROUGH``
    and then verified with ``FlushFileBuffers``. This proves the barrier file,
    not backend-atomic ordering with Chroma.
    """
    marker = directory / _DIRECTORY_SYNC_BARRIER
    data = f"{uuid.uuid4()}\n".encode("ascii")
    fd, temp_name = tempfile.mkstemp(prefix=f".{marker.name}.", suffix=".tmp", dir=directory)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _windows_move_file_write_through(temp_path, marker, replace_existing=True)
        _flush_and_verify_published_file(marker, data)
    except (OSError, ValueError) as exc:
        raise ReceiptDurabilityError(
            f"Windows directory sync barrier failed for {directory.name}"
        ) from exc
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            _LOGGER.warning("temporary Windows directory barrier cleanup failed")


def _windows_move_file_write_through(
    source: Path,
    destination: Path,
    *,
    replace_existing: bool,
) -> None:
    """Move a same-directory file with the strongest documented Win32 flush flag."""
    import ctypes
    from ctypes import wintypes

    move_file_ex = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
    move_file_ex.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    move_file_ex.restype = wintypes.BOOL
    flags = _MOVEFILE_WRITE_THROUGH
    if replace_existing:
        flags |= _MOVEFILE_REPLACE_EXISTING
    if not move_file_ex(
        _windows_api_path(source),
        _windows_api_path(destination),
        flags,
    ):
        error = ctypes.get_last_error()
        raise OSError(error, ctypes.FormatError(error), str(destination))


def _windows_api_path(path: Path) -> str:
    resolved = str(path.resolve())
    if resolved.startswith("\\\\?\\"):
        return resolved
    if resolved.startswith("\\\\"):
        return "\\\\?\\UNC\\" + resolved[2:]
    return "\\\\?\\" + resolved


def _flush_and_verify_published_file(path: Path, expected: bytes) -> str:
    expected_hash = hashlib.sha256(expected).hexdigest()
    with path.open("r+b", buffering=0) as handle:
        if os.name == "nt":
            _windows_flush_file_buffers(handle.fileno(), path)
        else:
            os.fsync(handle.fileno())
        handle.seek(0)
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    if size != len(expected) or digest.hexdigest() != expected_hash:
        raise ReceiptDurabilityError(f"durable publication verification failed for {path.name}")
    return f"sha256:{expected_hash}"


def _windows_flush_file_buffers(file_descriptor: int, path: Path) -> None:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    flush_file_buffers = ctypes.WinDLL("kernel32", use_last_error=True).FlushFileBuffers
    flush_file_buffers.argtypes = [wintypes.HANDLE]
    flush_file_buffers.restype = wintypes.BOOL
    handle = msvcrt.get_osfhandle(file_descriptor)
    if not flush_file_buffers(wintypes.HANDLE(handle)):
        error = ctypes.get_last_error()
        raise OSError(error, ctypes.FormatError(error), str(path))


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(str(path), flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object expected in {path}")
    return value


def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReceiptIdentityError(f"{label} is required")
    return value


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ReceiptIdentityError(f"{label} object is required")
    return value


def _require_token(value: Any, label: str) -> str:
    text = _require_text(value, label)
    if not _TOKEN_RE.fullmatch(text):
        raise ReceiptIdentityError(f"{label} must use restricted token syntax")
    return text


def _require_timestamp(value: Any, label: str) -> str:
    text = _require_text(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReceiptIdentityError(f"{label} must be an RFC 3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ReceiptIdentityError(f"{label} must include a timezone")
    return text


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
    return _require_hash(value, label, prefix="sha256")


def _require_hmac(value: Any, label: str) -> str:
    return _require_hash(value, label, prefix="hmac-sha256")


def _require_hash(value: Any, label: str, *, prefix: str) -> str:
    if (
        not isinstance(value, str)
        or not _HASH_RE.fullmatch(value)
        or not value.startswith(f"{prefix}:")
    ):
        raise ReceiptIdentityError(f"{label} must use {prefix}")
    return value


def _digest_value(value: str) -> str:
    return value.split(":", 1)[1]


__all__ = [
    "COMPLETE_PUBLICATION_SCHEMA",
    "INVALIDATION_SCHEMA",
    "IDENTITY_KEY_SCHEMA",
    "RECOVERY_SCHEMA",
    "META_OUTPUT_CONTENT_HASH",
    "META_RECEIPT_ID",
    "META_SOURCE_CONTENT_HASH",
    "META_SOURCE_IDENTITY",
    "META_SOURCE_VERSION_HASH",
    "DurablePublicationProof",
    "ManagedRunIdentity",
    "ManagedRewriteRecovery",
    "ManagedSourceSelectors",
    "ManagedSourceSnapshot",
    "RECEIPT_SCHEMA",
    "RECEIPT_STATES",
    "ReceiptConflictError",
    "ReceiptDurabilityError",
    "ReceiptError",
    "ReceiptIdentityError",
    "ReceiptRecoveryError",
    "ReceiptStateError",
    "ReceiptStore",
    "SourceWriteReceiptSession",
    "TERMINAL_STATES",
    "canonical_source_locator",
    "config_hash",
    "manifest_digest",
    "managed_write_scope",
    "output_identity",
    "purge_managed_source_snapshot",
    "rollback_managed_source_rows",
    "sha256_bytes",
    "sha256_text",
    "shared_receipt_projection",
    "snapshot_managed_source_rows",
    "source_size_bucket",
    "stamp_output_metadata",
    "version_hash",
    "write_receipted_collection_batch",
]
