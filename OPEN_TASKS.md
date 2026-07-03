# MemPalace Open Tasks

Last updated: 2026-07-03

This file is the durable local index for active `mempalace` issues.

## Active Issues

- [#13 - Chroma HNSW segment diverged from SQLite drawer metadata](https://github.com/iMelki/mempalace/issues/13)
  - Goal: make the July 2026 Chroma SQLite/HNSW divergence impossible to miss,
    keep vector fallback mode explicit in MemSys, and complete a supervised
    replay/rebuild only under an approved maintenance window.
  - Status: Bug report filed after live evidence showed drawers
    `sqlite=851,964` and `hnsw=33,982`. The repair CLI now detects divergence,
    supports SQLite-only dry-run, and refuses large replay without
    `--confirm-large-reembed`; full vector replay remains tracked through #12.
  - 2026-07-03 final provider-chat drain update: current `repair-status`
    after the final Codex batches reports drawers `sqlite=856,510`,
    `hnsw=38,471`, divergence `818,039`; closets remain within tolerance at
    `sqlite=12,107`, `hnsw=11,826`, divergence `281`. This is still
    `DIVERGED` for drawers and still requires the supervised #12 replay.
  - 2026-07-03 update: read-only proof after the latest Codex provider-chat
    drain window shows drawers `sqlite=851,964`, `hnsw=33,982`, divergence
    `817,982`; closets remain within tolerance (`sqlite=12,107`, `hnsw=11,826`,
    divergence `281`). Bridge logs still report
    `vector_disabled=true`, so BM25 fallback remains the safe path until the
    HNSW replay completes.

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
  - 2026-07-03 update: current read-only proof reports drawers
    `sqlite=851,964`, `hnsw=33,982`, divergence `817,982`. Artifacted dry-run
    at
    `S:\source\CCAI\Assistants\tools\Memory\mempalace\.codex\artifacts\hnsw-sqlite-proof-20260703-111445\sqlite-replay-dry-run\result.json`
    planned `832,966` replay rows in `833` batches, replayed `0`, and left the
    live collection unchanged; it now predates both the final Claude drain and
    the latest Codex provider-chat drain windows, so a fresh pre-replay dry-run
    should be captured before non-dry replay.
    Fresh palace backup
    `C:\Users\Milky\.mempalace\backups\palace-2026-07-03-1126-pre-hnsw-sqlite-replay.tar.gz`
    is verified tar-readable (`14,561.7 MB` compressed in `1,016.7s`; inventory
    now has `11` palace archives and `0` zero-size palace archives). Schedule
    the non-dry replay only inside an approved quiet maintenance window.
  - 2026-07-03 fresh dry-run update: after the final Codex provider-chat drain,
    `python -m mempalace.cli repair --mode sqlite-replay --dry-run
    --batch-size 1000` planned `856,510` rows in `857` batches, replayed `0`,
    and left the live collection unchanged. Artifact:
    `C:\Users\Milky\.mempalace\palace\.mempalace\repair-runs\sqlite-replay-20260703T121156644960Z\result.json`.
    The existing backup predates the final `+4,546` drawer growth, so a fresh
    palace backup is required before any non-dry replay.

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

- [#18 - repair-status lacks machine-readable read-only parity artifacts](https://github.com/iMelki/mempalace/issues/18)
  - Goal: let agents and incident bundles capture HNSW/SQLite parity counts
    without scraping human text or launching a replay dry-run.
  - Status: bug report filed from the 2026-07-03 sidecar preflight. Current
    workaround is to record `repair-status` text output plus existing replay
    dry-run artifacts; desired fix is `repair-status --json` and optional
    read-only artifact output.
  - 2026-07-03 update: fresh proof still confirms the gap. `repair-status`
    reports drawers `sqlite=851,964`, `hnsw=33,982`, divergence `817,982`, but
    `repair-status --help` exposes no JSON or artifact option.
  - 2026-07-03 final drain update: fresh `repair-status` reports drawers
    `sqlite=856,510`, `hnsw=38,471`, divergence `818,039`, and
    `repair-status --help` still exposes only human-readable help/no JSON
    artifact output.

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
