"""Narrow operations over adapter-facing :mod:`sources.context` objects.

The raw backend objects are held in closure-owned weak registries.  This is
not a Python security sandbox--source adapters are trusted in-process code--
but the normal importable API never returns a mutable backend handle.
"""

from __future__ import annotations

import weakref
from threading import RLock
from typing import Any, Optional


def _build_capability_operations():  # noqa: C901 - one closure owns every raw capability
    lock = RLock()
    contexts: weakref.WeakKeyDictionary[Any, dict[str, Any]] = weakref.WeakKeyDictionary()
    collection_proxies: weakref.WeakKeyDictionary[
        Any, tuple[weakref.ReferenceType[Any], str, str]
    ] = weakref.WeakKeyDictionary()
    graph_proxies: weakref.WeakKeyDictionary[Any, weakref.ReferenceType[Any]] = (
        weakref.WeakKeyDictionary()
    )

    def collections_for(context: Any) -> dict[str, Any]:
        with lock:
            capability = contexts.get(context)
        if capability is None:
            raise RuntimeError("PalaceContext capability is unavailable")
        return {
            name: capability[name]
            for name in ("drawers", "closets")
            if capability.get(name) is not None
        }

    def collection_for(proxy: Any) -> tuple[Any, Any, str, str]:
        with lock:
            capability = collection_proxies.get(proxy)
        if capability is None:
            raise RuntimeError("collection proxy capability is unavailable")
        context = capability[0]()
        if context is None:
            raise RuntimeError("PalaceContext is no longer available")
        collection_name, kind = capability[1:]
        collections = collections_for(context)
        try:
            collection = collections[collection_name]
        except KeyError as exc:  # pragma: no cover - registration is atomic
            raise RuntimeError("collection proxy capability is inconsistent") from exc
        return context, collection, collection_name, kind

    def register_context(
        context: Any,
        *,
        drawer_collection: Any,
        closet_collection: Optional[Any],
        knowledge_graph: Any,
    ) -> None:
        capability = {
            "drawers": drawer_collection,
            "closets": closet_collection,
            "knowledge_graph": knowledge_graph,
        }
        with lock:
            if context in contexts:
                raise RuntimeError("PalaceContext capability is already registered")
            contexts[context] = capability

    def register_collection_proxy(
        proxy: Any,
        context: Any,
        *,
        collection_name: str,
        kind: str,
    ) -> None:
        if collection_name not in collections_for(context):
            raise RuntimeError("collection proxy capability is inconsistent")
        with lock:
            collection_proxies[proxy] = (weakref.ref(context), collection_name, kind)

    def register_graph_proxy(proxy: Any, context: Any) -> None:
        collections_for(context)
        with lock:
            graph_proxies[proxy] = weakref.ref(context)

    def collection_proxy_write(proxy: Any, method: str, kwargs: dict[str, Any]) -> None:
        if method not in {"add", "upsert", "update"}:
            raise RuntimeError("unsupported collection proxy write")
        context, collection, collection_name, kind = collection_for(proxy)
        if not context._managed_ingest:
            getattr(collection, method)(**kwargs)
            return

        from ..write_receipts import ReceiptIdentityError, write_receipted_collection_batch

        session = context.receipt_session
        if session is None or context._active_source_file is None:
            raise ReceiptIdentityError("managed adapter writes require an active source receipt")
        write_receipted_collection_batch(
            collection,
            method,
            kwargs,
            session=session,
            source_file=context._active_source_file,
            collection_name=collection_name,
            kind=kind,
            local_path=context._active_source_local_path,
        )

    def collection_proxy_delete(proxy: Any, kwargs: dict[str, Any]) -> None:
        context, collection, collection_name, _ = collection_for(proxy)
        if context._managed_ingest:
            from ..write_receipts import ReceiptIdentityError

            raise ReceiptIdentityError(
                f"managed adapters cannot delete through PalaceContext.{collection_name}"
            )
        collection.delete(**kwargs)

    def collection_proxy_read(proxy: Any, method: str, kwargs: dict[str, Any]) -> Any:
        if method not in {"query", "get", "count"}:
            raise RuntimeError("unsupported collection proxy read")
        _, collection, _, _ = collection_for(proxy)
        if method == "count":
            if kwargs:
                raise TypeError("collection count does not accept keyword arguments")
            return collection.count()
        return getattr(collection, method)(**kwargs)

    def graph_proxy_add_triple(
        proxy: Any,
        subject: str,
        predicate: str,
        obj: str,
        kwargs: dict[str, Any],
    ) -> Any:
        with lock:
            context_ref = graph_proxies.get(proxy)
        if context_ref is None:
            raise RuntimeError("knowledge-graph proxy capability is unavailable")
        context = context_ref()
        if context is None:
            raise RuntimeError("PalaceContext is no longer available")
        if context._managed_ingest:
            from ..write_receipts import ReceiptIdentityError

            raise ReceiptIdentityError(
                "managed adapter knowledge-graph operations are not receipt-representable"
            )
        with lock:
            capability = contexts.get(context)
        if capability is None:
            raise RuntimeError("PalaceContext capability is unavailable")
        return capability["knowledge_graph"].add_triple(subject, predicate, obj, **kwargs)

    def managed_collection_names(context: Any) -> tuple[str, ...]:
        return tuple(sorted(collections_for(context)))

    def reconcile_pending_rewrites(context: Any, store: Any) -> tuple[dict, ...]:
        from ..write_receipts import ReceiptIdentityError, ReceiptStore

        if type(store) is not ReceiptStore:
            raise ReceiptIdentityError("managed recovery requires the core ReceiptStore")
        return ReceiptStore.reconcile_pending_rewrites(store, collections_for(context))

    def finalize_rewrite_recovery(
        context: Any,
        store: Any,
        source_identity: str,
        receipt_id: str,
    ) -> bool:
        from ..write_receipts import ReceiptIdentityError, ReceiptStore

        if type(store) is not ReceiptStore:
            raise ReceiptIdentityError("managed recovery requires the core ReceiptStore")
        return ReceiptStore.finalize_rewrite_recovery(
            store,
            source_identity,
            receipt_id,
            collections=collections_for(context),
        )

    def discard_rewrite_recovery(
        context: Any,
        store: Any,
        source_identity: str,
        receipt_id: str,
    ) -> bool:
        from ..write_receipts import ReceiptIdentityError, ReceiptStore

        if type(store) is not ReceiptStore:
            raise ReceiptIdentityError("managed recovery requires the core ReceiptStore")
        return ReceiptStore.discard_rewrite_recovery(
            store,
            source_identity,
            receipt_id,
            collections=collections_for(context),
        )

    def verify_context_receipt(
        context: Any,
        receipt: Any,
        *,
        current_source_content_hash: str,
        store: Any,
    ) -> Any:
        from ..receipt_verifier import verify_receipt

        return verify_receipt(
            receipt,
            collections=collections_for(context),
            current_source_content_hash=current_source_content_hash,
            store=store,
        )

    def complete_reused_context_receipt(
        context: Any,
        session: Any,
        prior: Any,
        *,
        source_file: str,
        source_aliases: tuple[str, ...] = (),
    ) -> Any:
        from ..write_receipts import complete_reused_receipt

        return complete_reused_receipt(
            session,
            prior,
            collections=collections_for(context),
            source_file=source_file,
            local_path=context._active_source_local_path,
            source_aliases=source_aliases,
        )

    def snapshot_managed_source(
        context: Any,
        source_file: str,
        source_identity: str,
        *,
        source_aliases: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        from ..write_receipts import snapshot_managed_source_rows

        return {
            name: snapshot_managed_source_rows(
                collection,
                source_file=source_file,
                source_identity=source_identity,
                local_path=context._active_source_local_path,
                source_aliases=source_aliases,
            )
            for name, collection in collections_for(context).items()
        }

    def purge_managed_collection_snapshot(
        context: Any,
        name: str,
        snapshot: Any,
        *,
        recovery_path: Any,
        source_file: str,
        source_identity: str,
        source_aliases: tuple[str, ...] = (),
    ) -> list[str]:
        from ..write_receipts import ReceiptIdentityError, purge_managed_source_snapshot

        collections = collections_for(context)
        if name not in collections:
            raise ReceiptIdentityError(f"unknown managed collection: {name}")
        return purge_managed_source_snapshot(
            collections[name],
            snapshot,
            recovery_path=recovery_path,
            collection_name=name,
            source_file=source_file,
            source_identity=source_identity,
            local_path=context._active_source_local_path,
            source_aliases=source_aliases,
        )

    def rollback_managed_source(
        context: Any,
        snapshots: dict[str, Any],
        source_identity: str,
        *,
        recovery_path: Any,
        receipt_id: str,
        mutated: tuple[str, ...],
    ) -> None:
        from ..write_receipts import ReceiptError, rollback_managed_source_rows

        collections = collections_for(context)
        failures = []
        for name in reversed(mutated):
            try:
                rollback_managed_source_rows(
                    collections[name],
                    snapshots[name],
                    recovery_path=recovery_path,
                    collection_name=name,
                    source_identity=source_identity,
                    receipt_id=receipt_id,
                )
            except Exception as exc:
                failures.append(exc)
        if failures:
            raise ReceiptError("managed adapter collection rollback failed") from failures[0]

    return (
        register_context,
        register_collection_proxy,
        register_graph_proxy,
        collection_proxy_write,
        collection_proxy_delete,
        collection_proxy_read,
        graph_proxy_add_triple,
        managed_collection_names,
        reconcile_pending_rewrites,
        finalize_rewrite_recovery,
        discard_rewrite_recovery,
        verify_context_receipt,
        complete_reused_context_receipt,
        snapshot_managed_source,
        purge_managed_collection_snapshot,
        rollback_managed_source,
    )


(
    _register_context,
    _register_collection_proxy,
    _register_graph_proxy,
    _collection_proxy_write,
    _collection_proxy_delete,
    _collection_proxy_read,
    _graph_proxy_add_triple,
    _managed_collection_names,
    _reconcile_pending_rewrites,
    _finalize_rewrite_recovery,
    _discard_rewrite_recovery,
    _verify_context_receipt,
    _complete_reused_context_receipt,
    _snapshot_managed_source,
    _purge_managed_collection_snapshot,
    _rollback_managed_source,
) = _build_capability_operations()
del _build_capability_operations
