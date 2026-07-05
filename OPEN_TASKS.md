# MemPalace Open Tasks

Last updated: 2026-07-05

This file is the durable local index for active `mempalace` issues.

## Active Issues

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
