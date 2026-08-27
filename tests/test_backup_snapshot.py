"""Tests for the clean-client lease + staged consistent palace snapshot (#33)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mempalace import backup_snapshot
from mempalace.backup_snapshot import (
    SNAPSHOT_STATUS_AWAITING_VERIFICATION,
    PalaceSnapshotError,
    canonical_identity_digest,
    clean_client_lease,
    read_catalog,
    stage_palace_snapshot,
    verify_snapshot_receipt,
    verify_staged_snapshot,
)
from mempalace.palace import mine_palace_lock

SEGMENT_ID = "18f8562d-a1d7-4dfd-aa88-2b7fec9031bd"
CLOSET_SEGMENT_ID = "8beea981-29ec-4ac7-a678-8d8a7b981956"


def _build_palace(root: Path, *, embeddings: int = 3) -> Path:
    """Create a minimal Chroma-shaped palace: catalog + referenced segments."""

    root.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(root / "chroma.sqlite3")
    try:
        connection.execute("PRAGMA journal_mode=delete")
        connection.executescript(
            """
            CREATE TABLE collections (id TEXT PRIMARY KEY, name TEXT);
            CREATE TABLE segments (id TEXT PRIMARY KEY, collection TEXT, scope TEXT);
            CREATE TABLE embeddings (
                id INTEGER PRIMARY KEY, segment_id TEXT, embedding_id TEXT
            );
            """
        )
        for collection_id, name, vector_id, metadata_id in (
            ("col-drawers", "mempalace_drawers", SEGMENT_ID, "meta-drawers"),
            ("col-closets", "mempalace_closets", CLOSET_SEGMENT_ID, "meta-closets"),
        ):
            connection.execute("INSERT INTO collections VALUES (?, ?)", (collection_id, name))
            connection.execute(
                "INSERT INTO segments VALUES (?, ?, 'VECTOR')", (vector_id, collection_id)
            )
            connection.execute(
                "INSERT INTO segments VALUES (?, ?, 'METADATA')", (metadata_id, collection_id)
            )
            for index in range(embeddings):
                connection.execute(
                    "INSERT INTO embeddings (segment_id, embedding_id) VALUES (?, ?)",
                    (metadata_id, f"{name}-{index}"),
                )
        connection.commit()
    finally:
        connection.close()

    for segment_id in (SEGMENT_ID, CLOSET_SEGMENT_ID):
        segment = root / segment_id
        segment.mkdir()
        (segment / "header.bin").write_bytes(b"header" + segment_id.encode())
        (segment / "data_level0.bin").write_bytes(b"data" * 32)
        (segment / "link_lists.bin").write_bytes(b"links")

    (root / ".blob_seq_ids_migrated").write_text("", encoding="utf-8")
    metadata = root / ".mempalace"
    metadata.mkdir()
    (metadata / ".mempalace-directory-durable-v1").write_text("durable", encoding="utf-8")
    (metadata / "write-receipts").mkdir()
    (metadata / "write-receipts" / "receipt-1.json").write_text("{}", encoding="utf-8")
    (metadata / "repair-runs").mkdir()
    return root


@pytest.fixture()
def palace(tmp_path: Path) -> Path:
    return _build_palace(tmp_path / "palace")


@pytest.fixture(autouse=True)
def isolated_lock_dir(tmp_path_factory, monkeypatch):
    """Keep mine_palace_lock out of the developer's real ~/.mempalace/locks."""

    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


# --------------------------------------------------------------------------
# Clean-client lease
# --------------------------------------------------------------------------


def test_lease_raises_the_shared_maintenance_marker_and_lowers_it(palace, tmp_path):
    marker = tmp_path / "MemSys" / ".maintenance"
    with clean_client_lease(palace, maintenance_marker=marker) as lease:
        assert marker.exists()
        assert lease["palaceLockHeld"] is True
        assert lease["maintenanceMarkerRaisedByLease"] is True
    assert not marker.exists()


def test_lease_leaves_a_pre_existing_maintenance_marker_alone(palace, tmp_path):
    marker = tmp_path / "MemSys" / ".maintenance"
    marker.parent.mkdir(parents=True)
    marker.write_text("another operator", encoding="utf-8")
    with clean_client_lease(palace, maintenance_marker=marker) as lease:
        assert lease["maintenanceMarkerPreExisting"] is True
        assert lease["maintenanceMarkerRaisedByLease"] is False
    assert marker.read_text(encoding="utf-8") == "another operator"


def test_lease_fails_closed_when_a_writer_already_holds_the_palace_lock(palace, tmp_path):
    with mine_palace_lock(str(palace.resolve())):
        with pytest.raises(PalaceSnapshotError, match="clean-client lease unavailable"):
            with clean_client_lease(palace, use_maintenance_marker=False):
                pytest.fail("lease must not be granted while a writer holds the lock")


def test_snapshot_fails_closed_when_a_writer_holds_the_lock(palace, tmp_path):
    with mine_palace_lock(str(palace.resolve())):
        with pytest.raises(PalaceSnapshotError, match="clean-client lease unavailable"):
            stage_palace_snapshot(palace, tmp_path / "staging", use_maintenance_marker=False)
    assert not (tmp_path / "staging" / "palace").exists()


# --------------------------------------------------------------------------
# Staged consistent snapshot
# --------------------------------------------------------------------------


def test_snapshot_stages_catalog_segments_and_metadata(palace, tmp_path):
    receipt = stage_palace_snapshot(palace, tmp_path / "staging", use_maintenance_marker=False)
    staged = Path(receipt["stagedRoot"])

    assert receipt["status"] == "complete"
    assert receipt["snapshotConsistencyProven"] is True
    assert receipt["contentIdentityProven"] is True
    assert receipt["leaseProven"] is True
    assert receipt["sqliteSnapshot"]["integrityCheck"] == "ok"
    assert receipt["sqliteSnapshot"]["method"] == "sqlite-online-backup"

    assert (staged / "chroma.sqlite3").is_file()
    assert (staged / SEGMENT_ID / "header.bin").is_file()
    assert (staged / CLOSET_SEGMENT_ID / "data_level0.bin").is_file()
    assert (staged / ".blob_seq_ids_migrated").is_file()
    assert (staged / ".mempalace" / "write-receipts" / "receipt-1.json").is_file()

    # The staged catalog is byte-identical in content to the source catalog.
    assert read_catalog(staged / "chroma.sqlite3") == read_catalog(palace / "chroma.sqlite3")


def test_snapshot_excludes_stale_sidecars_quarantine_and_unreferenced_segments(palace, tmp_path):
    (palace / "chroma.sqlite3.bak_20260705").write_bytes(b"stale" * 1000)
    (palace / "palace.db").write_text("", encoding="utf-8")
    unreferenced = palace / "d7028039-f033-428f-acac-7a03532d1543"
    unreferenced.mkdir()
    (unreferenced / "header.bin").write_bytes(b"orphan")
    quarantine = palace / "38461f25-7e34-4eed-82bb-35a27b14479d.orphan-quarantine-20260702T061956"
    quarantine.mkdir()
    (quarantine / "header.bin").write_bytes(b"quarantined")

    receipt = stage_palace_snapshot(palace, tmp_path / "staging", use_maintenance_marker=False)
    staged_names = {
        entry["relativePath"].split("/", 1)[0] for entry in receipt["contentIdentity"]["files"]
    }
    assert "chroma.sqlite3.bak_20260705" not in staged_names
    assert "palace.db" not in staged_names
    assert unreferenced.name not in staged_names
    assert quarantine.name not in staged_names


def test_snapshot_refuses_a_live_sqlite_sidecar(palace, tmp_path):
    (palace / "chroma.sqlite3-wal").write_bytes(b"pending")
    with pytest.raises(PalaceSnapshotError, match="live SQLite sidecars present"):
        stage_palace_snapshot(palace, tmp_path / "staging", use_maintenance_marker=False)


def test_snapshot_refuses_unclassified_palace_metadata(palace, tmp_path):
    (palace / ".mempalace" / "surprise").mkdir()
    with pytest.raises(PalaceSnapshotError, match="unclassified .mempalace metadata"):
        stage_palace_snapshot(palace, tmp_path / "staging", use_maintenance_marker=False)


def test_snapshot_refuses_staging_inside_the_palace(palace):
    with pytest.raises(PalaceSnapshotError, match="must not live inside the palace root"):
        stage_palace_snapshot(palace, palace / "staging", use_maintenance_marker=False)


def test_snapshot_refuses_a_non_empty_staging_directory(palace, tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "leftover.txt").write_text("x", encoding="utf-8")
    with pytest.raises(PalaceSnapshotError, match="staging directory is not empty"):
        stage_palace_snapshot(palace, staging, use_maintenance_marker=False)


def test_snapshot_fails_closed_when_another_connection_commits_mid_window(
    palace, tmp_path, monkeypatch
):
    """A data_version change is direct evidence the clean-client lease leaked."""

    real_snapshot = backup_snapshot._snapshot_sqlite

    def _writing_snapshot(source, destination, *, progress=False):
        result = real_snapshot(source, destination, progress=progress)
        intruder = sqlite3.connect(str(source))
        try:
            intruder.execute(
                "INSERT INTO embeddings (segment_id, embedding_id) VALUES ('meta-drawers', 'x')"
            )
            intruder.commit()
        finally:
            intruder.close()
        return result

    monkeypatch.setattr(backup_snapshot, "_snapshot_sqlite", _writing_snapshot)
    with pytest.raises(PalaceSnapshotError, match="clean-client lease was violated"):
        stage_palace_snapshot(palace, tmp_path / "staging", use_maintenance_marker=False)


def test_snapshot_reports_the_source_journal_mode_and_page_geometry(palace, tmp_path):
    receipt = stage_palace_snapshot(palace, tmp_path / "staging", use_maintenance_marker=False)
    lease = receipt["lease"]
    assert lease["sourceJournalMode"] == "delete"
    assert lease["sourcePageCount"] > 0
    assert lease["initialDataVersion"] == lease["finalDataVersion"]
    assert lease["writerQuiescenceProven"] is True


# --------------------------------------------------------------------------
# Content-identity proof
# --------------------------------------------------------------------------


def test_receipt_hashes_every_staged_file(palace, tmp_path):
    receipt = stage_palace_snapshot(palace, tmp_path / "staging", use_maintenance_marker=False)
    staged = Path(receipt["stagedRoot"])
    on_disk = {p.relative_to(staged).as_posix() for p in staged.rglob("*") if p.is_file()}
    hashed = {entry["relativePath"] for entry in receipt["contentIdentity"]["files"]}
    assert hashed == on_disk
    assert all(
        entry["sha256"].startswith("sha256:") for entry in receipt["contentIdentity"]["files"]
    )


def test_receipt_is_written_to_disk_and_is_not_overwritten(palace, tmp_path):
    receipt = stage_palace_snapshot(palace, tmp_path / "staging", use_maintenance_marker=False)
    written = json.loads(Path(receipt["receiptPath"]).read_text(encoding="utf-8"))
    assert written["contentIdentityDigest"] == receipt["contentIdentityDigest"]
    with pytest.raises(PalaceSnapshotError, match="staging directory is not empty"):
        stage_palace_snapshot(palace, tmp_path / "staging", use_maintenance_marker=False)


def test_verification_accepts_an_untouched_snapshot(palace, tmp_path):
    receipt = stage_palace_snapshot(palace, tmp_path / "staging", use_maintenance_marker=False)
    result = verify_snapshot_receipt(receipt["receiptPath"])
    assert result["valid"] is True
    assert result["problems"] == []
    assert result["hashesVerified"] == receipt["contentIdentity"]["fileCount"]


def test_verification_detects_a_tampered_staged_file(palace, tmp_path):
    receipt = stage_palace_snapshot(palace, tmp_path / "staging", use_maintenance_marker=False)
    target = Path(receipt["stagedRoot"]) / SEGMENT_ID / "data_level0.bin"
    target.write_bytes(b"tamp" * 32)  # same length, different content
    result = verify_snapshot_receipt(receipt["receiptPath"])
    assert result["valid"] is False
    assert any(problem.startswith("staged-file-hash-mismatch") for problem in result["problems"])


def test_verification_detects_a_missing_staged_file(palace, tmp_path):
    receipt = stage_palace_snapshot(palace, tmp_path / "staging", use_maintenance_marker=False)
    (Path(receipt["stagedRoot"]) / SEGMENT_ID / "link_lists.bin").unlink()
    result = verify_snapshot_receipt(receipt["receiptPath"])
    assert result["valid"] is False
    assert any(problem.startswith("staged-file-missing") for problem in result["problems"])


def test_structural_verification_skips_hashing_but_still_binds_the_digest(palace, tmp_path):
    receipt = stage_palace_snapshot(palace, tmp_path / "staging", use_maintenance_marker=False)
    result = verify_snapshot_receipt(receipt["receiptPath"], verify_hashes=False)
    assert result["valid"] is True
    assert result["hashesVerified"] == 0

    receipt_path = Path(receipt["receiptPath"])
    tampered = json.loads(receipt_path.read_text(encoding="utf-8"))
    tampered["contentIdentity"]["files"][0]["sha256"] = "sha256:" + "0" * 64
    receipt_path.write_text(json.dumps(tampered), encoding="utf-8")
    retried = verify_snapshot_receipt(receipt_path, verify_hashes=False)
    assert retried["valid"] is False
    assert "content-identity-digest-mismatch" in retried["problems"]


def test_verification_rejects_a_receipt_with_unset_proof_flags(palace, tmp_path):
    receipt = stage_palace_snapshot(palace, tmp_path / "staging", use_maintenance_marker=False)
    receipt_path = Path(receipt["receiptPath"])
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    payload["snapshotConsistencyProven"] = False
    receipt_path.write_text(json.dumps(payload), encoding="utf-8")
    result = verify_snapshot_receipt(receipt_path, verify_hashes=False)
    assert result["valid"] is False
    assert "proof-flag-not-set:snapshotConsistencyProven" in result["problems"]


def test_identity_digest_is_canonical_and_order_independent():
    left = canonical_identity_digest({"b": 2, "a": [1, {"y": 1, "x": 0}]})
    right = canonical_identity_digest({"a": [1, {"x": 0, "y": 1}], "b": 2})
    assert left == right


# --------------------------------------------------------------------------
# Deferred verification (memsys#423 follow-up): copy now, verify later in a
# separately scheduled, separately timed lane.
# --------------------------------------------------------------------------


def test_defer_verification_copies_without_running_integrity_check_or_hashing(
    palace, tmp_path, monkeypatch
):
    integrity_calls = []
    hash_calls = []
    monkeypatch.setattr(
        backup_snapshot,
        "_sqlite_integrity_check",
        lambda *a, **k: integrity_calls.append(1) or "ok",
    )
    monkeypatch.setattr(
        backup_snapshot,
        "_hash_staged_tree",
        lambda *a, **k: hash_calls.append(1) or ([], 0),
    )

    receipt = stage_palace_snapshot(
        palace, tmp_path / "staging", use_maintenance_marker=False, verify=False
    )

    assert integrity_calls == []
    assert hash_calls == []
    assert receipt["status"] == SNAPSHOT_STATUS_AWAITING_VERIFICATION
    assert receipt["leaseProven"] is True
    assert receipt["snapshotConsistencyProven"] is False
    assert receipt["contentIdentityProven"] is False
    assert receipt["sqliteSnapshot"]["integrityCheck"] == "deferred"
    assert receipt["contentIdentity"] is None
    assert receipt["verification"]["status"] == "pending"

    # The copy itself still happened: the staged tree is real and complete.
    staged = Path(receipt["stagedRoot"])
    assert (staged / "chroma.sqlite3").is_file()
    assert (staged / SEGMENT_ID / "header.bin").is_file()

    manifest_path = Path(receipt["manifestPath"])
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema"] == "mempalace-backup-snapshot-staging-manifest/v1"
    assert isinstance(manifest["sourceCounts"], dict)

    on_disk_receipt = json.loads(Path(receipt["receiptPath"]).read_text(encoding="utf-8"))
    assert on_disk_receipt["status"] == SNAPSHOT_STATUS_AWAITING_VERIFICATION


def test_verify_staged_snapshot_proves_a_previously_unverified_generation(palace, tmp_path):
    staging = tmp_path / "staging"
    copy_receipt = stage_palace_snapshot(
        palace, staging, use_maintenance_marker=False, verify=False
    )
    assert copy_receipt["status"] == SNAPSHOT_STATUS_AWAITING_VERIFICATION

    final_receipt = verify_staged_snapshot(staging)

    assert final_receipt["status"] == "complete"
    assert final_receipt["leaseProven"] is True
    assert final_receipt["snapshotConsistencyProven"] is True
    assert final_receipt["contentIdentityProven"] is True
    assert final_receipt["sqliteSnapshot"]["integrityCheck"] == "ok"
    assert final_receipt["contentIdentity"]["fileCount"] > 0
    assert final_receipt["verification"]["status"] == "complete"

    # The staged tree is fully and correctly hashed, matching an immediate
    # (verify=True) run over an equivalent palace.
    staged = Path(final_receipt["stagedRoot"])
    on_disk = {p.relative_to(staged).as_posix() for p in staged.rglob("*") if p.is_file()}
    hashed = {entry["relativePath"] for entry in final_receipt["contentIdentity"]["files"]}
    assert hashed == on_disk

    # The SAME on-disk receipt file was rewritten in place -- not a second file.
    rewritten = json.loads(Path(final_receipt["receiptPath"]).read_text(encoding="utf-8"))
    assert rewritten["status"] == "complete"
    assert rewritten["contentIdentityDigest"] == final_receipt["contentIdentityDigest"]

    # verify_snapshot_receipt's cheap structural re-check accepts the result.
    structural = verify_snapshot_receipt(final_receipt["receiptPath"], verify_hashes=False)
    assert structural["valid"] is True


def test_verify_staged_snapshot_reports_a_failed_integrity_check_without_silently_accepting(
    palace, tmp_path, monkeypatch
):
    staging = tmp_path / "staging"
    stage_palace_snapshot(palace, staging, use_maintenance_marker=False, verify=False)

    monkeypatch.setattr(
        backup_snapshot,
        "_sqlite_integrity_check",
        lambda *a, **k: (_ for _ in ()).throw(
            PalaceSnapshotError("snapshot SQLite integrity check did not return ok: corrupt")
        ),
    )

    final_receipt = verify_staged_snapshot(staging)

    # Failure is REPORTED, not silently accepted as proven.
    assert final_receipt["status"] == "error"
    assert final_receipt["snapshotConsistencyProven"] is False
    assert final_receipt["contentIdentityProven"] is False
    assert final_receipt["verification"]["status"] == "failed"
    assert "corrupt" in final_receipt["verification"]["message"]

    # The on-disk receipt reflects the failure -- the generation is never left
    # claiming (or looking like) a proven backup.
    on_disk = json.loads(Path(final_receipt["receiptPath"]).read_text(encoding="utf-8"))
    assert on_disk["status"] == "error"
    assert on_disk["snapshotConsistencyProven"] is False


def test_verify_staged_snapshot_reports_a_content_identity_mismatch(palace, tmp_path):
    staging = tmp_path / "staging"
    copy_receipt = stage_palace_snapshot(
        palace, staging, use_maintenance_marker=False, verify=False
    )
    manifest_path = Path(copy_receipt["manifestPath"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    # Force a mismatch against whatever the staged copy's own counts compute
    # to (this fixture's minimal palace naturally produces {} for both sides).
    manifest["sourceCounts"] = {
        "drawers": {"segmentId": "fixture", "sqliteCount": 999999, "hnswCount": 999999}
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    final_receipt = verify_staged_snapshot(staging)

    assert final_receipt["status"] == "error"
    assert final_receipt["contentIdentityProven"] is False
    assert "content identity failed" in final_receipt["verification"]["message"]


def test_verify_staged_snapshot_requires_a_staging_manifest(tmp_path):
    staging = tmp_path / "staging"
    (staging / "palace").mkdir(parents=True)
    with pytest.raises(PalaceSnapshotError, match="staging manifest is missing"):
        verify_staged_snapshot(staging)


def test_stage_palace_snapshot_defaults_to_verify_true(palace, tmp_path):
    """Backward compatibility: every existing caller omits verify=..., and must
    keep getting the original, fully-proven-inline behavior."""

    receipt = stage_palace_snapshot(palace, tmp_path / "staging", use_maintenance_marker=False)
    assert receipt["status"] == "complete"
    assert receipt["snapshotConsistencyProven"] is True
    assert receipt["contentIdentityProven"] is True


def test_snapshot_never_mutates_the_source_palace(palace, tmp_path):
    before = {
        path.relative_to(palace).as_posix(): path.stat().st_size
        for path in sorted(palace.rglob("*"))
        if path.is_file()
    }
    stage_palace_snapshot(palace, tmp_path / "staging", use_maintenance_marker=False)
    after = {
        path.relative_to(palace).as_posix(): path.stat().st_size
        for path in sorted(palace.rglob("*"))
        if path.is_file()
    }
    assert before == after
