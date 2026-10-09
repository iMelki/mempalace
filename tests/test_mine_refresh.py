"""Resume failures from MemSys #857, using only disposable fixture stores."""

import json
import os

import pytest

from mempalace.mine_progress import MineProgressError, MineProgressJournal, load_source_manifest
from mempalace.mine_refresh import PrefixRefreshJournal
from mempalace.miner import mine
from mempalace.write_receipts import ReceiptConflictError, ReceiptStore, sha256_bytes


def _interrupted(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / "mempalace.yaml").write_text(
        "wing: refresh_fixture\nrooms:\n  - name: general\n", encoding="utf-8"
    )
    for name in ("a.md", "b.md", "c.md"):
        (project / name).write_text((f"Original {name} fixture content. " * 15), encoding="utf-8")
    plan, progress, palace = (tmp_path / name for name in ("plan.json", "progress.jsonl", "palace"))
    monkeypatch.setattr("mempalace.miner._compute_topic_tunnels_for_wing", lambda wing: 0)
    original = MineProgressJournal.append_verified

    def interrupt(self, **kwargs):
        result = original(self, **kwargs)
        if kwargs["source_index"] == 1:
            raise SystemExit(23)
        return result

    with monkeypatch.context() as context:
        context.setattr(MineProgressJournal, "append_verified", interrupt)
        with pytest.raises(SystemExit, match="23"):
            mine(str(project), str(palace), plan_out=str(plan), progress_jsonl=str(progress))
    return project, palace, plan, progress


def _resume(fixture):
    project, palace, plan, progress = fixture
    mine(
        str(project),
        str(palace),
        manifest_path=str(plan),
        progress_jsonl=str(progress),
        start_index=2,
    )


@pytest.mark.parametrize("change", ["content", "same-stat-hash", "mtime", "zero-output"])
def test_changed_completed_source_gets_new_receipt_and_reaches_later_source(
    tmp_path, monkeypatch, change
):
    fixture = _interrupted(tmp_path, monkeypatch)
    project, palace, plan, progress = fixture
    manifest_bytes, prefix_bytes = plan.read_bytes(), progress.read_bytes()
    old_record = json.loads(prefix_bytes.splitlines()[0])
    source = project / "a.md"
    stat = source.stat()
    if change == "mtime":
        os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))
    elif change == "zero-output":
        source.write_text("", encoding="utf-8")
    else:
        source.write_text(source.read_text().replace("Original", "Modified"), encoding="utf-8")
        if change == "same-stat-hash":
            os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        else:
            with source.open("a", encoding="utf-8") as handle:
                handle.write("Longer changed source.")
    _resume(fixture)
    assert plan.read_bytes() == manifest_bytes
    assert progress.read_bytes().startswith(prefix_bytes)
    journal = MineProgressJournal(progress, manifest=load_source_manifest(plan))
    assert journal.verified_prefix() == 3
    refresh = PrefixRefreshJournal(journal)
    state = refresh.state(0)
    assert state["receipt_id"] != old_record["receipt_id"]
    assert state["pending"] is None
    assert state["item"]["content_hash"] == sha256_bytes(source.read_bytes())
    current = ReceiptStore(palace).find_current_read_only(old_record["source_identity"])
    assert current["receipt_id"] == state["receipt_id"]
    assert current["relations"]["predecessor_receipt_id"] == old_record["receipt_id"]
    if change != "mtime":
        assert current["relations"]["supersedes"]["receipt_id"] == old_record["receipt_id"]
    events_before = len(refresh.records)
    mine(str(project), str(palace), manifest_path=str(plan), progress_jsonl=str(progress))
    assert len(PrefixRefreshJournal(journal).records) == events_before
    assert (
        ReceiptStore(palace).find_current_read_only(old_record["source_identity"])["receipt_id"]
        == current["receipt_id"]
    )


@pytest.mark.parametrize("changed_again", [False, True])
def test_crash_after_complete_recovers_receipt_without_replaying_write(
    tmp_path, monkeypatch, changed_again
):
    fixture = _interrupted(tmp_path, monkeypatch)
    project, palace, plan, progress = fixture
    (project / "a.md").write_text("Changed fixture source. " * 15, encoding="utf-8")
    from mempalace import mine_refresh

    with monkeypatch.context() as context:
        context.setattr(
            mine_refresh, "_complete_refresh", lambda *_args: (_ for _ in ()).throw(SystemExit(29))
        )
        with pytest.raises(SystemExit, match="29"):
            _resume(fixture)
    journal = MineProgressJournal(progress, manifest=load_source_manifest(plan))
    pending = PrefixRefreshJournal(journal).state(0)
    assert pending["pending"] is not None
    current = ReceiptStore(palace).find_current_read_only(journal.records()[0]["source_identity"])
    assert current["receipt_id"] != pending["receipt_id"]
    if changed_again:
        (project / "a.md").write_text("Another changed snapshot. " * 15, encoding="utf-8")
    from mempalace import miner

    original_process = miner.process_file
    calls = []

    def counted(**kwargs):
        calls.append(kwargs["filepath"].name)
        return original_process(**kwargs)

    monkeypatch.setattr(miner, "process_file", counted)
    _resume(fixture)
    assert calls == (["a.md", "c.md"] if changed_again else ["c.md"])
    final_id = PrefixRefreshJournal(journal).state(0)["receipt_id"]
    assert (
        (final_id != current["receipt_id"])
        if changed_again
        else (final_id == current["receipt_id"])
    )
    final = ReceiptStore(palace).find_current_read_only(journal.records()[0]["source_identity"])
    assert final["source"]["content_hash"] == sha256_bytes((project / "a.md").read_bytes())
    assert PrefixRefreshJournal(journal).state(0)["pending"] is None
    if changed_again:
        assert final["relations"]["predecessor_receipt_id"] == current["receipt_id"]


@pytest.mark.parametrize("event", ["prepared", "represented"])
def test_visible_journal_publication_error_is_reconciled_before_retry(tmp_path, monkeypatch, event):
    fixture = _interrupted(tmp_path, monkeypatch)
    project, _, plan, progress = fixture
    (project / "a.md").write_text("Changed fixture source. " * 15, encoding="utf-8")
    from mempalace import mine_refresh, miner

    original_publish = mine_refresh._atomic_write_json

    def publish_then_raise(path, payload, **kwargs):
        result = original_publish(path, payload, **kwargs)
        if payload["event"] == event:
            raise OSError("fixture-visible-publication-error")
        return result

    with monkeypatch.context() as context:
        context.setattr(mine_refresh, "_atomic_write_json", publish_then_raise)
        with pytest.raises(OSError, match="fixture-visible-publication-error"):
            _resume(fixture)
    original_process = miner.process_file
    calls = []

    def counted(**kwargs):
        calls.append(kwargs["filepath"].name)
        return original_process(**kwargs)

    monkeypatch.setattr(miner, "process_file", counted)
    _resume(fixture)
    assert calls == (["a.md", "c.md"] if event == "prepared" else ["c.md"])
    journal = MineProgressJournal(progress, manifest=load_source_manifest(plan))
    assert journal.verified_prefix() == 3
    assert PrefixRefreshJournal(journal).state(0)["pending"] is None


def test_failed_refresh_does_not_advance_then_retries_known_failed_attempt(tmp_path, monkeypatch):
    fixture = _interrupted(tmp_path, monkeypatch)
    project, _, _, progress = fixture
    (project / "a.md").write_text("Changed fixture source. " * 15, encoding="utf-8")
    before = progress.read_bytes()
    with monkeypatch.context() as context:
        context.setattr(
            "mempalace.miner.process_file",
            lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("fixture-failure")),
        )
        with pytest.raises(RuntimeError, match="fixture-failure"):
            _resume(fixture)
    assert progress.read_bytes() == before
    _resume(fixture)
    assert len(progress.read_bytes().splitlines()) == 3


def test_prepared_refresh_reverted_to_old_snapshot_still_closes_pending_attempt(
    tmp_path, monkeypatch
):
    fixture = _interrupted(tmp_path, monkeypatch)
    project, _, plan, progress = fixture
    source = project / "a.md"
    original, old_stat = source.read_bytes(), source.stat()
    source.write_text("Changed fixture source. " * 15, encoding="utf-8")
    with monkeypatch.context() as context:
        context.setattr(
            "mempalace.miner.process_file",
            lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("fixture-failure")),
        )
        with pytest.raises(RuntimeError, match="fixture-failure"):
            _resume(fixture)
    source.write_bytes(original)
    os.utime(source, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    journal = MineProgressJournal(progress, manifest=load_source_manifest(plan))
    old_id = journal.records()[0]["receipt_id"]
    _resume(fixture)
    state = PrefixRefreshJournal(journal).state(0)
    assert state["pending"] is None
    assert state["receipt_id"] != old_id
    assert state["item"] == load_source_manifest(plan)["items"][0]


def test_refresh_journal_tampering_is_rejected(tmp_path, monkeypatch):
    fixture = _interrupted(tmp_path, monkeypatch)
    project, _, _, progress = fixture
    (project / "a.md").write_text("Changed fixture source. " * 15, encoding="utf-8")
    _resume(fixture)
    event_path = next(progress.parent.glob(progress.name + ".refresh/00000000.json"))
    event = json.loads(event_path.read_text())
    event["previous_receipt_id"] = "00000000-0000-4000-8000-000000000000"
    event_path.write_text(json.dumps(event), encoding="utf-8")
    with pytest.raises(MineProgressError, match="event digest is invalid"):
        project, palace, plan, progress = fixture
        mine(str(project), str(palace), manifest_path=str(plan), progress_jsonl=str(progress))


def test_deleted_completed_source_remains_a_failure(tmp_path, monkeypatch):
    fixture = _interrupted(tmp_path, monkeypatch)
    project, _, _, progress = fixture
    # Rename inside disposable scratch, retaining the bytes rather than deleting them.
    (project / "a.md").rename(project / "retained-a.md")
    before = progress.read_bytes()
    from mempalace.mine_progress import MineManifestDrift

    with pytest.raises(MineManifestDrift, match="source changed while"):
        _resume(fixture)
    assert progress.read_bytes() == before


@pytest.mark.parametrize(
    "damage,stage,cause",
    [
        ("missing-event", "read-event", "FileNotFoundError"),
        ("invalid-json", "read-index", "JSONDecodeError"),
        ("mismatched-index", "index-event-match", "ValueError"),
    ],
)
def test_receipt_conflict_names_exact_safe_stage_and_cause(tmp_path, damage, stage, cause):
    store = ReceiptStore(tmp_path / "palace")
    run = store.create_run(caller="fixture", mode="test", config={})
    digest = sha256_bytes(b"fixture")
    session = store.begin_source(
        run=run,
        source_locator="logical://fixture",
        source_content_hash=digest,
        source_version_hash=digest,
        source_size_bytes=7,
        adapter_name="fixture",
        adapter_version="1",
    )
    session.set_expected(drawers=0)
    complete = session.complete()
    index_path = next(store.sources_dir.glob("*.json"))
    original = index_path.read_bytes()
    index = json.loads(original)
    if damage == "missing-event":
        index["event_path"] = "events/missing-complete.json"
    elif damage == "mismatched-index":
        index["source_content_hash"] = "sha256:" + "0" * 64
    index_path.write_text("{broken" if damage == "invalid-json" else json.dumps(index))
    with pytest.raises(ReceiptConflictError) as error:
        store.find_current(complete["source"]["identity"])
    assert f"stage={stage}; cause={cause}" in str(error.value)
    assert str(tmp_path) not in str(error.value)
    index_path.write_bytes(original)
    assert (
        store.find_current(complete["source"]["identity"])["receipt_id"] == complete["receipt_id"]
    )
