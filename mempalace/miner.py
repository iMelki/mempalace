#!/usr/bin/env python3
"""
miner.py — Files everything into the palace.

Reads mempalace.yaml from the project directory to know the wing + rooms.
Routes each file to the right room based on content.
Stores verbatim chunks as drawers. No summaries. Ever.
"""

import os
import sys
import shlex
import hashlib
import fnmatch
from contextlib import nullcontext
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from typing import Mapping, Optional

from .palace import (
    NORMALIZE_VERSION,
    SKIP_DIRS,
    MineAlreadyRunning,
    build_closet_lines,
    file_already_mined,
    get_closets_collection,
    get_collection,
    mine_lock,
    mine_palace_lock,
    purge_file_closets,
    upsert_closet_lines,
)
from .receipt_verifier import ReceiptVerificationError, verify_receipt
from .mine_progress import (
    MineManifestDrift,
    MinePlanJournal,
    MinePlanError,
    MineProgressJournal,
    build_source_manifest,
    build_source_manifest_from_descriptors,
    load_source_manifest,
    miner_revision,
    publish_source_manifest,
    source_descriptor,
    source_path_for_item,
    validate_manifest_context,
    validate_source_bytes,
)
from .write_receipts import (
    ManagedRunIdentity,
    ReceiptError,
    ReceiptIdentityError,
    ReceiptStore,
    SourceWriteReceiptSession,
    canonical_source_locator,
    config_hash,
    complete_reused_receipt,
    managed_write_scope,
    purge_managed_source_snapshot,
    require_managed_receipts,
    rollback_managed_source_rows,
    sha256_bytes,
    snapshot_managed_source_rows,
    write_receipted_collection_batch,
)

READABLE_EXTENSIONS = {
    ".txt",
    ".md",
    ".py",
    ".js",
    ".ts",
    ".jsx",
    ".tsx",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".html",
    ".css",
    ".java",
    ".go",
    ".rs",
    ".rb",
    ".sh",
    ".csv",
    ".sql",
    ".toml",
}

SKIP_FILENAMES = {
    "entities.json",
    "mempalace.yaml",
    "mempalace.yml",
    "mempal.yaml",
    "mempal.yml",
    ".gitignore",
    "package-lock.json",
}

CHUNK_SIZE = 800  # chars per drawer
CHUNK_OVERLAP = 100  # overlap between chunks
MIN_CHUNK_SIZE = 50  # skip tiny chunks
DRAWER_UPSERT_BATCH_SIZE = 1000
MAX_FILE_SIZE = 500 * 1024 * 1024  # 500 MB — skip files larger than this.
MINE_LOCK_CONFLICT_EXIT_CODE = 75
# Long Claude Code sessions and large transcript exports routinely exceed
# 10 MB. The cap exists as a defensive rail against pathological binary
# files, not as a limit on legitimate text. Per-drawer size is bounded
# by CHUNK_SIZE, but larger sources still produce proportionally more
# drawers and therefore more storage, embedding, and processing work —
# and file reads are not streamed (the whole content is loaded into
# memory before chunking), so memory use scales with source size too.


# =============================================================================
# IGNORE MATCHING
# =============================================================================


class GitignoreMatcher:
    """Lightweight matcher for one directory's .gitignore patterns."""

    def __init__(self, base_dir: Path, rules: list):
        self.base_dir = base_dir
        self.rules = rules

    @classmethod
    def from_dir(cls, dir_path: Path):
        gitignore_path = dir_path / ".gitignore"
        if not gitignore_path.is_file():
            return None

        try:
            lines = gitignore_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            return None

        rules = []
        for raw_line in lines:
            line = raw_line.strip()
            if not line:
                continue

            if line.startswith("\\#") or line.startswith("\\!"):
                line = line[1:]
            elif line.startswith("#"):
                continue

            negated = line.startswith("!")
            if negated:
                line = line[1:]

            anchored = line.startswith("/")
            if anchored:
                line = line.lstrip("/")

            dir_only = line.endswith("/")
            if dir_only:
                line = line.rstrip("/")

            if not line:
                continue

            rules.append(
                {
                    "pattern": line,
                    "anchored": anchored,
                    "dir_only": dir_only,
                    "negated": negated,
                }
            )

        if not rules:
            return None

        return cls(dir_path, rules)

    def matches(self, path: Path, is_dir: bool = None):
        try:
            relative = path.relative_to(self.base_dir).as_posix().strip("/")
        except ValueError:
            return None

        if not relative:
            return None

        if is_dir is None:
            is_dir = path.is_dir()

        ignored = None
        for rule in self.rules:
            if self._rule_matches(rule, relative, is_dir):
                ignored = not rule["negated"]
        return ignored

    def _rule_matches(self, rule: dict, relative: str, is_dir: bool) -> bool:
        pattern = rule["pattern"]
        parts = relative.split("/")
        pattern_parts = pattern.split("/")

        if rule["dir_only"]:
            target_parts = parts if is_dir else parts[:-1]
            if not target_parts:
                return False
            if rule["anchored"] or len(pattern_parts) > 1:
                return self._match_from_root(target_parts, pattern_parts)
            return any(fnmatch.fnmatch(part, pattern) for part in target_parts)

        if rule["anchored"] or len(pattern_parts) > 1:
            return self._match_from_root(parts, pattern_parts)

        return any(fnmatch.fnmatch(part, pattern) for part in parts)

    def _match_from_root(self, target_parts: list, pattern_parts: list) -> bool:
        def matches(path_index: int, pattern_index: int) -> bool:
            if pattern_index == len(pattern_parts):
                return True

            if path_index == len(target_parts):
                return all(part == "**" for part in pattern_parts[pattern_index:])

            pattern_part = pattern_parts[pattern_index]
            if pattern_part == "**":
                return matches(path_index, pattern_index + 1) or matches(
                    path_index + 1, pattern_index
                )

            if not fnmatch.fnmatch(target_parts[path_index], pattern_part):
                return False

            return matches(path_index + 1, pattern_index + 1)

        return matches(0, 0)


def load_gitignore_matcher(dir_path: Path, cache: dict):
    """Load and cache one directory's .gitignore matcher."""
    if dir_path not in cache:
        cache[dir_path] = GitignoreMatcher.from_dir(dir_path)
    return cache[dir_path]


def is_gitignored(path: Path, matchers: list, is_dir: bool = False) -> bool:
    """Apply active .gitignore matchers in ancestor order; last match wins."""
    ignored = False
    for matcher in matchers:
        decision = matcher.matches(path, is_dir=is_dir)
        if decision is not None:
            ignored = decision
    return ignored


def should_skip_dir(dirname: str) -> bool:
    """Skip known generated/cache directories before gitignore matching."""
    return dirname in SKIP_DIRS or dirname.endswith(".egg-info")


def normalize_include_paths(include_ignored: list) -> set:
    """Normalize comma-parsed include paths into project-relative POSIX strings."""
    normalized = set()
    for raw_path in include_ignored or []:
        candidate = str(raw_path).strip().strip("/")
        if candidate:
            normalized.add(Path(candidate).as_posix())
    return normalized


def is_exact_force_include(path: Path, project_path: Path, include_paths: set) -> bool:
    """Return True when a path exactly matches an explicit include override."""
    if not include_paths:
        return False

    try:
        relative = path.relative_to(project_path).as_posix().strip("/")
    except ValueError:
        return False

    return relative in include_paths


def is_force_included(path: Path, project_path: Path, include_paths: set) -> bool:
    """Return True when a path or one of its ancestors/descendants was explicitly included."""
    if not include_paths:
        return False

    try:
        relative = path.relative_to(project_path).as_posix().strip("/")
    except ValueError:
        return False

    if not relative:
        return False

    for include_path in include_paths:
        if relative == include_path:
            return True
        if relative.startswith(f"{include_path}/"):
            return True
        if include_path.startswith(f"{relative}/"):
            return True

    return False


# =============================================================================
# CONFIG
# =============================================================================


def load_config(project_dir: str) -> dict:
    """Load mempalace.yaml from project directory (falls back to mempal.yaml)."""
    import yaml

    resolved_project_dir = Path(project_dir).expanduser().resolve()
    config_path = resolved_project_dir / "mempalace.yaml"
    if not config_path.exists():
        # Fallback to legacy name
        legacy_path = resolved_project_dir / "mempal.yaml"
        if legacy_path.exists():
            config_path = legacy_path
        else:
            from .config import normalize_wing_name

            # Normalize the dirname-derived fallback wing the same way
            # ``cmd_init`` and ``room_detector_local`` do — otherwise a
            # hyphenated project mined without a yaml file lands under a
            # raw-name wing while ``topics_by_wing`` was keyed under the
            # normalized slug, silently dropping every topic tunnel
            # (the no-yaml branch of issue #1194).
            wing_name = normalize_wing_name(resolved_project_dir.name)
            print(
                f"  No mempalace.yaml found in {resolved_project_dir} "
                f"— using auto-detected defaults (wing='{wing_name}'). "
                "Directories with the same basename will share a wing; "
                "add mempalace.yaml to disambiguate.",
                file=sys.stderr,
            )
            return {
                "wing": wing_name,
                "rooms": [
                    {
                        "name": "general",
                        "description": "All project files",
                        "keywords": ["general"],
                    }
                ],
            }
    with open(config_path) as f:
        return yaml.safe_load(f)


# =============================================================================
# FILE ROUTING — which room does this file belong to?
# =============================================================================


def detect_room(filepath: Path, content: str, rooms: list, project_path: Path) -> str:
    """
    Route a file to the right room.
    Priority:
    1. Folder path matches a room name
    2. Filename matches a room name or keyword
    3. Content keyword scoring
    4. Fallback: "general"
    """
    relative = str(filepath.relative_to(project_path)).lower()
    filename = filepath.stem.lower()
    content_lower = content[:2000].lower()

    # Priority 1: folder path matches room name or keywords
    # str() coercion: YAML parses bare numeric keywords (e.g. 2024) as ints.
    path_parts = relative.replace("\\", "/").split("/")
    for part in path_parts[:-1]:  # skip filename itself
        for room in rooms:
            candidates = [str(room["name"]).lower()] + [
                str(k).lower() for k in room.get("keywords", [])
            ]
            if any(part == c or c in part or part in c for c in candidates):
                return room["name"]

    # Priority 2: filename matches room name
    for room in rooms:
        if room["name"].lower() in filename or filename in room["name"].lower():
            return room["name"]

    # Priority 3: keyword scoring from room keywords + name
    scores = defaultdict(int)
    for room in rooms:
        keywords = room.get("keywords", []) + [room["name"]]
        for kw in keywords:
            count = content_lower.count(str(kw).lower())
            scores[room["name"]] += count

    if scores:
        best = max(scores, key=scores.get)
        if scores[best] > 0:
            return best

    return "general"


# =============================================================================
# CHUNKING
# =============================================================================


def chunk_text(content: str, source_file: str) -> list:
    """
    Split content into drawer-sized chunks.
    Tries to split on paragraph/line boundaries.
    Returns list of {"content": str, "chunk_index": int}
    """
    # Clean up
    content = content.strip()
    if not content:
        return []

    chunks = []
    start = 0
    chunk_index = 0

    while start < len(content):
        end = min(start + CHUNK_SIZE, len(content))

        # Try to break at paragraph boundary
        if end < len(content):
            newline_pos = content.rfind("\n\n", start, end)
            if newline_pos > start + CHUNK_SIZE // 2:
                end = newline_pos
            else:
                newline_pos = content.rfind("\n", start, end)
                if newline_pos > start + CHUNK_SIZE // 2:
                    end = newline_pos

        chunk = content[start:end].strip()
        if len(chunk) >= MIN_CHUNK_SIZE:
            chunks.append(
                {
                    "content": chunk,
                    "chunk_index": chunk_index,
                }
            )
            chunk_index += 1

        start = end - CHUNK_OVERLAP if end < len(content) else end

    return chunks


# =============================================================================
# PALACE — ChromaDB operations
# =============================================================================


_ENTITY_REGISTRY_PATH = os.path.join(os.path.expanduser("~"), ".mempalace", "known_entities.json")
_ENTITY_REGISTRY_CACHE: dict = {"mtime": None, "names": frozenset(), "raw": {}}
_ENTITY_EXTRACT_WINDOW = 5000  # chars of content scanned for capitalized words
_ENTITY_METADATA_LIMIT = 25  # max entities packed into the metadata field


def _refresh_known_entities_cache() -> None:
    """Reload ``~/.mempalace/known_entities.json`` into the module cache if
    its mtime changed since the last read. Shared by ``_load_known_entities``
    (flat set) and ``_load_known_entities_raw`` (category dict), so callers
    can pick whichever shape they need without duplicating the mtime-gated
    disk read.
    """
    try:
        mtime = os.path.getmtime(_ENTITY_REGISTRY_PATH)
    except OSError:
        if _ENTITY_REGISTRY_CACHE["mtime"] is not None:
            _ENTITY_REGISTRY_CACHE["mtime"] = None
            _ENTITY_REGISTRY_CACHE["names"] = frozenset()
            _ENTITY_REGISTRY_CACHE["raw"] = {}
        return

    if _ENTITY_REGISTRY_CACHE["mtime"] == mtime:
        return

    names: set = set()
    raw: dict = {}
    try:
        import json

        with open(_ENTITY_REGISTRY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            raw = data
            for cat_key, cat in data.items():
                # Special wing-keyed map — its inner values are topic
                # names but its outer keys are wings, which must NOT be
                # surfaced as known entities. Pull the topic names out
                # explicitly instead of treating it as a generic category.
                if cat_key == "topics_by_wing" and isinstance(cat, dict):
                    for topic_list in cat.values():
                        if isinstance(topic_list, list):
                            names.update(str(n) for n in topic_list if n)
                    continue
                if isinstance(cat, list):
                    names.update(str(n) for n in cat if n)
                elif isinstance(cat, dict):
                    names.update(str(k) for k in cat.keys() if k)
    except Exception:
        names = set()
        raw = {}

    _ENTITY_REGISTRY_CACHE["mtime"] = mtime
    _ENTITY_REGISTRY_CACHE["names"] = frozenset(names)
    _ENTITY_REGISTRY_CACHE["raw"] = raw


def _load_known_entities() -> frozenset:
    """Flat set of every known entity name (across all categories).

    Cached by mtime; invalidated when the registry file changes.
    """
    _refresh_known_entities_cache()
    return _ENTITY_REGISTRY_CACHE["names"]


def _load_known_entities_raw() -> dict:
    """Full category-dict view of the registry, shape
    ``{"category": ["Name1", ...], ...}``. Cached by mtime.

    Consumed by modules (e.g., fact_checker) that need to reason about
    categories rather than a flat name set. Never returns a mutable
    reference to the cache — callers get a shallow copy.
    """
    _refresh_known_entities_cache()
    return dict(_ENTITY_REGISTRY_CACHE["raw"])


def _set_wing_topics(existing: dict, wing_key: str, topics_for_wing: list, coerce) -> None:
    """Update ``existing['topics_by_wing'][wing_key]`` to the deduped list.

    Replaces (does not union) the wing's topic list — re-running ``init``
    should reflect the user's latest confirmation rather than accumulate
    stale labels. Empty input drops the wing entry; an empty map drops
    the ``topics_by_wing`` key entirely.
    """
    topics_map = existing.get("topics_by_wing")
    if not isinstance(topics_map, dict):
        topics_map = {}
    seen_lower: set = set()
    ordered: list = []
    for n in topics_for_wing:
        name = coerce(n)
        if not name:
            continue
        key = name.lower()
        if key in seen_lower:
            continue
        seen_lower.add(key)
        ordered.append(name)
    if ordered:
        topics_map[wing_key] = ordered
    else:
        topics_map.pop(wing_key, None)
    if topics_map:
        existing["topics_by_wing"] = topics_map
    else:
        existing.pop("topics_by_wing", None)


def add_to_known_entities(entities_by_category: dict, wing: str = None) -> str:
    """Union ``entities_by_category`` into ``~/.mempalace/known_entities.json``.

    Accepts ``{category: [names]}`` shape as produced by ``mempalace init``
    and merges into the registry the miner reads at mine time. Existing
    categories are preserved untouched unless also present in the input;
    for categories present in both, entries are unioned case-insensitively
    without changing the on-disk ordering of pre-existing names.

    If a category is stored on-disk as ``{name: code}`` (the alternate
    miner-supported shape, used by dialect-style configs), new names are
    added as keys with ``None`` values so existing code mappings aren't
    overwritten. A later compress pass can assign codes.

    When ``wing`` is provided AND ``entities_by_category`` contains a
    ``topics`` list, those topics are also recorded under
    ``topics_by_wing[wing]`` (case-insensitive dedup, preserving the
    casing of the first observed name). This is the signal source for
    ``palace_graph.compute_topic_tunnels`` at mine time. Topics for a
    wing are *replaced*, not unioned, so a re-run of ``init`` reflects
    the user's latest confirmation rather than accumulating stale labels
    indefinitely.

    The in-process cache is invalidated on write so same-process callers
    (notably ``cmd_init`` → ``cmd_mine`` in sequence) see the update
    immediately instead of waiting for a mtime re-check.

    Returns the registry path as a string for logging.
    """
    import json as _json
    from pathlib import Path as _Path

    registry_path = _Path(_ENTITY_REGISTRY_PATH)
    registry_path.parent.mkdir(parents=True, exist_ok=True)

    existing: dict = {}
    if registry_path.exists():
        try:
            loaded = _json.loads(registry_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except (_json.JSONDecodeError, OSError):
            existing = {}

    def _coerce_name(value):
        if not value:
            return None
        name = str(value)
        return name if name else None

    # Separate the topics_by_wing key from regular categories so we don't
    # treat it as a flat name-list elsewhere in this function.
    topics_for_wing = None
    if wing and isinstance(wing, str) and wing.strip():
        topics_for_wing = entities_by_category.get("topics") or []

    for category, names in entities_by_category.items():
        if category == "topics_by_wing":
            # Reserved key — managed separately below.
            continue
        if not isinstance(names, list) or not names:
            continue
        current = existing.get(category)
        if isinstance(current, list):
            seen_lower = {str(n).lower() for n in current}
            for n in names:
                name = _coerce_name(n)
                if not name:
                    continue
                if name.lower() not in seen_lower:
                    current.append(name)
                    seen_lower.add(name.lower())
        elif isinstance(current, dict):
            seen_lower = {str(name).lower() for name in current}
            for n in names:
                name = _coerce_name(n)
                if not name or name.lower() in seen_lower:
                    continue
                current[name] = None
                seen_lower.add(name.lower())
        else:
            # Missing or unrecognized shape — seed as a fresh list, deduped
            seen: set = set()
            ordered: list = []
            for n in names:
                name = _coerce_name(n)
                if not name:
                    continue
                key = name.lower()
                if key in seen:
                    continue
                seen.add(key)
                ordered.append(name)
            existing[category] = ordered

    if topics_for_wing is not None:
        _set_wing_topics(existing, wing.strip(), topics_for_wing, _coerce_name)

    registry_path.write_text(_json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        registry_path.chmod(0o600)
    except (OSError, NotImplementedError):
        pass

    # Invalidate in-process cache so later calls in the same run see the write.
    _ENTITY_REGISTRY_CACHE["mtime"] = None
    _ENTITY_REGISTRY_CACHE["names"] = frozenset()
    _ENTITY_REGISTRY_CACHE["raw"] = {}

    return str(registry_path)


def get_topics_by_wing() -> dict:
    """Return ``topics_by_wing`` from the global registry as a dict.

    Returns ``{}`` if the registry is missing, malformed, or has no
    ``topics_by_wing`` key. Casing is preserved from disk; callers that
    need case-insensitive comparison should normalize themselves.
    """
    raw = _load_known_entities_raw()
    topics_map = raw.get("topics_by_wing")
    if not isinstance(topics_map, dict):
        return {}
    out: dict = {}
    for wing, topics in topics_map.items():
        if not isinstance(wing, str) or not wing.strip():
            continue
        if isinstance(topics, list):
            cleaned = [str(t) for t in topics if isinstance(t, str) and t.strip()]
            if cleaned:
                out[wing.strip()] = cleaned
    return out


_HALL_KEYWORDS_CACHE = None


def detect_hall(content: str) -> str:
    """Route content to a hall based on keyword scoring.

    Halls connect rooms within a wing — they categorize the TYPE of content
    (emotional, technical, family, etc.) while rooms categorize the TOPIC.
    """
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

    if scores:
        return max(scores, key=scores.get)
    return "general"


def _extract_entities_for_metadata(
    content: str,
    *,
    known_entities=None,
    entity_languages=None,
) -> str:
    """Extract entity names from content for metadata tagging.

    Combines the user's known-entity registry (cached across calls) with
    capitalized words appearing ≥2 times in the first ``_ENTITY_EXTRACT_WINDOW``
    chars. Filters out the closet stoplist (``When``, ``After``, ``The``, …)
    so sentence-starters don't masquerade as proper nouns.

    Returns semicolon-separated string suitable for ChromaDB metadata
    filtering. The list is truncated to ``_ENTITY_METADATA_LIMIT`` entries
    *before* joining so a name is never cut in half.
    """
    import re

    from .palace import _ENTITY_STOPLIST

    matched: set = set()

    known = _load_known_entities() if known_entities is None else frozenset(known_entities)
    for name in known:
        if re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", content):
            matched.add(name)

    from .palace import _candidate_entity_words

    window = content[:_ENTITY_EXTRACT_WINDOW]
    words = _candidate_entity_words(window, entity_languages=entity_languages)
    freq: dict = {}
    for w in words:
        if w in _ENTITY_STOPLIST:
            continue
        freq[w] = freq.get(w, 0) + 1
    for w, c in freq.items():
        if c >= 2 and len(w) > 2:
            matched.add(w)

    if not matched:
        return ""
    # Truncate the *list*, not the joined string — never split a name.
    capped = sorted(matched)[:_ENTITY_METADATA_LIMIT]
    return ";".join(capped)


def _build_drawer_metadata(
    wing: str,
    room: str,
    source_file: str,
    chunk_index: int,
    agent: str,
    content: str,
    source_mtime: Optional[float],
) -> dict:
    """Build the metadata dict for one drawer without upserting.

    Split out from ``add_drawer`` so ``process_file`` can batch all chunks
    of a file into a single ``collection.upsert`` — one embedding forward
    pass per batch instead of per chunk.
    """
    metadata = {
        "wing": wing,
        "room": room,
        "source_file": source_file,
        "chunk_index": chunk_index,
        "added_by": agent,
        "filed_at": datetime.now().isoformat(),
        "normalize_version": NORMALIZE_VERSION,
    }
    if source_mtime is not None:
        # Chroma persists numeric metadata at six fractional digits. Normalize
        # before write so exact receipt readback compares the stored value.
        metadata["source_mtime"] = round(float(source_mtime), 6)
    metadata["hall"] = detect_hall(content)
    entities = _extract_entities_for_metadata(content)
    if entities:
        metadata["entities"] = entities
    return metadata


def add_drawer(
    collection, wing: str, room: str, content: str, source_file: str, chunk_index: int, agent: str
):
    """Add one drawer to the palace.

    Kept for backward compatibility with external callers. In-tree the
    miner uses ``_build_drawer_metadata`` + a batched ``collection.upsert``
    to amortize the embedding model's forward-pass cost across chunks.
    """
    drawer_id = f"drawer_{wing}_{room}_{hashlib.sha256((source_file + str(chunk_index)).encode()).hexdigest()[:24]}"
    try:
        source_mtime = os.path.getmtime(source_file)
    except OSError:
        source_mtime = None
    metadata = _build_drawer_metadata(
        wing, room, source_file, chunk_index, agent, content, source_mtime
    )
    collection.upsert(
        documents=[content],
        ids=[drawer_id],
        metadatas=[metadata],
    )
    return True


# =============================================================================
# PROCESS ONE FILE
# =============================================================================


def process_file(
    filepath: Path,
    project_path: Path,
    collection,
    wing: str,
    rooms: list,
    agent: str,
    dry_run: bool,
    closets_col=None,
    receipt_store: Optional[ReceiptStore] = None,
    receipt_run: Optional[ManagedRunIdentity] = None,
    expected_source: Optional[Mapping] = None,
) -> tuple:
    """Lock before reading so a waiting invocation cannot retain stale bytes."""
    require_managed_receipts(
        dry_run=dry_run,
        receipt_store=receipt_store,
        receipt_run=receipt_run,
        operation="project process_file",
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
            return _process_file_locked(
                filepath=Path(source_file),
                project_path=Path(canonical_source_locator(project_path, local_path=True)),
                collection=collection,
                wing=wing,
                rooms=rooms,
                agent=agent,
                dry_run=dry_run,
                closets_col=closets_col,
                receipt_store=receipt_store,
                receipt_run=receipt_run,
                source_aliases=(raw_source_alias,),
                expected_source=expected_source,
            )


def _process_file_locked(
    filepath: Path,
    project_path: Path,
    collection,
    wing: str,
    rooms: list,
    agent: str,
    dry_run: bool,
    closets_col=None,
    receipt_store: Optional[ReceiptStore] = None,
    receipt_run: Optional[ManagedRunIdentity] = None,
    source_aliases: tuple[str, ...] = (),
    expected_source: Optional[Mapping] = None,
) -> tuple:
    """Read, chunk, route, and file one file. Returns (drawer_count, room_name)."""

    require_managed_receipts(
        dry_run=dry_run,
        receipt_store=receipt_store,
        receipt_run=receipt_run,
        operation="project _process_file_locked",
    )

    source_file = str(filepath)
    managed = not dry_run and receipt_store is not None and receipt_run is not None
    if (
        not managed
        and not dry_run
        and file_already_mined(collection, source_file, check_mtime=True)
    ):
        return 0, "general"

    try:
        stat_before = filepath.stat()
        raw_content = filepath.read_bytes()
        stat_after = filepath.stat()
    except OSError as exc:
        if expected_source is not None:
            raise MineManifestDrift(
                f"source index {expected_source.get('index')} is no longer readable"
            ) from exc
        return 0, "general"
    if expected_source is not None:
        validate_source_bytes(
            path=filepath,
            project_path=project_path,
            item=expected_source,
            content=raw_content,
            stat_before=stat_before,
            stat_after=stat_after,
        )

    # Match Path.read_text's universal-newline behavior while binding the
    # receipt to the exact retained source bytes, before any transformation.
    content = raw_content.decode("utf-8", errors="replace")
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    content = content.strip()
    receipt: Optional[SourceWriteReceiptSession] = None
    if managed:
        source_digest = sha256_bytes(raw_content)
        recovery_collections = {"drawers": collection}
        if closets_col is not None:
            recovery_collections["closets"] = closets_col
        receipt_store.reconcile_pending_rewrites(
            recovery_collections,
            source_identity=receipt_store.source_identity(source_file, local_path=True),
        )
        receipt = receipt_store.begin_source(
            run=receipt_run,
            source_locator=source_file,
            source_content_hash=source_digest,
            source_version_hash=source_digest,
            source_size_bytes=len(raw_content),
            adapter_name="filesystem",
            adapter_version=str(NORMALIZE_VERSION),
            local_path=True,
        )
        if _reuse_verified_receipt(
            receipt,
            collection,
            closets_col,
            source_file=source_file,
            source_aliases=source_aliases,
        ):
            return 0, "general"

    try:
        if len(content) < MIN_CHUNK_SIZE:
            if receipt is not None:
                _complete_zero_output(
                    receipt=receipt,
                    collection=collection,
                    closets_col=closets_col,
                    source_file=source_file,
                    lock_held=True,
                    source_aliases=source_aliases,
                )
            return 0, "general"

        room = detect_room(filepath, content, rooms, project_path)
        chunks = chunk_text(content, source_file)

        if dry_run:
            print(f"    [DRY RUN] {filepath.name} -> room:{room} ({len(chunks)} drawers)")
            return len(chunks), room

        if receipt is not None:
            receipt.set_expected(drawers=len(chunks))

        drawers_added, skipped = _persist_file_chunks(
            collection=collection,
            closets_col=closets_col,
            source_file=source_file,
            content=content,
            chunks=chunks,
            wing=wing,
            room=room,
            agent=agent,
            receipt=receipt,
            lock_held=True,
            source_aliases=source_aliases,
        )
        if skipped:
            return 0, room
        return drawers_added, room
    except KeyboardInterrupt as exc:
        if receipt is not None and receipt.state not in {"COMPLETE", "ABORT", "FAIL"}:
            receipt.abort(exc, stage="filesystem-interrupted")
        raise
    except Exception as exc:
        if receipt is not None and receipt.state not in {"COMPLETE", "ABORT", "FAIL"}:
            receipt.fail(exc, stage="filesystem-write")
        raise


class _ReceiptedCollectionWriter:
    """Narrow adapter for helpers that only need collection.upsert()."""

    def __init__(
        self,
        collection,
        receipt: SourceWriteReceiptSession,
        source_file: str,
        *,
        collection_name: str,
        kind: str,
    ):
        self._collection = collection
        self._receipt = receipt
        self._source_file = source_file
        self._collection_name = collection_name
        self._kind = kind

    def upsert(self, **kwargs):
        return write_receipted_collection_batch(
            self._collection,
            "upsert",
            kwargs,
            session=self._receipt,
            source_file=self._source_file,
            collection_name=self._collection_name,
            kind=self._kind,
            local_path=True,
        )


def _rollback_file_mutations(
    receipt: SourceWriteReceiptSession,
    mutated: list[tuple[str, object, object]],
    *,
    recovery_path: Path,
    collections: dict[str, object],
    incomplete_purge: Optional[tuple[str, object, object]] = None,
) -> None:
    failures = []
    rollback_targets = list(reversed(mutated))
    if incomplete_purge is not None:
        rollback_targets.insert(0, incomplete_purge)
    for collection_name, collection, snapshot in rollback_targets:
        try:
            rollback_managed_source_rows(
                collection,
                snapshot,
                recovery_path=recovery_path,
                collection_name=collection_name,
                source_identity=receipt.source["identity"],
                receipt_id=receipt.receipt_id,
            )
        except Exception as exc:
            failures.append(exc)
    receipt.discard_pending_invalidations()
    if not failures:
        try:
            receipt.store.discard_rewrite_recovery(
                receipt.source["identity"],
                receipt.receipt_id,
                collections=collections,
            )
        except Exception as exc:
            failures.append(exc)
    if failures:
        raise ReceiptError("managed source rollback failed") from failures[0]


def _persist_file_chunks(
    *,
    collection,
    closets_col,
    source_file: str,
    content: str,
    chunks: list,
    wing: str,
    room: str,
    agent: str,
    receipt: Optional[SourceWriteReceiptSession],
    lock_held: bool = False,
    source_aliases: tuple[str, ...] = (),
) -> tuple[int, bool]:
    """Serialize the purge, drawer batches, and derived closet rebuild."""
    lock_context = nullcontext() if lock_held else mine_lock(os.path.normcase(source_file))
    with lock_context:
        if receipt is None:
            if file_already_mined(collection, source_file, check_mtime=True):
                return 0, True
        elif _reuse_verified_receipt(
            receipt,
            collection,
            closets_col,
            source_file=source_file,
            source_aliases=source_aliases,
        ):
            return 0, True

        prior = _current_prior_receipt(receipt) if receipt is not None else None
        if receipt is not None and prior is not None:
            _supersede_prior_receipt(receipt, prior)

        mutated: list[tuple[str, object, object]] = []
        incomplete_purge: Optional[tuple[str, object, object]] = None
        recovery_path = None
        stage = "snapshot-existing"
        try:
            snapshots: list[tuple[str, object, object]] = []
            recovery_snapshots = {}
            if receipt is not None:
                receipt.running("snapshotting-existing")
                drawer_snapshot = snapshot_managed_source_rows(
                    collection,
                    source_file=source_file,
                    source_identity=receipt.source["identity"],
                    local_path=True,
                    source_aliases=source_aliases,
                )
                snapshots.append(("drawers", collection, drawer_snapshot))
                recovery_snapshots["drawers"] = drawer_snapshot
                if closets_col is not None:
                    closet_snapshot = snapshot_managed_source_rows(
                        closets_col,
                        source_file=source_file,
                        source_identity=receipt.source["identity"],
                        local_path=True,
                        source_aliases=source_aliases,
                    )
                    snapshots.append(("closets", closets_col, closet_snapshot))
                    recovery_snapshots["closets"] = closet_snapshot
                recovery_path = receipt.store.prepare_rewrite_recovery(
                    session=receipt,
                    snapshots=recovery_snapshots,
                    source_file=source_file,
                    local_path=True,
                    source_aliases=source_aliases,
                    previous_receipt=prior,
                )
                receipt.running("recovery-prepared")
                receipt.running("purging-existing")

            stage = "purge-existing-drawers"
            if receipt is None:
                try:
                    collection.delete(where={"source_file": source_file})
                except Exception:
                    pass
            else:
                drawer_snapshot = snapshots[0][2]
                incomplete_purge = ("drawers", collection, drawer_snapshot)
                purge_managed_source_snapshot(
                    collection,
                    drawer_snapshot,
                    recovery_path=recovery_path,
                    collection_name="drawers",
                    source_file=source_file,
                    source_identity=receipt.source["identity"],
                    local_path=True,
                    source_aliases=source_aliases,
                )
                incomplete_purge = None
                mutated.append(("drawers", collection, drawer_snapshot))

            if receipt is not None and closets_col is not None:
                stage = "purge-existing-closets"
                closet_snapshot = snapshots[1][2]
                incomplete_purge = ("closets", closets_col, closet_snapshot)
                purge_managed_source_snapshot(
                    closets_col,
                    closet_snapshot,
                    recovery_path=recovery_path,
                    collection_name="closets",
                    source_file=source_file,
                    source_identity=receipt.source["identity"],
                    local_path=True,
                    source_aliases=source_aliases,
                )
                incomplete_purge = None
                mutated.append(("closets", closets_col, closet_snapshot))

            try:
                source_mtime = os.path.getmtime(source_file)
            except OSError:
                source_mtime = None

            stage = "write-drawers"
            if receipt is not None:
                receipt.running("writing-drawers")
            drawers_added = 0
            for batch_start in range(0, len(chunks), DRAWER_UPSERT_BATCH_SIZE):
                batch_docs: list = []
                batch_ids: list = []
                batch_metas: list = []
                for chunk in chunks[batch_start : batch_start + DRAWER_UPSERT_BATCH_SIZE]:
                    drawer_id = f"drawer_{wing}_{room}_{hashlib.sha256((source_file + str(chunk['chunk_index'])).encode()).hexdigest()[:24]}"
                    chunk_content = chunk["content"]
                    metadata = _build_drawer_metadata(
                        wing,
                        room,
                        source_file,
                        chunk["chunk_index"],
                        agent,
                        chunk_content,
                        source_mtime,
                    )
                    batch_docs.append(chunk_content)
                    batch_ids.append(drawer_id)
                    batch_metas.append(metadata)
                batch = {
                    "documents": batch_docs,
                    "ids": batch_ids,
                    "metadatas": batch_metas,
                }
                if receipt is None:
                    collection.upsert(**batch)
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

            if closets_col is not None and drawers_added > 0:
                drawer_ids = [
                    f"drawer_{wing}_{room}_{hashlib.sha256((source_file + str(c['chunk_index'])).encode()).hexdigest()[:24]}"
                    for c in chunks
                ]
                closet_lines = build_closet_lines(source_file, drawer_ids, content, wing, room)
                closet_id_base = (
                    f"closet_{wing}_{room}_{hashlib.sha256(source_file.encode()).hexdigest()[:24]}"
                )
                entities = _extract_entities_for_metadata(content)
                closet_meta = {
                    "wing": wing,
                    "room": room,
                    "source_file": source_file,
                    "drawer_count": drawers_added,
                    "filed_at": datetime.now().isoformat(),
                    "normalize_version": NORMALIZE_VERSION,
                }
                if entities:
                    closet_meta["entities"] = entities
                if receipt is None:
                    purge_file_closets(closets_col, source_file)
                    closet_writer = closets_col
                else:
                    stage = "write-closets"
                    receipt.running("writing-closets")
                    closet_writer = _ReceiptedCollectionWriter(
                        closets_col,
                        receipt,
                        source_file,
                        collection_name="closets",
                        kind="closet",
                    )
                upsert_closet_lines(closet_writer, closet_id_base, closet_lines, closet_meta)

            if receipt is not None:
                receipt.set_expected(drawers=len(chunks), items=len(receipt.outputs))
                if prior is not None:
                    receipt.record_invalidation(prior, reason="source-rewrite-purge")
                stage = "complete"
                receipt.complete()
                receipt.store.finalize_rewrite_recovery(
                    receipt.source["identity"],
                    receipt.receipt_id,
                    collections={
                        "drawers": collection,
                        **({"closets": closets_col} if closets_col is not None else {}),
                    },
                )
        except BaseException as exc:
            if receipt is not None and receipt.state not in {"COMPLETE", "ABORT", "FAIL"}:
                if mutated or incomplete_purge is not None:
                    try:
                        _rollback_file_mutations(
                            receipt,
                            mutated,
                            recovery_path=recovery_path,
                            collections={
                                "drawers": collection,
                                **({"closets": closets_col} if closets_col is not None else {}),
                            },
                            incomplete_purge=incomplete_purge,
                        )
                    except ReceiptError as rollback_exc:
                        receipt.fail(rollback_exc, stage="rollback-existing")
                        raise rollback_exc from exc
                if isinstance(exc, Exception):
                    receipt.fail(exc, stage=stage)
            raise
    return drawers_added, False


def _complete_zero_output(
    *,
    receipt: SourceWriteReceiptSession,
    collection,
    closets_col,
    source_file: str,
    lock_held: bool = False,
    source_aliases: tuple[str, ...] = (),
) -> None:
    """Purge every prior representation before asserting managed zero output."""
    receipt.set_expected(drawers=0)
    lock_context = nullcontext() if lock_held else mine_lock(os.path.normcase(source_file))
    with lock_context:
        if _reuse_verified_receipt(
            receipt,
            collection,
            closets_col,
            source_file=source_file,
            source_aliases=source_aliases,
        ):
            return

        prior = _current_prior_receipt(receipt)
        if prior is not None:
            _supersede_prior_receipt(receipt, prior)

        mutated: list[tuple[str, object, object]] = []
        incomplete_purge: Optional[tuple[str, object, object]] = None
        recovery_path = None
        stage = "snapshot-existing"
        try:
            receipt.running("snapshotting-existing")
            drawer_snapshot = snapshot_managed_source_rows(
                collection,
                source_file=source_file,
                source_identity=receipt.source["identity"],
                local_path=True,
                source_aliases=source_aliases,
            )
            closet_snapshot = None
            if closets_col is not None:
                closet_snapshot = snapshot_managed_source_rows(
                    closets_col,
                    source_file=source_file,
                    source_identity=receipt.source["identity"],
                    local_path=True,
                    source_aliases=source_aliases,
                )

            recovery_snapshots = {"drawers": drawer_snapshot}
            if closet_snapshot is not None:
                recovery_snapshots["closets"] = closet_snapshot
            recovery_path = receipt.store.prepare_rewrite_recovery(
                session=receipt,
                snapshots=recovery_snapshots,
                source_file=source_file,
                local_path=True,
                source_aliases=source_aliases,
                previous_receipt=prior,
            )
            receipt.running("recovery-prepared")

            receipt.running("purging-existing")
            stage = "purge-existing-drawers"
            incomplete_purge = ("drawers", collection, drawer_snapshot)
            purge_managed_source_snapshot(
                collection,
                drawer_snapshot,
                recovery_path=recovery_path,
                collection_name="drawers",
                source_file=source_file,
                source_identity=receipt.source["identity"],
                local_path=True,
                source_aliases=source_aliases,
            )
            incomplete_purge = None
            mutated.append(("drawers", collection, drawer_snapshot))
            if closets_col is not None:
                stage = "purge-existing-closets"
                incomplete_purge = ("closets", closets_col, closet_snapshot)
                purge_managed_source_snapshot(
                    closets_col,
                    closet_snapshot,
                    recovery_path=recovery_path,
                    collection_name="closets",
                    source_file=source_file,
                    source_identity=receipt.source["identity"],
                    local_path=True,
                    source_aliases=source_aliases,
                )
                incomplete_purge = None
                mutated.append(("closets", closets_col, closet_snapshot))

            if prior is not None:
                receipt.record_invalidation(prior, reason="source-zero-output-purge")
            stage = "complete"
            receipt.complete(disposition="ZERO_OUTPUT")
            receipt.store.finalize_rewrite_recovery(
                receipt.source["identity"],
                receipt.receipt_id,
                collections={
                    "drawers": collection,
                    **({"closets": closets_col} if closets_col is not None else {}),
                },
            )
        except BaseException as exc:
            if receipt.state not in {"COMPLETE", "ABORT", "FAIL"}:
                if mutated or incomplete_purge is not None:
                    try:
                        _rollback_file_mutations(
                            receipt,
                            mutated,
                            recovery_path=recovery_path,
                            collections={
                                "drawers": collection,
                                **({"closets": closets_col} if closets_col is not None else {}),
                            },
                            incomplete_purge=incomplete_purge,
                        )
                    except ReceiptError as rollback_exc:
                        receipt.fail(rollback_exc, stage="rollback-existing")
                        raise rollback_exc from exc
                if isinstance(exc, Exception):
                    receipt.fail(exc, stage=stage)
            raise


def _current_prior_receipt(receipt: SourceWriteReceiptSession) -> Optional[dict]:
    """Resolve the latest predecessor after acquiring the per-source lock."""
    return receipt.store.find_current(receipt.source["identity"]) or receipt.previous_complete


def _supersede_prior_receipt(
    receipt: SourceWriteReceiptSession,
    prior: dict,
) -> None:
    reason = (
        "source-version-changed"
        if prior.get("source", {}).get("version_hash") != receipt.source["version_hash"]
        else "representation-or-config-changed"
    )
    receipt.supersede(prior, reason=reason)


def _reuse_verified_receipt(
    receipt: SourceWriteReceiptSession,
    collection,
    closets_col=None,
    *,
    source_file: str,
    source_aliases: tuple[str, ...] = (),
) -> bool:
    """Reuse content only after rebinding every row to the new receipt."""
    prior = receipt.store.find_current(
        receipt.source["identity"],
        content_hash=receipt.source["content_hash"],
        version_digest=receipt.source["version_hash"],
        config_digest=receipt.run.config_digest,
    )
    if prior is None:
        return False
    output_collections = {
        item.get("collection") for item in prior.get("outputs", {}).get("identities", [])
    }
    if "closets" in output_collections and closets_col is None:
        raise ReceiptIdentityError(
            "managed project receipt includes closets but no closet collection was supplied"
        )
    if (
        closets_col is not None
        and prior.get("disposition") != "ZERO_OUTPUT"
        and "drawers" in output_collections
        and "closets" not in output_collections
    ):
        return False
    try:
        collections = {"drawers": collection}
        if closets_col is not None:
            collections["closets"] = closets_col
        result = verify_receipt(
            prior,
            collections=collections,
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
        collections=collections,
        source_file=source_file,
        local_path=True,
        source_aliases=source_aliases,
    )
    return True


# =============================================================================
# SCAN PROJECT
# =============================================================================


def scan_project(
    project_dir: str,
    respect_gitignore: bool = True,
    include_ignored: list = None,
) -> list:
    """Return list of all readable file paths."""
    project_path = Path(project_dir).expanduser().resolve()
    files = []
    active_matchers = []
    matcher_cache = {}
    include_paths = normalize_include_paths(include_ignored)

    for root, dirs, filenames in os.walk(project_path):
        root_path = Path(root)

        if respect_gitignore:
            active_matchers = [
                matcher
                for matcher in active_matchers
                if root_path == matcher.base_dir or matcher.base_dir in root_path.parents
            ]
            current_matcher = load_gitignore_matcher(root_path, matcher_cache)
            if current_matcher is not None:
                active_matchers.append(current_matcher)

        dirs[:] = sorted(
            [
                d
                for d in dirs
                if is_force_included(root_path / d, project_path, include_paths)
                or not should_skip_dir(d)
            ],
            key=lambda value: (os.path.normcase(value), value),
        )
        if respect_gitignore and active_matchers:
            dirs[:] = sorted(
                [
                    d
                    for d in dirs
                    if is_force_included(root_path / d, project_path, include_paths)
                    or not is_gitignored(root_path / d, active_matchers, is_dir=True)
                ],
                key=lambda value: (os.path.normcase(value), value),
            )

        for filename in sorted(
            filenames,
            key=lambda value: (os.path.normcase(value), value),
        ):
            filepath = root_path / filename
            force_include = is_force_included(filepath, project_path, include_paths)
            exact_force_include = is_exact_force_include(filepath, project_path, include_paths)

            if not force_include and filename in SKIP_FILENAMES:
                continue
            if filepath.suffix.lower() not in READABLE_EXTENSIONS and not exact_force_include:
                continue
            if respect_gitignore and active_matchers and not force_include:
                if is_gitignored(filepath, active_matchers, is_dir=False):
                    continue
            # Skip symlinks — prevents following links to /dev/urandom, etc.
            if filepath.is_symlink():
                continue
            # Skip files exceeding size limit
            try:
                if filepath.stat().st_size > MAX_FILE_SIZE:
                    continue
            except OSError:
                continue
            files.append(filepath)
    return sorted(
        files,
        key=lambda path: (
            os.path.normcase(path.relative_to(project_path).as_posix()),
            path.relative_to(project_path).as_posix(),
        ),
    )


def _plan_gitignore_matchers(
    project_path: Path,
    root_path: Path,
    matcher_cache: dict,
) -> list:
    """Rebuild one directory's matcher chain without mutable traversal state."""
    relative = root_path.relative_to(project_path)
    directories = [project_path]
    cursor = project_path
    for part in relative.parts:
        cursor = cursor / part
        directories.append(cursor)
    return [
        matcher
        for directory in directories
        for matcher in [load_gitignore_matcher(directory, matcher_cache)]
        if matcher is not None
    ]


def _discover_plan_directory(
    *,
    project_path: Path,
    relative_dir: str,
    respect_gitignore: bool,
    include_paths: list,
    excluded_artifacts: set[Path],
    matcher_cache: dict,
) -> tuple[list[str], list[str]]:
    root_path = (project_path / relative_dir).resolve()
    try:
        root_path.relative_to(project_path)
    except ValueError as exc:
        raise MinePlanError("mine plan directory escapes the project root") from exc
    active_matchers = (
        _plan_gitignore_matchers(project_path, root_path, matcher_cache)
        if respect_gitignore
        else []
    )
    try:
        entries = sorted(
            list(os.scandir(root_path)),
            key=lambda entry: (os.path.normcase(entry.name), entry.name),
        )
    except OSError as exc:
        raise MineManifestDrift("source directory became unreadable during planning") from exc

    child_dirs: list[str] = []
    files: list[str] = []
    for entry in entries:
        path = Path(entry.path)
        force_include = is_force_included(path, project_path, include_paths)
        exact_force_include = is_exact_force_include(path, project_path, include_paths)
        try:
            is_directory = entry.is_dir(follow_symlinks=False)
            is_file = entry.is_file(follow_symlinks=False)
        except OSError as exc:
            raise MineManifestDrift("source changed during plan discovery") from exc
        if is_directory:
            if not force_include and should_skip_dir(entry.name):
                continue
            if (
                respect_gitignore
                and active_matchers
                and not force_include
                and is_gitignored(path, active_matchers, is_dir=True)
            ):
                continue
            child_dirs.append(path.relative_to(project_path).as_posix())
            continue
        if not is_file:
            continue
        if not force_include and entry.name in SKIP_FILENAMES:
            continue
        if path.suffix.lower() not in READABLE_EXTENSIONS and not exact_force_include:
            continue
        if (
            respect_gitignore
            and active_matchers
            and not force_include
            and is_gitignored(path, active_matchers, is_dir=False)
        ):
            continue
        if path.resolve() in excluded_artifacts:
            continue
        try:
            if path.stat().st_size > MAX_FILE_SIZE:
                continue
        except OSError as exc:
            raise MineManifestDrift("source changed during plan discovery") from exc
        files.append(path.relative_to(project_path).as_posix())
    return child_dirs, files


def build_resumable_source_manifest(
    *,
    project_path: Path,
    contract: Mapping,
    plan_progress_jsonl: str,
    respect_gitignore: bool,
    include_ignored: list,
    excluded_artifacts: set[Path],
    limit: int,
) -> dict:
    """Resume directory discovery and byte hashing from a fsynced per-file cursor."""
    include_paths = normalize_include_paths(include_ignored)
    identity = {
        "schema": "mempalace-mine-plan-identity/v1",
        "project_identity": sha256_bytes(os.path.normcase(str(project_path)).encode("utf-8")),
        "contract": dict(contract),
        "respect_gitignore": bool(respect_gitignore),
        "include_ignored": sorted(include_paths),
        "limit": int(limit),
        "excluded_artifacts": sorted(
            path.relative_to(project_path).as_posix()
            for path in excluded_artifacts
            if path == project_path or project_path in path.parents
        ),
    }
    matcher_cache: dict = {}
    with mine_lock(os.path.normcase(str(Path(plan_progress_jsonl).resolve()))):
        journal = MinePlanJournal(plan_progress_jsonl, identity=identity)
        state = journal.replay()
        while state["pending_dirs"]:
            relative_dir = state["pending_dirs"][0]
            discovery = state["discovered"].get(relative_dir)
            if discovery is None:
                child_dirs, files = _discover_plan_directory(
                    project_path=project_path,
                    relative_dir=relative_dir,
                    respect_gitignore=respect_gitignore,
                    include_paths=include_paths,
                    excluded_artifacts=excluded_artifacts,
                    matcher_cache=matcher_cache,
                )
                journal.append(
                    "directory-discovered",
                    {
                        "relative_dir": relative_dir,
                        "child_dirs": child_dirs,
                        "files": files,
                    },
                )
                discovery = {"child_dirs": child_dirs, "files": files}
                state["discovered"][relative_dir] = discovery
            for relative_path in discovery["files"]:
                if relative_path in state["described"]:
                    continue
                path = (project_path / relative_path).resolve()
                descriptor = source_descriptor(
                    path=path,
                    relative_path=relative_path,
                    normalized_path=os.path.normcase(relative_path).replace("\\", "/"),
                )
                journal.append(
                    "file-described",
                    {
                        "relative_dir": relative_dir,
                        "relative_path": relative_path,
                        "descriptor": descriptor,
                    },
                )
                state["described"][relative_path] = descriptor
            journal.append("directory-complete", {"relative_dir": relative_dir})
            state["completed"].add(relative_dir)
            state["pending_dirs"].pop(0)
            state["pending_dirs"].extend(discovery["child_dirs"])

        descriptors = sorted(
            state["described"].values(),
            key=lambda item: (item["normalized_path"], item["relative_path"]),
        )
        if limit > 0:
            descriptors = descriptors[:limit]
        return build_source_manifest_from_descriptors(
            project_path=project_path,
            descriptors=descriptors,
            contract=contract,
        )


# =============================================================================
# MAIN: MINE
# =============================================================================


def _project_run_config(
    *,
    wing: str,
    rooms: list,
    respect_gitignore: bool,
    include_ignored: list,
    limit: int,
) -> dict:
    return {
        "pipeline": "filesystem",
        "wing": wing,
        "rooms": rooms,
        "normalize_version": NORMALIZE_VERSION,
        "chunk_size": CHUNK_SIZE,
        "chunk_overlap": CHUNK_OVERLAP,
        "managed_output_collections": ["closets", "drawers"],
        "respect_gitignore": respect_gitignore,
        "include_ignored": sorted(normalize_include_paths(include_ignored)),
        "limit": limit,
    }


def _source_plan_contract(run_config: Mapping) -> dict:
    return {
        "mode": "project",
        "parser": "filesystem",
        "receipt_config_digest": config_hash(run_config),
        "miner_revision": miner_revision(__file__),
    }


def _verify_manifest_source_receipt(
    *,
    receipt_store: ReceiptStore,
    receipt_run: ManagedRunIdentity,
    filepath: Path,
    item: Mapping,
    collection,
    closets_col,
) -> tuple[dict, object]:
    """Reload and exactly verify the source head before cursor advancement."""
    source_identity = receipt_store.source_identity(str(filepath), local_path=True)
    receipt = receipt_store.find_current_read_only(
        source_identity,
        content_hash=item["content_hash"],
        version_digest=item["content_hash"],
        config_digest=receipt_run.config_digest,
    )
    if receipt is None or receipt.get("state") != "COMPLETE":
        raise MineProgressJournalError(
            f"source index {item['index']} has no matching terminal managed receipt"
        )
    if receipt.get("disposition") not in {"WRITE", "UNCHANGED", "ZERO_OUTPUT"}:
        raise MineProgressJournalError(
            f"source index {item['index']} has an unsupported terminal disposition"
        )
    try:
        verification = verify_receipt(
            receipt,
            collections={"drawers": collection, "closets": closets_col},
            current_source_content_hash=item["content_hash"],
            store=receipt_store,
        )
    except (ReceiptVerificationError, ReceiptIdentityError, ValueError) as exc:
        raise MineProgressJournalError(
            f"source index {item['index']} terminal receipt could not be verified"
        ) from exc
    if verification.status != "represented":
        raise MineProgressJournalError(
            f"source index {item['index']} terminal receipt is not represented"
        )
    return receipt, verification


def _verify_progress_prefix_against_palace(
    *,
    progress: MineProgressJournal,
    receipt_store: ReceiptStore,
    receipt_run: ManagedRunIdentity,
    project_path: Path,
    manifest_items: list,
    collection,
    closets_col,
) -> int:
    """Re-prove every persisted cursor entry against the selected palace."""
    records = progress.records()
    for source_index, record in enumerate(records):
        item = manifest_items[source_index]
        filepath = source_path_for_item(project_path, item)
        expected_identity = receipt_store.source_identity(str(filepath), local_path=True)
        if record["source_identity"] != expected_identity:
            raise MineProgressJournalError(
                "mine progress belongs to a different palace or source identity"
            )
        try:
            with mine_lock(os.path.normcase(str(filepath))):
                stat_before = filepath.stat()
                source_bytes = filepath.read_bytes()
                stat_after = filepath.stat()
                validate_source_bytes(
                    path=filepath,
                    project_path=project_path,
                    item=item,
                    content=source_bytes,
                    stat_before=stat_before,
                    stat_after=stat_after,
                )
        except OSError as exc:
            raise MineManifestDrift(f"source index {source_index} is no longer readable") from exc
        receipt, verification = _verify_manifest_source_receipt(
            receipt_store=receipt_store,
            receipt_run=receipt_run,
            filepath=filepath,
            item=item,
            collection=collection,
            closets_col=closets_col,
        )
        if receipt["receipt_id"] != record["receipt_id"]:
            raise MineProgressJournalError(
                f"source index {source_index} progress no longer names the current receipt"
            )
        if len(verification.represented) != record["represented_count"]:
            raise MineProgressJournalError(f"source index {source_index} represented count changed")
    return len(records)


class MineProgressJournalError(MinePlanError):
    """Raised when a terminal per-source receipt cannot authorize progress."""


def _prepare_mine_source_plan(
    *,
    project_path: Path,
    project_dir: str,
    run_config: Mapping,
    limit: int,
    dry_run: bool,
    respect_gitignore: bool,
    include_ignored: list,
    files: Optional[list],
    plan_out: Optional[str],
    plan_progress_jsonl: Optional[str],
    manifest_path: Optional[str],
    start_index: Optional[int],
    progress_jsonl: Optional[str],
) -> tuple[dict, list[Path], Optional[MineProgressJournal], int, int]:
    if plan_out and manifest_path:
        raise MinePlanError("--plan-out and --manifest are mutually exclusive")
    if plan_progress_jsonl and not plan_out:
        raise MinePlanError("--plan-progress-jsonl requires --plan-out")
    if start_index is not None and (
        not isinstance(start_index, int) or isinstance(start_index, bool) or start_index < 0
    ):
        raise MinePlanError("--start-index must be a non-negative integer")
    if progress_jsonl and dry_run:
        raise MinePlanError("--progress-jsonl requires a managed non-dry mine")

    plan_contract = _source_plan_contract(run_config)
    if manifest_path:
        manifest = load_source_manifest(manifest_path)
        validate_manifest_context(
            manifest,
            project_path=project_path,
            contract=plan_contract,
        )
    else:
        discovered = files
        excluded_artifacts = {
            Path(path).expanduser().resolve()
            for path in (plan_out, plan_progress_jsonl, progress_jsonl)
            if path
        }
        if files is None and plan_progress_jsonl:
            manifest = build_resumable_source_manifest(
                project_path=project_path,
                contract=plan_contract,
                plan_progress_jsonl=plan_progress_jsonl,
                respect_gitignore=respect_gitignore,
                include_ignored=include_ignored,
                excluded_artifacts=excluded_artifacts,
                limit=limit,
            )
        else:
            if discovered is None:
                discovered = scan_project(
                    project_dir,
                    respect_gitignore=respect_gitignore,
                    include_ignored=include_ignored,
                )
            discovered = [
                Path(path).expanduser().resolve()
                for path in discovered
                if Path(path).expanduser().resolve() not in excluded_artifacts
            ]
            try:
                discovered = sorted(
                    discovered,
                    key=lambda path: (
                        os.path.normcase(path.relative_to(project_path).as_posix()),
                        path.relative_to(project_path).as_posix(),
                    ),
                )
            except ValueError as exc:
                raise MinePlanError("source plan contains a path outside the project root") from exc
            if limit > 0:
                discovered = discovered[:limit]
            manifest = build_source_manifest(
                project_path=project_path,
                files=discovered,
                contract=plan_contract,
            )
        if plan_out:
            manifest = publish_source_manifest(plan_out, manifest)

    manifest_items = manifest["items"]
    planned_files = [source_path_for_item(project_path, item) for item in manifest_items]
    progress = MineProgressJournal(progress_jsonl, manifest=manifest) if progress_jsonl else None
    verified_prefix = progress.verified_prefix() if progress is not None else 0
    if start_index is not None and start_index != verified_prefix:
        raise MinePlanError("--start-index must equal the contiguous verified progress prefix")
    effective_start = (
        start_index if start_index is not None else (verified_prefix if progress is not None else 0)
    )
    return manifest, planned_files, progress, verified_prefix, effective_start


def mine(
    project_dir: str,
    palace_path: str,
    wing_override: str = None,
    agent: str = "mempalace",
    limit: int = 0,
    dry_run: bool = False,
    respect_gitignore: bool = True,
    include_ignored: list = None,
    files: list = None,
    plan_out: str = None,
    plan_progress_jsonl: str = None,
    manifest_path: str = None,
    start_index: Optional[int] = None,
    progress_jsonl: str = None,
    raise_on_lock_conflict: bool = False,
):
    """Mine a project directory into the palace.

    ``files`` may optionally be a pre-scanned list of file paths from
    :func:`scan_project`. When provided, the corpus walk is skipped — the
    caller (e.g. ``init`` showing a file-count estimate before the mine
    prompt) avoids walking the tree twice. When ``None`` (the default),
    ``mine`` walks the tree itself just like before.
    """

    if dry_run:
        return _mine_impl(
            project_dir,
            palace_path,
            wing_override=wing_override,
            agent=agent,
            limit=limit,
            dry_run=dry_run,
            respect_gitignore=respect_gitignore,
            include_ignored=include_ignored,
            files=files,
            plan_out=plan_out,
            plan_progress_jsonl=plan_progress_jsonl,
            manifest_path=manifest_path,
            start_index=start_index,
            progress_jsonl=progress_jsonl,
        )

    try:
        with managed_write_scope(palace_path, lock_factory=mine_palace_lock):
            return _mine_impl(
                project_dir,
                palace_path,
                wing_override=wing_override,
                agent=agent,
                limit=limit,
                dry_run=dry_run,
                respect_gitignore=respect_gitignore,
                include_ignored=include_ignored,
                files=files,
                plan_out=plan_out,
                plan_progress_jsonl=plan_progress_jsonl,
                manifest_path=manifest_path,
                start_index=start_index,
                progress_jsonl=progress_jsonl,
            )
    except MineAlreadyRunning:
        if raise_on_lock_conflict:
            raise
        print(
            "mempalace: another `mine` already holds the requested palace; retry later.",
            file=sys.stderr,
        )
        return


def _mine_impl(
    project_dir: str,
    palace_path: str,
    wing_override: str = None,
    agent: str = "mempalace",
    limit: int = 0,
    dry_run: bool = False,
    respect_gitignore: bool = True,
    include_ignored: list = None,
    files: list = None,
    plan_out: str = None,
    plan_progress_jsonl: str = None,
    manifest_path: str = None,
    start_index: Optional[int] = None,
    progress_jsonl: str = None,
):
    project_path = Path(project_dir).expanduser().resolve()
    config = load_config(project_dir)

    wing = wing_override or config["wing"]
    rooms = config.get("rooms", [{"name": "general", "description": "All project files"}])
    run_config = _project_run_config(
        wing=wing,
        rooms=rooms,
        respect_gitignore=respect_gitignore,
        include_ignored=include_ignored,
        limit=limit,
    )
    manifest, files, progress, verified_prefix, effective_start = _prepare_mine_source_plan(
        project_path=project_path,
        project_dir=project_dir,
        run_config=run_config,
        limit=limit,
        dry_run=dry_run,
        respect_gitignore=respect_gitignore,
        include_ignored=include_ignored,
        files=files,
        plan_out=plan_out,
        plan_progress_jsonl=plan_progress_jsonl,
        manifest_path=manifest_path,
        start_index=start_index,
        progress_jsonl=progress_jsonl,
    )
    manifest_items = manifest["items"]

    from .embedding import describe_device

    print(f"\n{'=' * 55}")
    print("  MemPalace Mine")
    print(f"{'=' * 55}")
    print(f"  Wing:    {wing}")
    print(f"  Rooms:   {', '.join(r['name'] for r in rooms)}")
    print(f"  Files:   {len(files)}")
    print(f"  Start:   {effective_start}")
    print(f"  Plan:    {manifest['manifest_digest']}")
    if progress is not None and progress.recovered_torn_bytes:
        print(f"  Progress recovery: {progress.recovered_torn_bytes} torn tail byte(s) discarded")
    if not (plan_out or manifest_path or progress_jsonl):
        print(f"  Palace:  {palace_path}")
    print(f"  Device:  {describe_device()}")
    if dry_run:
        print("  DRY RUN — nothing will be filed")
    if not respect_gitignore:
        print("  .gitignore: DISABLED")
    if include_ignored:
        print(f"  Include: {', '.join(sorted(normalize_include_paths(include_ignored)))}")
    print(f"{'-' * 55}\n")

    if not dry_run:
        collection = get_collection(palace_path)
        closets_col = get_closets_collection(palace_path)
        receipt_store = ReceiptStore(palace_path)
        receipt_store.reconcile_pending_rewrites({"drawers": collection, "closets": closets_col})
        receipt_run = receipt_store.create_run(
            caller=agent,
            mode="project",
            config=run_config,
        )
        if progress is not None and verified_prefix:
            verified_against_palace = _verify_progress_prefix_against_palace(
                progress=progress,
                receipt_store=receipt_store,
                receipt_run=receipt_run,
                project_path=project_path,
                manifest_items=manifest_items,
                collection=collection,
                closets_col=closets_col,
            )
            if verified_against_palace != verified_prefix:
                raise MineProgressJournalError(
                    "mine progress prefix changed during palace verification"
                )
    else:
        collection = None
        closets_col = None
        receipt_store = None
        receipt_run = None

    total_drawers = 0
    files_skipped = 0
    files_processed = 0
    last_file = None
    room_counts = defaultdict(int)

    try:
        for source_index in range(effective_start, len(files)):
            filepath = files[source_index]
            item = manifest_items[source_index]
            display_index = source_index + 1
            try:
                drawers, room = process_file(
                    filepath=filepath,
                    project_path=project_path,
                    collection=collection,
                    wing=wing,
                    rooms=rooms,
                    agent=agent,
                    dry_run=dry_run,
                    closets_col=closets_col,
                    receipt_store=receipt_store,
                    receipt_run=receipt_run,
                    expected_source=item,
                )
            except KeyboardInterrupt:
                # Re-raise so the outer handler prints the summary; we
                # capture the last-attempted file via last_file below.
                last_file = filepath.name
                raise
            files_processed += 1
            last_file = filepath.name
            if not dry_run and progress is not None:
                receipt, verification = _verify_manifest_source_receipt(
                    receipt_store=receipt_store,
                    receipt_run=receipt_run,
                    filepath=filepath,
                    item=item,
                    collection=collection,
                    closets_col=closets_col,
                )
                progress.append_verified(
                    source_index=source_index,
                    source_identity=receipt["source"]["identity"],
                    receipt=receipt,
                    represented_count=len(verification.represented),
                )
            if drawers == 0 and not dry_run:
                files_skipped += 1
            else:
                total_drawers += drawers
                room_counts[room] += 1
                if not dry_run:
                    if plan_out or manifest_path or progress_jsonl:
                        print(
                            f"  + [{display_index:4}/{len(files)}] "
                            f"source-index:{source_index} +{drawers}"
                        )
                    else:
                        print(
                            f"  + [{display_index:4}/{len(files)}] "
                            f"{filepath.name[:50]:50} +{drawers}"
                        )

        if not dry_run:
            # Cross-wing topic tunnels: after every file in this wing has been
            # processed, link this wing to any other wing that shares a
            # confirmed TOPIC label. Out of scope for v1: manifest-dependency
            # overlap, per-topic allow/deny lists, search-result surfacing.
            try:
                tunnels_added = _compute_topic_tunnels_for_wing(wing)
                if tunnels_added:
                    print(f"\n  Topic tunnels: +{tunnels_added} cross-wing link(s)")
            except Exception as e:
                # Tunnel computation must never fail a mine — degrade quietly.
                print(
                    f"\n  WARNING: topic tunnel computation skipped — {e}",
                    file=sys.stderr,
                )

        print(f"\n{'=' * 55}")
        print("  Done.")
        print(f"  Files processed: {files_processed - files_skipped}")
        print(f"  Files skipped (already filed): {files_skipped}")
        print(f"  Drawers filed: {total_drawers}")
        print("\n  By room:")
        for room, count in sorted(room_counts.items(), key=lambda x: x[1], reverse=True):
            print(f"    {room:20} {count} files")
        print('\n  Next: mempalace search "what you\'re looking for"')
        print(f"{'=' * 55}\n")
    except KeyboardInterrupt:
        # Idempotent re-mine: deterministic drawer IDs mean already-filed
        # drawers upsert to the same row on next run, so partial progress
        # is safe to leave in place. A second Ctrl-C during this print
        # propagates to the default handler — we don't try to catch
        # everything.
        print("\n\n  Mine interrupted.")
        print(f"    files_processed: {files_processed}/{len(files) - effective_start}")
        print(f"    drawers_filed:   {total_drawers}")
        if plan_out or manifest_path or progress_jsonl:
            print("    last_source:     sanitized; inspect the verified cursor")
        else:
            print(f"    last_file:       {last_file or '<none>'}")
        print(
            f"\n  Re-run `mempalace mine {shlex.quote(project_dir)}` to resume — "
            "already-filed drawers are\n  upserted idempotently and will not duplicate.\n"
        )
        sys.exit(130)
    finally:
        # Clean up the hooks-side PID lock if it points at us. Stale
        # entries already pass _pid_alive() == False on POSIX, but
        # actively removing the file makes the state observable
        # (callers can stat it) and avoids accidental PID reuse on
        # short-lived test runs. Only remove if the file claims our
        # own PID — never another process's.
        _cleanup_mine_pid_file()


def _cleanup_mine_pid_file() -> None:
    """Remove the global mine PID file if it currently points at us.

    The PID file (``~/.mempalace/hook_state/mine.pid``, written by the
    hook in :func:`mempalace.hooks_cli._spawn_mine`) tracks the PID of
    the most recently spawned mine subprocess so the hook can dedup
    concurrent auto-ingest fires. When that subprocess exits — cleanly,
    on error, or via Ctrl-C — it should remove its own entry so the
    next hook fire isn't briefly fooled by a stale PID before
    ``_pid_alive`` returns False.

    We only delete the file if it claims our own PID; any other PID is
    left alone (could be an unrelated mine running concurrently from
    a different worktree / session).
    """
    try:
        from .hooks_cli import _MINE_PID_FILE
    except Exception:
        return
    try:
        if not _MINE_PID_FILE.exists():
            return
        recorded = _MINE_PID_FILE.read_text().strip()
        if recorded and recorded.isdigit() and int(recorded) == os.getpid():
            _MINE_PID_FILE.unlink()
    except OSError:
        # Best-effort cleanup; never fail the mine over PID bookkeeping.
        pass


def _compute_topic_tunnels_for_wing(wing: str) -> int:
    """Drop tunnels between ``wing`` and every other wing that shares
    confirmed topics, honoring the ``topic_tunnel_min_count`` config knob.

    Returns the number of tunnels created or refreshed. Zero means no
    overlap found (or the registry has no ``topics_by_wing`` map yet).
    """
    from .config import MempalaceConfig
    from .palace_graph import topic_tunnels_for_wing

    topics_map = get_topics_by_wing()
    if not topics_map or wing not in topics_map:
        return 0
    cfg = MempalaceConfig()
    min_count = cfg.topic_tunnel_min_count
    created = topic_tunnels_for_wing(wing, topics_map, min_count=min_count)
    return len(created)


# =============================================================================
# STATUS
# =============================================================================


def _sqlite_status_counts(palace_path: str):
    """Return ``(total, wing_rooms)`` from Chroma SQLite metadata when available.

    This keeps ``mempalace status`` usable when the HNSW vector segment is
    damaged or quarantined. Opening the collection just to count rooms can
    segfault in native hnswlib before Python gets a recoverable exception.
    """
    sqlite_path = os.path.join(palace_path, "chroma.sqlite3")
    if not os.path.exists(sqlite_path):
        return None

    try:
        import sqlite3

        conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                """
                SELECT
                    COALESCE(w.string_value, '?') AS wing,
                    COALESCE(r.string_value, '?') AS room,
                    COUNT(*) AS drawer_count
                FROM embeddings e
                JOIN segments s ON e.segment_id = s.id
                JOIN collections c ON s.collection = c.id
                LEFT JOIN embedding_metadata w ON w.id = e.id AND w.key = 'wing'
                LEFT JOIN embedding_metadata r ON r.id = e.id AND r.key = 'room'
                WHERE c.name = 'mempalace_drawers'
                GROUP BY COALESCE(w.string_value, '?'), COALESCE(r.string_value, '?')
                """
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return None

    wing_rooms: dict = defaultdict(lambda: defaultdict(int))
    total = 0
    for wing, room, count in rows:
        count = int(count or 0)
        wing_rooms[wing or "?"][room or "?"] += count
        total += count
    return total, wing_rooms


def status(palace_path: str):
    """Show what's been filed in the palace."""
    sqlite_counts = _sqlite_status_counts(palace_path)
    if sqlite_counts is not None:
        total, wing_rooms = sqlite_counts
    else:
        try:
            col = get_collection(palace_path, create=False)
        except Exception:
            print(f"\n  No palace found at {palace_path}")
            print("  Run: mempalace init <dir> then mempalace mine <dir>")
            return

        # Count by wing and room — paginate to avoid SQLite "too many SQL
        # variables" error on large palaces (see #802, #850).
        total = col.count()
        wing_rooms = defaultdict(lambda: defaultdict(int))
        batch_size = 5000
        offset = 0
        while offset < total:
            r = col.get(limit=batch_size, offset=offset, include=["metadatas"])
            batch = r["metadatas"]
            if not batch:
                break
            for m in batch:
                m = m or {}
                wing_rooms[m.get("wing", "?")][m.get("room", "?")] += 1
            offset += len(batch)

    print(f"\n{'=' * 55}")
    print(f"  MemPalace Status — {total} drawers")
    print(f"{'=' * 55}\n")
    for wing, rooms in sorted(wing_rooms.items()):
        print(f"  WING: {wing}")
        for room, count in sorted(rooms.items(), key=lambda x: x[1], reverse=True):
            print(f"    ROOM: {room:20} {count:5} drawers")
        print()
    print(f"{'=' * 55}\n")
