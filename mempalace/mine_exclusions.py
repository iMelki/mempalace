"""
mine_exclusions.py — Declarative mine-time exclusion policy plus read-only
backup/variant directory detection.

Two deliberately separate concerns live here:

1. **Exclusions — applied.** Generated and vendored content a mine should
   decline to ingest: machine-generated lockfiles and build/dependency
   output. These are large, near-identical across project copies, and have
   close to zero value as recallable memory — nobody asks the palace what a
   transitive dependency hash was. Declining to ingest is free. Deleting
   afterwards is irreversible in a store whose stated requirement is
   verbatim 100% recall, so *not ingesting* is the safe direction
   (mempalace#36).

2. **Variant detection — reported, never applied.** Directories whose names
   announce themselves as backups or forks of a sibling
   (``repeater-system_backup_250522``, ``backend-backup-git_broke``,
   ``repeater-system-all-ui-alpha-ver``). These are **not** excluded
   automatically and there is no config switch that makes them excluded
   automatically. A directory called ``backup`` can legitimately hold the
   only surviving copy of something — which is exactly why blanket
   exclusion is unsafe. The detector reports candidates; the operator
   decides and, if they agree, adds names to ``exclude.dirs``.

Reuse note — this is **not** a second ignore mechanism. The miner already
honours ``.gitignore`` (``GitignoreMatcher`` in :mod:`mempalace.miner`), a
hardcoded ``SKIP_DIRS`` set in :mod:`mempalace.palace`, and a hardcoded
``SKIP_FILENAMES`` set in :mod:`mempalace.miner`. This module turns those two
hardcoded sets into one configurable, documented policy and extends their
default membership. ``.gitignore`` handling is untouched: lockfiles are
normally committed on purpose, so ``.gitignore`` never covered them, which
is precisely why ~6,900 lockfile drawers reached the palace.

Config surface — the optional ``exclude:`` block of ``mempalace.yaml``::

    exclude:
      generated_files: true     # master switch for the lockfile/minified set
      generated_dirs: true      # master switch for the build-output set
      files: ["*.snap"]         # extra names/globs to exclude
      dirs: ["fixtures_huge"]   # extra directory names/globs to exclude
      allow_files: ["poetry.lock"]   # un-exclude — the recall escape hatch
      allow_dirs: ["bin"]            # un-exclude

    variants:
      enabled: true
      max_depth: 3
      globs: ["*_snapshot"]     # extra backup/variant name patterns

Every default is reversible from config without a code change: set the
master switch to ``false`` to drop a whole default set, or list individual
names under ``allow_files`` / ``allow_dirs`` to keep them mineable.
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from .palace import SKIP_DIRS
from .write_receipts import config_hash

# =============================================================================
# DEFAULT EXCLUSION SETS
# =============================================================================
#
# These are data, not control flow. Extend the tuples; do not add `if`
# branches to the matchers.

#: Machine-generated dependency lockfiles. One entry per ecosystem, spelled
#: the way the tool writes it. Matching is case-insensitive because Windows
#: filesystems are, so ``Cargo.lock`` and ``cargo.lock`` are the same file.
DEFAULT_GENERATED_FILES: tuple[str, ...] = (
    # JavaScript / TypeScript
    "package-lock.json",
    "npm-shrinkwrap.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lock",
    "bun.lockb",
    # Python
    "poetry.lock",
    "Pipfile.lock",
    "pdm.lock",
    "uv.lock",
    # Rust / Go / PHP / Ruby / Elixir / Dart / .NET / Swift
    "Cargo.lock",
    "go.sum",
    "composer.lock",
    "Gemfile.lock",
    "mix.lock",
    "pubspec.lock",
    "packages.lock.json",
    "Podfile.lock",
    "Package.resolved",
    # Build products that keep a readable extension
    "*.min.js",
    "*.min.css",
    "*.bundle.js",
)

#: Build output, caches, and vendored dependency trees. Seeded from the
#: pre-existing ``palace.SKIP_DIRS`` so nothing that was skipped before
#: becomes mineable now, then extended with the ecosystems that were
#: missing — most importantly ``obj`` and ``bin`` (.NET build output; a
#: single ``CategoriesAPI.Tests/obj`` tree contributed 418 drawers).
#:
#: ``bin`` is the one entry most likely to need reverting: Unix-style repos
#: keep hand-written entry scripts in ``bin/``. Add ``allow_dirs: ["bin"]``
#: to ``mempalace.yaml`` for those projects.
DEFAULT_GENERATED_DIRS: tuple[str, ...] = tuple(sorted(SKIP_DIRS)) + (
    "obj",
    "bin",
    "out",
    "vendor",
    "bower_components",
    "jspm_packages",
    "pods",
    "site-packages",
    ".terraform",
    ".gradle",
    ".dart_tool",
    ".turbo",
    ".parcel-cache",
    ".svelte-kit",
    ".nuxt",
    ".angular",
    ".yarn",
    ".pnpm-store",
    ".serverless",
    ".output",
    "*.egg-info",
)

#: Backup / variant directory name patterns. Report-only — see the module
#: docstring for why these are never auto-excluded.
DEFAULT_VARIANT_GLOBS: tuple[str, ...] = (
    "*backup*",
    "*_bak",
    "*-bak",
    "*.bak",
    "*_old",
    "*-old",
    "old",
    "*copy*",
    "*_orig",
    "*-orig",
    "*.orig",
    "*git_broke*",
    "*git-broke*",
    "*alpha-ver*",
    "*alpha_ver*",
    "*-fixes",
    "*_fixes",
    "*-v[0-9]",
    "*_v[0-9]",
    "*-broken*",
    "*_broken*",
)

#: A trailing ``yymmdd`` or ``yyyymmdd`` stamp behind a separator, e.g.
#: ``repeater-system_backup_250522``. Requires a real month/day so ordinary
#: numeric suffixes (``bui2``, ``api-v3``) do not match.
VARIANT_DATE_SUFFIX_RE = re.compile(
    r"[._\- ](?:(?:19|20)\d{2}|\d{2})(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])$"
)

#: Separators that can join a base directory name to a variant suffix.
_VARIANT_SEPARATORS: tuple[str, ...] = ("-", "_", ".", " ")

#: A sibling stem shorter than this is too generic to imply a copy
#: relationship (``app`` vs ``app-server`` is normal project structure).
MIN_SIBLING_STEM_LENGTH = 4

DEFAULT_VARIANT_MAX_DEPTH = 3


# =============================================================================
# PATTERN MATCHING
# =============================================================================

_GLOB_METACHARACTERS = ("*", "?", "[")


def _normalize(value: Any) -> str:
    return str(value).strip().lower()


def _split_patterns(values: Iterable[Any]) -> tuple[frozenset[str], tuple[str, ...]]:
    """Partition patterns into exact lowercase names and fnmatch globs."""
    names: set[str] = set()
    globs: list[str] = []
    for raw in values or ():
        value = _normalize(raw)
        if not value:
            continue
        if any(char in value for char in _GLOB_METACHARACTERS):
            globs.append(value)
        else:
            names.add(value)
    return frozenset(names), tuple(sorted(set(globs)))


def _matches(name: str, names: frozenset[str], globs: Sequence[str]) -> bool:
    lowered = _normalize(name)
    if not lowered:
        return False
    if lowered in names:
        return True
    return any(fnmatch.fnmatchcase(lowered, glob) for glob in globs)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        return [value]
    if isinstance(value, Mapping):
        return list(value)
    if isinstance(value, Iterable):
        return list(value)
    return [value]


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "on", "1"}:
            return True
        if lowered in {"false", "no", "off", "0"}:
            return False
        return default
    return bool(value)


# =============================================================================
# EXCLUSION POLICY
# =============================================================================

#: Reason codes recorded on every exclusion decision.
REASON_GENERATED_FILE = "generated-file"
REASON_GENERATED_DIR = "generated-directory"
REASON_PROJECT_ARTIFACT = "project-artifact"
REASON_CONFIGURED_FILE = "configured-file"
REASON_CONFIGURED_DIR = "configured-directory"


@dataclass(frozen=True)
class ExclusionPolicy:
    """One resolved, digestible mine-time exclusion policy.

    ``artifact_files`` carries MemPalace's own per-project files (the
    pre-existing ``SKIP_FILENAMES`` set) so the miner keeps a single
    decision point instead of two sequential membership checks.
    """

    artifact_files: frozenset[str] = frozenset()
    generated_files: frozenset[str] = frozenset()
    generated_file_globs: tuple[str, ...] = ()
    generated_dirs: frozenset[str] = frozenset()
    generated_dir_globs: tuple[str, ...] = ()
    extra_files: frozenset[str] = frozenset()
    extra_file_globs: tuple[str, ...] = ()
    extra_dirs: frozenset[str] = frozenset()
    extra_dir_globs: tuple[str, ...] = ()
    allow_files: frozenset[str] = frozenset()
    allow_file_globs: tuple[str, ...] = ()
    allow_dirs: frozenset[str] = frozenset()
    allow_dir_globs: tuple[str, ...] = ()

    # ── decisions ────────────────────────────────────────────────────────
    def file_exclusion_reason(self, filename: str) -> Optional[str]:
        """Return a reason code when this filename must not be ingested."""
        lowered = _normalize(filename)
        if not lowered:
            return None
        # MemPalace's own per-project artifacts are checked before the allow
        # list: `allow_files` is the operator's recall escape hatch for
        # generated content, not a way to mine mempalace.yaml back into the
        # corpus and break `init` idempotency.
        if str(filename).strip() in self.artifact_files:
            return REASON_PROJECT_ARTIFACT
        if _matches(lowered, self.allow_files, self.allow_file_globs):
            return None
        if _matches(lowered, self.extra_files, self.extra_file_globs):
            return REASON_CONFIGURED_FILE
        if _matches(lowered, self.generated_files, self.generated_file_globs):
            return REASON_GENERATED_FILE
        return None

    def dir_exclusion_reason(self, dirname: str) -> Optional[str]:
        """Return a reason code when this directory must not be walked."""
        lowered = _normalize(dirname)
        if not lowered:
            return None
        if _matches(lowered, self.allow_dirs, self.allow_dir_globs):
            return None
        if _matches(lowered, self.extra_dirs, self.extra_dir_globs):
            return REASON_CONFIGURED_DIR
        if _matches(lowered, self.generated_dirs, self.generated_dir_globs):
            return REASON_GENERATED_DIR
        return None

    def excludes_file(self, filename: str) -> bool:
        return self.file_exclusion_reason(filename) is not None

    def excludes_dir(self, dirname: str) -> bool:
        return self.dir_exclusion_reason(dirname) is not None

    # ── provenance ───────────────────────────────────────────────────────
    def as_dict(self) -> dict:
        """Canonical, order-stable description of the effective policy."""
        return {
            "schema": "mempalace-mine-exclusion-policy/v1",
            "artifact_files": sorted(self.artifact_files),
            "generated_files": sorted(self.generated_files),
            "generated_file_globs": sorted(self.generated_file_globs),
            "generated_dirs": sorted(self.generated_dirs),
            "generated_dir_globs": sorted(self.generated_dir_globs),
            "extra_files": sorted(self.extra_files),
            "extra_file_globs": sorted(self.extra_file_globs),
            "extra_dirs": sorted(self.extra_dirs),
            "extra_dir_globs": sorted(self.extra_dir_globs),
            "allow_files": sorted(self.allow_files),
            "allow_file_globs": sorted(self.allow_file_globs),
            "allow_dirs": sorted(self.allow_dirs),
            "allow_dir_globs": sorted(self.allow_dir_globs),
        }

    def digest(self) -> str:
        """Tagged digest of the effective policy, for plan-contract binding."""
        return config_hash(self.as_dict())

    def summary(self) -> str:
        """One-line human summary for mine output."""
        return (
            f"{len(self.generated_files) + len(self.generated_file_globs)} generated file "
            f"pattern(s), {len(self.generated_dirs) + len(self.generated_dir_globs)} "
            f"directory pattern(s)"
            + (
                f", {len(self.allow_files) + len(self.allow_dirs)} allow-listed"
                if self.allow_files or self.allow_dirs
                else ""
            )
        )


def load_exclusion_policy(
    config: Optional[Mapping[str, Any]] = None,
    *,
    artifact_files: Iterable[str] = (),
) -> ExclusionPolicy:
    """Build an :class:`ExclusionPolicy` from a parsed ``mempalace.yaml``.

    ``config`` is the whole project config mapping (or ``None``); the
    ``exclude:`` block is optional and every key inside it is optional.
    Unknown keys are ignored rather than rejected, so an older mempalace
    keeps mining a newer project config.
    """
    block: Mapping[str, Any] = {}
    if isinstance(config, Mapping):
        candidate = config.get("exclude")
        if isinstance(candidate, Mapping):
            block = candidate

    use_generated_files = _as_bool(block.get("generated_files"), True)
    use_generated_dirs = _as_bool(block.get("generated_dirs"), True)

    generated_names, generated_globs = (
        _split_patterns(DEFAULT_GENERATED_FILES) if use_generated_files else (frozenset(), ())
    )
    generated_dir_names, generated_dir_globs = (
        _split_patterns(DEFAULT_GENERATED_DIRS) if use_generated_dirs else (frozenset(), ())
    )
    extra_names, extra_globs = _split_patterns(_as_list(block.get("files")))
    extra_dir_names, extra_dir_globs = _split_patterns(_as_list(block.get("dirs")))
    allow_names, allow_globs = _split_patterns(_as_list(block.get("allow_files")))
    allow_dir_names, allow_dir_globs = _split_patterns(_as_list(block.get("allow_dirs")))

    return ExclusionPolicy(
        artifact_files=frozenset(str(name) for name in artifact_files),
        generated_files=generated_names,
        generated_file_globs=generated_globs,
        generated_dirs=generated_dir_names,
        generated_dir_globs=generated_dir_globs,
        extra_files=extra_names,
        extra_file_globs=extra_globs,
        extra_dirs=extra_dir_names,
        extra_dir_globs=extra_dir_globs,
        allow_files=allow_names,
        allow_file_globs=allow_globs,
        allow_dirs=allow_dir_names,
        allow_dir_globs=allow_dir_globs,
    )


def read_project_config(project_dir: str) -> dict:
    """Quietly read ``mempalace.yaml`` (or the legacy ``mempal.yaml``).

    Unlike :func:`mempalace.miner.load_config` this never prints, never
    synthesizes a wing, and never raises: a missing or malformed config
    yields ``{}`` so the caller falls back to the documented defaults.
    """
    try:
        import yaml
    except ImportError:  # pragma: no cover - yaml is a hard dependency
        return {}

    try:
        root = Path(project_dir).expanduser().resolve()
    except (OSError, ValueError):
        return {}

    for name in ("mempalace.yaml", "mempalace.yml", "mempal.yaml", "mempal.yml"):
        path = root / name
        try:
            if not path.is_file():
                continue
            parsed = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError, yaml.YAMLError):
            continue
        if isinstance(parsed, Mapping):
            return dict(parsed)
    return {}


def resolve_exclusion_policy(
    project_dir: str,
    *,
    artifact_files: Iterable[str] = (),
) -> ExclusionPolicy:
    """Resolve the effective policy for a project directory."""
    return load_exclusion_policy(
        read_project_config(project_dir),
        artifact_files=artifact_files,
    )


#: Default policy with no project config and no MemPalace artifact files.
DEFAULT_EXCLUSION_POLICY = load_exclusion_policy(None)


# =============================================================================
# BACKUP / VARIANT DIRECTORY DETECTION  (report-only)
# =============================================================================

SIGNAL_NAME_PATTERN = "name-pattern"
SIGNAL_DATE_STAMP = "date-stamp"
SIGNAL_SIBLING_PREFIX = "sibling-prefix"


@dataclass(frozen=True)
class VariantCandidate:
    """One directory that looks like a backup or variant of another.

    Reported, never excluded. ``confidence`` is advisory ranking only — it
    is not a threshold anything acts on.
    """

    relative_path: str
    name: str
    signals: tuple[str, ...]
    matched_patterns: tuple[str, ...]
    sibling_base: Optional[str]
    confidence: str

    def to_dict(self) -> dict:
        return {
            "relative_path": self.relative_path,
            "name": self.name,
            "signals": list(self.signals),
            "matched_patterns": list(self.matched_patterns),
            "sibling_base": self.sibling_base,
            "confidence": self.confidence,
        }


def load_variant_settings(config: Optional[Mapping[str, Any]] = None) -> dict:
    """Read the optional ``variants:`` block of ``mempalace.yaml``."""
    block: Mapping[str, Any] = {}
    if isinstance(config, Mapping):
        candidate = config.get("variants")
        if isinstance(candidate, Mapping):
            block = candidate

    max_depth = block.get("max_depth")
    try:
        depth = int(max_depth) if max_depth is not None else DEFAULT_VARIANT_MAX_DEPTH
    except (TypeError, ValueError):
        depth = DEFAULT_VARIANT_MAX_DEPTH

    globs = tuple(DEFAULT_VARIANT_GLOBS) + tuple(
        _normalize(value) for value in _as_list(block.get("globs")) if _normalize(value)
    )
    return {
        "enabled": _as_bool(block.get("enabled"), True),
        "max_depth": max(1, depth),
        "globs": globs,
    }


def _sibling_base(name: str, sibling_names: Sequence[str]) -> Optional[str]:
    """Return the sibling this name looks like a suffixed copy of."""
    lowered = _normalize(name)
    best: Optional[str] = None
    for sibling in sibling_names:
        other = _normalize(sibling)
        if other == lowered or len(other) < MIN_SIBLING_STEM_LENGTH:
            continue
        if not lowered.startswith(other):
            continue
        remainder = lowered[len(other) :]
        if not remainder or remainder[0] not in _VARIANT_SEPARATORS:
            continue
        if best is None or len(other) > len(best):
            best = sibling
    return best


def _variant_signals(
    name: str,
    sibling_names: Sequence[str],
    globs: Sequence[str],
) -> tuple[tuple[str, ...], tuple[str, ...], Optional[str]]:
    lowered = _normalize(name)
    matched = tuple(glob for glob in globs if fnmatch.fnmatchcase(lowered, glob))
    signals: list[str] = []
    if matched:
        signals.append(SIGNAL_NAME_PATTERN)
    if VARIANT_DATE_SUFFIX_RE.search(lowered):
        signals.append(SIGNAL_DATE_STAMP)
    base = _sibling_base(name, sibling_names)
    if base is not None:
        signals.append(SIGNAL_SIBLING_PREFIX)
    return tuple(signals), matched, base


def _confidence(signals: Sequence[str]) -> str:
    if len(signals) >= 2:
        return "high"
    if SIGNAL_NAME_PATTERN in signals or SIGNAL_DATE_STAMP in signals:
        return "medium"
    return "low"


_CONFIDENCE_ORDER = {"high": 0, "medium": 1, "low": 2}


def detect_variant_directories(
    root: str,
    *,
    max_depth: int = DEFAULT_VARIANT_MAX_DEPTH,
    policy: Optional[ExclusionPolicy] = None,
    globs: Optional[Sequence[str]] = None,
) -> list[VariantCandidate]:
    """Report directories that look like backups or variants of a sibling.

    Read-only and filesystem-only: this never opens the palace, never
    excludes anything, and never mutates state. Directories already covered
    by the exclusion policy (``node_modules``, ``obj``, …) are pruned so
    their internals cannot generate noise.
    """
    active_policy = policy or DEFAULT_EXCLUSION_POLICY
    patterns = tuple(globs) if globs is not None else DEFAULT_VARIANT_GLOBS
    try:
        root_path = Path(root).expanduser().resolve()
    except (OSError, ValueError):
        return []
    if not root_path.is_dir():
        return []

    candidates: list[VariantCandidate] = []
    pending: list[tuple[Path, int]] = [(root_path, 0)]
    while pending:
        parent, depth = pending.pop()
        try:
            entries = sorted(
                (entry for entry in os.scandir(parent)),
                key=lambda entry: (os.path.normcase(entry.name), entry.name),
            )
        except OSError:
            continue

        child_dirs: list[Path] = []
        for entry in entries:
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if active_policy.excludes_dir(entry.name):
                continue
            child_dirs.append(Path(entry.path))

        sibling_names = [path.name for path in child_dirs]
        for path in child_dirs:
            signals, matched, base = _variant_signals(path.name, sibling_names, patterns)
            if signals:
                try:
                    relative = path.relative_to(root_path).as_posix()
                except ValueError:  # pragma: no cover - resolved children only
                    relative = path.name
                candidates.append(
                    VariantCandidate(
                        relative_path=relative,
                        name=path.name,
                        signals=signals,
                        matched_patterns=matched,
                        sibling_base=base,
                        confidence=_confidence(signals),
                    )
                )
            if depth + 1 < max_depth:
                pending.append((path, depth + 1))

    candidates.sort(
        key=lambda item: (
            _CONFIDENCE_ORDER.get(item.confidence, 3),
            os.path.normcase(item.relative_path),
            item.relative_path,
        )
    )
    return candidates


def format_variant_report(
    candidates: Sequence[VariantCandidate],
    *,
    limit: Optional[int] = None,
    indent: str = "  ",
) -> list[str]:
    """Render candidates as operator-facing lines. Never a failure."""
    if not candidates:
        return [f"{indent}Backup/variant candidates: none detected."]

    lines = [
        f"{indent}Backup/variant directory candidates: {len(candidates)} "
        "(REPORT ONLY - nothing was excluded).",
    ]
    shown = candidates if limit is None else candidates[:limit]
    for candidate in shown:
        detail = ",".join(candidate.signals)
        if candidate.sibling_base:
            detail += f" of:{candidate.sibling_base}"
        lines.append(f"{indent}  [{candidate.confidence:6}] {candidate.relative_path}  ({detail})")
    if limit is not None and len(candidates) > limit:
        lines.append(f"{indent}  ... {len(candidates) - limit} more")
    lines.append(
        f"{indent}  A 'backup' directory can hold the only copy of something. Confirm each one,"
    )
    lines.append(
        f"{indent}  then add the names you agree with to `exclude.dirs` in mempalace.yaml."
    )
    return lines
