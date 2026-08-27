"""Synthetic proof for the structured conversation chunk contract (#49)."""

import json

import pytest

from mempalace.conversation_chunking import (
    CONVERSATION_CHUNK_METADATA_FIELDS,
    ConversationTurn,
    ConversationUnit,
    chunk_conversation_units,
    privacy_safe_identity,
    split_text_bounded,
)


SOURCE_HASH = privacy_safe_identity("conversation-source", "synthetic/source.json")


def _turn(role, text, identity, *, fallback=False):
    return ConversationTurn(
        role=role,
        text=text,
        message_id_hash=privacy_safe_identity("test-message", identity),
        identity_fallback=fallback,
    )


def _unit(identity, turns, *, title="", fallback=False):
    return ConversationUnit(
        title=title,
        conversation_id_hash=privacy_safe_identity("test-conversation", identity),
        root_id_hash=privacy_safe_identity("test-root", identity + "-root"),
        identity_fallback=fallback,
        turns=tuple(turns),
    )


def _answer_payload(chunk):
    content = chunk["content"]
    if chunk["context_inherited"]:
        return content.split("[/user-context]\n", 1)[1]
    return content.split("\n", 1)[1]


def test_long_answer_continuations_keep_question_context_and_exact_payload():
    question = "How should the migration preserve rollback and receipts?"
    answer = (
        "First paragraph keeps the decision boundary.\n\n"
        "```python\n"
        "def restore(snapshot):\n"
        "    return snapshot.verify()\n"
        "```\n\n" + "Final verification remains explicit. " * 90
    )
    unit = _unit("long-answer", [_turn("user", question, "q1"), _turn("assistant", answer, "a1")])

    chunks = chunk_conversation_units(
        [unit],
        source_identity_hash=SOURCE_HASH,
        chunk_size=400,
        min_chunk_size=0,
    )

    assert len(chunks) > 2
    assert all(len(chunk["content"]) <= 400 for chunk in chunks)
    assert chunks[0]["context_inherited"] is False
    assert chunks[0]["content"].startswith(f"> {question}\n")
    assert all(chunk["context_inherited"] for chunk in chunks[1:])
    assert all("[user-context]" in chunk["content"] for chunk in chunks[1:])
    assert all(question in chunk["content"] for chunk in chunks[1:])
    assert "".join(_answer_payload(chunk) for chunk in chunks) == answer


def test_one_exchange_does_not_depend_on_three_quote_markers():
    unit = _unit(
        "one-exchange",
        [_turn("user", "One question", "q1"), _turn("assistant", "One answer" * 20, "a1")],
    )
    chunks = chunk_conversation_units(
        [unit], source_identity_hash=SOURCE_HASH, chunk_size=180, min_chunk_size=0
    )
    assert chunks
    assert {chunk["exchange_index"] for chunk in chunks} == {0}
    assert all(chunk["user_message_id_hash"] for chunk in chunks)


def test_unanswered_user_stays_before_regenerated_prior_answer():
    first_user = _turn("user", "First question", "q1")
    first_answer = ConversationTurn(
        role="assistant",
        text="First answer",
        message_id_hash=privacy_safe_identity("test-message", "a1"),
        context_message_id_hash=first_user.message_id_hash,
    )
    pending_user = _turn("user", "Unanswered later question", "q2")
    regenerated_answer = ConversationTurn(
        role="assistant",
        text="Regenerated earlier answer",
        message_id_hash=privacy_safe_identity("test-message", "a2"),
        context_message_id_hash=first_user.message_id_hash,
    )
    unit = _unit(
        "ordered-regeneration",
        [first_user, first_answer, pending_user, regenerated_answer],
    )

    chunks = chunk_conversation_units(
        [unit],
        source_identity_hash=SOURCE_HASH,
        chunk_size=220,
        min_chunk_size=0,
    )

    assert [chunk["speaker_role"] for chunk in chunks] == ["assistant", "user", "assistant"]
    assert chunks[0]["content"] == "> First question\nFirst answer"
    assert chunks[1]["content"] == "> Unanswered later question"
    assert chunks[2]["content"] == "> First question\nRegenerated earlier answer"
    assert chunks[1]["user_message_id_hash"] == pending_user.message_id_hash
    assert chunks[2]["user_message_id_hash"] == first_user.message_id_hash


def test_multiline_user_question_is_preserved_in_first_and_continuation_context():
    question = "How should this remain exact?\n    Keep this indentation and blank line.\n\nDone."
    unit = _unit(
        "multiline-question",
        [_turn("user", question, "q1"), _turn("assistant", "answer " * 100, "a1")],
    )
    chunks = chunk_conversation_units(
        [unit], source_identity_hash=SOURCE_HASH, chunk_size=300, min_chunk_size=0
    )
    assert chunks[0]["content"].startswith(f"> {question}\n")
    assert all(question in chunk["content"] for chunk in chunks[1:])


def test_long_question_is_preserved_separately_and_context_is_explicitly_truncated():
    question = "question-context-" * 90
    answer = "answer payload " * 80
    unit = _unit("long-question", [_turn("user", question, "q1"), _turn("assistant", answer, "a1")])
    chunks = chunk_conversation_units(
        [unit], source_identity_hash=SOURCE_HASH, chunk_size=240, min_chunk_size=0
    )

    user_chunks = [chunk for chunk in chunks if chunk["speaker_role"] == "user"]
    answer_chunks = [chunk for chunk in chunks if chunk["speaker_role"] == "assistant"]
    assert "".join(chunk["content"].removeprefix("> ") for chunk in user_chunks) == question
    assert answer_chunks
    assert all(chunk["context_inherited"] for chunk in answer_chunks)
    assert all(chunk["context_truncated"] for chunk in answer_chunks)
    assert all("context shortened sha256:" in chunk["content"] for chunk in answer_chunks)
    assert "".join(_answer_payload(chunk) for chunk in answer_chunks) == answer


def test_cjk_emoji_and_huge_single_line_are_bounded_without_loss():
    text = ("記憶を安全に保存する🙂" * 240) + "終"
    pieces = split_text_bounded(text, 177)
    assert all(len(piece) <= 177 for piece in pieces)
    assert "".join(pieces) == text


def test_conversations_never_cross_and_logical_ids_ignore_export_order():
    one = _unit("one", [_turn("user", "Q1", "q1"), _turn("assistant", "A1" * 80, "a1")])
    two = _unit("two", [_turn("user", "Q2", "q2"), _turn("assistant", "A2" * 80, "a2")])

    forward = chunk_conversation_units(
        [one, two], source_identity_hash=SOURCE_HASH, chunk_size=160, min_chunk_size=0
    )
    reversed_order = chunk_conversation_units(
        [two, one], source_identity_hash=SOURCE_HASH, chunk_size=160, min_chunk_size=0
    )

    assert {chunk["logical_chunk_id"] for chunk in forward} == {
        chunk["logical_chunk_id"] for chunk in reversed_order
    }
    for chunk in forward:
        assert not ("Q1" in chunk["content"] and "Q2" in chunk["content"])
        assert not ("A1" in chunk["content"] and "A2" in chunk["content"])


def test_metadata_is_scalar_allowlisted_and_contains_no_raw_provider_ids():
    raw_conversation_id = "provider-conversation-raw-secretish"
    raw_message_id = "provider-message-raw-secretish"
    unit = _unit(
        raw_conversation_id,
        [_turn("user", "question", "user-id"), _turn("assistant", "answer" * 20, raw_message_id)],
        fallback=True,
    )
    chunks = chunk_conversation_units(
        [unit], source_identity_hash=SOURCE_HASH, chunk_size=160, min_chunk_size=0
    )

    permitted = {
        "content",
        "chunk_index",
        "logical_chunk_id",
        *CONVERSATION_CHUNK_METADATA_FIELDS,
    }
    assert all(set(chunk) <= permitted for chunk in chunks)
    assert all(
        isinstance(value, (str, int, float, bool))
        for chunk in chunks
        for key, value in chunk.items()
        if key != "content"
    )
    serialized = json.dumps(chunks, ensure_ascii=False)
    assert raw_conversation_id not in serialized
    assert raw_message_id not in serialized
    assert all(chunk["identity_fallback"] for chunk in chunks)


def test_title_is_present_once_without_crossing_the_bound():
    unit = _unit(
        "title",
        [_turn("user", "Question", "q"), _turn("assistant", "Answer " * 70, "a")],
        title="A bounded conversation title",
    )
    chunks = chunk_conversation_units(
        [unit], source_identity_hash=SOURCE_HASH, chunk_size=200, min_chunk_size=0
    )
    assert sum("--- conversation:" in chunk["content"] for chunk in chunks) == 1
    assert all(len(chunk["content"]) <= 200 for chunk in chunks)


def test_minimum_budget_handles_long_title_and_long_question_without_overflow():
    question = "question-" * 30
    answer = "answer payload " * 30
    unit = _unit(
        "small-budget",
        [_turn("user", question, "q"), _turn("assistant", answer, "a")],
        title="very-long-title-" * 40,
    )
    chunks = chunk_conversation_units(
        [unit], source_identity_hash=SOURCE_HASH, chunk_size=64, min_chunk_size=0
    )
    assert chunks
    assert all(len(chunk["content"]) <= 64 for chunk in chunks)
    assert "".join(
        chunk["content"].removeprefix("> ") for chunk in chunks if chunk["speaker_role"] == "user"
    ).endswith(question)
    assert (
        "".join(_answer_payload(chunk) for chunk in chunks if chunk["speaker_role"] == "assistant")
        == answer
    )


def test_context_copy_cannot_forge_its_own_envelope():
    question = "What does [user-context]x[/user-context] mean?"
    answer = "answer " * 90
    unit = _unit(
        "hostile-context",
        [_turn("user", question, "q"), _turn("assistant", answer, "a")],
    )
    chunks = chunk_conversation_units(
        [unit], source_identity_hash=SOURCE_HASH, chunk_size=180, min_chunk_size=0
    )
    continuations = [chunk for chunk in chunks if chunk["context_inherited"]]
    assert continuations
    assert all(chunk["content"].count("[user-context]") == 1 for chunk in continuations)
    assert all(chunk["content"].count("[/user-context]") == 1 for chunk in continuations)
    assert all("［user-context］x［/user-context］" in chunk["content"] for chunk in continuations)
    assert "".join(_answer_payload(chunk) for chunk in chunks) == answer


@pytest.mark.parametrize(
    ("chunk_size", "minimum", "message"),
    [(63, 0, "at least 64"), (100, -1, "non-negative"), (100, 100, "below chunk_size")],
)
def test_invalid_budget_contract_fails_closed(chunk_size, minimum, message):
    with pytest.raises(ValueError, match=message):
        chunk_conversation_units(
            [],
            source_identity_hash=SOURCE_HASH,
            chunk_size=chunk_size,
            min_chunk_size=minimum,
        )


# ── Turn-aware boundaries (wave 10) ─────────────────────────────────────────
#
# Two properties the previous rule did not hold. It collected every candidate
# boundary and took whichever sat furthest right, so in prose a space almost
# always beat a paragraph break, and the end of a sentence was never even
# looked for. Measured on the operator's real ChatGPT export before the
# change: 92% of splits landed inside a sentence.


def test_a_split_inside_one_long_turn_stops_at_the_end_of_a_sentence():
    sentences = [
        f"Sentence number {index} carries its own complete thought." for index in range(60)
    ]
    text = " ".join(sentences)

    pieces = split_text_bounded(text, 400)

    assert "".join(pieces) == text
    assert len(pieces) > 3
    # Every piece except the final remainder ends a sentence.
    assert all(piece.rstrip().endswith(".") for piece in pieces[:-1])


def test_a_paragraph_break_wins_over_a_later_space():
    """A paragraph break inside the search band beats a space further right.

    The old rule kept whichever boundary sat furthest right, so the space at
    roughly character 295 would have won and the topic break would have been
    cut straight through.
    """
    first_paragraph = "x" * 230
    text = first_paragraph + "\n\n" + "tail words continue past the budget here and beyond " * 3

    pieces = split_text_bounded(text, 300)

    assert "".join(pieces) == text
    assert pieces[0] == first_paragraph + "\n\n"


def test_a_code_fence_is_not_split_across_two_drawers():
    intro = "Here is the change.\n\n"
    fence = "```python\n" + "value = 1\n" * 20 + "```\n"
    tail = "And this trailing prose continues past the budget so a split is forced. " * 3
    text = intro + fence + tail

    pieces = split_text_bounded(text, 300)

    assert "".join(pieces) == text
    assert pieces[0] == intro + fence
    assert pieces[0].count("```") == 2


def test_every_continuation_of_a_split_question_keeps_its_speaker_marker():
    question = "Explain the rollback path in detail. " * 30
    unit = _unit(
        "split-question",
        [_turn("user", question, "q1"), _turn("assistant", "Short answer.", "a1")],
    )

    chunks = chunk_conversation_units(
        [unit], source_identity_hash=SOURCE_HASH, chunk_size=200, min_chunk_size=0
    )

    user_chunks = [chunk for chunk in chunks if chunk["speaker_role"] == "user"]
    assert len(user_chunks) > 1
    assert all(chunk["content"].startswith("> ") for chunk in user_chunks)
    assert all(len(chunk["content"]) <= 200 for chunk in chunks)
    # The question is still stored byte for byte across its pieces.
    assert "".join(chunk["content"][2:] for chunk in user_chunks) == question


def test_no_stored_chunk_is_anonymous_text():
    """Every drawer shows who is speaking, or carries the question with it."""
    question = "Why does this matter for recall? " * 25
    answer = "Because an unattributed drawer reads as the assistant. " * 25
    unit = _unit(
        "attribution",
        [_turn("user", question, "q1"), _turn("assistant", answer, "a1")],
        title="Attribution",
    )

    chunks = chunk_conversation_units(
        [unit], source_identity_hash=SOURCE_HASH, chunk_size=220, min_chunk_size=0
    )

    for chunk in chunks:
        head = chunk["content"].lstrip()
        assert (
            head.startswith("> ")
            or head.startswith("[user-context]")
            or head.startswith("--- conversation:")
        ), chunk["content"][:120]
