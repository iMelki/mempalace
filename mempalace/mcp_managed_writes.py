"""Receipt-aware mutation service for MCP drawer and diary tools."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from .palace import mine_palace_lock
from .provenance import managed_adapter_ingest
from .receipt_verifier import verify_receipt
from .sources.base import AdapterSchema, BaseSourceAdapter, SourceItemMetadata, SourceRef
from .sources.context import PalaceContext
from .write_receipts import (
    META_SOURCE_IDENTITY,
    ReceiptConflictError,
    ReceiptIdentityError,
    ReceiptStore,
    _collection_rows_for_ids,
    managed_write_scope,
    sha256_bytes,
)

_CONTRACT = "mempalace-mcp-managed-write/v1"
_ADAPTER_NAME = "mcp-managed-drawer"
_ADAPTER_VERSION = "1.1.0"
_MAX_SOURCE_ID_LENGTH = 512
MCP_SEMANTIC_METADATA_HASH = "mcp_semantic_metadata_hash"
_VOLATILE_METADATA_FIELDS = frozenset(
    {
        "source_file",
        "filed_at",
        "updated_at",
        "date",
        MCP_SEMANTIC_METADATA_HASH,
    }
)


@dataclass(frozen=True)
class ManagedMcpMutationResult:
    """Verified result of one managed MCP row mutation."""

    drawer_id: str
    receipt_id: str
    disposition: str
    unchanged: bool
    verification_status: str


class _ManagedMcpRowAdapter(BaseSourceAdapter):
    """Present one caller-owned MCP row as a managed logical source."""

    name = _ADAPTER_NAME
    adapter_version = _ADAPTER_VERSION
    capabilities = frozenset({"supports_incremental"})
    supported_modes = frozenset({"whole_record"})
    empty_output_disposition = "ZERO_OUTPUT"

    def __init__(
        self,
        *,
        source_uri: str,
        source_content_hash: str,
        source_size_bytes: int,
        drawer_id: str,
        content: Optional[str],
        metadata: Mapping[str, Any],
        semantic_metadata: Mapping[str, Any],
        semantic_metadata_hash: str,
        embedding: Optional[Any] = None,
    ) -> None:
        self.source_uri = source_uri
        self.source_content_hash = source_content_hash
        self.source_size_bytes = source_size_bytes
        self.drawer_id = drawer_id
        self.content = content
        self.metadata = dict(metadata)
        self.semantic_metadata = dict(semantic_metadata)
        self.semantic_metadata_hash = semantic_metadata_hash
        self.embedding = embedding

    def ingest(self, *, source: SourceRef, palace: PalaceContext):
        if source.uri != self.source_uri:
            raise ReceiptIdentityError("MCP adapter source URI changed before ingestion")
        yield SourceItemMetadata(
            source_file=self.source_uri,
            version=self.source_content_hash,
            size_hint=self.source_size_bytes,
            content_hash=self.source_content_hash,
        )
        if palace._skip_requested or self.content is None:
            return
        write_kwargs = {
            "ids": [self.drawer_id],
            "documents": [self.content],
            "metadatas": [self.metadata],
        }
        if self.embedding is not None:
            write_kwargs["embeddings"] = [[float(value) for value in self.embedding]]
        palace.drawer_collection.upsert(**write_kwargs)

    def describe_schema(self) -> AdapterSchema:
        return AdapterSchema(fields={}, version="1")

    def is_current(self, *, item: SourceItemMetadata, existing_metadata: Optional[dict]) -> bool:
        del item
        if existing_metadata is None:
            return False
        actual_semantic = _semantic_metadata(existing_metadata)
        actual_hash = _semantic_metadata_hash(actual_semantic)
        return (
            existing_metadata.get("source_file") == self.source_uri
            and actual_semantic == self.semantic_metadata
            and actual_hash == self.semantic_metadata_hash
            and existing_metadata.get(MCP_SEMANTIC_METADATA_HASH) == self.semantic_metadata_hash
        )


class ManagedMcpMutationService:
    """Bind MCP mutations to one stable source identity and exact receipt."""

    def __init__(
        self,
        *,
        palace_path: str,
        drawer_collection: Any,
        knowledge_graph: Any,
        closet_collection: Optional[Any] = None,
        caller: str = "mcp",
    ) -> None:
        self.palace_path = palace_path
        self.drawer_collection = drawer_collection
        self.closet_collection = closet_collection
        self.knowledge_graph = knowledge_graph
        self.caller = caller
        self.receipt_store = ReceiptStore(palace_path)

    @property
    def managed_collections(self) -> dict[str, Any]:
        collections = {"drawers": self.drawer_collection}
        if self.closet_collection is not None:
            collections["closets"] = self.closet_collection
        return collections

    @staticmethod
    def drawer_id_for(source_id: str) -> str:
        source_uri = _source_uri("drawer", source_id)
        suffix = hashlib.sha256(source_uri.encode("utf-8")).hexdigest()[:24]
        return f"drawer_mcp_{suffix}"

    @staticmethod
    def diary_entry_id_for(*, source_id: str, agent: str, wing: str) -> str:
        source_uri = _diary_source_uri(source_id=source_id, agent=agent, wing=wing)
        suffix = hashlib.sha256(source_uri.encode("utf-8")).hexdigest()[:24]
        return f"diary_mcp_{suffix}"

    def write_drawer(
        self,
        *,
        source_id: str,
        drawer_id: str,
        content: str,
        metadata: Mapping[str, Any],
        embedding: Optional[Any] = None,
    ) -> ManagedMcpMutationResult:
        with managed_write_scope(self.palace_path, lock_factory=mine_palace_lock):
            self._require_drawer_id(source_id, drawer_id)
            source_uri = _source_uri("drawer", source_id)
            self._assert_current_output_shape(source_uri, drawer_id, allow_absent=True)
            return self._mutate(
                source_uri=source_uri,
                drawer_id=drawer_id,
                content=content,
                metadata=metadata,
                embedding=embedding,
            )

    def require_owned_drawer(self, *, source_id: str, drawer_id: str) -> tuple[str, dict, Any]:
        """Return an exact managed row only when its current receipt owns it."""
        with managed_write_scope(self.palace_path, lock_factory=mine_palace_lock):
            self._require_drawer_id(source_id, drawer_id)
            source_uri = _source_uri("drawer", source_id)
            current = self._assert_current_output_shape(source_uri, drawer_id, allow_absent=False)
            verification = verify_receipt(
                current,
                collections=self.managed_collections,
                store=self.receipt_store,
            )
            if verification.status != "represented":
                raise ReceiptConflictError(
                    f"current source receipt is not exactly represented: {verification.status}"
                )
            rows = _collection_rows_for_ids(
                self.drawer_collection,
                [drawer_id],
                require_all=False,
                include_embeddings=True,
            )
            if drawer_id not in rows:
                raise ReceiptConflictError(f"managed drawer is missing: {drawer_id}")
            document, metadata, embedding = rows[drawer_id]
            self._assert_exact_row(
                receipt=current,
                source_uri=source_uri,
                drawer_id=drawer_id,
                content=document,
                semantic_metadata=_semantic_metadata(metadata),
            )
            return document, dict(metadata), embedding

    def delete_drawer(
        self,
        *,
        source_id: str,
        drawer_id: str,
    ) -> ManagedMcpMutationResult:
        with managed_write_scope(self.palace_path, lock_factory=mine_palace_lock):
            self._require_drawer_id(source_id, drawer_id)
            source_uri = _source_uri("drawer", source_id)
            current = self.receipt_store.find_current(
                self.receipt_store.source_identity(source_uri)
            )
            if current is not None and current.get("disposition") == "ZERO_OUTPUT":
                outputs = current.get("outputs", {}).get("identities", [])
                if outputs:
                    raise ReceiptConflictError(
                        "zero-output receipt unexpectedly names stored outputs"
                    )
                self._assert_target_absent(drawer_id)
                verification = verify_receipt(
                    current,
                    collections=self.managed_collections,
                    store=self.receipt_store,
                )
                if verification.status != "represented":
                    raise ReceiptConflictError(
                        f"deleted source receipt is not exactly represented: {verification.status}"
                    )
                return ManagedMcpMutationResult(
                    drawer_id=drawer_id,
                    receipt_id=current["receipt_id"],
                    disposition=current["disposition"],
                    unchanged=True,
                    verification_status=verification.status,
                )

            self.require_owned_drawer(source_id=source_id, drawer_id=drawer_id)
            return self._mutate(
                source_uri=source_uri,
                drawer_id=drawer_id,
                content=None,
                metadata={},
                embedding=None,
            )

    def write_diary_entry(
        self,
        *,
        source_id: str,
        agent: str,
        wing: str,
        entry_id: str,
        entry: str,
        metadata: Mapping[str, Any],
    ) -> ManagedMcpMutationResult:
        with managed_write_scope(self.palace_path, lock_factory=mine_palace_lock):
            expected_id = self.diary_entry_id_for(
                source_id=source_id,
                agent=agent,
                wing=wing,
            )
            if entry_id != expected_id:
                raise ReceiptIdentityError(
                    "diary entry ID does not match its agent, wing, and logical source"
                )
            source_uri = _diary_source_uri(source_id=source_id, agent=agent, wing=wing)
            self._assert_current_output_shape(source_uri, entry_id, allow_absent=True)
            return self._mutate(
                source_uri=source_uri,
                drawer_id=entry_id,
                content=entry,
                metadata=metadata,
                embedding=None,
            )

    def _mutate(
        self,
        *,
        source_uri: str,
        drawer_id: str,
        content: Optional[str],
        metadata: Mapping[str, Any],
        embedding: Optional[Any],
    ) -> ManagedMcpMutationResult:
        semantic_metadata = _semantic_metadata(metadata)
        semantic_hash = _semantic_metadata_hash(semantic_metadata)
        stored_metadata = _stored_metadata(metadata, semantic_hash=semantic_hash)
        payload_bytes = _identity_payload_bytes(
            drawer_id=drawer_id,
            content=content,
            semantic_metadata=semantic_metadata,
        )
        content_hash = sha256_bytes(payload_bytes)
        adapter = _ManagedMcpRowAdapter(
            source_uri=source_uri,
            source_content_hash=content_hash,
            source_size_bytes=len(payload_bytes),
            drawer_id=drawer_id,
            content=content,
            metadata=stored_metadata,
            semantic_metadata=semantic_metadata,
            semantic_metadata_hash=semantic_hash,
            embedding=embedding,
        )
        context = PalaceContext(
            drawer_collection=self.drawer_collection,
            closet_collection=self.closet_collection,
            knowledge_graph=self.knowledge_graph,
            palace_path=self.palace_path,
            adapter_name=adapter.name,
            adapter_version=adapter.adapter_version,
        )
        result = managed_adapter_ingest(
            adapter=adapter,
            source=SourceRef(uri=source_uri),
            palace=context,
            receipt_store=self.receipt_store,
            caller=self.caller,
            config={"contract": _CONTRACT},
        )
        current = self.receipt_store.find_current(
            self.receipt_store.source_identity(source_uri),
            content_hash=content_hash,
        )
        if current is None:
            raise ReceiptConflictError("managed MCP mutation did not publish a current receipt")
        verification = verify_receipt(
            current,
            collections=self.managed_collections,
            current_source_content_hash=content_hash,
            store=self.receipt_store,
        )
        if verification.status != "represented":
            raise ReceiptConflictError(
                f"managed MCP mutation receipt is not exactly represented: {verification.status}"
            )
        expected_ids = [] if content is None else [drawer_id]
        actual_ids = [
            item["id"]
            for item in current.get("outputs", {}).get("identities", [])
            if item.get("collection") == "drawers"
        ]
        if actual_ids != expected_ids:
            raise ReceiptConflictError(
                "managed MCP mutation published an unexpected output manifest"
            )
        if content is None:
            self._assert_target_absent(drawer_id)
        else:
            self._assert_exact_row(
                receipt=current,
                source_uri=source_uri,
                drawer_id=drawer_id,
                content=content,
                semantic_metadata=semantic_metadata,
            )
        return ManagedMcpMutationResult(
            drawer_id=drawer_id,
            receipt_id=current["receipt_id"],
            disposition=current["disposition"],
            unchanged=result.sources_unchanged == 1,
            verification_status=verification.status,
        )

    def _assert_exact_row(
        self,
        *,
        receipt: Mapping[str, Any],
        source_uri: str,
        drawer_id: str,
        content: str,
        semantic_metadata: Mapping[str, Any],
    ) -> None:
        rows = _collection_rows_for_ids(
            self.drawer_collection,
            [drawer_id],
            require_all=False,
        )
        if drawer_id not in rows:
            raise ReceiptConflictError(f"managed drawer is missing: {drawer_id}")
        actual_document, actual_metadata, _ = rows[drawer_id]
        actual_semantic = _semantic_metadata(actual_metadata)
        semantic_hash = _semantic_metadata_hash(semantic_metadata)
        expected_source_identity = self.receipt_store.source_identity(source_uri)
        expected_source_hash = sha256_bytes(
            _identity_payload_bytes(
                drawer_id=drawer_id,
                content=content,
                semantic_metadata=semantic_metadata,
            )
        )
        if actual_document != content or actual_semantic != dict(semantic_metadata):
            raise ReceiptConflictError("managed MCP row semantic readback did not match")
        if (
            actual_metadata.get(MCP_SEMANTIC_METADATA_HASH) != semantic_hash
            or _semantic_metadata_hash(actual_semantic) != semantic_hash
        ):
            raise ReceiptConflictError("managed MCP row semantic metadata hash did not match")
        if (
            actual_metadata.get("source_file") != source_uri
            or actual_metadata.get(META_SOURCE_IDENTITY) != expected_source_identity
        ):
            raise ReceiptConflictError("managed MCP row source identity did not match")
        if receipt.get("source", {}).get("content_hash") != expected_source_hash:
            raise ReceiptConflictError("managed MCP receipt does not attest the row semantics")

    def _assert_target_absent(self, drawer_id: str) -> None:
        if _collection_rows_for_ids(
            self.drawer_collection,
            [drawer_id],
            require_all=False,
        ):
            raise ReceiptConflictError(
                "deleted drawer ID is present even though its current receipt has no output"
            )

    def _require_drawer_id(self, source_id: str, drawer_id: str) -> None:
        expected = self.drawer_id_for(source_id)
        if drawer_id != expected:
            raise ReceiptIdentityError(
                "drawer_id does not match the supplied logical source identity; "
                "legacy rows need an explicit provenance migration"
            )

    def _assert_current_output_shape(
        self,
        source_uri: str,
        drawer_id: str,
        *,
        allow_absent: bool,
    ) -> Optional[dict]:
        source_identity = self.receipt_store.source_identity(source_uri)
        current = self.receipt_store.find_current(source_identity)
        if current is None:
            if allow_absent:
                existing = _collection_rows_for_ids(
                    self.drawer_collection,
                    [drawer_id],
                    require_all=False,
                )
                if drawer_id in existing:
                    raise ReceiptConflictError(
                        "drawer ID already exists without the supplied source receipt"
                    )
                return None
            raise ReceiptIdentityError(
                "drawer has no managed receipt for the supplied logical source; "
                "legacy rows need an explicit provenance migration"
            )
        outputs = current.get("outputs", {}).get("identities", [])
        drawer_outputs = [item for item in outputs if item.get("collection") == "drawers"]
        if len(drawer_outputs) != 1 or drawer_outputs[0].get("id") != drawer_id:
            if allow_absent and not drawer_outputs and current.get("disposition") == "ZERO_OUTPUT":
                return current
            raise ReceiptConflictError(
                "logical source does not own exactly the requested drawer output"
            )
        return current


def validate_source_id(source_id: Optional[str]) -> str:
    """Validate a stable caller-selected identity without treating it as a path."""
    if not isinstance(source_id, str) or not source_id.strip():
        raise ReceiptIdentityError(
            "source_id is required; use one stable ID for the real note, message, or record"
        )
    value = source_id.strip()
    if len(value) > _MAX_SOURCE_ID_LENGTH:
        raise ReceiptIdentityError(f"source_id must be at most {_MAX_SOURCE_ID_LENGTH} characters")
    if any(ord(character) < 32 for character in value):
        raise ReceiptIdentityError("source_id must not contain control characters")
    return value


def _source_uri(namespace: str, source_id: str) -> str:
    validated = validate_source_id(source_id)
    return _opaque_source_uri(namespace, validated)


def _diary_source_uri(*, source_id: str, agent: str, wing: str) -> str:
    validated = validate_source_id(source_id)
    if not isinstance(agent, str) or not agent:
        raise ReceiptIdentityError("diary agent is required")
    if not isinstance(wing, str) or not wing:
        raise ReceiptIdentityError("diary wing is required")
    scoped = _canonical_json_bytes(
        {
            "agent": agent,
            "source_id": validated,
            "wing": wing,
        }
    ).decode("utf-8")
    return _opaque_source_uri("diary", scoped)


def _opaque_source_uri(namespace: str, logical_identity: str) -> str:
    token = hashlib.sha256(f"{namespace}\0{logical_identity}".encode("utf-8")).hexdigest()
    return f"mempalace://mcp/{namespace}/{token}"


def _semantic_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): value
        for key, value in metadata.items()
        if not str(key).startswith("write_") and str(key) not in _VOLATILE_METADATA_FIELDS
    }


def _semantic_metadata_hash(metadata: Mapping[str, Any]) -> str:
    return sha256_bytes(_canonical_json_bytes(dict(metadata)))


def _stored_metadata(metadata: Mapping[str, Any], *, semantic_hash: str) -> dict[str, Any]:
    stored = {
        str(key): value
        for key, value in metadata.items()
        if not str(key).startswith("write_")
        and str(key) not in {"source_file", MCP_SEMANTIC_METADATA_HASH}
    }
    stored[MCP_SEMANTIC_METADATA_HASH] = semantic_hash
    return stored


def _identity_payload_bytes(
    *,
    drawer_id: str,
    content: Optional[str],
    semantic_metadata: Mapping[str, Any],
) -> bytes:
    return _canonical_json_bytes(
        {
            "schema": _CONTRACT,
            "operation": "delete" if content is None else "write",
            "drawer_id": drawer_id,
            "content": content,
            "semantic_metadata": dict(semantic_metadata),
        }
    )


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


__all__ = [
    "MCP_SEMANTIC_METADATA_HASH",
    "ManagedMcpMutationResult",
    "ManagedMcpMutationService",
    "validate_source_id",
]
