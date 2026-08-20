"""Deliberately broken caller fixture for the #49 continuation-context gate."""

import mempalace.conversation_chunking as chunking


def test_broken_context_helper_is_rejected(monkeypatch):
    monkeypatch.setattr(chunking, "_context_envelope", lambda *_args, **_kwargs: ("", False))
    assert chunking._context_envelope("question", 160) == ("", False), "break did not apply"

    unit = chunking.ConversationUnit(
        title="",
        conversation_id_hash=chunking.privacy_safe_identity("fixture", "conversation"),
        root_id_hash=chunking.privacy_safe_identity("fixture", "root"),
        identity_fallback=False,
        turns=(
            chunking.ConversationTurn(
                role="user",
                text="Why must every continuation retain the question?",
                message_id_hash=chunking.privacy_safe_identity("fixture", "user"),
            ),
            chunking.ConversationTurn(
                role="assistant",
                text="Because isolated retrieval needs meaning. " * 80,
                message_id_hash=chunking.privacy_safe_identity("fixture", "assistant"),
            ),
        ),
    )
    chunks = chunking.chunk_conversation_units(
        [unit],
        source_identity_hash=chunking.privacy_safe_identity("fixture", "source"),
        chunk_size=160,
        min_chunk_size=0,
    )
    continuations = [chunk for chunk in chunks if chunk["part_index"] > 0]
    assert continuations, "fixture did not produce continuation chunks"
    assert all(chunk["context_inherited"] for chunk in continuations), (
        "continuation context gate admitted a chunk without user context"
    )
