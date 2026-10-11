"""Receipt-backed refreshes of completed sources, preserving the original plan."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from .mine_progress import (
    MineProgressError,
    _digest,
    _is_tagged_hash,
    _validate_item,
    source_descriptor,
    utc_now,
)
from .write_receipts import _atomic_write_json, canonical_source_locator


class PrefixRefreshJournal:
    """Immutable prepared/represented events bound to the original prefix."""

    def __init__(self, progress):
        self.progress = progress
        self.root = Path(str(progress.path) + ".refresh")
        self.records = []
        self.states = {}
        for path in sorted(self.root.glob("*.json")):
            try:
                event = json.loads(path.read_text(encoding="utf-8"))
                if path.name != f"{len(self.records):08d}.json":
                    raise MineProgressError("refresh journal sequence has a gap")
                self._accept(event)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise MineProgressError("refresh journal is unreadable or invalid") from exc

    def state(self, index):
        return self.states.setdefault(
            index,
            {
                "item": self.progress.items[index],
                "receipt_id": self.progress.records()[index]["receipt_id"],
                "represented_count": self.progress.records()[index]["represented_count"],
                "pending": None,
                "recovered_head": None,
                "recovery_pending": None,
            },
        )

    def _accept(self, event):
        unsigned = {k: v for k, v in event.items() if k != "event_digest"}
        if event.get("event_digest") != _digest(unsigned):
            raise MineProgressError("refresh journal event digest is invalid")
        if event.get("schema") != "mempalace-mine-prefix-refresh/v1":
            raise MineProgressError("refresh journal schema is unsupported")
        if event.get("manifest_digest") != self.progress.manifest_digest:
            raise MineProgressError("refresh journal belongs to a different manifest")
        previous = self.records[-1]["event_digest"] if self.records else None
        if event.get("sequence") != len(self.records) or event.get("previous_digest") != previous:
            raise MineProgressError("refresh journal hash chain is inconsistent")
        index = event.get("source_index")
        prefix = self.progress.records()
        if type(index) is not int or not 0 <= index < len(prefix):
            raise MineProgressError("refresh journal advances outside the completed prefix")
        if event.get("original_record_digest") != prefix[index]["record_digest"]:
            raise MineProgressError("refresh journal belongs to a different progress record")
        state = self.state(index)
        if event.get("previous_receipt_id") != state["receipt_id"]:
            raise MineProgressError("refresh journal predecessor receipt changed")
        if event.get("event") == "prepared":
            self._accept_prepared(event, index, state)
        elif event.get("event") == "represented":
            self._accept_represented(event, state)
        else:
            raise MineProgressError("refresh journal event is unsupported")
        self.records.append(event)

    def _accept_prepared(self, event, index, state):
        item = event.get("item")
        _validate_item(item, expected_index=index)
        original = self.progress.items[index]
        if any(item[k] != original[k] for k in ("relative_path", "normalized_path")):
            raise MineProgressError("refresh journal cannot change source path identity")
        state["pending"] = event

    @staticmethod
    def _accept_represented(event, state):
        pending = state["pending"]
        if pending is None and event.get("recovered_head") is not None:
            pending = state["recovery_pending"]
        if pending is None or event.get("prepared_digest") != pending["event_digest"]:
            raise MineProgressError("refresh completion has no matching prepared source")
        receipt_id = event.get("receipt_id")
        uuid.UUID(receipt_id)
        count = event.get("represented_count")
        if receipt_id == state["receipt_id"] or type(count) is not int or count < 0:
            raise MineProgressError("refresh completion requires a new represented receipt")
        item = event.get("item", pending["item"])
        _validate_item(item, expected_index=pending["source_index"])
        if any(item[k] != pending["item"][k] for k in ("relative_path", "normalized_path")):
            raise MineProgressError("refresh completion cannot change source path identity")
        recovered = event.get("recovered_head")
        if recovered is not None and (
            not isinstance(recovered, dict)
            or set(recovered) != {"content_hash", "config_digest"}
            or not all(_is_tagged_hash(value) for value in recovered.values())
        ):
            raise MineProgressError("refresh recovered head identity is invalid")
        state.update(
            item=item,
            receipt_id=receipt_id,
            represented_count=count,
            pending=None,
            recovered_head=recovered,
            recovery_pending=pending if recovered is not None else None,
        )

    def append(self, index, event, **payload):
        state = self.state(index)
        record = {
            "schema": "mempalace-mine-prefix-refresh/v1",
            "manifest_digest": self.progress.manifest_digest,
            "sequence": len(self.records),
            "previous_digest": self.records[-1]["event_digest"] if self.records else None,
            "original_record_digest": self.progress.records()[index]["record_digest"],
            "source_index": index,
            "previous_receipt_id": state["receipt_id"],
            "recorded_at": utc_now(),
            "event": event,
            **payload,
        }
        record["event_digest"] = _digest(record)
        _atomic_write_json(
            self.root / f"{len(self.records):08d}.json",
            record,
            immutable=True,
            durable=True,
            durability_anchor=self.root.parent,
        )
        self._accept(record)


def _complete_refresh(journal, index, receipt, verification):
    state = journal.state(index)
    relations = receipt.get("relations", {})
    predecessor = relations.get("predecessor_receipt_id")
    if predecessor != state["receipt_id"]:
        raise MineProgressError("refreshed receipt does not replace the recorded predecessor")
    journal.append(
        index,
        "represented",
        receipt_id=receipt["receipt_id"],
        represented_count=len(verification.represented),
        prepared_digest=state["pending"]["event_digest"],
    )


def _recover_pending(journal, index, verification_args):
    from .mine_refresh_recovery import recover_pending_head, verify_recorded_head

    state = journal.state(index)
    pending = state["pending"]
    if pending is None:
        if state["recovered_head"] is None:
            return
        current, proof = verify_recorded_head(state, verification_args)
        pending = state["recovery_pending"]
    else:
        store = verification_args["receipt_store"]
        current = store.find_current_read_only(
            store.source_identity(str(verification_args["filepath"]), local_path=True)
        )
        if current is not None and current["receipt_id"] != state["receipt_id"]:
            proof = recover_pending_head(current, state, verification_args)
    if current is not None and current["receipt_id"] != state["receipt_id"]:
        item = _snapshot_item(verification_args["filepath"], pending["item"], index)
        # Only attach an observed descriptor when it describes the verified head.
        if item["content_hash"] != current["source"]["content_hash"]:
            item = pending["item"]
        journal.append(
            index,
            "represented",
            receipt_id=current["receipt_id"],
            represented_count=len(proof.represented),
            prepared_digest=pending["event_digest"],
            item=item,
            recovered_head={
                "content_hash": current["source"]["content_hash"],
                "config_digest": current["producer"]["config"]["digest"],
            },
        )


def _snapshot(filepath, item):
    from .palace import mine_lock

    with mine_lock(os.path.normcase(canonical_source_locator(filepath, local_path=True))):
        return source_descriptor(
            path=filepath,
            relative_path=item["relative_path"],
            normalized_path=item["normalized_path"],
        )


def _snapshot_item(filepath, item, index):
    snapshot = {"index": index, **_snapshot(filepath, item)}
    snapshot["item_digest"] = _digest(snapshot)
    return snapshot


def _rewrite_refresh(journal, index, item, verification_args, *, project_path, wing, rooms, agent):
    from .miner import _verify_manifest_source_receipt, process_file

    journal.append(index, "prepared", item=item)
    process_file(
        **verification_args,
        project_path=project_path,
        expected_source=item,
        wing=wing,
        rooms=rooms,
        agent=agent,
        dry_run=False,
    )
    receipt, proof = _verify_manifest_source_receipt(item=item, **verification_args)
    _complete_refresh(journal, index, receipt, proof)


def refresh_completed_source(
    *,
    journal,
    index,
    filepath,
    project_path,
    receipt_store,
    receipt_run,
    collection,
    closets_col,
    wing,
    rooms,
    agent,
):
    """Recover a committed refresh before re-mining further source changes."""
    from .mine_refresh_recovery import verify_recorded_head

    state = journal.state(index)
    verification_args = dict(
        receipt_store=receipt_store,
        receipt_run=receipt_run,
        filepath=filepath,
        collection=collection,
        closets_col=closets_col,
    )
    _recover_pending(journal, index, verification_args)
    receipt, proof = verify_recorded_head(state, verification_args)
    if receipt["receipt_id"] != state["receipt_id"]:
        raise MineProgressError("completed source no longer names the recorded current receipt")
    if len(proof.represented) != state["represented_count"]:
        raise MineProgressError("completed source represented count changed")
    item = _snapshot_item(filepath, state["item"], index)
    config_changed = receipt["producer"]["config"]["digest"] != receipt_run.config_digest
    content_changed = item["content_hash"] != receipt["source"]["content_hash"]
    if item != state["item"] or state["pending"] is not None or config_changed or content_changed:
        _rewrite_refresh(
            journal,
            index,
            item,
            verification_args,
            project_path=project_path,
            wing=wing,
            rooms=rooms,
            agent=agent,
        )
    return state
