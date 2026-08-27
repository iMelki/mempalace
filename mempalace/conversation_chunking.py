"""Structured, privacy-safe chunking for normalized conversations.

This module is deliberately pure.  It does not open a palace, read a source
file, or call an embedding model.  Normalizers provide :class:`ConversationUnit`
records and the miner persists the returned scalar metadata.
"""

from dataclasses import dataclass
import hashlib
import re
import unicodedata
from typing import Dict, Iterable, List, Optional, Tuple, Union


# v2 (2026-08): two changes to what a stored drawer contains, so drawers built
#               under v1 must be rebuilt to benefit.
#               1. A split inside one long turn now stops at the strongest
#                  nearby structural boundary (closed code fence, paragraph,
#                  end of sentence, line, word) instead of at whichever
#                  boundary happened to sit furthest right — which in practice
#                  was almost always a space, i.e. mid-sentence.
#               2. Every continuation piece of a split user turn keeps its
#                  "> " speaker marker, so no stored drawer is anonymous text
#                  that reads as if the assistant said it.
CONVERSATION_CHUNK_SCHEMA_VERSION = 2
CONVERSATION_CHUNK_BUDGET_METHOD = "structure-aware-chars-v2"

CONVERSATION_CHUNK_METADATA_FIELDS = (
    "source_identity_hash",
    "conversation_id_hash",
    "root_id_hash",
    "user_message_id_hash",
    "assistant_message_id_hash",
    "speaker_role",
    "exchange_index",
    "part_index",
    "part_count",
    "context_inherited",
    "context_truncated",
    "identity_fallback",
    "chunk_schema_version",
    "chunk_budget_method",
    "chunk_budget_limit",
)

_CONTEXT_HEADER = "[user-context]\n"
_CONTEXT_FOOTER = "\n[/user-context]\n"
_FENCE_RE = re.compile(r"(?m)^ {0,3}```[^\n]*(?:\n|$)")
# End of a sentence: closing punctuation, any closing quote or bracket that
# trails it, then the whitespace that separates it from the next sentence.
_SENTENCE_BREAK_RE = re.compile("[.!?…。！？][\"'’”)\\]]*\\s+")
# How much of the budget a split is allowed to give up in exchange for a
# stronger boundary. Boundaries are looked for in the top quarter of the
# budget first, then in the top half, and only then does an exact character
# cut apply. Half is the same floor the previous implementation used.
_BOUNDARY_SEARCH_FLOORS = (0.75, 0.5)

ScalarMetadata = Union[str, int, float, bool]


@dataclass(frozen=True)
class ConversationTurn:
    """One selected speaker turn with an opaque stable identity."""

    role: str
    text: str
    message_id_hash: str
    identity_fallback: bool = False
    context_message_id_hash: str = ""


@dataclass(frozen=True)
class ConversationUnit:
    """One provider conversation, kept isolated from every other unit."""

    title: str
    conversation_id_hash: str
    root_id_hash: str
    identity_fallback: bool
    turns: Tuple[ConversationTurn, ...]


@dataclass(frozen=True)
class ConversationChunk:
    """A bounded drawer payload plus scalar persistence metadata."""

    content: str
    payload: str
    logical_id: str
    metadata: Dict[str, ScalarMetadata]

    def as_miner_chunk(self, chunk_index: int) -> dict:
        """Return the legacy miner mapping without exposing internal helpers."""
        return {
            "content": self.content,
            "chunk_index": chunk_index,
            "logical_chunk_id": self.logical_id,
            **self.metadata,
        }


def privacy_safe_identity(namespace: str, value: str) -> str:
    """Hash an identity under a purpose-specific namespace.

    Provider identifiers and source locators must never be copied into the
    chunk metadata.  The namespace prevents the same raw identifier from
    producing a reusable token across unrelated identity domains.
    """
    material = f"mempalace:{namespace}:v1\0{value}".encode("utf-8")
    return "sha256:" + hashlib.sha256(material).hexdigest()


def conversation_units_text(units: Iterable[ConversationUnit]) -> str:
    """Render units for room detection only; no identity metadata is included."""
    blocks: List[str] = []
    for unit in units:
        lines: List[str] = []
        if unit.title:
            lines.append(f"--- conversation: {_single_line_title(unit.title)} ---")
        for turn in unit.turns:
            prefix = "> " if turn.role == "user" else ""
            lines.append(prefix + turn.text)
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


def chunk_conversation_units(
    units: Iterable[ConversationUnit],
    *,
    source_identity_hash: str,
    chunk_size: int,
    min_chunk_size: int,
) -> List[dict]:
    """Chunk conversations without crossing conversation or exchange boundaries.

    ``structure-aware-chars-v2`` is an explicit compatibility budget, not a
    model-token guarantee.  A turn is never merged with another turn and a
    conversation is never merged with another conversation; a single turn is
    split only when it does not fit on its own, and then it stops at the
    strongest structural boundary available near the budget — a closed code
    fence, a paragraph break, the end of a sentence, a line break, and only
    then a word break.  Every returned ``content`` is bounded by
    ``chunk_size``.
    """
    if chunk_size < 64:
        raise ValueError("conversation chunk_size must be at least 64 characters")
    if min_chunk_size < 0 or min_chunk_size >= chunk_size:
        raise ValueError("min_chunk_size must be non-negative and below chunk_size")

    chunks: List[ConversationChunk] = []
    for unit in units:
        _chunk_unit(
            chunks,
            unit,
            source_identity_hash=source_identity_hash,
            chunk_size=chunk_size,
            min_chunk_size=min_chunk_size,
        )
    return [chunk.as_miner_chunk(index) for index, chunk in enumerate(chunks)]


def split_text_bounded(text: str, limit: int) -> List[str]:
    """Split arbitrary text into exact, ordered, structure-aware prefixes."""
    pieces = []
    remaining = text
    while remaining:
        piece, remaining = _take_bounded(remaining, limit)
        pieces.append(piece)
    return pieces


def _chunk_unit(
    output: List[ConversationChunk],
    unit: ConversationUnit,
    *,
    source_identity_hash: str,
    chunk_size: int,
    min_chunk_size: int,
) -> None:
    pending_user = None
    users_by_id = {turn.message_id_hash: turn for turn in unit.turns if turn.role == "user"}
    exchange_index = 0
    first_chunk_in_conversation = True

    for turn in unit.turns:
        if turn.role == "user":
            if pending_user is not None:
                first_chunk_in_conversation = _emit_standalone_turn(
                    output,
                    unit,
                    pending_user,
                    source_identity_hash=source_identity_hash,
                    exchange_index=exchange_index,
                    chunk_size=chunk_size,
                    min_chunk_size=min_chunk_size,
                    include_title=first_chunk_in_conversation,
                )
                exchange_index += 1
            pending_user = turn
            continue

        if turn.role != "assistant":
            continue

        context_user = users_by_id.get(turn.context_message_id_hash, pending_user)
        if pending_user is not None and context_user is not pending_user:
            first_chunk_in_conversation = _emit_standalone_turn(
                output,
                unit,
                pending_user,
                source_identity_hash=source_identity_hash,
                exchange_index=exchange_index,
                chunk_size=chunk_size,
                min_chunk_size=min_chunk_size,
                include_title=first_chunk_in_conversation,
            )
            pending_user = None
            exchange_index += 1
        first_chunk_in_conversation = _emit_exchange(
            output,
            unit,
            context_user,
            turn,
            source_identity_hash=source_identity_hash,
            exchange_index=exchange_index,
            chunk_size=chunk_size,
            min_chunk_size=min_chunk_size,
            include_title=first_chunk_in_conversation,
        )
        if context_user is pending_user:
            pending_user = None
        exchange_index += 1

    if pending_user is not None:
        _emit_standalone_turn(
            output,
            unit,
            pending_user,
            source_identity_hash=source_identity_hash,
            exchange_index=exchange_index,
            chunk_size=chunk_size,
            min_chunk_size=min_chunk_size,
            include_title=first_chunk_in_conversation,
        )


def _emit_exchange(
    output: List[ConversationChunk],
    unit: ConversationUnit,
    user_turn: ConversationTurn,
    assistant_turn: ConversationTurn,
    *,
    source_identity_hash: str,
    exchange_index: int,
    chunk_size: int,
    min_chunk_size: int,
    include_title: bool,
) -> bool:
    title_prefix = _title_prefix(unit, include_title, chunk_size)
    user_prefix = f"> {user_turn.text}\n" if user_turn is not None else ""
    first_prefix = title_prefix + user_prefix

    # A very long question cannot leave a useful answer payload inside the
    # same bounded drawer. Preserve the entire question in standalone chunks,
    # then use a labelled bounded context excerpt for every answer part.
    if user_turn is not None and len(first_prefix) >= chunk_size:
        _emit_standalone_turn(
            output,
            unit,
            user_turn,
            source_identity_hash=source_identity_hash,
            exchange_index=exchange_index,
            chunk_size=chunk_size,
            min_chunk_size=min_chunk_size,
            include_title=include_title,
        )
        first_prefix = ""
        include_title = False

    context, context_truncated = _context_envelope(
        user_turn.text if user_turn is not None else "",
        chunk_size,
    )
    inherited_prefix = context if user_turn is not None else ""
    payloads = _plan_exchange_payloads(
        assistant_turn.text,
        first_prefix=first_prefix,
        inherited_prefix=inherited_prefix,
        context_truncated=context_truncated,
        chunk_size=chunk_size,
        min_chunk_size=min_chunk_size,
    )
    if not payloads:
        return include_title

    for part_index, (payload, context_inherited, inherited_truncated) in enumerate(payloads):
        content_prefix = first_prefix if part_index == 0 and first_prefix else inherited_prefix
        _append_chunk(
            output,
            unit,
            payload=payload,
            content=content_prefix + payload,
            source_identity_hash=source_identity_hash,
            user_turn=user_turn,
            assistant_turn=assistant_turn,
            speaker_role="assistant",
            exchange_index=exchange_index,
            part_index=part_index,
            part_count=len(payloads),
            context_inherited=context_inherited,
            context_truncated=inherited_truncated,
            chunk_size=chunk_size,
        )
    return False


def _plan_exchange_payloads(
    assistant_text: str,
    *,
    first_prefix: str,
    inherited_prefix: str,
    context_truncated: bool,
    chunk_size: int,
    min_chunk_size: int,
) -> List[Tuple[str, bool, bool]]:
    payloads: List[Tuple[str, bool, bool]] = []
    remaining = assistant_text
    if first_prefix:
        first_payload, remaining = _take_bounded(remaining, chunk_size - len(first_prefix))
        if first_payload or len(first_prefix.strip()) > min_chunk_size:
            payloads.append((first_payload, False, False))

    inherited_budget = chunk_size - len(inherited_prefix)
    if inherited_budget <= 0:
        raise ValueError("conversation context envelope leaves no payload budget")
    while remaining:
        payload, remaining = _take_bounded(remaining, inherited_budget)
        payloads.append((payload, bool(inherited_prefix), context_truncated))
    if not payloads and len(assistant_text.strip()) > min_chunk_size:
        payloads.append((assistant_text, False, False))
    return payloads


def _emit_standalone_turn(
    output: List[ConversationChunk],
    unit: ConversationUnit,
    turn: ConversationTurn,
    *,
    source_identity_hash: str,
    exchange_index: int,
    chunk_size: int,
    min_chunk_size: int,
    include_title: bool,
) -> bool:
    title_prefix = _title_prefix(unit, include_title, chunk_size)
    role_prefix = "> " if turn.role == "user" else ""
    first_prefix = title_prefix + role_prefix
    # Every later piece of a split question keeps the "> " marker. Without it
    # a continuation drawer is anonymous text that a reader — and the legacy
    # transcript chunker — attributes to the assistant, which is the exact
    # mis-attribution the ChatGPT parser fix had to correct once already.
    continuation_prefix = role_prefix
    remaining = turn.text
    payloads: List[str] = []

    first_budget = chunk_size - len(first_prefix)
    if first_budget > 0:
        payload, remaining = _take_bounded(remaining, first_budget)
        payloads.append(payload)
    continuation_budget = chunk_size - len(continuation_prefix)
    if continuation_budget <= 0:
        raise ValueError("conversation speaker marker leaves no payload budget")
    while remaining:
        payload, remaining = _take_bounded(remaining, continuation_budget)
        payloads.append(payload)

    if len(turn.text.strip()) <= min_chunk_size and len(first_prefix.strip()) <= min_chunk_size:
        return include_title

    for part_index, payload in enumerate(payloads):
        _append_chunk(
            output,
            unit,
            payload=payload,
            content=(first_prefix if part_index == 0 else continuation_prefix) + payload,
            source_identity_hash=source_identity_hash,
            user_turn=turn if turn.role == "user" else None,
            assistant_turn=turn if turn.role == "assistant" else None,
            speaker_role=turn.role,
            exchange_index=exchange_index,
            part_index=part_index,
            part_count=len(payloads),
            context_inherited=False,
            context_truncated=False,
            chunk_size=chunk_size,
        )
    return False


def _title_prefix(unit: ConversationUnit, include_title: bool, chunk_size: int) -> str:
    """Render a safe bounded display title while identity keeps the full unit."""
    if not include_title or not unit.title:
        return ""
    title = _single_line_title(unit.title)
    fixed_width = len("--- conversation:  ---\n")
    title_limit = max(1, chunk_size // 2 - fixed_width)
    if len(title) > title_limit:
        title = title[: max(0, title_limit - 1)].rstrip() + "…"
    return f"--- conversation: {title} ---\n"


def _single_line_title(title: str) -> str:
    """Neutralize every Unicode control/line-separator title character."""
    title = "".join(
        " " if unicodedata.category(character) in {"Cc", "Zl", "Zp"} else character
        for character in title
    ).strip()
    return re.sub(r"-{3,}", "--", title)


def _append_chunk(
    output: List[ConversationChunk],
    unit: ConversationUnit,
    *,
    payload: str,
    content: str,
    source_identity_hash: str,
    user_turn: ConversationTurn,
    assistant_turn: ConversationTurn,
    speaker_role: str,
    exchange_index: int,
    part_index: int,
    part_count: int,
    context_inherited: bool,
    context_truncated: bool,
    chunk_size: int,
) -> None:
    identity_turn = assistant_turn or user_turn
    output.append(
        ConversationChunk(
            content=content,
            payload=payload,
            logical_id=_logical_chunk_id(
                source_identity_hash,
                unit.conversation_id_hash,
                identity_turn.message_id_hash,
                speaker_role,
                part_index,
            ),
            metadata=_chunk_metadata(
                source_identity_hash=source_identity_hash,
                unit=unit,
                user_turn=user_turn,
                assistant_turn=assistant_turn,
                speaker_role=speaker_role,
                exchange_index=exchange_index,
                part_index=part_index,
                part_count=part_count,
                context_inherited=context_inherited,
                context_truncated=context_truncated,
                chunk_size=chunk_size,
            ),
        )
    )


def _chunk_metadata(
    *,
    source_identity_hash: str,
    unit: ConversationUnit,
    user_turn: ConversationTurn,
    assistant_turn: ConversationTurn,
    speaker_role: str,
    exchange_index: int,
    part_index: int,
    part_count: int,
    context_inherited: bool,
    context_truncated: bool,
    chunk_size: int,
) -> Dict[str, ScalarMetadata]:
    user_hash = user_turn.message_id_hash if user_turn is not None else ""
    assistant_hash = assistant_turn.message_id_hash if assistant_turn is not None else ""
    identity_fallback = unit.identity_fallback or bool(
        (user_turn is not None and user_turn.identity_fallback)
        or (assistant_turn is not None and assistant_turn.identity_fallback)
    )
    return {
        "source_identity_hash": source_identity_hash,
        "conversation_id_hash": unit.conversation_id_hash,
        "root_id_hash": unit.root_id_hash,
        "user_message_id_hash": user_hash,
        "assistant_message_id_hash": assistant_hash,
        "speaker_role": speaker_role,
        "exchange_index": exchange_index,
        "part_index": part_index,
        "part_count": part_count,
        "context_inherited": context_inherited,
        "context_truncated": context_truncated,
        "identity_fallback": identity_fallback,
        "chunk_schema_version": CONVERSATION_CHUNK_SCHEMA_VERSION,
        "chunk_budget_method": CONVERSATION_CHUNK_BUDGET_METHOD,
        "chunk_budget_limit": chunk_size,
    }


def _logical_chunk_id(
    source_identity_hash: str,
    conversation_id_hash: str,
    message_id_hash: str,
    role: str,
    part_index: int,
) -> str:
    material = "|".join(
        (source_identity_hash, conversation_id_hash, message_id_hash, role, str(part_index))
    )
    return privacy_safe_identity("conversation-chunk", material)


def _context_envelope(user_text: str, chunk_size: int) -> Tuple[str, bool]:
    if not user_text:
        return "", False
    # The first drawer keeps the source question verbatim. Continuation
    # context is a derived copy, so neutralize bracket tokens that could forge
    # this module's structural envelope without increasing its character size.
    safe_user_text = user_text.translate(str.maketrans({"[": "［", "]": "］"}))
    envelope_overhead = len(_CONTEXT_HEADER) + len(_CONTEXT_FOOTER)
    payload_budget = max(1, min(240, chunk_size // 3, chunk_size - envelope_overhead - 16))
    if len(safe_user_text) <= payload_budget:
        excerpt = safe_user_text
        truncated = False
    else:
        digest = hashlib.sha256(user_text.encode("utf-8")).hexdigest()[:16]
        verbose_marker = f"\n… [context shortened sha256:{digest}] …\n"
        marker = verbose_marker if len(verbose_marker) < payload_budget else "…"
        retained = max(0, payload_budget - len(marker))
        head = (retained + 1) // 2
        tail = retained // 2
        excerpt = safe_user_text[:head] + marker + (safe_user_text[-tail:] if tail else "")
        truncated = True
    return _CONTEXT_HEADER + excerpt + _CONTEXT_FOOTER, truncated


def _take_bounded(text: str, limit: int) -> Tuple[str, str]:
    """Take one exact prefix, stopping at the strongest boundary near ``limit``.

    Boundary strength, strongest first: a closed code fence, a paragraph break,
    the end of a sentence, a line break, a space between two words. A stronger
    boundary wins even when a weaker one sits further to the right, so a drawer
    ends at the end of a thought rather than at the last space before the
    character budget runs out. The previous rule took whichever boundary sat
    furthest right, which in prose is nearly always a space, so almost every
    split landed mid-sentence.

    Nothing is dropped or added: the two returned pieces always concatenate
    back to ``text``.
    """
    if limit <= 0:
        raise ValueError("chunk payload limit must be positive")
    if len(text) <= limit:
        return text, ""

    window = text[:limit]
    for fraction in _BOUNDARY_SEARCH_FLOORS:
        floor = max(1, int(limit * fraction))
        cut = _strongest_boundary(window, floor)
        if cut is not None:
            return text[:cut], text[cut:]
    return text[:limit], text[limit:]


def _strongest_boundary(window: str, floor: int) -> Optional[int]:
    """Return the best cut position at or after ``floor``, or None if there is none.

    Classes are tried in order of strength, and the last usable occurrence of
    the first available class wins.
    """
    # A fenced code block that opens and closes inside the window: never split
    # a code block across two drawers when the whole block fits.
    fences = list(_FENCE_RE.finditer(window))
    for index in range(len(fences), 0, -1):
        match = fences[index - 1]
        if index % 2 == 0 and match.end() >= floor:
            return match.end()

    position = window.rfind("\n\n")
    if position != -1 and position + 2 >= floor:
        return position + 2

    sentence_cut = -1
    for match in _SENTENCE_BREAK_RE.finditer(window):
        if match.end() >= floor:
            sentence_cut = match.end()
    if sentence_cut != -1:
        return sentence_cut

    for boundary in ("\n", " "):
        position = window.rfind(boundary)
        if position != -1 and position + len(boundary) >= floor:
            return position + len(boundary)
    return None
