"""
dedup.py — Detect and remove near-duplicate drawers
====================================================

When the same files are mined multiple times, near-identical drawers
accumulate. This module finds drawers from the same source_file that
are too similar (cosine distance < threshold), keeps the longest/richest
version, and deletes the rest.

No API calls — uses ChromaDB's built-in embedding similarity.

Usage (standalone):
    python -m mempalace.dedup                          # DRY-RUN preview (default)
    python -m mempalace.dedup --apply                  # actually delete duplicates
    python -m mempalace.dedup --threshold 0.10         # stricter (near-identical only)
    python -m mempalace.dedup --threshold 0.35         # looser (catches paraphrased content)
    python -m mempalace.dedup --wing my_project        # scope to one wing
    python -m mempalace.dedup --stats                  # stats only (metadata pass)
    python -m mempalace.dedup --source "my_project"    # filter by source

    # --stats honours --wing/--source and prints the active scope. For a real
    # byte-identical duplicate count add --exact-duplicates (reads document
    # text, so pair it with --progress on a large palace):
    python -m mempalace.dedup --stats --wing coding --exact-duplicates --progress

    # --exact-duplicates groups WITHIN each source_file. When the same content
    # was mined from several on-disk copies of one project, every copy has a
    # distinct source_file and that grouping cannot see it. Use the read-only
    # cross-source mode, which groups by content hash regardless of source_file
    # and names the contributing source paths (iMelki/mempalace#19):
    python -m mempalace.dedup --stats --wing coding --cross-source-duplicates --progress

    # --progress covers every long pass, including the metadata page loop and
    # the embedding-distance dry-run below (iMelki/mempalace#32). It writes to
    # stderr only, so stdout stays a clean report channel; off by default.
    python -m mempalace.dedup --wing coding --progress

    # Operator policy decision (2026-08-07, iMelki/mempalace#19): the ONLY
    # dedup mutation this repo will build a delete path for is same-filename
    # cross-source sets -- one file mined from several on-disk copies of a
    # project. Mixed-filename sets that merely share a boilerplate chunk
    # (e.g. 10 CSS skins sharing one header) are PERMANENTLY out of scope for
    # deletion and are structurally unreachable from this mode. Preview first
    # (the default), then apply once a fresh known-good, offsite-verified
    # backup exists (checked automatically; see check_backup_freshness()):
    python -m mempalace.dedup --same-filename-cleanup --wing coding --progress
    python -m mempalace.dedup --same-filename-cleanup --apply-same-filename --wing coding

Usage (from CLI):
    mempalace dedup [--apply] [--threshold 0.15] [--stats]

Safety: bare invocation is a dry-run preview. Live deletion requires an
explicit --apply, a palace backup from the last 2 days, and (for bulk runs)
an operator-approved window — see iMelki/mempalace#19. The same-filename
cleanup path (--same-filename-cleanup) additionally checks that backup
freshness in code via check_backup_freshness() before --apply-same-filename
is allowed to delete anything -- see that function's docstring for exactly
what "fresh" means and iMelki/mempalace#19's operator-decision comment for
why the scope is same-filename-only.
"""

import argparse
import glob
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

from .backends.chroma import ChromaBackend


COLLECTION_NAME = "mempalace_drawers"
# Cosine DISTANCE threshold (not similarity). Lower = stricter.
# 0.15 = ~85% cosine similarity — catches near-identical chunks.
# For looser dedup of paraphrased content, try 0.3–0.4.
DEFAULT_THRESHOLD = 0.15
MIN_DRAWERS_TO_CHECK = 5
# Document text is read in batches of this size and released; a hash scan never
# holds the whole corpus in memory (and never spills to disk — see the
# count_cross_source_duplicates() memory note).
HASH_BATCH_SIZE = 500
# How many duplicate SETS the cross-source report prints. Counts always cover
# every set; this bounds output only.
CROSS_SOURCE_MAX_SETS = 20
# How many contributing source paths to print per set before summarising.
CROSS_SOURCE_MAX_PATHS = 8
CROSS_SOURCE_SNIPPET_CHARS = 100
# The module's OWN decoration must be ASCII. _printable() protects the arbitrary
# user content this module prints, but the horizontal rules and arrows here were
# literal U+2500/U+2192 and are not routed through it -- so redirecting a run
# (`... --progress > run.log`) on a Windows host, where the redirected stream
# defaults to cp1252, died with UnicodeEncodeError before printing a single
# result or completion metric. A report must not depend on the console's codepage
# to survive (iMelki/mempalace#32).
RULE = "-" * 55

# ── same-filename-only cross-source APPLY path (iMelki/mempalace#19) ──────
# Operator policy decision, 2026-08-07: any dedup cleanup touches ONLY
# duplicate sets where EVERY contributing drawer's source_file resolves to
# the SAME basename -- one file mined from several on-disk copies of a
# project (8,145 such sets / 21,017 redundant drawers measured on the coding
# wing). The 256 sets where DIFFERENT filenames happen to share an identical
# chunk (e.g. 10 CSS skins sharing a header) are PERMANENTLY out of scope for
# deletion; see plan_same_filename_deletions() for the structural filter.
BACKUP_RECEIPT_SCHEMA = "knowledge-backup-generation-receipt.v1"
BACKUP_ARCHIVE_GLOB = "palace-*.tar.gz"
DEFAULT_BACKUP_MAX_AGE_DAYS = 2
DEFAULT_BACKUP_DIR = os.path.join(os.path.expanduser("~"), ".mempalace", "backups")
# Fractional-second component of an ISO-8601 timestamp, for _parse_iso8601().
_FRACTIONAL_SECONDS_RE = re.compile(r"(\.\d+)")


def _get_palace_path():
    """Resolve palace path from config."""
    try:
        from .config import MempalaceConfig

        return MempalaceConfig().palace_path
    except Exception:
        return os.path.join(os.path.expanduser("~"), ".mempalace", "palace")


def _progress_tick(label, done, total):
    """Overwrite one in-place percentage line for a long-running pass.

    Progress is written to STDERR, never stdout. stdout carries the report an
    operator (or a machine) consumes, and a percentage heartbeat interleaved
    into it would corrupt a structured document the moment any path emits one.
    backup_snapshot.py established this shape and stream ("Print SQLite
    online-backup progress to stderr"); the two scans added in
    iMelki/mempalace#33 wrote the same shape to stdout, corrected here
    (iMelki/mempalace#32). flush=True matters: an unflushed \\r line does not
    appear until the buffer fills, which is exactly when progress is useless.

    The percentage is clamped because a denominator can legitimately exceed the
    rows actually read -- see get_source_groups(), where a wing filter means far
    fewer rows come back than col.count() reports.
    """
    if not total:
        return
    pct = min(100.0, done * 100.0 / total)
    print(f"\r  {label}: {pct:5.1f}%", end="", file=sys.stderr, flush=True)


def _progress_end(label, done, note=""):
    """Close a progress line with the absolute count actually processed.

    A trailing percentage can mislead: a wing-scoped metadata pass stops as soon
    as Chroma runs out of matching rows, which is well below 100% of the
    collection. The closing line states the real number, and is longer than the
    tick it overwrites so no residue is left on a terminal.
    """
    suffix = f"  ({note})" if note else ""
    print(f"\r  {label}: done, {done:,} processed{suffix}", file=sys.stderr, flush=True)


def _format_metrics(metrics):
    """Render a completion-metrics dict as one grep-friendly key=value line.

    On-demand runs in this workspace are expected to end with duration,
    processed/changed counts and an outcome/status rather than leaving an
    operator to infer them from the narrative above (iMelki/mempalace#32).
    A None value means "this pass did not run", printed as not-computed so the
    line never implies a zero was measured.
    """
    return "  ".join(
        f"{key}={'not-computed' if value is None else value}" for key, value in metrics.items()
    )


def get_source_groups(
    col, min_count=MIN_DRAWERS_TO_CHECK, source_pattern=None, wing=None, progress=False
):
    """Group drawers by source_file, return groups with min_count+ entries.

    If wing is specified, only considers drawers in that wing. This catches
    cross-wing duplicates when the same source was mined into multiple wings.

    On a ~1M-drawer palace this page loop is itself the slow part, before any
    duplicate work starts, so it takes `progress` too (iMelki/mempalace#32).
    """
    total = col.count()
    groups = defaultdict(list)

    offset = 0
    scanned = 0
    batch_size = 1000
    while offset < total:
        kwargs = {"limit": batch_size, "offset": offset, "include": ["metadatas"]}
        if wing:
            kwargs["where"] = {"wing": wing}
        batch = col.get(**kwargs)
        if not batch["ids"]:
            break
        for did, meta in zip(batch["ids"], batch["metadatas"]):
            src = meta.get("source_file", "unknown")
            if source_pattern and source_pattern.lower() not in src.lower():
                continue
            groups[src].append(did)
        offset += len(batch["ids"])
        scanned += len(batch["ids"])
        if progress:
            _progress_tick("metadata scan", scanned, total)

    result = {src: ids for src, ids in groups.items() if len(ids) >= min_count}
    if progress:
        _progress_end(
            "metadata scan",
            scanned,
            note=f"{len(result):,} sources with {min_count}+ drawers",
        )
    return result


def get_cpu_usage():
    """Best-effort CPU load percentage, or None if unavailable.

    Prefers psutil. Falls back to an absolute-path WMIC call: `wmic` is
    deprecated/absent on current Windows builds, and on this workspace's host
    PATH lacks System32 entirely, so a bare-name invocation silently fails for
    every process (iMelki/projects-ops#115). Never shell out by bare name here.
    """
    try:
        import psutil

        return int(psutil.cpu_percent(interval=0.1))
    except Exception:
        pass

    try:
        import subprocess

        wmic = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", "wbem", "WMIC.exe")
        if not os.path.isfile(wmic):
            return None
        res = subprocess.run(
            [wmic, "cpu", "get", "loadpercentage"], capture_output=True, text=True, timeout=2
        )
        lines = [line.strip() for line in res.stdout.strip().splitlines() if line.strip()]
        if len(lines) > 1:
            return int(lines[1])
    except Exception:
        pass
    return None


def dedup_source_group(
    col, drawer_ids, threshold=DEFAULT_THRESHOLD, dry_run=True, progress_cb=None
):
    """Dedup drawers within one source_file group.

    Greedy: sort by doc length (longest first), keep if not too similar
    to any already-kept drawer. Returns (kept_ids, deleted_ids).

    progress_cb, if given, is called with the number of drawers just classified
    (always 1). The unit of work in this loop is one throttled col.query per
    drawer, so per-drawer is the only granularity that gives an operator real
    visibility -- a single large source can dominate an entire run, and
    per-source reporting would sit silent through it (iMelki/mempalace#32).
    """
    data = col.get(ids=drawer_ids, include=["documents", "metadatas"])
    items = list(zip(data["ids"], data["documents"], data["metadatas"]))
    items.sort(key=lambda x: len(x[1] or ""), reverse=True)

    kept = []
    to_delete = []

    for did, doc, meta in items:
        # try/finally so the early-out branches below (empty/short documents)
        # still report, otherwise a group of short drawers would look stalled.
        try:
            if not doc or len(doc) < 20:
                to_delete.append(did)
                continue

            if not kept:
                kept.append((did, doc))
                continue

            # Throttling to prevent CPU hogging during inner queries
            if len(kept) % 10 == 0:
                time.sleep(0.01)

            try:
                results = col.query(
                    query_texts=[doc],
                    n_results=min(len(kept), 5),
                    include=["distances"],
                )
                dists = results["distances"][0] if results["distances"] else []
                kept_ids_set = {k[0] for k in kept}

                is_dup = False
                for rid, dist in zip(results["ids"][0], dists):
                    if rid in kept_ids_set and dist < threshold:
                        is_dup = True
                        break

                if is_dup:
                    to_delete.append(did)
                else:
                    kept.append((did, doc))
            except Exception:
                kept.append((did, doc))
        finally:
            if progress_cb is not None:
                progress_cb(1)

    if to_delete and not dry_run:
        for i in range(0, len(to_delete), 500):
            col.delete(ids=to_delete[i : i + 500])

    return [k[0] for k in kept], to_delete


def count_exact_duplicates(col, groups, progress=False):
    """Count EXACT duplicate drawers by content hash, within each source group.

    Returns (duplicate_group_count, redundant_drawer_count).

    "Redundant" means drawers beyond the first in a set of byte-identical
    documents: a set of N identical drawers contributes N-1. This is an exact
    measurement, not an estimate, and it deliberately does NOT detect
    near-duplicates -- that requires embedding distance, which is what
    dedup_source_group() does at DEFAULT_THRESHOLD.
    """
    import hashlib

    dup_groups = 0
    redundant = 0
    scanned = 0
    total = sum(len(ids) for ids in groups.values())

    for ids in groups.values():
        seen = defaultdict(int)
        for i in range(0, len(ids), HASH_BATCH_SIZE):
            chunk = ids[i : i + HASH_BATCH_SIZE]
            data = col.get(ids=chunk, include=["documents"])
            for doc in data["documents"]:
                if not doc:
                    continue
                seen[hashlib.sha256(doc.encode("utf-8", "replace")).hexdigest()] += 1
            scanned += len(chunk)
            if progress:
                _progress_tick("exact-duplicate scan", scanned, total)
        for count in seen.values():
            if count > 1:
                dup_groups += 1
                redundant += count - 1

    if progress:
        _progress_end("exact-duplicate scan", scanned)
    return dup_groups, redundant


def _doc_snippet(doc, limit=CROSS_SOURCE_SNIPPET_CHARS):
    """One-line, whitespace-collapsed preview of a document, for operator review."""
    text = " ".join((doc or "").split())
    return text[:limit] + ("..." if len(text) > limit else "")


def _basename(path):
    """Final path segment, for BOTH separators.

    os.path.basename() does not split on backslashes when running on Linux, so a
    mined Windows path would come back whole and every set would look like it
    spanned distinct filenames on CI while looking correct locally.
    """
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def _printable(text):
    """Render text safely on the active stdout encoding.

    Drawer text and mined file paths are arbitrary user content; a Windows
    console defaults to cp1252, which cannot encode CJK or many symbols. A live
    run of the cross-source report died with UnicodeEncodeError mid-report on a
    CSS chunk containing CJK font names. A read-only audit must never crash on
    the content it is auditing, so unencodable characters are replaced. Only the
    printed form is sanitized: returned data keeps the original text.
    """
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        return text.encode(encoding, "replace").decode(encoding, "replace")
    except (LookupError, UnicodeError, TypeError):
        return text.encode("ascii", "replace").decode("ascii")


def count_cross_source_duplicates(
    col, groups, progress=False, max_sets=CROSS_SOURCE_MAX_SETS, batch_size=HASH_BATCH_SIZE
):
    """Count EXACT duplicate drawers by content hash ACROSS source_file groups.

    This is the measurement iMelki/mempalace#19 asks for. It is READ-ONLY: it
    never deletes, and it is deliberately not reachable from any --apply path.

    Why a separate mode from count_exact_duplicates():
        count_exact_duplicates() hashes within each source_file group and does
        not merge across groups (asserted by
        test_count_exact_duplicates_does_not_merge_across_source_groups). That
        scoping is correct for "was one file mined into redundant chunks?" but
        structurally blind to this palace's dominant duplication mode: five
        on-disk copies of one project, each with its own source_file path.
        Measured on wing=coding (39,461 drawers): intra-source found 20 sets /
        83 redundant drawers (0.21%), while the top source groups were five
        near-identical copies of S:\\source\\EMTS\\Repeater_System at
        1,674-1,676 drawers each.

    What "redundant" means here, stated explicitly:
        A duplicate SET is >=2 drawers whose document text is byte-identical,
        grouped regardless of source_file. A set of N drawers contributes N-1
        redundant drawers, because exactly one copy must survive to preserve the
        content. WHICH copy is canonical is a policy question this function
        refuses to answer: no winner is selected, no deletion candidate list is
        produced, and contributing source paths are reported symmetrically
        (path + drawer count) so an operator decides. Picking a canonical copy
        implicitly is how a "safe audit" turns into an unreviewed deletion plan.

    Two buckets are reported because they mean different things:
        - sets spanning 2+ distinct source paths: the same content mined from
          multiple locations (project copies, backup dirs, generated lockfiles).
          This is the actionable, cross-source redundancy.
        - sets confined to a single source path: identical chunks inside one
          source, i.e. the kind of redundancy the intra-source mode looks for.

    The single-path bucket is NOT simply the intra-source number. When text
    repeats inside source A *and* also appears in source B, the intra-source
    mode reports two separate sets while this mode merges them into one set and
    classifies it as cross-path. So intra-source redundancy that is also
    cross-source redundancy migrates out of the single-path bucket, and the
    single-path figure can be lower than the intra-source figure over the same
    drawers. Measured on wing=coding: intra-source 20 sets / 83 redundant, while
    the cross-source pass over a superset (41,934 drawers) reports 8,416 sets /
    22,300 redundant with only 15 sets / 73 redundant confined to one path.

    Memory: document text is read `batch_size` rows at a time and released, so
    the corpus is never resident and nothing spills to disk (host C: has ~2%
    free). The one unavoidable resident structure is a hash index over drawers
    in scope, which is inherent to grouping across source_file. Singleton
    hashes store a single shared source-index int (not a list), so a scan of
    ~1M drawers costs roughly a couple of hundred MB rather than gigabytes.

    Returns a dict:
        drawers_scanned, sets, redundant,
        cross_path_sets, cross_path_redundant,
        single_path_sets, single_path_redundant,
        top_sets: [{drawers, redundant, distinct_sources, sources, snippet}]
    """
    import hashlib

    source_names = []
    source_index = {}
    # digest -> source-index int while only one drawer carries that text,
    # promoted to list[int] on the second occurrence. Keeping singletons as a
    # shared int is what makes a full-palace scan affordable.
    seen = {}
    snippets = {}

    total = sum(len(ids) for ids in groups.values())
    scanned = 0

    for src, ids in groups.items():
        idx = source_index.get(src)
        if idx is None:
            idx = len(source_names)
            source_index[src] = idx
            source_names.append(src)

        for i in range(0, len(ids), batch_size):
            chunk = ids[i : i + batch_size]
            data = col.get(ids=chunk, include=["documents"])
            for doc in data["documents"]:
                if not doc:
                    continue
                digest = hashlib.sha256(doc.encode("utf-8", "replace")).digest()
                prev = seen.get(digest)
                if prev is None:
                    seen[digest] = idx
                elif isinstance(prev, list):
                    prev.append(idx)
                else:
                    seen[digest] = [prev, idx]
                    # Only duplicates get a stored snippet: the text is
                    # byte-identical by definition, so the second occurrence
                    # carries the same preview as the first.
                    snippets[digest] = _doc_snippet(doc)
            scanned += len(chunk)
            if progress:
                _progress_tick("cross-source duplicate scan", scanned, total)

    if progress:
        _progress_end("cross-source duplicate scan", scanned)

    sets_total = 0
    redundant_total = 0
    cross_path_sets = 0
    cross_path_redundant = 0
    same_name_sets = 0
    same_name_redundant = 0
    ranked = []

    for digest, val in seen.items():
        if not isinstance(val, list):
            continue
        n = len(val)
        sets_total += 1
        redundant_total += n - 1
        distinct = set(val)
        if len(distinct) > 1:
            cross_path_sets += 1
            cross_path_redundant += n - 1
            # Two very different situations both land in the cross-path bucket:
            # one file present in several trees (all paths share a filename), and
            # different files that happen to contain an identical chunk (shared
            # boilerplate, e.g. 50 sibling CSS skins). Their deletion policy is
            # not the same, so the split is reported as fact, not judged here.
            if len({_basename(source_names[i]) for i in distinct}) == 1:
                same_name_sets += 1
                same_name_redundant += n - 1
        ranked.append((n, digest, val))

    # Ordering is for legibility only and carries NO canonicality signal.
    ranked.sort(key=lambda item: (-item[0], sorted(source_names[i] for i in set(item[2]))))

    top_sets = []
    for n, digest, indices in ranked[:max_sets]:
        counts = Counter(source_names[i] for i in indices)
        top_sets.append(
            {
                "drawers": n,
                "redundant": n - 1,
                "distinct_sources": len(counts),
                "distinct_filenames": len({_basename(p) for p in counts}),
                "sources": sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])),
                "snippet": snippets.get(digest, ""),
            }
        )

    return {
        "drawers_scanned": scanned,
        "sets": sets_total,
        "redundant": redundant_total,
        "cross_path_sets": cross_path_sets,
        "cross_path_redundant": cross_path_redundant,
        "single_path_sets": sets_total - cross_path_sets,
        "single_path_redundant": redundant_total - cross_path_redundant,
        "same_filename_sets": same_name_sets,
        "same_filename_redundant": same_name_redundant,
        "top_sets": top_sets,
    }


def print_cross_source_report(result, max_paths=CROSS_SOURCE_MAX_PATHS):
    """Print a cross-source duplicate report: counts, then contributing paths.

    A bare count is not actionable. "These 4 paths hold identical content" is.
    """
    sets = f"{result['sets']:,}"
    redundant = f"{result['redundant']:,}"
    print(f"\n  Cross-source exact-duplicate sets (grouped across source_file): {sets}")
    print(f"  Redundant drawers in those sets (N-1 per set):                  {redundant}")
    print(
        f"    sets spanning 2+ distinct source paths: {result['cross_path_sets']:,}"
        f"  ({result['cross_path_redundant']:,} redundant)"
    )
    print(
        f"      of those, sets whose paths all share one filename: "
        f"{result['same_filename_sets']:,}"
        f"  ({result['same_filename_redundant']:,} redundant)"
    )
    print(
        f"    sets confined to a single source path:  {result['single_path_sets']:,}"
        f"  ({result['single_path_redundant']:,} redundant)"
    )
    print(f"  Drawers hashed: {result['drawers_scanned']:,}")

    if result["top_sets"]:
        print(f"\n  Largest duplicate sets (top {len(result['top_sets'])}) and their source paths:")
        for i, dup in enumerate(result["top_sets"], 1):
            print(
                f"    [{i}] {dup['drawers']} identical drawers across "
                f"{dup['distinct_sources']} source path(s) "
                f"({dup['distinct_filenames']} distinct filename(s)) "
                f"-> {dup['redundant']} redundant"
            )
            if dup["snippet"]:
                print('        text: "' + _printable(dup["snippet"]) + '"')
            for path, count in dup["sources"][:max_paths]:
                print(f"        {count:5d}  {_printable(path)}")
            hidden = len(dup["sources"]) - max_paths
            if hidden > 0:
                print(f"        ... +{hidden} more source path(s)")

    print("\n  NOTE: exact byte-identical matches only. Near-duplicates need an embedding pass.")
    print("  NOTE: read-only audit. No canonical copy is chosen and no deletion is proposed;")
    print("        'redundant' is N-1 per set, but WHICH copy to keep is an operator decision.")
    print("  NOTE: 1 distinct filename across many paths = one file mined from several trees.")
    print("        Several filenames = different files sharing a chunk (e.g. CSS boilerplate);")
    print("        that is still stored N times, but it is not a copied-directory artifact.")


# ── same-filename-only cross-source APPLY path (iMelki/mempalace#19) ──────
#
# Everything above this line (count_cross_source_duplicates,
# print_cross_source_report) is the read-only audit mode from #19: it counts,
# it never deletes, and it deliberately never picks a canonical copy. This
# section is the thing that audit was missing -- an actual, tested, gated
# deletion path -- built strictly to the operator policy decision recorded on
# #19 (2026-08-07): ONLY same-filename cross-source sets are ever eligible.
# Mixed-filename sets (shared boilerplate across distinct files) are excluded
# by construction in plan_same_filename_deletions(), with no parameter of any
# kind that widens that scope -- there is no "force" or "include mixed"
# switch to bypass it, on this function or on apply_same_filename_dedup().


def _parse_iso8601(value):
    """Tolerant ISO-8601 -> aware UTC datetime, for backup receipt timestamps.

    Real generation receipts come from at least two writers this function
    must both understand: this repo's own `generatedAt` convention
    (`datetime.now(timezone.utc).replace(microsecond=0).isoformat()` with a
    trailing "Z", see backup_snapshot._utc_now()) and .NET's round-trip "o"
    format used by the agent-settings backup pipeline that actually produces
    `<archive>.tar.gz.receipt.json` -- which always emits 7-digit fractional
    seconds and an explicit "+00:00" offset, e.g.
    "2026-07-12T01:20:49.7912623+00:00". `datetime.fromisoformat()` on the
    oldest CI-tested interpreter (3.9) accepts only 0, 3, or 6 fractional
    digits and does not understand a trailing "Z" at all (that arrived in
    3.11), so both quirks are normalized here instead of assumed away. Raises
    ValueError on anything it cannot parse; callers treat that as an invalid
    receipt, never as "fresh enough".
    """
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    def _pad_to_six(match):
        digits = (match.group(1)[1:] + "000000")[:6]
        return "." + digits

    text = _FRACTIONAL_SECONDS_RE.sub(_pad_to_six, text, count=1)
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _receipt_is_known_good(receipt):
    """Same "knownGood" proof set as agent-settings'
    Get-KnowledgeBackupRetentionPlan.ps1 (iMelki/agent-settings#456): a
    receipt only counts if the archive's creation AND structural validation
    are verified, the offsite copy is verified via a real method (not
    "pending"/"none"/"not-configured"), a disposable restore was verified,
    and the run's own terminal status was a clean success.

    Deliberately NOT reproduced here: that tool's optional current-archive
    SHA-256 recompute (`-VerifyArchiveHashes`). That check is itself opt-in
    in the PS retention planner and is a *retention-planning* concern (is
    this specific archive's bytes still intact on disk), not a per-apply
    freshness precondition -- recomputing a multi-GB hash on every dedup
    invocation would make the safety check itself the slow, disk-heavy part
    of a run whose whole point is to be cheap enough to run before every
    apply. Returns (ok: bool, reasons: list[str]); reasons is empty iff ok.
    """
    reasons = []
    if not isinstance(receipt, dict):
        return False, ["generation-receipt-unreadable"]
    if receipt.get("schema") != BACKUP_RECEIPT_SCHEMA:
        reasons.append("generation-receipt-schema-mismatch")

    archive = receipt.get("archive") or {}
    offsite = receipt.get("offsite") or {}
    restore = receipt.get("restore") or {}
    terminal = receipt.get("terminal") or {}

    if archive.get("creationStatus") != "verified":
        reasons.append("archive-creation-not-verified")
    if archive.get("structuralValidationStatus") != "verified":
        reasons.append("archive-structure-not-verified")
    if offsite.get("status") != "verified":
        reasons.append("offsite-copy-not-verified")
    if offsite.get("method") not in ("cryptcheck", "checksum-and-retrieval"):
        reasons.append("offsite-verification-method-insufficient")
    if restore.get("status") != "verified":
        reasons.append("disposable-restore-not-verified")
    if terminal.get("status") != "succeeded" or terminal.get("exitCode") != 0:
        reasons.append("generation-terminal-state-not-success")

    return not reasons, reasons


def check_backup_freshness(backup_dir=None, max_age_days=DEFAULT_BACKUP_MAX_AGE_DAYS, now=None):
    """Fail-closed precondition: does a known-good, fresh palace backup exist?

    This is the check dedup_palace()'s docstring has always DESCRIBED
    ("Live deletion requires an explicit --apply, a palace backup from the
    last 2 days...") without ever ENFORCING in code -- the 2026-07-05
    incident (iMelki/mempalace#19) happened with that requirement stated and
    unchecked: 42,606 drawers were deleted with the newest backup already
    past the 2-day window. This function exists so any new apply path can
    refuse to run instead of documenting a requirement nobody checks.

    Looks for `<backup_dir>/palace-*.tar.gz` archives with an adjacent
    `<archive>.tar.gz.receipt.json` generation receipt (schema
    knowledge-backup-generation-receipt.v1, iMelki/agent-settings#456) and
    treats an archive as usable only if ALL of:
      - _receipt_is_known_good() passes (verified creation/structure/offsite/
        restore/terminal -- see that function for exact fields);
      - the receipt's createdAt is within max_age_days of `now` (default: the
        real current time) and not in the future.

    This function only reads files under backup_dir; it never creates,
    fetches, uploads, or repairs a backup, and it never touches the palace.
    Any missing directory, missing archive, missing/unreadable/invalid
    receipt, or stale timestamp fails CLOSED (ok=False) -- there is no
    partial-credit path. Multiple archives may qualify; `ok` is True if at
    least one does.

    Returns a dict: ok, backup_dir, max_age_days, archives_checked,
    known_good_count, newest_known_good_age_days, reason, problems (one
    string per archive that did not qualify, or per non-archive failure).
    """
    backup_dir = backup_dir or DEFAULT_BACKUP_DIR
    now = now or datetime.now(timezone.utc)
    result = {
        "ok": False,
        "backup_dir": str(backup_dir),
        "max_age_days": max_age_days,
        "archives_checked": 0,
        "known_good_count": 0,
        "newest_known_good_age_days": None,
        "reason": "",
        "problems": [],
    }

    if not os.path.isdir(backup_dir):
        result["reason"] = f"backup directory not found: {backup_dir}"
        result["problems"].append("backup-directory-missing")
        return result

    archive_paths = sorted(glob.glob(os.path.join(backup_dir, BACKUP_ARCHIVE_GLOB)))
    result["archives_checked"] = len(archive_paths)
    if not archive_paths:
        result["reason"] = f"no backup archives matching {BACKUP_ARCHIVE_GLOB} in {backup_dir}"
        result["problems"].append("no-archives-present")
        return result

    best_age_days = None
    per_archive_problems = {}

    for archive_path in archive_paths:
        name = os.path.basename(archive_path)
        receipt_path = archive_path + ".receipt.json"

        if not os.path.isfile(receipt_path):
            per_archive_problems[name] = ["generation-receipt-missing"]
            continue
        try:
            with open(receipt_path, encoding="utf-8") as fh:
                receipt = json.load(fh)
        except (OSError, ValueError) as exc:
            per_archive_problems[name] = [f"generation-receipt-unreadable:{exc}"]
            continue

        ok, reasons = _receipt_is_known_good(receipt)
        if not ok:
            per_archive_problems[name] = reasons
            continue

        try:
            created_at = _parse_iso8601(str(receipt.get("createdAt", "")))
        except (ValueError, AttributeError):
            per_archive_problems[name] = ["generation-created-at-invalid"]
            continue

        age_days = (now - created_at).total_seconds() / 86400.0
        if age_days < 0:
            # A receipt timestamped in the future is untrustworthy, not
            # "extra fresh" -- treat it the same as an invalid timestamp.
            per_archive_problems[name] = ["generation-created-at-in-future"]
            continue
        if age_days > max_age_days:
            per_archive_problems[name] = [f"generation-stale:{age_days:.2f}d>{max_age_days}d"]
            continue

        result["known_good_count"] += 1
        if best_age_days is None or age_days < best_age_days:
            best_age_days = age_days

    result["newest_known_good_age_days"] = (
        None if best_age_days is None else round(best_age_days, 3)
    )
    result["problems"] = [
        f"{name}: {'; '.join(reasons)}" for name, reasons in sorted(per_archive_problems.items())
    ]

    if result["known_good_count"] > 0:
        result["ok"] = True
        result["reason"] = (
            f"{result['known_good_count']} known-good archive(s) within {max_age_days} "
            f"day(s); newest is {result['newest_known_good_age_days']:.2f}d old"
        )
    else:
        result["reason"] = (
            f"no known-good backup archive within {max_age_days} day(s) among "
            f"{len(archive_paths)} archive(s) checked in {backup_dir}"
        )
    return result


def plan_same_filename_deletions(col, groups, batch_size=HASH_BATCH_SIZE, progress=False):
    """Build a same-filename-only cross-source deletion plan.

    This is the ONLY set of drawer ids this module will ever propose for
    deletion outside the pre-existing embedding-based dedup_source_group()
    path. It hashes document text across the whole scoped set exactly like
    count_cross_source_duplicates(), but -- unlike that read-only function,
    which deliberately discards drawer ids to stay cheap on a ~1M-drawer scan
    -- this one keeps them, because turning a measurement into a deletion
    plan requires knowing which specific drawers to keep and delete.

    A digest's set of drawers becomes a deletion candidate if and only if:
      1. len(entries) >= 2 (an actual duplicate, not a singleton);
      2. the entries span 2+ DISTINCT source_file paths (cross-source, not
         the kind --exact-duplicates already looks for);
      3. every one of those distinct paths reduces to the SAME basename via
         _basename().

    Any set failing (2) or (3) -- including every one of the 256
    mixed-filename shared-boilerplate sets from #19's own audit -- is
    dropped here, structurally, before a delete id is ever produced. There
    is no parameter to relax this. A set failing only (2) (repeats confined
    to one source path) is the domain of --exact-duplicates, not this path.

    Keep-longest, reusing dedup_source_group()'s convention: within a
    qualifying set every drawer's document text is byte-identical by
    definition (same hash implies same content implies same length), so
    "keep longest" always ties on length -- the sort key is `(-len(doc),
    drawer_id)`, so ties fall to the lowest drawer id. That keeps the choice
    deterministic across repeated dry-runs rather than depending on dict or
    set iteration order, without inventing a "richness" heuristic content
    identity cannot distinguish.

    Memory/IO: identical batching to count_cross_source_duplicates()
    (`batch_size` rows read and released at a time). Unlike that function,
    this one DOES hold full (drawer_id, source_file, doc_length) tuples per
    digest rather than a compact source-index int, because it needs real ids
    to delete -- so it is intentionally only ever run over an already-scoped
    subset (a --wing/--source filter), not blindly over the whole palace.

    Returns a dict: drawers_scanned, sets (list of {filename, digest,
    keep_id, delete_ids, redundant, distinct_paths, paths}, largest first),
    sets_count, redundant_total, delete_ids (flattened, every id in every
    set's delete_ids -- this is the actual deletion candidate list).
    """
    import hashlib

    total = sum(len(ids) for ids in groups.values())
    scanned = 0
    # digest -> [(drawer_id, source_file, doc_length), ...]
    buckets = defaultdict(list)

    for src, ids in groups.items():
        for i in range(0, len(ids), batch_size):
            chunk = ids[i : i + batch_size]
            data = col.get(ids=chunk, include=["documents"])
            for did, doc in zip(chunk, data["documents"]):
                if not doc:
                    continue
                digest = hashlib.sha256(doc.encode("utf-8", "replace")).hexdigest()
                buckets[digest].append((did, src, len(doc)))
            scanned += len(chunk)
            if progress:
                _progress_tick("same-filename plan scan", scanned, total)

    if progress:
        _progress_end("same-filename plan scan", scanned)

    sets = []
    for digest, entries in buckets.items():
        if len(entries) < 2:
            continue
        distinct_paths = sorted({src for _, src, _ in entries})
        if len(distinct_paths) < 2:
            continue  # single-path: --exact-duplicates' scope, not this one
        distinct_names = {_basename(p) for p in distinct_paths}
        if len(distinct_names) != 1:
            continue  # mixed filenames: PERMANENTLY out of scope (#19 decision)

        ordered = sorted(entries, key=lambda entry: (-entry[2], entry[0]))
        keep_id = ordered[0][0]
        delete_ids = [entry[0] for entry in ordered[1:]]

        sets.append(
            {
                "filename": next(iter(distinct_names)),
                "digest": digest,
                "keep_id": keep_id,
                "delete_ids": delete_ids,
                "redundant": len(delete_ids),
                "distinct_paths": len(distinct_paths),
                "paths": distinct_paths,
            }
        )

    # Ordering is for legibility only, same convention as
    # count_cross_source_duplicates(): largest set first, filename as a
    # deterministic tiebreak.
    sets.sort(key=lambda s: (-s["redundant"], s["filename"]))

    delete_ids_all = [did for s in sets for did in s["delete_ids"]]
    return {
        "drawers_scanned": scanned,
        "sets": sets,
        "sets_count": len(sets),
        "redundant_total": len(delete_ids_all),
        "delete_ids": delete_ids_all,
    }


def print_same_filename_plan(
    plan, max_sets=CROSS_SOURCE_MAX_SETS, max_paths=CROSS_SOURCE_MAX_PATHS
):
    """Print a same-filename deletion plan: counts, then the sets themselves.

    Every printed set names its filename, which drawer would be kept, and
    every contributing path -- so a dry-run report is reviewable before any
    --apply-same-filename run, not just a bare count.
    """
    print(f"\n  Same-filename cross-source duplicate sets: {plan['sets_count']:,}")
    print(f"  Redundant drawers (deletion candidates):    {plan['redundant_total']:,}")
    print(f"  Drawers hashed: {plan['drawers_scanned']:,}")

    if plan["sets"]:
        shown = plan["sets"][:max_sets]
        print(f"\n  Sets (top {len(shown)} of {plan['sets_count']:,}):")
        for i, s in enumerate(shown, 1):
            print(
                f"    [{i}] {_printable(s['filename'])} -- {s['redundant']} redundant across "
                f"{s['distinct_paths']} path(s); keep={s['keep_id']}"
            )
            for path in s["paths"][:max_paths]:
                print(f"        {_printable(path)}")
            hidden = len(s["paths"]) - max_paths
            if hidden > 0:
                print(f"        ... +{hidden} more source path(s)")

    print("\n  NOTE: only sets where every contributing path shares ONE filename are ever")
    print("        deletion candidates. Mixed-filename shared-boilerplate sets are")
    print("        PERMANENTLY out of scope (operator decision, iMelki/mempalace#19).")


def apply_same_filename_dedup(
    palace_path=None,
    dry_run=True,
    wing=None,
    source_pattern=None,
    backup_dir=None,
    backup_max_age_days=DEFAULT_BACKUP_MAX_AGE_DAYS,
    progress=False,
    max_sets=CROSS_SOURCE_MAX_SETS,
):
    """Same-filename-only cross-source dedup: the APPLY path for #19.

    Scope is deliberately narrow and non-negotiable: plan_same_filename_deletions()
    structurally excludes every set that is not cross-source AND same-filename,
    with no parameter anywhere in this call chain to widen that. Dry-run is the
    unconditional default -- `dry_run` must be explicitly False to delete
    anything, matching dedup_palace()'s existing --apply convention.

    Even with dry_run=False, nothing is deleted unless check_backup_freshness()
    reports ok=True for `backup_dir` (default ~/.mempalace/backups) within
    `backup_max_age_days` (default 2). A blocked gate is not an error: it
    prints the reason, computes and reports the plan that WOULD have run, and
    returns with drawers_removed=0 and outcome="blocked-backup-not-fresh" --
    the same shape a caller would see from a clean dry run, but distinguishable
    by outcome/status so a script cannot mistake a refusal for a no-op.

    Returns the completion-metrics dict it also prints (see _format_metrics),
    matching dedup_palace()'s and show_stats()'s existing contract.
    """
    started = time.monotonic()
    palace_path = palace_path or _get_palace_path()

    print(f"\n{'=' * 55}")
    print("  MemPalace Same-Filename Cross-Source Dedup")
    print(f"{'=' * 55}")

    col = ChromaBackend().get_collection(palace_path, COLLECTION_NAME)

    print(f"  Palace: {_printable(str(palace_path))}")
    print(f"  Drawers: {col.count():,}")
    print(f"  Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    print(f"{RULE}")

    if wing:
        print(f"  Wing: {wing}")

    # min_count=1, same reasoning as --cross-source-duplicates: five copies of
    # one file can be one drawer each, and the default min_count=5 would hide
    # exactly the duplication this mode looks for.
    groups = get_source_groups(
        col, min_count=1, source_pattern=source_pattern, wing=wing, progress=progress
    )
    print(f"\n  Sources in scope: {len(groups)}")

    plan = plan_same_filename_deletions(col, groups, progress=progress)
    print_same_filename_plan(plan, max_sets=max_sets)

    outcome = "ok"
    status = "dry-run"
    backup_check = None
    deleted = 0

    if dry_run:
        print("\n  [DRY RUN] No changes written.")
        print("  Live deletion requires dry_run=False (CLI: --apply-same-filename) AND a")
        print("  passing backup-freshness gate (see check_backup_freshness()).")
    elif not plan["delete_ids"]:
        status = "no-op"
        print("\n  Nothing to delete: no same-filename cross-source duplicate sets in scope.")
    else:
        backup_check = check_backup_freshness(
            backup_dir=backup_dir, max_age_days=backup_max_age_days
        )
        print(f"\n  Backup-freshness gate: {'PASS' if backup_check['ok'] else 'BLOCKED'}")
        print(f"    {backup_check['reason']}")

        if not backup_check["ok"]:
            outcome = "blocked-backup-not-fresh"
            status = "blocked"
            print(
                "\n  [BLOCKED] Live deletion refused: no known-good, offsite-verified "
                f"backup within {backup_max_age_days} day(s). See iMelki/mempalace#19."
            )
            for problem in backup_check["problems"][:10]:
                print(f"    - {problem}")
            hidden = len(backup_check["problems"]) - 10
            if hidden > 0:
                print(f"    ... +{hidden} more")
        else:
            status = "applied"
            for i in range(0, len(plan["delete_ids"]), 500):
                col.delete(ids=plan["delete_ids"][i : i + 500])
            deleted = len(plan["delete_ids"])
            print(f"\n  Deleted {deleted:,} redundant drawers across {plan['sets_count']:,} sets.")

    palace_after = col.count()
    print(f"\n  Palace after: {palace_after:,} drawers")

    warm_seconds = None
    if not dry_run and deleted:
        # Same post-mutation warm as dedup_palace() (iMelki/mempalace#19): pay
        # the one-time post-deletion cost here, at mutation time.
        print("\n  Pre-warming palace after deletions...")
        t_warm = time.monotonic()
        try:
            from .searcher import search_memories

            search_memories("warmup", palace_path, n_results=1)
            warm_seconds = round(time.monotonic() - t_warm, 3)
            print(f"  Palace warm in {warm_seconds:.1f}s")
        except Exception as e:
            print(f"  Warm warning (non-fatal): {e}")
            if outcome == "ok":
                outcome = "ok-with-warnings"

    metrics = {
        "operation": "dedup-same-filename",
        "outcome": outcome,
        "status": status,
        "duration_seconds": round(time.monotonic() - started, 3),
        "sources_in_scope": len(groups),
        "drawers_hashed": plan["drawers_scanned"],
        "same_filename_sets": plan["sets_count"],
        "redundant_drawers_found": plan["redundant_total"],
        "drawers_removed": deleted,
        "backup_gate_ok": None if backup_check is None else backup_check["ok"],
        "backup_gate_reason": None if backup_check is None else backup_check["reason"],
        "palace_drawers_after": palace_after,
        "warm_seconds": warm_seconds,
    }
    print(f"\n  Metrics: {_format_metrics(metrics)}")
    print(f"{'=' * 55}\n")
    return metrics


def show_stats(
    palace_path=None,
    wing=None,
    source_pattern=None,
    min_count=MIN_DRAWERS_TO_CHECK,
    exact_duplicates=False,
    cross_source_duplicates=False,
    progress=False,
    max_sets=CROSS_SOURCE_MAX_SETS,
):
    """Show duplication statistics without making changes.

    Returns the completion-metrics dict it also prints (see _format_metrics).
    """
    started = time.monotonic()
    palace_path = palace_path or _get_palace_path()
    col = ChromaBackend().get_collection(palace_path, COLLECTION_NAME)

    # Scope must be both applied AND printed. Previously this function took no
    # wing/source arguments at all, so `--wing X --stats` silently scanned the
    # whole palace and reported cross-wing sources as if they were in scope
    # (iMelki/mempalace#33).
    print(f"\n  Palace: {_printable(str(palace_path))}")
    print(
        f"  Scope:  wing={wing or 'ALL'}  source={source_pattern or 'ALL'}  min_count={min_count}"
    )

    scoped_groups = None
    if cross_source_duplicates:
        # Cross-source hashing must cover the WHOLE scoped set. min_count exists
        # to skip sources too small to contain intra-source duplication, but
        # five copies of one file can be one drawer each — filtering them out
        # would hide exactly the duplication this mode looks for. One metadata
        # pass serves both views.
        scoped_groups = get_source_groups(
            col, min_count=1, source_pattern=source_pattern, wing=wing, progress=progress
        )
        groups = {src: ids for src, ids in scoped_groups.items() if len(ids) >= min_count}
    else:
        groups = get_source_groups(
            col, min_count=min_count, source_pattern=source_pattern, wing=wing, progress=progress
        )

    total_drawers = sum(len(ids) for ids in groups.values())
    print(f"\n  Sources with {min_count}+ drawers: {len(groups)}")
    print(f"  Total drawers in those sources: {total_drawers:,}")
    if scoped_groups is not None:
        print(
            f"  Drawers in scope incl. sources under min_count: "
            f"{sum(len(ids) for ids in scoped_groups.values()):,} "
            f"across {len(scoped_groups)} sources"
        )

    print("\n  Top 15 by drawer count:")
    sorted_groups = sorted(groups.items(), key=lambda x: len(x[1]), reverse=True)
    for src, ids in sorted_groups[:15]:
        # Mined paths are arbitrary content and can be unencodable on a cp1252
        # console; the listing must not abort the audit (see _printable).
        print(f"    {len(ids):5d}  {_printable(src[:65])}")

    # A large drawer count for one source is NOT duplication: chunking one big
    # document into many verbatim drawers is the intended design. This used to
    # print `sum(len(ids) * 0.4)` for groups > 20 as "estimated duplicates",
    # which measured nothing and implied ~36% of the palace was redundant
    # (iMelki/mempalace#33). Report a real number or none at all.
    passes = []
    drawers_hashed = 0
    exact_sets = exact_redundant = None
    cross_sets = cross_redundant = None

    if exact_duplicates:
        passes.append("exact")
        exact_sets, exact_redundant = count_exact_duplicates(col, groups, progress=progress)
        # This pass reads the document text of every drawer in `groups`.
        drawers_hashed += total_drawers
        print(f"\n  Exact-duplicate sets (byte-identical documents): {exact_sets:,}")
        print(f"  Redundant drawers in those sets:                 {exact_redundant:,}")
        print("  NOTE: exact matches only. Near-duplicates need an embedding pass.")
        print("  NOTE: grouped WITHIN each source_file. Content mined from several")
        print("        on-disk copies of one project is invisible here -- use")
        print("        --cross-source-duplicates for that (iMelki/mempalace#19).")

    if cross_source_duplicates:
        passes.append("cross-source")
        result = count_cross_source_duplicates(
            col, scoped_groups, progress=progress, max_sets=max_sets
        )
        cross_sets = result["sets"]
        cross_redundant = result["redundant"]
        drawers_hashed += result["drawers_scanned"]
        print_cross_source_report(result)

    if not passes:
        print("\n  Duplicate counts not computed (metadata-only pass).")
        print("  Re-run with --exact-duplicates for a real byte-identical count,")
        print("  --cross-source-duplicates to group identical text across source paths,")
        print("  or run without --stats for the embedding-distance dry-run preview.")

    # Completion metrics for an on-demand run: duration, what was processed, and
    # an explicit status naming which passes actually ran (iMelki/mempalace#32).
    # Counts a pass did not compute stay None rather than 0 -- see _format_metrics.
    metrics = {
        "operation": "dedup-stats",
        "outcome": "ok",
        "status": "+".join(passes) if passes else "metadata-only",
        "duration_seconds": round(time.monotonic() - started, 3),
        "sources_in_scope": len(groups),
        "drawers_in_scope": total_drawers,
        "drawers_hashed": drawers_hashed,
        "exact_duplicate_sets": exact_sets,
        "exact_redundant_drawers": exact_redundant,
        "cross_source_duplicate_sets": cross_sets,
        "cross_source_redundant_drawers": cross_redundant,
    }
    print(f"\n  Metrics: {_format_metrics(metrics)}")
    return metrics


def dedup_palace(
    palace_path=None,
    threshold=DEFAULT_THRESHOLD,
    dry_run=True,
    source_pattern=None,
    min_count=MIN_DRAWERS_TO_CHECK,
    wing=None,
    progress=False,
):
    """Main entry point: deduplicate near-identical drawers across the palace.

    This is the long pass: one throttled col.query per drawer. `progress` emits
    a per-drawer heartbeat on stderr, and the run always ends with completion
    metrics (iMelki/mempalace#32). Returns the metrics dict it also prints.
    """
    started = time.monotonic()
    palace_path = palace_path or _get_palace_path()

    print(f"\n{'=' * 55}")
    print("  MemPalace Deduplicator")
    print(f"{'=' * 55}")

    col = ChromaBackend().get_collection(palace_path, COLLECTION_NAME)

    print(f"  Palace: {palace_path}")
    print(f"  Drawers: {col.count():,}")
    print(f"  Threshold: {threshold}")
    print(f"  Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    print(f"{RULE}")

    if wing:
        print(f"  Wing: {wing}")
    groups = get_source_groups(col, min_count, source_pattern, wing=wing, progress=progress)
    print(f"\n  Sources to check: {len(groups)}")

    t0 = time.monotonic()
    total_kept = 0
    total_deleted = 0

    drawers_in_scope = sum(len(ids) for ids in groups.values())
    seen = {"drawers": 0}

    def _drawer_progress(count):
        seen["drawers"] += count
        _progress_tick("embedding dedup", seen["drawers"], drawers_in_scope)

    # None when progress is off, so a non-interactive run pays no per-drawer
    # callback at all and emits nothing.
    progress_cb = _drawer_progress if progress else None

    sorted_groups = sorted(groups.items(), key=lambda x: len(x[1]), reverse=True)

    for i, (src, drawer_ids) in enumerate(sorted_groups):
        # Throttling between source files to keep CPU load low
        if i % 5 == 0:
            cpu = get_cpu_usage()
            if cpu and cpu > 70:
                time.sleep(2.0)
            else:
                time.sleep(0.1)

        kept, deleted = dedup_source_group(
            col, drawer_ids, threshold, dry_run, progress_cb=progress_cb
        )
        total_kept += len(kept)
        total_deleted += len(deleted)

        if deleted:
            print(
                f"  [{i + 1:3d}/{len(groups)}] "
                # Mined paths are arbitrary content; a cp1252 console cannot
                # encode all of it and must not abort the run (see _printable).
                f"{_printable(src[:50]):50s} {len(drawer_ids):4d} -> {len(kept):4d}  "
                f"(-{len(deleted)})"
            )

    elapsed = time.monotonic() - t0
    if progress:
        _progress_end("embedding dedup", seen["drawers"], note=f"{len(groups):,} sources")

    palace_after = col.count()

    print(f"\n{RULE}")
    print(f"  Done in {elapsed:.1f}s")
    print(
        f"  Drawers: {total_kept + total_deleted:,} -> {total_kept:,}  (-{total_deleted:,} removed)"
    )
    print(f"  Palace after: {palace_after:,} drawers")

    if dry_run:
        print("\n  [DRY RUN] No changes written. Re-run with --apply to delete.")

    outcome = "ok"
    warm_seconds = None

    if not dry_run and total_deleted:
        # Post-mutation warm (iMelki/mempalace#19): the first palace open
        # after bulk deletions can do heavy one-time work (measured 1,004s
        # after a 42,606-drawer dedup). Pay that cost here, at mutation time,
        # instead of ambushing the next bridge start or agent query.
        print("\n  Pre-warming palace after deletions...")
        t_warm = time.monotonic()
        try:
            from .searcher import search_memories

            search_memories("warmup", palace_path, n_results=1)
            warm_seconds = round(time.monotonic() - t_warm, 3)
            print(f"  Palace warm in {warm_seconds:.1f}s")
        except Exception as e:
            print(f"  Warm warning (non-fatal): {e}")
            # A skipped warm is not a failed dedup, but it is not a clean run
            # either: the next reader pays the cost this pass was meant to absorb.
            outcome = "ok-with-warnings"

    # Completion metrics for an on-demand run (iMelki/mempalace#32). flagged vs
    # removed is the load-bearing distinction: a dry-run identifies candidates
    # and changes nothing, so drawers_removed is 0 until --apply.
    metrics = {
        "operation": "dedup",
        "outcome": outcome,
        "status": "dry-run" if dry_run else "applied",
        "duration_seconds": round(time.monotonic() - started, 3),
        "sources_processed": len(groups),
        "drawers_processed": total_kept + total_deleted,
        "drawers_kept": total_kept,
        "drawers_flagged": total_deleted,
        "drawers_removed": 0 if dry_run else total_deleted,
        "palace_drawers_after": palace_after,
        "warm_seconds": warm_seconds,
    }
    print(f"\n  Metrics: {_format_metrics(metrics)}")

    print(f"{'=' * 55}\n")
    return metrics


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Deduplicate near-identical drawers")
    parser.add_argument("--palace", default=None, help="Palace directory path")
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help=f"Cosine distance threshold (default: {DEFAULT_THRESHOLD})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview without deleting (this is already the default)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete duplicates. Without this flag dedup always runs "
        "as a dry-run preview - live deletion must be explicitly requested.",
    )
    parser.add_argument("--stats", action="store_true", help="Show stats only")
    parser.add_argument("--wing", default=None, help="Scope dedup to a single wing")
    parser.add_argument("--source", default=None, help="Filter by source file pattern")
    parser.add_argument(
        "--exact-duplicates",
        action="store_true",
        help="With --stats, compute a real byte-identical duplicate count "
        "(reads document text, so it is slower than the metadata-only pass)",
    )
    parser.add_argument(
        "--cross-source-duplicates",
        "--cross-source",
        dest="cross_source_duplicates",
        action="store_true",
        help="With --stats, compute a byte-identical duplicate count that groups "
        "ACROSS source_file (the mode iMelki/mempalace#19 asks for) and report the "
        "contributing source paths per set. Read-only: it never deletes and is not "
        "reachable from --apply. `--cross-source` is accepted as an alias.",
    )
    parser.add_argument(
        "--max-sets",
        type=int,
        default=CROSS_SOURCE_MAX_SETS,
        help=f"How many duplicate sets --cross-source-duplicates prints "
        f"(default: {CROSS_SOURCE_MAX_SETS}). Counts always cover every set.",
    )
    parser.add_argument(
        "--progress",
        action="store_true",
        help="Print incremental progress during long passes: the metadata page "
        "loop, the hash scans, and the embedding-distance dry-run. Written to "
        "stderr only, so stdout stays a clean report channel. Off by default, so "
        "non-interactive callers and tests are unaffected.",
    )
    parser.add_argument(
        "--same-filename-cleanup",
        action="store_true",
        help="Select the same-filename-only cross-source dedup mode (iMelki/mempalace#19 "
        "operator decision, 2026-08-07). Dry-run by default: prints the deletion plan "
        "without deleting. Mutually exclusive with --stats and --apply -- it is its own "
        "mode, not a modifier of the embedding-based path.",
    )
    parser.add_argument(
        "--apply-same-filename",
        action="store_true",
        help="With --same-filename-cleanup, actually delete the same-filename cross-source "
        "duplicates found. Requires --same-filename-cleanup. Even then, nothing is deleted "
        "unless check_backup_freshness() finds a known-good, offsite-verified backup within "
        "--backup-max-age-days -- see that function's docstring.",
    )
    parser.add_argument(
        "--backup-dir",
        default=None,
        help=f"Where --apply-same-filename looks for palace-*.tar.gz backup archives and "
        f"their .receipt.json files (default: {DEFAULT_BACKUP_DIR}).",
    )
    parser.add_argument(
        "--backup-max-age-days",
        type=float,
        default=DEFAULT_BACKUP_MAX_AGE_DAYS,
        help=f"How fresh a known-good backup must be for --apply-same-filename to proceed "
        f"(default: {DEFAULT_BACKUP_MAX_AGE_DAYS} days).",
    )
    args = parser.parse_args()

    path = os.path.expanduser(args.palace) if args.palace else None
    backup_dir = os.path.expanduser(args.backup_dir) if args.backup_dir else None

    if args.apply and args.dry_run:
        parser.error("--apply and --dry-run are mutually exclusive")

    if args.exact_duplicates and not args.stats:
        parser.error("--exact-duplicates only applies to --stats")

    # Cross-source is a read-only measurement mode. Refusing it outside --stats
    # keeps it structurally unable to sit on the deletion path: dedup_palace()
    # never sees the flag (iMelki/mempalace#19).
    if args.cross_source_duplicates and not args.stats:
        parser.error("--cross-source-duplicates only applies to --stats")

    if args.apply_same_filename and not args.same_filename_cleanup:
        parser.error("--apply-same-filename only applies to --same-filename-cleanup")

    if args.same_filename_cleanup and args.stats:
        parser.error("--same-filename-cleanup cannot be combined with --stats")

    if args.same_filename_cleanup and args.apply:
        parser.error(
            "--same-filename-cleanup cannot be combined with --apply (use --apply-same-filename)"
        )

    if args.same_filename_cleanup:
        apply_same_filename_dedup(
            palace_path=path,
            dry_run=not args.apply_same_filename,
            wing=args.wing,
            source_pattern=args.source,
            backup_dir=backup_dir,
            backup_max_age_days=args.backup_max_age_days,
            progress=args.progress,
            max_sets=args.max_sets,
        )
    elif args.stats:
        show_stats(
            palace_path=path,
            wing=args.wing,
            source_pattern=args.source,
            exact_duplicates=args.exact_duplicates,
            cross_source_duplicates=args.cross_source_duplicates,
            progress=args.progress,
            max_sets=args.max_sets,
        )
    else:
        # Safety default (2026-07-05 incident, iMelki/mempalace#19): a bare
        # invocation used to run LIVE deletions; 42,606 drawers were deleted
        # with no fresh backup. Deletion is irreversible, so it now requires
        # an explicit --apply.
        dedup_palace(
            palace_path=path,
            threshold=args.threshold,
            dry_run=not args.apply,
            source_pattern=args.source,
            wing=args.wing,
            progress=args.progress,
        )
