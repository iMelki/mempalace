from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mempalace.evaluation_identity import EvaluationCorpusManifestError
from mempalace.evaluation_manifest import (
    ATTESTATION_SCHEMA,
    SNAPSHOT_METHOD,
    build_evaluation_corpus_manifest,
    finalize_evaluation_corpus_manifest,
    prepare_evaluation_snapshot,
    scan_evaluation_snapshot,
)
from mempalace.evaluation_identity import sha256_identity


DATA_PLANE_ID = "sha256:" + "d" * 64


def _seed_palace(path: Path, rows: list[tuple[str, str, dict[str, object]]]) -> None:
    path.mkdir()
    database = path / "chroma.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE collections (id TEXT PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE segments (id TEXT PRIMARY KEY, collection TEXT NOT NULL, scope TEXT NOT NULL);
            CREATE TABLE embeddings (id INTEGER PRIMARY KEY, embedding_id TEXT NOT NULL, segment_id TEXT NOT NULL);
            CREATE TABLE embedding_metadata (
              id INTEGER NOT NULL, key TEXT NOT NULL, string_value TEXT,
              int_value INTEGER, float_value REAL, bool_value INTEGER
            );
            """
        )
        connection.execute("INSERT INTO collections VALUES ('c', 'mempalace_drawers')")
        connection.execute("INSERT INTO segments VALUES ('s', 'c', 'METADATA')")
        for index, (identifier, document, metadata) in enumerate(rows, start=1):
            connection.execute("INSERT INTO embeddings VALUES (?, ?, 's')", (index, identifier))
            connection.execute(
                "INSERT INTO embedding_metadata (id, key, string_value) VALUES (?, 'chroma:document', ?)",
                (index, document),
            )
            for key, value in metadata.items():
                connection.execute(
                    "INSERT INTO embedding_metadata (id, key, string_value) VALUES (?, ?, ?)",
                    (index, key, str(value)),
                )


def test_manifest_is_stable_across_sqlite_row_order_and_has_attestation(tmp_path: Path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    left = [("drawer-b", "Second", {"room": "beta"}), ("drawer-a", "First", {"room": "alpha"})]
    _seed_palace(first, left)
    _seed_palace(second, list(reversed(left)))

    first_manifest, first_attestation = build_evaluation_corpus_manifest(
        first, data_plane_id=DATA_PLANE_ID, captured_at_utc="2026-07-29T15:00:00Z"
    )
    second_manifest, second_attestation = build_evaluation_corpus_manifest(
        second, data_plane_id=DATA_PLANE_ID, captured_at_utc="2026-07-29T15:00:00Z"
    )

    assert first_manifest == second_manifest
    assert first_manifest["itemCount"] == 2
    assert first_attestation["schema"] == ATTESTATION_SCHEMA
    assert first_attestation["snapshotMethod"] == SNAPSHOT_METHOD
    assert first_attestation["manifestSha256"] == second_attestation["manifestSha256"]
    assert first_attestation["inventorySha256"] == second_attestation["inventorySha256"]


def test_manifest_excludes_non_retrievable_blank_documents(tmp_path: Path):
    palace = tmp_path / "palace"
    _seed_palace(palace, [("included", "visible", {}), ("blank", "", {})])

    manifest, attestation = build_evaluation_corpus_manifest(
        palace, data_plane_id=DATA_PLANE_ID, captured_at_utc="2026-07-29T15:00:00Z"
    )

    assert manifest["itemCount"] == 1
    assert manifest["inventorySha256"] == attestation["inventorySha256"]
    assert manifest["scopeSha256"].startswith("sha256:")


def test_manifest_fails_closed_for_duplicate_drawer_ids(tmp_path: Path):
    palace = tmp_path / "palace"
    _seed_palace(palace, [("duplicate", "one", {}), ("duplicate", "two", {})])

    with pytest.raises(EvaluationCorpusManifestError, match="duplicate drawer ids"):
        build_evaluation_corpus_manifest(
            palace, data_plane_id=DATA_PLANE_ID, captured_at_utc="2026-07-29T15:00:00Z"
        )


def test_durable_snapshot_scan_resumes_and_matches_one_shot_manifest(tmp_path: Path):
    palace = tmp_path / "palace"
    _seed_palace(
        palace,
        [
            ("drawer-c", "Third", {"room": "gamma"}),
            ("drawer-a", "First", {"room": "alpha"}),
            ("drawer-b", "Second", {"room": "beta"}),
        ],
    )
    captured = "2026-07-29T15:00:00Z"
    expected_manifest, _ = build_evaluation_corpus_manifest(
        palace, data_plane_id=DATA_PLANE_ID, batch_size=1, captured_at_utc=captured
    )
    stage = tmp_path / "stage"
    receipt = prepare_evaluation_snapshot(
        palace, staging_dir=stage, data_plane_id=DATA_PLANE_ID, captured_at_utc=captured
    )
    assert receipt["status"] == "complete"
    partial = scan_evaluation_snapshot(
        stage, data_plane_id=DATA_PLANE_ID, batch_size=1, max_batches=1
    )
    assert partial["status"] == "scanning"
    with pytest.raises(EvaluationCorpusManifestError, match="incomplete"):
        finalize_evaluation_corpus_manifest(stage, data_plane_id=DATA_PLANE_ID)
    complete = scan_evaluation_snapshot(stage, data_plane_id=DATA_PLANE_ID, batch_size=1)
    assert complete["status"] == "complete"
    manifest, attestation = finalize_evaluation_corpus_manifest(
        stage, data_plane_id=DATA_PLANE_ID, captured_at_utc=captured
    )
    assert manifest == expected_manifest
    assert attestation["itemCount"] == 3
    assert not any("document" in path.read_text(encoding="utf-8") for path in (stage / "shards").glob("*.json"))


def test_staging_fails_closed_when_snapshot_or_checkpoint_binding_changes(tmp_path: Path):
    palace = tmp_path / "palace"
    _seed_palace(palace, [("drawer-a", "First", {})])
    stage = tmp_path / "stage"
    prepare_evaluation_snapshot(palace, staging_dir=stage, data_plane_id=DATA_PLANE_ID)
    checkpoint = stage / "inventory-checkpoint.json"
    checkpoint.write_text(checkpoint.read_text(encoding="utf-8").replace(DATA_PLANE_ID, "sha256:" + "e" * 64), encoding="utf-8")
    with pytest.raises(EvaluationCorpusManifestError, match="not bound"):
        scan_evaluation_snapshot(stage, data_plane_id=DATA_PLANE_ID)


def test_snapshot_creator_revision_and_scan_processor_revision_are_separately_bound(tmp_path: Path, monkeypatch):
    palace = tmp_path / "palace"
    _seed_palace(palace, [("drawer-a", "First", {})])
    stage = tmp_path / "stage"
    import mempalace.evaluation_manifest as manifest_module

    monkeypatch.setattr(manifest_module, "_package_source_digest", lambda _path: "sha256:" + "a" * 64)
    prepare_evaluation_snapshot(palace, staging_dir=stage, data_plane_id=DATA_PLANE_ID)
    monkeypatch.setattr(manifest_module, "_package_source_digest", lambda _path: "sha256:" + "b" * 64)
    scan_evaluation_snapshot(stage, data_plane_id=DATA_PLANE_ID, batch_size=1)
    manifest, attestation = finalize_evaluation_corpus_manifest(stage, data_plane_id=DATA_PLANE_ID)
    assert manifest["sourceRevision"] == "sha256:" + "a" * 64
    assert manifest["processingSourceRevision"] == "sha256:" + "b" * 64
    assert attestation["processingSourceRevision"] == "sha256:" + "b" * 64
    monkeypatch.setattr(manifest_module, "_package_source_digest", lambda _path: "sha256:" + "c" * 64)
    with pytest.raises(EvaluationCorpusManifestError, match="processing source revision is stale"):
        finalize_evaluation_corpus_manifest(stage, data_plane_id=DATA_PLANE_ID)


def test_streamed_inventory_hash_is_historical_canonical_inventory_hash(tmp_path: Path):
    palace = tmp_path / "palace"
    _seed_palace(palace, [("drawer-b", "Second", {}), ("drawer-a", "First", {})])
    stage = tmp_path / "stage"
    prepare_evaluation_snapshot(palace, staging_dir=stage, data_plane_id=DATA_PLANE_ID)
    scan_evaluation_snapshot(stage, data_plane_id=DATA_PLANE_ID, batch_size=1)
    manifest, _ = finalize_evaluation_corpus_manifest(stage, data_plane_id=DATA_PLANE_ID)
    rows = [
        {"id": "drawer-a", "rowSha256": sha256_identity({"id": "drawer-a", "document": "First", "metadata": {}})},
        {"id": "drawer-b", "rowSha256": sha256_identity({"id": "drawer-b", "document": "Second", "metadata": {}})},
    ]
    assert manifest["inventorySha256"] == sha256_identity(
        {
            "schema": "mempalace-evaluation-logical-inventory/v1",
            "backend": "chroma-sqlite",
            "collection": "mempalace_drawers",
            "eligibility": "metadata-segment with non-empty chroma:document",
            "rows": rows,
        }
    )


def test_snapshot_isolated_from_source_mutation_after_completion(tmp_path: Path):
    palace = tmp_path / "palace"
    _seed_palace(palace, [("drawer-a", "First", {})])
    stage = tmp_path / "stage"
    prepare_evaluation_snapshot(palace, staging_dir=stage, data_plane_id=DATA_PLANE_ID)
    with sqlite3.connect(palace / "chroma.sqlite3") as connection:
        connection.execute("INSERT INTO embeddings VALUES (2, 'drawer-b', 's')")
        connection.execute(
            "INSERT INTO embedding_metadata (id, key, string_value) VALUES (2, 'chroma:document', 'Second')"
        )
    scan_evaluation_snapshot(stage, data_plane_id=DATA_PLANE_ID, batch_size=1)
    manifest, _ = finalize_evaluation_corpus_manifest(stage, data_plane_id=DATA_PLANE_ID)
    assert manifest["itemCount"] == 1


def test_duplicate_ids_in_separate_resumed_shards_fail_at_finalization(tmp_path: Path):
    palace = tmp_path / "palace"
    _seed_palace(palace, [("duplicate", "First", {}), ("duplicate", "Second", {})])
    stage = tmp_path / "stage"
    prepare_evaluation_snapshot(palace, staging_dir=stage, data_plane_id=DATA_PLANE_ID)
    scan_evaluation_snapshot(stage, data_plane_id=DATA_PLANE_ID, batch_size=1, shard_batches=1)
    with pytest.raises(EvaluationCorpusManifestError, match="duplicate drawer ids"):
        finalize_evaluation_corpus_manifest(stage, data_plane_id=DATA_PLANE_ID)
