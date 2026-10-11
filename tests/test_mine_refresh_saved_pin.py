"""Advancing a saved recovery still requires its exact receipt pin and live outputs."""

import json
import uuid

import pytest

from mempalace.mine_progress import MineProgressError, MineProgressJournal, _digest
from mempalace.mine_progress import load_source_manifest
from mempalace.mine_refresh import PrefixRefreshJournal
from mempalace.miner import mine
from mempalace.receipt_verifier import verify_receipt
from test_mine_refresh import (
    _hard_crash_refresh,
    _interrupted,
    _interrupt_recovery_publication,
    _resume,
)


def _saved_recovery_with_later_head(tmp_path, monkeypatch):
    fixture = _interrupted(tmp_path, monkeypatch, stop_index=0)
    project, palace, plan, progress = fixture
    source = project / "a.md"
    source.write_text("Pending refresh source. " * 15, encoding="utf-8")
    _hard_crash_refresh(fixture)
    mine(str(project), str(palace), files=[source], wing_override="other_fixture")
    _interrupt_recovery_publication(fixture, monkeypatch)
    journal = PrefixRefreshJournal(
        MineProgressJournal(progress, manifest=load_source_manifest(plan))
    )
    assert journal.state(0)["pending"] is None
    assert journal.state(0)["recovery_pending"] is not None
    mine(str(project), str(palace), files=[source])
    return fixture, journal


@pytest.mark.parametrize("corrupt", ["receipt_id", "content_hash", "config_digest"])
def test_saved_recovery_rejects_corrupted_pin(tmp_path, monkeypatch, corrupt):
    fixture, journal = _saved_recovery_with_later_head(tmp_path, monkeypatch)
    path = sorted(journal.root.glob("*.json"))[-1]
    event = json.loads(path.read_text(encoding="utf-8"))
    assert event["event"] == "represented"
    if corrupt == "receipt_id":
        event["receipt_id"] = str(uuid.uuid4())
    else:
        event["recovered_head"][corrupt] = "sha256:" + "0" * 64
    event["event_digest"] = _digest({k: v for k, v in event.items() if k != "event_digest"})
    path.write_text(json.dumps(event), encoding="utf-8")
    # The digest and journal structure are valid; only the saved receipt claim is false.
    loaded = PrefixRefreshJournal(journal.progress).state(0)
    assert loaded["receipt_id"] == event["receipt_id"]
    before = [p.read_bytes() for p in sorted(journal.root.glob("*.json"))]
    prefix = fixture[3].read_bytes()
    with pytest.raises(MineProgressError, match="saved recovery.*matching committed ancestor"):
        _resume(fixture, start_index=1)
    assert fixture[3].read_bytes() == prefix
    assert [p.read_bytes() for p in sorted(journal.root.glob("*.json"))] == before


def test_saved_recovery_rejects_later_head_with_missing_outputs(tmp_path, monkeypatch):
    fixture, journal = _saved_recovery_with_later_head(tmp_path, monkeypatch)
    before = [p.read_bytes() for p in sorted(journal.root.glob("*.json"))]
    prefix = fixture[3].read_bytes()

    class MissingRows:
        def get(self, **_kwargs):
            return {"ids": [], "documents": [], "metadatas": []}

    def missing_readback(receipt, **kwargs):
        kwargs["collections"] = {"drawers": MissingRows(), "closets": MissingRows()}
        return verify_receipt(receipt, **kwargs)

    monkeypatch.setattr("mempalace.mine_refresh_recovery.verify_receipt", missing_readback)
    with pytest.raises(MineProgressError, match="head is not represented"):
        _resume(fixture, start_index=1)
    assert fixture[3].read_bytes() == prefix
    assert [p.read_bytes() for p in sorted(journal.root.glob("*.json"))] == before
