"""
diary_ingest.py — Ingest daily summary files into the palace.

Architecture:
- ONE drawer per (wing, day) — full verbatim content, upserted as the day grows.
- Closets pack topics up to CLOSET_CHAR_LIMIT, never split mid-topic.
- A re-ingest fully purges the prior day's closets before rebuilding so a
  shorter day never leaves orphans behind.
- Only new entries are processed by default (tracks entry count in a state
  file under ``~/.mempalace/state/`` — never inside the user's diary dir).
- Per-file ``mine_lock`` so concurrent ingest from two terminals can't race.
- Entities extracted and stamped on metadata for filterable search.

Usage:
    python -m mempalace.diary_ingest --dir ~/daily_summaries --palace ~/.mempalace/palace
    python -m mempalace.diary_ingest --dir ~/daily_summaries --palace ~/.mempalace/palace --force
"""

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .config import MempalaceConfig
from .miner import _extract_entities_for_metadata, _load_known_entities
from .palace import (
    build_closet_lines,
    get_closets_collection,
    get_collection,
    mine_lock,
    upsert_closet_lines,
)
from .provenance import managed_adapter_ingest
from .sources.base import AdapterSchema, BaseSourceAdapter, SourceItemMetadata, SourceRef
from .sources.context import PalaceContext
from .write_receipts import META_SOURCE_CONTENT_HASH, ReceiptStore, sha256_bytes

DIARY_ENTRY_RE = re.compile(r"^## .+", re.MULTILINE)
_DIARY_RECEIPT_CONTRACT = "mempalace-diary-file-managed-write/v1"


def _state_file_for(palace_path: str, diary_dir: Path) -> Path:
    """Return the per-(palace, diary-dir) state-file path under ~/.mempalace/state.

    Keyed by sha256 of (palace_path, diary_dir) so multiple diary folders
    pointing at the same palace each get an independent state file. The
    state file is *never* written inside the user's diary directory.
    """
    state_root = Path(os.path.expanduser("~")) / ".mempalace" / "state"
    state_root.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(f"{palace_path}|{diary_dir}".encode()).hexdigest()[:24]
    return state_root / f"diary_ingest_{key}.json"


def _write_state_file(path: Path, state: dict[str, Any]) -> None:
    """Atomically publish mutable convenience state after receipt completion."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _split_entries(text):
    """Split diary text into (header, body) pairs per ## entry."""
    parts = DIARY_ENTRY_RE.split(text)
    headers = DIARY_ENTRY_RE.findall(text)
    entries = []
    for i, header in enumerate(headers):
        body = parts[i + 1] if i + 1 < len(parts) else ""
        entries.append((header.strip(), body.strip()))
    return entries


def _diary_drawer_id(wing: str, date_str: str) -> str:
    """Stable, wing-scoped drawer ID. Two diaries (e.g. 'work' vs 'personal')
    sharing the same date never collide."""
    suffix = hashlib.sha256(f"{wing}|{date_str}".encode()).hexdigest()[:24]
    return f"drawer_diary_{suffix}"


def _diary_closet_id_base(wing: str, date_str: str) -> str:
    suffix = hashlib.sha256(f"{wing}|{date_str}".encode()).hexdigest()[:24]
    return f"closet_diary_{suffix}"


def _dated_diary_files(diary_files: list[Path], *, wing: str) -> list[tuple[Path, str]]:
    """Resolve dated inputs and reject two sources targeting one day identity."""
    dated_files: list[tuple[Path, str]] = []
    source_for_date: dict[str, Path] = {}
    for diary_path in diary_files:
        date_match = re.match(r"(\d{4}-\d{2}-\d{2})", diary_path.stem)
        if not date_match:
            continue
        date_str = date_match.group(1)
        previous = source_for_date.get(date_str)
        if previous is not None:
            raise ValueError(
                "multiple diary files target the same managed day "
                f"({wing!r}, {date_str!r}): {previous.name!r}, {diary_path.name!r}"
            )
        source_for_date[date_str] = diary_path
        dated_files.append((diary_path, date_str))
    return dated_files


def _capture_entity_extraction_config() -> tuple[frozenset[str], tuple[str, ...], str]:
    """Snapshot output-affecting entity inputs and return a privacy-safe digest."""
    known_entities = frozenset(str(name) for name in _load_known_entities())
    entity_languages = tuple(str(language) for language in MempalaceConfig().entity_languages)
    payload = json.dumps(
        {
            "known_entities": sorted(known_entities),
            "entity_languages": list(entity_languages),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return known_entities, entity_languages, sha256_bytes(payload)


class _DiaryFileAdapter(BaseSourceAdapter):
    """Materialize one diary source into one drawer and its complete closets."""

    name = "diary-file"
    adapter_version = "1.0.0"
    capabilities = frozenset({"supports_incremental"})
    supported_modes = frozenset({"whole_record"})
    declared_transformations = frozenset({"newline-normalization", "diary-topic-packing"})
    empty_output_disposition = "ZERO_OUTPUT"

    def __init__(
        self,
        *,
        wing: str,
        date_str: str,
        force: bool,
        known_entities: frozenset[str],
        entity_languages: tuple[str, ...],
    ) -> None:
        self.wing = wing
        self.date_str = date_str
        self.force = force
        self.known_entities = known_entities
        self.entity_languages = entity_languages
        self.content_hash: Optional[str] = None
        self.source_size = 0
        self.entry_count = 0
        self.closets_written = 0

    def ingest(self, *, source: SourceRef, palace: PalaceContext):
        if source.local_path is None or source.uri is not None:
            raise ValueError("diary-file adapter requires exactly one local source path")

        diary_path = Path(source.local_path).expanduser().resolve()
        raw_content = diary_path.read_bytes()
        text = raw_content.decode("utf-8", errors="replace")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        source_file = str(diary_path)
        content_hash = sha256_bytes(raw_content)
        self.content_hash = content_hash
        self.source_size = len(raw_content)
        entries = _split_entries(text)
        self.entry_count = len(entries)

        yield SourceItemMetadata(
            source_file=source_file,
            version=content_hash,
            size_hint=len(raw_content),
            content_hash=content_hash,
        )
        if palace._skip_requested:
            return

        if len(text.strip()) < 50:
            return

        now_iso = datetime.now(timezone.utc).isoformat()
        drawer_id = _diary_drawer_id(self.wing, self.date_str)
        entities = _extract_entities_for_metadata(
            text,
            known_entities=self.known_entities,
            entity_languages=self.entity_languages,
        )
        drawer_meta = {
            "date": self.date_str,
            "wing": self.wing,
            "room": "daily",
            "source_file": source_file,
            "source_session": "daily_diary",
            "filed_at": now_iso,
            "chunk_index": 0,
            "adapter_name": self.name,
            "adapter_version": self.adapter_version,
        }
        if entities:
            drawer_meta["entities"] = entities
        palace.drawer_collection.upsert(
            documents=[text],
            ids=[drawer_id],
            metadatas=[drawer_meta],
        )

        all_lines = []
        for header, body in entries:
            entry_text = f"{header}\n{body}"
            all_lines.extend(
                build_closet_lines(
                    source_file,
                    [drawer_id],
                    entry_text,
                    self.wing,
                    "daily",
                    entity_languages=self.entity_languages,
                )
            )

        if not all_lines:
            return
        if palace.closet_collection is None:
            raise RuntimeError("diary-file managed ingest requires a closet collection")

        closet_meta = {
            "date": self.date_str,
            "wing": self.wing,
            "room": "daily",
            "source_file": source_file,
            "filed_at": now_iso,
            "adapter_name": self.name,
            "adapter_version": self.adapter_version,
        }
        if entities:
            closet_meta["entities"] = entities
        self.closets_written = upsert_closet_lines(
            palace.closet_collection,
            _diary_closet_id_base(self.wing, self.date_str),
            all_lines,
            closet_meta,
        )

    def describe_schema(self) -> AdapterSchema:
        return AdapterSchema(fields={}, version="1")

    def is_current(
        self,
        *,
        item: SourceItemMetadata,
        existing_metadata: Optional[dict],
    ) -> bool:
        if self.force:
            return False
        if existing_metadata is None:
            return True
        return (
            existing_metadata.get(META_SOURCE_CONTENT_HASH) == item.content_hash
            and existing_metadata.get("adapter_name") == self.name
            and existing_metadata.get("adapter_version") == self.adapter_version
            and existing_metadata.get("wing") == self.wing
            and existing_metadata.get("date") == self.date_str
        )


def _empty_ingest_result() -> dict[str, Any]:
    return {
        "days_updated": 0,
        "days_unchanged": 0,
        "closets_created": 0,
        "closets_written": 0,
        "receipt_ids": [],
        "run_ids": [],
    }


def _ingest_diary_files_locked(
    *,
    diary_files: list[Path],
    palace_path: str,
    state_file: Path,
    wing: str,
    force: bool,
) -> dict[str, Any]:
    """Ingest one directory while its mutable state-file lock is held."""
    dated_files = _dated_diary_files(diary_files, wing=wing)
    if not dated_files:
        print("No dated .md diary files found")
        return _empty_ingest_result()
    if not state_file.exists():
        state: dict = {}
    else:
        try:
            loaded_state = json.loads(state_file.read_text(encoding="utf-8"))
            state = loaded_state if isinstance(loaded_state, dict) else {}
        except Exception:
            state = {}

    drawers_col = get_collection(palace_path)
    closets_col = get_closets_collection(palace_path)
    receipt_store = ReceiptStore(palace_path)
    palace = PalaceContext(
        drawer_collection=drawers_col,
        closet_collection=closets_col,
        knowledge_graph=None,
        palace_path=palace_path,
        adapter_name=_DiaryFileAdapter.name,
        adapter_version=_DiaryFileAdapter.adapter_version,
    )

    days_updated = 0
    days_unchanged = 0
    closets_written = 0
    receipt_ids: list[str] = []
    run_ids: list[str] = []

    for diary_path, date_str in dated_files:
        state_key = f"{wing}|{diary_path.name}"
        stored_previous_state = state.get(state_key, {})
        previous_state = stored_previous_state if isinstance(stored_previous_state, dict) else {}
        known_entities, entity_languages, entity_config_digest = _capture_entity_extraction_config()
        adapter = _DiaryFileAdapter(
            wing=wing,
            date_str=date_str,
            force=force,
            known_entities=known_entities,
            entity_languages=entity_languages,
        )
        source = SourceRef(
            local_path=str(diary_path),
            options={"wing": wing, "date": date_str},
        )
        result = managed_adapter_ingest(
            adapter=adapter,
            source=source,
            palace=palace,
            receipt_store=receipt_store,
            caller="diary-ingest",
            config={
                "contract": _DIARY_RECEIPT_CONTRACT,
                "wing": wing,
                "materialization": "complete-source",
                "entity_extraction_digest": entity_config_digest,
            },
        )
        changed = result.sources_completed - result.sources_unchanged
        days_updated += changed
        days_unchanged += result.sources_unchanged
        closets_written += adapter.closets_written
        receipt_ids.extend(result.receipt_ids)
        if result.run_id not in run_ids:
            run_ids.append(result.run_id)

        ingested_at = previous_state.get("ingested_at")
        if changed or not ingested_at:
            ingested_at = datetime.now(timezone.utc).isoformat()
        state[state_key] = {
            "size": adapter.source_size,
            "entry_count": adapter.entry_count,
            "source_content_hash": adapter.content_hash,
            "receipt_id": result.receipt_ids[-1],
            "ingested_at": ingested_at,
        }

    _write_state_file(state_file, state)
    if days_updated:
        print(f"Diary: {days_updated} days updated, {closets_written} closets written")

    return {
        "days_updated": days_updated,
        "days_unchanged": days_unchanged,
        # Backward-compatible alias retained for existing callers.
        "closets_created": closets_written,
        "closets_written": closets_written,
        "receipt_ids": receipt_ids,
        "run_ids": run_ids,
    }


def ingest_diaries(
    diary_dir,
    palace_path,
    wing="diary",
    force=False,
):
    """Ingest daily summary files into the palace.

    Each date file gets ONE drawer keyed by ``(wing, date)`` and closets that
    pack topics atomically up to ``CLOSET_CHAR_LIMIT``. Every source change is
    materialized as a complete managed drawer+closet replacement. ``force=True``
    requests a verified rebuild even when the source hash is unchanged.
    """
    diary_dir = Path(diary_dir).expanduser().resolve()
    if not diary_dir.exists():
        print(f"Diary directory not found: {diary_dir}")
        return _empty_ingest_result()

    diary_files = sorted(diary_dir.glob("*.md"))
    if not diary_files:
        print(f"No .md files in {diary_dir}")
        return _empty_ingest_result()

    resolved_palace_path = str(Path(palace_path).expanduser().resolve())
    state_file = _state_file_for(resolved_palace_path, diary_dir)
    state_lock_key = os.path.normcase(str(state_file.resolve()))
    with mine_lock(state_lock_key):
        return _ingest_diary_files_locked(
            diary_files=diary_files,
            palace_path=resolved_palace_path,
            state_file=state_file,
            wing=wing,
            force=force,
        )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Ingest daily summaries into the palace")
    parser.add_argument("--dir", required=True, help="Path to daily_summaries directory")
    parser.add_argument("--palace", default=os.path.expanduser("~/.mempalace/palace"))
    parser.add_argument("--wing", default="diary")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    ingest_diaries(args.dir, args.palace, wing=args.wing, force=args.force)
