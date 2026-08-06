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

Usage (from CLI):
    mempalace dedup [--apply] [--threshold 0.15] [--stats]

Safety: bare invocation is a dry-run preview. Live deletion requires an
explicit --apply, a palace backup from the last 2 days, and (for bulk runs)
an operator-approved window — see iMelki/mempalace#19.
"""

import argparse
import os
import sys
import time
from collections import Counter, defaultdict

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


def _get_palace_path():
    """Resolve palace path from config."""
    try:
        from .config import MempalaceConfig

        return MempalaceConfig().palace_path
    except Exception:
        return os.path.join(os.path.expanduser("~"), ".mempalace", "palace")


def get_source_groups(col, min_count=MIN_DRAWERS_TO_CHECK, source_pattern=None, wing=None):
    """Group drawers by source_file, return groups with min_count+ entries.

    If wing is specified, only considers drawers in that wing. This catches
    cross-wing duplicates when the same source was mined into multiple wings.
    """
    total = col.count()
    groups = defaultdict(list)

    offset = 0
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

    return {src: ids for src, ids in groups.items() if len(ids) >= min_count}


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


def dedup_source_group(col, drawer_ids, threshold=DEFAULT_THRESHOLD, dry_run=True):
    """Dedup drawers within one source_file group.

    Greedy: sort by doc length (longest first), keep if not too similar
    to any already-kept drawer. Returns (kept_ids, deleted_ids).
    """
    data = col.get(ids=drawer_ids, include=["documents", "metadatas"])
    items = list(zip(data["ids"], data["documents"], data["metadatas"]))
    items.sort(key=lambda x: len(x[1] or ""), reverse=True)

    kept = []
    to_delete = []

    for did, doc, meta in items:
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
            if progress and total:
                print(f"\r  exact-duplicate scan: {scanned * 100.0 / total:5.1f}%", end="")
        for count in seen.values():
            if count > 1:
                dup_groups += 1
                redundant += count - 1

    if progress:
        print()
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
            if progress and total:
                print(f"\r  cross-source duplicate scan: {scanned * 100.0 / total:5.1f}%", end="")

    if progress:
        print()

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
    """Show duplication statistics without making changes."""
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
            col, min_count=1, source_pattern=source_pattern, wing=wing
        )
        groups = {src: ids for src, ids in scoped_groups.items() if len(ids) >= min_count}
    else:
        groups = get_source_groups(
            col, min_count=min_count, source_pattern=source_pattern, wing=wing
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
    if exact_duplicates:
        dup_groups, redundant = count_exact_duplicates(col, groups, progress=progress)
        print(f"\n  Exact-duplicate sets (byte-identical documents): {dup_groups:,}")
        print(f"  Redundant drawers in those sets:                 {redundant:,}")
        print("  NOTE: exact matches only. Near-duplicates need an embedding pass.")
        print("  NOTE: grouped WITHIN each source_file. Content mined from several")
        print("        on-disk copies of one project is invisible here — use")
        print("        --cross-source-duplicates for that (iMelki/mempalace#19).")

    if cross_source_duplicates:
        result = count_cross_source_duplicates(
            col, scoped_groups, progress=progress, max_sets=max_sets
        )
        print_cross_source_report(result)

    if not exact_duplicates and not cross_source_duplicates:
        print("\n  Duplicate counts not computed (metadata-only pass).")
        print("  Re-run with --exact-duplicates for a real byte-identical count,")
        print("  --cross-source-duplicates to group identical text across source paths,")
        print("  or run without --stats for the embedding-distance dry-run preview.")


def dedup_palace(
    palace_path=None,
    threshold=DEFAULT_THRESHOLD,
    dry_run=True,
    source_pattern=None,
    min_count=MIN_DRAWERS_TO_CHECK,
    wing=None,
):
    """Main entry point: deduplicate near-identical drawers across the palace."""
    palace_path = palace_path or _get_palace_path()

    print(f"\n{'=' * 55}")
    print("  MemPalace Deduplicator")
    print(f"{'=' * 55}")

    col = ChromaBackend().get_collection(palace_path, COLLECTION_NAME)

    print(f"  Palace: {palace_path}")
    print(f"  Drawers: {col.count():,}")
    print(f"  Threshold: {threshold}")
    print(f"  Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    print(f"{'─' * 55}")

    if wing:
        print(f"  Wing: {wing}")
    groups = get_source_groups(col, min_count, source_pattern, wing=wing)
    print(f"\n  Sources to check: {len(groups)}")

    t0 = time.time()
    total_kept = 0
    total_deleted = 0

    sorted_groups = sorted(groups.items(), key=lambda x: len(x[1]), reverse=True)

    for i, (src, drawer_ids) in enumerate(sorted_groups):
        # Throttling between source files to keep CPU load low
        if i % 5 == 0:
            cpu = get_cpu_usage()
            if cpu and cpu > 70:
                time.sleep(2.0)
            else:
                time.sleep(0.1)

        kept, deleted = dedup_source_group(col, drawer_ids, threshold, dry_run)
        total_kept += len(kept)
        total_deleted += len(deleted)

        if deleted:
            print(
                f"  [{i + 1:3d}/{len(groups)}] "
                f"{src[:50]:50s} {len(drawer_ids):4d} → {len(kept):4d}  "
                f"(-{len(deleted)})"
            )

    elapsed = time.time() - t0

    print(f"\n{'─' * 55}")
    print(f"  Done in {elapsed:.1f}s")
    print(
        f"  Drawers: {total_kept + total_deleted:,} → {total_kept:,}  (-{total_deleted:,} removed)"
    )
    print(f"  Palace after: {col.count():,} drawers")

    if dry_run:
        print("\n  [DRY RUN] No changes written. Re-run with --apply to delete.")

    if not dry_run and total_deleted:
        # Post-mutation warm (iMelki/mempalace#19): the first palace open
        # after bulk deletions can do heavy one-time work (measured 1,004s
        # after a 42,606-drawer dedup). Pay that cost here, at mutation time,
        # instead of ambushing the next bridge start or agent query.
        print("\n  Pre-warming palace after deletions...")
        t_warm = time.time()
        try:
            from .searcher import search_memories

            search_memories("warmup", palace_path, n_results=1)
            print(f"  Palace warm in {time.time() - t_warm:.1f}s")
        except Exception as e:
            print(f"  Warm warning (non-fatal): {e}")

    print(f"{'=' * 55}\n")


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
        help="Print incremental progress during long scans",
    )
    args = parser.parse_args()

    path = os.path.expanduser(args.palace) if args.palace else None

    if args.apply and args.dry_run:
        parser.error("--apply and --dry-run are mutually exclusive")

    if args.exact_duplicates and not args.stats:
        parser.error("--exact-duplicates only applies to --stats")

    # Cross-source is a read-only measurement mode. Refusing it outside --stats
    # keeps it structurally unable to sit on the deletion path: dedup_palace()
    # never sees the flag (iMelki/mempalace#19).
    if args.cross_source_duplicates and not args.stats:
        parser.error("--cross-source-duplicates only applies to --stats")

    if args.stats:
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
        )
