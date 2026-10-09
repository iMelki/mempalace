"""Saved recovery metadata is a pin, not a substitute for receipt ancestry."""

from pathlib import Path

import pytest

from mempalace.mine_progress import MineProgressError, MineProgressJournal, load_source_manifest
from mempalace.mine_refresh import PrefixRefreshJournal
from mempalace.miner import mine
from mempalace.write_receipts import ReceiptStore
from test_mine_refresh import _interrupted, _resume


@pytest.mark.parametrize("unrelated", ["other-bytes", "other-config"])
def test_recovered_head_rechecks_original_prepared_lineage(tmp_path, monkeypatch, unrelated):
    fixture = _interrupted(tmp_path, monkeypatch, stop_index=0)
    project, palace, plan, progress = fixture
    source = project / "a.md"
    source.write_text("Prepared but never published. " * 15, encoding="utf-8")
    with monkeypatch.context() as context:
        context.setattr(
            "mempalace.miner.process_file",
            lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("before-refresh-write")),
        )
        with pytest.raises(RuntimeError, match="before-refresh-write"):
            _resume(fixture, start_index=1)
    if unrelated == "other-bytes":
        source.write_text("Unrelated successor source. " * 15, encoding="utf-8")
    mine(
        str(project),
        str(palace),
        files=[source],
        wing_override="other_fixture" if unrelated == "other-config" else None,
    )
    journal = PrefixRefreshJournal(
        MineProgressJournal(progress, manifest=load_source_manifest(plan))
    )
    state = journal.state(0)
    head = ReceiptStore(palace).find_current_read_only(
        journal.progress.records()[0]["source_identity"]
    )
    assert head["relations"]["predecessor_receipt_id"] == state["receipt_id"]
    pending = state["pending"]
    # Publish a structurally valid but semantically false recovered-head claim.
    journal.append(
        0,
        "represented",
        receipt_id=head["receipt_id"],
        represented_count=len(head["outputs"]["identities"]),
        prepared_digest=pending["event_digest"],
        recovered_head={
            "content_hash": head["source"]["content_hash"],
            "config_digest": head["producer"]["config"]["digest"],
        },
    )
    loaded = PrefixRefreshJournal(journal.progress).state(0)
    assert loaded["pending"] is None
    assert loaded["recovered_head"]["content_hash"] == head["source"]["content_hash"]
    before = [p.read_bytes() for p in sorted(journal.root.glob("*.json"))]
    prefix = progress.read_bytes()
    with pytest.raises(MineProgressError, match="no matching committed refresh ancestor"):
        _resume(fixture, start_index=1)
    assert progress.read_bytes() == prefix
    assert [p.read_bytes() for p in sorted(Path(journal.root).glob("*.json"))] == before
