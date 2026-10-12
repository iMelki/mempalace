"""Saved recovery must survive later writers and metadata-preserving source reverts."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from mempalace.mine_progress import MineProgressJournal, load_source_manifest
from mempalace.mine_refresh import PrefixRefreshJournal, _snapshot_item
from mempalace.miner import mine
from mempalace.write_receipts import ReceiptStore
from test_mine_refresh import (
    _assert_recovered_fixture,
    _hard_crash_refresh,
    _interrupted,
    _interrupt_recovery_publication,
    _resume,
)


def _hard_crash_followup_prepared(fixture):
    from mempalace.native_lifecycle import release_native_sessions

    release_native_sessions()
    child = Path(__file__).parent / "fixtures" / "mine_refresh_hard_exit.py"
    result = subprocess.run(
        [sys.executable, str(child), *(str(path) for path in fixture), "--after-prepared"],
        capture_output=True,
        text=True,
        timeout=90,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 31, result.stdout + result.stderr


@pytest.mark.parametrize("first_mine", ["different-config", "unmined-edit"])
@pytest.mark.parametrize("later_mine", ["same-bytes", "further-edit", "different-config"])
@pytest.mark.parametrize("crash_point", ["represented", "prepared"])
def test_saved_recovery_accepts_later_verified_descendant(
    tmp_path, monkeypatch, first_mine, later_mine, crash_point
):
    fixture = _interrupted(tmp_path, monkeypatch, stop_index=0)
    project, palace, plan, progress = fixture
    source = project / "a.md"
    source.write_text("Pending refresh source. " * 15, encoding="utf-8")
    pending = _hard_crash_refresh(fixture)
    manifest_bytes, prefix_bytes = plan.read_bytes(), progress.read_bytes()
    mine(
        str(project),
        str(palace),
        files=[source],
        wing_override="other_fixture" if first_mine == "different-config" else None,
    )
    if first_mine == "unmined-edit":
        source.write_text("Unmined source after first successor. " * 15, encoding="utf-8")
    if crash_point == "prepared":
        _hard_crash_followup_prepared(fixture)
    else:
        _interrupt_recovery_publication(fixture, monkeypatch)
    journal = MineProgressJournal(progress, manifest=load_source_manifest(plan))
    saved = PrefixRefreshJournal(journal).state(0)
    if crash_point == "prepared":
        assert saved["pending"] is not None
        assert saved["pending"]["event_digest"] != saved["recovery_pending"]["event_digest"]
        assert saved["pending"]["previous_receipt_id"] == saved["receipt_id"]
        assert (
            saved["pending"]["item"]["content_hash"]
            == _snapshot_item(source, saved["item"], 0)["content_hash"]
        )
    else:
        assert saved["pending"] is None
    assert saved["recovery_pending"] is not None
    saved_events = [
        p.read_bytes() for p in sorted(PrefixRefreshJournal(journal).root.glob("*.json"))
    ]
    if later_mine == "further-edit":
        source.write_text("Further source after saved recovery. " * 15, encoding="utf-8")
    mine(
        str(project),
        str(palace),
        files=[source],
        wing_override="third_fixture" if later_mine == "different-config" else None,
    )
    head = ReceiptStore(palace).find_current_read_only(pending["source"]["identity"])
    assert head["relations"]["predecessor_receipt_id"] == saved["receipt_id"]
    _resume(fixture, start_index=1)
    _assert_recovered_fixture(
        fixture, head, manifest_bytes, prefix_bytes, expect_rewrite=later_mine == "different-config"
    )
    assert [p.read_bytes() for p in sorted(PrefixRefreshJournal(journal).root.glob("*.json"))][
        : len(saved_events)
    ] == saved_events
    final = PrefixRefreshJournal(journal).state(0)
    if final["recovered_head"] is not None:
        assert final["recovery_pending"] == saved["recovery_pending"]


@pytest.mark.parametrize("crash_after_recovery", [False, True])
def test_recovery_rewrites_same_stat_revert_to_prepared_bytes(
    tmp_path, monkeypatch, crash_after_recovery
):
    fixture = _interrupted(tmp_path, monkeypatch, stop_index=0)
    project, palace, plan, progress = fixture
    source = project / "a.md"
    source.write_text("Pending refresh source. " * 15, encoding="utf-8")
    prepared_bytes, prepared_stat = source.read_bytes(), source.stat()
    pending = _hard_crash_refresh(fixture)
    manifest_bytes, prefix_bytes = plan.read_bytes(), progress.read_bytes()
    source.write_text("Further edited successor. " * 15, encoding="utf-8")
    mine(str(project), str(palace), files=[source])
    head = ReceiptStore(palace).find_current_read_only(pending["source"]["identity"])
    assert head["source"]["content_hash"] != pending["source"]["content_hash"]
    source.write_bytes(prepared_bytes)
    os.utime(source, ns=(prepared_stat.st_atime_ns, prepared_stat.st_mtime_ns))
    journal = MineProgressJournal(progress, manifest=load_source_manifest(plan))
    state = PrefixRefreshJournal(journal).state(0)
    assert _snapshot_item(source, state["pending"]["item"], 0) == state["pending"]["item"]
    if crash_after_recovery:
        _interrupt_recovery_publication(fixture, monkeypatch)
    _resume(fixture, start_index=1)
    _assert_recovered_fixture(fixture, head, manifest_bytes, prefix_bytes, expect_rewrite=True)
