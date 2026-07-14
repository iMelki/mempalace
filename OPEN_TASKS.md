# MemPalace Open Tasks

Last updated: 2026-07-14

This file is the durable local index for active `mempalace` issues.

## Active Issues

- [#22 - Add durable source-to-drawer receipts and supervised historical cohort recovery](https://github.com/iMelki/mempalace/issues/22)
  - Status: the affected `279`-test receipt/transport/backend suite, final
    repository-wide `1,673`-test release gate, and independent receipt re-review
    are green. The managed-write foundation is committed on `dev` in `04f5bf3`.
    Historical recovery remains NO-GO; no historical mining or recovery is
    authorized.
  - Fresh read-only audit reconciled 25,448 of 29,449 retained staged source
    paths exactly. The remaining 4,001 comprise 1,435 bootstrap-only records,
    2,548 format exclusions, and 18 intact current-rule candidates.
  - The 18 sources project to 22,220 drawer rows, but none of those projected
    IDs appears in the current store or retained July snapshots. All 18 exceed
    the former 10 MiB scan cap, making `probable-never-receipted-current-rule-output`
    the supported classification. They are not proven deleted or corrupted.
  - Implemented locally: versioned terminal source-write receipts, exact
    read-only verification, lock-before-read canonical source handling,
    non-destructive normalization failure, create-only journal publication,
    key-continuity checks, index-authoritative predecessor reconciliation,
    fail-closed OS-durable pre-purge recovery with exact restart restoration,
    post-publication row-set equality and exact-ID purge,
    embedding-preserving rollback snapshots, palace-wide managed-adapter
    and MCP mutation serialization, exact existing-row rechecks including
    embeddings, managed drawer/closet receipt writes, a non-mutating verifier
    lookup with mandatory durable COMPLETE markers, dual HMAC/source-file
    ownership, bounded fail-closed HNSW probes, and a pseudonymized shared
    projection using per-palace HMAC identities plus bucketed source size.
  - Local proof: the affected receipt/backend/HTTP/dispatch/server run passes
    `279` tests with one platform skip. Real-Chroma write readback is exact and
    bounded; stale or temporarily missing rows are retried for at most two
    seconds, and file mtimes are normalized to Chroma's six fractional digits
    before comparison. Exact vectors are read only through Chroma's supported
    collection API. If the Rust metadata/vector view does not converge inside
    that window, the managed write fails closed and restores its durable
    predecessor snapshot; MemPalace no longer opens Chroma's live SQLite/WAL as
    an in-process fallback.
  - Explicit remaining scope: receipts are not automatic across the whole
    product. Migration, repair, dedup, sweep, compression, diary/closet/KG/
    tunnel writes, MCP drawer mutations, backend open-time repairs, direct
    collection APIs, and adapters that bypass `managed_adapter_ingest()` remain
    unmanaged. Legacy `mempalace migrate` now blocks if receipt state exists so
    it cannot silently discard the journal; adapting or retiring the remaining
    paths stays in #22.
  - Next: disposable real-Chroma interruption/restart proof. The separate
    small-collection durability check is green: three explicit vectors below
    MemPalace's `50,000` Chroma sync
    threshold survived final-client close/reopen and matched within `1e-6`
    float32 tolerance; the corrected disposable run completed in `1.68s` and is
    now a normal real-Chroma regression. Historical recovery still stays NO-GO
    until an interrupted managed rewrite is restored and reverified after a
    true client/process restart. Explicit backend shutdown now calls Chroma's
    public `close()`. Automatic cache replacement cannot do that safely until
    collection-handle lifetimes are tracked; closing a replaced client while a
    caller still held its collection reproduced `RustBindingsAPI` without
    `bindings` in the full suite, so handle-aware retirement remains in #22.
    Windows DACL enforcement/readback remains explicitly unproven follow-up;
    POSIX mode requests are not evidence of an NTFS access-control boundary.
    Any historical recovery still needs fresh backup/restore proof, a bounded
    reviewed plan, an exact expected-output manifest, and separate operator
    approval.

- [#13 - mempalace_mcp_wrapper unconditionally disables real vector search (permanent keyword-only fallback since 2026-04)](https://github.com/iMelki/mempalace/issues/13)
  - Retitled 2026-07-04. The original count-divergence this issue was filed
    for is fixed (see #12, closed): post-replay `repair-status` reports
    drawers `sqlite=861,715`, `hnsw=850,000`, divergence `11,715`,
    `status=OK (within flush-lag tolerance)`.
  - The remaining, bigger problem: live `mempalace_search` still returns
    `"_search_mode": "text_match"` with note `"Keyword matching (HNSW index
    unreadable after Rust migration)"`. Root cause is
    `agent-settings/shared/tools/mempalace_mcp_wrapper.py` (2026-04-20
    chromadb crash workaround, see
    `agent-settings/shared/memory/mempalace-mcp-crash-report-2026-04-20.md`):
    Patch 4 unconditionally replaces `search_memories()` with keyword-only
    `get(where_document)` regardless of current HNSW readability, and Patch 3
    skips HNSW init entirely. Semantic/vector search has therefore been
    silently keyword-only since April 2026, independent of the July replay.
  - Next: verify on an ISOLATED palace copy (never live — the original
    failure was a 0xC0000005 segfault) whether current chromadb/mempalace
    pins can read the rebuilt HNSW files; then relax wrapper patches behind a
    feature flag, or make the keyword fallback a visible CoreHealth warning
    instead of a silent note.

- [#19 - Audit likely duplicate drawers surfaced by #13 probe](https://github.com/iMelki/mempalace/issues/19)
  - Status: open go/no-go decision. This is a duplicate-drawer audit and
    dedupe safety lane, not a live deletion approval.
  - Evidence: `python -m mempalace.dedup --stats` completed read-only on
    2026-07-07 against the current 825,422-drawer palace. It found 10,945
    sources with 5+ drawers, 791,410 drawers in those source groups, and a
    heuristic remaining duplicate estimate of about 292,998 drawers.
  - Interpretation: the 292,998 number is a coarse estimate (`40%` of drawers
    in source groups larger than 20), not a reviewed deletion list. Top
    offenders are mostly bulk repo digests and export staging trees where
    near-identical chunks can be legitimate.
  - Safety state: bare `python -m mempalace.dedup` is now dry-run by default;
    live mutation requires `--apply`. `dedup --apply` auto-runs
    `mempalace warm` after deletions so the post-mutation cold-open cost is
    paid during the approved mutation window.
  - Decision needed: approve or reject a supervised dedup pass. Recommended
    shape if approved is source-scoped top-10 dry-run first, then `--apply`
    per source only with fresh backup, artifact logging, and operator sign-off.

- [#16 - SQLite replay lacks bounded window, checkpoint, and structured repair artifacts](https://github.com/iMelki/mempalace/issues/16)
  - Goal: add operator-grade controls before replaying the full 851,964-row
    drawers segment: max-row/max-batch limits, durable logs/JSON, checkpoint or
    explicit no-resume safety, and rollback docs.
  - Status: bug report filed from the #12/#13 preflight. Current
    `--batch-size` controls only the per-upsert chunk size and is not a bounded
    replay window.
  - 2026-07-03 update: CLI/changelog support now includes pre-mutation
    `--max-rows` / `--max-batches` gates, `result.json` + `events.jsonl`
    artifacts, explicit `resume_supported=false`, and a forced immutable source
    snapshot even when callers pass `--no-backup`. These are abort gates and
    observability artifacts, not resumable partial replay controls.

- [#15 - Local test bootstrap depends on fragile PATH and PyPI TLS state](https://github.com/iMelki/mempalace/issues/15)
  - Goal: make focused MemPalace tests runnable without guessing which Python
    is on `PATH` or relying on a fresh PyPI download during repair work.
  - Status: bug report filed after `python -m pytest` resolved to a lean Hermes
    venv without pytest and `uv run` failed fetching `idna==3.11` with
    `UnknownIssuer`; focused tests passed through the globally installed
    Python 3.13 `pytest.exe`.

- [#17 - Markdown link hook has confusing manual-scope failures](https://github.com/iMelki/mempalace/issues/17)
  - Goal: make manual focused pre-commit verification match operator intent and
    keep full-repo Markdown link validation either clean or explicitly
    baselined.
  - Status: bug report filed after `pre-commit run --files ...` failed by
    scanning unrelated tracked Markdown when nothing was staged. The normal
    staged hook passed for the SQLite replay commit, so this is a verification
    ergonomics/docs-baseline issue rather than a blocker for #16.

- [#5 - Use relevant skills for market research, competitor analysis, and monetization planning](https://github.com/iMelki/mempalace/issues/5)
  - Goal: Use the relevant shared skills to map competitors, ICPs, monetization options, and positioning for the user-owned MemPalace fork.

- [#6 - Design and build a landing page](https://github.com/iMelki/mempalace/issues/6)
  - Goal: Define and implement a landing page for the user-owned MemPalace fork with clear audience, value proposition, proof, and CTA.

- [#11 - Validate and extend MemPalace website SEO/GEO baseline](https://github.com/iMelki/mempalace/issues/11)
  - Goal: Validate the generated VitePress output for robots/sitemap/canonical/JSON-LD coverage after website dependencies are restored, then decide whether richer answer-first content work belongs in a separate pass.

- [#3 - Review and split preserved search and MCP runtime WIP](https://github.com/iMelki/mempalace/issues/3)
  - Goal: Review the preserved runtime branch, address Copilot findings, add targeted tests, and split into focused PRs.
  - Status: Open (preserved branch `agent/codex/mempalace-search-mcp-wip`).

## Recently Completed

- [#21 - Implement native loopback Streamable HTTP MCP transport](https://github.com/iMelki/mempalace/issues/21)
  - 2026-07-14: completed native authenticated Streamable HTTP on
    `127.0.0.1:8787/mcp`, shared stdio/HTTP dispatch, strict Host/Origin/auth
    handling, active-aware bounded sessions, and serial-by-default backend
    calls in commit `04f5bf3`. The full release gate passed `1,673` tests with
    `7` skips and `106` intentional deselections. Agent-settings selected the
    native path, retained supergateway only as rollback, and proved an exact
    attended restart. The four-client live gate passed; sustained artifact
    `mempalace-attended-native-sustained-burnin-20260714T060927Z.json` then
    passed six four-client waves over `132.39s` with `24/24` real read-only
    calls, `24/24` cleanup, zero lingering workers, and one stable bridge
    identity. Fresh transport decision
    `mempalace-bridge-transport-readiness-20260714T061355Z` is
    `native-transport-ready`. Railway and hosted deployment stayed out of
    scope.

- [#18 - repair-status lacks machine-readable read-only parity artifacts](https://github.com/iMelki/mempalace/issues/18)
  - 2026-07-05: implemented `mempalace repair-status --json` (single JSON
    object to stdout: schema `mempalace.repair-status.v1`, palace path, UTC
    timestamp, per-collection `sqlite_count`/`hnsw_count`/`divergence`/
    `status`/`note`) plus optional `--artifact-dir` writing the same JSON to
    a timestamped `repair-status-<UTC>.json` file with no repair-run
    directory. Default human output verified byte-identical against the
    dd6a158 baseline on the live palace; live `--json` readback reported
    drawers `sqlite=868,028`, `hnsw=850,000`, divergence `18,028`, `OK` and
    closets `12,107`/`11,826`/`281`, `OK`. Tests cover drawers-diverged,
    closets-within-tolerance, missing palace, artifact writing, human-path
    preservation, lean-runtime (blocked `chromadb` import), and CLI wiring.

- [#12 - Rebuild quarantined drawers HNSW segment after local crash repair](https://github.com/iMelki/mempalace/issues/12)
  - 2026-07-04: closed with proof. The non-dry
    `repair --mode sqlite-replay` run completed 2026-07-03T18:13:13Z:
    `status=completed`, `replayed=856,510 == planned_reembed_count`,
    `verified_count=856,510`, `warnings=[]`, duration `18,621.7s` (~5h10m).
    Result artifact:
    `C:\Users\Milky\.mempalace\repair-runs\sqlite-replay-final-20260703T130250Z\result.json`.
    Follow-up `repair-status` (2026-07-04): drawers `sqlite=861,715`,
    `hnsw=850,000`, divergence `11,715`, `status=OK`; closets
    `sqlite=12,107`, `hnsw=11,826`, divergence `281`, `status=OK`. Divergence
    fell from `818,039` to `11,715` (98.6% convergence); the remainder is
    ordinary flush lag. The stale post-replay maintenance marker was archived
    to `maintenance-audits\archived-markers\` and cleared, and router/bridge
    were formally restored (see agent-settings#201/#209 for the follow-up
    launcher/bridge reliability bugs found in the same pass).

- [#14 - mempalace status imports Chroma before SQLite fallback in lean runtimes](https://github.com/iMelki/mempalace/issues/14)
  - 2026-07-03: split CLI status into dependency-light `mempalace.status`,
    added regression tests, and live-verified `python -m mempalace.cli status`
    prints the 820,220-drawer SQLite status without importing `chromadb`.

- [#10 - Install git-toolkit secrets filter and pre-commit hooks from monthly health](https://github.com/iMelki/mempalace/issues/10)
  - 2026-06-23: repaired the local git-toolkit hook cache path by reinstalling
    the secrets filter and commit hooks, added the baseline deep-scan ignore
    file, and verified the repo-health audit reports `grade=OK`, `warn=0`,
    `fail=0`.
- Markdown link validation baseline added on 2026-05-12.
  - Added `scripts/check-markdown-links.ps1`, wired it into `.pre-commit-config.yaml`, and documented the docs check in `CONTRIBUTING.md`.
- [#1 - Adopt projects-ops repo bootstrap governance baseline](https://github.com/iMelki/mempalace/issues/1)
  - Completed via [PR #2](https://github.com/iMelki/mempalace/pull/2).

## Supporting Docs

- [AGENTS.md](AGENTS.md)
- [CONTRIBUTING.md](CONTRIBUTING.md)
- [.github/labels.yml](.github/labels.yml)
