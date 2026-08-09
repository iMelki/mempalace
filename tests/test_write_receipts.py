"""Focused coverage for the issue #22 managed-write receipt foundation."""

import copy
import importlib.util
import json
import os
import re
import sys
import threading
import time
import uuid
from contextlib import contextmanager

import chromadb
import pytest

import mempalace.convo_miner as convo_miner_module
import mempalace.provenance as provenance_module
import mempalace.sources._context_capabilities as context_capabilities_module
import mempalace.write_receipts as write_receipts_module
from mempalace.backends import EmbeddingVisibilityError
from mempalace.convo_miner import _process_conversation_file
from mempalace.miner import process_file
from mempalace.normalize import normalize
from mempalace.palace import MineAlreadyRunning, mine_palace_lock
from mempalace.provenance import managed_adapter_ingest
from mempalace.receipt_verifier import load_and_validate_receipt, verify_receipt
from mempalace.sources.base import (
    AdapterSchema,
    BaseSourceAdapter,
    DrawerRecord,
    SourceItemMetadata,
    SourceRef,
)
from mempalace.sources.context import PalaceContext
from mempalace.write_receipts import (
    META_OUTPUT_CONTENT_HASH,
    META_RECEIPT_ID,
    META_SOURCE_IDENTITY,
    ReceiptConflictError,
    ReceiptDurabilityError,
    ReceiptError,
    ReceiptIdentityError,
    ReceiptRecoveryError,
    ReceiptStore,
    canonical_source_locator,
    purge_managed_source_snapshot,
    rollback_managed_source_rows,
    require_managed_receipts,
    sha256_bytes,
    shared_receipt_projection,
    snapshot_managed_source_rows,
    stamp_output_metadata,
)

_THREAD_TEST_TIMEOUT_SECONDS = 10.0


def test_managed_receipt_requirement_preserves_receipt_free_dry_runs():
    require_managed_receipts(
        dry_run=True,
        receipt_store=None,
        receipt_run=None,
        operation="test dry run",
    )


def test_managed_receipt_requirement_rejects_partial_invalid_and_foreign_pairs(tmp_path):
    store = ReceiptStore(tmp_path / "palace-a")
    run = store.create_run(caller="test", mode="test", config={})
    foreign_store = ReceiptStore(tmp_path / "palace-b")

    for receipt_store, receipt_run in (
        (store, None),
        (None, run),
        (object(), run),
        (store, object()),
    ):
        with pytest.raises(ReceiptIdentityError, match="require both ReceiptStore"):
            require_managed_receipts(
                dry_run=False,
                receipt_store=receipt_store,
                receipt_run=receipt_run,
                operation="test write",
            )

    with pytest.raises(ReceiptIdentityError, match="different ReceiptStore"):
        require_managed_receipts(
            dry_run=False,
            receipt_store=foreign_store,
            receipt_run=run,
            operation="test write",
        )

    digest = sha256_bytes(b"source")
    with pytest.raises(ReceiptIdentityError, match="different ReceiptStore"):
        foreign_store.begin_source(
            run=run,
            source_locator="logical://source",
            source_content_hash=digest,
            source_version_hash=digest,
            source_size_bytes=6,
            adapter_name="test",
            adapter_version="1",
        )


class _MemoryCollection:
    def __init__(self):
        self.rows = {}
        self.upsert_calls = 0
        self.update_calls = 0
        self.upsert_error = None
        self.delete_calls = 0
        self.delete_error = None

    def add(self, **kwargs):
        duplicates = set(kwargs.get("ids") or ()) & set(self.rows)
        if duplicates:
            raise ValueError(f"duplicate IDs: {sorted(duplicates)}")
        self.upsert(**kwargs)

    def upsert(self, *, documents, ids, metadatas=None, embeddings=None):
        del embeddings
        if self.upsert_error is not None:
            raise self.upsert_error
        self.upsert_calls += 1
        resolved_metas = metadatas or [{} for _ in ids]
        for item_id, document, metadata in zip(ids, documents, resolved_metas):
            self.rows[item_id] = (document, dict(metadata or {}))

    def query(self, **kwargs):
        del kwargs
        return {}

    def update(self, *, ids, documents=None, metadatas=None, embeddings=None):
        del embeddings
        self.update_calls += 1
        for index, item_id in enumerate(ids):
            document, metadata = self.rows[item_id]
            if documents is not None:
                document = documents[index]
            if metadatas is not None:
                metadata = dict(metadatas[index] or {})
            self.rows[item_id] = (document, metadata)

    def get(
        self,
        *,
        ids=None,
        where=None,
        where_document=None,
        limit=None,
        offset=None,
        include=None,
    ):
        del where_document, include
        selected = []
        for item_id, (document, metadata) in self.rows.items():
            if ids is not None and item_id not in ids:
                continue
            if where is not None and not self._matches_where(metadata, where):
                continue
            selected.append((item_id, document, dict(metadata)))
        start = offset or 0
        selected = selected[start:]
        if limit is not None:
            selected = selected[:limit]
        return {
            "ids": [row[0] for row in selected],
            "documents": [row[1] for row in selected],
            "metadatas": [row[2] for row in selected],
        }

    def delete(self, *, ids=None, where=None, where_document=None):
        self.delete_calls += 1
        if self.delete_error is not None:
            raise self.delete_error
        for item_id, (document, metadata) in list(self.rows.items()):
            if ids is not None and item_id not in ids:
                continue
            if where is not None and not self._matches_where(metadata, where):
                continue
            if where_document is not None:
                if "$contains" in where_document:
                    expected = where_document["$contains"]
                    if not isinstance(expected, str) or expected not in document:
                        continue
                elif "$regex" in where_document:
                    pattern = where_document["$regex"]
                    if not isinstance(pattern, str) or re.fullmatch(pattern, document) is None:
                        continue
                else:
                    continue
            self.rows.pop(item_id)

    def count(self):
        return len(self.rows)

    @classmethod
    def _matches_where(cls, metadata, where):
        if "$and" in where:
            return all(cls._matches_where(metadata, item) for item in where["$and"])
        return all(metadata.get(key) == value for key, value in where.items())


class _FakeKnowledgeGraph:
    def add_triple(self, subject, predicate, obj, **kwargs):
        return (subject, predicate, obj, kwargs)


def _store_and_run(tmp_path, *, mode="test", config=None):
    palace_path = tmp_path / "palace"
    store = ReceiptStore(palace_path)
    run = store.create_run(
        caller="test-runner",
        mode=mode,
        config=config or {"fixture": 1},
    )
    return palace_path, store, run


@contextmanager
def _managed_write_scope(store):
    with write_receipts_module.managed_write_scope(
        store.palace_path,
        lock_factory=mine_palace_lock,
    ):
        yield


def _current_for_path(store, source_path):
    identity = store.source_identity(str(source_path), local_path=True)
    current = store.find_current(identity)
    assert current is not None
    return current


def _events_for_receipt(store, receipt_id):
    paths = [path for path in store.events_dir.rglob("*.json") if path.parent.name == receipt_id]
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(paths)]


def _terminal_events(store, state):
    paths = store.events_dir.rglob(f"*-{state.lower()}.json")
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(paths)]


def _long_source_text(label="alpha", lines=90):
    return "\n".join(
        f"{label} implementation detail {index} with enough context" for index in range(lines)
    )


def _seed_receipted_recovery_source(tmp_path):
    palace, store, run = _store_and_run(tmp_path)
    collection = _MemoryCollection()
    source_locator = "logical://recovery/source"
    source_hash = sha256_bytes(b"recovery baseline")
    session = store.begin_source(
        run=run,
        source_locator=source_locator,
        source_content_hash=source_hash,
        source_version_hash=source_hash,
        source_size_bytes=17,
        adapter_name="recovery-fixture",
        adapter_version="1",
    )
    document = "durable baseline row"
    metadata = stamp_output_metadata({"source_file": source_locator}, session, document)
    collection.upsert(documents=[document], ids=["baseline-row"], metadatas=[metadata])
    session.record_output("baseline-row", document)
    session.set_expected(drawers=1)
    baseline = session.complete()
    snapshot = snapshot_managed_source_rows(
        collection,
        source_file=source_locator,
        source_identity=baseline["source"]["identity"],
        local_path=False,
    )
    return palace, store, collection, source_locator, baseline, snapshot


def _begin_recovery_rewrite(store, source_locator, baseline, snapshot):
    run = store.create_run(caller="test-runner", mode="test", config={"fixture": 2})
    source_hash = sha256_bytes(b"replacement source")
    session = store.begin_source(
        run=run,
        source_locator=source_locator,
        source_content_hash=source_hash,
        source_version_hash=source_hash,
        source_size_bytes=18,
        adapter_name="recovery-fixture",
        adapter_version="1",
    )
    session.supersede(baseline, reason="test-rewrite")
    recovery_path = store.prepare_rewrite_recovery(
        session=session,
        snapshots={"drawers": snapshot},
        source_file=source_locator,
        local_path=False,
        previous_receipt=baseline,
    )
    return session, recovery_path


def test_multi_chunk_receipt_is_atomic_content_bound_and_represented(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    source = project / "notes.md"
    source.write_text(_long_source_text(), encoding="utf-8")
    palace, store, run = _store_and_run(tmp_path)
    collection = _MemoryCollection()

    drawers, _ = process_file(
        source,
        project,
        collection,
        "project",
        [{"name": "general", "description": "general project notes"}],
        "test-runner",
        False,
        receipt_store=store,
        receipt_run=run,
    )

    receipt = _current_for_path(store, source)
    assert drawers > 1
    assert receipt["state"] == "COMPLETE"
    assert receipt["counts"]["drawers_written"] == drawers
    assert receipt["outputs"]["count"] == drawers
    assert len({item["content_hash"] for item in receipt["outputs"]["identities"]}) > 1
    lifecycle = _events_for_receipt(store, receipt["receipt_id"])
    assert [event["state"] for event in lifecycle] == [
        "START",
        "RUNNING",
        "RUNNING",
        "RUNNING",
        "RUNNING",
        "COMPLETE",
    ]
    assert verify_receipt(receipt, collection, store=store).status == "represented"
    assert not list(store.root.rglob("*.tmp"))
    assert palace.exists()


def test_unchanged_rerun_reuses_verified_manifest_without_writes(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    source = project / "stable.md"
    source.write_text(_long_source_text("stable"), encoding="utf-8")
    _, store, first_run = _store_and_run(tmp_path, config={"pipeline": "same"})
    collection = _MemoryCollection()
    kwargs = {
        "filepath": source,
        "project_path": project,
        "collection": collection,
        "wing": "project",
        "rooms": [{"name": "general", "description": "general"}],
        "agent": "test-runner",
        "dry_run": False,
        "receipt_store": store,
    }

    process_file(receipt_run=first_run, **kwargs)
    first = _current_for_path(store, source)
    writes_after_first = collection.upsert_calls
    updates_after_first = collection.update_calls
    second_run = store.create_run(caller="test-runner", mode="test", config={"pipeline": "same"})
    drawers, _ = process_file(receipt_run=second_run, **kwargs)
    second = _current_for_path(store, source)

    assert drawers == 0
    assert collection.upsert_calls == writes_after_first
    assert collection.update_calls == updates_after_first + 1
    assert second["receipt_id"] != first["receipt_id"]
    assert second["disposition"] == "UNCHANGED"
    assert second["relations"]["reuses_receipt_id"] == first["receipt_id"]
    assert second["outputs"]["manifest_digest"] != first["outputs"]["manifest_digest"]

    def without_producer(item):
        return {key: value for key, value in item.items() if key != "producer_receipt_id"}

    assert [without_producer(item) for item in second["outputs"]["identities"]] == [
        without_producer(item) for item in first["outputs"]["identities"]
    ]
    assert all(
        item["producer_receipt_id"] == second["receipt_id"]
        for item in second["outputs"]["identities"]
    )
    assert shared_receipt_projection(second)["relations"] == {
        "predecessor_receipt_id": first["receipt_id"],
        "reuses_receipt_id": first["receipt_id"],
    }
    assert verify_receipt(second, collection, store=store).status == "represented"


def test_changed_source_version_supersedes_and_invalidates_prior_receipt(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    source = project / "changing.md"
    source.write_text(_long_source_text("before"), encoding="utf-8")
    _, store, first_run = _store_and_run(tmp_path, config={"pipeline": "same"})
    collection = _MemoryCollection()
    base_kwargs = {
        "filepath": source,
        "project_path": project,
        "collection": collection,
        "wing": "project",
        "rooms": [{"name": "general", "description": "general"}],
        "agent": "test-runner",
        "dry_run": False,
        "receipt_store": store,
    }

    process_file(receipt_run=first_run, **base_kwargs)
    first = _current_for_path(store, source)
    source.write_text(_long_source_text("after"), encoding="utf-8")
    second_run = store.create_run(caller="test-runner", mode="test", config={"pipeline": "same"})
    process_file(receipt_run=second_run, **base_kwargs)
    second = _current_for_path(store, source)

    assert second["source"]["content_hash"] != first["source"]["content_hash"]
    assert second["source"]["version_hash"] != first["source"]["version_hash"]
    assert second["relations"]["supersedes"]["receipt_id"] == first["receipt_id"]
    invalidations = store.invalidations_for(first["receipt_id"])
    assert invalidations
    invalidation_path = (
        store.invalidations_dir
        / first["receipt_id"]
        / f"{invalidations[0]['invalidation_id']}.json"
    )
    assert invalidation_path.exists()
    invalidation_path.unlink()
    assert store.invalidations_for(first["receipt_id"]) == invalidations
    shared_relations = shared_receipt_projection(second)["relations"]
    assert shared_relations["supersedes"]["receipt_id"] == first["receipt_id"]
    assert len(shared_relations["invalidations"]) == 1
    assert verify_receipt(second, collection, store=store).status == "represented"
    prior_result = verify_receipt(first, collection, store=store)
    assert prior_result.status in {"conflict", "stale"}
    assert prior_result.outcomes["stale"] == 1


def test_receipt_verifier_rejects_unsupported_and_contradictory_relations(tmp_path):
    _, store, run = _store_and_run(tmp_path)
    source_hash = sha256_bytes(b"relation validation")
    session = store.begin_source(
        run=run,
        source_locator="logical://relations",
        source_content_hash=source_hash,
        source_version_hash=source_hash,
        source_size_bytes=19,
        adapter_name="fixture",
        adapter_version="1",
    )
    session.set_expected(drawers=0)
    complete = session.complete()

    unsupported = copy.deepcopy(complete)
    unsupported["relations"]["unknown_relation"] = str(uuid.uuid4())
    with pytest.raises(ReceiptIdentityError, match="unsupported fields"):
        load_and_validate_receipt(unsupported, require_complete=True)

    contradictory = copy.deepcopy(complete)
    contradictory["relations"] = {
        "predecessor_receipt_id": str(uuid.uuid4()),
        "reuses_receipt_id": str(uuid.uuid4()),
    }
    contradictory["disposition"] = "UNCHANGED"
    with pytest.raises(ReceiptConflictError, match="contradictory predecessor"):
        load_and_validate_receipt(contradictory, require_complete=True)


def test_receipt_verifier_rejects_invalidation_bound_to_foreign_successor(tmp_path):
    _, store, first_run = _store_and_run(tmp_path)
    first_hash = sha256_bytes(b"relations first")
    first_session = store.begin_source(
        run=first_run,
        source_locator="logical://relations/foreign",
        source_content_hash=first_hash,
        source_version_hash=first_hash,
        source_size_bytes=15,
        adapter_name="fixture",
        adapter_version="1",
    )
    first_session.set_expected(drawers=0)
    first = first_session.complete()

    second_run = store.create_run(caller="test-runner", mode="test", config={"fixture": 2})
    second_hash = sha256_bytes(b"relations second")
    second_session = store.begin_source(
        run=second_run,
        source_locator="logical://relations/foreign",
        source_content_hash=second_hash,
        source_version_hash=second_hash,
        source_size_bytes=16,
        adapter_name="fixture",
        adapter_version="1",
    )
    second_session.supersede(first, reason="source-changed")
    second_session.record_invalidation(first, reason="source-changed")
    second_session.set_expected(drawers=0)
    second = second_session.complete(disposition="ZERO_OUTPUT")

    tampered = copy.deepcopy(second)
    foreign = store.prepare_invalidation(
        invalidated_receipt=first,
        by_receipt_id=str(uuid.uuid4()),
        reason="source-changed",
    )
    tampered["relations"]["invalidations"] = [foreign["invalidation_id"]]
    tampered["relations"]["invalidation_records"] = [foreign]
    with pytest.raises(ReceiptIdentityError, match="does not match its supersession"):
        load_and_validate_receipt(tampered, require_complete=True)


def test_changed_project_source_to_zero_output_purges_prior_and_legacy_rows(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    source = project / "shrinking.md"
    source.write_text(_long_source_text("before"), encoding="utf-8")
    _, store, first_run = _store_and_run(tmp_path, config={"pipeline": "same"})
    collection = _MemoryCollection()
    closets = _MemoryCollection()
    kwargs = {
        "filepath": source,
        "project_path": project,
        "collection": collection,
        "closets_col": closets,
        "wing": "project",
        "rooms": [{"name": "general", "description": "general"}],
        "agent": "test-runner",
        "dry_run": False,
        "receipt_store": store,
    }

    process_file(receipt_run=first_run, **kwargs)
    first = _current_for_path(store, source)
    collection.rows["legacy-drawer"] = (
        "legacy semantic output",
        {"source_file": str(source), "wing": "project", "room": "general"},
    )
    closets.rows["legacy-closet"] = (
        "legacy index output",
        {"source_file": str(source), "wing": "project", "room": "general"},
    )

    source.write_text("now tiny", encoding="utf-8")
    second_run = store.create_run(caller="test-runner", mode="test", config={"pipeline": "same"})
    drawers, _ = process_file(receipt_run=second_run, **kwargs)
    second = _current_for_path(store, source)

    assert drawers == 0
    assert collection.get(where={"source_file": str(source)})["ids"] == []
    assert closets.get(where={"source_file": str(source)})["ids"] == []
    assert second["disposition"] == "ZERO_OUTPUT"
    assert second["outputs"]["count"] == 0
    assert second["relations"]["supersedes"]["receipt_id"] == first["receipt_id"]
    assert second["counts"]["items_invalidated"] == first["outputs"]["count"]
    assert store.invalidations_for(first["receipt_id"])
    assert verify_receipt(second, collection, store=store).status == "represented"
    assert (
        verify_receipt(
            first,
            collections={"drawers": collection, "closets": closets},
            store=store,
        ).status
        == "missing"
    )


def test_managed_project_purge_failure_restores_snapshot_and_fails(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    source = project / "purge-failure.md"
    source.write_text(_long_source_text("before"), encoding="utf-8")
    _, store, first_run = _store_and_run(tmp_path, config={"pipeline": "same"})
    collection = _MemoryCollection()
    kwargs = {
        "filepath": source,
        "project_path": project,
        "collection": collection,
        "wing": "project",
        "rooms": [{"name": "general", "description": "general"}],
        "agent": "test-runner",
        "dry_run": False,
        "receipt_store": store,
    }

    process_file(receipt_run=first_run, **kwargs)
    first = _current_for_path(store, source)
    rows_before = copy.deepcopy(collection.rows)
    writes_before = collection.upsert_calls
    source.write_text(_long_source_text("after"), encoding="utf-8")
    collection.delete_error = RuntimeError("private purge failure")
    second_run = store.create_run(caller="test-runner", mode="test", config={"pipeline": "same"})

    with pytest.raises(RuntimeError, match="private purge failure"):
        process_file(receipt_run=second_run, **kwargs)

    failures = _terminal_events(store, "FAIL")
    assert collection.upsert_calls == writes_before
    assert collection.rows == rows_before
    assert _current_for_path(store, source)["receipt_id"] == first["receipt_id"]
    assert len(failures) == 1
    assert len(failures[0]["errors"]) == 1
    assert failures[0]["errors"][0]["type"] == "builtins.RuntimeError"
    assert failures[0]["errors"][0]["stage"] == "purge-existing-drawers"
    assert failures[0]["errors"][0]["message_digest"].startswith("sha256:")
    assert failures[0]["relations"]["supersedes"]["receipt_id"] == first["receipt_id"]
    assert store.invalidations_for(first["receipt_id"]) == []


def test_partial_delete_exception_restores_every_prior_row(tmp_path):
    class PartialDeleteCollection(_MemoryCollection):
        fail_partial_delete = False

        def delete(self, *, ids=None, where=None, where_document=None):
            if not self.fail_partial_delete:
                return super().delete(
                    ids=ids,
                    where=where,
                    where_document=where_document,
                )
            self.delete_calls += 1
            self.fail_partial_delete = False
            if ids:
                self.rows.pop(ids[0], None)
            raise RuntimeError("delete failed after partial mutation")

    project = tmp_path / "project"
    project.mkdir()
    source = project / "partial-delete.md"
    source.write_text(_long_source_text("before"), encoding="utf-8")
    _, store, first_run = _store_and_run(tmp_path, config={"pipeline": "same"})
    collection = PartialDeleteCollection()
    kwargs = {
        "filepath": source,
        "project_path": project,
        "collection": collection,
        "wing": "project",
        "rooms": [{"name": "general", "description": "general"}],
        "agent": "test-runner",
        "dry_run": False,
        "receipt_store": store,
    }

    process_file(receipt_run=first_run, **kwargs)
    first = _current_for_path(store, source)
    rows_before = copy.deepcopy(collection.rows)
    source.write_text(_long_source_text("after"), encoding="utf-8")
    collection.fail_partial_delete = True
    second_run = store.create_run(caller="test-runner", mode="test", config={"pipeline": "same"})

    with pytest.raises(RuntimeError, match="partial mutation"):
        process_file(receipt_run=second_run, **kwargs)

    assert collection.rows == rows_before
    assert _current_for_path(store, source)["receipt_id"] == first["receipt_id"]
    assert store.invalidations_for(first["receipt_id"]) == []


def test_rollback_failure_retains_recovery_then_exact_retry_restores_embeddings(tmp_path):
    class EmbeddedCollection(_MemoryCollection):
        def __init__(self):
            super().__init__()
            self.embeddings = {}
            self.fail_next_delete = False

        def upsert(self, *, documents, ids, metadatas=None, embeddings=None):
            super().upsert(documents=documents, ids=ids, metadatas=metadatas)
            if embeddings is not None:
                for item_id, embedding in zip(ids, embeddings):
                    self.embeddings[item_id] = list(embedding)

        def get(self, **kwargs):
            result = super().get(**kwargs)
            result["embeddings"] = [self.embeddings.get(item_id) for item_id in result["ids"]]
            return result

        def delete(self, *, ids=None, where=None, where_document=None):
            if self.fail_next_delete:
                self.delete_calls += 1
                self.fail_next_delete = False
                if ids:
                    item_id = ids[0]
                    self.rows.pop(item_id, None)
                    self.embeddings.pop(item_id, None)
                raise RuntimeError("transient partial rollback delete")
            selected = list(ids or self.get(where=where)["ids"])
            super().delete(
                ids=ids,
                where=where,
                where_document=where_document,
            )
            for item_id in selected:
                self.embeddings.pop(item_id, None)

    source = "logical://embedded/source"
    collection = EmbeddedCollection()
    _, store, run = _store_and_run(tmp_path)
    source_hash = sha256_bytes(b"embedded baseline")
    baseline_session = store.begin_source(
        run=run,
        source_locator=source,
        source_content_hash=source_hash,
        source_version_hash=source_hash,
        source_size_bytes=17,
        adapter_name="embedded-fixture",
        adapter_version="1",
    )
    documents = ["old one", "old two"]
    metadatas = [
        stamp_output_metadata({"source_file": source}, baseline_session, document)
        for document in documents
    ]
    collection.upsert(
        documents=documents,
        ids=["old-1", "old-2"],
        metadatas=metadatas,
        embeddings=[[0.1, 0.2], [0.3, 0.4]],
    )
    for item_id, document in zip(["old-1", "old-2"], documents):
        baseline_session.record_output(item_id, document)
    baseline_session.set_expected(drawers=2)
    baseline = baseline_session.complete()
    snapshot = snapshot_managed_source_rows(
        collection,
        source_file=source,
        source_identity=baseline["source"]["identity"],
        local_path=False,
    )
    old_rows = copy.deepcopy(collection.rows)
    old_embeddings = copy.deepcopy(collection.embeddings)
    rewrite, recovery_path = _begin_recovery_rewrite(store, source, baseline, snapshot)
    with _managed_write_scope(store):
        purge_managed_source_snapshot(
            collection,
            snapshot,
            recovery_path=recovery_path,
            collection_name="drawers",
            source_file=source,
            source_identity=baseline["source"]["identity"],
            local_path=False,
        )
        replacement = "replacement"
        collection.upsert(
            documents=[replacement],
            ids=["old-1"],
            metadatas=[stamp_output_metadata({"source_file": source}, rewrite, replacement)],
            embeddings=[[9.0, 9.1]],
        )
        collection.fail_next_delete = True
        with pytest.raises(RuntimeError, match="transient partial rollback delete"):
            rollback_managed_source_rows(
                collection,
                snapshot,
                recovery_path=recovery_path,
                collection_name="drawers",
                source_identity=baseline["source"]["identity"],
                receipt_id=rewrite.receipt_id,
            )
        assert recovery_path.exists()

        rollback_managed_source_rows(
            collection,
            snapshot,
            recovery_path=recovery_path,
            collection_name="drawers",
            source_identity=baseline["source"]["identity"],
            receipt_id=rewrite.receipt_id,
        )
        store.discard_rewrite_recovery(
            baseline["source"]["identity"],
            rewrite.receipt_id,
            collections={"drawers": collection},
        )

    assert collection.rows == old_rows
    assert collection.embeddings == old_embeddings
    assert not recovery_path.exists()


def test_managed_project_rewrite_with_fewer_chunks_leaves_no_stale_ids(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    source = project / "fewer.md"
    source.write_text(_long_source_text("before"), encoding="utf-8")
    _, store, first_run = _store_and_run(tmp_path, config={"pipeline": "same"})
    collection = _MemoryCollection()
    kwargs = {
        "filepath": source,
        "project_path": project,
        "collection": collection,
        "wing": "project",
        "rooms": [{"name": "general", "description": "general"}],
        "agent": "test-runner",
        "dry_run": False,
        "receipt_store": store,
    }

    process_file(receipt_run=first_run, **kwargs)
    first = _current_for_path(store, source)
    first_ids = {item["id"] for item in first["outputs"]["identities"]}
    source.write_text(_long_source_text("after", lines=12), encoding="utf-8")
    second_run = store.create_run(caller="test-runner", mode="test", config={"pipeline": "same"})
    process_file(receipt_run=second_run, **kwargs)
    second = _current_for_path(store, source)
    second_ids = {item["id"] for item in second["outputs"]["identities"]}
    current_ids = set(collection.get(where={"source_file": str(source)})["ids"])

    assert 0 < len(second_ids) < len(first_ids)
    assert first_ids - second_ids
    assert current_ids == second_ids
    assert not (first_ids - second_ids) & current_ids
    assert verify_receipt(second, collection, store=store).status == "represented"


def test_zero_output_conversation_has_exact_sentinel_identity(tmp_path):
    source_dir = tmp_path / "conversations"
    source_dir.mkdir()
    source = source_dir / "empty.txt"
    source.write_text("too short", encoding="utf-8")
    _, store, run = _store_and_run(
        tmp_path,
        mode="conversations:exchange",
        config={"pipeline": "conversations", "extract_mode": "exchange"},
    )
    collection = _MemoryCollection()

    drawers, _, skipped = _process_conversation_file(
        filepath=source,
        collection=collection,
        wing="conversations",
        agent="test-runner",
        extract_mode="exchange",
        dry_run=False,
        index=1,
        total_files=1,
        receipt_store=store,
        receipt_run=run,
    )

    receipt = _current_for_path(store, source)
    assert drawers == 0
    assert skipped is False
    assert receipt["disposition"] == "ZERO_OUTPUT"
    assert receipt["counts"]["drawers_expected"] == 0
    assert receipt["counts"]["sentinels_written"] == 1
    assert receipt["outputs"]["count"] == 1
    assert receipt["outputs"]["identities"][0]["kind"] == "sentinel"
    assert verify_receipt(receipt, collection, store=store).status == "represented"


def test_changed_conversation_to_zero_output_replaces_every_prior_row(tmp_path):
    source_dir = tmp_path / "conversations"
    source_dir.mkdir()
    source = source_dir / "shrinking.txt"
    source.write_text(_long_source_text("before"), encoding="utf-8")
    config = {"pipeline": "conversations", "extract_mode": "exchange"}
    _, store, first_run = _store_and_run(
        tmp_path,
        mode="conversations:exchange",
        config=config,
    )
    collection = _MemoryCollection()
    kwargs = {
        "filepath": source,
        "collection": collection,
        "wing": "conversations",
        "agent": "test-runner",
        "extract_mode": "exchange",
        "dry_run": False,
        "index": 1,
        "total_files": 1,
        "receipt_store": store,
    }

    _process_conversation_file(receipt_run=first_run, **kwargs)
    first = _current_for_path(store, source)
    first_ids = {item["id"] for item in first["outputs"]["identities"]}
    collection.rows["legacy-conversation-row"] = (
        "legacy transcript output",
        {"source_file": str(source), "wing": "conversations", "room": "general"},
    )

    source.write_text("now tiny", encoding="utf-8")
    second_run = store.create_run(
        caller="test-runner",
        mode="conversations:exchange",
        config=config,
    )
    drawers, _, skipped = _process_conversation_file(receipt_run=second_run, **kwargs)
    second = _current_for_path(store, source)
    current_ids = set(collection.get(where={"source_file": str(source)})["ids"])
    sentinel_id = second["outputs"]["identities"][0]["id"]

    assert drawers == 0
    assert skipped is False
    assert current_ids == {sentinel_id}
    assert not first_ids & current_ids
    assert "legacy-conversation-row" not in current_ids
    assert second["disposition"] == "ZERO_OUTPUT"
    assert second["outputs"]["identities"][0]["kind"] == "sentinel"
    assert second["relations"]["supersedes"]["receipt_id"] == first["receipt_id"]
    assert store.invalidations_for(first["receipt_id"])
    assert verify_receipt(second, collection, store=store).status == "represented"


@pytest.mark.parametrize(
    "replacement",
    [_long_source_text("changed"), "tiny"],
    ids=["chunk-rewrite", "zero-output-sentinel"],
)
def test_managed_conversation_purge_failure_restores_snapshot(tmp_path, replacement):
    source_dir = tmp_path / "conversations"
    source_dir.mkdir()
    source = source_dir / "purge-failure.txt"
    source.write_text(_long_source_text("before"), encoding="utf-8")
    config = {"pipeline": "conversations", "extract_mode": "exchange"}
    _, store, first_run = _store_and_run(
        tmp_path,
        mode="conversations:exchange",
        config=config,
    )
    collection = _MemoryCollection()
    kwargs = {
        "filepath": source,
        "collection": collection,
        "wing": "conversations",
        "agent": "test-runner",
        "extract_mode": "exchange",
        "dry_run": False,
        "index": 1,
        "total_files": 1,
        "receipt_store": store,
    }

    _process_conversation_file(receipt_run=first_run, **kwargs)
    first = _current_for_path(store, source)
    rows_before = copy.deepcopy(collection.rows)
    writes_before = collection.upsert_calls
    source.write_text(replacement, encoding="utf-8")
    collection.delete_error = RuntimeError("private conversation purge failure")
    second_run = store.create_run(
        caller="test-runner",
        mode="conversations:exchange",
        config=config,
    )

    with pytest.raises(RuntimeError, match="private conversation purge failure"):
        _process_conversation_file(receipt_run=second_run, **kwargs)

    failures = _terminal_events(store, "FAIL")
    assert collection.upsert_calls == writes_before
    assert collection.rows == rows_before
    assert _current_for_path(store, source)["receipt_id"] == first["receipt_id"]
    assert len(failures) == 1
    assert failures[0]["errors"][0]["stage"] == "purge-existing-drawers"
    assert failures[0]["errors"][0]["message_digest"].startswith("sha256:")
    assert failures[0]["relations"]["supersedes"]["receipt_id"] == first["receipt_id"]
    assert store.invalidations_for(first["receipt_id"]) == []


def test_managed_conversation_rewrite_with_fewer_chunks_leaves_no_stale_ids(tmp_path):
    source_dir = tmp_path / "conversations"
    source_dir.mkdir()
    source = source_dir / "fewer.txt"
    source.write_text(_long_source_text("before"), encoding="utf-8")
    config = {"pipeline": "conversations", "extract_mode": "exchange"}
    _, store, first_run = _store_and_run(
        tmp_path,
        mode="conversations:exchange",
        config=config,
    )
    collection = _MemoryCollection()
    kwargs = {
        "filepath": source,
        "collection": collection,
        "wing": "conversations",
        "agent": "test-runner",
        "extract_mode": "exchange",
        "dry_run": False,
        "index": 1,
        "total_files": 1,
        "receipt_store": store,
    }

    _process_conversation_file(receipt_run=first_run, **kwargs)
    first = _current_for_path(store, source)
    first_ids = {item["id"] for item in first["outputs"]["identities"]}
    source.write_text(_long_source_text("after", lines=30), encoding="utf-8")
    second_run = store.create_run(
        caller="test-runner",
        mode="conversations:exchange",
        config=config,
    )
    _process_conversation_file(receipt_run=second_run, **kwargs)
    second = _current_for_path(store, source)
    second_ids = {item["id"] for item in second["outputs"]["identities"]}
    current_ids = set(collection.get(where={"source_file": str(source)})["ids"])

    assert 0 < len(second_ids) < len(first_ids)
    assert first_ids - second_ids
    assert current_ids == second_ids
    assert not (first_ids - second_ids) & current_ids
    assert verify_receipt(second, collection, store=store).status == "represented"


def test_unmanaged_miners_fail_closed_before_best_effort_purge_or_write(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    project_source = project / "legacy-project.md"
    project_source.write_text(_long_source_text("project"), encoding="utf-8")
    project_collection = _MemoryCollection()
    project_collection.delete_error = RuntimeError("legacy project purge failure")

    with pytest.raises(ReceiptIdentityError, match="require both ReceiptStore"):
        process_file(
            project_source,
            project,
            project_collection,
            "project",
            [{"name": "general", "description": "general"}],
            "test-runner",
            False,
        )

    conversation_source = project / "legacy-conversation.txt"
    conversation_source.write_text(_long_source_text("conversation"), encoding="utf-8")
    conversation_collection = _MemoryCollection()
    conversation_collection.delete_error = RuntimeError("legacy conversation purge failure")
    with pytest.raises(ReceiptIdentityError, match="require both ReceiptStore"):
        _process_conversation_file(
            filepath=conversation_source,
            collection=conversation_collection,
            wing="conversations",
            agent="test-runner",
            extract_mode="exchange",
            dry_run=False,
            index=1,
            total_files=1,
        )

    assert project_collection.delete_calls == 0
    assert project_collection.upsert_calls == 0
    assert conversation_collection.delete_calls == 0
    assert conversation_collection.upsert_calls == 0


def test_managed_normalization_consumes_the_bytes_bound_to_the_receipt(tmp_path):
    source = tmp_path / "changing.txt"
    source.write_text("different bytes on disk", encoding="utf-8")
    retained = b"exact retained source bytes\r\nsecond line"

    assert normalize(str(source), source_bytes=retained) == (
        "exact retained source bytes\nsecond line"
    )


@pytest.mark.parametrize("error", [OSError("decode failed"), ValueError("invalid export")])
def test_normalization_failure_preserves_prior_rows_and_emits_fail(tmp_path, monkeypatch, error):
    source = tmp_path / "normalization-failure.txt"
    source.write_text(_long_source_text("before"), encoding="utf-8")
    config = {"pipeline": "conversations", "extract_mode": "exchange"}
    _, store, first_run = _store_and_run(
        tmp_path,
        mode="conversations:exchange",
        config=config,
    )
    collection = _MemoryCollection()
    kwargs = {
        "filepath": source,
        "collection": collection,
        "wing": "conversations",
        "agent": "test-runner",
        "extract_mode": "exchange",
        "dry_run": False,
        "index": 1,
        "total_files": 1,
        "receipt_store": store,
    }
    _process_conversation_file(receipt_run=first_run, **kwargs)
    first = _current_for_path(store, source)
    rows_before = copy.deepcopy(collection.rows)
    deletes_before = collection.delete_calls
    source.write_text(_long_source_text("after"), encoding="utf-8")

    def fail_normalize(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(convo_miner_module, "normalize", fail_normalize)
    second_run = store.create_run(
        caller="test-runner",
        mode="conversations:exchange",
        config=config,
    )
    result = _process_conversation_file(receipt_run=second_run, **kwargs)

    failures = _terminal_events(store, "FAIL")
    assert result == (0, {}, False)
    assert collection.rows == rows_before
    assert collection.delete_calls == deletes_before
    assert _current_for_path(store, source)["receipt_id"] == first["receipt_id"]
    assert len(failures) == 1
    assert failures[0]["errors"][0]["stage"] == "normalize"
    assert failures[0]["disposition"] == "WRITE"
    assert store.invalidations_for(first["receipt_id"]) == []


def test_conversation_lock_precedes_read_so_newest_source_bytes_win(tmp_path, monkeypatch):
    source = tmp_path / "concurrent.txt"
    source.write_text(_long_source_text("older"), encoding="utf-8")
    config = {"pipeline": "conversations", "extract_mode": "exchange"}
    _, store, older_run = _store_and_run(
        tmp_path,
        mode="conversations:exchange",
        config=config,
    )
    collection = _MemoryCollection()
    older_waiting = threading.Event()
    newer_done = threading.Event()
    errors = []

    @contextmanager
    def ordered_lock(_source_file):
        if threading.current_thread().name == "older-snapshot":
            older_waiting.set()
            if not newer_done.wait(timeout=5):
                raise TimeoutError("newer invocation did not finish")
        yield

    @contextmanager
    def no_palace_lock(_palace_path):
        yield

    monkeypatch.setattr(convo_miner_module, "mine_lock", ordered_lock)
    monkeypatch.setattr(convo_miner_module, "mine_palace_lock", no_palace_lock)
    kwargs = {
        "filepath": source,
        "collection": collection,
        "wing": "conversations",
        "agent": "test-runner",
        "extract_mode": "exchange",
        "dry_run": False,
        "index": 1,
        "total_files": 1,
        "receipt_store": store,
    }

    def run_older():
        try:
            _process_conversation_file(receipt_run=older_run, **kwargs)
        except BaseException as exc:  # captured for assertion in the parent thread
            errors.append(exc)

    older_thread = threading.Thread(target=run_older, name="older-snapshot")
    older_thread.start()
    assert older_waiting.wait(timeout=3)
    source.write_text(_long_source_text("newer"), encoding="utf-8")
    newer_run = store.create_run(
        caller="test-runner",
        mode="conversations:exchange",
        config=config,
    )
    try:
        _process_conversation_file(receipt_run=newer_run, **kwargs)
    finally:
        newer_done.set()
    older_thread.join(timeout=5)

    assert not older_thread.is_alive()
    assert errors == []
    current = _current_for_path(store, source)
    current_documents = [document for document, _ in collection.rows.values()]
    assert current["source"]["content_hash"] == sha256_bytes(source.read_bytes())
    assert current_documents
    assert all("newer" in document for document in current_documents)


def test_conversation_mine_uses_palace_wide_cross_process_lock(tmp_path, monkeypatch):
    held = False

    @contextmanager
    def palace_lock(path):
        nonlocal held
        assert path == str(tmp_path / "palace")
        held = True
        try:
            yield
        finally:
            held = False

    monkeypatch.setattr(convo_miner_module, "mine_palace_lock", palace_lock)
    monkeypatch.setattr(convo_miner_module, "_mine_convos_impl", lambda **_kwargs: held)

    assert (
        convo_miner_module.mine_convos(
            str(tmp_path),
            str(tmp_path / "palace"),
        )
        is True
    )


@pytest.mark.parametrize(
    ("error", "terminal"),
    [
        (KeyboardInterrupt(), "ABORT"),
        (RuntimeError("private source C:/secret/write failed"), "FAIL"),
    ],
)
def test_interrupted_and_failed_writes_are_terminal_and_privacy_safe(tmp_path, error, terminal):
    project = tmp_path / "project"
    project.mkdir()
    source = project / "failure.md"
    source.write_text(_long_source_text("failure"), encoding="utf-8")
    _, store, run = _store_and_run(tmp_path)
    collection = _MemoryCollection()
    collection.upsert_error = error

    with pytest.raises(type(error)):
        process_file(
            source,
            project,
            collection,
            "project",
            [{"name": "general", "description": "general"}],
            "test-runner",
            False,
            receipt_store=store,
            receipt_run=run,
        )

    terminal_paths = list(store.events_dir.rglob(f"*-{terminal.lower()}.json"))
    assert len(terminal_paths) == 1
    event_text = terminal_paths[0].read_text(encoding="utf-8")
    event = json.loads(event_text)
    assert event["state"] == terminal
    assert event["counts"]["errors"] == 1
    assert event["errors"][0]["message_digest"].startswith("sha256:")
    assert "C:/secret" not in event_text
    shared_event = shared_receipt_projection(event)
    assert shared_event["errors"] == [
        {
            "type": event["errors"][0]["type"],
            "stage": event["errors"][0]["stage"],
            "message_identity": event["errors"][0]["shared_message_identity"],
        }
    ]
    assert "message_digest" not in shared_event["errors"][0]
    assert event["errors"][0]["message_digest"] not in json.dumps(shared_event)
    assert store.find_current(event["source"]["identity"]) is None


def test_verifier_reports_all_outcome_families_and_fails_closed(tmp_path):
    _, store, run = _store_and_run(tmp_path)
    collection = _MemoryCollection()
    source_hash = sha256_bytes(b"source-v1")
    session = store.begin_source(
        run=run,
        source_locator="logical://source/one",
        source_content_hash=source_hash,
        source_version_hash=source_hash,
        source_size_bytes=9,
        adapter_name="fixture",
        adapter_version="1",
    )
    session.set_expected(drawers=1)
    session.running("writing")
    document = "represented output"
    metadata = stamp_output_metadata({}, session, document)
    collection.upsert(documents=[document], ids=["drawer-one"], metadatas=[metadata])
    session.record_output("drawer-one", document)
    receipt = session.complete()

    represented = verify_receipt(receipt, collection, store=store)
    assert represented.status == "represented"
    assert represented.outcomes == {
        "represented": 1,
        "missing": 0,
        "excess": 0,
        "conflict": 0,
        "stale": 0,
    }
    assert verify_receipt(session.last_event_path, collection).status == "represented"
    assert represented.as_dict(include_identities=False) == {
        "receipt_id": receipt["receipt_id"],
        "status": "represented",
        "outcomes": represented.outcomes,
    }
    assert represented.as_dict()["represented"] == ["drawers:drawer-one"]

    original_row = copy.deepcopy(collection.rows["drawer-one"])
    collection.delete(ids=["drawer-one"])
    assert verify_receipt(receipt, collection).status == "missing"

    collection.rows["drawer-one"] = original_row
    collection.rows["drawer-excess"] = (
        "extra",
        {
            **original_row[1],
            "write_output_content_hash": sha256_bytes(b"extra"),
        },
    )
    excess = verify_receipt(receipt, collection)
    assert excess.status == "excess"
    assert excess.outcomes["excess"] == 1

    collection.rows.pop("drawer-excess")
    collection.rows["drawer-one"] = ("conflicting output", original_row[1])
    assert verify_receipt(receipt, collection).status == "conflict"

    collection.rows["drawer-one"] = original_row
    stale = verify_receipt(
        receipt,
        collection,
        current_source_content_hash=sha256_bytes(b"source-v2"),
    )
    assert stale.status == "stale"

    malformed = copy.deepcopy(receipt)
    malformed["source"].pop("content_hash")
    with pytest.raises(ReceiptIdentityError):
        verify_receipt(malformed, collection)
    with pytest.raises(ReceiptIdentityError):
        verify_receipt(shared_receipt_projection(receipt), collection)

    foreign_manifest = copy.deepcopy(receipt)
    foreign_manifest["outputs"]["identities"][0]["producer_receipt_id"] = str(uuid.uuid4())
    foreign_manifest["outputs"]["manifest_digest"] = write_receipts_module.manifest_digest(
        foreign_manifest["outputs"]["identities"]
    )
    with pytest.raises(ReceiptIdentityError, match="foreign producer receipt"):
        verify_receipt(foreign_manifest, collection)

    collection.rows["drawer-one"][1][META_RECEIPT_ID] = str(uuid.uuid4())
    assert verify_receipt(receipt, collection).status == "conflict"


def test_verifier_batches_large_exact_manifests(tmp_path):
    _, store, run = _store_and_run(tmp_path)
    collection = _MemoryCollection()
    source_hash = sha256_bytes(b"large source")
    session = store.begin_source(
        run=run,
        source_locator="logical://source/large",
        source_content_hash=source_hash,
        source_version_hash=source_hash,
        source_size_bytes=12,
        adapter_name="fixture",
        adapter_version="1",
    )
    item_count = 1005
    session.set_expected(drawers=item_count)
    session.running("writing")
    ids = [f"drawer-{index:04d}" for index in range(item_count)]
    documents = [f"content-{index}" for index in range(item_count)]
    metadatas = [stamp_output_metadata({}, session, document) for document in documents]
    collection.upsert(documents=documents, ids=ids, metadatas=metadatas)
    for item_id, document in zip(ids, documents):
        session.record_output(item_id, document)
    receipt = session.complete()

    result = verify_receipt(receipt, collection)
    assert result.status == "represented"
    assert result.outcomes["represented"] == item_count


@pytest.mark.parametrize("phase", ["prepared", "partial-replacement"])
def test_durable_rewrite_recovery_restores_interrupted_precomplete_phases(tmp_path, phase):
    palace, store, collection, source_locator, baseline, snapshot = _seed_receipted_recovery_source(
        tmp_path
    )
    rewrite, recovery_path = _begin_recovery_rewrite(store, source_locator, baseline, snapshot)
    rows_before = copy.deepcopy(collection.rows)

    if phase == "partial-replacement":
        collection.delete(ids=list(snapshot.ids))
        document = "partially written replacement"
        metadata = stamp_output_metadata({"source_file": source_locator}, rewrite, document)
        collection.upsert(documents=[document], ids=["partial-row"], metadatas=[metadata])

    reopened = ReceiptStore(palace)
    blocked_run = reopened.create_run(caller="test-runner", mode="test", config={})
    blocked_hash = sha256_bytes(b"blocked replacement")
    with pytest.raises(ReceiptRecoveryError, match="must be reconciled"):
        reopened.begin_source(
            run=blocked_run,
            source_locator=source_locator,
            source_content_hash=blocked_hash,
            source_version_hash=blocked_hash,
            source_size_bytes=19,
            adapter_name="recovery-fixture",
            adapter_version="1",
        )

    with _managed_write_scope(reopened):
        outcomes = reopened.reconcile_pending_rewrites(
            {"drawers": collection},
            source_identity=baseline["source"]["identity"],
        )

    assert outcomes == (
        {
            "receipt_id": rewrite.receipt_id,
            "source_identity": baseline["source"]["identity"],
            "action": "restore",
        },
    )
    assert collection.rows == rows_before
    assert not recovery_path.exists()
    assert (
        reopened.find_current(baseline["source"]["identity"])["receipt_id"]
        == baseline["receipt_id"]
    )


def test_durable_rewrite_recovery_commits_complete_before_cleanup(tmp_path):
    palace, store, collection, source_locator, baseline, snapshot = _seed_receipted_recovery_source(
        tmp_path
    )
    rewrite, recovery_path = _begin_recovery_rewrite(store, source_locator, baseline, snapshot)
    collection.delete(ids=list(snapshot.ids))
    rewrite.set_expected(drawers=0)
    complete = rewrite.complete()

    reopened = ReceiptStore(palace)
    with _managed_write_scope(reopened):
        outcomes = reopened.reconcile_pending_rewrites({"drawers": collection})

    assert outcomes == (
        {
            "receipt_id": complete["receipt_id"],
            "source_identity": complete["source"]["identity"],
            "action": "commit",
        },
    )
    assert collection.rows == {}
    assert not recovery_path.exists()
    assert (
        reopened.find_current(complete["source"]["identity"])["receipt_id"]
        == complete["receipt_id"]
    )


def test_complete_recovery_cleanup_requires_unique_authoritative_dag_head(tmp_path):
    palace, store, collection, source_locator, baseline, snapshot = _seed_receipted_recovery_source(
        tmp_path
    )
    rewrite, recovery_path = _begin_recovery_rewrite(store, source_locator, baseline, snapshot)
    rewrite.set_expected(drawers=0)
    rewrite.complete()
    rogue_event = copy.deepcopy(rewrite.last_event)
    rogue_event["receipt_id"] = str(uuid.uuid4())
    rogue_event["event_id"] = write_receipts_module.receipt_event_id(
        rogue_event["receipt_id"], rogue_event["sequence"]
    )
    rogue_event["relations"] = {}
    rogue_path = store.write_event(rogue_event)
    store.set_current(rogue_event, rogue_path)

    with _managed_write_scope(store):
        with pytest.raises(ReceiptConflictError, match="disconnected or ambiguous"):
            store.finalize_rewrite_recovery(
                baseline["source"]["identity"],
                rewrite.receipt_id,
                collections={"drawers": collection},
            )
    assert recovery_path.exists()

    reopened = ReceiptStore(palace)
    with _managed_write_scope(reopened):
        with pytest.raises(ReceiptConflictError, match="disconnected or ambiguous"):
            reopened.reconcile_pending_rewrites({"drawers": collection})
    assert recovery_path.exists()


def test_complete_sync_uncertainty_retains_recovery(tmp_path, monkeypatch):
    _, store, collection, source_locator, baseline, snapshot = _seed_receipted_recovery_source(
        tmp_path
    )
    rewrite, recovery_path = _begin_recovery_rewrite(store, source_locator, baseline, snapshot)
    with _managed_write_scope(store):
        purge_managed_source_snapshot(
            collection,
            snapshot,
            recovery_path=recovery_path,
            collection_name="drawers",
            source_file=source_locator,
            source_identity=baseline["source"]["identity"],
            local_path=False,
        )
        rewrite.set_expected(drawers=0)

        def fail_complete_publication(*_args, **_kwargs):
            raise ReceiptDurabilityError("simulated COMPLETE directory-sync failure")

        monkeypatch.setattr(
            write_receipts_module,
            "_publish_durable_file",
            fail_complete_publication,
        )
        with pytest.raises(ReceiptDurabilityError, match="COMPLETE directory-sync"):
            rewrite.complete()

    assert rewrite.state != "COMPLETE"
    assert recovery_path.exists()
    assert collection.rows == {}


def test_finalizer_reproves_complete_durability_before_recovery_removal(tmp_path, monkeypatch):
    _, store, collection, source_locator, baseline, snapshot = _seed_receipted_recovery_source(
        tmp_path
    )
    rewrite, recovery_path = _begin_recovery_rewrite(store, source_locator, baseline, snapshot)
    with _managed_write_scope(store):
        purge_managed_source_snapshot(
            collection,
            snapshot,
            recovery_path=recovery_path,
            collection_name="drawers",
            source_file=source_locator,
            source_identity=baseline["source"]["identity"],
            local_path=False,
        )
        rewrite.set_expected(drawers=0)
        rewrite.complete()
        monkeypatch.setattr(
            write_receipts_module,
            "_flush_and_verify_published_file",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("flush uncertain")),
        )
        with pytest.raises(ReceiptDurabilityError, match="re-proven"):
            store.finalize_rewrite_recovery(
                rewrite.source["identity"],
                rewrite.receipt_id,
                collections={"drawers": collection},
            )

    assert recovery_path.exists()


def test_empty_baseline_selector_catches_late_receiptless_row_before_commit_cleanup(tmp_path):
    _, store, run = _store_and_run(tmp_path)
    collection = _MemoryCollection()
    source = "logical://empty-baseline"
    source_hash = sha256_bytes(b"empty baseline")
    session = store.begin_source(
        run=run,
        source_locator=source,
        source_content_hash=source_hash,
        source_version_hash=source_hash,
        source_size_bytes=14,
        adapter_name="empty-fixture",
        adapter_version="1",
    )
    snapshot = snapshot_managed_source_rows(
        collection,
        source_file=source,
        source_identity=session.source["identity"],
        local_path=False,
    )
    recovery_path = store.prepare_rewrite_recovery(
        session=session,
        snapshots={"drawers": snapshot},
        source_file=source,
        local_path=False,
    )
    session.set_expected(drawers=0)
    session.complete(disposition="ZERO_OUTPUT")
    collection.rows["late-legacy"] = ("late", {"source_file": source})

    with _managed_write_scope(store):
        with pytest.raises(ReceiptRecoveryError, match="exactly match COMPLETE"):
            store.finalize_rewrite_recovery(
                session.source["identity"],
                session.receipt_id,
                collections={"drawers": collection},
            )

    assert recovery_path.exists()
    assert "late-legacy" in collection.rows


@pytest.mark.skipif(os.name != "nt", reason="Windows write-through marker proof")
def test_first_recovery_publish_durability_marks_new_directory_chain(tmp_path):
    _, store, _, source_locator, baseline, snapshot = _seed_receipted_recovery_source(tmp_path)
    _, recovery_path = _begin_recovery_rewrite(store, source_locator, baseline, snapshot)

    marker_name = write_receipts_module._DIRECTORY_DURABILITY_MARKER
    assert (store.root / marker_name).read_bytes() == (
        write_receipts_module._DIRECTORY_DURABILITY_BYTES
    )
    assert (store.recoveries_dir / marker_name).exists()
    assert (recovery_path.parent / marker_name).exists()


def test_posix_recovery_directory_chain_syncs_every_directory_and_parent(tmp_path, monkeypatch):
    anchor = tmp_path / "palace" / ".mempalace"
    target = anchor / "write-receipts" / "v1" / "recoveries" / "source"
    anchor.parent.mkdir(parents=True)
    sync_calls = []
    monkeypatch.setattr(
        write_receipts_module,
        "_fsync_directory",
        lambda path: sync_calls.append(path.resolve()),
    )

    write_receipts_module._ensure_private_dir_durable_chain(
        target,
        anchor=anchor,
        _platform_name="posix",
    )

    directories = [anchor]
    current = anchor
    for part in target.relative_to(anchor).parts:
        current = current / part
        directories.append(current)
    for directory in directories:
        assert directory.resolve() in sync_calls
        assert directory.parent.resolve() in sync_calls


def test_corrupt_rewrite_recovery_fails_closed_without_collection_mutation(tmp_path):
    palace, store, collection, source_locator, baseline, snapshot = _seed_receipted_recovery_source(
        tmp_path
    )
    rewrite, recovery_path = _begin_recovery_rewrite(store, source_locator, baseline, snapshot)
    collection.delete(ids=list(snapshot.ids))
    document = "partial replacement remains quarantined"
    metadata = stamp_output_metadata({"source_file": source_locator}, rewrite, document)
    collection.upsert(documents=[document], ids=["partial-row"], metadatas=[metadata])
    rows_before = copy.deepcopy(collection.rows)
    corrupted = json.loads(recovery_path.read_text(encoding="utf-8"))
    corrupted["collections"]["drawers"]["documents"][0] = "tampered snapshot"
    recovery_path.write_text(json.dumps(corrupted), encoding="utf-8")

    reopened = ReceiptStore(palace)
    with _managed_write_scope(reopened):
        with pytest.raises(ReceiptRecoveryError, match="digest does not match"):
            reopened.reconcile_pending_rewrites({"drawers": collection})

    assert collection.rows == rows_before
    assert recovery_path.exists()


def test_rewrite_recovery_refuses_unexpected_same_source_rows_without_mutation(tmp_path):
    palace, store, collection, source_locator, baseline, snapshot = _seed_receipted_recovery_source(
        tmp_path
    )
    _, recovery_path = _begin_recovery_rewrite(store, source_locator, baseline, snapshot)
    baseline_document, baseline_metadata = collection.rows["baseline-row"]
    collection.upsert(
        documents=["unexpected concurrent representation"],
        ids=["unexpected-row"],
        metadatas=[dict(baseline_metadata)],
    )
    rows_before = copy.deepcopy(collection.rows)

    reopened = ReceiptStore(palace)
    with _managed_write_scope(reopened):
        with pytest.raises(ReceiptRecoveryError, match="unexpected managed row"):
            reopened.reconcile_pending_rewrites({"drawers": collection})

    assert collection.rows == rows_before
    assert collection.rows["baseline-row"][0] == baseline_document
    assert recovery_path.exists()


def test_reconciliation_never_deletes_rows_added_after_its_validated_plan(tmp_path, monkeypatch):
    palace, store, collection, source_locator, baseline, snapshot = _seed_receipted_recovery_source(
        tmp_path
    )
    rewrite, recovery_path = _begin_recovery_rewrite(store, source_locator, baseline, snapshot)
    with _managed_write_scope(store):
        purge_managed_source_snapshot(
            collection,
            snapshot,
            recovery_path=recovery_path,
            collection_name="drawers",
            source_file=source_locator,
            source_identity=baseline["source"]["identity"],
            local_path=False,
        )
        partial_document = "interrupted replacement"
        partial_metadata = stamp_output_metadata(
            {"source_file": source_locator},
            rewrite,
            partial_document,
        )
        collection.upsert(
            documents=[partial_document],
            ids=["partial-row"],
            metadatas=[partial_metadata],
        )
        original_delete = collection.delete
        raced = False

        def delete_then_race(**kwargs):
            nonlocal raced
            if not raced:
                raced = True
                late_document = "late interrupted row"
                collection.rows["late-row"] = (
                    late_document,
                    stamp_output_metadata(
                        {"source_file": source_locator},
                        rewrite,
                        late_document,
                    ),
                )
            return original_delete(**kwargs)

        monkeypatch.setattr(collection, "delete", delete_then_race)
        reopened = ReceiptStore(palace)

        with pytest.raises(ReceiptRecoveryError, match="retains replacement|interrupted-attempt"):
            reopened.reconcile_pending_rewrites({"drawers": collection})

    assert collection.rows["late-row"][0] == "late interrupted row"
    assert collection.rows["baseline-row"][0] == snapshot.documents[0]
    assert recovery_path.exists()


def test_in_process_rollback_keeps_recovery_until_exact_readback_succeeds(tmp_path, monkeypatch):
    _, store, collection, source_locator, baseline, snapshot = _seed_receipted_recovery_source(
        tmp_path
    )
    rewrite, recovery_path = _begin_recovery_rewrite(store, source_locator, baseline, snapshot)
    with _managed_write_scope(store):
        purge_managed_source_snapshot(
            collection,
            snapshot,
            recovery_path=recovery_path,
            collection_name="drawers",
            source_file=source_locator,
            source_identity=baseline["source"]["identity"],
            local_path=False,
        )
        original_add = collection.add
        monkeypatch.setattr(collection, "add", lambda **_kwargs: None)

        with pytest.raises(ReceiptIdentityError, match="changed while its snapshot was read"):
            rollback_managed_source_rows(
                collection,
                snapshot,
                recovery_path=recovery_path,
                collection_name="drawers",
                source_identity=baseline["source"]["identity"],
                receipt_id=rewrite.receipt_id,
            )

        assert recovery_path.exists()
        monkeypatch.setattr(collection, "add", original_add)
        rollback_managed_source_rows(
            collection,
            snapshot,
            recovery_path=recovery_path,
            collection_name="drawers",
            source_identity=baseline["source"]["identity"],
            receipt_id=rewrite.receipt_id,
        )
        store.discard_rewrite_recovery(
            baseline["source"]["identity"],
            rewrite.receipt_id,
            collections={"drawers": collection},
        )
    assert not recovery_path.exists()


def test_direct_writer_addition_after_durable_publication_blocks_project_purge(
    tmp_path, monkeypatch
):
    project = tmp_path / "project"
    project.mkdir()
    source = project / "direct-race.md"
    source.write_text(_long_source_text("before"), encoding="utf-8")
    _, store, first_run = _store_and_run(tmp_path, config={"pipeline": "race"})
    collection = _MemoryCollection()
    kwargs = {
        "filepath": source,
        "project_path": project,
        "collection": collection,
        "wing": "project",
        "rooms": [{"name": "general", "description": "general"}],
        "agent": "test-runner",
        "dry_run": False,
        "receipt_store": store,
    }
    process_file(receipt_run=first_run, **kwargs)
    first = _current_for_path(store, source)
    rows_before = copy.deepcopy(collection.rows)
    deletes_before = collection.delete_calls
    baseline_metadata = copy.deepcopy(next(iter(collection.rows.values()))[1])
    source.write_text(_long_source_text("after"), encoding="utf-8")
    second_run = store.create_run(
        caller="test-runner",
        mode="test",
        config={"pipeline": "race"},
    )
    original_prepare = store.prepare_rewrite_recovery

    def prepare_then_race(**prepare_kwargs):
        path = original_prepare(**prepare_kwargs)
        collection.upsert(
            documents=["direct writer arrived after the durable snapshot"],
            ids=["direct-writer-row"],
            metadatas=[baseline_metadata],
        )
        return path

    monkeypatch.setattr(store, "prepare_rewrite_recovery", prepare_then_race)

    with pytest.raises(ReceiptError, match="rollback failed"):
        process_file(receipt_run=second_run, **kwargs)

    assert collection.delete_calls == deletes_before
    assert all(collection.rows[item_id] == row for item_id, row in rows_before.items())
    assert collection.rows["direct-writer-row"][0].startswith("direct writer")
    assert _current_for_path(store, source)["receipt_id"] == first["receipt_id"]
    assert len(store._pending_recovery_paths(first["source"]["identity"])) == 1


def test_direct_replacement_between_validation_and_delete_survives_and_blocks_purge(tmp_path):
    class BetweenValidationAndDeleteCollection(_MemoryCollection):
        race_enabled = False

        def delete(self, *, ids=None, where=None, where_document=None):
            if self.race_enabled and ids:
                self.race_enabled = False
                baseline_document = self.rows[ids[0]][0]
                baseline_metadata = self.rows[ids[0]][1]
                replacement_document = f"prefix {baseline_document} suffix"
                self.rows[ids[0]] = (
                    replacement_document,
                    {
                        **baseline_metadata,
                        META_RECEIPT_ID: str(uuid.uuid4()),
                        META_OUTPUT_CONTENT_HASH: sha256_bytes(
                            replacement_document.encode("utf-8")
                        ),
                    },
                )
            return super().delete(
                ids=ids,
                where=where,
                where_document=where_document,
            )

    project = tmp_path / "project"
    project.mkdir()
    source = project / "between-validation-delete.md"
    source.write_text(_long_source_text("before"), encoding="utf-8")
    _, store, first_run = _store_and_run(tmp_path, config={"pipeline": "race"})
    collection = BetweenValidationAndDeleteCollection()
    kwargs = {
        "filepath": source,
        "project_path": project,
        "collection": collection,
        "wing": "project",
        "rooms": [{"name": "general", "description": "general"}],
        "agent": "test-runner",
        "dry_run": False,
        "receipt_store": store,
    }
    process_file(receipt_run=first_run, **kwargs)
    first = _current_for_path(store, source)
    source.write_text(_long_source_text("after"), encoding="utf-8")
    collection.race_enabled = True
    second_run = store.create_run(
        caller="test-runner",
        mode="test",
        config={"pipeline": "race"},
    )

    with pytest.raises(ReceiptError, match="rollback failed"):
        process_file(receipt_run=second_run, **kwargs)

    assert any(
        document.startswith("prefix ") and document.endswith(" suffix")
        for document, _ in collection.rows.values()
    )
    assert len(store._pending_recovery_paths(first["source"]["identity"])) == 1
    assert _current_for_path(store, source)["receipt_id"] == first["receipt_id"]


def test_matching_stamped_hash_uses_metadata_binding_without_document_regex(tmp_path):
    _, _, collection, _, _, _ = _seed_receipted_recovery_source(tmp_path)
    document, metadata = collection.rows["baseline-row"]
    row = write_receipts_module._validated_collection_row(
        "baseline-row",
        (document, metadata, None),
    )

    where, where_document = write_receipts_module._delete_filters_for_validated_row(row)

    assert where_document is None
    assert {META_OUTPUT_CONTENT_HASH: sha256_bytes(document.encode("utf-8"))} in where["$and"]


@pytest.mark.parametrize("hash_state", ["missing", "stale"])
def test_legacy_or_stale_hash_retains_exact_document_regex(tmp_path, hash_state):
    _, _, collection, _, _, _ = _seed_receipted_recovery_source(tmp_path)
    document, metadata = collection.rows["baseline-row"]
    metadata = dict(metadata)
    if hash_state == "missing":
        metadata.pop(META_OUTPUT_CONTENT_HASH)
    else:
        metadata[META_OUTPUT_CONTENT_HASH] = sha256_bytes(b"different document")
    row = write_receipts_module._validated_collection_row(
        "baseline-row",
        (document, metadata, None),
    )

    _, where_document = write_receipts_module._delete_filters_for_validated_row(row)

    assert where_document == {"$regex": f"(?s)^{re.escape(document)}$"}


def test_stale_hash_on_empty_row_fails_closed(tmp_path):
    _, _, collection, _, _, _ = _seed_receipted_recovery_source(tmp_path)
    _, metadata = collection.rows["baseline-row"]
    row = write_receipts_module._validated_collection_row(
        "baseline-row",
        ("", metadata, None),
    )

    with pytest.raises(ReceiptRecoveryError, match="stale content hash on empty row"):
        write_receipts_module._delete_filters_for_validated_row(row)


def test_real_chroma_large_stamped_row_purges_without_compiling_document_regex(tmp_path):
    palace_path = tmp_path / "real-chroma-large-delete"
    store = ReceiptStore(palace_path)
    run = store.create_run(caller="test-runner", mode="test", config={"fixture": "large"})
    source_locator = "logical://recovery/large-source"
    source_hash = sha256_bytes(b"large recovery baseline")
    session = store.begin_source(
        run=run,
        source_locator=source_locator,
        source_content_hash=source_hash,
        source_version_hash=source_hash,
        source_size_bytes=393216,
        adapter_name="large-recovery-fixture",
        adapter_version="1",
    )
    document = ("[](){}.*+?^$\\| large managed row\n" * 16384)[:393216]
    metadata = stamp_output_metadata({"source_file": source_locator}, session, document)
    client = chromadb.PersistentClient(path=str(palace_path))
    collection = client.get_or_create_collection("drawers")
    collection.add(
        ids=["large-baseline-row"],
        documents=[document],
        metadatas=[metadata],
        embeddings=[[0.1, 0.2, 0.3]],
    )
    session.record_output("large-baseline-row", document)
    session.set_expected(drawers=1)
    baseline = session.complete()
    snapshot = snapshot_managed_source_rows(
        collection,
        source_file=source_locator,
        source_identity=baseline["source"]["identity"],
        local_path=False,
    )
    _, recovery_path = _begin_recovery_rewrite(store, source_locator, baseline, snapshot)

    with _managed_write_scope(store):
        deleted = purge_managed_source_snapshot(
            collection,
            snapshot,
            recovery_path=recovery_path,
            collection_name="drawers",
            source_file=source_locator,
            source_identity=baseline["source"]["identity"],
            local_path=False,
        )

    assert deleted == ["large-baseline-row"]
    assert collection.get(ids=["large-baseline-row"])["ids"] == []


def test_private_purge_boundary_rejects_forged_capability_without_mutation(tmp_path):
    collection = _MemoryCollection()
    collection.rows["protected"] = ("protected", {"source_file": "logical://protected"})
    deletes_before = collection.delete_calls

    with pytest.raises(ReceiptRecoveryError, match="capability is required"):
        write_receipts_module._delete_validated_collection_rows(collection, object())

    assert collection.delete_calls == deletes_before
    assert "protected" in collection.rows


def test_validated_purge_capability_cannot_cross_exclusive_scope(tmp_path):
    _, store, collection, source_locator, baseline, snapshot = _seed_receipted_recovery_source(
        tmp_path
    )
    _, recovery_path = _begin_recovery_rewrite(store, source_locator, baseline, snapshot)
    with _managed_write_scope(store):
        capability = write_receipts_module._validate_managed_source_snapshot_current(
            collection,
            snapshot,
            recovery_path=recovery_path,
            collection_name="drawers",
            source_file=source_locator,
            source_identity=baseline["source"]["identity"],
            local_path=False,
        )

    with _managed_write_scope(store):
        with pytest.raises(ReceiptRecoveryError, match="escaped the exclusive"):
            write_receipts_module._delete_validated_collection_rows(collection, capability)

    assert "baseline-row" in collection.rows


def test_managed_empty_document_is_content_bound_for_delete(tmp_path):
    _, store, collection, source_locator, baseline, _ = _seed_receipted_recovery_source(tmp_path)
    original_metadata = collection.rows["baseline-row"][1]
    collection.rows["baseline-row"] = (
        "",
        {
            **original_metadata,
            META_OUTPUT_CONTENT_HASH: sha256_bytes(b""),
        },
    )
    snapshot = snapshot_managed_source_rows(
        collection,
        source_file=source_locator,
        source_identity=baseline["source"]["identity"],
        local_path=False,
    )
    _, recovery_path = _begin_recovery_rewrite(store, source_locator, baseline, snapshot)

    with _managed_write_scope(store):
        deleted = purge_managed_source_snapshot(
            collection,
            snapshot,
            recovery_path=recovery_path,
            collection_name="drawers",
            source_file=source_locator,
            source_identity=baseline["source"]["identity"],
            local_path=False,
        )

    assert deleted == ["baseline-row"]
    assert collection.rows == {}


def test_embedding_only_managed_replacement_cannot_enter_validation_delete_scope(tmp_path):
    class EmbeddedCollection(_MemoryCollection):
        def __init__(self):
            super().__init__()
            self.embeddings = {}

        def upsert(self, *, documents, ids, metadatas=None, embeddings=None):
            super().upsert(documents=documents, ids=ids, metadatas=metadatas)
            if embeddings is not None:
                for item_id, embedding in zip(ids, embeddings):
                    self.embeddings[item_id] = list(embedding)

        def get(self, **kwargs):
            result = super().get(**kwargs)
            result["embeddings"] = [self.embeddings.get(item_id) for item_id in result["ids"]]
            return result

        def delete(self, **kwargs):
            selected = list(kwargs.get("ids") or ())
            super().delete(**kwargs)
            for item_id in selected:
                if item_id not in self.rows:
                    self.embeddings.pop(item_id, None)

    _, store, run = _store_and_run(tmp_path)
    collection = EmbeddedCollection()
    source = "logical://embedding-lock"
    source_hash = sha256_bytes(b"embedding baseline")
    baseline_session = store.begin_source(
        run=run,
        source_locator=source,
        source_content_hash=source_hash,
        source_version_hash=source_hash,
        source_size_bytes=18,
        adapter_name="embedding-fixture",
        adapter_version="1",
    )
    document = "embedding row"
    collection.upsert(
        documents=[document],
        ids=["embedding-row"],
        metadatas=[stamp_output_metadata({"source_file": source}, baseline_session, document)],
        embeddings=[[0.1, 0.2]],
    )
    baseline_session.record_output("embedding-row", document)
    baseline_session.set_expected(drawers=1)
    baseline = baseline_session.complete()
    snapshot = snapshot_managed_source_rows(
        collection,
        source_file=source,
        source_identity=baseline["source"]["identity"],
        local_path=False,
    )
    _, recovery_path = _begin_recovery_rewrite(store, source, baseline, snapshot)
    race_errors = []

    with _managed_write_scope(store):
        capability = write_receipts_module._validate_managed_source_snapshot_current(
            collection,
            snapshot,
            recovery_path=recovery_path,
            collection_name="drawers",
            source_file=source,
            source_identity=baseline["source"]["identity"],
            local_path=False,
        )

        def managed_embedding_replacement():
            try:
                with write_receipts_module.managed_write_scope(
                    store.palace_path,
                    lock_factory=mine_palace_lock,
                ):
                    collection.embeddings["embedding-row"] = [9.0, 9.0]
            except BaseException as exc:
                race_errors.append(exc)

        racer = threading.Thread(target=managed_embedding_replacement)
        racer.start()
        racer.join(timeout=3)
        assert not racer.is_alive()
        assert len(race_errors) == 1
        assert isinstance(race_errors[0], MineAlreadyRunning)
        assert collection.embeddings["embedding-row"] == [0.1, 0.2]
        write_receipts_module._delete_validated_collection_rows(collection, capability)

    assert collection.rows == {}
    assert collection.embeddings == {}


def test_embedding_only_change_fails_final_delete_recheck(tmp_path):
    class EmbeddedCollection(_MemoryCollection):
        def __init__(self):
            super().__init__()
            self.embeddings = {}

        def upsert(self, *, documents, ids, metadatas=None, embeddings=None):
            super().upsert(documents=documents, ids=ids, metadatas=metadatas)
            if embeddings is not None:
                for item_id, embedding in zip(ids, embeddings):
                    self.embeddings[item_id] = list(embedding)

        def get(self, **kwargs):
            result = super().get(**kwargs)
            if "embeddings" in (kwargs.get("include") or ()):
                result["embeddings"] = [self.embeddings.get(item_id) for item_id in result["ids"]]
            return result

    _, store, run = _store_and_run(tmp_path)
    collection = EmbeddedCollection()
    source = "logical://embedding-final-delete"
    source_hash = sha256_bytes(b"embedding baseline")
    baseline_session = store.begin_source(
        run=run,
        source_locator=source,
        source_content_hash=source_hash,
        source_version_hash=source_hash,
        source_size_bytes=18,
        adapter_name="embedding-fixture",
        adapter_version="1",
    )
    document = "embedding row"
    collection.upsert(
        documents=[document],
        ids=["embedding-row"],
        metadatas=[stamp_output_metadata({"source_file": source}, baseline_session, document)],
        embeddings=[[0.1, 0.2]],
    )
    baseline_session.record_output("embedding-row", document)
    baseline_session.set_expected(drawers=1)
    baseline = baseline_session.complete()
    snapshot = snapshot_managed_source_rows(
        collection,
        source_file=source,
        source_identity=baseline["source"]["identity"],
        local_path=False,
    )
    _, recovery_path = _begin_recovery_rewrite(store, source, baseline, snapshot)

    with _managed_write_scope(store):
        capability = write_receipts_module._validate_managed_source_snapshot_current(
            collection,
            snapshot,
            recovery_path=recovery_path,
            collection_name="drawers",
            source_file=source,
            source_identity=baseline["source"]["identity"],
            local_path=False,
        )
        collection.embeddings["embedding-row"] = [9.0, 9.0]
        with pytest.raises(ReceiptRecoveryError, match="changed before conditional deletion"):
            write_receipts_module._delete_validated_collection_rows(collection, capability)

    assert "embedding-row" in collection.rows
    assert collection.delete_calls == 0


def test_source_file_selector_rejects_contradictory_foreign_identity_before_snapshot(tmp_path):
    _, store, _ = _store_and_run(tmp_path)
    collection = _MemoryCollection()
    source = "logical://selector/target"
    target_identity = store.source_identity(source)
    foreign_identity = store.source_identity("logical://selector/foreign")
    collection.rows["foreign-owned"] = (
        "foreign",
        {
            "source_file": source,
            META_SOURCE_IDENTITY: foreign_identity,
        },
    )

    with pytest.raises(ReceiptConflictError, match="owned by another source"):
        snapshot_managed_source_rows(
            collection,
            source_file=source,
            source_identity=target_identity,
            local_path=False,
        )

    assert collection.delete_calls == 0
    assert "foreign-owned" in collection.rows


def test_identity_selector_rejects_contradictory_foreign_source_file(tmp_path):
    _, store, _ = _store_and_run(tmp_path)
    collection = _MemoryCollection()
    source = "logical://selector/target"
    target_identity = store.source_identity(source)
    collection.rows["foreign-file"] = (
        "foreign",
        {
            "source_file": "logical://selector/foreign",
            META_SOURCE_IDENTITY: target_identity,
        },
    )

    with pytest.raises(ReceiptConflictError, match="another source file"):
        snapshot_managed_source_rows(
            collection,
            source_file=source,
            source_identity=target_identity,
            local_path=False,
        )

    assert collection.delete_calls == 0
    assert "foreign-file" in collection.rows


def test_exact_delete_uses_capable_raw_collection_behind_legacy_wrapper(tmp_path):
    class LegacyDeleteWrapper:
        def __init__(self, raw):
            self._collection = raw

        def get(self, **kwargs):
            return self._collection.get(**kwargs)

        def delete(self, *, ids=None, where=None):
            raise AssertionError(f"unsafe wrapper delete reached: {ids!r} {where!r}")

    _, store, raw, source_locator, baseline, snapshot = _seed_receipted_recovery_source(tmp_path)
    rewrite, recovery_path = _begin_recovery_rewrite(store, source_locator, baseline, snapshot)
    wrapper = LegacyDeleteWrapper(raw)

    with _managed_write_scope(store):
        deleted = purge_managed_source_snapshot(
            wrapper,
            snapshot,
            recovery_path=recovery_path,
            collection_name="drawers",
            source_file=source_locator,
            source_identity=baseline["source"]["identity"],
            local_path=False,
        )

    assert deleted == ["baseline-row"]
    assert raw.rows == {}
    assert recovery_path.exists()
    assert rewrite.receipt_id


def test_exact_delete_fails_closed_when_wrapper_cannot_forward_content_filter(tmp_path):
    class UnsupportedDeleteWrapper:
        def __init__(self, raw):
            self.raw = raw

        def get(self, **kwargs):
            return self.raw.get(**kwargs)

        def delete(self, *, ids=None, where=None):
            self.raw.delete(ids=ids, where=where)

    _, store, raw, source_locator, baseline, snapshot = _seed_receipted_recovery_source(tmp_path)
    _, recovery_path = _begin_recovery_rewrite(store, source_locator, baseline, snapshot)
    wrapper = UnsupportedDeleteWrapper(raw)

    with _managed_write_scope(store):
        with pytest.raises(ReceiptRecoveryError, match="content-bound conditional deletion"):
            purge_managed_source_snapshot(
                wrapper,
                snapshot,
                recovery_path=recovery_path,
                collection_name="drawers",
                source_file=source_locator,
                source_identity=baseline["source"]["identity"],
                local_path=False,
            )

    assert "baseline-row" in raw.rows
    assert recovery_path.exists()


def test_stale_stored_content_hash_is_deleted_only_by_exact_snapshot_document(tmp_path):
    _, store, collection, source_locator, baseline, _ = _seed_receipted_recovery_source(tmp_path)
    original_document, original_metadata = collection.rows["baseline-row"]
    assert original_metadata[META_OUTPUT_CONTENT_HASH] == sha256_bytes(
        original_document.encode("utf-8")
    )
    collection.rows["baseline-row"] = ("STALE NOISE", original_metadata)
    stale_snapshot = snapshot_managed_source_rows(
        collection,
        source_file=source_locator,
        source_identity=baseline["source"]["identity"],
        local_path=False,
    )
    _, recovery_path = _begin_recovery_rewrite(
        store,
        source_locator,
        baseline,
        stale_snapshot,
    )

    with _managed_write_scope(store):
        deleted = purge_managed_source_snapshot(
            collection,
            stale_snapshot,
            recovery_path=recovery_path,
            collection_name="drawers",
            source_file=source_locator,
            source_identity=baseline["source"]["identity"],
            local_path=False,
        )

    assert deleted == ["baseline-row"]
    assert collection.rows == {}


def test_pre_purge_validation_rejects_removed_rows_without_deleting(tmp_path):
    _, store, collection, source_locator, baseline, snapshot = _seed_receipted_recovery_source(
        tmp_path
    )
    _, recovery_path = _begin_recovery_rewrite(store, source_locator, baseline, snapshot)
    collection.rows.pop("baseline-row")
    deletes_before = collection.delete_calls

    with _managed_write_scope(store):
        with pytest.raises(ReceiptRecoveryError, match="removed=1"):
            purge_managed_source_snapshot(
                collection,
                snapshot,
                recovery_path=recovery_path,
                collection_name="drawers",
                source_file=source_locator,
                source_identity=baseline["source"]["identity"],
                local_path=False,
            )

    assert collection.delete_calls == deletes_before
    assert collection.rows == {}


def test_pre_purge_validation_rejects_duplicate_query_ids(tmp_path, monkeypatch):
    _, store, collection, source_locator, baseline, snapshot = _seed_receipted_recovery_source(
        tmp_path
    )
    _, recovery_path = _begin_recovery_rewrite(store, source_locator, baseline, snapshot)
    original_get = collection.get

    def duplicate_get(**kwargs):
        result = original_get(**kwargs)
        if kwargs.get("where") and result["ids"]:
            result["ids"] = [result["ids"][0], result["ids"][0]]
        return result

    monkeypatch.setattr(collection, "get", duplicate_get)
    deletes_before = collection.delete_calls

    with _managed_write_scope(store):
        with pytest.raises(ReceiptIdentityError, match="duplicate purge identities"):
            purge_managed_source_snapshot(
                collection,
                snapshot,
                recovery_path=recovery_path,
                collection_name="drawers",
                source_file=source_locator,
                source_identity=baseline["source"]["identity"],
                local_path=False,
            )

    assert collection.delete_calls == deletes_before
    assert "baseline-row" in collection.rows


@pytest.mark.parametrize("failure_point", ["publication", "flush"])
def test_durability_failure_blocks_project_purge(tmp_path, monkeypatch, failure_point):
    project = tmp_path / "project"
    project.mkdir()
    source = project / "durability.md"
    source.write_text(_long_source_text("before"), encoding="utf-8")
    _, store, first_run = _store_and_run(tmp_path, config={"pipeline": "durability"})
    collection = _MemoryCollection()
    kwargs = {
        "filepath": source,
        "project_path": project,
        "collection": collection,
        "wing": "project",
        "rooms": [{"name": "general", "description": "general"}],
        "agent": "test-runner",
        "dry_run": False,
        "receipt_store": store,
    }
    process_file(receipt_run=first_run, **kwargs)
    first = _current_for_path(store, source)
    rows_before = copy.deepcopy(collection.rows)
    deletes_before = collection.delete_calls
    source.write_text(_long_source_text("after"), encoding="utf-8")
    second_run = store.create_run(
        caller="test-runner",
        mode="test",
        config={"pipeline": "durability"},
    )

    if failure_point == "publication":

        def fail_publication(*_args, **_kwargs):
            raise ReceiptDurabilityError("simulated atomic publication failure")

        monkeypatch.setattr(write_receipts_module, "_publish_durable_file", fail_publication)
    else:

        def fail_flush(*_args, **_kwargs):
            raise OSError("simulated durable flush failure")

        monkeypatch.setattr(
            write_receipts_module,
            "_flush_and_verify_published_file",
            fail_flush,
        )

    with pytest.raises(ReceiptDurabilityError):
        process_file(receipt_run=second_run, **kwargs)

    assert collection.rows == rows_before
    assert collection.delete_calls == deletes_before
    assert _current_for_path(store, source)["receipt_id"] == first["receipt_id"]


def test_durable_json_publication_returns_reopened_hash_proof(tmp_path):
    path = tmp_path / "receipt-state" / "recovery.json"
    payload = {"schema": "test/durable-publication", "value": 1}
    proof = write_receipts_module._atomic_write_json(
        path,
        payload,
        immutable=True,
        durable=True,
    )
    repeated = write_receipts_module._atomic_write_json(
        path,
        payload,
        immutable=True,
        durable=True,
    )

    assert proof is not None
    assert repeated is not None
    assert proof.path == path.resolve()
    assert repeated.content_sha256 == proof.content_sha256
    assert proof.size_bytes == path.stat().st_size
    assert proof.content_sha256 == sha256_bytes(path.read_bytes())
    assert proof.primitive == (
        "windows-directory-marker+movefileex-write-through"
        if os.name == "nt"
        else "posix-link-fsync"
    )
    with pytest.raises(ReceiptConflictError, match="immutable receipt already exists"):
        write_receipts_module._atomic_write_json(
            path,
            {**payload, "value": 2},
            immutable=True,
            durable=True,
        )


def test_current_index_beats_reversed_wall_clock_ordering(tmp_path):
    palace = tmp_path / "palace"
    store = ReceiptStore(palace, clock=lambda: "2026-07-12T12:00:00Z")
    first_run = store.create_run(caller="test-runner", mode="test", config={})
    first_hash = sha256_bytes(b"newer wall clock")
    first = store.begin_source(
        run=first_run,
        source_locator="logical://clock/rollback",
        source_content_hash=first_hash,
        source_version_hash=first_hash,
        source_size_bytes=16,
        adapter_name="fixture",
        adapter_version="1",
    )
    first.set_expected(drawers=0)
    first.complete()

    store.clock = lambda: "2026-07-11T12:00:00Z"
    second_run = store.create_run(caller="test-runner", mode="test", config={})
    second_hash = sha256_bytes(b"authoritative successor")
    second = store.begin_source(
        run=second_run,
        source_locator="logical://clock/rollback",
        source_content_hash=second_hash,
        source_version_hash=second_hash,
        source_size_bytes=23,
        adapter_name="fixture",
        adapter_version="1",
    )
    second.set_expected(drawers=0)
    second_complete = second.complete()

    reopened = ReceiptStore(palace)
    current = reopened.find_current(second_complete["source"]["identity"])
    assert current["receipt_id"] == second_complete["receipt_id"]
    assert current["event_time"] == "2026-07-11T12:00:00Z"


def test_receipt_verifier_resolves_current_without_repairing_missing_index(tmp_path):
    _, store, collection, _, complete, _ = _seed_receipted_recovery_source(tmp_path)
    index_path = next(store.sources_dir.glob("*.json"))
    index_path.unlink()

    result = verify_receipt(complete, collection, store=store)

    assert result.status == "represented"
    assert not index_path.exists()


def test_complete_without_durable_publication_marker_never_verifies(tmp_path):
    _, _, collection, _, complete, _ = _seed_receipted_recovery_source(tmp_path)
    unmarked = copy.deepcopy(complete)
    unmarked.pop("publication")

    with pytest.raises(ReceiptIdentityError, match="COMPLETE publication"):
        verify_receipt(unmarked, collection)


def test_find_current_reconciles_newer_journal_event_after_index_refresh_failure(
    tmp_path, monkeypatch
):
    _, store, first_run = _store_and_run(tmp_path)
    first_hash = sha256_bytes(b"source-v1")
    first = store.begin_source(
        run=first_run,
        source_locator="logical://source/index-failure",
        source_content_hash=first_hash,
        source_version_hash=first_hash,
        source_size_bytes=9,
        adapter_name="fixture",
        adapter_version="1",
    )
    first.set_expected(drawers=0)
    first_complete = first.complete()

    second_run = store.create_run(caller="test-runner", mode="test", config={"fixture": 1})
    second_hash = sha256_bytes(b"source-v2")
    second = store.begin_source(
        run=second_run,
        source_locator="logical://source/index-failure",
        source_content_hash=second_hash,
        source_version_hash=second_hash,
        source_size_bytes=9,
        adapter_name="fixture",
        adapter_version="1",
    )
    second.set_expected(drawers=0)
    original_set_current = store.set_current

    def fail_index_refresh(_event, _event_path):
        raise OSError("simulated current-index publication failure")

    monkeypatch.setattr(store, "set_current", fail_index_refresh)
    second_complete = second.complete()
    monkeypatch.setattr(store, "set_current", original_set_current)
    current = store.find_current(first_complete["source"]["identity"])

    assert second_complete["state"] == "COMPLETE"
    assert current is not None
    assert current["receipt_id"] == second_complete["receipt_id"]
    assert current["source"]["content_hash"] == second_hash


def test_find_current_rejects_index_event_with_mismatched_source_identity(tmp_path):
    _, store, run = _store_and_run(tmp_path)
    source_one_hash = sha256_bytes(b"source-one")
    source_one = store.begin_source(
        run=run,
        source_locator="logical://source/one",
        source_content_hash=source_one_hash,
        source_version_hash=source_one_hash,
        source_size_bytes=10,
        adapter_name="fixture",
        adapter_version="1",
    )
    source_one.set_expected(drawers=0)
    source_one_complete = source_one.complete()

    second_run = store.create_run(caller="test-runner", mode="test", config={"fixture": 1})
    source_two_hash = sha256_bytes(b"source-two")
    source_two = store.begin_source(
        run=second_run,
        source_locator="logical://source/two",
        source_content_hash=source_two_hash,
        source_version_hash=source_two_hash,
        source_size_bytes=10,
        adapter_name="fixture",
        adapter_version="1",
    )
    source_two.set_expected(drawers=0)
    source_two_complete = source_two.complete()

    source_one_identity = source_one_complete["source"]["identity"]
    source_one_index = next(
        path
        for path in store.sources_dir.glob("*.json")
        if json.loads(path.read_text(encoding="utf-8"))["source_identity"] == source_one_identity
    )
    mismatched = {
        "schema": "mempalace-source-write-receipt-index/v1",
        "source_identity": source_one_identity,
        "receipt_id": source_two_complete["receipt_id"],
        "event_path": source_two.last_event_path.relative_to(store.root).as_posix(),
        "source_content_hash": source_two_complete["source"]["content_hash"],
        "source_version_hash": source_two_complete["source"]["version_hash"],
        "config_digest": source_two_complete["producer"]["config"]["digest"],
        "updated_at": source_two_complete["event_time"],
    }
    source_one_index.write_text(json.dumps(mismatched), encoding="utf-8")

    with pytest.raises(ReceiptConflictError, match="current receipt index is inconsistent"):
        store.find_current(source_one_identity)


def test_indexed_lineage_rejects_a_disconnected_complete_branch(tmp_path):
    indexed_id = "00000000-0000-4000-8000-000000000001"
    disconnected_id = "00000000-0000-4000-8000-000000000002"
    candidates = {
        indexed_id: (
            {"receipt_id": indexed_id, "relations": {}},
            tmp_path / "indexed-complete.json",
        ),
        disconnected_id: (
            {"receipt_id": disconnected_id, "relations": {}},
            tmp_path / "disconnected-complete.json",
        ),
    }

    with pytest.raises(ReceiptConflictError, match="disconnected or ambiguous"):
        write_receipts_module._resolve_receipt_lineage(
            candidates,
            indexed_receipt_id=indexed_id,
        )


def test_declared_missing_predecessor_fails_closed_without_explicit_legacy_rule(tmp_path):
    receipt_id = "00000000-0000-4000-8000-000000000011"
    missing_id = "00000000-0000-4000-8000-000000000012"
    candidates = {
        receipt_id: (
            {
                "receipt_id": receipt_id,
                "relations": {"predecessor_receipt_id": missing_id},
            },
            tmp_path / "complete.json",
        )
    }

    with pytest.raises(ReceiptConflictError, match="missing predecessor"):
        write_receipts_module._resolve_receipt_lineage(candidates)

    candidates[receipt_id][0]["relations"]["legacy_missing_predecessor_compatibility"] = (
        write_receipts_module.LEGACY_MISSING_PREDECESSOR_COMPATIBILITY
    )
    resolved, _ = write_receipts_module._resolve_receipt_lineage(candidates)
    assert resolved["receipt_id"] == receipt_id


def test_shared_projection_omits_paths_content_ids_and_raw_caller(tmp_path):
    palace = tmp_path / "palace"
    store = ReceiptStore(palace)
    run = store.create_run(
        caller="private.person@example.com",
        mode="project",
        config={"private_path": "C:/Users/Private/config.yml"},
    )
    source_path = "C:/Users/Private/Documents/medical-notes.txt"
    source_content = "private medical content"
    source_hash = sha256_bytes(source_content.encode("utf-8"))
    session = store.begin_source(
        run=run,
        source_locator=source_path,
        source_content_hash=source_hash,
        source_version_hash=source_hash,
        source_size_bytes=len(source_content),
        adapter_name="filesystem",
        adapter_version="2",
        local_path=True,
    )
    session.set_expected(drawers=1)
    session.running("writing")
    session.record_output("drawer_private_wing_identifier", source_content)
    receipt = session.complete()

    local_receipt_text = json.dumps(receipt, sort_keys=True)
    projection = shared_receipt_projection(receipt)
    projection_text = json.dumps(projection, sort_keys=True)
    assert source_path not in local_receipt_text
    assert source_content not in local_receipt_text
    assert "private.person@example.com" not in local_receipt_text
    assert "C:/Users/Private/config.yml" not in local_receipt_text
    assert source_path not in projection_text
    assert source_content not in projection_text
    assert "drawer_private_wing_identifier" not in projection_text
    assert "private.person@example.com" not in projection_text
    assert "C:/Users/Private/config.yml" not in projection_text
    assert receipt["source"]["identity"].startswith("hmac-sha256:")
    assert projection["projection"] == "pseudonymized-shared"
    assert projection["source"] == {
        "identity": receipt["source"]["identity"],
        "content_identity": receipt["source"]["shared_content_identity"],
        "version_identity": receipt["source"]["shared_version_identity"],
        "size_bucket": receipt["source"]["size_bucket"],
        "adapter": {"name": "filesystem", "version": "2"},
    }
    assert source_hash not in projection_text
    assert "content_hash" not in projection["source"]
    assert "version_hash" not in projection["source"]
    assert "size_bytes" not in projection["source"]
    assert "source_bytes" not in projection["counts"]
    assert projection["source"]["size_bucket"] != str(len(source_content))

    malformed = copy.deepcopy(receipt)
    malformed["stage"] = "C:/private/stage"
    with pytest.raises(ReceiptIdentityError):
        shared_receipt_projection(malformed)
    malformed = copy.deepcopy(receipt)
    malformed["producer"]["git"]["state"] = "mystery"
    with pytest.raises(ReceiptIdentityError):
        shared_receipt_projection(malformed)


def test_local_source_aliases_resolve_to_one_private_identity(tmp_path):
    project = tmp_path / "project"
    nested = project / "nested"
    nested.mkdir(parents=True)
    source = project / "source.txt"
    source.write_text("same source", encoding="utf-8")
    store = ReceiptStore(tmp_path / "palace")
    alias = nested / ".." / "source.txt"

    assert store.source_identity(str(source), local_path=True) == store.source_identity(
        str(alias), local_path=True
    )


def test_alias_rewrite_and_zero_output_purge_use_one_canonical_source_key(tmp_path):
    source_dir = tmp_path / "conversations"
    nested = source_dir / "nested"
    nested.mkdir(parents=True)
    source = source_dir / "alias.txt"
    source.write_text(_long_source_text("before"), encoding="utf-8")
    alias = nested / ".." / "alias.txt"
    canonical = canonical_source_locator(alias, local_path=True)
    config = {"pipeline": "conversations", "extract_mode": "exchange"}
    _, store, first_run = _store_and_run(
        tmp_path,
        mode="conversations:exchange",
        config=config,
    )
    collection = _MemoryCollection()
    kwargs = {
        "collection": collection,
        "wing": "conversations",
        "agent": "test-runner",
        "extract_mode": "exchange",
        "dry_run": False,
        "index": 1,
        "total_files": 1,
        "receipt_store": store,
    }

    _process_conversation_file(filepath=alias, receipt_run=first_run, **kwargs)
    first = _current_for_path(store, source)
    first_ids = {item["id"] for item in first["outputs"]["identities"]}
    assert first_ids
    assert {metadata["source_file"] for _, metadata in collection.rows.values()} == {canonical}

    # Reproduce rows from the old mismatch: canonical receipt identity but an
    # alias spelling in source_file metadata. Identity-based purge must catch it.
    for item_id in first_ids:
        document, metadata = collection.rows[item_id]
        collection.rows[item_id] = (document, {**metadata, "source_file": str(alias)})

    source.write_text("tiny", encoding="utf-8")
    second_run = store.create_run(
        caller="test-runner",
        mode="conversations:exchange",
        config=config,
    )
    _process_conversation_file(filepath=source, receipt_run=second_run, **kwargs)
    second = _current_for_path(store, source)
    current_ids = set(collection.get(where={"source_file": canonical})["ids"])

    assert second["disposition"] == "ZERO_OUTPUT"
    assert current_ids == {second["outputs"]["identities"][0]["id"]}
    assert not first_ids & set(collection.rows)
    assert verify_receipt(second, collection, store=store).status == "represented"


class _ReceiptAdapter(BaseSourceAdapter):
    name = "receipt-fixture"
    adapter_version = "1.0"
    capabilities = frozenset({"supports_incremental"})

    def __init__(self):
        self.current = False

    def ingest(self, *, source, palace):
        content_hash = sha256_bytes(b"adapter source bytes")
        yield SourceItemMetadata(
            source_file=source.uri,
            version="source-revision-1",
            size_hint=20,
            content_hash=content_hash,
        )
        if palace._skip_requested:
            return
        yield DrawerRecord(content="chunk zero", source_file=source.uri, chunk_index=0)
        yield DrawerRecord(content="chunk one", source_file=source.uri, chunk_index=1)

    def describe_schema(self):
        return AdapterSchema(fields={}, version="1")

    def is_current(self, *, item, existing_metadata):
        del item, existing_metadata
        return self.current


def test_managed_source_adapter_emits_receipts_and_reuses_unchanged_output(tmp_path):
    palace, store, _ = _store_and_run(tmp_path)
    collection = _MemoryCollection()
    context = PalaceContext(
        drawer_collection=collection,
        knowledge_graph=_FakeKnowledgeGraph(),
        palace_path=str(palace),
        adapter_name="receipt-fixture",
        adapter_version="1.0",
    )
    adapter = _ReceiptAdapter()
    source = SourceRef(uri="logical://adapter/item-1")

    first = managed_adapter_ingest(
        adapter=adapter,
        source=source,
        palace=context,
        receipt_store=store,
        caller="test-runner",
        config={"fixture": True},
    )
    first_writes = collection.upsert_calls
    first_updates = collection.update_calls
    adapter.current = True
    second = managed_adapter_ingest(
        adapter=adapter,
        source=source,
        palace=context,
        receipt_store=store,
        caller="test-runner",
        config={"fixture": True},
    )

    current = store.find_current(store.source_identity(source.uri))
    assert current is not None
    assert first.drawers_written == 2
    assert first.receipt_verification_statuses == ("represented",)
    assert first.receipt_validation_errors == (None,)
    assert second.sources_unchanged == 1
    assert second.receipt_verification_statuses == ("represented",)
    assert second.receipt_validation_errors == (None,)
    assert collection.upsert_calls == first_writes
    assert collection.update_calls == first_updates + 1
    assert current["disposition"] == "UNCHANGED"
    assert verify_receipt(current, collection, store=store).status == "represented"


def test_managed_adapter_default_post_commit_verification_failure_still_raises(
    tmp_path, monkeypatch
):
    palace, store, _ = _store_and_run(tmp_path)
    collection = _MemoryCollection()
    context = PalaceContext(
        drawer_collection=collection,
        knowledge_graph=_FakeKnowledgeGraph(),
        palace_path=str(palace),
        adapter_name="receipt-fixture",
        adapter_version="1.0",
    )
    source = SourceRef(uri="logical://adapter/default-terminal-error")

    def fail_terminal_verification(*args, **kwargs):
        raise RuntimeError("injected default terminal verification failure")

    monkeypatch.setattr(
        provenance_module,
        "_verify_context_receipt",
        fail_terminal_verification,
    )
    with pytest.raises(RuntimeError, match="injected default terminal verification failure"):
        managed_adapter_ingest(
            adapter=_ReceiptAdapter(),
            source=source,
            palace=context,
            receipt_store=store,
            caller="test-runner",
            config={"fixture": True},
        )

    current = store.find_current(store.source_identity(source.uri))
    assert current is not None
    assert current["state"] == "COMPLETE"
    assert len(list(store._pending_recovery_paths())) == 1


def test_managed_adapter_raw_collection_write_is_stamped_and_receipted(tmp_path):
    class RawCollectionAdapter(_ReceiptAdapter):
        name = "raw-collection-fixture"

        def ingest(self, *, source, palace):
            content_hash = sha256_bytes(b"raw adapter source")
            yield SourceItemMetadata(
                source_file=source.uri,
                version="raw-v1",
                size_hint=18,
                content_hash=content_hash,
            )
            palace.drawer_collection.upsert(
                documents=["raw collection output"],
                ids=["raw-output"],
                metadatas=[{"source_file": "ambiguous-alias"}],
            )

    palace, store, _ = _store_and_run(tmp_path)
    collection = _MemoryCollection()
    context = PalaceContext(
        drawer_collection=collection,
        knowledge_graph=_FakeKnowledgeGraph(),
        palace_path=str(palace),
    )
    source = SourceRef(uri="logical://adapter/raw-write")

    result = managed_adapter_ingest(
        adapter=RawCollectionAdapter(),
        source=source,
        palace=context,
        receipt_store=store,
        caller="test-runner",
        config={"fixture": True},
    )

    current = store.find_current(store.source_identity(source.uri))
    assert current is not None
    assert result.drawers_written == 1
    assert current["outputs"]["count"] == 1
    assert current["outputs"]["identities"][0]["id"] == "raw-output"
    assert collection.rows["raw-output"][1]["source_file"] == source.uri
    assert verify_receipt(current, collection, store=store).status == "represented"


def test_palace_context_does_not_publish_accidental_raw_collection_attributes(tmp_path):
    palace, _, _ = _store_and_run(tmp_path)
    collection = _MemoryCollection()
    graph = _FakeKnowledgeGraph()
    context = PalaceContext(
        drawer_collection=collection,
        knowledge_graph=graph,
        palace_path=str(palace),
    )

    assert not hasattr(context, "_raw_drawer_collection")
    assert not hasattr(context.drawer_collection, "_raw")
    assert not hasattr(context, "_receipt_collections")
    assert collection is not context.drawer_collection
    assert graph is not context.knowledge_graph
    assert collection not in vars(context).values()
    assert graph not in vars(context).values()
    with pytest.raises(TypeError):
        vars(context.drawer_collection)
    with pytest.raises(TypeError):
        vars(context.knowledge_graph)
    assert context._managed_collection_names() == ("drawers",)


def test_managed_adapter_rejects_cross_source_id_ownership_and_rolls_back(tmp_path):
    class ForeignIdAdapter(_ReceiptAdapter):
        name = "foreign-id-fixture"

        def ingest(self, *, source, palace):
            content_hash = sha256_bytes(b"second source")
            yield SourceItemMetadata(
                source_file=source.uri,
                version="second-v1",
                size_hint=13,
                content_hash=content_hash,
            )
            palace.drawer_collection.upsert(
                documents=["must not overwrite"],
                ids=["shared-id"],
                metadatas=[{"source_file": source.uri}],
            )

    palace, store, first_run = _store_and_run(tmp_path)
    collection = _MemoryCollection()
    first_hash = sha256_bytes(b"first source")
    first = store.begin_source(
        run=first_run,
        source_locator="logical://source/one",
        source_content_hash=first_hash,
        source_version_hash=first_hash,
        source_size_bytes=12,
        adapter_name="first-fixture",
        adapter_version="1",
    )
    with _managed_write_scope(store):
        write_receipts_module.write_receipted_collection_batch(
            collection,
            "upsert",
            {
                "documents": ["owned by source one"],
                "ids": ["shared-id"],
                "metadatas": [{"source_file": "logical://source/one"}],
            },
            session=first,
            source_file="logical://source/one",
        )
    first.set_expected(drawers=1)
    first.complete()
    row_before = copy.deepcopy(collection.rows["shared-id"])
    writes_before = collection.upsert_calls
    context = PalaceContext(
        drawer_collection=collection,
        knowledge_graph=_FakeKnowledgeGraph(),
        palace_path=str(palace),
    )

    with pytest.raises(ReceiptConflictError, match="owned by another source"):
        managed_adapter_ingest(
            adapter=ForeignIdAdapter(),
            source=SourceRef(uri="logical://source/two"),
            palace=context,
            receipt_store=store,
            caller="test-runner",
            config={"fixture": True},
        )

    assert collection.rows["shared-id"] == row_before
    assert collection.upsert_calls == writes_before


def test_upsert_absent_id_race_uses_add_and_never_overwrites_foreign_row(tmp_path):
    class InsertForeignBeforeAdd(_MemoryCollection):
        armed = True

        def add(self, **kwargs):
            if self.armed:
                self.armed = False
                item_id = kwargs["ids"][0]
                self.rows[item_id] = (
                    "foreign won",
                    {
                        "source_file": "logical://foreign-owner",
                        META_SOURCE_IDENTITY: foreign_identity,
                    },
                )
            return super().add(**kwargs)

    _, store, run = _store_and_run(tmp_path)
    collection = InsertForeignBeforeAdd()
    source = "logical://upsert-race"
    foreign_identity = store.source_identity("logical://foreign-owner")
    source_hash = sha256_bytes(b"race source")
    session = store.begin_source(
        run=run,
        source_locator=source,
        source_content_hash=source_hash,
        source_version_hash=source_hash,
        source_size_bytes=11,
        adapter_name="race-fixture",
        adapter_version="1",
    )

    with _managed_write_scope(store):
        with pytest.raises(ValueError, match="duplicate IDs"):
            write_receipts_module.write_receipted_collection_batch(
                collection,
                "upsert",
                {
                    "documents": ["must not overwrite"],
                    "ids": ["raced-id"],
                    "metadatas": [{"source_file": source}],
                },
                session=session,
                source_file=source,
            )

    assert collection.rows["raced-id"][0] == "foreign won"
    assert collection.rows["raced-id"][1][META_SOURCE_IDENTITY] == foreign_identity
    assert session.outputs == []
    second_identity = store.source_identity("logical://source/two")
    assert store._pending_recovery_paths(second_identity) == []


def test_real_chroma_receipt_readback_does_not_require_embeddings(
    tmp_path, palace_path, collection
):
    class NoEmbeddingRead:
        def __init__(self, raw):
            self.raw = raw
            self.embedding_reads = 0

        def __getattr__(self, name):
            return getattr(self.raw, name)

        def get(self, **kwargs):
            if "embeddings" in (kwargs.get("include") or ()):
                self.embedding_reads += 1
                raise AssertionError("post-write embedding read is deliberately unavailable")
            return self.raw.get(**kwargs)

    store = ReceiptStore(palace_path)
    run = store.create_run(caller="test-runner", mode="real-chroma", config={})
    source = "logical://real-chroma/readback"
    source_hash = sha256_bytes(b"real Chroma exact receipt proof")
    session = store.begin_source(
        run=run,
        source_locator=source,
        source_content_hash=source_hash,
        source_version_hash=source_hash,
        source_size_bytes=31,
        adapter_name="real-chroma-fixture",
        adapter_version="1",
    )
    guarded = NoEmbeddingRead(collection)

    with _managed_write_scope(store):
        write_receipts_module.write_receipted_collection_batch(
            guarded,
            "upsert",
            {
                "documents": ["real Chroma exact receipt proof"],
                "ids": ["real-chroma-row"],
                "metadatas": [{"source_file": source}],
            },
            session=session,
            source_file=source,
        )

    session.set_expected(drawers=1)
    complete = session.complete()
    assert guarded.embedding_reads == 0
    assert verify_receipt(complete, collection, store=store).status == "represented"


def test_managed_write_readback_waits_for_exact_document_metadata_visibility(tmp_path):
    class StaleOnceCollection(_MemoryCollection):
        def __init__(self):
            super().__init__()
            self.written = False
            self.post_write_reads = 0

        def add(self, **kwargs):
            super().add(**kwargs)
            self.written = True

        def get(self, **kwargs):
            result = super().get(**kwargs)
            if self.written and kwargs.get("include") == ["documents", "metadatas"]:
                self.post_write_reads += 1
                if self.post_write_reads == 1 and result["documents"]:
                    result["documents"] = ["temporarily stale"]
            return result

    _, store, run = _store_and_run(tmp_path)
    collection = StaleOnceCollection()
    source = "logical://readback/stabilizes"
    source_hash = sha256_bytes(b"stable source")
    session = store.begin_source(
        run=run,
        source_locator=source,
        source_content_hash=source_hash,
        source_version_hash=source_hash,
        source_size_bytes=13,
        adapter_name="readback-fixture",
        adapter_version="1",
    )

    with _managed_write_scope(store):
        write_receipts_module.write_receipted_collection_batch(
            collection,
            "upsert",
            {
                "documents": ["stable output"],
                "ids": ["stable-row"],
                "metadatas": [{"source_file": source}],
            },
            session=session,
            source_file=source,
        )

    assert collection.post_write_reads == 2
    assert [item["id"] for item in session.outputs] == ["stable-row"]


def test_managed_write_readback_fails_closed_when_exact_visibility_never_arrives(
    tmp_path, monkeypatch
):
    class PermanentlyStaleCollection(_MemoryCollection):
        def __init__(self):
            super().__init__()
            self.written = False
            self.post_write_reads = 0

        def add(self, **kwargs):
            super().add(**kwargs)
            self.written = True

        def get(self, **kwargs):
            result = super().get(**kwargs)
            if (
                self.written
                and kwargs.get("include") == ["documents", "metadatas"]
                and result["documents"]
            ):
                self.post_write_reads += 1
                result["documents"] = ["permanently stale"]
            return result

    monkeypatch.setattr(
        write_receipts_module,
        "_MANAGED_WRITE_READBACK_TIMEOUT_SECONDS",
        0.0,
    )
    _, store, run = _store_and_run(tmp_path)
    collection = PermanentlyStaleCollection()
    source = "logical://readback/never-stabilizes"
    source_hash = sha256_bytes(b"unstable source")
    session = store.begin_source(
        run=run,
        source_locator=source,
        source_content_hash=source_hash,
        source_version_hash=source_hash,
        source_size_bytes=15,
        adapter_name="readback-fixture",
        adapter_version="1",
    )

    with _managed_write_scope(store):
        with pytest.raises(ReceiptRecoveryError, match="exact readback did not match"):
            write_receipts_module.write_receipted_collection_batch(
                collection,
                "upsert",
                {
                    "documents": ["expected output"],
                    "ids": ["unstable-row"],
                    "metadatas": [{"source_file": source}],
                },
                session=session,
                source_file=source,
            )

    assert session.outputs == []
    assert collection.post_write_reads == 1


def test_managed_write_retries_supported_exact_embedding_readback(tmp_path):
    class DelayedExactCollection(_MemoryCollection):
        def __init__(self):
            super().__init__()
            self.exact_reads = 0
            self.embeddings = {}

        def upsert(self, **kwargs):
            ids = list(kwargs["ids"])
            embeddings = kwargs.get("embeddings")
            super().upsert(**kwargs)
            if embeddings is not None:
                self.embeddings.update(
                    {item_id: tuple(embedding) for item_id, embedding in zip(ids, embeddings)}
                )

        def get_exact_embeddings(self, ids):
            self.exact_reads += 1
            if self.exact_reads == 1:
                raise EmbeddingVisibilityError("Nothing found on disk")
            return {item_id: self.embeddings[item_id] for item_id in ids}

    _, store, run = _store_and_run(tmp_path)
    collection = DelayedExactCollection()
    source = "logical://readback/delayed-exact-embedding"
    source_hash = sha256_bytes(b"delayed exact embedding")
    session = store.begin_source(
        run=run,
        source_locator=source,
        source_content_hash=source_hash,
        source_version_hash=source_hash,
        source_size_bytes=23,
        adapter_name="readback-fixture",
        adapter_version="1",
    )

    with _managed_write_scope(store):
        write_receipts_module.write_receipted_collection_batch(
            collection,
            "upsert",
            {
                "documents": ["stable output"],
                "ids": ["stable-row"],
                "metadatas": [{"source_file": source}],
                "embeddings": [[0.25, 0.5]],
            },
            session=session,
            source_file=source,
        )

    assert collection.exact_reads == 2
    assert [item["id"] for item in session.outputs] == ["stable-row"]


def test_exact_snapshot_retries_supported_delayed_embedding_visibility():
    class DelayedSnapshotCollection(_MemoryCollection):
        def __init__(self):
            super().__init__()
            self.exact_reads = 0

        def get_exact_embeddings(self, ids):
            self.exact_reads += 1
            if self.exact_reads == 1:
                raise EmbeddingVisibilityError("Nothing found on disk")
            return {item_id: (0.25, 0.5) for item_id in ids}

    collection = DelayedSnapshotCollection()
    collection.upsert(
        documents=["stable output"],
        ids=["stable-row"],
        metadatas=[{"source_file": "logical://snapshot/delayed"}],
    )

    rows = write_receipts_module._collection_rows_for_ids(
        collection,
        ["stable-row"],
        include_embeddings=True,
    )

    assert collection.exact_reads == 2
    assert rows["stable-row"][2] == (0.25, 0.5)


def test_exact_snapshot_fails_closed_when_embedding_visibility_never_arrives(monkeypatch):
    class MissingSnapshotCollection(_MemoryCollection):
        def get_exact_embeddings(self, _ids):
            raise EmbeddingVisibilityError("Nothing found on disk")

    collection = MissingSnapshotCollection()
    collection.upsert(
        documents=["stable output"],
        ids=["stable-row"],
        metadatas=[{"source_file": "logical://snapshot/missing"}],
    )
    monkeypatch.setattr(
        write_receipts_module,
        "_MANAGED_WRITE_READBACK_TIMEOUT_SECONDS",
        0.0,
    )

    with pytest.raises(ReceiptIdentityError, match="exact embeddings did not stabilize"):
        write_receipts_module._collection_rows_for_ids(
            collection,
            ["stable-row"],
            include_embeddings=True,
        )


@pytest.mark.parametrize(
    "raw", ["0", "0.001", "0.5", "0.999", "-1", "nan", "inf", "1e300", "60.01", "not-a-number"]
)
def test_readback_timeout_env_rejects_non_positive_non_finite_and_invalid_values(monkeypatch, raw):
    monkeypatch.setenv("MEMPALACE_MANAGED_WRITE_READBACK_TIMEOUT_SECONDS", raw)

    assert (
        write_receipts_module._read_positive_finite_timeout_from_env(
            "MEMPALACE_MANAGED_WRITE_READBACK_TIMEOUT_SECONDS", 5.0
        )
        == 5.0
    )


def test_readback_timeout_env_accepts_a_positive_finite_value(monkeypatch):
    monkeypatch.setenv("MEMPALACE_MANAGED_WRITE_READBACK_TIMEOUT_SECONDS", "12.5")

    assert (
        write_receipts_module._read_positive_finite_timeout_from_env(
            "MEMPALACE_MANAGED_WRITE_READBACK_TIMEOUT_SECONDS", 5.0
        )
        == 12.5
    )


def test_readback_timeout_env_accepts_its_reviewed_upper_bound(monkeypatch):
    monkeypatch.setenv("MEMPALACE_MANAGED_WRITE_READBACK_TIMEOUT_SECONDS", "60")

    assert (
        write_receipts_module._read_positive_finite_timeout_from_env(
            "MEMPALACE_MANAGED_WRITE_READBACK_TIMEOUT_SECONDS", 5.0
        )
        == 60.0
    )


def test_readback_timeout_invalid_env_log_does_not_echo_the_raw_value(monkeypatch, caplog):
    raw = "private-config-value"
    monkeypatch.setenv("MEMPALACE_MANAGED_WRITE_READBACK_TIMEOUT_SECONDS", raw)

    with caplog.at_level("WARNING", logger="mempalace.write_receipts"):
        assert (
            write_receipts_module._read_positive_finite_timeout_from_env(
                "MEMPALACE_MANAGED_WRITE_READBACK_TIMEOUT_SECONDS", 5.0
            )
            == 5.0
        )

    assert raw not in caplog.text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, 5.0),
        ("12.5", 12.5),
        ("0", 5.0),
        ("0.999", 5.0),
        ("nan", 5.0),
        ("inf", 5.0),
        ("1e300", 5.0),
        ("not-a-number", 5.0),
    ],
)
def test_readback_timeout_env_is_applied_during_a_fresh_module_import(monkeypatch, raw, expected):
    """The import-time constant must use the bounded parser, not just expose it."""
    module_name = "mempalace._test_write_receipts_import_config"
    spec = importlib.util.spec_from_file_location(module_name, write_receipts_module.__file__)
    assert spec is not None
    assert spec.loader is not None
    fresh_module = importlib.util.module_from_spec(spec)

    with monkeypatch.context() as environment:
        if raw is None:
            environment.delenv("MEMPALACE_MANAGED_WRITE_READBACK_TIMEOUT_SECONDS", raising=False)
        else:
            environment.setenv("MEMPALACE_MANAGED_WRITE_READBACK_TIMEOUT_SECONDS", raw)
        sys.modules[module_name] = fresh_module
        try:
            spec.loader.exec_module(fresh_module)
            assert fresh_module._MANAGED_WRITE_READBACK_TIMEOUT_SECONDS == expected
        finally:
            sys.modules.pop(module_name, None)


def test_exact_embedding_readback_propagates_unrelated_backend_errors():
    class ClosedCollection(_MemoryCollection):
        def get_exact_embeddings(self, ids):
            raise RuntimeError("client is closed")

    collection = ClosedCollection()
    collection.upsert(
        documents=["stable output"],
        ids=["stable-row"],
        metadatas=[{"source_file": "logical://readback/closed"}],
    )

    with pytest.raises(RuntimeError, match="client is closed"):
        write_receipts_module._collection_embeddings_for_ids(collection, ["stable-row"])


def test_existing_id_embedding_change_fails_before_managed_update(tmp_path):
    class EmbeddingRaceCollection(_MemoryCollection):
        def __init__(self):
            super().__init__()
            self.embeddings = {}
            self.document_reads = 0
            self.update_calls = 0

        def upsert(self, *, documents, ids, metadatas=None, embeddings=None):
            super().upsert(documents=documents, ids=ids, metadatas=metadatas)
            if embeddings is not None:
                for item_id, embedding in zip(ids, embeddings):
                    self.embeddings[item_id] = list(embedding)

        def update(self, **kwargs):
            self.update_calls += 1
            return super().update(**kwargs)

        def get(self, **kwargs):
            result = super().get(**kwargs)
            include = kwargs.get("include") or ()
            if include == ["documents", "metadatas"]:
                self.document_reads += 1
                if self.document_reads == 2:
                    self.embeddings["existing-row"] = [9.0, 9.0]
            if "embeddings" in include:
                result["embeddings"] = [self.embeddings.get(item_id) for item_id in result["ids"]]
            return result

    _, store, run = _store_and_run(tmp_path)
    collection = EmbeddingRaceCollection()
    source = "logical://existing-id/embedding-race"
    source_hash = sha256_bytes(b"existing source")
    session = store.begin_source(
        run=run,
        source_locator=source,
        source_content_hash=source_hash,
        source_version_hash=source_hash,
        source_size_bytes=15,
        adapter_name="embedding-race-fixture",
        adapter_version="1",
    )
    original = "original document"
    collection.upsert(
        documents=[original],
        ids=["existing-row"],
        metadatas=[stamp_output_metadata({"source_file": source}, session, original)],
        embeddings=[[0.1, 0.2]],
    )

    with _managed_write_scope(store):
        with pytest.raises(ReceiptConflictError, match="identity changed before mutation"):
            write_receipts_module.write_receipted_collection_batch(
                collection,
                "update",
                {
                    "documents": ["replacement document"],
                    "ids": ["existing-row"],
                    "metadatas": [{"source_file": source}],
                },
                session=session,
                source_file=source,
            )

    assert collection.rows["existing-row"][0] == original
    assert collection.embeddings["existing-row"] == [9.0, 9.0]
    assert collection.update_calls == 0
    assert session.outputs == []


def test_managed_adapter_raw_write_before_source_identity_fails_closed(tmp_path):
    class EarlyRawWriteAdapter(_ReceiptAdapter):
        name = "early-raw-write"

        def ingest(self, *, source, palace):
            palace.drawer_collection.upsert(
                documents=["must not persist"],
                ids=["unreceipted"],
                metadatas=[{"source_file": source.uri}],
            )
            yield SourceItemMetadata(
                source_file=source.uri,
                version="v1",
                content_hash=sha256_bytes(b"never reached"),
            )

    palace, store, _ = _store_and_run(tmp_path)
    collection = _MemoryCollection()
    context = PalaceContext(
        drawer_collection=collection,
        knowledge_graph=_FakeKnowledgeGraph(),
        palace_path=str(palace),
    )

    with pytest.raises(ReceiptIdentityError, match="active source receipt"):
        managed_adapter_ingest(
            adapter=EarlyRawWriteAdapter(),
            source=SourceRef(uri="logical://adapter/early-write"),
            palace=context,
            receipt_store=store,
            caller="test-runner",
            config={},
        )
    assert collection.count() == 0


def test_managed_adapter_cannot_escape_to_raw_receipt_collections(tmp_path):
    class RawEscapeAdapter(_ReceiptAdapter):
        name = "raw-escape"

        def ingest(self, *, source, palace):
            yield SourceItemMetadata(
                source_file=source.uri,
                version="v1",
                content_hash=sha256_bytes(b"escape attempt"),
            )
            palace._receipt_collections()["drawers"].upsert(
                documents=["must not persist"],
                ids=["escaped-row"],
                metadatas=[{"source_file": source.uri}],
            )

    palace, store, _ = _store_and_run(tmp_path)
    collection = _MemoryCollection()
    context = PalaceContext(
        drawer_collection=collection,
        knowledge_graph=_FakeKnowledgeGraph(),
        palace_path=str(palace),
    )

    with pytest.raises(AttributeError, match="_receipt_collections"):
        managed_adapter_ingest(
            adapter=RawEscapeAdapter(),
            source=SourceRef(uri="logical://adapter/raw-escape"),
            palace=context,
            receipt_store=store,
            caller="test-runner",
            config={},
        )

    assert collection.count() == 0


def test_adapter_import_has_no_raw_context_capability_or_authority(tmp_path):
    palace, _, _ = _store_and_run(tmp_path)
    collection = _MemoryCollection()
    context = PalaceContext(
        drawer_collection=collection,
        knowledge_graph=_FakeKnowledgeGraph(),
        palace_path=str(palace),
    )

    assert not hasattr(context_capabilities_module, "_CORE_CAPABILITY_AUTHORITY")
    assert not hasattr(context_capabilities_module, "_CONTEXTS")
    assert not hasattr(context_capabilities_module, "_context_capability")
    assert not hasattr(context_capabilities_module, "_collection_proxy_capability")
    assert not hasattr(context_capabilities_module, "_graph_proxy_capability")
    assert collection not in vars(context).values()
    assert not hasattr(context.drawer_collection, "__dict__")


def test_importable_proxy_operations_do_not_offer_generic_raw_calls(tmp_path):
    palace, _, _ = _store_and_run(tmp_path)
    collection = _MemoryCollection()
    context = PalaceContext(
        drawer_collection=collection,
        knowledge_graph=_FakeKnowledgeGraph(),
        palace_path=str(palace),
    )

    with pytest.raises(RuntimeError, match="unsupported collection proxy read"):
        context_capabilities_module._collection_proxy_read(
            context.drawer_collection,
            "delete",
            {},
        )
    with pytest.raises(RuntimeError, match="unsupported collection proxy write"):
        context_capabilities_module._collection_proxy_write(
            context.drawer_collection,
            "raw_handle",
            {},
        )


def test_importable_recovery_operations_reject_store_capture_objects(tmp_path):
    class CaptureStore:
        def __init__(self):
            self.captured = None

        def reconcile_pending_rewrites(self, collections):
            self.captured = collections
            return ()

    palace, _, _ = _store_and_run(tmp_path)
    collection = _MemoryCollection()
    context = PalaceContext(
        drawer_collection=collection,
        knowledge_graph=_FakeKnowledgeGraph(),
        palace_path=str(palace),
    )
    capture = CaptureStore()

    with pytest.raises(ReceiptIdentityError, match="core ReceiptStore"):
        context_capabilities_module._reconcile_pending_rewrites(context, capture)

    assert capture.captured is None


def test_managed_adapter_purge_failure_restores_snapshot_and_fails(tmp_path):
    palace, store, _ = _store_and_run(tmp_path)
    collection = _MemoryCollection()
    context = PalaceContext(
        drawer_collection=collection,
        knowledge_graph=_FakeKnowledgeGraph(),
        palace_path=str(palace),
        adapter_name="receipt-fixture",
        adapter_version="1.0",
    )
    adapter = _ReceiptAdapter()
    source = SourceRef(uri="logical://adapter/purge-failure")

    managed_adapter_ingest(
        adapter=adapter,
        source=source,
        palace=context,
        receipt_store=store,
        caller="test-runner",
        config={"fixture": True},
    )
    source_identity = store.source_identity(source.uri)
    first = store.find_current(source_identity)
    assert first is not None
    rows_before = copy.deepcopy(collection.rows)
    writes_before = collection.upsert_calls
    collection.delete_error = RuntimeError("private adapter purge failure")

    with pytest.raises(RuntimeError, match="private adapter purge failure"):
        managed_adapter_ingest(
            adapter=adapter,
            source=source,
            palace=context,
            receipt_store=store,
            caller="test-runner",
            config={"fixture": True},
        )

    failures = _terminal_events(store, "FAIL")
    current = store.find_current(source_identity)
    assert current is not None
    assert collection.upsert_calls == writes_before
    assert collection.rows == rows_before
    assert current["receipt_id"] == first["receipt_id"]
    assert len(failures) == 1
    assert failures[0]["errors"][0]["stage"] == "adapter-ingest"
    assert failures[0]["relations"]["supersedes"]["receipt_id"] == first["receipt_id"]
    assert store.invalidations_for(first["receipt_id"]) == []


def test_managed_adapter_repairs_missing_output_instead_of_reusing_receipt(tmp_path):
    palace, store, _ = _store_and_run(tmp_path)
    collection = _MemoryCollection()
    context = PalaceContext(
        drawer_collection=collection,
        knowledge_graph=_FakeKnowledgeGraph(),
        palace_path=str(palace),
        adapter_name="receipt-fixture",
        adapter_version="1.0",
    )
    adapter = _ReceiptAdapter()
    source = SourceRef(uri="logical://adapter/repair")
    managed_adapter_ingest(
        adapter=adapter,
        source=source,
        palace=context,
        receipt_store=store,
        caller="test-runner",
        config={"fixture": True},
    )
    collection.rows.pop(next(iter(collection.rows)))
    remaining_metadata = copy.deepcopy(next(iter(collection.rows.values()))[1])
    remaining_metadata[META_OUTPUT_CONTENT_HASH] = sha256_bytes(b"stale extra")
    collection.rows["stale-extra"] = ("stale extra", remaining_metadata)
    writes_before_repair = collection.upsert_calls
    adapter.current = True

    repaired = managed_adapter_ingest(
        adapter=adapter,
        source=source,
        palace=context,
        receipt_store=store,
        caller="test-runner",
        config={"fixture": True},
    )

    current = store.find_current(store.source_identity(source.uri))
    assert repaired.sources_unchanged == 0
    assert collection.upsert_calls > writes_before_repair
    assert "stale-extra" not in collection.rows
    assert verify_receipt(current, collection, store=store).status == "represented"


def test_managed_source_adapter_requires_content_identity_before_write(tmp_path):
    class MissingIdentityAdapter(_ReceiptAdapter):
        name = "missing-identity"

        def ingest(self, *, source, palace):
            del palace
            yield SourceItemMetadata(source_file=source.uri, version="v1")
            yield DrawerRecord(content="must not write", source_file=source.uri)

    palace, store, _ = _store_and_run(tmp_path)
    collection = _MemoryCollection()
    context = PalaceContext(
        drawer_collection=collection,
        knowledge_graph=_FakeKnowledgeGraph(),
        palace_path=str(palace),
    )
    with pytest.raises(ReceiptIdentityError):
        managed_adapter_ingest(
            adapter=MissingIdentityAdapter(),
            source=SourceRef(uri="logical://missing"),
            palace=context,
            receipt_store=store,
            caller="test-runner",
            config={},
        )
    assert collection.count() == 0


def test_project_closets_are_stamped_manifested_and_verified(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    source = project / "closets.md"
    source.write_text(_long_source_text("closet provenance"), encoding="utf-8")
    _, store, run = _store_and_run(tmp_path, config={"pipeline": "closets"})
    drawers = _MemoryCollection()
    closets = _MemoryCollection()

    process_file(
        source,
        project,
        drawers,
        "project",
        [{"name": "general", "description": "general"}],
        "test-runner",
        False,
        closets_col=closets,
        receipt_store=store,
        receipt_run=run,
    )

    receipt = _current_for_path(store, source)
    output_collections = {item["collection"] for item in receipt["outputs"]["identities"]}
    assert output_collections == {"drawers", "closets"}
    assert closets.count() > 0
    assert all(
        metadata[META_RECEIPT_ID] == receipt["receipt_id"]
        and metadata[META_SOURCE_IDENTITY] == receipt["source"]["identity"]
        for _, metadata in closets.rows.values()
    )
    assert (
        verify_receipt(
            receipt,
            collections={"drawers": drawers, "closets": closets},
            store=store,
        ).status
        == "represented"
    )
    missing_closet_run = store.create_run(
        caller="test-runner",
        mode="test",
        config={"pipeline": "closets"},
    )
    with pytest.raises(ReceiptIdentityError, match="no closet collection"):
        process_file(
            source,
            project,
            drawers,
            "project",
            [{"name": "general", "description": "general"}],
            "test-runner",
            False,
            receipt_store=store,
            receipt_run=missing_closet_run,
        )
    assert _current_for_path(store, source)["receipt_id"] == receipt["receipt_id"]


def test_pre_closet_project_receipt_is_rewritten_when_closets_become_managed(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    source = project / "closet-migration.md"
    source.write_text(_long_source_text("closet migration"), encoding="utf-8")
    _, store, first_run = _store_and_run(tmp_path, config={"pipeline": "same"})
    drawers = _MemoryCollection()
    closets = _MemoryCollection()
    base = {
        "filepath": source,
        "project_path": project,
        "collection": drawers,
        "wing": "project",
        "rooms": [{"name": "general", "description": "general"}],
        "agent": "test-runner",
        "dry_run": False,
        "receipt_store": store,
    }
    process_file(receipt_run=first_run, **base)
    legacy = _current_for_path(store, source)
    assert {item["collection"] for item in legacy["outputs"]["identities"]} == {"drawers"}

    second_run = store.create_run(caller="test-runner", mode="test", config={"pipeline": "same"})
    process_file(receipt_run=second_run, closets_col=closets, **base)
    migrated = _current_for_path(store, source)
    assert migrated["disposition"] == "WRITE"
    assert {item["collection"] for item in migrated["outputs"]["identities"]} == {
        "drawers",
        "closets",
    }
    assert closets.count() > 0


def test_managed_adapter_closet_write_is_receipted(tmp_path):
    class ClosetAdapter(_ReceiptAdapter):
        name = "closet-adapter"

        def ingest(self, *, source, palace):
            yield SourceItemMetadata(
                source_file=source.uri,
                version="closet-v1",
                content_hash=sha256_bytes(b"closet source"),
            )
            palace.closet_collection.upsert(
                documents=["topic|;|drawer-1"],
                ids=["adapter-closet"],
                metadatas=[{"source_file": "wrong-alias"}],
            )

    palace, store, _ = _store_and_run(tmp_path)
    drawers = _MemoryCollection()
    closets = _MemoryCollection()
    context = PalaceContext(
        drawer_collection=drawers,
        closet_collection=closets,
        knowledge_graph=_FakeKnowledgeGraph(),
        palace_path=str(palace),
    )
    source = SourceRef(uri="logical://adapter/closet")

    result = managed_adapter_ingest(
        adapter=ClosetAdapter(),
        source=source,
        palace=context,
        receipt_store=store,
        caller="test-runner",
        config={},
    )

    receipt = store.find_current(store.source_identity(source.uri))
    assert result.drawers_written == 0
    assert receipt["outputs"]["identities"][0]["collection"] == "closets"
    assert closets.rows["adapter-closet"][1]["source_file"] == source.uri
    assert (
        verify_receipt(
            receipt,
            collections={"drawers": drawers, "closets": closets},
            store=store,
        ).status
        == "represented"
    )


def test_managed_adapter_knowledge_graph_operation_fails_closed(tmp_path):
    class GraphAdapter(_ReceiptAdapter):
        name = "graph-adapter"

        def ingest(self, *, source, palace):
            yield SourceItemMetadata(
                source_file=source.uri,
                version="graph-v1",
                content_hash=sha256_bytes(b"graph source"),
            )
            palace.knowledge_graph.add_triple("a", "links", "b")

    palace, store, _ = _store_and_run(tmp_path)
    drawers = _MemoryCollection()
    context = PalaceContext(
        drawer_collection=drawers,
        knowledge_graph=_FakeKnowledgeGraph(),
        palace_path=str(palace),
    )

    with pytest.raises(ReceiptIdentityError, match="not receipt-representable"):
        managed_adapter_ingest(
            adapter=GraphAdapter(),
            source=SourceRef(uri="logical://adapter/graph"),
            palace=context,
            receipt_store=store,
            caller="test-runner",
            config={},
        )
    assert drawers.count() == 0
    assert _terminal_events(store, "FAIL")


def test_adapter_extraction_failure_after_purge_restores_prior_rows(tmp_path):
    class FailingAdapter(_ReceiptAdapter):
        name = "rollback-adapter"

        def __init__(self):
            super().__init__()
            self.value = "before"
            self.fail_after_metadata = False

        def ingest(self, *, source, palace):
            del palace
            value = self.value
            yield SourceItemMetadata(
                source_file=source.uri,
                version=value,
                content_hash=sha256_bytes(value.encode()),
            )
            if self.fail_after_metadata:
                raise RuntimeError("adapter extraction failed")
            yield DrawerRecord(content=value, source_file=source.uri, chunk_index=0)

    palace, store, _ = _store_and_run(tmp_path)
    collection = _MemoryCollection()
    context = PalaceContext(
        drawer_collection=collection,
        knowledge_graph=_FakeKnowledgeGraph(),
        palace_path=str(palace),
    )
    source = SourceRef(uri="logical://adapter/rollback")
    adapter = FailingAdapter()
    managed_adapter_ingest(
        adapter=adapter,
        source=source,
        palace=context,
        receipt_store=store,
        caller="test-runner",
        config={},
    )
    first = store.find_current(store.source_identity(source.uri))
    rows_before = copy.deepcopy(collection.rows)

    adapter.value = "after"
    adapter.fail_after_metadata = True
    with pytest.raises(RuntimeError, match="extraction failed"):
        managed_adapter_ingest(
            adapter=adapter,
            source=source,
            palace=context,
            receipt_store=store,
            caller="test-runner",
            config={},
        )

    assert collection.rows == rows_before
    assert (
        store.find_current(store.source_identity(source.uri))["receipt_id"] == first["receipt_id"]
    )
    assert store.invalidations_for(first["receipt_id"]) == []


def test_project_closet_failure_rolls_back_drawers_and_closets(tmp_path):
    class FailOnceCollection(_MemoryCollection):
        def __init__(self):
            super().__init__()
            self.fail_next_upsert = False

        def upsert(self, **kwargs):
            if self.fail_next_upsert:
                self.fail_next_upsert = False
                raise RuntimeError("closet write failed once")
            return super().upsert(**kwargs)

    project = tmp_path / "project"
    project.mkdir()
    source = project / "rollback.md"
    source.write_text(_long_source_text("before"), encoding="utf-8")
    _, store, first_run = _store_and_run(tmp_path, config={"pipeline": "same"})
    drawers = _MemoryCollection()
    closets = FailOnceCollection()
    kwargs = {
        "filepath": source,
        "project_path": project,
        "collection": drawers,
        "closets_col": closets,
        "wing": "project",
        "rooms": [{"name": "general", "description": "general"}],
        "agent": "test-runner",
        "dry_run": False,
        "receipt_store": store,
    }
    process_file(receipt_run=first_run, **kwargs)
    first = _current_for_path(store, source)
    drawers_before = copy.deepcopy(drawers.rows)
    closets_before = copy.deepcopy(closets.rows)

    source.write_text(_long_source_text("after"), encoding="utf-8")
    closets.fail_next_upsert = True
    second_run = store.create_run(caller="test-runner", mode="test", config={"pipeline": "same"})
    with pytest.raises(RuntimeError, match="closet write failed once"):
        process_file(receipt_run=second_run, **kwargs)

    assert drawers.rows == drawers_before
    assert closets.rows == closets_before
    assert _current_for_path(store, source)["receipt_id"] == first["receipt_id"]
    assert store.invalidations_for(first["receipt_id"]) == []

    retry_run = store.create_run(caller="test-runner", mode="test", config={"pipeline": "same"})
    process_file(receipt_run=retry_run, **kwargs)
    retried = _current_for_path(store, source)
    assert retried["source"]["content_hash"] == sha256_bytes(source.read_bytes())
    assert (
        verify_receipt(
            retried,
            collections={"drawers": drawers, "closets": closets},
            store=store,
        ).status
        == "represented"
    )


def test_complete_journal_failure_rolls_back_replacement(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    source = project / "complete-failure.md"
    source.write_text(_long_source_text("before"), encoding="utf-8")
    _, store, first_run = _store_and_run(tmp_path, config={"pipeline": "same"})
    collection = _MemoryCollection()
    kwargs = {
        "filepath": source,
        "project_path": project,
        "collection": collection,
        "wing": "project",
        "rooms": [{"name": "general", "description": "general"}],
        "agent": "test-runner",
        "dry_run": False,
        "receipt_store": store,
    }
    process_file(receipt_run=first_run, **kwargs)
    first = _current_for_path(store, source)
    rows_before = copy.deepcopy(collection.rows)
    source.write_text(_long_source_text("after"), encoding="utf-8")
    after_hash = sha256_bytes(source.read_bytes())
    original_write_event = store.write_event

    def fail_new_complete(event):
        if event["state"] == "COMPLETE" and event["source"]["content_hash"] == after_hash:
            raise OSError("complete journal publication failed")
        return original_write_event(event)

    monkeypatch.setattr(store, "write_event", fail_new_complete)
    second_run = store.create_run(caller="test-runner", mode="test", config={"pipeline": "same"})
    with pytest.raises(OSError, match="complete journal publication failed"):
        process_file(receipt_run=second_run, **kwargs)

    assert collection.rows == rows_before
    assert _current_for_path(store, source)["receipt_id"] == first["receipt_id"]
    assert store.invalidations_for(first["receipt_id"]) == []


def test_complete_fails_closed_after_post_publication_sync_error(tmp_path, monkeypatch):
    _, store, run = _store_and_run(tmp_path)
    source_hash = sha256_bytes(b"post-publication sync")
    session = store.begin_source(
        run=run,
        source_locator="logical://receipt/post-publication-sync",
        source_content_hash=source_hash,
        source_version_hash=source_hash,
        source_size_bytes=21,
        adapter_name="fixture",
        adapter_version="1",
    )
    session.set_expected(drawers=0)
    previous_event_path = session.last_event_path

    def fail_directory_sync(_path):
        raise OSError("directory sync failed after link")

    monkeypatch.setattr(write_receipts_module, "_sync_published_parent", fail_directory_sync)
    with pytest.raises(ReceiptDurabilityError, match="durable publication failed"):
        session.complete()

    assert session.state != "COMPLETE"
    assert session.last_event_path == previous_event_path


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing", "missing"),
        ("truncated", "exactly 32 bytes"),
        ("oversized", "exactly 32 bytes"),
        ("replaced", "recorded fingerprint"),
    ],
)
def test_receipt_identity_key_lifecycle_fails_closed(tmp_path, mutation, message):
    palace, store, run = _store_and_run(tmp_path)
    source_hash = sha256_bytes(b"key lifecycle")
    session = store.begin_source(
        run=run,
        source_locator="logical://key/lifecycle",
        source_content_hash=source_hash,
        source_version_hash=source_hash,
        source_size_bytes=13,
        adapter_name="fixture",
        adapter_version="1",
    )
    session.set_expected(drawers=0)
    session.complete()
    if mutation == "missing":
        store.identity_key_path.unlink()
    elif mutation == "truncated":
        store.identity_key_path.write_bytes(b"x" * 31)
    elif mutation == "oversized":
        store.identity_key_path.write_bytes(b"x" * 33)
    else:
        store.identity_key_path.write_bytes(os.urandom(32))

    with pytest.raises(ReceiptIdentityError, match=message):
        ReceiptStore(palace)


def test_legacy_key_metadata_is_backfilled_without_rotating_identity(tmp_path):
    palace, store, run = _store_and_run(tmp_path)
    source_hash = sha256_bytes(b"legacy key")
    session = store.begin_source(
        run=run,
        source_locator="logical://key/legacy",
        source_content_hash=source_hash,
        source_version_hash=source_hash,
        source_size_bytes=10,
        adapter_name="fixture",
        adapter_version="1",
    )
    session.set_expected(drawers=0)
    session.complete()
    identity_before = store.source_identity("logical://key/legacy")
    store.identity_metadata_path.unlink()

    reopened = ReceiptStore(palace)
    assert reopened.identity_metadata_path.exists()
    assert reopened.source_identity("logical://key/legacy") == identity_before


def test_immutable_event_conflict_never_overwrites_existing_bytes(tmp_path):
    _, store, run = _store_and_run(tmp_path)
    source_hash = sha256_bytes(b"immutable")
    session = store.begin_source(
        run=run,
        source_locator="logical://immutable",
        source_content_hash=source_hash,
        source_version_hash=source_hash,
        source_size_bytes=9,
        adapter_name="fixture",
        adapter_version="1",
    )
    event_path = session.last_event_path
    bytes_before = event_path.read_bytes()
    conflicting = copy.deepcopy(session.last_event)
    conflicting["stage"] = "tampered"

    with pytest.raises(ReceiptConflictError):
        store.write_event(conflicting)
    assert event_path.read_bytes() == bytes_before


def test_find_current_falls_back_to_legacy_events_after_failed_start_partition(tmp_path):
    _, store, first_run = _store_and_run(tmp_path)
    first_hash = sha256_bytes(b"legacy complete")
    first = store.begin_source(
        run=first_run,
        source_locator="logical://legacy-layout",
        source_content_hash=first_hash,
        source_version_hash=first_hash,
        source_size_bytes=15,
        adapter_name="fixture",
        adapter_version="1",
    )
    first.set_expected(drawers=0)
    first_complete = first.complete()
    legacy_dir = store.events_dir / "legacy-layout"
    legacy_dir.mkdir()
    legacy_path = legacy_dir / first.last_event_path.name
    first.last_event_path.replace(legacy_path)
    for index_path in store.sources_dir.glob("*.json"):
        index_path.unlink()

    second_run = store.create_run(caller="test-runner", mode="test", config={"fixture": 1})
    second_hash = sha256_bytes(b"failed start")
    store.begin_source(
        run=second_run,
        source_locator="logical://legacy-layout",
        source_content_hash=second_hash,
        source_version_hash=second_hash,
        source_size_bytes=12,
        adapter_name="fixture",
        adapter_version="1",
    )

    current = store.find_current(
        first_complete["source"]["identity"],
        content_hash=first_hash,
        version_digest=first_hash,
        config_digest=first_run.config_digest,
    )
    assert current["receipt_id"] == first_complete["receipt_id"]


def test_find_current_fails_closed_on_unreadable_legacy_complete(tmp_path):
    _, store, run = _store_and_run(tmp_path)
    source_hash = sha256_bytes(b"legacy corrupt")
    session = store.begin_source(
        run=run,
        source_locator="logical://legacy-corrupt",
        source_content_hash=source_hash,
        source_version_hash=source_hash,
        source_size_bytes=14,
        adapter_name="fixture",
        adapter_version="1",
    )
    session.set_expected(drawers=0)
    complete = session.complete()
    legacy_dir = store.events_dir / "legacy-corrupt"
    legacy_dir.mkdir()
    legacy_path = legacy_dir / session.last_event_path.name
    session.last_event_path.replace(legacy_path)
    for index_path in store.sources_dir.glob("*.json"):
        index_path.unlink()
    legacy_path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(ReceiptConflictError, match="legacy receipt journal is unreadable"):
        store.find_current(complete["source"]["identity"])


def test_receiptless_raw_local_alias_is_purged_safely(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    nested = project / "nested"
    nested.mkdir()
    source = project / "alias.md"
    source.write_text(_long_source_text("alias"), encoding="utf-8")
    raw_alias = nested / ".." / source.name
    _, store, run = _store_and_run(tmp_path)
    collection = _MemoryCollection()
    collection.rows["legacy-alias"] = (
        "legacy",
        {"source_file": str(raw_alias), "wing": "project", "room": "general"},
    )

    process_file(
        raw_alias,
        project,
        collection,
        "project",
        [{"name": "general", "description": "general"}],
        "test-runner",
        False,
        receipt_store=store,
        receipt_run=run,
    )
    assert "legacy-alias" not in collection.rows


def test_purge_rejects_alias_that_resolves_to_another_path(tmp_path):
    _, store, run = _store_and_run(tmp_path)
    source = tmp_path / "source.md"
    other = tmp_path / "other.md"
    source.write_text("source", encoding="utf-8")
    other.write_text("other", encoding="utf-8")
    collection = _MemoryCollection()
    collection.rows["other-row"] = ("other", {"source_file": str(other)})
    canonical = canonical_source_locator(str(source), local_path=True)
    source_hash = sha256_bytes(b"source")
    session = store.begin_source(
        run=run,
        source_locator=canonical,
        source_content_hash=source_hash,
        source_version_hash=source_hash,
        source_size_bytes=6,
        adapter_name="alias-fixture",
        adapter_version="1",
        local_path=True,
    )
    snapshot = snapshot_managed_source_rows(
        collection,
        source_file=canonical,
        source_identity=session.source["identity"],
        local_path=True,
    )
    recovery_path = store.prepare_rewrite_recovery(
        session=session,
        snapshots={"drawers": snapshot},
        source_file=canonical,
        local_path=True,
    )

    with pytest.raises(ReceiptIdentityError, match="different local path"):
        purge_managed_source_snapshot(
            collection,
            snapshot,
            recovery_path=recovery_path,
            collection_name="drawers",
            source_file=str(source),
            source_identity=session.source["identity"],
            local_path=True,
            source_aliases=(str(other),),
        )
    assert collection.delete_calls == 0
    assert "other-row" in collection.rows


def test_unexpected_normalization_exception_fails_without_purge(tmp_path, monkeypatch):
    source = tmp_path / "normalize-runtime.txt"
    source.write_text(_long_source_text("before"), encoding="utf-8")
    config = {"pipeline": "conversations", "extract_mode": "exchange"}
    _, store, first_run = _store_and_run(
        tmp_path,
        mode="conversations:exchange",
        config=config,
    )
    collection = _MemoryCollection()
    kwargs = {
        "filepath": source,
        "collection": collection,
        "wing": "conversations",
        "agent": "test-runner",
        "extract_mode": "exchange",
        "dry_run": False,
        "index": 1,
        "total_files": 1,
        "receipt_store": store,
    }
    _process_conversation_file(receipt_run=first_run, **kwargs)
    first = _current_for_path(store, source)
    rows_before = copy.deepcopy(collection.rows)
    deletes_before = collection.delete_calls
    source.write_text(_long_source_text("after"), encoding="utf-8")
    monkeypatch.setattr(
        convo_miner_module,
        "normalize",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("normalizer crashed")),
    )
    second_run = store.create_run(
        caller="test-runner",
        mode="conversations:exchange",
        config=config,
    )

    with pytest.raises(RuntimeError, match="normalizer crashed"):
        _process_conversation_file(receipt_run=second_run, **kwargs)
    assert collection.rows == rows_before
    assert collection.delete_calls == deletes_before
    assert _current_for_path(store, source)["receipt_id"] == first["receipt_id"]
    failure = _terminal_events(store, "FAIL")[-1]
    assert failure["errors"][0]["stage"] == "normalize"
    assert failure["disposition"] == "WRITE"


def test_managed_adapter_lock_covers_source_read_through_write(tmp_path, monkeypatch):
    class ConcurrentAdapter(_ReceiptAdapter):
        name = "concurrent-adapter"

        def __init__(self):
            super().__init__()
            self.value = "older"
            self.calls = 0
            self.calls_lock = threading.Lock()
            self.first_read = threading.Event()
            self.release_first = threading.Event()

        def ingest(self, *, source, palace):
            del palace
            value = self.value
            with self.calls_lock:
                self.calls += 1
                call_number = self.calls
            if call_number == 1:
                self.first_read.set()
                assert self.release_first.wait(timeout=_THREAD_TEST_TIMEOUT_SECONDS)
            yield SourceItemMetadata(
                source_file=source.uri,
                version=value,
                content_hash=sha256_bytes(value.encode()),
            )
            yield DrawerRecord(content=value, source_file=source.uri, chunk_index=0)

    source_lock = threading.Lock()

    @contextmanager
    def thread_lock(_key):
        with source_lock:
            yield

    @contextmanager
    def no_palace_lock(_palace_path):
        yield

    monkeypatch.setattr(provenance_module, "mine_palace_lock", no_palace_lock)
    monkeypatch.setattr(provenance_module, "mine_lock", thread_lock)
    palace, store, _ = _store_and_run(tmp_path)
    collection = _MemoryCollection()
    context = PalaceContext(
        drawer_collection=collection,
        knowledge_graph=_FakeKnowledgeGraph(),
        palace_path=str(palace),
    )
    source = SourceRef(uri="logical://adapter/concurrent")
    adapter = ConcurrentAdapter()
    errors = []

    def run_ingest():
        try:
            managed_adapter_ingest(
                adapter=adapter,
                source=source,
                palace=context,
                receipt_store=store,
                caller="test-runner",
                config={},
            )
        except BaseException as exc:
            errors.append(exc)

    older = threading.Thread(target=run_ingest)
    older.start()
    assert adapter.first_read.wait(timeout=_THREAD_TEST_TIMEOUT_SECONDS)
    adapter.value = "newer"
    newer = threading.Thread(target=run_ingest)
    newer.start()
    time.sleep(0.1)
    assert adapter.calls == 1
    adapter.release_first.set()
    older.join(timeout=_THREAD_TEST_TIMEOUT_SECONDS)
    newer.join(timeout=_THREAD_TEST_TIMEOUT_SECONDS)

    assert not older.is_alive() and not newer.is_alive()
    assert errors == []
    assert {document for document, _ in collection.rows.values()} == {"newer"}
    current = store.find_current(store.source_identity(source.uri))
    assert current["source"]["content_hash"] == sha256_bytes(b"newer")


def test_managed_adapter_palace_lock_serializes_cross_source_writes(tmp_path, monkeypatch):
    class CrossSourceAdapter(_ReceiptAdapter):
        name = "cross-source-concurrency"

        def __init__(self):
            super().__init__()
            self.calls = 0
            self.calls_lock = threading.Lock()
            self.first_read = threading.Event()
            self.release_first = threading.Event()

        def ingest(self, *, source, palace):
            del palace
            with self.calls_lock:
                self.calls += 1
                call_number = self.calls
            if call_number == 1:
                self.first_read.set()
                assert self.release_first.wait(timeout=_THREAD_TEST_TIMEOUT_SECONDS)
            content = f"content for {source.uri}"
            yield SourceItemMetadata(
                source_file=source.uri,
                version=content,
                content_hash=sha256_bytes(content.encode()),
            )
            yield DrawerRecord(content=content, source_file=source.uri, chunk_index=0)

    palace_lock = threading.Lock()
    lock_state = {"active": 0, "maximum": 0}

    @contextmanager
    def blocking_palace_lock(_palace_path):
        with palace_lock:
            lock_state["active"] += 1
            lock_state["maximum"] = max(lock_state["maximum"], lock_state["active"])
            try:
                yield
            finally:
                lock_state["active"] -= 1

    @contextmanager
    def no_source_lock(_key):
        yield

    monkeypatch.setattr(provenance_module, "mine_palace_lock", blocking_palace_lock)
    monkeypatch.setattr(provenance_module, "mine_lock", no_source_lock)
    palace, store, _ = _store_and_run(tmp_path)
    collection = _MemoryCollection()
    contexts = [
        PalaceContext(
            drawer_collection=collection,
            knowledge_graph=_FakeKnowledgeGraph(),
            palace_path=str(palace),
        )
        for _ in range(2)
    ]
    sources = [
        SourceRef(uri="logical://adapter/cross-source-one"),
        SourceRef(uri="logical://adapter/cross-source-two"),
    ]
    adapter = CrossSourceAdapter()
    errors = []

    def run_ingest(index):
        try:
            managed_adapter_ingest(
                adapter=adapter,
                source=sources[index],
                palace=contexts[index],
                receipt_store=store,
                caller="test-runner",
                config={},
            )
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=run_ingest, args=(0,))
    second = threading.Thread(target=run_ingest, args=(1,))
    first.start()
    assert adapter.first_read.wait(timeout=_THREAD_TEST_TIMEOUT_SECONDS)
    second.start()
    time.sleep(0.1)
    assert adapter.calls == 1
    adapter.release_first.set()
    first.join(timeout=_THREAD_TEST_TIMEOUT_SECONDS)
    second.join(timeout=_THREAD_TEST_TIMEOUT_SECONDS)

    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    assert adapter.calls == 2
    assert lock_state["maximum"] == 1
    assert {document for document, _ in collection.rows.values()} == {
        "content for logical://adapter/cross-source-one",
        "content for logical://adapter/cross-source-two",
    }


def test_embedding_readback_matches_across_float32_round_trip():
    match = write_receipts_module._embedding_matches_stored
    float64_vector = (0.12345678901234567, -0.9876543210987654, 1.5e-3)
    import struct as _struct

    float32_round_trip = tuple(
        _struct.unpack("<f", _struct.pack("<f", value))[0] for value in float64_vector
    )
    assert float32_round_trip != float64_vector
    assert match(float32_round_trip, float64_vector)
    assert match(float64_vector, float64_vector)
    assert match(None, None)
    assert not match(None, float64_vector)
    assert not match(float64_vector, None)
    assert not match(float32_round_trip[:2], float64_vector)
    perturbed = (float64_vector[0] + 1e-3,) + float64_vector[1:]
    assert not match(perturbed, float64_vector)
    assert not match(("not-a-number", 0.0, 0.0), float64_vector)
    # Re-embedding identical text can shift stored float32 components by one
    # ULP (observed in production recovery 8787515f); that noise must match.
    one_ulp_pairs = (
        (-0.05243675038218498, -0.052436746656894684),
        (-0.116607666015625, -0.1166076585650444),
        (0.0036799798253923655, 0.003679979592561722),
    )
    assert match(
        tuple(a for a, _ in one_ulp_pairs),
        tuple(b for _, b in one_ulp_pairs),
    )


def test_row_matches_snapshot_tolerates_float32_quantization_noise():
    import struct as _struct

    exact = (0.111111111111111, 0.222222222222222)
    quantized = tuple(_struct.unpack("<f", _struct.pack("<f", value))[0] for value in exact)
    snapshot = write_receipts_module.ManagedSourceSnapshot(
        ids=("row-1",),
        documents=("doc",),
        metadatas=({"k": "v"},),
        embeddings=((exact),),
    )
    assert write_receipts_module._row_matches_snapshot(("doc", {"k": "v"}, quantized), snapshot, 0)
    assert not write_receipts_module._row_matches_snapshot(
        ("doc", {"k": "v"}, (exact[0] + 0.5, exact[1])), snapshot, 0
    )
    assert not write_receipts_module._row_matches_snapshot(
        ("other", {"k": "v"}, quantized), snapshot, 0
    )
