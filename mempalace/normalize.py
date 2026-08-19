#!/usr/bin/env python3
"""
normalize.py — Convert any chat export format to MemPalace transcript format.

Supported:
    - Plain text with > markers (pass through)
    - Claude.ai JSON export
    - ChatGPT conversations.json
    - Claude Code JSONL (with tool_use/tool_result block capture)
    - OpenAI Codex CLI JSONL
    - Gemini CLI JSONL (~/.gemini/tmp/<project_hash>/chats/session-*.jsonl)
    - Slack JSON export
    - Plain text (pass through for paragraph chunking)

No API key. No internet. Everything local.
"""

import json
import os
import re
from pathlib import Path
from typing import Optional

# Provenance footer appended to Slack transcript output so downstream consumers
# know the speaker roles are positionally assigned, not verified.
_SLACK_PROVENANCE_FOOTER = (
    "\n[source: slack-export | multi-party chat — speaker roles are positional, not verified]"
)


# ─── Noise stripping ─────────────────────────────────────────────────────
# Claude Code and other tools inject system tags, hook output, and UI chrome
# into transcripts. These waste drawer space and pollute search results.
#
# Verbatim is sacred — every pattern here is anchored to line boundaries and
# refuses to cross blank lines, so a stray unclosed tag in one message can
# never eat content from neighboring messages. When in doubt, leave text
# alone.

_NOISE_TAGS = (
    "system-reminder",
    "command-message",
    "command-name",
    "task-notification",
    "user-prompt-submit-hook",
    "hook_output",
)


def _tag_pattern(name: str) -> "re.Pattern[str]":
    # Opening tag must begin a line (optionally after a `> ` blockquote marker,
    # since _messages_to_transcript prefixes lines with `> `). Body is lazy but
    # forbidden from crossing a blank line, so a dangling open tag can't span
    # multiple messages. Closing tag eats optional trailing whitespace + newline.
    return re.compile(
        rf"(?m)^(?:> )?<{name}(?:\s[^>]*)?>" rf"(?:(?!\n\s*\n)[\s\S])*?" rf"</{name}>[ \t]*\n?"
    )


_NOISE_TAG_PATTERNS = [_tag_pattern(t) for t in _NOISE_TAGS]

# Strings that identify an entire noise line when found at its start.
# Matched case-sensitively and anchored to line-start so user prose mentioning
# e.g. "current time:" in a sentence is untouched.
_NOISE_LINE_PREFIXES = (
    "CURRENT TIME:",
    "VERIFIED FACTS (do not contradict)",
    "AGENT SPECIALIZATION:",
    "Checking verified facts...",
    "Injecting timestamp...",
    "Starting background pipeline...",
    "Checking emotional weights...",
    "Auto-save reminder...",
    "Checking pipeline...",
    "MemPalace auto-save checkpoint.",
)

_NOISE_LINE_PATTERNS = [
    re.compile(rf"(?m)^(?:> )?{re.escape(p)}.*\n?") for p in _NOISE_LINE_PREFIXES
]

# Claude Code TUI hook-run chrome, e.g. "Ran 2 Stop hook", "Ran 1 PreCompact hook".
# Line-anchored, case-sensitive, explicit hook names — prose like
# "our CI has a stop hook" stays intact.
_HOOK_LINE_RE = re.compile(
    r"(?m)^(?:> )?Ran \d+ (?:Stop|PreCompact|PreToolUse|PostToolUse|UserPromptSubmit|Notification|SessionStart|SessionEnd) hook[s]?.*\n?"
)

# "… +N lines" collapsed-output marker, line-anchored.
_COLLAPSED_LINES_RE = re.compile(r"(?m)^(?:> )?…\s*\+\d+ lines.*\n?")


def strip_noise(text: str) -> str:
    """Remove system tags, hook output, and Claude Code UI chrome from text.

    All patterns are line-anchored. User prose that happens to mention these
    strings inline (e.g., documenting them) is preserved verbatim.
    """
    for pat in _NOISE_TAG_PATTERNS:
        text = pat.sub("", text)
    for pat in _NOISE_LINE_PATTERNS:
        text = pat.sub("", text)
    text = _HOOK_LINE_RE.sub("", text)
    text = _COLLAPSED_LINES_RE.sub("", text)
    # Strip the Claude Code collapsed-output chrome "[N tokens] (ctrl+o to expand)".
    # Narrow shape — a bare "(ctrl+o to expand)" in user prose stays intact.
    text = re.sub(r"\s*\[\d+\s+tokens?\]\s*\(ctrl\+o to expand\)", "", text)
    # Collapse runs of blank lines created by the removals
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


def normalize(filepath: str, *, source_bytes: Optional[bytes] = None) -> str:
    """
    Load a file and normalize to transcript format if it's a chat export.
    Plain text files pass through unchanged.

    ``source_bytes`` lets managed mining bind a receipt hash to the exact
    bytes normalized, avoiding a second-read race between hashing and parsing.
    """
    if source_bytes is None:
        try:
            file_size = os.path.getsize(filepath)
        except OSError as e:
            raise IOError(f"Could not read {filepath}: {e}")
    else:
        file_size = len(source_bytes)
    if file_size > 500 * 1024 * 1024:  # 500 MB safety limit
        raise IOError(f"File too large ({file_size // (1024 * 1024)} MB): {filepath}")
    if source_bytes is None:
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError as e:
            raise IOError(f"Could not read {filepath}: {e}")
    else:
        content = source_bytes.decode("utf-8", errors="replace")
        content = content.replace("\r\n", "\n").replace("\r", "\n")

    if not content.strip():
        return content

    # Already has > markers — pass through unchanged.
    lines = content.split("\n")
    if sum(1 for line in lines if line.strip().startswith(">")) >= 3:
        return content

    # Try JSON normalization. strip_noise is applied inside the Claude Code
    # JSONL parser (the only format that injects system tags/hook chrome);
    # other formats pass through verbatim.
    ext = Path(filepath).suffix.lower()
    if ext in (".json", ".jsonl") or content.strip()[:1] in ("{", "["):
        normalized = _try_normalize_json(content)
        if normalized:
            return normalized

    return content


def _try_normalize_json(content: str) -> Optional[str]:
    """Try all known JSON chat schemas."""

    normalized = _try_claude_code_jsonl(content)
    if normalized:
        return normalized

    normalized = _try_codex_jsonl(content)
    if normalized:
        return normalized

    normalized = _try_gemini_jsonl(content)
    if normalized:
        return normalized

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None

    # ChatGPT runs first: its signature (a ``mapping`` dict) is unambiguous,
    # so it can never steal a Claude.ai or Slack file. The reverse is not
    # true — _try_claude_ai_json walks *any* top-level list looking for
    # role/sender keys, and a ChatGPT account export is a top-level list.
    for parser in (_try_chatgpt_json, _try_claude_ai_json, _try_slack_json):
        normalized = parser(data)
        if normalized:
            return normalized

    return None


def _try_claude_code_jsonl(content: str) -> Optional[str]:
    """Claude Code JSONL sessions."""
    lines = [line.strip() for line in content.strip().split("\n") if line.strip()]
    messages = []
    tool_use_map = {}  # tool_use_id → tool_name

    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        msg_type = entry.get("type", "")
        message = entry.get("message", {})
        if not isinstance(message, dict):
            continue
        msg_content = message.get("content", "")

        # Build tool_use_map from assistant messages
        if msg_type == "assistant" and isinstance(msg_content, list):
            for block in msg_content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_id = block.get("id", "")
                    if tool_id:
                        tool_use_map[tool_id] = block.get("name", "Unknown")

        if msg_type in ("human", "user"):
            # Check if this message is tool_results only (no user text)
            is_tool_only = isinstance(msg_content, list) and all(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in msg_content
            )
            text = _extract_content(msg_content, tool_use_map=tool_use_map)
            # Strip Claude Code system-injected noise per message, never across
            # message boundaries — prevents span-eating.
            if text:
                text = strip_noise(text)
            if text:
                if is_tool_only and messages and messages[-1][0] == "assistant":
                    # Append tool results to the previous assistant message
                    prev_role, prev_text = messages[-1]
                    messages[-1] = (prev_role, prev_text + "\n" + text)
                elif not is_tool_only:
                    messages.append(("user", text))
        elif msg_type == "assistant":
            text = _extract_content(msg_content, tool_use_map=tool_use_map)
            if text:
                text = strip_noise(text)
            if text:
                # If previous message is also assistant (multi-turn tool loop),
                # merge into the same assistant turn
                if messages and messages[-1][0] == "assistant":
                    prev_role, prev_text = messages[-1]
                    messages[-1] = (prev_role, prev_text + "\n" + text)
                else:
                    messages.append(("assistant", text))

    if len(messages) >= 2:
        return _messages_to_transcript(messages)
    return None


def _try_codex_jsonl(content: str) -> Optional[str]:
    """OpenAI Codex CLI sessions (~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl).

    Uses only event_msg entries (user_message / agent_message) which represent
    the canonical conversation turns. response_item entries are skipped because
    they include synthetic context injections and duplicate the real messages.
    """
    lines = [line.strip() for line in content.strip().split("\n") if line.strip()]
    messages = []
    has_session_meta = False
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue

        entry_type = entry.get("type", "")
        if entry_type == "session_meta":
            has_session_meta = True
            continue

        if entry_type != "event_msg":
            continue

        payload = entry.get("payload", {})
        if not isinstance(payload, dict):
            continue

        payload_type = payload.get("type", "")
        msg = payload.get("message")
        if not isinstance(msg, str):
            continue
        text = msg.strip()
        if not text:
            continue

        if payload_type == "user_message":
            messages.append(("user", text))
        elif payload_type == "agent_message":
            messages.append(("assistant", text))

    if len(messages) >= 2 and has_session_meta:
        return _messages_to_transcript(messages)
    return None


def _try_gemini_jsonl(content: str) -> Optional[str]:
    """Gemini CLI sessions (~/.gemini/tmp/<project_hash>/chats/session-*.jsonl).

    Schema (per google-gemini/gemini-cli#15292): a session_metadata record
    on the first line, then a stream of ``{"type": "user", "content":
    [{"text": "..."}]}`` and ``{"type": "gemini", "content": [...]}``
    records, with optional ``message_update`` records carrying token
    counts only.

    Detection requires a ``session_metadata`` record so this parser does
    not false-positive against Claude Code or Codex JSONL passed through
    the dispatch chain. Any ``user``/``gemini`` lines that appear before
    ``session_metadata`` are discarded — they are treated as preamble
    noise, not conversational turns. ``message_update`` entries are
    skipped — they have no message text. Multiple text blocks within a
    single message's content array are concatenated in order, separated
    by newlines.
    """
    lines = [line.strip() for line in content.strip().split("\n") if line.strip()]
    messages = []
    has_session_metadata = False
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue

        entry_type = entry.get("type", "")
        if entry_type == "session_metadata":
            has_session_metadata = True
            continue

        # Discard everything (including user/gemini turns) until the
        # session_metadata sentinel has been seen.
        if not has_session_metadata:
            continue

        if entry_type not in ("user", "gemini"):
            # Skips message_update, system events, anything else.
            continue

        content_blocks = entry.get("content", [])
        if not isinstance(content_blocks, list):
            continue

        parts = []
        for block in content_blocks:
            if not isinstance(block, dict):
                continue
            text = block.get("text", "")
            if isinstance(text, str) and text.strip():
                parts.append(text)
        if not parts:
            continue
        joined = "\n".join(parts)

        if entry_type == "user":
            messages.append(("user", joined))
        else:  # "gemini"
            messages.append(("assistant", joined))

    if len(messages) >= 2 and has_session_metadata:
        return _messages_to_transcript(messages)
    return None


def _try_claude_ai_json(data) -> Optional[str]:
    """Claude.ai JSON export: flat messages list or privacy export with chat_messages."""
    if isinstance(data, dict):
        data = data.get("messages", data.get("chat_messages", []))
    if not isinstance(data, list):
        return None

    # Privacy export: array of conversation objects, each containing its own
    # message list under "chat_messages" or "messages" (both variants seen in the wild).
    if data and isinstance(data[0], dict) and ("chat_messages" in data[0] or "messages" in data[0]):
        transcripts = []
        for convo in data:
            if not isinstance(convo, dict):
                continue
            chat_msgs = convo.get("chat_messages") or convo.get("messages", [])
            messages = _collect_claude_messages(chat_msgs)
            if len(messages) >= 2:
                transcripts.append(_messages_to_transcript(messages))
        if transcripts:
            return "\n\n".join(transcripts)
        return None

    # Flat messages list
    messages = _collect_claude_messages(data)
    if len(messages) >= 2:
        return _messages_to_transcript(messages)
    return None


def _collect_claude_messages(items) -> list:
    """Extract (role, text) pairs from a Claude.ai message list.

    Accepts both ``role`` (API format) and ``sender`` (privacy export) as the
    author field, and falls back to a top-level ``text`` key when the
    ``content`` blocks are empty or absent.
    """
    messages = []
    for item in items:
        if not isinstance(item, dict):
            continue
        role = item.get("role") or item.get("sender", "")
        text = _extract_content(item.get("content", "")) or (item.get("text") or "").strip()
        if role in ("user", "human") and text:
            messages.append(("user", text))
        elif role in ("assistant", "ai") and text:
            messages.append(("assistant", text))
    return messages


# ─── ChatGPT export ──────────────────────────────────────────────────────
#
# Two shapes are seen in the wild and both are supported:
#
#   1. a single conversation object            {"title": ..., "mapping": {...}}
#   2. an account-export shard: a LIST of      [{...}, {...}, ...]
#      conversation objects (this is what
#      "Export data" produces — conversations-000.json … -0NN.json)
#
# TREE TRAVERSAL — why we walk backwards.
#
# A ChatGPT conversation is a tree, not a list: editing a prompt or hitting
# "regenerate" forks a new branch, and the old branch stays in the file. The
# branch you actually see in the app is the one ending at ``current_node``.
#
# Measured against a real 1,023-conversation export (2026-06-02, 11 shards):
#   - 24,356 mapping nodes total
#   -      0 nodes carry a ``children`` key
#   - 24,356 nodes carry a ``parent`` key
#   -  1,023 conversations carry ``current_node``
#
# So the only reliable walk is: start at ``current_node`` and follow
# ``parent`` back to the root, then reverse. Older exports that *do* carry
# ``children`` still work — those lists are used to find leaves, and the
# same backward walk then runs.
#
# TWO EXPLICIT SWITCHES (both default OFF — see docstrings for the reasoning):
#
#   all_branches    — index every branch in the tree, including prompts that
#                     were edited away or answers that were regenerated over.
#                     Env override: MEMPALACE_CHATGPT_ALL_BRANCHES=1
#   include_thoughts— keep reasoning-model internals (``thoughts`` blocks and
#                     "Thought for 8 seconds" recaps).
#                     Env override: MEMPALACE_CHATGPT_INCLUDE_THOUGHTS=1

_CHATGPT_ALL_BRANCHES_ENV = "MEMPALACE_CHATGPT_ALL_BRANCHES"
_CHATGPT_INCLUDE_THOUGHTS_ENV = "MEMPALACE_CHATGPT_INCLUDE_THOUGHTS"

# Author roles that represent a real speaker. ``system`` and ``tool`` are
# platform plumbing (custom-instruction injections, browsing scaffolding) and
# are dropped, matching the previous behavior.
_CHATGPT_ROLES = {"user": "user", "human": "user", "assistant": "assistant"}

# Non-text attachment kinds inside a ``multimodal_text`` message. These carry
# no words, only a file pointer. We emit a short marker instead of dropping
# the turn, so an image-only message stays visible in the transcript rather
# than vanishing silently (silent vanishing is the exact failure this parser
# is being fixed for).
_CHATGPT_ASSET_LABELS = {
    "image_asset_pointer": "image",
    "audio_asset_pointer": "audio",
    "video_container_asset_pointer": "video",
    "real_time_user_audio_video_asset_pointer": "audio/video",
}

_CHATGPT_TITLE_MAX = 120


def _env_flag(name: str, default: bool = False) -> bool:
    """Read a boolean switch from the environment. Unset → ``default``."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _try_chatgpt_json(
    data,
    *,
    all_branches: Optional[bool] = None,
    include_thoughts: Optional[bool] = None,
) -> Optional[str]:
    """ChatGPT export — a single conversation object or a list of them.

    Args:
        data: Parsed JSON. Either one conversation dict carrying ``mapping``,
            or a list of such dicts (the account-export shard shape).
        all_branches: ``False`` (default) indexes only the live thread — the
            branch ending at ``current_node``, i.e. what the conversation
            looks like in the app today. ``True`` indexes every branch,
            including prompts that were edited away and answers that were
            regenerated over. Default off because those branches are drafts
            the user replaced on purpose; indexing them makes search return
            superseded text alongside the real answer. ``None`` reads the
            ``MEMPALACE_CHATGPT_ALL_BRANCHES`` environment variable.
        include_thoughts: ``False`` (default) drops reasoning-model internals
            (``thoughts`` blocks and ``reasoning_recap`` chrome such as
            "Thought for 8 seconds"). Default off because those are the
            model's private scratchpad, not anything either party said, and
            in the measured export they are 22% of all message nodes — enough
            to swamp search results with drafts. ``None`` reads the
            ``MEMPALACE_CHATGPT_INCLUDE_THOUGHTS`` environment variable.

    Returns:
        Transcript text, or ``None`` when the data is not a ChatGPT export or
        yielded no usable conversation.
    """
    if all_branches is None:
        all_branches = _env_flag(_CHATGPT_ALL_BRANCHES_ENV, False)
    if include_thoughts is None:
        include_thoughts = _env_flag(_CHATGPT_INCLUDE_THOUGHTS_ENV, False)

    conversations, is_export_shard = _chatgpt_conversations(data)
    if not conversations:
        return None

    # A list of dicts each carrying a ``mapping`` is an unambiguous ChatGPT
    # export, so a one-message conversation is real data, not a false
    # positive. A bare dict could be anything, so the historical 2-message
    # floor is kept there as format-detection safety.
    min_messages = 1 if is_export_shard else 2

    transcripts = []
    for convo in conversations:
        messages = _chatgpt_conversation_messages(
            convo,
            all_branches=all_branches,
            include_thoughts=include_thoughts,
        )
        if len(messages) < min_messages:
            continue
        transcript = _messages_to_transcript(messages)
        title = _chatgpt_title(convo)
        if title:
            # A `---` line is a hard chunk boundary for convo_miner's
            # exchange chunker, so one drawer can never straddle two
            # different conversations.
            transcript = f"--- conversation: {title} ---\n{transcript}"
        transcripts.append(transcript)

    if not transcripts:
        return None
    return "\n".join(transcripts)


def _chatgpt_conversations(data) -> tuple:
    """Return ``(conversations, is_export_shard)``.

    ``is_export_shard`` is True when the input was a top-level list, which
    only the account export produces.
    """
    if isinstance(data, dict):
        return ([data] if isinstance(data.get("mapping"), dict) else [], False)
    if isinstance(data, list):
        found = [c for c in data if isinstance(c, dict) and isinstance(c.get("mapping"), dict)]
        return (found, bool(found))
    return ([], False)


def _chatgpt_title(convo: dict) -> str:
    """Sanitized conversation title, or "" when absent.

    Newlines and control characters are stripped so a crafted title cannot
    forge transcript structure (extra ``>`` turns or ``---`` boundaries).
    """
    title = convo.get("title")
    if not isinstance(title, str):
        return ""
    title = re.sub(r"[\n\r\x00-\x1f]", " ", title).strip()
    title = re.sub(r"-{3,}", "--", title)
    if len(title) > _CHATGPT_TITLE_MAX:
        title = title[:_CHATGPT_TITLE_MAX].rstrip() + "…"
    return title


def _chatgpt_node_sort_key(mapping: dict, node_id):
    """Deterministic ordering key: message create_time, then node id."""
    node = mapping.get(node_id) or {}
    msg = node.get("message") if isinstance(node, dict) else None
    raw = (msg or {}).get("create_time") if isinstance(msg, dict) else None
    try:
        created = float(raw)
    except (TypeError, ValueError):
        created = 0.0
    return (created, str(node_id))


def _chatgpt_child_index(mapping: dict) -> dict:
    """node_id → list of child node ids.

    Uses explicit ``children`` lists when the export has them (older format).
    The 2026 export has none, so the index is rebuilt from every node's
    ``parent`` pointer and ordered by create_time for determinism.
    """
    children = {node_id: [] for node_id in mapping}
    explicit = False
    for node_id, node in mapping.items():
        if not isinstance(node, dict):
            continue
        kids = node.get("children")
        if isinstance(kids, list):
            kept = [k for k in kids if k in mapping]
            if kept:
                explicit = True
                children[node_id] = kept
    if explicit:
        return children

    for node_id, node in mapping.items():
        if not isinstance(node, dict):
            continue
        parent = node.get("parent")
        if isinstance(parent, str) and parent in children and parent != node_id:
            children[parent].append(node_id)
    for node_id in children:
        children[node_id].sort(key=lambda n: _chatgpt_node_sort_key(mapping, n))
    return children


def _chatgpt_live_chain(mapping: dict, current_node, child_index: dict) -> list:
    """Node ids from root to ``current_node``, following ``parent`` backwards.

    When ``current_node`` is missing or dangling, the newest leaf (no
    children, latest create_time) is used as the endpoint instead.
    """
    start = current_node if isinstance(current_node, str) and current_node in mapping else None
    if start is None:
        leaves = [n for n in mapping if not child_index.get(n)]
        if leaves:
            start = max(leaves, key=lambda n: _chatgpt_node_sort_key(mapping, n))
    chain = []
    seen = set()
    cursor = start
    while isinstance(cursor, str) and cursor in mapping and cursor not in seen:
        seen.add(cursor)
        chain.append(cursor)
        node = mapping.get(cursor)
        cursor = node.get("parent") if isinstance(node, dict) else None
    chain.reverse()
    return chain


def _chatgpt_all_nodes(mapping: dict, child_index: dict) -> list:
    """Every node, depth-first from each root — includes abandoned branches."""
    roots = []
    for node_id, node in mapping.items():
        parent = node.get("parent") if isinstance(node, dict) else None
        if not isinstance(parent, str) or parent not in mapping:
            roots.append(node_id)
    roots.sort(key=lambda n: _chatgpt_node_sort_key(mapping, n))

    ordered = []
    seen = set()
    stack = list(reversed(roots))
    while stack:
        node_id = stack.pop()
        if node_id in seen:
            continue
        seen.add(node_id)
        ordered.append(node_id)
        for kid in reversed(child_index.get(node_id, [])):
            if kid not in seen:
                stack.append(kid)

    # Anything unreachable (cycle, corrupt parent pointer) is still emitted —
    # dropping nodes silently is the failure mode this parser is fixing.
    for node_id in sorted(mapping, key=lambda n: _chatgpt_node_sort_key(mapping, n)):
        if node_id not in seen:
            seen.add(node_id)
            ordered.append(node_id)
    return ordered


def _chatgpt_conversation_messages(
    convo: dict, *, all_branches: bool, include_thoughts: bool
) -> list:
    """Extract ``[(role, text), ...]`` from one ChatGPT conversation."""
    mapping = convo.get("mapping")
    if not isinstance(mapping, dict) or not mapping:
        return []
    child_index = _chatgpt_child_index(mapping)
    if all_branches:
        node_ids = _chatgpt_all_nodes(mapping, child_index)
    else:
        node_ids = _chatgpt_live_chain(mapping, convo.get("current_node"), child_index)

    messages = []
    for node_id in node_ids:
        node = mapping.get(node_id)
        if not isinstance(node, dict):
            continue
        msg = node.get("message")
        if not isinstance(msg, dict):
            continue
        author = msg.get("author")
        raw_role = (author or {}).get("role") if isinstance(author, dict) else None
        role = _CHATGPT_ROLES.get(str(raw_role or "").lower())
        if role is None:
            continue
        text = _chatgpt_message_text(msg.get("content"), include_thoughts=include_thoughts)
        if text:
            messages.append((role, text))
    return messages


def _chatgpt_message_text(content, *, include_thoughts: bool) -> str:
    """Text of one ChatGPT message, across every content kind in the export.

    Measured content_type distribution in the 2026-06 export (23,333 message
    nodes): text 14,642 · thoughts 4,379 · multimodal_text 3,594 ·
    reasoning_recap 718. Only ``text`` was handled before this fix.
    """
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, dict):
        return ""

    content_type = str(content.get("content_type") or "")

    if content_type in ("thoughts", "reasoning_recap"):
        if not include_thoughts:
            return ""
        return _chatgpt_reasoning_text(content, content_type)

    parts = content.get("parts")
    if isinstance(parts, list):
        # Words first, attachment markers after. A voice turn stores the
        # asset pointer before the transcription, and _messages_to_transcript
        # only puts the FIRST line behind the `> ` marker — so leaving the
        # marker first would push the actual speech onto line 2, where the
        # exchange chunker reads it as the assistant's reply.
        words, markers = [], []
        for part in parts:
            piece = _chatgpt_part_text(part)
            if not piece:
                continue
            (markers if _is_chatgpt_asset_marker(piece) else words).append(piece)
        pieces = words + markers
        if pieces:
            # Keep a one-line message on one line so speaker attribution
            # survives chunking; only genuinely multi-line bodies get
            # newline-joined.
            sep = "\n" if any("\n" in p for p in pieces) else " "
            return sep.join(pieces).strip()

    # Kinds that carry their body outside ``parts`` (code, execution output,
    # tether quotes). Unknown future kinds land here too rather than vanishing.
    for key in ("text", "result", "content"):
        value = content.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _chatgpt_part_text(part) -> str:
    """One element of a ``parts`` array — plain string, or an object.

    In ``multimodal_text`` messages the objects are either an audio
    transcription (real words, under ``text``) or an attachment pointer
    (no words). Measured in the 2026-06 export: audio_transcription 3,143 ·
    audio_asset_pointer 1,515 · real_time_user_audio_video_asset_pointer
    1,514 · image_asset_pointer 658.
    """
    if isinstance(part, str):
        return part.strip()
    if not isinstance(part, dict):
        return ""

    text = part.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()

    label = _CHATGPT_ASSET_LABELS.get(str(part.get("content_type") or ""))
    if not label:
        return ""
    pointer = part.get("asset_pointer")
    if isinstance(pointer, str) and pointer.strip():
        pointer = re.sub(r"[\n\r\x00-\x1f\[\]]", "", pointer).strip()[:120]
        if pointer:
            return f"[{label}: {pointer}]"
    return f"[{label}]"


def _is_chatgpt_asset_marker(piece: str) -> bool:
    """True for a marker this module generated for a wordless attachment."""
    if not piece.startswith("[") or not piece.endswith("]") or "\n" in piece:
        return False
    inner = piece[1:-1].split(":", 1)[0].strip()
    return inner in _CHATGPT_ASSET_LABELS.values()


def _chatgpt_reasoning_text(content: dict, content_type: str) -> str:
    """Reasoning-model internals, only reached when include_thoughts is on."""
    if content_type == "reasoning_recap":
        value = content.get("content")
        if isinstance(value, str) and value.strip():
            return f"[reasoning] {value.strip()}"
        return ""

    lines = []
    for thought in content.get("thoughts") or []:
        if not isinstance(thought, dict):
            continue
        pieces = [
            v.strip()
            for v in (thought.get("summary"), thought.get("content"))
            if isinstance(v, str) and v.strip()
        ]
        if pieces:
            lines.append(" — ".join(pieces))
    if not lines:
        return ""
    return "[thoughts] " + "\n".join(lines)


def _try_slack_json(data) -> Optional[str]:
    """
    Slack channel export: [{"type": "message", "user": "...", "text": "..."}]

    Slack exports are multi-party chats where no speaker is inherently the
    "user" or "assistant".  To preserve exchange-pair chunking (which relies
    on ``>`` markers from the ``user`` role), we still alternate roles, but
    prefix each message with the speaker ID so downstream consumers can
    distinguish the original author.  A provenance header marks the
    transcript as a Slack import.
    """
    if not isinstance(data, list):
        return None
    messages = []
    seen_users = {}
    last_role = None
    for item in data:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        raw_user_id = item.get("user", item.get("username", ""))
        # Sanitize speaker ID: strip brackets, newlines, and control chars
        # to prevent chunk-boundary injection via crafted exports
        user_id = re.sub(r"[\[\]\n\r\x00-\x1f]", "_", raw_user_id).strip()
        text = item.get("text", "").strip()
        if not text or not user_id:
            continue
        if user_id not in seen_users:
            # Alternate roles so exchange chunking works with any number of speakers
            if not seen_users:
                seen_users[user_id] = "user"
            elif last_role == "user":
                seen_users[user_id] = "assistant"
            else:
                seen_users[user_id] = "user"
        last_role = seen_users[user_id]
        # Prefix with speaker ID so the original author is preserved
        messages.append((seen_users[user_id], f"[{user_id}] {text}"))
    if len(messages) >= 2:
        return _messages_to_transcript(messages) + _SLACK_PROVENANCE_FOOTER
    return None


def _extract_content(content, tool_use_map: dict = None) -> str:
    """Pull text from content — handles str, list of blocks, or dict.

    Args:
        content: Message content — string, list of content blocks, or dict.
        tool_use_map: Optional mapping of tool_use_id → tool_name, used to
                      select the right formatting strategy for tool_result blocks.
    """
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                block_type = item.get("type")
                if block_type == "text":
                    parts.append(item.get("text", ""))
                elif block_type == "tool_use":
                    parts.append(_format_tool_use(item))
                elif block_type == "tool_result":
                    tid = item.get("tool_use_id", "")
                    tname = (tool_use_map or {}).get(tid, "Unknown")
                    result_content = item.get("content", "")
                    formatted = _format_tool_result(result_content, tname)
                    if formatted:
                        parts.append(formatted)
        return "\n".join(p for p in parts if p).strip()
    if isinstance(content, dict):
        return content.get("text", "").strip()
    return ""


def _format_tool_use(block: dict) -> str:
    """Format a tool_use block into a human-readable one-liner."""
    name = block.get("name", "Unknown")
    inp = block.get("input", {})

    if name == "Bash":
        cmd = inp.get("command", "")
        if len(cmd) > 200:
            cmd = cmd[:200] + "..."
        return f"[Bash] {cmd}"

    if name == "Read":
        path = inp.get("file_path", "?")
        offset = inp.get("offset")
        limit = inp.get("limit")
        if offset is not None and limit is not None:
            try:
                return f"[Read {path}:{offset}-{int(offset) + int(limit)}]"
            except (ValueError, TypeError):
                return f"[Read {path}:{offset}+{limit}]"
        return f"[Read {path}]"

    if name == "Grep":
        pattern = inp.get("pattern", "")
        target = inp.get("path") or inp.get("glob") or ""
        return f"[Grep] {pattern} in {target}"

    if name == "Glob":
        pattern = inp.get("pattern", "")
        return f"[Glob] {pattern}"

    if name in ("Edit", "Write"):
        path = inp.get("file_path", "?")
        return f"[{name} {path}]"

    # Unknown tool — serialize input, truncate
    summary = json.dumps(inp, separators=(",", ":"))
    if len(summary) > 200:
        summary = summary[:200] + "..."
    return f"[{name}] {summary}"


_TOOL_RESULT_MAX_LINES_BASH = 20  # head and tail line count
_TOOL_RESULT_MAX_MATCHES = 20  # Grep/Glob cap
_TOOL_RESULT_MAX_BYTES = 2048  # fallback cap for unknown tools


def _format_tool_result(content, tool_name: str) -> str:
    """Format a tool_result based on the originating tool's type.

    Args:
        content: Result text (str) or list of content blocks (list of dicts).
        tool_name: Name of the tool that produced this result.

    Returns:
        Formatted string prefixed with ``→ ``, or empty string if omitted.
    """
    # Normalize list-of-blocks to plain text
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif isinstance(item, str):
                parts.append(item)
        text = "\n".join(parts)
    else:
        text = str(content) if content else ""

    text = text.strip()
    if not text:
        return ""

    # Read/Edit/Write — omit result (content is in palace or git)
    if tool_name in ("Read", "Edit", "Write"):
        return ""

    lines = text.split("\n")

    # Bash — head + tail
    if tool_name == "Bash":
        n = _TOOL_RESULT_MAX_LINES_BASH
        if len(lines) <= n * 2:
            return "→ " + "\n→ ".join(lines)
        head = lines[:n]
        tail = lines[-n:]
        omitted = len(lines) - 2 * n
        return (
            "→ "
            + "\n→ ".join(head)
            + f"\n→ ... [{omitted} lines omitted] ..."
            + "\n→ "
            + "\n→ ".join(tail)
        )

    # Grep/Glob — cap matches
    if tool_name in ("Grep", "Glob"):
        cap = _TOOL_RESULT_MAX_MATCHES
        if len(lines) <= cap:
            return "→ " + "\n→ ".join(lines)
        kept = lines[:cap]
        remaining = len(lines) - cap
        return "→ " + "\n→ ".join(kept) + f"\n→ ... [{remaining} more matches]"

    # Unknown — byte cap
    if len(text) > _TOOL_RESULT_MAX_BYTES:
        return "→ " + text[:_TOOL_RESULT_MAX_BYTES] + f"... [truncated, {len(text)} chars]"
    return "→ " + text


def _messages_to_transcript(messages: list, spellcheck: bool = True) -> str:
    """Convert [(role, text), ...] to transcript format with > markers."""
    if spellcheck:
        try:
            from mempalace.spellcheck import spellcheck_user_text

            _fix = spellcheck_user_text
        except ImportError:
            _fix = None
    else:
        _fix = None

    lines = []
    i = 0
    while i < len(messages):
        role, text = messages[i]
        if role == "user":
            if _fix is not None:
                text = _fix(text)
            lines.append(f"> {text}")
            if i + 1 < len(messages) and messages[i + 1][0] == "assistant":
                lines.append(messages[i + 1][1])
                i += 2
            else:
                i += 1
        else:
            lines.append(text)
            i += 1
        lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python normalize.py <filepath>")
        sys.exit(1)
    filepath = sys.argv[1]
    result = normalize(filepath)
    quote_count = sum(1 for line in result.split("\n") if line.strip().startswith(">"))
    print(f"\nFile: {os.path.basename(filepath)}")
    print(f"Normalized: {len(result)} chars | {quote_count} user turns detected")
    print("\n--- Preview (first 20 lines) ---")
    print("\n".join(result.split("\n")[:20]))
