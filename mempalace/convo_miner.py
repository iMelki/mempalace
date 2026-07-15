#!/usr/bin/env python3
"""
convo_miner.py — Mine conversations into the palace.

Ingests chat exports (Claude Code, ChatGPT, Slack, plain text transcripts).
Normalizes format, chunks by exchange pair (Q+A = one unit), files to palace.

Same palace as project mining. Different ingest strategy.
"""

import os
import sys
import hashlib
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from contextlib import nullcontext
from typing import Optional

from .normalize import normalize
from .palace import (
    NORMALIZE_VERSION,
    SKIP_DIRS,
    MineAlreadyRunning,
    file_already_mined,
    get_collection,
    mine_lock,
    mine_palace_lock,
)
from .receipt_verifier import ReceiptVerificationError, verify_receipt
from .write_receipts import (
    ManagedRunIdentity,
    ReceiptError,
    ReceiptStore,
    SourceWriteReceiptSession,
    canonical_source_locator,
    complete_reused_receipt,
    managed_write_scope,
    purge_managed_source_snapshot,
    require_managed_receipts,
    rollback_managed_source_rows,
    sha256_bytes,
    snapshot_managed_source_rows,
    write_receipted_collection_batch,
)


# Cached hall keywords — avoids re-reading config per drawer
_HALL_KEYWORDS_CACHE = None


def _detect_hall_cached(content: str) -> str:
    """Route content to a hall using cached keywords. Same logic as miner.detect_hall."""
    global _HALL_KEYWORDS_CACHE
    if _HALL_KEYWORDS_CACHE is None:
        from .config import MempalaceConfig

        _HALL_KEYWORDS_CACHE = MempalaceConfig().hall_keywords
    content_lower = content[:3000].lower()
    scores = {}
    for hall, keywords in _HALL_KEYWORDS_CACHE.items():
        score = sum(1 for kw in keywords if kw in content_lower)
        if score > 0:
            scores[hall] = score
    return max(scores, key=scores.get) if scores else "general"


# File types that might contain conversations
CONVO_EXTENSIONS = {
    ".txt",
    ".md",
    ".json",
    ".jsonl",
}

MIN_CHUNK_SIZE = 30
CHUNK_SIZE = 800  # chars per drawer — align with miner.py
DRAWER_UPSERT_BATCH_SIZE = 1000
MAX_FILE_SIZE = 500 * 1024 * 1024  # 500 MB — skip files larger than this.
# Matches miner.py at 500 MB. Long Claude Code sessions, multi-year
# ChatGPT exports, and lifetime Slack dumps routinely exceed 10 MB; the
# cap at that level silently dropped them with `continue`. Per-drawer
# size is bounded by CHUNK_SIZE, but larger source files still produce
# more drawers and therefore more embedding/storage work — and content
# is normalized and loaded fully into memory before chunking, so memory
# use also scales with source size.


def _register_file(
    collection,
    source_file: str,
    wing: str,
    agent: str,
    receipt: SourceWriteReceiptSession = None,
):
    """Write a sentinel so file_already_mined() returns True for 0-chunk files.

    Without this, files that normalize to nothing or produce zero chunks are
    re-read and re-processed on every mine run because nothing was written to
    ChromaDB on the first pass.
    """
    sentinel_id = f"_reg_{hashlib.sha256(source_file.encode()).hexdigest()[:24]}"
    document = f"[registry] {source_file}"
    metadata = {
        "wing": wing,
        "room": "_registry",
        "source_file": source_file,
        "added_by": agent,
        "filed_at": datetime.now().isoformat(),
        "ingest_mode": "registry",
        "normalize_version": NORMALIZE_VERSION,
    }
    batch = {"documents": [document], "ids": [sentinel_id], "metadatas": [metadata]}
    if receipt is None:
        collection.upsert(**batch)
    else:
        write_receipted_collection_batch(
            collection,
            "upsert",
            batch,
            session=receipt,
            source_file=source_file,
            kind="sentinel",
            local_path=True,
        )
    return sentinel_id


# =============================================================================
# CHUNKING — exchange pairs for conversations
# =============================================================================


def chunk_exchanges(content: str) -> list:
    """
    Chunk by exchange pair: one > turn + AI response = one unit.
    Falls back to paragraph chunking if no > markers.
    """
    lines = content.split("\n")
    quote_lines = sum(1 for line in lines if line.strip().startswith(">"))

    if quote_lines >= 3:
        return _chunk_by_exchange(lines)
    else:
        return _chunk_by_paragraph(content)


def _chunk_by_exchange(lines: list) -> list:
    """One user turn (>) + the AI response that follows = one or more chunks.

    The full AI response is preserved verbatim.  When the combined
    user-turn + response exceeds CHUNK_SIZE the response is split across
    consecutive drawers so nothing is silently discarded.
    """
    chunks = []
    i = 0

    while i < len(lines):
        line = lines[i]
        if line.strip().startswith(">"):
            user_turn = line.strip()
            i += 1

            ai_lines = []
            while i < len(lines):
                next_line = lines[i]
                if next_line.strip().startswith(">") or next_line.strip().startswith("---"):
                    break
                if next_line.strip():
                    ai_lines.append(next_line.strip())
                i += 1

            ai_response = " ".join(ai_lines)
            content = f"{user_turn}\n{ai_response}" if ai_response else user_turn

            # Split into multiple drawers when the exchange exceeds CHUNK_SIZE
            if len(content) > CHUNK_SIZE:
                # First chunk: user turn + as much response as fits
                first_part = content[:CHUNK_SIZE]
                if len(first_part.strip()) > MIN_CHUNK_SIZE:
                    chunks.append({"content": first_part, "chunk_index": len(chunks)})
                # Remaining response in CHUNK_SIZE-sized continuation drawers
                remainder = content[CHUNK_SIZE:]
                while remainder:
                    part = remainder[:CHUNK_SIZE]
                    remainder = remainder[CHUNK_SIZE:]
                    if len(part.strip()) > MIN_CHUNK_SIZE:
                        chunks.append({"content": part, "chunk_index": len(chunks)})
            elif len(content.strip()) > MIN_CHUNK_SIZE:
                chunks.append(
                    {
                        "content": content,
                        "chunk_index": len(chunks),
                    }
                )
        else:
            i += 1

    return chunks


def _chunk_by_paragraph(content: str) -> list:
    """Fallback: chunk by paragraph breaks."""
    chunks = []
    paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]

    # If no paragraph breaks and long content, chunk by line groups
    if len(paragraphs) <= 1 and content.count("\n") > 20:
        lines = content.split("\n")
        for i in range(0, len(lines), 25):
            group = "\n".join(lines[i : i + 25]).strip()
            if len(group) > MIN_CHUNK_SIZE:
                chunks.append({"content": group, "chunk_index": len(chunks)})
        return chunks

    for para in paragraphs:
        if len(para) > MIN_CHUNK_SIZE:
            chunks.append({"content": para, "chunk_index": len(chunks)})

    return chunks


# =============================================================================
# ROOM DETECTION — topic-based for conversations
# =============================================================================

TOPIC_KEYWORDS = {
    "technical": [
        "code",
        "python",
        "function",
        "bug",
        "error",
        "api",
        "database",
        "server",
        "deploy",
        "git",
        "test",
        "debug",
        "refactor",
    ],
    "architecture": [
        "architecture",
        "design",
        "pattern",
        "structure",
        "schema",
        "interface",
        "module",
        "component",
        "service",
        "layer",
    ],
    "planning": [
        "plan",
        "roadmap",
        "milestone",
        "deadline",
        "priority",
        "sprint",
        "backlog",
        "scope",
        "requirement",
        "spec",
    ],
    "decisions": [
        "decided",
        "chose",
        "picked",
        "switched",
        "migrated",
        "replaced",
        "trade-off",
        "alternative",
        "option",
        "approach",
    ],
    "problems": [
        "problem",
        "issue",
        "broken",
        "failed",
        "crash",
        "stuck",
        "workaround",
        "fix",
        "solved",
        "resolved",
    ],
}


def detect_convo_room(content: str) -> str:
    """Score conversation content against topic keywords."""
    content_lower = content[:3000].lower()
    scores = {}
    for room, keywords in TOPIC_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in content_lower)
        if score > 0:
            scores[room] = score
    if scores:
        return max(scores, key=scores.get)
    return "general"


# =============================================================================
# PALACE OPERATIONS
# =============================================================================


# =============================================================================
# SCAN FOR CONVERSATION FILES
# =============================================================================


def scan_convos(convo_dir: str) -> list:
    """Find all potential conversation files."""
    convo_path = Path(convo_dir).expanduser().resolve()
    files = []
    for root, dirs, filenames in os.walk(convo_path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for filename in filenames:
            if filename.endswith(".meta.json"):
                continue
            filepath = Path(root) / filename
            if filepath.suffix.lower() in CONVO_EXTENSIONS:
                # Skip symlinks and oversized files
                if filepath.is_symlink():
                    continue
                try:
                    if filepath.stat().st_size > MAX_FILE_SIZE:
                        continue
                except OSError:
                    continue
                files.append(filepath)
    return files


# =============================================================================
# MINE CONVERSATIONS
# =============================================================================


def _file_chunks_locked(
    collection,
    source_file,
    chunks,
    wing,
    room,
    agent,
    extract_mode,
    receipt: SourceWriteReceiptSession = None,
    lock_held: bool = False,
    source_aliases: tuple[str, ...] = (),
):
    """Lock the source file, purge stale drawers, and upsert fresh chunks.

    Combines the per-file serialization that prevents concurrent agents from
    duplicating work (via mine_lock) with the normalize-version rebuild
    contract (purge-before-insert so pre-v2 drawers don't survive).

    Returns (drawers_added, room_counts_delta, skipped).
    """
    room_counts_delta: dict = defaultdict(int)
    drawers_added = 0
    source_file = canonical_source_locator(source_file, local_path=True)
    lock_context = nullcontext() if lock_held else mine_lock(os.path.normcase(source_file))
    with lock_context:
        # Re-check after lock — another agent may have just finished this file
        # at the current schema. A stale-version hit here returns False, so we
        # still fall through to the purge+rebuild path below.
        if receipt is None:
            if file_already_mined(collection, source_file):
                return 0, room_counts_delta, True
        elif _reuse_verified_receipt(
            receipt,
            collection,
            source_file=source_file,
            source_aliases=source_aliases,
        ):
            return 0, room_counts_delta, True

        prior = _current_prior_receipt(receipt) if receipt is not None else None
        if receipt is not None and prior is not None:
            _supersede_prior_receipt(receipt, prior)

        mutated = False
        incomplete_purge = False
        snapshot = None
        recovery_path = None
        stage = "snapshot-existing"
        try:
            if receipt is not None:
                receipt.running("snapshotting-existing")
                snapshot = snapshot_managed_source_rows(
                    collection,
                    source_file=source_file,
                    source_identity=receipt.source["identity"],
                    local_path=True,
                    source_aliases=source_aliases,
                )
                recovery_path = receipt.store.prepare_rewrite_recovery(
                    session=receipt,
                    snapshots={"drawers": snapshot},
                    source_file=source_file,
                    local_path=True,
                    source_aliases=source_aliases,
                    previous_receipt=prior,
                )
                receipt.running("recovery-prepared")
                receipt.running("purging-existing")

            # Purge stale drawers first. When the normalize schema bumps,
            # file_already_mined() returned False for pre-v2 drawers — clean
            # them out so the source doesn't end up with mixed old/new drawers.
            stage = "purge-existing-drawers"
            if receipt is None:
                try:
                    collection.delete(where={"source_file": source_file})
                except Exception:
                    pass
            else:
                incomplete_purge = True
                purge_managed_source_snapshot(
                    collection,
                    snapshot,
                    recovery_path=recovery_path,
                    collection_name="drawers",
                    source_file=source_file,
                    source_identity=receipt.source["identity"],
                    local_path=True,
                    source_aliases=source_aliases,
                )
                incomplete_purge = False
                mutated = True

            if receipt is not None:
                receipt.running("writing-drawers")
            stage = "write-drawers"
            # Batch chunks into bounded upserts so large transcripts keep most
            # of the embedding speedup without one huge Chroma/SQLite request.
            filed_at = datetime.now().isoformat()
            for batch_start in range(0, len(chunks), DRAWER_UPSERT_BATCH_SIZE):
                batch_docs: list = []
                batch_ids: list = []
                batch_metas: list = []
                for chunk in chunks[batch_start : batch_start + DRAWER_UPSERT_BATCH_SIZE]:
                    chunk_room = (
                        chunk.get("memory_type", room) if extract_mode == "general" else room
                    )
                    if extract_mode == "general":
                        room_counts_delta[chunk_room] += 1
                    drawer_id = f"drawer_{wing}_{chunk_room}_{hashlib.sha256((source_file + str(chunk['chunk_index'])).encode()).hexdigest()[:24]}"
                    batch_docs.append(chunk["content"])
                    batch_ids.append(drawer_id)
                    batch_metas.append(
                        {
                            "wing": wing,
                            "room": chunk_room,
                            "hall": _detect_hall_cached(chunk["content"]),
                            "source_file": source_file,
                            "chunk_index": chunk["chunk_index"],
                            "added_by": agent,
                            "filed_at": filed_at,
                            "ingest_mode": "convos",
                            "extract_mode": extract_mode,
                            "normalize_version": NORMALIZE_VERSION,
                        }
                    )
                batch = {
                    "documents": batch_docs,
                    "ids": batch_ids,
                    "metadatas": batch_metas,
                }
                if receipt is None:
                    try:
                        collection.upsert(**batch)
                    except Exception as exc:
                        if "already exists" not in str(exc).lower():
                            raise
                else:
                    write_receipted_collection_batch(
                        collection,
                        "upsert",
                        batch,
                        session=receipt,
                        source_file=source_file,
                        local_path=True,
                    )
                drawers_added += len(batch_docs)

            if receipt is not None:
                receipt.set_expected(drawers=len(chunks), items=len(receipt.outputs))
                if prior is not None:
                    receipt.record_invalidation(prior, reason="source-rewrite-purge")
                stage = "complete"
                receipt.complete()
                receipt.store.finalize_rewrite_recovery(
                    receipt.source["identity"],
                    receipt.receipt_id,
                    collections={"drawers": collection},
                )
        except BaseException as exc:
            if receipt is not None and receipt.state not in {"COMPLETE", "ABORT", "FAIL"}:
                if mutated or incomplete_purge:
                    try:
                        rollback_managed_source_rows(
                            collection,
                            snapshot,
                            recovery_path=recovery_path,
                            collection_name="drawers",
                            source_identity=receipt.source["identity"],
                            receipt_id=receipt.receipt_id,
                        )
                        receipt.discard_pending_invalidations()
                        receipt.store.discard_rewrite_recovery(
                            receipt.source["identity"],
                            receipt.receipt_id,
                            collections={"drawers": collection},
                        )
                    except Exception as rollback_exc:
                        error = ReceiptError("managed conversation rollback failed")
                        receipt.fail(error, stage="rollback-existing")
                        raise error from rollback_exc
                if isinstance(exc, Exception):
                    receipt.fail(exc, stage=stage)
            raise
    return drawers_added, room_counts_delta, False


def _reuse_verified_receipt(
    receipt: SourceWriteReceiptSession,
    collection,
    *,
    source_file: str,
    source_aliases: tuple[str, ...] = (),
) -> bool:
    prior = receipt.store.find_current(
        receipt.source["identity"],
        content_hash=receipt.source["content_hash"],
        version_digest=receipt.source["version_hash"],
        config_digest=receipt.run.config_digest,
    )
    if prior is None:
        return False
    try:
        result = verify_receipt(
            prior,
            collection,
            current_source_content_hash=receipt.source["content_hash"],
            store=receipt.store,
        )
    except (ReceiptVerificationError, ValueError):
        return False
    if result.status != "represented":
        return False
    complete_reused_receipt(
        receipt,
        prior,
        collections={"drawers": collection},
        source_file=source_file,
        local_path=True,
        source_aliases=source_aliases,
    )
    return True


def _process_conversation_file(
    *,
    filepath: Path,
    collection,
    wing: str,
    agent: str,
    extract_mode: str,
    dry_run: bool,
    index: int,
    total_files: int,
    receipt_store: ReceiptStore = None,
    receipt_run: ManagedRunIdentity = None,
) -> tuple[int, dict, bool]:
    """Lock before reading so a waiting invocation cannot retain stale bytes."""
    require_managed_receipts(
        dry_run=dry_run,
        receipt_store=receipt_store,
        receipt_run=receipt_run,
        operation="conversation _process_conversation_file",
    )
    raw_source_alias = os.fspath(filepath)
    source_file = canonical_source_locator(filepath, local_path=True)
    managed = not dry_run and receipt_store is not None and receipt_run is not None
    write_scope = (
        managed_write_scope(receipt_store.palace_path, lock_factory=mine_palace_lock)
        if managed
        else nullcontext()
    )
    with write_scope:
        with mine_lock(os.path.normcase(source_file)):
            return _process_conversation_file_locked(
                filepath=Path(source_file),
                collection=collection,
                wing=wing,
                agent=agent,
                extract_mode=extract_mode,
                dry_run=dry_run,
                index=index,
                total_files=total_files,
                receipt_store=receipt_store,
                receipt_run=receipt_run,
                source_aliases=(raw_source_alias,),
            )


def _process_conversation_file_locked(
    *,
    filepath: Path,
    collection,
    wing: str,
    agent: str,
    extract_mode: str,
    dry_run: bool,
    index: int,
    total_files: int,
    receipt_store: ReceiptStore = None,
    receipt_run: ManagedRunIdentity = None,
    source_aliases: tuple[str, ...] = (),
) -> tuple[int, dict, bool]:
    """Process one conversation source and close its receipt terminally."""
    require_managed_receipts(
        dry_run=dry_run,
        receipt_store=receipt_store,
        receipt_run=receipt_run,
        operation="conversation _process_conversation_file_locked",
    )
    source_file = str(filepath)
    receipt = None
    if not dry_run and receipt_store is not None and receipt_run is not None:
        try:
            raw_content = filepath.read_bytes()
        except OSError:
            return 0, {}, False
        source_digest = sha256_bytes(raw_content)
        receipt_store.reconcile_pending_rewrites(
            {"drawers": collection},
            source_identity=receipt_store.source_identity(source_file, local_path=True),
        )
        receipt = receipt_store.begin_source(
            run=receipt_run,
            source_locator=source_file,
            source_content_hash=source_digest,
            source_version_hash=source_digest,
            source_size_bytes=len(raw_content),
            adapter_name="conversations",
            adapter_version=str(NORMALIZE_VERSION),
            local_path=True,
        )
        if _reuse_verified_receipt(
            receipt,
            collection,
            source_file=source_file,
            source_aliases=source_aliases,
        ):
            return 0, {}, True

    try:
        try:
            content = normalize(
                source_file,
                source_bytes=raw_content if receipt is not None else None,
            )
        except Exception as exc:
            if receipt is not None:
                receipt.fail(exc, stage="normalize")
            if isinstance(exc, (OSError, ValueError)):
                return 0, {}, False
            raise

        if not content or len(content.strip()) < MIN_CHUNK_SIZE:
            if receipt is not None:
                _complete_zero_output(
                    receipt,
                    collection,
                    source_file,
                    wing,
                    agent,
                    lock_held=True,
                    source_aliases=source_aliases,
                )
            return 0, {}, False

        if extract_mode == "general":
            from .general_extractor import extract_memories

            chunks = extract_memories(content)
        else:
            chunks = chunk_exchanges(content)

        if not chunks:
            if receipt is not None:
                _complete_zero_output(
                    receipt,
                    collection,
                    source_file,
                    wing,
                    agent,
                    lock_held=True,
                    source_aliases=source_aliases,
                )
            return 0, {}, False

        room = detect_convo_room(content) if extract_mode != "general" else None
        if dry_run:
            room_delta = _print_conversation_dry_run(filepath, chunks, room, extract_mode)
            return len(chunks), room_delta, False

        if receipt is not None:
            receipt.set_expected(drawers=len(chunks))
            receipt.running("writing-drawers")
        drawers_added, room_delta, skipped = _file_chunks_locked(
            collection,
            source_file,
            chunks,
            wing,
            room,
            agent,
            extract_mode,
            receipt,
            True,
            source_aliases,
        )
        if skipped:
            return 0, {}, True
        if extract_mode != "general":
            room_delta[room] += 1
        print(f"  + [{index:4}/{total_files}] {filepath.name[:50]:50} +{drawers_added}")
        return drawers_added, dict(room_delta), False
    except KeyboardInterrupt as exc:
        if receipt is not None and receipt.state not in {"COMPLETE", "ABORT", "FAIL"}:
            receipt.abort(exc, stage="conversation-interrupted")
        raise
    except Exception as exc:
        if receipt is not None and receipt.state not in {"COMPLETE", "ABORT", "FAIL"}:
            receipt.fail(exc, stage="conversation-write")
        raise


def _complete_zero_output(
    receipt,
    collection,
    source_file,
    wing,
    agent,
    lock_held: bool = False,
    source_aliases: tuple[str, ...] = (),
) -> None:
    receipt.set_expected(drawers=0, items=1)
    source_file = canonical_source_locator(source_file, local_path=True)
    lock_context = nullcontext() if lock_held else mine_lock(os.path.normcase(source_file))
    with lock_context:
        if _reuse_verified_receipt(
            receipt,
            collection,
            source_file=source_file,
            source_aliases=source_aliases,
        ):
            return

        prior = _current_prior_receipt(receipt)
        if prior is not None:
            _supersede_prior_receipt(receipt, prior)

        mutated = False
        incomplete_purge = False
        recovery_path = None
        stage = "snapshot-existing"
        try:
            receipt.running("snapshotting-existing")
            snapshot = snapshot_managed_source_rows(
                collection,
                source_file=source_file,
                source_identity=receipt.source["identity"],
                local_path=True,
                source_aliases=source_aliases,
            )
            recovery_path = receipt.store.prepare_rewrite_recovery(
                session=receipt,
                snapshots={"drawers": snapshot},
                source_file=source_file,
                local_path=True,
                source_aliases=source_aliases,
                previous_receipt=prior,
            )
            receipt.running("recovery-prepared")
            receipt.running("purging-existing")
            stage = "purge-existing-drawers"
            incomplete_purge = True
            purge_managed_source_snapshot(
                collection,
                snapshot,
                recovery_path=recovery_path,
                collection_name="drawers",
                source_file=source_file,
                source_identity=receipt.source["identity"],
                local_path=True,
                source_aliases=source_aliases,
            )
            incomplete_purge = False
            mutated = True
            receipt.running("zero-output-sentinel")
            stage = "zero-output-sentinel"
            _register_file(collection, source_file, wing, agent, receipt)
            if prior is not None:
                receipt.record_invalidation(prior, reason="source-zero-output-purge")
            stage = "complete"
            receipt.complete(disposition="ZERO_OUTPUT")
            receipt.store.finalize_rewrite_recovery(
                receipt.source["identity"],
                receipt.receipt_id,
                collections={"drawers": collection},
            )
        except BaseException as exc:
            if receipt.state not in {"COMPLETE", "ABORT", "FAIL"}:
                if mutated or incomplete_purge:
                    try:
                        rollback_managed_source_rows(
                            collection,
                            snapshot,
                            recovery_path=recovery_path,
                            collection_name="drawers",
                            source_identity=receipt.source["identity"],
                            receipt_id=receipt.receipt_id,
                        )
                        receipt.discard_pending_invalidations()
                        receipt.store.discard_rewrite_recovery(
                            receipt.source["identity"],
                            receipt.receipt_id,
                            collections={"drawers": collection},
                        )
                    except Exception as rollback_exc:
                        error = ReceiptError("managed conversation rollback failed")
                        receipt.fail(error, stage="rollback-existing")
                        raise error from rollback_exc
                if isinstance(exc, Exception):
                    receipt.fail(exc, stage=stage)
            raise


def _current_prior_receipt(receipt: SourceWriteReceiptSession) -> Optional[dict]:
    """Resolve the latest predecessor after acquiring the per-source lock."""
    return receipt.store.find_current(receipt.source["identity"]) or receipt.previous_complete


def _supersede_prior_receipt(receipt: SourceWriteReceiptSession, prior: dict) -> None:
    reason = (
        "source-version-changed"
        if prior.get("source", {}).get("version_hash") != receipt.source["version_hash"]
        else "representation-or-config-changed"
    )
    receipt.supersede(prior, reason=reason)


def _print_conversation_dry_run(filepath, chunks, room, extract_mode) -> dict:
    room_delta = defaultdict(int)
    if extract_mode == "general":
        from collections import Counter

        type_counts = Counter(c.get("memory_type", "general") for c in chunks)
        types_str = ", ".join(f"{name}:{count}" for name, count in type_counts.most_common())
        print(f"    [DRY RUN] {filepath.name} → {len(chunks)} memories ({types_str})")
        for chunk in chunks:
            room_delta[chunk.get("memory_type", "general")] += 1
    else:
        print(f"    [DRY RUN] {filepath.name} → room:{room} ({len(chunks)} drawers)")
        room_delta[room] += 1
    return dict(room_delta)


def mine_convos(
    convo_dir: str,
    palace_path: str,
    wing: str = None,
    agent: str = "mempalace",
    limit: int = 0,
    dry_run: bool = False,
    extract_mode: str = "exchange",
):
    """Serialize every mutating conversation run against one palace."""
    kwargs = {
        "convo_dir": convo_dir,
        "palace_path": palace_path,
        "wing": wing,
        "agent": agent,
        "limit": limit,
        "dry_run": dry_run,
        "extract_mode": extract_mode,
    }
    if dry_run:
        return _mine_convos_impl(**kwargs)
    try:
        with managed_write_scope(palace_path, lock_factory=mine_palace_lock):
            return _mine_convos_impl(**kwargs)
    except MineAlreadyRunning:
        print(
            f"mempalace: another mine is already running against {palace_path} - exiting cleanly.",
            file=sys.stderr,
        )
        return None


def _mine_convos_impl(
    convo_dir: str,
    palace_path: str,
    wing: str = None,
    agent: str = "mempalace",
    limit: int = 0,
    dry_run: bool = False,
    extract_mode: str = "exchange",
):
    """Mine a directory of conversation files into the palace.

    extract_mode:
        "exchange" — default exchange-pair chunking (Q+A = one unit)
        "general"  — general extractor: decisions, preferences, milestones, problems, emotions
    """

    convo_path = Path(convo_dir).expanduser().resolve()
    if not wing:
        from .config import normalize_wing_name

        wing = normalize_wing_name(convo_path.name)

    files = scan_convos(convo_dir)
    if limit > 0:
        files = files[:limit]

    print(f"\n{'=' * 55}")
    print("  MemPalace Mine — Conversations")
    print(f"{'=' * 55}")
    print(f"  Wing:    {wing}")
    print(f"  Source:  {convo_path}")
    print(f"  Files:   {len(files)}")
    print(f"  Palace:  {palace_path}")
    if dry_run:
        print("  DRY RUN — nothing will be filed")
    print(f"{'-' * 55}\n")

    collection = get_collection(palace_path) if not dry_run else None
    if not dry_run:
        receipt_store = ReceiptStore(palace_path)
        receipt_store.reconcile_pending_rewrites({"drawers": collection})
        receipt_run = receipt_store.create_run(
            caller=agent,
            mode=f"conversations:{extract_mode}",
            config={
                "pipeline": "conversations",
                "wing": wing,
                "extract_mode": extract_mode,
                "normalize_version": NORMALIZE_VERSION,
                "chunk_size": CHUNK_SIZE,
                "managed_output_collections": ["drawers"],
                "limit": limit,
            },
        )
    else:
        receipt_store = None
        receipt_run = None

    total_drawers = 0
    files_skipped = 0
    room_counts = defaultdict(int)

    for i, filepath in enumerate(files, 1):
        drawers_added, room_delta, skipped = _process_conversation_file(
            filepath=filepath,
            collection=collection,
            wing=wing,
            agent=agent,
            extract_mode=extract_mode,
            dry_run=dry_run,
            index=i,
            total_files=len(files),
            receipt_store=receipt_store,
            receipt_run=receipt_run,
        )
        if skipped:
            files_skipped += 1
        for r, n in room_delta.items():
            room_counts[r] += n
        total_drawers += drawers_added

    print(f"\n{'=' * 55}")
    print("  Done.")
    print(f"  Files processed: {len(files) - files_skipped}")
    print(f"  Files skipped (already filed): {files_skipped}")
    print(f"  Drawers filed: {total_drawers}")
    if room_counts:
        print("\n  By room:")
        for room, count in sorted(room_counts.items(), key=lambda x: x[1], reverse=True):
            print(f"    {room:20} {count} files")
    print('\n  Next: mempalace search "what you\'re looking for"')
    print(f"{'=' * 55}\n")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python convo_miner.py <convo_dir> [--palace PATH] [--limit N] [--dry-run]")
        sys.exit(1)
    from .config import MempalaceConfig

    mine_convos(sys.argv[1], palace_path=MempalaceConfig().palace_path)
