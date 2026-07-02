# MemPalace Open Tasks

Last updated: 2026-07-02

This file is the durable local index for active `mempalace` issues.

## Active Issues

- [#13 - Chroma HNSW segment diverged from SQLite drawer metadata](https://github.com/iMelki/mempalace/issues/13)
  - Goal: make the July 2026 Chroma SQLite/HNSW divergence impossible to miss,
    keep vector fallback mode explicit in MemSys, and complete a supervised
    replay/rebuild only under an approved maintenance window.
  - Status: Bug report filed after live evidence showed drawers
    `sqlite=820,220` and `hnsw=3,168`. The repair CLI now detects divergence,
    supports SQLite-only dry-run, and refuses large replay without
    `--confirm-large-reembed`; full vector replay remains tracked through #12.

- [#12 - Rebuild quarantined drawers HNSW segment after local crash repair](https://github.com/iMelki/mempalace/issues/12)
  - Goal: Rebuild or replay the quarantined drawers vector segment from the
    2026-07-02 local repair, then verify `mempalace status`,
    `repair-status`, and representative search behavior before removing any
    preserved segment directories.
  - 2026-07-02 update: added `repair --mode sqlite-replay` with SQLite-only
    dry-run, snapshot restore, progress, and a large re-embed confirmation
    guard. Live dry-run validated 820,220 SQLite drawer rows. A full replay was
    intentionally stopped after the first 1,000-row batch because rebuilding
    all vectors would be a long maintenance job; the original SQLite database
    was restored, the partial collection was removed, and BM25 fallback remains
    the safe search path until an explicit `--confirm-large-reembed` window is
    scheduled.

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
