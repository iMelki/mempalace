"""Unit tests for convo_miner pure functions (no chromadb needed)."""

import contextlib
import json

from mempalace.convo_miner import (
    _file_chunks_locked,
    _mine_convos_impl,
    _process_conversation_file_locked,
    chunk_exchanges,
    detect_convo_room,
    scan_convos,
)


class TestChunkExchanges:
    def test_exchange_chunking(self):
        content = (
            "> What is memory?\n"
            "Memory is persistence of information over time.\n\n"
            "> Why does it matter?\n"
            "It enables continuity across sessions and conversations.\n\n"
            "> How do we build it?\n"
            "With structured storage and retrieval mechanisms.\n"
        )
        chunks = chunk_exchanges(content)
        assert len(chunks) >= 2
        assert all("content" in c and "chunk_index" in c for c in chunks)

    def test_paragraph_fallback(self):
        """Content without '>' lines falls back to paragraph chunking."""
        content = (
            "This is a long paragraph about memory systems. " * 10 + "\n\n"
            "This is another paragraph about storage. " * 10 + "\n\n"
            "And a third paragraph about retrieval. " * 10
        )
        chunks = chunk_exchanges(content)
        assert len(chunks) >= 2

    def test_paragraph_line_group_fallback(self):
        """Long content with no paragraph breaks chunks by line groups."""
        lines = [f"Line {i}: some content that is meaningful" for i in range(60)]
        content = "\n".join(lines)
        chunks = chunk_exchanges(content)
        assert len(chunks) >= 1

    def test_empty_content(self):
        chunks = chunk_exchanges("")
        assert chunks == []

    def test_short_content_skipped(self):
        chunks = chunk_exchanges("> hi\nbye")
        # Too short to produce chunks (below MIN_CHUNK_SIZE)
        assert isinstance(chunks, list)

    def test_long_ai_response_not_truncated(self):
        """AI responses longer than 8 lines must be stored in full (verbatim principle)."""
        lines = [f"Step {i}: important detail that must be stored" for i in range(1, 14)]
        content = "> How do I implement authentication?\n" + "\n".join(lines)
        chunks = chunk_exchanges(content)
        assert len(chunks) >= 1
        stored = chunks[0]["content"]
        # All 13 lines must be present — none silently dropped
        for i in range(1, 14):
            assert f"Step {i}:" in stored, f"Step {i} was truncated and not stored"

    def test_legacy_chunks_reconstruct_preamble_titles_and_trailing_newline(self):
        content = (
            "preamble\n--- conversation: one ---\n> Q1\nA1\n--- conversation: two ---\n> Q2\nA2\n"
        )
        chunks = chunk_exchanges(content, chunk_size=37, min_chunk_size=0)
        assert "".join(chunk["content"] for chunk in chunks) == content
        assert all(len(chunk["content"]) <= 37 for chunk in chunks)

    def test_one_markdown_blockquote_does_not_drop_ordinary_preamble(self):
        content = "Introductory prose.\n\n> quoted documentation\n\nClosing prose.\n"
        chunks = chunk_exchanges(content, chunk_size=200, min_chunk_size=0)
        assert len(chunks) == 1
        assert chunks[0]["content"] == content


class TestDetectConvoRoom:
    def test_technical_room(self):
        content = "Let me debug this python function and fix the code error in the api"
        assert detect_convo_room(content) == "technical"

    def test_planning_room(self):
        content = "We need to plan the roadmap for the next sprint and set milestone deadlines"
        assert detect_convo_room(content) == "planning"

    def test_architecture_room(self):
        content = "The architecture uses a service layer with component interface and module design"
        assert detect_convo_room(content) == "architecture"

    def test_decisions_room(self):
        content = "We decided to switch and migrated to the new framework after we chose it"
        assert detect_convo_room(content) == "decisions"

    def test_general_fallback(self):
        content = "Hello, how are you doing today? The weather is nice."
        assert detect_convo_room(content) == "general"


class TestScanConvos:
    def test_scan_finds_txt_and_md(self, tmp_path):
        (tmp_path / "chat.txt").write_text("hello", encoding="utf-8")
        (tmp_path / "notes.md").write_text("world", encoding="utf-8")
        (tmp_path / "image.png").write_bytes(b"fake")
        files = scan_convos(str(tmp_path))
        extensions = {f.suffix for f in files}
        assert ".txt" in extensions
        assert ".md" in extensions
        assert ".png" not in extensions

    def test_scan_skips_git_dir(self, tmp_path):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "config.txt").write_text("git stuff", encoding="utf-8")
        (tmp_path / "chat.txt").write_text("hello", encoding="utf-8")
        files = scan_convos(str(tmp_path))
        assert len(files) == 1

    def test_scan_skips_meta_json(self, tmp_path):
        (tmp_path / "chat.meta.json").write_text("{}", encoding="utf-8")
        (tmp_path / "chat.json").write_text("{}", encoding="utf-8")
        files = scan_convos(str(tmp_path))
        names = [f.name for f in files]
        assert "chat.json" in names
        assert "chat.meta.json" not in names

    def test_scan_empty_dir(self, tmp_path):
        files = scan_convos(str(tmp_path))
        assert files == []


class TestFileChunksLocked:
    def test_uses_bounded_upsert_batches(self, monkeypatch):
        import mempalace.convo_miner as convo_miner

        class FakeCol:
            def __init__(self):
                self.batch_sizes = []

            def delete(self, *args, **kwargs):
                pass

            def upsert(self, documents, ids, metadatas):
                self.batch_sizes.append(len(documents))

        chunks = [{"content": f"chunk {i} " * 20, "chunk_index": i} for i in range(5)]
        col = FakeCol()
        monkeypatch.setattr(convo_miner, "DRAWER_UPSERT_BATCH_SIZE", 2)
        monkeypatch.setattr(
            convo_miner, "file_already_mined", lambda collection, source_file: False
        )
        monkeypatch.setattr(convo_miner, "mine_lock", lambda source_file: contextlib.nullcontext())
        monkeypatch.setattr(convo_miner, "_detect_hall_cached", lambda content: "conversations")

        drawers, room_counts, skipped = _file_chunks_locked(
            col, "chat.txt", chunks, "wing", "general", "agent", "exchange"
        )

        assert drawers == 5
        assert dict(room_counts) == {}
        assert skipped is False
        assert col.batch_sizes == [2, 2, 1]

    def test_persists_only_allowlisted_structured_metadata_and_stable_id(self, monkeypatch):
        import mempalace.convo_miner as convo_miner

        class FakeCol:
            def __init__(self):
                self.calls = []

            def delete(self, *args, **kwargs):
                pass

            def upsert(self, documents, ids, metadatas):
                self.calls.append((documents, ids, metadatas))

        chunk = {
            "content": "bounded content for metadata proof",
            "chunk_index": 3,
            "logical_chunk_id": "sha256:" + "a" * 64,
            "conversation_id_hash": "sha256:" + "b" * 64,
            "chunk_schema_version": 1,
            "context_inherited": True,
            "unapproved_nested_metadata": {"raw": "must not persist"},
        }
        monkeypatch.setattr(convo_miner, "file_already_mined", lambda *_args: False)
        monkeypatch.setattr(convo_miner, "mine_lock", lambda *_args: contextlib.nullcontext())
        monkeypatch.setattr(convo_miner, "_detect_hall_cached", lambda _content: "general")

        first = FakeCol()
        second = FakeCol()
        for collection in (first, second):
            _file_chunks_locked(
                collection,
                "source.json",
                [chunk],
                "conversations",
                "general",
                "test",
                "exchange",
            )

        first_id = first.calls[0][1][0]
        second_id = second.calls[0][1][0]
        metadata = first.calls[0][2][0]
        assert first_id == second_id
        assert metadata["conversation_id_hash"] == "sha256:" + "b" * 64
        assert metadata["chunk_schema_version"] == 1
        assert metadata["context_inherited"] is True
        assert "logical_chunk_id" not in metadata
        assert "unapproved_nested_metadata" not in metadata


def test_chatgpt_file_uses_structured_units_before_flat_text(tmp_path, monkeypatch):
    import mempalace.convo_miner as convo_miner

    source = tmp_path / "conversations.json"
    source.write_text(
        json.dumps(
            [
                {
                    "conversation_id": "raw-conversation-id",
                    "title": "Structured test",
                    "current_node": "a1",
                    "mapping": {
                        "root": {"parent": None, "message": None},
                        "u1": {
                            "parent": "root",
                            "message": {
                                "id": "raw-user-id",
                                "author": {"role": "user"},
                                "content": {"content_type": "text", "parts": ["Why receipts?"]},
                            },
                        },
                        "a1": {
                            "parent": "u1",
                            "message": {
                                "id": "raw-assistant-id",
                                "author": {"role": "assistant"},
                                "content": {
                                    "content_type": "text",
                                    "parts": ["Because retries need identity. " * 70],
                                },
                            },
                        },
                    },
                }
            ]
        ),
        encoding="utf-8",
    )
    captured = {}
    real_chunker = convo_miner.chunk_conversation_units

    def capture(units, **kwargs):
        chunks = real_chunker(units, **kwargs)
        captured["units"] = units
        captured["chunks"] = chunks
        return chunks

    monkeypatch.setattr(convo_miner, "chunk_conversation_units", capture)
    monkeypatch.setattr(
        convo_miner,
        "normalize",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("flattened path used")),
    )
    monkeypatch.setattr(convo_miner, "_print_conversation_dry_run", lambda *_args: {})

    count, _, skipped = _process_conversation_file_locked(
        filepath=source,
        collection=object(),
        wing="conversations",
        agent="test",
        extract_mode="exchange",
        dry_run=True,
        index=1,
        total_files=1,
    )

    assert skipped is False
    assert count == len(captured["chunks"]) > 1
    assert len(captured["units"]) == 1
    serialized = json.dumps(captured["chunks"])
    assert "raw-conversation-id" not in serialized
    assert "raw-user-id" not in serialized
    assert "raw-assistant-id" not in serialized


def test_convo_miner_reconciles_both_managed_collections(tmp_path, monkeypatch):
    """Conversation mining must recover a prior drawers+closets rewrite."""
    import mempalace.convo_miner as convo_miner

    drawers = object()
    closets = object()
    captured = {}

    class FakeReceiptStore:
        def __init__(self, _palace_path):
            pass

        def reconcile_pending_rewrites(self, collections):
            captured["collections"] = collections

        def create_run(self, **_kwargs):
            captured["run_config"] = _kwargs["config"]
            return object()

    monkeypatch.setattr(convo_miner, "get_collection", lambda *_args, **_kwargs: drawers)
    monkeypatch.setattr(
        convo_miner,
        "get_closets_collection",
        lambda *_args, **_kwargs: closets,
    )
    monkeypatch.setattr(convo_miner, "ReceiptStore", FakeReceiptStore)
    monkeypatch.setattr(convo_miner, "mine_palace_lock", lambda _path: contextlib.nullcontext())
    monkeypatch.setenv("MEMPALACE_CHATGPT_ALL_BRANCHES", "true")
    monkeypatch.setenv("MEMPALACE_CHATGPT_INCLUDE_THOUGHTS", "false")

    _mine_convos_impl(
        convo_dir=str(tmp_path),
        palace_path=str(tmp_path / "palace"),
        wing="test",
        dry_run=False,
    )

    assert captured["collections"] == {"drawers": drawers, "closets": closets}
    assert captured["run_config"]["conversation_chunk_schema_version"] == 1
    assert captured["run_config"]["conversation_chunk_budget_method"] == (
        "structure-aware-chars-v1"
    )
    assert captured["run_config"]["min_chunk_size"] == 30
    assert captured["run_config"]["chatgpt_all_branches"] is True
    assert captured["run_config"]["chatgpt_include_thoughts"] is False
