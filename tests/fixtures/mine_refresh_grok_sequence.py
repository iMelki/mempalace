"""Exact five-step prepared-rewrite crash sequence, with real receipt readback."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from mempalace.mine_progress import MineProgressJournal, load_source_manifest  # noqa: E402
from mempalace.mine_refresh import PrefixRefreshJournal  # noqa: E402
from mempalace.miner import mine  # noqa: E402
from mempalace.write_receipts import ReceiptStore, sha256_bytes  # noqa: E402
from test_mine_refresh import (  # noqa: E402
    _assert_recovered_fixture,
    _hard_crash_refresh,
    _interrupted,
    _resume,
)
from test_mine_refresh_recovery_interleavings import _hard_crash_followup_prepared  # noqa: E402


def test_grok_five_step_prepared_rewrite_sequence(tmp_path, monkeypatch):
    fixture = _interrupted(tmp_path, monkeypatch, stop_index=0)
    project, palace, plan, progress = fixture
    source = project / "a.md"
    source.write_text("Prepared P source. " * 15, encoding="utf-8")
    r1 = _hard_crash_refresh(fixture)
    manifest, prefix = plan.read_bytes(), progress.read_bytes()
    source.write_text("First later mined F source. " * 15, encoding="utf-8")
    mine(str(project), str(palace), files=[source])
    store = ReceiptStore(palace)
    r2 = store.find_current_read_only(r1["source"]["identity"])
    assert r2["relations"]["predecessor_receipt_id"] == r1["receipt_id"]
    assert r2["source"]["content_hash"] != r1["source"]["content_hash"]
    source.write_text("Unmined follow-up U source. " * 15, encoding="utf-8")
    _hard_crash_followup_prepared(fixture)
    journal = MineProgressJournal(progress, manifest=load_source_manifest(plan))
    refresh = PrefixRefreshJournal(journal)
    saved = refresh.state(0)
    assert saved["receipt_id"] == r2["receipt_id"]
    assert saved["pending"]["item"]["content_hash"] == sha256_bytes(source.read_bytes())
    assert saved["recovery_pending"] == refresh.records[0]
    assert saved["pending"]["item"]["content_hash"] != r2["source"]["content_hash"]
    before = [p.read_bytes() for p in sorted(refresh.root.glob("*.json"))]
    source.write_text("Second later mined G source. " * 15, encoding="utf-8")
    mine(str(project), str(palace), files=[source])
    r3 = store.find_current_read_only(r1["source"]["identity"])
    assert r3["relations"]["predecessor_receipt_id"] == r2["receipt_id"]
    assert r3["source"]["content_hash"] != saved["pending"]["item"]["content_hash"]
    _resume(fixture, start_index=1)
    _assert_recovered_fixture(fixture, r3, manifest, prefix, expect_rewrite=False)
    assert [p.read_bytes() for p in sorted(refresh.root.glob("*.json"))][: len(before)] == before
    assert PrefixRefreshJournal(journal).state(0)["recovery_pending"] == saved["recovery_pending"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([str(Path(__file__)), "-q"]))
