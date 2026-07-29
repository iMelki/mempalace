import argparse
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

import chromadb
import pytest

from mempalace.cli import cmd_mine
from mempalace.mine_progress import (
    MineManifestDrift,
    MinePlanJournal,
    MinePlanError,
    MineProgressError,
    MineProgressJournal,
    build_source_manifest,
    load_source_manifest,
    publish_source_manifest,
)
from mempalace.miner import (
    MINE_LOCK_CONFLICT_EXIT_CODE,
    MineProgressJournalError,
    mine,
)
from mempalace.palace import MineAlreadyRunning


def _write_project(root: Path, names=("b.md", "a.md", "nested/c.md")) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "mempalace.yaml").write_text(
        "wing: progress_test\nrooms:\n  - name: general\n    description: General\n",
        encoding="utf-8",
    )
    for index, name in enumerate(names):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"source {index} deterministic content " * 12,
            encoding="utf-8",
        )


def _contract() -> dict:
    return {
        "mode": "project",
        "parser": "filesystem",
        "receipt_config_digest": "sha256:" + "1" * 64,
        "miner_revision": {
            "progress_contract": "1",
            "module_sha256": "sha256:" + "2" * 64,
        },
    }


def _fake_receipt(disposition="WRITE") -> dict:
    return {
        "receipt_id": str(uuid.uuid4()),
        "state": "COMPLETE",
        "disposition": disposition,
    }


def _hmac_identity(char="a") -> str:
    return "hmac-sha256:" + char * 64


def _mine_args(**overrides):
    values = {
        "dir": "/secret/source",
        "palace": "/secret/palace",
        "mode": "projects",
        "wing": None,
        "agent": "mempalace",
        "limit": 0,
        "dry_run": False,
        "no_gitignore": False,
        "include_ignored": [],
        "extract": "exchange",
        "plan_out": None,
        "plan_progress_jsonl": None,
        "manifest": None,
        "start_index": None,
        "progress_jsonl": None,
        "redetect_origin": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_resumable_plan_continues_at_exact_next_file(tmp_path, monkeypatch):
    project = tmp_path / "project"
    _write_project(project, names=("c.md", "a.md", "b.md"))
    plan = tmp_path / "plan.json"
    progress = tmp_path / "plan-progress.jsonl"

    from mempalace import miner as miner_module

    original_descriptor = miner_module.source_descriptor
    first_calls = []

    def interrupt_on_second(**kwargs):
        first_calls.append(kwargs["relative_path"])
        if len(first_calls) == 2:
            raise RuntimeError("fixture-plan-interrupt")
        return original_descriptor(**kwargs)

    with monkeypatch.context() as context:
        context.setattr(miner_module, "source_descriptor", interrupt_on_second)
        with pytest.raises(RuntimeError, match="fixture-plan-interrupt"):
            mine(
                str(project),
                str(tmp_path / "unused-palace"),
                dry_run=True,
                plan_out=str(plan),
                plan_progress_jsonl=str(progress),
            )

    described_before = [
        record["relative_path"]
        for record in (
            json.loads(line) for line in progress.read_text(encoding="utf-8").splitlines()
        )
        if record["event"] == "file-described"
    ]
    assert described_before == first_calls[:1]

    resumed_calls = []

    def count_resumed(**kwargs):
        resumed_calls.append(kwargs["relative_path"])
        return original_descriptor(**kwargs)

    with monkeypatch.context() as context:
        context.setattr(miner_module, "source_descriptor", count_resumed)
        context.setattr(
            miner_module,
            "_discover_plan_directory",
            lambda **_kwargs: pytest.fail("a committed directory listing must not be rediscovered"),
        )
        mine(
            str(project),
            str(tmp_path / "unused-palace"),
            dry_run=True,
            plan_out=str(plan),
            plan_progress_jsonl=str(progress),
        )

    manifest = load_source_manifest(plan)
    assert described_before[0] not in resumed_calls
    assert len(resumed_calls) == manifest["source_count"] - 1
    assert (
        len(
            [
                record
                for record in (
                    json.loads(line) for line in progress.read_text(encoding="utf-8").splitlines()
                )
                if record["event"] == "file-described"
            ]
        )
        == manifest["source_count"]
    )


def test_plan_journal_recovers_only_torn_uncommitted_tail(tmp_path):
    path = tmp_path / "plan-progress.jsonl"
    identity = {"schema": "fixture/v1", "scope": "unit"}
    journal = MinePlanJournal(path, identity=identity)
    journal.append(
        "directory-discovered",
        {"relative_dir": "", "child_dirs": [], "files": []},
    )
    with path.open("ab") as handle:
        handle.write(b'{"partial":')
        handle.flush()
        os.fsync(handle.fileno())

    restarted = MinePlanJournal(path, identity=identity)
    assert len(restarted.records()) == 1
    assert restarted.recovered_torn_bytes == len(b'{"partial":')
    assert path.read_bytes().endswith(b"\n")


def test_plan_journal_rejects_semantically_divergent_complete_record(tmp_path):
    from mempalace import mine_progress as progress_module

    path = tmp_path / "plan-progress.jsonl"
    identity = {"schema": "fixture/v1", "scope": "semantic"}
    journal = MinePlanJournal(path, identity=identity)
    journal.append(
        "directory-discovered",
        {"relative_dir": "", "child_dirs": [], "files": ["a.md"]},
    )
    journal.append(
        "file-described",
        {
            "relative_dir": "",
            "relative_path": "a.md",
            "descriptor": {
                "relative_path": "a.md",
                "normalized_path": "a.md",
                "size_bytes": 1,
                "mtime_ns": 1,
                "content_hash": "sha256:" + "a" * 64,
            },
        },
    )
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    records[1]["descriptor"]["normalized_path"] = "other.md"
    records[1].pop("record_digest")
    records[1]["record_digest"] = progress_module._digest(records[1])
    path.write_text(
        "\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(MinePlanError, match="does not match its file cursor"):
        MinePlanJournal(path, identity=identity).records()


def _snapshot_outputs(palace_path: Path) -> dict:
    client = chromadb.PersistentClient(path=str(palace_path))
    drawers = client.get_collection("mempalace_drawers").get(include=["documents"])
    closets = client.get_collection("mempalace_closets").get(include=["documents"])
    return {
        "drawers": sorted(zip(drawers["ids"], drawers["documents"])),
        "closets": sorted(zip(closets["ids"], closets["documents"])),
    }


def test_manifest_order_and_identity_are_deterministic(tmp_path):
    project = tmp_path / "project"
    _write_project(project)
    paths = [project / "b.md", project / "nested/c.md", project / "a.md"]

    first = build_source_manifest(
        project_path=project,
        files=paths,
        contract=_contract(),
    )
    second = build_source_manifest(
        project_path=project,
        files=reversed(paths),
        contract=_contract(),
    )

    assert first["manifest_digest"] == second["manifest_digest"]
    assert [item["relative_path"] for item in first["items"]] == [
        "a.md",
        "b.md",
        "nested/c.md",
    ]
    assert all(item["content_hash"].startswith("sha256:") for item in first["items"])
    assert all(isinstance(item["mtime_ns"], int) for item in first["items"])


def test_immutable_manifest_reuses_exact_plan_and_rejects_drift(tmp_path):
    project = tmp_path / "project"
    _write_project(project, names=("one.md",))
    manifest_path = tmp_path / "plan.json"
    first = build_source_manifest(
        project_path=project,
        files=[project / "one.md"],
        contract=_contract(),
    )
    published = publish_source_manifest(manifest_path, first)

    same = build_source_manifest(
        project_path=project,
        files=[project / "one.md"],
        contract=_contract(),
    )
    assert publish_source_manifest(manifest_path, same) == published

    source = project / "one.md"
    old_stat = source.stat()
    original = source.read_text(encoding="utf-8")
    source.write_text(original.replace("source", "SOURCE"), encoding="utf-8")
    os.utime(source, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    changed = build_source_manifest(
        project_path=project,
        files=[source],
        contract=_contract(),
    )
    with pytest.raises(MinePlanError, match="different content"):
        publish_source_manifest(manifest_path, changed)


def test_manifest_source_drift_fails_before_dry_processing(tmp_path):
    project = tmp_path / "project"
    _write_project(project, names=("one.md",))
    plan = tmp_path / "plan.json"
    mine(str(project), str(tmp_path / "unused-palace"), dry_run=True, plan_out=str(plan))

    source = project / "one.md"
    old_stat = source.stat()
    original = source.read_text(encoding="utf-8")
    source.write_text(original.replace("source", "SOURCE"), encoding="utf-8")
    os.utime(source, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))

    with pytest.raises(MineManifestDrift, match="source index 0"):
        mine(
            str(project),
            str(tmp_path / "unused-palace"),
            dry_run=True,
            manifest_path=str(plan),
        )


def test_progress_recovers_torn_tail_and_rejects_corrupt_divergent_records(tmp_path):
    project = tmp_path / "project"
    _write_project(project, names=("one.md", "two.md"))
    manifest = build_source_manifest(
        project_path=project,
        files=[project / "one.md", project / "two.md"],
        contract=_contract(),
    )
    progress_path = tmp_path / "progress.jsonl"
    journal = MineProgressJournal(progress_path, manifest=manifest)
    first = journal.append_verified(
        source_index=0,
        source_identity=_hmac_identity(),
        receipt=_fake_receipt(),
        represented_count=2,
    )
    assert journal.verified_prefix() == 1

    progress_path.write_bytes(progress_path.read_bytes() + b'{"partial":')
    assert journal.verified_prefix() == 1
    assert journal.recovered_torn_bytes == len(b'{"partial":')
    assert progress_path.read_bytes().endswith(b"\n")

    progress_path.write_text(
        json.dumps({**first, "manifest_digest": "sha256:" + "f" * 64}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(MineProgressError, match="different manifest"):
        journal.verified_prefix()

    progress_path.write_text(
        json.dumps({**first, "source_index": 1, "next_source_index": 2}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(MineProgressError, match="contiguous"):
        journal.verified_prefix()

    progress_path.write_text(
        json.dumps({**first, "receipt_id": str(uuid.uuid4())}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(MineProgressError, match="digest"):
        journal.verified_prefix()


def test_progress_is_sanitized_and_only_accepts_terminal_receipts(tmp_path):
    secret_name = "api-key-SHOULD-NOT-LEAK.md"
    secret_content = "TOP-SECRET-CONTENT-" + "x" * 80
    project = tmp_path / "project"
    project.mkdir()
    source = project / secret_name
    source.write_text(secret_content, encoding="utf-8")
    manifest = build_source_manifest(
        project_path=project,
        files=[source],
        contract=_contract(),
    )
    progress_path = tmp_path / "progress.jsonl"
    journal = MineProgressJournal(progress_path, manifest=manifest)

    with pytest.raises(MineProgressError, match="terminal COMPLETE"):
        journal.append_verified(
            source_index=0,
            source_identity=_hmac_identity(),
            receipt={**_fake_receipt(), "state": "RUNNING"},
            represented_count=0,
        )
    assert not progress_path.exists()

    journal.append_verified(
        source_index=0,
        source_identity=_hmac_identity(),
        receipt=_fake_receipt("ZERO_OUTPUT"),
        represented_count=0,
    )
    raw = progress_path.read_text(encoding="utf-8")
    assert secret_name not in raw
    assert secret_content not in raw
    assert str(project) not in raw
    assert '"verification_status":"represented"' in raw


def test_progress_append_reuses_a_validated_prefix_without_quadratic_rereads(tmp_path, monkeypatch):
    project = tmp_path / "project"
    _write_project(project, names=("one.md", "two.md", "three.md"))
    manifest = build_source_manifest(
        project_path=project,
        files=[project / "one.md", project / "two.md", project / "three.md"],
        contract=_contract(),
    )
    progress_path = tmp_path / "progress.jsonl"
    original_read_bytes = Path.read_bytes
    read_count = 0

    def counted_read_bytes(path):
        nonlocal read_count
        if path == progress_path:
            read_count += 1
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", counted_read_bytes)
    journal = MineProgressJournal(progress_path, manifest=manifest)
    for source_index in range(3):
        journal.append_verified(
            source_index=source_index,
            source_identity=_hmac_identity(str(source_index + 1)),
            receipt=_fake_receipt(),
            represented_count=1,
        )
    assert journal.verified_prefix() == 3
    assert read_count == 0

    restarted = MineProgressJournal(progress_path, manifest=manifest)
    assert restarted.verified_prefix() == 3
    assert restarted.verified_prefix() == 3
    assert read_count == 1


def test_mine_does_not_advance_progress_when_receipt_readback_fails(tmp_path, monkeypatch):
    project = tmp_path / "project"
    _write_project(project, names=("one.md",))
    progress = tmp_path / "progress.jsonl"
    plan = tmp_path / "plan.json"
    monkeypatch.setattr("mempalace.miner._compute_topic_tunnels_for_wing", lambda wing: 0)

    with (
        patch(
            "mempalace.miner._verify_manifest_source_receipt",
            side_effect=MineProgressJournalError("receipt readback failed"),
        ),
        pytest.raises(MineProgressJournalError, match="readback failed"),
    ):
        mine(
            str(project),
            str(tmp_path / "palace"),
            plan_out=str(plan),
            progress_jsonl=str(progress),
        )

    assert not progress.exists()


def test_restartable_console_output_omits_source_and_palace_details(tmp_path, monkeypatch, capsys):
    secret_name = "customer-token-DO-NOT-LOG.md"
    project = tmp_path / "sensitive-project"
    _write_project(project, names=(secret_name,))
    secret_text = (project / secret_name).read_text(encoding="utf-8")
    palace = tmp_path / "sensitive-palace-name"
    monkeypatch.setattr("mempalace.miner._compute_topic_tunnels_for_wing", lambda wing: 0)

    mine(
        str(project),
        str(palace),
        plan_out=str(tmp_path / "plan.json"),
        progress_jsonl=str(tmp_path / "progress.jsonl"),
    )

    output = capsys.readouterr().out
    assert secret_name not in output
    assert secret_text not in output
    assert str(palace) not in output
    assert "source-index:0" in output


def test_hard_process_exit_keeps_flushed_prefix_for_restart(tmp_path):
    project = tmp_path / "project"
    _write_project(project, names=("one.md", "two.md"))
    manifest_path = tmp_path / "plan.json"
    progress_path = tmp_path / "progress.jsonl"
    publish_source_manifest(
        manifest_path,
        build_source_manifest(
            project_path=project,
            files=[project / "one.md", project / "two.md"],
            contract=_contract(),
        ),
    )
    child_script = (
        "import os;"
        "from mempalace.mine_progress import load_source_manifest,MineProgressJournal;"
        f"m=load_source_manifest({str(manifest_path)!r});"
        f"j=MineProgressJournal({str(progress_path)!r},manifest=m);"
        "j.append_verified(source_index=0,"
        f"source_identity={_hmac_identity()!r},"
        "receipt={'receipt_id':'11111111-1111-4111-8111-111111111111',"
        "'state':'COMPLETE','disposition':'WRITE'},represented_count=1);"
        "os._exit(23)"
    )
    result = subprocess.run([sys.executable, "-c", child_script], check=False)
    assert result.returncode == 23

    restarted = MineProgressJournal(progress_path, manifest=load_source_manifest(manifest_path))
    assert restarted.verified_prefix() == 1
    restarted.append_verified(
        source_index=1,
        source_identity=_hmac_identity("b"),
        receipt=_fake_receipt(),
        represented_count=1,
    )
    assert restarted.verified_prefix() == 2


def test_cli_lock_conflict_is_distinct_nonzero_and_sanitized(capsys):
    args = _mine_args()
    with (
        patch("mempalace.cli.MempalaceConfig"),
        patch("mempalace.miner.mine", side_effect=MineAlreadyRunning("secret path")),
        pytest.raises(SystemExit) as exc_info,
    ):
        cmd_mine(args)

    assert exc_info.value.code == MINE_LOCK_CONFLICT_EXIT_CODE
    assert exc_info.value.code != 0
    stderr = capsys.readouterr().err
    assert "retry later" in stderr
    assert "/secret/source" not in stderr
    assert "/secret/palace" not in stderr
    assert "secret path" not in stderr


def test_interrupted_mine_resumes_exact_prefix_and_matches_baseline(tmp_path, monkeypatch):
    project = tmp_path / "project"
    _write_project(project)
    plan = tmp_path / "plan.json"
    interrupted_progress = tmp_path / "interrupted.jsonl"
    baseline_progress = tmp_path / "baseline.jsonl"
    baseline_palace = tmp_path / "baseline-palace"
    resumed_palace = tmp_path / "resumed-palace"

    monkeypatch.setattr("mempalace.miner._compute_topic_tunnels_for_wing", lambda wing: 0)
    mine(
        str(project),
        str(baseline_palace),
        plan_out=str(plan),
        progress_jsonl=str(baseline_progress),
    )
    baseline = _snapshot_outputs(baseline_palace)

    original_append = MineProgressJournal.append_verified

    def append_then_exit(self, **kwargs):
        result = original_append(self, **kwargs)
        if kwargs["source_index"] == 0:
            raise SystemExit(23)
        return result

    with monkeypatch.context() as context:
        context.setattr(MineProgressJournal, "append_verified", append_then_exit)
        with pytest.raises(SystemExit, match="23"):
            mine(
                str(project),
                str(resumed_palace),
                manifest_path=str(plan),
                progress_jsonl=str(interrupted_progress),
            )

    manifest = load_source_manifest(plan)
    assert MineProgressJournal(interrupted_progress, manifest=manifest).verified_prefix() == 1

    mine(
        str(project),
        str(resumed_palace),
        manifest_path=str(plan),
        progress_jsonl=str(interrupted_progress),
    )
    assert MineProgressJournal(interrupted_progress, manifest=manifest).verified_prefix() == 3
    assert _snapshot_outputs(resumed_palace) == baseline

    before = _snapshot_outputs(resumed_palace)
    mine(
        str(project),
        str(resumed_palace),
        manifest_path=str(plan),
        progress_jsonl=str(interrupted_progress),
    )
    assert _snapshot_outputs(resumed_palace) == before

    with pytest.raises(MineProgressJournalError, match="different palace"):
        mine(
            str(project),
            str(tmp_path / "wrong-palace"),
            manifest_path=str(plan),
            progress_jsonl=str(interrupted_progress),
        )

    completed_source = project / "a.md"
    old_stat = completed_source.stat()
    original = completed_source.read_text(encoding="utf-8")
    completed_source.write_text(original.replace("source", "SOURCE"), encoding="utf-8")
    os.utime(completed_source, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    with pytest.raises(MineManifestDrift, match="source index 0"):
        mine(
            str(project),
            str(resumed_palace),
            manifest_path=str(plan),
            progress_jsonl=str(interrupted_progress),
        )
