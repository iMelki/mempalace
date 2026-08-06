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

Usage (from CLI):
    mempalace dedup [--apply] [--threshold 0.15] [--stats]

Safety: bare invocation is a dry-run preview. Live deletion requires an
explicit --apply, a palace backup from the last 2 days, and (for bulk runs)
an operator-approved window — see iMelki/mempalace#19.
"""

import argparse
import os
import time
from collections import defaultdict

from .backends.chroma import ChromaBackend


COLLECTION_NAME = "mempalace_drawers"
# Cosine DISTANCE threshold (not similarity). Lower = stricter.
# 0.15 = ~85% cosine similarity — catches near-identical chunks.
# For looser dedup of paraphrased content, try 0.3–0.4.
DEFAULT_THRESHOLD = 0.15
MIN_DRAWERS_TO_CHECK = 5


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
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
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


def show_stats(
    palace_path=None,
    wing=None,
    source_pattern=None,
    min_count=MIN_DRAWERS_TO_CHECK,
    exact_duplicates=False,
    progress=False,
):
    """Show duplication statistics without making changes."""
    palace_path = palace_path or _get_palace_path()
    col = ChromaBackend().get_collection(palace_path, COLLECTION_NAME)

    # Scope must be both applied AND printed. Previously this function took no
    # wing/source arguments at all, so `--wing X --stats` silently scanned the
    # whole palace and reported cross-wing sources as if they were in scope
    # (iMelki/mempalace#33).
    print(f"\n  Palace: {palace_path}")
    print(
        f"  Scope:  wing={wing or 'ALL'}  source={source_pattern or 'ALL'}  min_count={min_count}"
    )

    groups = get_source_groups(col, min_count=min_count, source_pattern=source_pattern, wing=wing)

    total_drawers = sum(len(ids) for ids in groups.values())
    print(f"\n  Sources with {min_count}+ drawers: {len(groups)}")
    print(f"  Total drawers in those sources: {total_drawers:,}")

    print("\n  Top 15 by drawer count:")
    sorted_groups = sorted(groups.items(), key=lambda x: len(x[1]), reverse=True)
    for src, ids in sorted_groups[:15]:
        print(f"    {len(ids):5d}  {src[:65]}")

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
    else:
        print("\n  Duplicate counts not computed (metadata-only pass).")
        print("  Re-run with --exact-duplicates for a real byte-identical count,")
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

    if args.stats:
        show_stats(
            palace_path=path,
            wing=args.wing,
            source_pattern=args.source,
            exact_duplicates=args.exact_duplicates,
            progress=args.progress,
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
