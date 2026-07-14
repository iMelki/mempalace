"""Managed source-adapter ingestion with durable write receipts."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional

from .palace import mine_lock, mine_palace_lock
from .receipt_verifier import ReceiptVerificationError
from .sources.base import BaseSourceAdapter, DrawerRecord, SourceItemMetadata, SourceRef
from .sources._context_capabilities import (
    _complete_reused_context_receipt,
    _discard_rewrite_recovery,
    _finalize_rewrite_recovery,
    _managed_collection_names,
    _purge_managed_collection_snapshot,
    _reconcile_pending_rewrites,
    _rollback_managed_source,
    _snapshot_managed_source,
    _verify_context_receipt,
)
from .sources.context import PalaceContext
from .write_receipts import (
    ManagedRunIdentity,
    ReceiptError,
    ReceiptIdentityError,
    ReceiptStore,
    SourceWriteReceiptSession,
    canonical_source_locator,
    managed_write_scope,
    version_hash,
)


@dataclass(frozen=True)
class ManagedAdapterResult:
    """Summary of one managed adapter invocation."""

    run_id: str
    sources_completed: int
    sources_unchanged: int
    drawers_written: int
    receipt_ids: tuple[str, ...]


def managed_adapter_ingest(
    *,
    adapter: BaseSourceAdapter,
    source: SourceRef,
    palace: PalaceContext,
    receipt_store: ReceiptStore,
    caller: str,
    config: Any,
    run: Optional[ManagedRunIdentity] = None,
) -> ManagedAdapterResult:
    """Serialize source read-through-write for one managed adapter invocation."""
    lock_key = _managed_adapter_lock_key(source)
    with managed_write_scope(palace.palace_path, lock_factory=mine_palace_lock):
        with mine_lock(lock_key):
            _reconcile_pending_rewrites(palace, receipt_store)
            return _managed_adapter_ingest_locked(
                adapter=adapter,
                source=source,
                palace=palace,
                receipt_store=receipt_store,
                caller=caller,
                config=config,
                run=run,
            )


def _managed_adapter_ingest_locked(  # noqa: C901 - transactional generator orchestration
    *,
    adapter: BaseSourceAdapter,
    source: SourceRef,
    palace: PalaceContext,
    receipt_store: ReceiptStore,
    caller: str,
    config: Any,
    run: Optional[ManagedRunIdentity] = None,
) -> ManagedAdapterResult:
    """Drive an RFC 002 adapter through the fail-closed receipt path.

    A managed adapter must yield ``SourceItemMetadata`` with a tagged SHA-256
    ``content_hash`` before drawers for that item. This prevents a source-side
    version token alone from being mistaken for content identity.
    """
    resolved_run = run or receipt_store.create_run(
        caller=caller,
        mode=f"adapter:{adapter.name}",
        config={
            "adapter": adapter.name,
            "adapter_version": adapter.adapter_version,
            "spec_version": adapter.spec_version,
            "transformations": sorted(adapter.declared_transformations),
            "source_options": source.options,
            "config": config,
            "managed_output_collections": list(_managed_collection_names(palace)),
            "knowledge_graph_writes": "rejected-until-receiptable",
        },
    )
    active: Optional[SourceWriteReceiptSession] = None
    active_item: Optional[SourceItemMetadata] = None
    active_source_file: Optional[str] = None
    active_source_aliases: tuple[str, ...] = ()
    active_snapshots: dict[str, Any] = {}
    active_mutated: list[str] = []
    active_incomplete_purge: Optional[str] = None
    active_recovery_path: Optional[Any] = None
    active_previous: Optional[dict] = None
    skip_active = False
    completed = 0
    unchanged = 0
    drawers_written = 0
    receipt_ids: list[str] = []

    def finish_active() -> None:
        nonlocal active, active_item, active_source_file, active_source_aliases
        nonlocal active_snapshots, active_mutated, active_incomplete_purge, active_recovery_path
        nonlocal active_previous
        nonlocal skip_active, completed, unchanged, drawers_written
        if active is None:
            return
        if active.state not in {"COMPLETE", "ABORT", "FAIL"}:
            active.set_expected(
                drawers=active.counts["drawers_written"],
                items=len(active.outputs),
            )
            empty_disposition = None
            if not active.outputs:
                empty_disposition = adapter.empty_output_disposition
            if active_previous is not None:
                invalidation_reason = (
                    "source-zero-output-purge"
                    if empty_disposition == "ZERO_OUTPUT"
                    else "source-rewrite-purge"
                )
                active.record_invalidation(active_previous, reason=invalidation_reason)
            active.complete(disposition=empty_disposition)
            _finalize_rewrite_recovery(
                palace,
                active.store,
                active.source["identity"],
                active.receipt_id,
            )
        if active.state == "COMPLETE":
            completed += 1
            drawers_written += active.counts["drawers_written"]
        if active.disposition == "UNCHANGED":
            unchanged += 1
        receipt_ids.append(active.receipt_id)
        active = None
        active_item = None
        active_source_file = None
        active_source_aliases = ()
        active_snapshots = {}
        active_mutated = []
        active_incomplete_purge = None
        active_recovery_path = None
        active_previous = None
        skip_active = False
        palace._clear_receipt()

    def rollback_active() -> None:
        if (
            active is None
            or active.state in {"COMPLETE", "ABORT", "FAIL"}
            or (not active_mutated and active_incomplete_purge is None)
            or active_source_file is None
            or active_recovery_path is None
        ):
            return
        failures = []
        rollback_targets = list(active_mutated)
        if active_incomplete_purge is not None:
            rollback_targets.append(active_incomplete_purge)
        if rollback_targets:
            try:
                _rollback_managed_source(
                    palace,
                    active_snapshots,
                    active.source["identity"],
                    recovery_path=active_recovery_path,
                    receipt_id=active.receipt_id,
                    mutated=tuple(rollback_targets),
                )
            except Exception as exc:
                failures.append(exc)
        active.discard_pending_invalidations()
        if not failures:
            try:
                _discard_rewrite_recovery(
                    palace,
                    active.store,
                    active.source["identity"],
                    active.receipt_id,
                )
            except Exception as exc:
                failures.append(exc)
        if failures:
            raise ReceiptError("managed adapter rollback failed") from failures[0]

    def rollback_or_raise() -> None:
        try:
            rollback_active()
        except Exception as rollback_exc:
            error = ReceiptError("managed adapter rollback failed")
            if active is not None and active.state not in {"COMPLETE", "ABORT", "FAIL"}:
                active.fail(error, stage="adapter-rollback")
                receipt_ids.append(active.receipt_id)
            raise error from rollback_exc

    local_source = source.local_path is not None
    palace._begin_managed_ingest()
    try:
        for result in adapter.ingest(source=source, palace=palace):
            if isinstance(result, SourceItemMetadata):
                finish_active()
                if not result.content_hash:
                    raise ReceiptIdentityError(
                        "managed adapters must provide SourceItemMetadata.content_hash"
                    )
                active_item = result
                active_source_aliases = (result.source_file,)
                active_source_file = canonical_source_locator(
                    result.source_file,
                    local_path=local_source,
                )
                active = receipt_store.begin_source(
                    run=resolved_run,
                    source_locator=active_source_file,
                    source_content_hash=result.content_hash,
                    source_version_hash=version_hash(result.version),
                    source_size_bytes=result.size_hint or 0,
                    adapter_name=adapter.name,
                    adapter_version=adapter.adapter_version,
                    local_path=local_source,
                )
                palace._activate_receipt(active, active_source_file, local_path=local_source)
                existing_metadata = _existing_metadata(
                    palace.drawer_collection,
                    active_source_file,
                )
                adapter_current, trusted_prior = _trusted_current_receipt(
                    adapter=adapter,
                    item=result,
                    existing_metadata=existing_metadata,
                    active=active,
                    palace=palace,
                    receipt_store=receipt_store,
                    config_digest=resolved_run.config_digest,
                )
                if trusted_prior is not None:
                    _complete_reused_context_receipt(
                        palace,
                        active,
                        trusted_prior,
                        source_file=active_source_file,
                        source_aliases=active_source_aliases,
                    )
                    palace.skip_current_item()
                    skip_active = True
                elif adapter_current:
                    palace.emit(
                        "receipt_rewrite_required",
                        source_identity=active.source["identity"],
                    )
                if not skip_active:
                    previous = active.previous_complete
                    if previous is not None:
                        reason = (
                            "source-version-changed"
                            if previous.get("source", {}).get("version_hash")
                            != active.source["version_hash"]
                            else "adapter-reextract-or-representation-repair"
                        )
                        active.supersede(previous, reason=reason)
                    active_previous = previous
                    active.running("snapshotting-existing")
                    active_snapshots = _snapshot_managed_source(
                        palace,
                        active_source_file,
                        active.source["identity"],
                        source_aliases=active_source_aliases,
                    )
                    active_recovery_path = active.store.prepare_rewrite_recovery(
                        session=active,
                        snapshots=active_snapshots,
                        source_file=active_source_file,
                        local_path=local_source,
                        source_aliases=active_source_aliases,
                        previous_receipt=previous,
                    )
                    active.running("recovery-prepared")
                    active.running("purging-existing")
                    for collection_name in _managed_collection_names(palace):
                        active_incomplete_purge = collection_name
                        _purge_managed_collection_snapshot(
                            palace,
                            collection_name,
                            active_snapshots[collection_name],
                            recovery_path=active_recovery_path,
                            source_file=active_source_file,
                            source_identity=active.source["identity"],
                            source_aliases=active_source_aliases,
                        )
                        active_incomplete_purge = None
                        active_mutated.append(collection_name)
                    active.running("adapter-ingest")
                continue

            if isinstance(result, DrawerRecord):
                if active is None or active_item is None:
                    raise ReceiptIdentityError(
                        "managed adapter yielded a drawer before source identity"
                    )
                result_source_file = canonical_source_locator(
                    result.source_file,
                    local_path=local_source,
                )
                if result_source_file != active_source_file:
                    raise ReceiptIdentityError(
                        "managed adapter drawer does not match the active source item"
                    )
                if not skip_active:
                    palace.upsert_drawer(result)
                continue

            raise ReceiptIdentityError(
                f"managed adapter yielded unsupported type: {type(result).__name__}"
            )
        finish_active()
    except KeyboardInterrupt as exc:
        rollback_or_raise()
        if active is not None and active.state not in {"COMPLETE", "ABORT", "FAIL"}:
            active.abort(exc, stage="adapter-interrupted")
            receipt_ids.append(active.receipt_id)
        raise
    except Exception as exc:
        rollback_or_raise()
        if active is not None and active.state not in {"COMPLETE", "ABORT", "FAIL"}:
            active.fail(exc, stage="adapter-ingest")
            receipt_ids.append(active.receipt_id)
        raise
    finally:
        palace._end_managed_ingest()

    return ManagedAdapterResult(
        run_id=resolved_run.run_id,
        sources_completed=completed,
        sources_unchanged=unchanged,
        drawers_written=drawers_written,
        receipt_ids=tuple(receipt_ids),
    )


def _existing_metadata(collection: Any, source_file: str) -> Optional[dict]:
    try:
        result = collection.get(
            where={"source_file": source_file},
            limit=1,
            include=["metadatas"],
        )
        metadatas = result.get("metadatas") or []
        return dict(metadatas[0]) if metadatas else None
    except Exception:
        return None


def _managed_adapter_lock_key(source: SourceRef) -> str:
    has_local = source.local_path is not None
    has_uri = source.uri is not None
    if has_local == has_uri:
        raise ReceiptIdentityError("managed adapter source must define exactly one locator")
    if has_local:
        return os.path.normcase(canonical_source_locator(source.local_path, local_path=True))
    return f"mempalace-uri:{canonical_source_locator(source.uri, local_path=False)}"


def _trusted_current_receipt(
    *,
    adapter: BaseSourceAdapter,
    item: SourceItemMetadata,
    existing_metadata: Optional[dict],
    active: SourceWriteReceiptSession,
    palace: PalaceContext,
    receipt_store: ReceiptStore,
    config_digest: str,
) -> tuple[bool, Optional[dict]]:
    if not adapter.is_current(item=item, existing_metadata=existing_metadata):
        return False, None
    prior = receipt_store.find_current(
        active.source["identity"],
        content_hash=active.source["content_hash"],
        version_digest=active.source["version_hash"],
        config_digest=config_digest,
    )
    if prior is None:
        return True, None
    try:
        verification = _verify_context_receipt(
            palace,
            prior,
            current_source_content_hash=active.source["content_hash"],
            store=receipt_store,
        )
    except (ReceiptIdentityError, ReceiptVerificationError):
        return True, None
    return True, prior if verification.status == "represented" else None


__all__ = ["ManagedAdapterResult", "managed_adapter_ingest"]
