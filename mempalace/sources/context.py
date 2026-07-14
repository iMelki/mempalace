"""``PalaceContext`` facade passed to source adapters (RFC 002 §9).

Bundles the palace-side surface an adapter needs during :meth:`ingest`:
drawer collection, closet collection, knowledge graph, palace config, and
progress hooks. Adapters receive a ``PalaceContext`` instance and MUST NOT
import ``mempalace.palace`` directly — that coupling is what the facade
exists to prevent.

Adapters are trusted in-process code, not a security sandbox. During managed
ingest they receive receipt-aware collection wrappers. Raw mutable backend
handles live in an out-of-band weak capability registry, never in the context
or proxy object graph exposed to adapters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol

from ._context_capabilities import (
    _collection_proxy_delete,
    _collection_proxy_read,
    _collection_proxy_write,
    _graph_proxy_add_triple,
    _managed_collection_names,
    _register_collection_proxy,
    _register_context,
    _register_graph_proxy,
)
from .base import DrawerRecord


class _CollectionLike(Protocol):
    """Minimum of :class:`mempalace.backends.BaseCollection` adapters rely on.

    Declared as a Protocol so tests and third-party adapters can substitute
    any object with compatible method signatures without importing the
    concrete backend. See ``mempalace/backends/base.py`` for the full surface.
    """

    def add(self, **kwargs: Any) -> None: ...
    def upsert(self, **kwargs: Any) -> None: ...
    def update(self, **kwargs: Any) -> None: ...
    def query(self, **kwargs: Any) -> Any: ...
    def get(self, **kwargs: Any) -> Any: ...
    def delete(self, **kwargs: Any) -> None: ...
    def count(self) -> int: ...


class _KnowledgeGraphLike(Protocol):
    def add_triple(self, subject: str, predicate: str, obj: str, **kwargs: Any) -> Any: ...


# Progress hook signature: ``fn(event_name, **details) -> None``.
ProgressHook = Callable[..., None]


class _ReceiptAwareCollection:
    """Contain adapter collection writes inside the active receipt session."""

    __slots__ = ("__weakref__",)

    def __init__(
        self,
        context: "PalaceContext",
        *,
        collection_name: str,
        kind: str,
    ):
        _register_collection_proxy(
            self,
            context,
            collection_name=collection_name,
            kind=kind,
        )

    def add(self, **kwargs: Any) -> None:
        self._write("add", kwargs)

    def upsert(self, **kwargs: Any) -> None:
        self._write("upsert", kwargs)

    def update(self, **kwargs: Any) -> None:
        self._write("update", kwargs)

    def delete(self, **kwargs: Any) -> None:
        _collection_proxy_delete(self, kwargs)

    def query(self, **kwargs: Any) -> Any:
        return _collection_proxy_read(self, "query", kwargs)

    def get(self, **kwargs: Any) -> Any:
        return _collection_proxy_read(self, "get", kwargs)

    def count(self) -> int:
        return _collection_proxy_read(self, "count", {})

    def _write(self, method: str, kwargs: dict[str, Any]) -> None:
        _collection_proxy_write(self, method, kwargs)


class _ReceiptAwareKnowledgeGraph:
    """Fail closed while managed receipts cannot represent graph mutations."""

    __slots__ = ("__weakref__",)

    def __init__(self, context: "PalaceContext"):
        _register_graph_proxy(self, context)

    def add_triple(self, subject: str, predicate: str, obj: str, **kwargs: Any) -> Any:
        return _graph_proxy_add_triple(self, subject, predicate, obj, kwargs)


@dataclass(eq=False)
class PalaceContext:
    """Per-mine-invocation facade passed to :meth:`BaseSourceAdapter.ingest`.

    Fields:
        drawer_collection: The palace's drawer collection (via RFC 001 backend).
        closet_collection: The palace's closet collection, or ``None`` if the
            palace has no closets yet. Managed writes use the same receipt-aware
            boundary as drawers.
        knowledge_graph: The palace's SQLite knowledge graph. Managed adapter
            operations currently fail closed because V1 receipts cannot attest
            graph mutations.
        palace_path: Filesystem root of the palace (convenience; same as
            ``backend.PalaceRef.local_path``).
        config: Palace config object (hall keywords, rooms list, privacy
            floor, etc.). Shape is the existing :class:`MempalaceConfig`.
        adapter_name: Name of the adapter currently ingesting; populated by
            core so drawers can carry ``metadata["adapter_name"]``.
        adapter_version: Version of the adapter currently ingesting.
        progress_hooks: Optional callables core invokes on progress events.

    Methods are intentionally thin wrappers so the concrete mine loop in
    core can swap implementations without changing adapter code.
    """

    drawer_collection: _CollectionLike
    knowledge_graph: _KnowledgeGraphLike
    palace_path: str
    closet_collection: Optional[_CollectionLike] = None
    config: Optional[Any] = None
    adapter_name: str = ""
    adapter_version: str = ""
    progress_hooks: list[ProgressHook] = field(default_factory=list)
    receipt_session: Optional[Any] = None
    _managed_ingest: bool = field(default=False, init=False, repr=False)
    _active_source_file: Optional[str] = field(default=None, init=False, repr=False)
    _active_source_local_path: bool = field(default=False, init=False, repr=False)

    # Internal: flag set by :meth:`skip_current_item` and checked by the core
    # mine loop between yields. Not part of the adapter-facing contract; the
    # adapter only needs to know that calling :meth:`skip_current_item` stops
    # drawer emission for the current ``SourceItemMetadata``.
    _skip_requested: bool = False

    def __post_init__(self) -> None:
        raw_drawers = self.drawer_collection
        raw_closets = self.closet_collection
        raw_graph = self.knowledge_graph
        _register_context(
            self,
            drawer_collection=raw_drawers,
            closet_collection=raw_closets,
            knowledge_graph=raw_graph,
        )
        self.drawer_collection = _ReceiptAwareCollection(
            self,
            collection_name="drawers",
            kind="drawer",
        )
        if raw_closets is not None:
            self.closet_collection = _ReceiptAwareCollection(
                self,
                collection_name="closets",
                kind="closet",
            )
        self.knowledge_graph = _ReceiptAwareKnowledgeGraph(self)

    # ------------------------------------------------------------------
    # Adapter-facing surface
    # ------------------------------------------------------------------

    def upsert_drawer(self, record: DrawerRecord) -> str:
        """Persist a ``DrawerRecord`` to the drawer collection.

        Applies the spec-mandated ``adapter_name`` and ``adapter_version``
        metadata stamps (§5.1) so adapters never need to populate them.
        """
        meta = dict(record.metadata)
        source_file = self._active_source_file or record.source_file
        if self.receipt_session is not None:
            meta["source_file"] = source_file
        else:
            meta.setdefault("source_file", source_file)
        meta.setdefault("chunk_index", record.chunk_index)
        if self.adapter_name:
            meta.setdefault("adapter_name", self.adapter_name)
        if self.adapter_version:
            meta.setdefault("adapter_version", self.adapter_version)
        drawer_id = _build_drawer_id(record, source_file=source_file)
        self.drawer_collection.upsert(
            documents=[record.content],
            ids=[drawer_id],
            metadatas=[meta],
        )
        return drawer_id

    def _begin_managed_ingest(self) -> None:
        if self._managed_ingest:
            raise RuntimeError("PalaceContext is already driving a managed adapter")
        self._managed_ingest = True

    def _activate_receipt(self, session: Any, source_file: str, *, local_path: bool) -> None:
        from ..write_receipts import canonical_source_locator

        if not self._managed_ingest:
            raise RuntimeError("managed receipt activated outside managed ingest")
        self.receipt_session = session
        self._active_source_file = canonical_source_locator(source_file, local_path=local_path)
        self._active_source_local_path = local_path

    def _clear_receipt(self) -> None:
        self.receipt_session = None
        self._active_source_file = None
        self._active_source_local_path = False
        self._skip_requested = False

    def _end_managed_ingest(self) -> None:
        self._clear_receipt()
        self._managed_ingest = False

    def _managed_collection_names(self) -> tuple[str, ...]:
        """Return managed collection names without exposing backend handles."""
        return _managed_collection_names(self)

    def skip_current_item(self) -> None:
        """Signal to core that the current ``SourceItemMetadata`` is up-to-date
        and no drawers should be emitted for it. Core resets the flag after
        advancing past the item."""
        self._skip_requested = True

    def emit(self, event: str, **details: Any) -> None:
        """Invoke each registered progress hook with ``(event, **details)``."""
        for hook in self.progress_hooks:
            try:
                hook(event, **details)
            except Exception:  # pragma: no cover - hook errors never fail mine
                import logging

                logging.getLogger(__name__).exception("progress hook failed on %r", event)


def _build_drawer_id(record: DrawerRecord, *, source_file: Optional[str] = None) -> str:
    """Deterministic drawer id: ``<sha256(source_file)[:24]>_<chunk_index>``.

    Matches the shape existing miners rely on (``source_file`` + chunk index
    pair) while keeping the id chroma-safe (no separators that collide with
    existing metadata values). 96-bit SHA-256 prefix keeps collision risk
    negligible across corpora the size of a palace (sha1@64 bits was too
    close to the birthday bound for large ingests). Adapters that need a
    different id scheme can call ``drawer_collection.upsert`` directly; during
    managed ingestion that facade still validates, stamps, and receipts every
    document write.
    """
    import hashlib

    resolved_source = record.source_file if source_file is None else source_file
    digest = hashlib.sha256(resolved_source.encode("utf-8")).hexdigest()[:24]
    return f"{digest}_{record.chunk_index}"
