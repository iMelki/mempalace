# MemPalace Open Tasks

Last updated: 2026-08-09

This file is the durable local index for active `mempalace` issues.

## Active Issues

- [#31 - Bind immutable evaluation corpus identity for MemSys gold baselines](https://github.com/iMelki/mempalace/issues/31)
  - The HTTP consumer is shipped. The producer now has a durable three-phase
    snapshot / scan / finalize workflow: an integrity-checked SQLite online
    backup is retained first, a resumable private id/hash scan runs only over
    that immutable snapshot, and a public manifest remains unavailable until
    the complete shard chain validates. This replaces the prior all-in-memory
    inventory path that could lose a multi-gigabyte snapshot on timeout. The
    snapshot creator revision remains immutable provenance; the first scan
    separately pins its processor revision, which must also perform finalization
    so a post-snapshot evaluator update cannot silently change results.
  - The active managed-palace snapshot is a runtime artifact, not proof of a
    complete identity yet. It must finish, scan to EOF, finalize the public
    manifest and attestation, and then be bound on a separately attended bridge
    restart before a gold run becomes comparable.
  - Implemented the startup-only manifest validation and authenticated identity
    surface. A complete identity needs a separately attested logical-inventory
    manifest bound to the running data plane; a status count, arbitrary
    environment digest, dynamic database scan, or path is not accepted.
  - Remaining: complete the staged managed-palace run and register its exact
    manifest on an attended bridge restart before MemSys can publish a
    comparable complete-identity gold baseline. Until then
    `corpusGeneration=unavailable` remains the correct live state.


- [#30 - Stabilize the official MCP client loopback integration test](https://github.com/iMelki/mempalace/issues/30)
  - A second 2026-07-29 guarded run timed out at the five-second MCP
    initialization budget after `1758` passing tests. The prior run passed the
    same test, so this is an integration-test flake, not a live bridge outage.
  - Reproduce only with disposable loopback state, collect phase/duration
    evidence, and preserve the overall bounded scenario deadline before
    considering any narrow test-harness change. Production MCP timeouts are
    out of scope.

- [#28 - Return bounded degraded result when BM25 SQLite fallback is locked](https://github.com/iMelki/mempalace/issues/28)
  - Live bridge readiness on 2026-07-29 found a listener on `127.0.0.1:8787`
    but MemSys callers timed out because the read-only fallback raised
    `sqlite3.OperationalError: database is locked` during its metadata read.
  - The fallback now returns the same retryable structured degraded result for
    `locked`/`busy` failures while opening the read-only connection, selecting
    FTS/recency/id candidates, or reading metadata. The receipt exposes a
    bounded `retryAfterMs`, a stable `sqlite_locked` reason, and a safe phase
    diagnostic, rather than pretending that the palace has no candidates.
    This does not stop a process, remove a lock, change SQLite/WAL
    configuration, or mutate the palace.
  - Router explicit-source proof completed on 2026-07-29: a normal MemPalace
    read returned evidence in 21.1 seconds under the 30-second Router deadline.
    Remaining proof is a future naturally contended Router/MCP read; diagnose
    the actual writer only from owned process evidence before any lifecycle
    action.

- [#26 - Report conversation palace-lock contention as a temporary failure](https://github.com/iMelki/mempalace/issues/26)
  - The naturally scheduled MemSys provider-chat run
    `provider-chat-ingestion-20260729T001002Z` overlapped the weekly knowledge
    mine. Conversation mode swallowed `MineAlreadyRunning`, returned `0`, and
    forced the receipt consumer to infer a terminal reconciliation failure.
  - Implemented locally: the CLI requests lock-conflict propagation and returns
    the existing temporary-failure code `75`; direct library callers preserve
    their prior default. A live empty-source lock probe returned `75` and
    selected no source files. The CLI regression suite passes `59` tests.
  - The paired MemSys consumer change is tracked on
    [memsys#89](https://github.com/iMelki/memsys/issues/89) and classifies
    exit `75` as retryable without advancing source state.
  - Follow-up found a second independent recovery-path defect: conversation
    mining supplied only drawers to the palace-wide recovery reconciler while a
    prior filesystem rewrite required drawers and closets. Fixed on `dev` with
    optional `create=False` closet binding and regression coverage; one live
    bounded retry remains to prove the pending recovery is reconciled.

- [#25 - Expose deterministic mine manifests and resumable source progress](https://github.com/iMelki/mempalace/issues/25)
  - Implemented locally: normalized deterministic source ordering; immutable
    stat/content/parser/config/miner-bound manifests; exact `--start-index`;
    hash-chained and fsynced sanitized progress; target-palace receipt readback;
    torn-tail recovery; drift/committed-corruption/cross-palace rejection; and
    CLI temporary-lock exit `75`.
  - The follow-up planner contract adds `--plan-progress-jsonl`: directory
    discovery and every completed file descriptor are fsynced into a compact,
    hash-chained journal, so a killed source-tree walk resumes at the exact next
    directory/file rather than repeating the full scan/hash pass. Only a torn
    non-newline tail is repaired; complete corruption and identity drift fail
    closed. Research and contract:
    `docs/research/resumable-source-plan-contract-2026-07-29.md`.
    The post-fix repository gate passes `1,756` tests with `7` skipped and
    `106` intentionally deselected in `203.30s`.
  - Disposable focused proof covers hard exit/restart, lagging-prefix replay,
    output equality, idempotency, source drift, lock conflict, and no
    path/content leakage. The journal reuses an unchanged validated prefix so
    repeated appends are linear rather than quadratic (`14` focused tests
    passed). The expanded miner, lock, receipt, CLI, and progress regression
    passes `223` tests in `63.98s`; Ruff and local Markdown-link checks pass.
    No configured palace was opened.
  - Implementation and the linear-prefix follow-up are pushed on `dev`. This
    unblocks the exact-file continuation dependency for
    [agent-settings#486](https://github.com/iMelki/agent-settings/issues/486).

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
  - Receipt-optional core-miner bypass retired locally on `dev`: project and
    conversation helper writes now require a valid `ReceiptStore` and
    `ManagedRunIdentity` bound to the same receipt root. Missing, invalid, or
    foreign pairs fail before collection reads, purge, or upsert; dry runs stay
    receipt-free and the top-level CLI paths already supply the pair. The
    batched benchmark now creates a disposable managed run. Focused proof
    passes `5` tests in `1.31s` and retains `2,2,1` bounded real-Chroma writes.
    Final expanded receipt/miner/conversation/CLI proof passes `209` tests with
    `29` dependency warnings in `69.91s` and no stderr. Contract and community
    basis are in
    `docs/research/core-miner-receipt-required-contract-2026-07-15.md`.
    The final repository gate passes `1,735` tests, with `7` skipped and `106`
    intentionally deselected, in `211.25s`; its stderr log is empty. Durable
    output is under
    `.local-logs/pytest-receipt-required-full-final-20260715T054246.{out,err}.log`.
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
    `PalaceContext` paths that bypass `managed_adapter_ingest()` remain
    unmanaged. The MCP drawer, MCP diary, diary-file, and JSONL sweeper paths
    are no longer in this list: four of the ten managed-receipt adaptations are
    met on `dev`, with six still open. The receipt-optional canonical
    project/conversation helper bypass is also retired: one of six retirement
    dispositions is met, with five still open. Pre-receipt sweeper rows still
    require an explicit provenance migration; no tranche fabricates historical
    receipts. Legacy `mempalace migrate` blocks if receipt state exists so it
    cannot silently discard the journal; adapting or retiring the remaining
    paths stays in #22.
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

- [#36 - Root cause: mining ingests backup/variant copies and generated lockfiles](https://github.com/iMelki/mempalace/issues/36)
  - Mechanism shipped. `mempalace/mine_exclusions.py` turns the two previously
    hardcoded sets (`palace.SKIP_DIRS`, `miner.SKIP_FILENAMES`) into one
    configurable, documented policy seeded from the old sets, and extends the
    defaults with the dependency lockfiles of 12 ecosystems plus `obj/`, `bin/`,
    `out/`, `vendor/`, and 16 more build/cache trees. `.gitignore` respect is
    untouched — it never covered lockfiles, which is why they reached the palace.
  - Verified on a fixture tree carrying the issue's own shapes: the pre-change
    scan took 4 files (`pnpm-lock.yaml`, `obj/Debug/m.json`, plus 2 real
    sources); the post-change scan takes 2. Adding
    `exclude: {allow_files: [pnpm-lock.yaml]}` re-admits the lockfile on the
    next mine while the already-filed sources are correctly skipped, proving
    both reversibility and that existing receipts survive a policy change.
  - Backup/variant directories are reported, never auto-excluded
    (`mempalace variants DIR [--json]`, plus a header advisory during
    `mempalace mine`). A directory named `backup` can hold the only surviving
    copy of something, so no config switch makes exclusion automatic; content
    inside a flagged directory is still mined, and there is a regression test
    asserting exactly that.
  - **Open operator decision (not decided here): should lockfiles be excluded
    outright, or is there a recall case for dependency-resolution history?**
    The default is exclusion because a lockfile's recall value is close to zero,
    not because the recall case was ruled out. Reversal is one config line.
  - Remaining from the issue: item 3, the mine-time content-hash duplicate check
    against existing drawers (`mempalace_check_duplicate` exists as an MCP tool
    but the mining paths may not all route through it). Not implemented here.
  - Nothing already in the palace was deleted or modified. Cleanup of existing
    duplicates stays under #19 and remains gated on a real offsite backup.

- [#19 - Audit likely duplicate drawers surfaced by #13 probe](https://github.com/iMelki/mempalace/issues/19)
  - Status: open. Operator decision made (2026-08-07, same-filename-only
    scope); apply path built and tested; NOT applied to the live palace.
  - **Operator policy decision (2026-08-07):** any cleanup touches ONLY the
    8,145 same-filename cross-source sets (21,017 redundant drawers). The 256
    mixed-filename shared-boilerplate sets are PERMANENTLY out of scope for
    deletion.
  - **Apply path shipped (built, not run):** `plan_same_filename_deletions()` +
    `check_backup_freshness()` + `apply_same_filename_dedup()` in `dedup.py`,
    CLI `--same-filename-cleanup [--apply-same-filename]`. Dry-run is the
    default; live deletion additionally requires a code-enforced
    backup-freshness gate (new — `dedup_palace()`'s equivalent requirement was
    previously documentation-only). 45 new tests in `tests/test_dedup.py`.
  - **Not applied.** `check_backup_freshness()` against this workspace's real
    `~/.mempalace/backups` returns `ok=false` right now: all 6 local archives
    have `offsite.status: "pending"` (agent-settings#457, offsite backup, is
    still in progress). That refusal is proven in the test suite, not worked
    around. Operator command once the gate clears:
    `python -m mempalace.dedup --same-filename-cleanup --apply-same-filename --wing coding`
  - Retracted evidence: the old `--stats` "estimated duplicates" figure
    (292,998, later 367,462) was `count * 0.4` over large source groups with no
    content comparison at all. It measured nothing and must not be cited. Fixed
    and removed in #33 (`3788ae6`).
  - Real evidence, intra-source (`--exact-duplicates`, 2026-08-06): the
    correctly-scoped coding wing (986 sources / 39,461 drawers) holds 20
    byte-identical sets and 83 redundant drawers — 0.21%. Essentially nil,
    because per-`source_file` grouping cannot see the actual duplication.
  - Real evidence, cross-source (`--cross-source-duplicates`, 2026-08-06):
    `--wing coding` (41,934 drawers in 2,311 sources, 102s, exit 0) holds 8,416
    duplicate sets and 22,300 redundant drawers, 22,227 of them spanning 2+
    distinct source paths and only 73 confined to one path. Of the cross-path
    sets, 8,145 (21,017 redundant drawers) have paths that all share one
    filename — one file mined from several trees, the copied-directory
    signature; the remaining 256 sets are different files sharing a chunk.
    Narrowed to `--source Repeater_System` (24,587 drawers): 5,382 sets /
    18,746 redundant, 18,683 cross-path. Cause: five on-disk copies of one project
    (`repeater-system`, `-mobile-fixes`, `-all-ui-alpha-ver`, `_backup_250522`,
    plus a `files\CLOUD NMS installation package` tree) were each mined under a
    distinct `source_file`.
  - Interpretation: duplication is real but cross-source, an order of magnitude
    smaller than the retracted estimate, and concentrated in project copies and
    generated files (`pnpm-lock.yaml` across project variants). Prevention at
    mine time is tracked separately as #36.
  - Safety state: bare `python -m mempalace.dedup` is dry-run by default; live
    mutation requires `--apply`. `dedup --apply` auto-runs `mempalace warm`
    after deletions. The cross-source audit mode is read-only, rejected outside
    `--stats`, and reports sets without choosing a canonical copy.
  - Blocker for any `--apply`: no offsite backup exists. Local archives report
    `knownGood: 0` and offsite copy is fail-closed behind agent-settings#457
    (with agent-settings#538 for the unreachable palace compress/upload step).
  - Decision needed: approve or reject a supervised dedup pass, and separately
    decide the canonical-copy policy per duplicate set (the audit deliberately
    does not pick winners).

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

<!-- Cured 2026-08-06 via the workspace issue-state audit (projects-ops#101/#73): issue closed while listed active. -->
- [#29 - Prevent Windows WFP timeout warnings from leaking out of the CLI test](https://github.com/iMelki/mempalace/issues/29)
  - The 2026-07-29 guarded full test lane passed, but
    `tests/test_cli.py::test_cmd_init_no_entities` emitted Windows
    `FWP_E_TIMEOUT` (`0x80320012`) while opening an LLM-availability request.
    The output was warning-grade, not evidence of a retrieval failure.
  - Fixed locally: the default-LLM unit path now injects a non-network provider,
    while neighboring init tests opt out of LLM explicitly. Three repeated CLI
    suites passed (`59` each) without the fatal WFP output. The next full suite
    is the final confirmation; do not weaken firewall or security policy.

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
    `<user-home>/.mempalace/repair-runs/sqlite-replay-final-20260703T130250Z/result.json`.
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

## Recently Closed

- [#43 - Pre-push Git environment breaks temporary-repository tests](https://github.com/iMelki/mempalace/issues/43)
  - Closed 2026-08-09 after applying the existing allowlisted environment to every
    Git subprocess that creates and configures disposable project-scanner repos.
    The focused project-scanner and palace-lock tranche passes `53` tests, including
    a regression for inherited repository-local Git variables.

- [#42 - Sanitize personal paths from public documentation](https://github.com/iMelki/mempalace/issues/42)
  - Closed 2026-08-09 after replacing workstation-specific roots in the changelog,
    task index, and dedup fixtures with portable placeholders or synthetic paths.
    The focused dedup suite passed `117` tests with `1` skipped, and local Markdown
    links validated successfully.

- [#27 - Conversation miner cannot reconcile pending drawer-and-closet rewrite](https://github.com/iMelki/mempalace/issues/27)
  - Closed 2026-07-29 after `69ea0d9` bound an existing closet collection
    during conversation recovery and the memsys#89 bounded retry reconciled
    the pending record with zero recovery files remaining.

## Supporting Docs

- [AGENTS.md](AGENTS.md)
- [CONTRIBUTING.md](CONTRIBUTING.md)
- [.github/labels.yml](.github/labels.yml)
