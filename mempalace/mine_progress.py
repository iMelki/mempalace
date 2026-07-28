"""Deterministic source plans and crash-resumable progress for project mining.

The source-write receipt journal remains the authority for represented palace
content.  This module supplies the outer, source-order contract: an immutable
manifest and a sanitized append-only cursor that may advance only after the
caller has verified one terminal receipt.
"""

from __future__ import annotations

import json
import os
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Union

from .write_receipts import (
    ReceiptConflictError,
    _atomic_write_json,
    sha256_bytes,
)

MINE_MANIFEST_SCHEMA = "mempalace-mine-source-manifest/v1"
MINE_PROGRESS_SCHEMA = "mempalace-mine-source-progress/v1"
MINE_PROGRESS_EVENT = "source-represented"
MINE_PROGRESS_REVISION = "1"


class MinePlanError(RuntimeError):
    """Base error for deterministic source-plan and cursor validation."""


class MineManifestDrift(MinePlanError):
    """Raised when a source no longer matches its immutable manifest entry."""


class MineProgressError(MinePlanError):
    """Raised when progress cannot prove one contiguous verified prefix."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def miner_revision(miner_path: Union[str, os.PathLike]) -> dict[str, str]:
    """Bind a plan to both the protocol and exact project-miner source bytes."""
    path = Path(miner_path).resolve()
    return {
        "progress_contract": MINE_PROGRESS_REVISION,
        "module_sha256": sha256_bytes(path.read_bytes()),
    }


def build_source_manifest(
    *,
    project_path: Union[str, os.PathLike],
    files: Iterable[Union[str, os.PathLike]],
    contract: Mapping[str, Any],
) -> dict:
    """Build a deterministic, content-bound source plan without mutating it."""
    root = Path(project_path).expanduser().resolve()
    descriptors = []
    seen_paths: set[str] = set()
    for raw_path in files:
        path = Path(raw_path).expanduser().resolve()
        try:
            relative = path.relative_to(root)
        except ValueError as exc:
            raise MinePlanError("source plan contains a path outside the project root") from exc
        relative_path = relative.as_posix()
        normalized_path = os.path.normcase(relative_path).replace("\\", "/")
        if normalized_path in seen_paths:
            raise MinePlanError("source plan contains a duplicate normalized path")
        seen_paths.add(normalized_path)
        descriptors.append(
            _source_descriptor(
                path=path,
                relative_path=relative_path,
                normalized_path=normalized_path,
            )
        )

    descriptors.sort(key=lambda item: (item["normalized_path"], item["relative_path"]))
    items = []
    for index, descriptor in enumerate(descriptors):
        unsigned = {"index": index, **descriptor}
        items.append({**unsigned, "item_digest": _digest(unsigned)})

    core = {
        "schema": MINE_MANIFEST_SCHEMA,
        "project_identity": sha256_bytes(os.path.normcase(str(root)).encode("utf-8")),
        "contract": _jsonable_mapping(contract),
        "source_count": len(items),
        "items": items,
    }
    return {
        **core,
        "manifest_digest": _digest(core),
        "created_at": utc_now(),
    }


def publish_source_manifest(
    path: Union[str, os.PathLike],
    manifest: Mapping[str, Any],
) -> dict:
    """Create one immutable manifest, or accept an exact existing copy."""
    validated = validate_source_manifest(manifest)
    target = Path(path).expanduser().resolve()
    if target.exists():
        current = load_source_manifest(target)
        if current["manifest_digest"] != validated["manifest_digest"]:
            raise MinePlanError("immutable mine manifest already exists with different content")
        return current
    try:
        _atomic_write_json(
            target,
            validated,
            immutable=True,
            durable=True,
            durability_anchor=target.parent,
        )
    except ReceiptConflictError as exc:
        try:
            current = load_source_manifest(target)
        except MinePlanError:
            raise MinePlanError(
                "immutable mine manifest already exists with different content"
            ) from exc
        if current["manifest_digest"] != validated["manifest_digest"]:
            raise MinePlanError(
                "immutable mine manifest already exists with different content"
            ) from exc
        return current
    return load_source_manifest(target)


def load_source_manifest(path: Union[str, os.PathLike]) -> dict:
    target = Path(path).expanduser().resolve()
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise MinePlanError("mine manifest is unreadable") from exc
    return validate_source_manifest(value)


def validate_source_manifest(value: Mapping[str, Any]) -> dict:
    if not isinstance(value, Mapping):
        raise MinePlanError("mine manifest must be a JSON object")
    manifest = dict(value)
    if manifest.get("schema") != MINE_MANIFEST_SCHEMA:
        raise MinePlanError("mine manifest schema is unsupported")
    if not _is_tagged_hash(manifest.get("project_identity")):
        raise MinePlanError("mine manifest project identity is invalid")
    contract = manifest.get("contract")
    if not isinstance(contract, dict):
        raise MinePlanError("mine manifest contract is invalid")
    items = manifest.get("items")
    if not isinstance(items, list):
        raise MinePlanError("mine manifest items are invalid")
    if manifest.get("source_count") != len(items):
        raise MinePlanError("mine manifest source count is inconsistent")

    seen: set[str] = set()
    for index, item in enumerate(items):
        _validate_item(item, expected_index=index)
        normalized = item["normalized_path"]
        if normalized in seen:
            raise MinePlanError("mine manifest contains duplicate normalized paths")
        seen.add(normalized)
    if [item["normalized_path"] for item in items] != sorted(
        (item["normalized_path"] for item in items)
    ):
        raise MinePlanError("mine manifest source order is not deterministic")

    core = {
        "schema": manifest["schema"],
        "project_identity": manifest["project_identity"],
        "contract": contract,
        "source_count": manifest["source_count"],
        "items": items,
    }
    if manifest.get("manifest_digest") != _digest(core):
        raise MinePlanError("mine manifest digest does not match its contents")
    created_at = manifest.get("created_at")
    if not isinstance(created_at, str) or not created_at:
        raise MinePlanError("mine manifest creation timestamp is invalid")
    return manifest


def validate_manifest_context(
    manifest: Mapping[str, Any],
    *,
    project_path: Union[str, os.PathLike],
    contract: Mapping[str, Any],
) -> None:
    root = Path(project_path).expanduser().resolve()
    expected_project = sha256_bytes(os.path.normcase(str(root)).encode("utf-8"))
    if manifest.get("project_identity") != expected_project:
        raise MinePlanError("mine manifest belongs to a different project root")
    if manifest.get("contract") != _jsonable_mapping(contract):
        raise MinePlanError("mine manifest parser, configuration, or miner revision changed")


def source_path_for_item(
    project_path: Union[str, os.PathLike],
    item: Mapping[str, Any],
) -> Path:
    """Resolve a relative item path and reject project-root escape."""
    root = Path(project_path).expanduser().resolve()
    path = (root / item["relative_path"]).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise MinePlanError("mine manifest source path escapes the project root") from exc
    return path


def validate_source_bytes(
    *,
    path: Path,
    project_path: Path,
    item: Mapping[str, Any],
    content: bytes,
    stat_before: os.stat_result,
    stat_after: os.stat_result,
) -> None:
    """Fail closed if locked source bytes no longer match their plan entry."""
    try:
        relative_path = path.resolve().relative_to(project_path.resolve()).as_posix()
    except ValueError as exc:
        raise MineManifestDrift("manifest source resolved outside the project root") from exc
    normalized_path = os.path.normcase(relative_path).replace("\\", "/")
    stable_stat = (
        stat_before.st_size == stat_after.st_size
        and stat_before.st_mtime_ns == stat_after.st_mtime_ns
    )
    matches = (
        stable_stat
        and item.get("relative_path") == relative_path
        and item.get("normalized_path") == normalized_path
        and item.get("size_bytes") == len(content) == stat_after.st_size
        and item.get("mtime_ns") == stat_after.st_mtime_ns
        and item.get("content_hash") == sha256_bytes(content)
    )
    if not matches:
        index = item.get("index")
        raise MineManifestDrift(f"source index {index} drifted from the immutable manifest")


class MineProgressJournal:
    """Read and durably append one sanitized contiguous source cursor."""

    def __init__(
        self,
        path: Union[str, os.PathLike],
        *,
        manifest: Mapping[str, Any],
    ):
        self.path = Path(path).expanduser().resolve()
        self.manifest = validate_source_manifest(manifest)
        self.manifest_digest = self.manifest["manifest_digest"]
        self.items = self.manifest["items"]
        self.recovered_torn_bytes = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def verified_prefix(self) -> int:
        return len(self.records())

    def records(self) -> list[dict]:
        """Return the validated, hash-chained contiguous progress records."""
        if not self.path.exists():
            return []
        try:
            raw = self.path.read_bytes()
        except OSError as exc:
            raise MineProgressError("mine progress journal is unreadable") from exc
        if not raw:
            return []
        if not raw.endswith(b"\n"):
            committed_end = raw.rfind(b"\n") + 1
            self.recovered_torn_bytes += len(raw) - committed_end
            raw = raw[:committed_end]
            self._truncate_torn_tail(committed_end)
            if not raw:
                return []

        records = []
        previous_digest = "sha256:" + "0" * 64
        for line_number, raw_line in enumerate(raw.splitlines(), 1):
            try:
                record = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
                raise MineProgressError(
                    f"mine progress record {line_number} is unreadable"
                ) from exc
            self._validate_record(
                record,
                expected_index=len(records),
                expected_previous_digest=previous_digest,
            )
            records.append(record)
            previous_digest = record["record_digest"]
        return records

    def _truncate_torn_tail(self, length: int) -> None:
        """Discard only bytes after the last committed newline and fsync."""
        try:
            fd = os.open(str(self.path), os.O_WRONLY)
            try:
                os.ftruncate(fd, length)
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError as exc:
            raise MineProgressError("torn mine progress tail could not be recovered") from exc

    def append_verified(
        self,
        *,
        source_index: int,
        source_identity: str,
        receipt: Mapping[str, Any],
        represented_count: int,
    ) -> dict:
        records = self.records()
        prefix = len(records)
        if source_index < prefix:
            return {"status": "already-recorded", "next_source_index": prefix}
        if source_index != prefix:
            raise MineProgressError("mine progress cannot skip an unverified source index")
        item = self.items[source_index]
        receipt_id = receipt.get("receipt_id")
        try:
            uuid.UUID(str(receipt_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise MineProgressError("verified receipt id is invalid") from exc
        if receipt.get("state") != "COMPLETE":
            raise MineProgressError("progress requires a terminal COMPLETE source receipt")
        disposition = receipt.get("disposition")
        if disposition not in {"WRITE", "UNCHANGED", "ZERO_OUTPUT"}:
            raise MineProgressError("verified receipt disposition is unsupported")
        if not _is_hmac_identity(source_identity):
            raise MineProgressError("verified source identity is invalid")
        if not isinstance(represented_count, int) or represented_count < 0:
            raise MineProgressError("represented output count is invalid")

        record = {
            "schema": MINE_PROGRESS_SCHEMA,
            "event": MINE_PROGRESS_EVENT,
            "recorded_at": utc_now(),
            "manifest_digest": self.manifest_digest,
            "source_index": source_index,
            "next_source_index": source_index + 1,
            "item_digest": item["item_digest"],
            "source_identity": source_identity,
            "receipt_id": str(receipt_id),
            "receipt_disposition": disposition,
            "verification_status": "represented",
            "represented_count": represented_count,
            "previous_record_digest": (
                records[-1]["record_digest"] if records else "sha256:" + "0" * 64
            ),
        }
        record["record_digest"] = _digest(record)
        data = (
            json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
        ).encode("utf-8")
        if len(data) > 4096:
            raise MineProgressError("sanitized progress record exceeds the atomic append bound")

        created = not self.path.exists()
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        try:
            fd = os.open(str(self.path), flags, 0o600)
            try:
                written = os.write(fd, data)
                if written != len(data):
                    raise MineProgressError("mine progress append was incomplete")
                os.fsync(fd)
            finally:
                os.close(fd)
            if created:
                _fsync_directory(self.path.parent)
        except MineProgressError:
            raise
        except OSError as exc:
            raise MineProgressError("mine progress append could not be made durable") from exc
        return record

    def _validate_record(
        self,
        record: Any,
        *,
        expected_index: int,
        expected_previous_digest: str,
    ) -> None:
        if not isinstance(record, dict):
            raise MineProgressError("mine progress record must be a JSON object")
        if record.get("schema") != MINE_PROGRESS_SCHEMA:
            raise MineProgressError("mine progress record schema is unsupported")
        if record.get("event") != MINE_PROGRESS_EVENT:
            raise MineProgressError("mine progress event is unsupported")
        if record.get("manifest_digest") != self.manifest_digest:
            raise MineProgressError("mine progress belongs to a different manifest")
        if record.get("source_index") != expected_index:
            raise MineProgressError("mine progress is not one contiguous source prefix")
        if record.get("next_source_index") != expected_index + 1:
            raise MineProgressError("mine progress next-source cursor is inconsistent")
        if record.get("previous_record_digest") != expected_previous_digest:
            raise MineProgressError("mine progress hash chain is inconsistent")
        unsigned = {key: value for key, value in record.items() if key != "record_digest"}
        if record.get("record_digest") != _digest(unsigned):
            raise MineProgressError("mine progress record digest does not match its contents")
        if expected_index >= len(self.items):
            raise MineProgressError("mine progress advances beyond the source manifest")
        if record.get("item_digest") != self.items[expected_index]["item_digest"]:
            raise MineProgressError("mine progress source identity diverges from the manifest")
        if record.get("verification_status") != "represented":
            raise MineProgressError("mine progress contains an unverified source")
        if not _is_hmac_identity(record.get("source_identity")):
            raise MineProgressError("mine progress source identity is invalid")
        try:
            uuid.UUID(str(record.get("receipt_id")))
        except (TypeError, ValueError, AttributeError) as exc:
            raise MineProgressError("mine progress receipt id is invalid") from exc
        if record.get("receipt_disposition") not in {"WRITE", "UNCHANGED", "ZERO_OUTPUT"}:
            raise MineProgressError("mine progress receipt disposition is invalid")
        represented_count = record.get("represented_count")
        if not isinstance(represented_count, int) or represented_count < 0:
            raise MineProgressError("mine progress represented count is invalid")
        if not isinstance(record.get("recorded_at"), str) or not record["recorded_at"]:
            raise MineProgressError("mine progress timestamp is invalid")


def _source_descriptor(*, path: Path, relative_path: str, normalized_path: str) -> dict:
    try:
        before = path.stat()
        if not stat.S_ISREG(before.st_mode):
            raise MinePlanError("source plan contains a non-regular file")
        content = path.read_bytes()
        after = path.stat()
    except OSError as exc:
        raise MineManifestDrift("source changed while the mine manifest was being built") from exc
    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
        raise MineManifestDrift("source changed while the mine manifest was being built")
    if len(content) != after.st_size:
        raise MineManifestDrift("source size changed while the mine manifest was being built")
    return {
        "relative_path": relative_path,
        "normalized_path": normalized_path,
        "size_bytes": after.st_size,
        "mtime_ns": after.st_mtime_ns,
        "content_hash": sha256_bytes(content),
    }


def _validate_item(item: Any, *, expected_index: int) -> None:
    if not isinstance(item, dict):
        raise MinePlanError("mine manifest item must be a JSON object")
    if item.get("index") != expected_index:
        raise MinePlanError("mine manifest item index is inconsistent")
    for key in ("relative_path", "normalized_path"):
        value = item.get(key)
        if not isinstance(value, str) or not value or "\x00" in value:
            raise MinePlanError("mine manifest source path is invalid")
    relative = Path(item["relative_path"])
    if relative.is_absolute() or ".." in relative.parts:
        raise MinePlanError("mine manifest source path is not project-relative")
    for key in ("size_bytes", "mtime_ns"):
        value = item.get(key)
        if not isinstance(value, int) or value < 0:
            raise MinePlanError("mine manifest source stat is invalid")
    if not _is_tagged_hash(item.get("content_hash")):
        raise MinePlanError("mine manifest source content hash is invalid")
    unsigned = {key: value for key, value in item.items() if key != "item_digest"}
    if item.get("item_digest") != _digest(unsigned):
        raise MinePlanError("mine manifest item digest does not match its contents")


def _jsonable_mapping(value: Mapping[str, Any]) -> dict:
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise MinePlanError("mine manifest contract is not JSON serializable") from exc
    if not isinstance(decoded, dict):
        raise MinePlanError("mine manifest contract must be an object")
    return decoded


def _digest(value: Mapping[str, Any]) -> str:
    data = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return sha256_bytes(data)


def _is_tagged_hash(value: Any) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("sha256:")
        and len(value) == len("sha256:") + 64
        and all(char in "0123456789abcdef" for char in value[len("sha256:") :])
    )


def _is_hmac_identity(value: Any) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("hmac-sha256:")
        and len(value) == len("hmac-sha256:") + 64
        and all(char in "0123456789abcdef" for char in value[len("hmac-sha256:") :])
    )


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
