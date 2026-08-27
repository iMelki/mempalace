#!/usr/bin/env python3
"""Regression tests for the ChatGPT account-export reader (memsys#522).

The bug these guard against: MemPalace read a real ChatGPT export as an
unrecognized file and stored the raw JSON verbatim. Nothing failed, nothing
warned — the mine run reported success while recovering zero conversations.

The failure was invisible because no test asserted output *volume*. Every
test here asserts a non-zero, exact message count, so a future parser that
silently recovers nothing fails loudly.

Fixture: ``tests/fixtures/chatgpt_export_shard_sample.json``. Schema-exact
copy of a real export shard (top-level list, ``parent`` pointers only, no
``children`` key, ``current_node`` per conversation, text / multimodal_text /
thoughts / reasoning_recap messages, one abandoned branch). All prose in it
is invented — no operator content.
"""

import json
from pathlib import Path

import pytest

from mempalace.conversation_chunking import chunk_conversation_units, privacy_safe_identity
from mempalace.normalize import (
    _chatgpt_conversation_messages,
    _try_chatgpt_json,
    normalize,
    try_chatgpt_conversation_units,
)

FIXTURE = Path(__file__).parent / "fixtures" / "chatgpt_export_shard_sample.json"

# Expected recovery from the fixture, per switch setting.
# conversation 1: 4 live text turns (2 user, 2 assistant), 2 turns on an
#                 abandoned branch, 1 thoughts block, 1 reasoning_recap.
# conversation 2: 4 live turns (voice transcription, reply, image-only
#                 upload, reply).
EXPECTED_DEFAULT = 8  # live thread, reasoning internals dropped
EXPECTED_WITH_THOUGHTS = 10  # + thoughts + reasoning_recap
EXPECTED_ALL_BRANCHES = 10  # + the 2 messages on the abandoned branch
EXPECTED_ALL_BRANCHES_WITH_THOUGHTS = 12


@pytest.fixture
def export_data():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _count_messages(data, **kwargs):
    """Total (role, text) pairs recovered across every conversation."""
    return sum(len(_chatgpt_conversation_messages(c, **kwargs)) for c in data)


# ── the core regression: non-zero recovery ────────────────────────────────


def test_fixture_exists():
    assert FIXTURE.is_file(), f"missing committed fixture: {FIXTURE}"


def test_export_shard_recovers_nonzero_messages(export_data):
    """THE guard. A list-shaped export must yield real messages, not zero."""
    total = _count_messages(export_data, all_branches=False, include_thoughts=False)
    assert total > 0, "ChatGPT export recovered zero messages — issue #522 regressed"
    assert total == EXPECTED_DEFAULT


def test_normalize_end_to_end_is_not_passthrough(tmp_path):
    """The original symptom: input chars == output chars, byte-identical.

    normalize() returned the raw JSON unchanged, which the miner then sliced
    into drawers of JSON. Output must be a transcript, not the input.
    """
    raw = FIXTURE.read_text(encoding="utf-8")
    target = tmp_path / "conversations-000.json"
    target.write_text(raw, encoding="utf-8")

    out = normalize(str(target))

    assert out != raw, "normalize() passed the export through unchanged (issue #522)"
    assert not out.lstrip().startswith("["), "output still looks like raw JSON"
    assert '"mapping"' not in out, "raw JSON keys leaked into the transcript"

    user_turns = [line for line in out.split("\n") if line.startswith("> ")]
    assert len(user_turns) == 4, f"expected 4 user turns, got {len(user_turns)}"


def test_every_conversation_in_the_shard_is_emitted(export_data):
    """One transcript per conversation — not just the first object."""
    result = _try_chatgpt_json(export_data, all_branches=False, include_thoughts=False)
    assert result is not None
    headers = [line for line in result.split("\n") if line.startswith("--- conversation:")]
    assert len(headers) == len(export_data) == 2
    assert "Sourdough starter troubleshooting" in result
    assert "Voice note about the garage shelf" in result


def test_single_conversation_object_still_works(export_data):
    """The legacy shape (one conversation dict) must keep parsing."""
    result = _try_chatgpt_json(export_data[0], all_branches=False, include_thoughts=False)
    assert result is not None
    assert "hooch" in result


def test_structured_units_hash_provider_ids_and_keep_conversations_isolated(export_data):
    raw = json.dumps(export_data)
    units = try_chatgpt_conversation_units(raw)

    assert units is not None
    assert len(units) == 2
    assert [len(unit.turns) for unit in units] == [4, 4]
    assert len({unit.conversation_id_hash for unit in units}) == 2
    assert all(unit.conversation_id_hash.startswith("sha256:") for unit in units)
    assert all(turn.message_id_hash.startswith("sha256:") for unit in units for turn in unit.turns)

    structured = repr(units)
    for raw_id in (
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
    ):
        assert raw_id not in structured
    assert "message_id_hash='u1'" not in structured
    assert "message_id_hash='a5'" not in structured


def test_structured_unit_identities_survive_export_order_changes(export_data):
    forward = try_chatgpt_conversation_units(json.dumps(export_data))
    reversed_order = try_chatgpt_conversation_units(json.dumps(list(reversed(export_data))))

    assert forward is not None and reversed_order is not None
    forward_ids = {
        unit.title: (
            unit.conversation_id_hash,
            tuple(turn.message_id_hash for turn in unit.turns),
        )
        for unit in forward
    }
    reversed_ids = {
        unit.title: (
            unit.conversation_id_hash,
            tuple(turn.message_id_hash for turn in unit.turns),
        )
        for unit in reversed_order
    }
    assert forward_ids == reversed_ids


def test_structured_units_mark_deterministic_missing_id_fallback(export_data):
    convo = export_data[0]
    convo.pop("conversation_id")
    convo.pop("id")
    for node in convo["mapping"].values():
        node.pop("id", None)
        if isinstance(node.get("message"), dict):
            node["message"].pop("id", None)

    first = try_chatgpt_conversation_units(json.dumps([convo]))
    second = try_chatgpt_conversation_units(json.dumps([convo]))
    assert first is not None and second is not None
    assert first[0].identity_fallback is True
    assert first[0].conversation_id_hash == second[0].conversation_id_hash
    assert first[0].turns
    assert all(turn.identity_fallback for turn in first[0].turns)
    assert [turn.message_id_hash for turn in first[0].turns] == [
        turn.message_id_hash for turn in second[0].turns
    ]


def test_missing_conversation_ids_do_not_collide_when_node_ids_repeat():
    def conversation(title, answer):
        return {
            "title": title,
            "current_node": "a",
            "mapping": {
                "root": {"parent": None, "message": None},
                "u": {
                    "parent": "root",
                    "message": {
                        "author": {"role": "user"},
                        "content": {"content_type": "text", "parts": ["Same question"]},
                    },
                },
                "a": {
                    "parent": "u",
                    "message": {
                        "author": {"role": "assistant"},
                        "content": {"content_type": "text", "parts": [answer]},
                    },
                },
            },
        }

    units = try_chatgpt_conversation_units(
        json.dumps([conversation("First", "First answer"), conversation("Second", "Second answer")])
    )
    assert units is not None
    assert len({unit.conversation_id_hash for unit in units}) == 2
    assert len({turn.message_id_hash for unit in units for turn in unit.turns}) == 4


def test_regenerated_answers_keep_their_parent_question_context():
    convo = {
        "conversation_id": "regeneration-fixture",
        "title": "Regenerated answer",
        "current_node": "a2",
        "mapping": {
            "root": {"parent": None, "message": None},
            "u": {
                "parent": "root",
                "message": {
                    "id": "user-message",
                    "author": {"role": "user"},
                    "content": {"content_type": "text", "parts": ["Keep my question"]},
                },
            },
            "a1": {
                "parent": "u",
                "message": {
                    "id": "assistant-one",
                    "author": {"role": "assistant"},
                    "content": {"content_type": "text", "parts": ["First answer " * 30]},
                },
            },
            "a2": {
                "parent": "u",
                "message": {
                    "id": "assistant-two",
                    "author": {"role": "assistant"},
                    "content": {"content_type": "text", "parts": ["Second answer " * 30]},
                },
            },
        },
    }
    units = try_chatgpt_conversation_units(json.dumps([convo]), all_branches=True)
    assert units is not None
    user_hash = next(turn.message_id_hash for turn in units[0].turns if turn.role == "user")
    assistants = [turn for turn in units[0].turns if turn.role == "assistant"]
    assert len(assistants) == 2
    assert {turn.context_message_id_hash for turn in assistants} == {user_hash}

    chunks = chunk_conversation_units(
        units,
        source_identity_hash=privacy_safe_identity("conversation-source", "fixture"),
        chunk_size=160,
        min_chunk_size=0,
    )
    assistant_chunks = [chunk for chunk in chunks if chunk["speaker_role"] == "assistant"]
    assert {chunk["assistant_message_id_hash"] for chunk in assistant_chunks} == {
        turn.message_id_hash for turn in assistants
    }
    assert all(chunk["user_message_id_hash"] == user_hash for chunk in assistant_chunks)
    assert all("Keep my question" in chunk["content"] for chunk in assistant_chunks)


# ── traversal: parent/current_node, not children ──────────────────────────


def test_walks_parent_pointers_when_children_key_is_absent(export_data):
    """No node in a real export has ``children``; the walk must not need it."""
    for convo in export_data:
        for node in convo["mapping"].values():
            assert "children" not in node, "fixture drifted from the real export shape"
    assert _count_messages(export_data, all_branches=False, include_thoughts=False) > 0


def test_live_thread_excludes_abandoned_branch(export_data):
    result = _try_chatgpt_json(export_data, all_branches=False, include_thoughts=False)
    assert "Never mind, I threw it out" not in result
    assert "starting a new starter takes about a week" not in result
    # ... but the live branch that replaced it is present.
    assert "Twice a day at room temperature" in result


def test_all_branches_switch_includes_abandoned_branch(export_data):
    total = _count_messages(export_data, all_branches=True, include_thoughts=False)
    assert total == EXPECTED_ALL_BRANCHES
    result = _try_chatgpt_json(export_data, all_branches=True, include_thoughts=False)
    assert "Never mind, I threw it out" in result
    assert "starting a new starter takes about a week" in result


def test_all_branches_env_override(export_data, monkeypatch):
    monkeypatch.setenv("MEMPALACE_CHATGPT_ALL_BRANCHES", "1")
    result = _try_chatgpt_json(export_data)
    assert "Never mind, I threw it out" in result
    monkeypatch.setenv("MEMPALACE_CHATGPT_ALL_BRANCHES", "0")
    result = _try_chatgpt_json(export_data)
    assert "Never mind, I threw it out" not in result


def test_dangling_current_node_falls_back_to_newest_leaf(export_data):
    """A corrupt ``current_node`` must not zero out the conversation."""
    convo = export_data[0]
    convo["current_node"] = "does-not-exist"
    messages = _chatgpt_conversation_messages(convo, all_branches=False, include_thoughts=False)
    assert len(messages) > 0


def test_missing_current_node_falls_back_to_newest_leaf(export_data):
    convo = export_data[0]
    del convo["current_node"]
    messages = _chatgpt_conversation_messages(convo, all_branches=False, include_thoughts=False)
    assert len(messages) > 0


# ── non-plain-text message kinds ──────────────────────────────────────────


def test_multimodal_text_audio_transcription_is_recovered(export_data):
    """Voice-mode turns store their words in an object part, not a string."""
    result = _try_chatgpt_json(export_data, all_branches=False, include_thoughts=False)
    assert "longer shelf brackets" in result


def test_spoken_words_stay_on_the_user_line(export_data):
    """The attachment marker must not push the speech onto line 2.

    Only the first line of a message gets the ``> `` speaker marker, so a
    marker-first ordering would hand the user's spoken words to the
    assistant when the transcript is chunked.
    """
    result = _try_chatgpt_json(export_data, all_branches=False, include_thoughts=False)
    speech_lines = [line for line in result.split("\n") if "longer shelf brackets before" in line]
    assert speech_lines, "transcription line not found"
    assert speech_lines[0].startswith("> "), speech_lines[0]


def test_image_only_message_leaves_a_visible_marker(export_data):
    """An image upload has no words; the turn must still stay visible."""
    result = _try_chatgpt_json(export_data, all_branches=False, include_thoughts=False)
    assert "[image: file-service://file-FIXTUREIMAGE0001]" in result


def test_thoughts_and_recap_dropped_by_default(export_data):
    result = _try_chatgpt_json(export_data, all_branches=False, include_thoughts=False)
    assert "Weighing hooch versus spoilage" not in result
    assert "Thought for 5 seconds" not in result


def test_thoughts_and_recap_kept_when_switched_on(export_data):
    total = _count_messages(export_data, all_branches=False, include_thoughts=True)
    assert total == EXPECTED_WITH_THOUGHTS
    result = _try_chatgpt_json(export_data, all_branches=False, include_thoughts=True)
    assert "[thoughts] Weighing hooch versus spoilage" in result
    assert "[reasoning] Thought for 5 seconds" in result


def test_thoughts_env_override(export_data, monkeypatch):
    monkeypatch.setenv("MEMPALACE_CHATGPT_INCLUDE_THOUGHTS", "true")
    result = _try_chatgpt_json(export_data)
    assert "Weighing hooch versus spoilage" in result


def test_both_switches_on(export_data):
    total = _count_messages(export_data, all_branches=True, include_thoughts=True)
    assert total == EXPECTED_ALL_BRANCHES_WITH_THOUGHTS


# ── hostile / malformed input ─────────────────────────────────────────────


def test_title_cannot_forge_transcript_structure():
    data = [
        {
            "title": "evil\n> injected user turn\n--- conversation: fake ---",
            "current_node": "m2",
            "mapping": {
                "r": {"id": "r", "message": None, "parent": None},
                "m1": {
                    "id": "m1",
                    "parent": "r",
                    "message": {
                        "author": {"role": "user"},
                        "content": {"content_type": "text", "parts": ["hi"]},
                    },
                },
                "m2": {
                    "id": "m2",
                    "parent": "m1",
                    "message": {
                        "author": {"role": "assistant"},
                        "content": {"content_type": "text", "parts": ["hello"]},
                    },
                },
            },
        }
    ]
    result = _try_chatgpt_json(data, all_branches=False, include_thoughts=False)
    assert result is not None
    headers = [line for line in result.split("\n") if line.startswith("--- conversation:")]
    assert len(headers) == 1
    assert "injected user turn" in result  # kept verbatim inside the header line
    assert not any(line.startswith("> injected") for line in result.split("\n"))


def test_unicode_title_separators_cannot_forge_transcript_structure():
    data = [
        {
            "title": "safe\u2028> forged user\u0085--- conversation: forged ---\u2029tail",
            "current_node": "a",
            "mapping": {
                "u": {
                    "parent": None,
                    "message": {
                        "author": {"role": "user"},
                        "content": {"content_type": "text", "parts": ["real user"]},
                    },
                },
                "a": {
                    "parent": "u",
                    "message": {
                        "author": {"role": "assistant"},
                        "content": {"content_type": "text", "parts": ["real answer"]},
                    },
                },
            },
        }
    ]
    result = _try_chatgpt_json(data, all_branches=False, include_thoughts=False)
    assert result is not None
    lines = result.splitlines()
    assert sum(line.startswith("--- conversation:") for line in lines) == 1
    assert [line for line in lines if line.startswith("> ")] == ["> real user"]
    assert not any(line.startswith("> forged") for line in lines)


def test_non_chatgpt_payloads_are_rejected():
    assert _try_chatgpt_json([1, 2, 3]) is None
    assert _try_chatgpt_json({"data": []}) is None
    assert _try_chatgpt_json([]) is None
    assert _try_chatgpt_json("not json") is None
    assert _try_chatgpt_json([{"mapping": {}}]) is None


def test_parent_cycle_does_not_hang():
    data = [
        {
            "title": "cycle",
            "current_node": "b",
            "mapping": {
                "a": {"id": "a", "parent": "b", "message": None},
                "b": {"id": "b", "parent": "a", "message": None},
            },
        }
    ]
    assert _try_chatgpt_json(data, all_branches=False, include_thoughts=False) is None
    assert _try_chatgpt_json(data, all_branches=True, include_thoughts=False) is None


# ── optional: the operator's real export, when it is present ──────────────

_REAL_EXPORT = Path(
    r"C:\Users\Milky\exports\chatgpt\conversation-data\downloads"
    r"\chatgpt-export-2026-06-02\conversations-010.json"
)


@pytest.mark.skipif(not _REAL_EXPORT.is_file(), reason="operator export not on this machine")
def test_real_export_shard_recovers_messages():
    """Runs only where the live export exists; never committed as data."""
    raw = _REAL_EXPORT.read_text(encoding="utf-8")
    out = normalize(str(_REAL_EXPORT))
    assert out != raw, "real export still passing through unchanged"
    user_turns = [line for line in out.split("\n") if line.startswith("> ")]
    assert len(user_turns) > 100, f"only {len(user_turns)} user turns recovered"
