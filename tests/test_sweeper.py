"""Managed message-level JSONL ingestion and compatibility tests."""

import json
import os

import pytest


def _rows_by_id(result):
    ids = list(result["ids"])
    documents = result.get("documents") or [None] * len(ids)
    metadatas = result.get("metadatas") or [None] * len(ids)
    embeddings = result.get("embeddings") or [None] * len(ids)
    return {
        item_id: {
            "document": documents[index],
            "metadata": metadatas[index],
            "embedding": None if embeddings[index] is None else list(embeddings[index]),
        }
        for index, item_id in enumerate(ids)
    }


@pytest.fixture
def mock_claude_jsonl(tmp_path):
    """Real Claude Code jsonl shape: user/assistant records among progress noise."""
    path = tmp_path / "session_abc.jsonl"
    lines = [
        # Noise: progress event, no message
        {
            "type": "progress",
            "timestamp": "2026-04-18T10:00:00Z",
            "sessionId": "abc",
            "uuid": "p-1",
        },
        # User message
        {
            "type": "user",
            "timestamp": "2026-04-18T10:00:05Z",
            "sessionId": "abc",
            "uuid": "u-1",
            "message": {"role": "user", "content": "What's the capital of France?"},
        },
        # Assistant reply
        {
            "type": "assistant",
            "timestamp": "2026-04-18T10:00:06Z",
            "sessionId": "abc",
            "uuid": "a-1",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Paris."}]},
        },
        # Noise: file-history-snapshot
        {"type": "file-history-snapshot", "messageId": "abc-snap"},
        # Second user/assistant exchange
        {
            "type": "user",
            "timestamp": "2026-04-18T10:01:00Z",
            "sessionId": "abc",
            "uuid": "u-2",
            "message": {"role": "user", "content": "And of Germany?"},
        },
        {
            "type": "assistant",
            "timestamp": "2026-04-18T10:01:01Z",
            "sessionId": "abc",
            "uuid": "a-2",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Berlin."}]},
        },
    ]
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return path


class TestSweeperParsing:
    def test_parse_yields_only_user_and_assistant(self, mock_claude_jsonl):
        from mempalace.sweeper import parse_claude_jsonl

        records = list(parse_claude_jsonl(str(mock_claude_jsonl)))
        roles = [r["role"] for r in records]
        assert roles == ["user", "assistant", "user", "assistant"], (
            f"Expected 4 user/assistant in order, got {roles}. "
            "Noise records (progress, file-history-snapshot) must be "
            "filtered out."
        )

    def test_parse_extracts_session_id_and_timestamp(self, mock_claude_jsonl):
        from mempalace.sweeper import parse_claude_jsonl

        records = list(parse_claude_jsonl(str(mock_claude_jsonl)))
        first = records[0]
        assert first["session_id"] == "abc"
        assert first["timestamp"] == "2026-04-18T10:00:05Z"
        assert first["uuid"] == "u-1"

    def test_parse_normalizes_assistant_content_list_to_text(self, mock_claude_jsonl):
        from mempalace.sweeper import parse_claude_jsonl

        records = list(parse_claude_jsonl(str(mock_claude_jsonl)))
        assistant_rec = records[1]
        assert assistant_rec["role"] == "assistant"
        assert (
            "Paris" in assistant_rec["content"]
        ), f"Assistant content blocks must be flattened to text; got: {assistant_rec['content']!r}"

    def test_parse_preserves_tool_blocks_verbatim(self, tmp_path):
        """Per the design principle "verbatim always", tool_use and
        tool_result blocks must NOT be truncated. A long tool input
        (e.g. a large diff handed to a code-edit tool) must round-trip
        in full, otherwise we silently lose user-adjacent data.
        """
        import json as _json

        from mempalace.sweeper import parse_claude_jsonl

        big_input = {"diff": "x" * 5000}  # well past the old 500-char cap
        path = tmp_path / "session_tools.jsonl"
        path.write_text(
            _json.dumps(
                {
                    "type": "assistant",
                    "timestamp": "2026-04-18T10:00:00Z",
                    "sessionId": "tools-1",
                    "uuid": "a-tool",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "tool_use", "name": "Edit", "input": big_input},
                        ],
                    },
                }
            )
            + "\n"
        )

        records = list(parse_claude_jsonl(str(path)))
        assert len(records) == 1
        content = records[0]["content"]
        # The full 5000-char value must be present — no truncation marker,
        # no [:500] slice. Look for the raw string in the serialized form.
        assert big_input["diff"] in content, (
            "tool_use input was truncated. The verbatim guarantee requires "
            f"the full payload to round-trip. Got len={len(content)}."
        )

    def test_parse_preserves_non_object_content_blocks(self, tmp_path):
        from mempalace.sweeper import parse_claude_jsonl

        path = tmp_path / "mixed-blocks.jsonl"
        path.write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "timestamp": "2026-04-18T10:00:00Z",
                    "sessionId": "mixed",
                    "uuid": "a-1",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "normal text"},
                            "orphan string block",
                            17,
                            None,
                        ],
                    },
                }
            )
            + "\n"
        )

        content = list(parse_claude_jsonl(str(path)))[0]["content"]
        assert "normal text" in content
        assert json.dumps("orphan string block") in content
        assert "17" in content
        assert "null" in content


class TestSweeperTandem:
    """The sweeper coordinates with other miners via max(timestamp)."""

    def test_sweep_empty_palace_ingests_all_messages(self, mock_claude_jsonl, tmp_path):
        from mempalace.sweeper import sweep

        palace_path = str(tmp_path / "palace")
        result = sweep(str(mock_claude_jsonl), palace_path)
        assert result["drawers_added"] == 4, (
            f"Empty palace: all 4 user/assistant messages should ingest. "
            f"Got drawers_added={result['drawers_added']}."
        )
        assert result["drawers_expected"] == 4
        assert result["drawers_verifier_confirmed"] == 4
        assert result["drawers_represented"] == 4
        assert result["drawers_upserted"] == 4
        assert result["verification_status"] == "represented"
        assert result["disposition"] == "WRITE"

    def test_sweep_is_idempotent(self, mock_claude_jsonl, tmp_path):
        """Running the sweep twice must not duplicate drawers."""
        from mempalace.sweeper import sweep

        palace_path = str(tmp_path / "palace")
        first = sweep(str(mock_claude_jsonl), palace_path)
        second = sweep(str(mock_claude_jsonl), palace_path)
        assert first["drawers_added"] == 4
        assert second["drawers_added"] == 0, (
            f"Second sweep must be a no-op on unchanged data. "
            f"Got drawers_added={second['drawers_added']} — "
            "cursor logic is broken."
        )
        assert second["drawers_already_present"] == 4
        assert second["drawers_upserted"] == 0
        assert second["drawers_rebound"] == 4
        assert second["drawers_physical_mutations"] == 4
        assert second["unchanged"] is True
        assert second["verification_status"] == "represented"

    def test_sweep_resumes_from_cursor(self, tmp_path):
        """If half the messages are already in the palace, sweep picks up
        only the later half."""
        from mempalace.sweeper import sweep

        jsonl_path = tmp_path / "session.jsonl"
        lines = [
            {
                "type": "user",
                "timestamp": "2026-04-18T09:00:00Z",
                "sessionId": "s1",
                "uuid": "u1",
                "message": {"role": "user", "content": "first"},
            },
            {
                "type": "assistant",
                "timestamp": "2026-04-18T09:00:01Z",
                "sessionId": "s1",
                "uuid": "a1",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "one"}]},
            },
        ]
        jsonl_path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")

        palace_path = str(tmp_path / "palace")
        first = sweep(str(jsonl_path), palace_path)
        assert first["drawers_added"] == 2

        # Append two more exchanges simulating live session growth.
        more_lines = [
            {
                "type": "user",
                "timestamp": "2026-04-18T09:05:00Z",
                "sessionId": "s1",
                "uuid": "u2",
                "message": {"role": "user", "content": "second"},
            },
            {
                "type": "assistant",
                "timestamp": "2026-04-18T09:05:01Z",
                "sessionId": "s1",
                "uuid": "a2",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "two"}]},
            },
        ]
        with open(jsonl_path, "a") as f:
            for x in more_lines:
                f.write(json.dumps(x) + "\n")

        second = sweep(str(jsonl_path), palace_path)
        assert second["drawers_added"] == 2, (
            f"Second sweep should pick up only the 2 new exchanges, "
            f"got {second['drawers_added']}. Cursor (max-timestamp) "
            "coordination is broken."
        )
        assert second["drawers_already_present"] == 2
        assert second["drawers_removed"] == 0
        assert second["drawers_represented"] == 4

    def test_sweep_adds_message_at_existing_max_timestamp(self, tmp_path):
        """A later source version may add an ID at the prior max timestamp."""
        from mempalace.sweeper import sweep

        shared_ts = "2026-04-18T11:00:00Z"
        lines = [
            {
                "type": "user",
                "timestamp": shared_ts,
                "sessionId": "s-tie",
                "uuid": f"u-{i}",
                "message": {"role": "user", "content": f"msg {i}"},
            }
            for i in range(3)
        ]
        jsonl_path = tmp_path / "tied.jsonl"
        palace_path = str(tmp_path / "palace")
        jsonl_path.write_text("\n".join(json.dumps(x) for x in lines[:2]) + "\n")
        first = sweep(str(jsonl_path), palace_path)
        assert first["drawers_added"] == 2

        jsonl_path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
        result = sweep(str(jsonl_path), palace_path)
        assert result["drawers_added"] == 1
        assert result["drawers_already_present"] == 2
        assert result["drawers_represented"] == 3
        assert result["cursor_by_session"] == {"s-tie": shared_ts}

    def test_sweep_refuses_legacy_unmanaged_id_collision(self, tmp_path):
        """Old direct rows need explicit migration; a new receipt cannot claim them."""
        from mempalace.palace import get_collection
        from mempalace.sweeper import _drawer_id_for_message, sweep
        from mempalace.write_receipts import ReceiptConflictError

        jsonl_path = tmp_path / "legacy.jsonl"
        record = {
            "type": "user",
            "timestamp": "2026-04-18T11:00:00Z",
            "sessionId": "legacy",
            "uuid": "u-1",
            "message": {"role": "user", "content": "old direct row"},
        }
        jsonl_path.write_text(json.dumps(record) + "\n")
        palace_path = str(tmp_path / "palace")
        collection = get_collection(palace_path, create=True)
        drawer_id = _drawer_id_for_message("legacy", "u-1")
        metadata = {
            "session_id": "legacy",
            "timestamp": record["timestamp"],
            "message_uuid": "u-1",
            "role": "user",
            "source_file": str(jsonl_path),
            "ingest_mode": "sweep",
        }
        collection.upsert(ids=[drawer_id], documents=["USER: old direct row"], metadatas=[metadata])

        with pytest.raises(ReceiptConflictError, match="explicit provenance migration"):
            sweep(str(jsonl_path), palace_path)

        assert not (tmp_path / "palace" / ".mempalace" / "write-receipts" / "v1").exists()
        row = collection.get(ids=[drawer_id], include=["documents", "metadatas"])
        assert row["documents"] == ["USER: old direct row"]
        assert row["metadatas"] == [metadata]

        jsonl_path.write_text(json.dumps({"type": "progress"}) + "\n")
        with pytest.raises(ReceiptConflictError, match="explicit provenance migration"):
            sweep(str(jsonl_path), palace_path)
        assert collection.get(ids=[drawer_id], include=["documents"])["documents"] == [
            "USER: old direct row"
        ]

    def test_empty_source_detects_legacy_relative_path_spelling(self, tmp_path):
        from mempalace.palace import get_collection
        from mempalace.sweeper import _drawer_id_for_message, sweep
        from mempalace.write_receipts import ReceiptConflictError

        jsonl_path = tmp_path / "legacy-relative.jsonl"
        jsonl_path.write_text(json.dumps({"type": "progress"}) + "\n")
        palace_path = str(tmp_path / "palace")
        collection = get_collection(palace_path, create=True)
        drawer_id = _drawer_id_for_message("legacy-relative", "u-1")
        collection.upsert(
            ids=[drawer_id],
            documents=["USER: historical row"],
            metadatas=[
                {
                    "session_id": "legacy-relative",
                    "message_uuid": "u-1",
                    "source_file": jsonl_path.name,
                    "ingest_mode": "sweep",
                }
            ],
        )

        with pytest.raises(ReceiptConflictError, match="explicit provenance migration"):
            sweep(str(jsonl_path.resolve()), palace_path)

        assert not (tmp_path / "palace" / ".mempalace" / "write-receipts" / "v1").exists()
        assert collection.get(ids=[drawer_id], include=["documents"])["documents"] == [
            "USER: historical row"
        ]


class TestSweeperDrawerMetadata:
    """Each drawer must carry the metadata the tandem-miner coordination
    depends on: session_id, timestamp, uuid, role."""

    def test_drawer_has_session_id_and_timestamp_metadata(self, mock_claude_jsonl, tmp_path):
        from mempalace.sweeper import sweep
        from mempalace.palace import get_collection

        palace_path = str(tmp_path / "palace")
        sweep(str(mock_claude_jsonl), palace_path)

        col = get_collection(palace_path, create=False)
        data = col.get(include=["metadatas"])
        metas = data["metadatas"]
        assert metas, "No drawers written"

        for m in metas:
            assert m.get("session_id") == "abc", f"Drawer missing session_id metadata: {m}"
            assert m.get("timestamp"), f"Drawer missing timestamp metadata: {m}"
            assert m.get("message_uuid"), f"Drawer missing message_uuid metadata: {m}"
            assert m.get("origin_source_file") == os.path.normcase(str(mock_claude_jsonl.resolve()))
            assert m.get("role") in (
                "user",
                "assistant",
            ), f"Drawer missing or wrong role metadata: {m}"
            assert m.get("source_file", "").startswith("mempalace://sweeper/jsonl/")
            assert m.get("write_receipt_id")
            assert m.get("write_source_identity")

    def test_managed_lane_supports_filtered_vector_readback(self, mock_claude_jsonl, tmp_path):
        from mempalace.palace import get_collection
        from mempalace.sweeper import sweep

        palace_path = str(tmp_path / "palace")
        result = sweep(str(mock_claude_jsonl), palace_path)
        collection = get_collection(palace_path, create=False)

        filtered = collection.get(
            where={"source_file": result["source_uri"]},
            include=["documents", "metadatas"],
        )
        assert len(filtered["ids"]) == 4
        assert all(
            metadata["source_file"] == result["source_uri"] for metadata in filtered["metadatas"]
        )

        queried = collection.query(
            query_texts=["Germany Berlin"],
            where={"source_file": result["source_uri"]},
            n_results=4,
            include=["documents", "metadatas", "distances"],
        )
        assert set(queried["ids"][0]) == set(filtered["ids"])
        assert all(
            metadata["source_file"] == result["source_uri"] for metadata in queried["metadatas"][0]
        )


class TestSweeperManagedReplacement:
    @pytest.mark.skipif(os.name != "nt", reason="Windows path-case semantics")
    def test_windows_path_case_alias_is_the_same_managed_source(self, mock_claude_jsonl, tmp_path):
        from mempalace.sweeper import sweep

        palace_path = str(tmp_path / "palace")
        first = sweep(str(mock_claude_jsonl), palace_path)
        case_alias = str(mock_claude_jsonl).swapcase()
        second = sweep(case_alias, palace_path)

        assert second["source_uri"] == first["source_uri"]
        assert second["receipt_id"] != first["receipt_id"]
        assert second["unchanged"] is True
        assert second["drawers_upserted"] == 0
        assert second["drawers_rebound"] == 4
        assert second["drawers_physical_mutations"] == 4

    def test_unchanged_source_repairs_semantic_metadata_tamper(self, mock_claude_jsonl, tmp_path):
        from mempalace.palace import get_collection
        from mempalace.sweeper import _drawer_id_for_message, sweep

        palace_path = str(tmp_path / "palace")
        first = sweep(str(mock_claude_jsonl), palace_path)
        collection = get_collection(palace_path, create=False)
        target_id = _drawer_id_for_message("abc", "a-2", source_uri=first["source_uri"])
        collection.update(
            ids=[target_id],
            metadatas=[{"timestamp": "2099-01-01T00:00:00Z"}],
        )

        repaired = sweep(str(mock_claude_jsonl), palace_path)
        assert repaired["receipt_id"] != first["receipt_id"]
        assert repaired["unchanged"] is False
        assert repaired["drawers_upserted"] == 4
        assert repaired["drawers_updated"] == 1
        assert repaired["drawers_semantically_unchanged"] == 3
        assert repaired["drawers_rewritten"] == 4
        assert repaired["verification_status"] == "represented"
        row = collection.get(ids=[target_id], include=["metadatas"])
        assert row["metadatas"][0]["timestamp"] == "2026-04-18T10:01:01Z"

    def test_removed_messages_do_not_linger(self, mock_claude_jsonl, tmp_path):
        from mempalace.palace import get_collection
        from mempalace.sweeper import _drawer_id_for_message, sweep

        palace_path = str(tmp_path / "palace")
        first = sweep(str(mock_claude_jsonl), palace_path)
        assert first["drawers_represented"] == 4

        records = [json.loads(line) for line in mock_claude_jsonl.read_text().splitlines()]
        kept = [record for record in records if record.get("uuid") not in {"u-2", "a-2"}]
        mock_claude_jsonl.write_text("\n".join(json.dumps(item) for item in kept) + "\n")

        second = sweep(str(mock_claude_jsonl), palace_path)
        assert second["drawers_added"] == 0
        assert second["drawers_already_present"] == 2
        assert second["drawers_removed"] == 2
        assert second["drawers_represented"] == 2
        assert second["verification_status"] == "represented"

        collection = get_collection(palace_path, create=False)
        stale_ids = [
            _drawer_id_for_message("abc", "u-2", source_uri=first["source_uri"]),
            _drawer_id_for_message("abc", "a-2", source_uri=first["source_uri"]),
        ]
        assert collection.get(ids=stale_ids, include=[])["ids"] == []

    def test_renamed_source_gets_a_disjoint_lane_without_foreign_id_failure(
        self, mock_claude_jsonl, tmp_path
    ):
        from mempalace.palace import get_collection
        from mempalace.sweeper import sweep

        palace_path = str(tmp_path / "palace")
        original = sweep(str(mock_claude_jsonl), palace_path)
        renamed_path = mock_claude_jsonl.with_name("renamed-session.jsonl")
        mock_claude_jsonl.rename(renamed_path)

        renamed = sweep(str(renamed_path), palace_path)
        assert renamed["source_uri"] != original["source_uri"]
        collection = get_collection(palace_path, create=False)
        original_ids = set(
            collection.get(where={"source_file": original["source_uri"]}, include=[])["ids"]
        )
        renamed_ids = set(
            collection.get(where={"source_file": renamed["source_uri"]}, include=[])["ids"]
        )
        assert len(original_ids) == 4
        assert len(renamed_ids) == 4
        assert original_ids.isdisjoint(renamed_ids)

    def test_semantic_zero_output_removes_prior_rows(self, mock_claude_jsonl, tmp_path):
        from mempalace.palace import get_collection
        from mempalace.sweeper import sweep
        from mempalace.write_receipts import ReceiptConflictError

        palace_path = str(tmp_path / "palace")
        first = sweep(str(mock_claude_jsonl), palace_path)
        mock_claude_jsonl.write_text(
            json.dumps(
                {
                    "type": "progress",
                    "timestamp": "2026-04-18T12:00:00Z",
                    "sessionId": "abc",
                    "uuid": "noise-only",
                }
            )
            + "\n"
        )

        with pytest.raises(ReceiptConflictError, match="allow_zero_output=True"):
            sweep(str(mock_claude_jsonl), palace_path)

        collection = get_collection(palace_path, create=False)
        assert (
            len(collection.get(where={"source_file": first["source_uri"]}, include=[])["ids"]) == 4
        )

        second = sweep(str(mock_claude_jsonl), palace_path, allow_zero_output=True)
        assert second["drawers_removed"] == 4
        assert second["drawers_represented"] == 0
        assert second["drawers_upserted"] == 0
        assert second["disposition"] == "ZERO_OUTPUT"
        assert second["verification_status"] == "represented"

        assert collection.get(where={"source_file": first["source_uri"]}, include=[])["ids"] == []

    def test_malformed_message_line_fails_before_replacement(self, mock_claude_jsonl, tmp_path):
        from mempalace.palace import get_collection
        from mempalace.sweeper import sweep
        from mempalace.write_receipts import ReceiptIdentityError, ReceiptStore

        palace_path = str(tmp_path / "palace")
        baseline = sweep(str(mock_claude_jsonl), palace_path)
        with mock_claude_jsonl.open("a", encoding="utf-8") as handle:
            handle.write('{"type":"user","message":')

        with pytest.raises(ReceiptIdentityError, match="malformed JSON"):
            sweep(str(mock_claude_jsonl), palace_path)

        collection = get_collection(palace_path, create=False)
        assert (
            len(collection.get(where={"source_file": baseline["source_uri"]}, include=[])["ids"])
            == 4
        )
        store = ReceiptStore(palace_path)
        current = store.find_current(store.source_identity(baseline["source_uri"]))
        assert current["receipt_id"] == baseline["receipt_id"]

    def test_invalid_utf8_fails_before_replacement(self, mock_claude_jsonl, tmp_path):
        from mempalace.palace import get_collection
        from mempalace.sweeper import sweep
        from mempalace.write_receipts import ReceiptIdentityError, ReceiptStore

        palace_path = str(tmp_path / "palace")
        baseline = sweep(str(mock_claude_jsonl), palace_path)
        with mock_claude_jsonl.open("ab") as handle:
            handle.write(b'\xff{"type":"user"}\n')

        with pytest.raises(ReceiptIdentityError, match="not valid UTF-8"):
            sweep(str(mock_claude_jsonl), palace_path)

        collection = get_collection(palace_path, create=False)
        assert (
            len(collection.get(where={"source_file": baseline["source_uri"]}, include=[])["ids"])
            == 4
        )
        store = ReceiptStore(palace_path)
        current = store.find_current(store.source_identity(baseline["source_uri"]))
        assert current["receipt_id"] == baseline["receipt_id"]

    def test_duplicate_message_identity_fails_before_mutation(self, tmp_path):
        from mempalace.sweeper import sweep
        from mempalace.write_receipts import ReceiptIdentityError

        record = {
            "type": "user",
            "timestamp": "2026-04-18T12:00:00Z",
            "sessionId": "duplicate",
            "uuid": "u-1",
            "message": {"role": "user", "content": "same identity"},
        }
        path = tmp_path / "duplicate.jsonl"
        path.write_text(json.dumps(record) + "\n" + json.dumps(record) + "\n")
        palace_path = str(tmp_path / "palace")

        with pytest.raises(ReceiptIdentityError, match="duplicate session/message identity"):
            sweep(str(path), palace_path)

        assert not (tmp_path / "palace").exists()

    def test_mid_batch_failure_restores_exact_predecessor(self, tmp_path, monkeypatch):
        import mempalace.sweeper as sweeper_module
        from mempalace.palace import get_collection
        from mempalace.write_receipts import ReceiptStore

        path = tmp_path / "rollback.jsonl"
        initial_records = [
            {
                "type": "user",
                "timestamp": f"2026-04-18T12:00:0{index}Z",
                "sessionId": "rollback",
                "uuid": f"u-{index}",
                "message": {"role": "user", "content": f"baseline {index}"},
            }
            for index in range(2)
        ]
        path.write_text("\n".join(json.dumps(item) for item in initial_records) + "\n")
        palace_path = str(tmp_path / "palace")
        baseline_result = sweeper_module.sweep(str(path), palace_path)
        collection = get_collection(palace_path, create=False)
        baseline_ids = sorted(
            sweeper_module._drawer_id_for_message(
                "rollback",
                f"u-{index}",
                source_uri=baseline_result["source_uri"],
            )
            for index in range(2)
        )
        baseline = collection.get(
            ids=baseline_ids,
            include=["documents", "metadatas", "embeddings"],
        )

        replacement_records = [
            {
                "type": "user",
                "timestamp": f"2026-04-18T13:{index // 60:02d}:{index % 60:02d}Z",
                "sessionId": "rollback",
                "uuid": f"replacement-{index}",
                "message": {"role": "user", "content": f"replacement {index}"},
            }
            for index in range(65)
        ]
        path.write_text("\n".join(json.dumps(item) for item in replacement_records) + "\n")

        class FailSecondAdd:
            def __init__(self, delegate):
                self.delegate = delegate
                self.add_calls = 0

            def add(self, **kwargs):
                self.add_calls += 1
                if self.add_calls == 2:
                    raise RuntimeError("injected second sweeper batch failure")
                return self.delegate.add(**kwargs)

            def __getattr__(self, name):
                return getattr(self.delegate, name)

        failing_collection = FailSecondAdd(collection)
        monkeypatch.setattr(
            sweeper_module, "get_collection", lambda *_args, **_kwargs: failing_collection
        )
        with pytest.raises(RuntimeError, match="injected second sweeper batch failure"):
            sweeper_module.sweep(str(path), palace_path)

        restored = collection.get(
            ids=baseline_ids,
            include=["documents", "metadatas", "embeddings"],
        )
        restored_by_id = _rows_by_id(restored)
        baseline_by_id = _rows_by_id(baseline)
        assert set(restored_by_id) == set(baseline_by_id)
        for item_id, expected in baseline_by_id.items():
            assert restored_by_id[item_id]["document"] == expected["document"]
            assert restored_by_id[item_id]["metadata"] == expected["metadata"]
            assert restored_by_id[item_id]["embedding"] == pytest.approx(
                expected["embedding"], abs=1e-6
            )

        replacement_ids = [
            sweeper_module._drawer_id_for_message(
                "rollback",
                f"replacement-{index}",
                source_uri=baseline_result["source_uri"],
            )
            for index in range(65)
        ]
        assert collection.get(ids=replacement_ids, include=[])["ids"] == []
        store = ReceiptStore(palace_path)
        current = store.find_current(store.source_identity(baseline_result["source_uri"]))
        assert current["receipt_id"] == baseline_result["receipt_id"]
        assert list(store._pending_recovery_paths()) == []

    def test_source_change_during_write_restores_exact_predecessor(self, tmp_path, monkeypatch):
        import mempalace.sweeper as sweeper_module
        from mempalace.palace import get_collection
        from mempalace.write_receipts import ReceiptConflictError, ReceiptStore

        path = tmp_path / "changing.jsonl"
        initial = {
            "type": "user",
            "timestamp": "2026-04-18T12:00:00Z",
            "sessionId": "changing",
            "uuid": "baseline",
            "message": {"role": "user", "content": "baseline"},
        }
        path.write_text(json.dumps(initial) + "\n")
        palace_path = str(tmp_path / "palace")
        baseline_result = sweeper_module.sweep(str(path), palace_path)
        collection = get_collection(palace_path, create=False)
        baseline_id = sweeper_module._drawer_id_for_message(
            "changing",
            "baseline",
            source_uri=baseline_result["source_uri"],
        )
        baseline = collection.get(
            where={"source_file": baseline_result["source_uri"]},
            include=["documents", "metadatas", "embeddings"],
        )

        replacement = [
            {
                "type": "user",
                "timestamp": f"2026-04-18T13:{index // 60:02d}:{index % 60:02d}Z",
                "sessionId": "changing",
                "uuid": f"replacement-{index}",
                "message": {"role": "user", "content": f"replacement {index}"},
            }
            for index in range(65)
        ]
        path.write_text("\n".join(json.dumps(item) for item in replacement) + "\n")
        appended = {
            "type": "user",
            "timestamp": "2026-04-18T14:00:00Z",
            "sessionId": "changing",
            "uuid": "late-append",
            "message": {"role": "user", "content": "changed during write"},
        }

        class ChangeAfterFirstAdd:
            def __init__(self, delegate):
                self.delegate = delegate
                self.changed = False

            def add(self, **kwargs):
                result = self.delegate.add(**kwargs)
                if not self.changed:
                    self.changed = True
                    with path.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(appended) + "\n")
                return result

            def __getattr__(self, name):
                return getattr(self.delegate, name)

        monkeypatch.setattr(
            sweeper_module,
            "get_collection",
            lambda *_args, **_kwargs: ChangeAfterFirstAdd(collection),
        )
        with pytest.raises(ReceiptConflictError, match="source (semantics )?changed"):
            sweeper_module.sweep(str(path), palace_path)

        restored = collection.get(
            where={"source_file": baseline_result["source_uri"]},
            include=["documents", "metadatas", "embeddings"],
        )
        restored_by_id = _rows_by_id(restored)
        baseline_by_id = _rows_by_id(baseline)
        assert set(restored_by_id) == {baseline_id} == set(baseline_by_id)
        for item_id, expected in baseline_by_id.items():
            assert restored_by_id[item_id]["document"] == expected["document"]
            assert restored_by_id[item_id]["metadata"] == expected["metadata"]
            assert restored_by_id[item_id]["embedding"] == pytest.approx(
                expected["embedding"], abs=1e-6
            )
        replacement_ids = [
            sweeper_module._drawer_id_for_message(
                "changing",
                f"replacement-{index}",
                source_uri=baseline_result["source_uri"],
            )
            for index in range(65)
        ]
        replacement_ids.append(
            sweeper_module._drawer_id_for_message(
                "changing",
                "late-append",
                source_uri=baseline_result["source_uri"],
            )
        )
        assert collection.get(ids=replacement_ids, include=[])["ids"] == []
        store = ReceiptStore(palace_path)
        current = store.find_current(store.source_identity(baseline_result["source_uri"]))
        assert current["receipt_id"] == baseline_result["receipt_id"]
        assert list(store._pending_recovery_paths()) == []

    def test_terminal_verification_holds_the_palace_lock(
        self, mock_claude_jsonl, tmp_path, monkeypatch
    ):
        import mempalace.provenance as provenance_module

        from mempalace.palace import MineAlreadyRunning, mine_palace_lock
        from mempalace.sweeper import sweep
        from mempalace.write_receipts import ReceiptStore

        palace_path = str(tmp_path / "palace")
        original_verify = provenance_module._verify_context_receipt
        original_finalize = ReceiptStore.finalize_rewrite_recovery
        observations = []

        def verify_while_locked(*args, **kwargs):
            with pytest.raises(MineAlreadyRunning):
                with mine_palace_lock(palace_path):
                    pass
            observations.append("verify-locked")
            return original_verify(*args, **kwargs)

        def finalize_while_locked(self, *args, **kwargs):
            with pytest.raises(MineAlreadyRunning):
                with mine_palace_lock(palace_path):
                    pass
            observations.append("finalize-locked")
            return original_finalize(self, *args, **kwargs)

        monkeypatch.setattr(provenance_module, "_verify_context_receipt", verify_while_locked)
        monkeypatch.setattr(ReceiptStore, "finalize_rewrite_recovery", finalize_while_locked)
        result = sweep(str(mock_claude_jsonl), palace_path)

        assert result["verification_status"] == "represented"
        assert observations == ["verify-locked", "finalize-locked"]

    def test_finalization_failure_after_complete_reports_committed_unverified(
        self, mock_claude_jsonl, tmp_path, monkeypatch
    ):
        from mempalace.palace import get_collection
        from mempalace.sweeper import sweep
        from mempalace.write_receipts import ReceiptStore

        def fail_finalization(self, *args, **kwargs):
            raise RuntimeError("injected terminal finalization failure")

        monkeypatch.setattr(ReceiptStore, "finalize_rewrite_recovery", fail_finalization)
        palace_path = str(tmp_path / "palace")
        result = sweep(str(mock_claude_jsonl), palace_path)

        assert result["committed"] is True
        assert result["drawers_expected"] == 4
        assert result["drawers_verifier_confirmed"] == 0
        assert result["drawers_represented"] == 0
        assert result["verification_status"] == "committed-unverified"
        assert "injected terminal finalization failure" in result["verification_error"]
        collection = get_collection(palace_path, create=False)
        assert (
            len(collection.get(where={"source_file": result["source_uri"]}, include=[])["ids"]) == 4
        )
        store = ReceiptStore(palace_path)
        current = store.find_current(store.source_identity(result["source_uri"]))
        assert current["receipt_id"] == result["receipt_id"]
        assert len(list(store._pending_recovery_paths())) == 1

    def test_terminal_receipt_readback_failure_reports_committed_unverified(
        self, mock_claude_jsonl, tmp_path, monkeypatch
    ):
        import mempalace.provenance as provenance_module

        from mempalace.palace import get_collection
        from mempalace.receipt_verifier import ReceiptVerificationError
        from mempalace.sweeper import sweep
        from mempalace.write_receipts import ReceiptStore

        def fail_verification(*args, **kwargs):
            raise ReceiptVerificationError("injected durable receipt readback failure")

        monkeypatch.setattr(provenance_module, "_verify_context_receipt", fail_verification)
        palace_path = str(tmp_path / "palace")
        result = sweep(str(mock_claude_jsonl), palace_path)

        assert result["committed"] is True
        assert result["drawers_expected"] == 4
        assert result["drawers_verifier_confirmed"] == 0
        assert result["drawers_represented"] == 0
        assert result["verification_status"] == "committed-unverified"
        assert "injected durable receipt readback failure" in result["verification_error"]
        collection = get_collection(palace_path, create=False)
        assert (
            len(collection.get(where={"source_file": result["source_uri"]}, include=[])["ids"]) == 4
        )
        store = ReceiptStore(palace_path)
        assert len(list(store._pending_recovery_paths())) == 1

    def test_unchanged_rebind_verification_failure_reports_committed_unverified(
        self, mock_claude_jsonl, tmp_path, monkeypatch
    ):
        import mempalace.provenance as provenance_module

        from mempalace.receipt_verifier import ReceiptVerificationError
        from mempalace.sweeper import sweep
        from mempalace.write_receipts import ReceiptStore

        palace_path = str(tmp_path / "palace")
        baseline = sweep(str(mock_claude_jsonl), palace_path)
        original_verify = provenance_module._verify_context_receipt
        calls = 0

        def fail_new_terminal_verification(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ReceiptVerificationError("injected unchanged terminal verification failure")
            return original_verify(*args, **kwargs)

        monkeypatch.setattr(
            provenance_module,
            "_verify_context_receipt",
            fail_new_terminal_verification,
        )
        result = sweep(str(mock_claude_jsonl), palace_path)

        assert calls == 2
        assert result["committed"] is True
        assert result["unchanged"] is True
        assert result["receipt_id"] != baseline["receipt_id"]
        assert result["drawers_expected"] == 4
        assert result["drawers_verifier_confirmed"] == 0
        assert result["drawers_represented"] == 0
        assert result["verification_status"] == "committed-unverified"
        assert "injected unchanged terminal verification failure" in result["verification_error"]
        store = ReceiptStore(palace_path)
        current = store.find_current(store.source_identity(result["source_uri"]))
        assert current["receipt_id"] == result["receipt_id"]
        assert list(store._pending_recovery_paths()) == []

    def test_unchanged_rebind_finalization_failure_reports_committed_unverified(
        self, mock_claude_jsonl, tmp_path, monkeypatch
    ):
        from mempalace.sweeper import sweep
        from mempalace.write_receipts import ReceiptStore

        palace_path = str(tmp_path / "palace")
        baseline = sweep(str(mock_claude_jsonl), palace_path)

        def fail_finalization(self, *args, **kwargs):
            raise RuntimeError("injected unchanged finalization failure")

        monkeypatch.setattr(ReceiptStore, "finalize_rewrite_recovery", fail_finalization)
        result = sweep(str(mock_claude_jsonl), palace_path)

        assert result["committed"] is True
        assert result["unchanged"] is True
        assert result["receipt_id"] != baseline["receipt_id"]
        assert result["drawers_expected"] == 4
        assert result["drawers_verifier_confirmed"] == 0
        assert result["drawers_represented"] == 0
        assert result["verification_status"] == "committed-unverified"
        assert "injected unchanged finalization failure" in result["verification_error"]
        store = ReceiptStore(palace_path)
        current = store.find_current(store.source_identity(result["source_uri"]))
        assert current["receipt_id"] == result["receipt_id"]
        assert len(list(store._pending_recovery_paths())) == 1

    def test_missing_terminal_readback_reports_committed_unverified(
        self, mock_claude_jsonl, tmp_path, monkeypatch
    ):
        from dataclasses import replace

        import mempalace.sweeper as sweeper_module
        from mempalace.palace import get_collection

        original_ingest = sweeper_module.managed_adapter_ingest

        def hide_terminal_event(**kwargs):
            result = original_ingest(**kwargs)
            return replace(result, receipt_events=())

        monkeypatch.setattr(sweeper_module, "managed_adapter_ingest", hide_terminal_event)
        palace_path = str(tmp_path / "palace")
        result = sweeper_module.sweep(str(mock_claude_jsonl), palace_path)

        assert result["committed"] is True
        assert result["receipt_id"]
        assert result["drawers_expected"] == 4
        assert result["drawers_verifier_confirmed"] == 0
        assert result["drawers_represented"] == 0
        assert result["verification_status"] == "committed-unverified"
        assert "terminal COMPLETE" in result["verification_error"]
        collection = get_collection(palace_path, create=False)
        assert (
            len(collection.get(where={"source_file": result["source_uri"]}, include=[])["ids"]) == 4
        )

    def test_directory_metrics_do_not_count_unverified_outputs_as_represented(
        self, tmp_path, monkeypatch
    ):
        import mempalace.sweeper as sweeper_module

        source_dir = tmp_path / "sources"
        source_dir.mkdir()
        verified_source = source_dir / "a-verified.jsonl"
        unverified_source = source_dir / "z-unverified.jsonl"
        verified_source.write_text("{}\n", encoding="utf-8")
        unverified_source.write_text("{}\n", encoding="utf-8")
        represented = {
            "drawers_added": 2,
            "drawers_already_present": 0,
            "drawers_updated": 0,
            "drawers_semantically_unchanged": 0,
            "drawers_rewritten": 0,
            "drawers_rebound": 0,
            "drawers_removed": 0,
            "drawers_expected": 2,
            "drawers_verifier_confirmed": 2,
            "drawers_represented": 2,
            "drawers_upserted": 2,
            "drawers_physical_mutations": 2,
            "receipt_id": "receipt-represented",
            "verification_status": "represented",
            "verification_error": None,
        }
        committed_unverified = {
            "drawers_added": 4,
            "drawers_already_present": 0,
            "drawers_updated": 0,
            "drawers_semantically_unchanged": 0,
            "drawers_rewritten": 0,
            "drawers_rebound": 0,
            "drawers_removed": 0,
            "drawers_expected": 4,
            "drawers_verifier_confirmed": 0,
            "drawers_represented": 0,
            "drawers_upserted": 4,
            "drawers_physical_mutations": 4,
            "receipt_id": "receipt-unverified",
            "verification_status": "committed-unverified",
            "verification_error": "injected readback failure",
        }
        monkeypatch.setattr(
            sweeper_module,
            "sweep",
            lambda path, *_args, **_kwargs: (
                represented if path == str(verified_source) else committed_unverified
            ),
        )

        result = sweeper_module.sweep_directory(str(source_dir), str(tmp_path / "palace"))

        assert result["drawers_expected"] == 6
        assert result["drawers_verifier_confirmed"] == 2
        assert result["drawers_represented"] == 0
        assert result["files_committed_unverified"] == 1
        assert result["per_file"][0]["expected"] == 2
        assert result["per_file"][0]["represented"] == 2
        assert result["per_file"][1]["expected"] == 4
        assert result["per_file"][1]["represented"] == 0

    def test_busy_new_palace_has_no_chroma_or_receipt_side_effects(
        self, mock_claude_jsonl, tmp_path
    ):
        from mempalace.palace import MineAlreadyRunning, mine_palace_lock
        from mempalace.sweeper import sweep

        palace_path = str(tmp_path / "new-palace")
        with mine_palace_lock(palace_path):
            with pytest.raises(MineAlreadyRunning):
                sweep(str(mock_claude_jsonl), palace_path)

        assert not (tmp_path / "new-palace").exists()

    def test_concurrent_palace_lock_refuses_a_second_sweep_without_mutation(
        self, mock_claude_jsonl, tmp_path
    ):
        from mempalace.palace import MineAlreadyRunning, get_collection, mine_palace_lock
        from mempalace.sweeper import sweep
        from mempalace.write_receipts import ReceiptStore

        palace_path = str(tmp_path / "palace")
        baseline = sweep(str(mock_claude_jsonl), palace_path)
        collection = get_collection(palace_path, create=False)
        before_ids = set(
            collection.get(where={"source_file": baseline["source_uri"]}, include=[])["ids"]
        )

        with mine_palace_lock(palace_path):
            with pytest.raises(MineAlreadyRunning):
                sweep(str(mock_claude_jsonl), palace_path)

        after_ids = set(
            collection.get(where={"source_file": baseline["source_uri"]}, include=[])["ids"]
        )
        assert after_ids == before_ids
        store = ReceiptStore(palace_path)
        current = store.find_current(store.source_identity(baseline["source_uri"]))
        assert current["receipt_id"] == baseline["receipt_id"]
