from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mempalace.evaluation_identity import EvaluationCorpusManifestError
from mempalace.evaluation_manifest import (
    ATTESTATION_SCHEMA,
    SNAPSHOT_METHOD,
    build_evaluation_corpus_manifest,
)


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
