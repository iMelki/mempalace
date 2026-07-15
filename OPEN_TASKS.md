# MemPalace Open Tasks

Last updated: 2026-07-15

This file is the durable local index for active `mempalace` issues.

## Active Issues

- [#22 - Add durable source-to-drawer receipts and supervised historical cohort recovery](https://github.com/iMelki/mempalace/issues/22)
  - Status: the managed MCP writer proof passes `270` tests with one platform
    skip, the MCP-server proof passes `91` tests with one skip, and the protected
    repository gate passes `1,695` tests with `7` skipped and `106` intentionally
    deselected. The MCP managed-write tranche is committed and pushed on
    `dev` in `3cd8d10`; GitHub #22 remains open and `In progress`. Historical
    recovery remains NO-GO; no historical mining or recovery is authorized.
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
  - MCP writer tranche completed on `dev`: drawer add/update/delete and MCP
    diary writes now delegate to one receipt-aware service. Drawer mutations
    require a stable logical `source_id`; replacement publishes a superseding
    receipt, deletion publishes a verified `ZERO_OUTPUT` successor plus
    predecessor invalidation, and failed replacement restores the exact old
    row and receipt. Document bytes and semantic metadata are attested together;
    tampered wing/room/agent/topic state cannot be reused as unchanged. Diary
    callers may supply an agent-and-wing-scoped `source_id` for retry
    idempotency. Read/status cache misses no longer create a collection or pin
    HNSW settings. Legacy rows without receipts remain readable but require an
    explicit provenance migration before mutation. Contract and community
    research are in
    `docs/research/mcp-managed-write-contract-2026-07-15.md`.
  - MCP writer focused proof: the receipt, HTTP, dispatch, source, and MCP
    server suites pass `270` tests with one platform skip. The durable log is
    under `.local-logs/pytest-mcp-managed-20260715-013828.out.log` and remains
    an untracked local artifact.
  - Diary-file writer tranche implemented on `dev`: each dated Markdown source
    now replaces one day drawer plus its complete current closets under one
    managed receipt and durable rollback snapshot. Exact source-byte hashes,
    the entity-registry/language configuration digest, same-size changes,
    semantic zero-output, and drawer/closet identities participate in reuse or
    supersession. The mutable state file is atomic convenience state rather
    than authority; malformed scalar entries are repaired after commit. Two
    files targeting one `(wing, date)` fail before palace mutation, and a
    conflicting source from another invocation fails the managed ownership
    check before overwrite. Undated-only Markdown is a no-op before palace
    creation. Exact snapshot/reuse reads also retry only Chroma's classified
    delayed-vector visibility for at most two seconds and otherwise fail
    closed. The focused diary proof passes `13` tests; contract
    and community research are in
    `docs/research/diary-file-managed-write-contract-2026-07-15.md`.
  - Diary-file final repository gate: `1,706` passed, `7` skipped, `106`
    intentionally deselected in `141.05s`. The expanded receipt/diary proof
    passes `207` tests in `59.03s`. Durable local evidence is under
    `.local-logs/pytest-diary-final-20260715T030401.out.log` and
    `.local-logs/diary-receipt-broad-final-20260715T030241.log`.
  - JSONL sweeper managed-writer tranche implemented locally on `dev`: one
    physical file now owns an isolated receipt source lane so message-level
    rows can coexist with primary-miner chunks. Exact file bytes, deterministic
    source-namespaced message IDs, and source-derived semantic metadata govern
    whole-source reuse or replacement. Invalid UTF-8 and malformed message
    input fail before mutation; removing every current row requires explicit
    `--allow-zero-output`. Legacy unmanaged sweeper rows fail before receipt
    storage is initialized. Renamed/copied sources use disjoint lanes instead
    of failing on foreign IDs, while old lanes remain visible pending reviewed
    cleanup. Exact terminal validation remains under the palace lock, and the
    CLI reports semantic updates, receipt rebindings, and physical mutations
    separately. Palace locking now precedes every Chroma/receipt side effect;
    empty legacy sources detect historical relative path spellings; mixed JSON
    content blocks are preserved. After `COMPLETE`, the generic driver reloads
    the durable receipt event and verifies exact represented/missing/excess/
    conflict/stale state before recovery cleanup. An injected verifier or
    finalization failure reports `committed-unverified` and exits nonzero
    instead of implying a rollback. Expected terminal-manifest rows and
    verifier-confirmed represented rows are separate, so unverified output is
    never counted as represented. Mixed directory runs expose partial per-file
    verifier evidence but zero the whole-run represented claim, and the CLI
    prints the single-file and directory distinctions. Injected second-batch
    failure and source mutation both restore
    the full exact predecessor lane with no replacement survivors. Focused
    proof passes `35` tests; the expanded sweeper/CLI/receipt proof passes `204`
    tests in `100.85s` with no stderr. Contract and upstream research are in
    `docs/research/sweeper-jsonl-managed-write-contract-2026-07-15.md`; durable
    expanded logs are under
    `.local-logs/sweeper-aggregate-expanded-final-20260715T045557.{out,err}.log`.
  - Final repository gate: `1,733` passed, `7` skipped, `106` intentionally
    deselected in `232.05s` with no stderr; durable output is under
    `.local-logs/pytest-sweeper-aggregate-full-final-20260715T045818.{out,err}.log`.
  - Local proof: the affected receipt/backend/HTTP/dispatch/server run passes
    `279` tests with one platform skip. Real-Chroma write readback is exact and
    bounded; stale or temporarily missing rows are retried for at most two
    seconds, and file mtimes are normalized to Chroma's six fractional digits
    before comparison. Exact vectors are read only through Chroma's supported
    collection API. If the Rust metadata/vector view does not converge inside
    that window, the managed write fails closed and restores its durable
    predecessor snapshot; MemPalace no longer opens Chroma's live SQLite/WAL as
    an in-process fallback.
  - Large managed-row correction: the 2026-07-14 provider-chat failure was not
    evidence of palace corruption. Chroma `1.5.7` compiled the escaped full
    document supplied as `where_document` into a Rust regex and returned SQLite
    extended constraint code `1043` once the regex exceeded the compiler size
    limit. The same `393,216`-byte fixture still fails on Chroma `1.5.9`.
    Receipt-stamped rows whose stored SHA-256 matches the fetched document now
    delete by exact ID plus source/receipt/content-hash metadata and do not send
    that redundant document regex. Legacy or stale-hash rows retain the exact
    regex; an empty stale-hash row fails closed. A disposable real-Chroma test
    deletes exactly one large row, `tests/test_write_receipts.py` passes `110`
    tests, Ruff is clean, and no live palace was opened by this proof.
  - Explicit remaining scope: receipts are not automatic across the whole
    product. Migration, repair, dedup, compression, closet regeneration,
    KG/tunnel writes, backend open-time repairs, direct collection APIs, and
    adapters that bypass `managed_adapter_ingest()` remain unmanaged. The MCP
    drawer, MCP diary, diary-file, and JSONL sweeper paths are no longer in this
    list: four of the ten managed-receipt adaptations are now met on `dev`, with
    six still open. Pre-receipt sweeper rows still require an explicit
    provenance migration; this tranche does not fabricate historical receipts.
    Legacy `mempalace migrate` blocks if receipt state exists so it cannot
    silently discard the journal; adapting or retiring the remaining paths
    stays in #22.
  - Writer disposition is now explicit and machine-readable in
    `docs/research/managed-write-boundary-dispositions-2026-07-14.json`: 21
    mutation surfaces are accounted for. Ten source/derived-output paths must
    adapt to managed receipts, six unmanaged mutation surfaces must retire in
    favor of receipt-aware or read-only replacements, and five physical,
    topology, graph, configuration, or operational stores are excluded from
    source-drawer completeness only under named separate contracts. These are
    completed decisions, not completed implementations.
  - Process-restart gate: green on a disposable synthetic Chroma `1.5.9`
    database. Four strictly sequential child processes seeded one exact
    receipt, published recovery and hard-exited with the expected code `73`
    after a partial rewrite, restored the predecessor in a fresh process, and
    reopened it in a second fresh process. The final vector query returned the
    baseline ID, SQLite integrity was `ok`, no recovery manifest or partial row
    remained, and disposable cleanup succeeded. The operator run completed in
    `7.3s`; evidence is under
    `%LOCALAPPDATA%\MemSys\eval-artifacts\mempalace-write-receipt-restart\20260714T143903Z-2b73fba1`.
    This proves the managed process-death recovery path, not arbitrary power-
    loss or faulty-storage durability.
  - Remaining: implement the decided writer manifest. The separately
    reviewed 18-source plan is also published in
    `docs/research/historical-cohort-recovery-plan-2026-07-14.{md,json}` and is
    intentionally `NO-GO`: all eight gates remain pending, including equivalent-
    content review, a clean full-directory backup, native disposable restore,
    clone canary, and explicit named live approval. The plan permits one source
    per attended run and never automatic advance. The bounded live provider-
    chat canary also waits for genuine MemSys host-pressure admission. Explicit
    backend shutdown calls Chroma's public `close()`, but automatic cache
    replacement still needs handle-aware retirement because eager close
    invalidated live collection handles in the full suite. Windows DACL
    enforcement/readback remains unproven. Diary files that disappear or are
    renamed are not auto-pruned: absence remains visible as stale convenience
    state until #22 defines an explicit, reviewable deletion policy.

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

- [#13 - Restore real vector search in the MemPalace wrapper](https://github.com/iMelki/mempalace/issues/13)
  - 2026-07-07: closed after isolated current-pin proof, a 201-query
    concurrent load test with zero errors or segfaults, live feature-flag
    rollout, and a clean soak with no vector faults. Vector search is now the
    default; `MEMPALACE_WRAPPER_VECTOR_SEARCH=0` is the explicit keyword-only
    fallback. Final live/CoreHealth evidence reported vector mode and stable
    HNSW divergence. Retained wrapper compatibility patches are follow-up
    cleanup, not a functional blocker.

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
