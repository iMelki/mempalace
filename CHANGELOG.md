# Changelog

All notable changes to [MemPalace](https://github.com/MemPalace/mempalace) are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/).

---

## [3.3.5] — unreleased

### Added

- **`dedup --progress` now covers every long pass, and every dedup run ends with
  completion metrics (#32).** `--progress` previously only instrumented the
  exact-duplicate content scan, leaving the two passes an operator actually
  waits on silent. Both are covered now:

  - the metadata pass (`get_source_groups()`), whose 1000-row page loop is
    itself the slow part on a ~1M-drawer palace, *before* any duplicate work
    starts;
  - the embedding-distance dry-run (`dedup_palace()` /
    `dedup_source_group()`), which runs one throttled `col.query` per drawer.
    Its heartbeat is per drawer, not per source, because a single large source
    can dominate an entire run and per-source reporting would sit silent
    through it. This is the pass that precedes any `--apply`, so it is the one
    that most needed visibility.

  Every run now also prints a one-line `Metrics:` summary — duration, an
  outcome, a status, and processed/changed counts — which both entry points also
  return as a dict for programmatic callers. `drawers_flagged` and
  `drawers_removed` are reported separately, so a dry-run states what it found
  while being explicit that it changed nothing (`drawers_removed=0` until
  `--apply`); a count no pass computed prints as `not-computed` rather than `0`.
  A failed post-mutation warm downgrades the outcome to `ok-with-warnings`
  instead of passing silently.

  Progress is opt-in and off by default, and is written to **stderr only** —
  matching `backup_snapshot.py`, and keeping stdout a clean report channel so a
  heartbeat can never interleave into a structured document. The two scans added
  in #33 wrote the same shape to stdout and were moved to stderr.

- **Mining declines generated and vendored content, and reports backup/variant
  directory candidates (#36).** A `wing=coding` audit found ~6,900+ drawers of
  machine-generated `pnpm-lock.yaml` chunks and one project ingested from five
  on-disk copies. Deduplicating afterwards is the bandaid; declining to ingest
  is the durable and safe direction, because deletion is irreversible in a
  store whose requirement is verbatim 100% recall while not ingesting costs
  nothing.

  This extends the mechanisms that already existed rather than adding a fourth
  one. `.gitignore` respect is untouched (it never covered lockfiles — those
  are committed on purpose, which is why they reached the palace); the
  previously hardcoded `palace.SKIP_DIRS` and `miner.SKIP_FILENAMES` sets are
  now one configurable, documented policy in `mempalace/mine_exclusions.py`,
  seeded from the old sets so nothing that was skipped before becomes mineable
  now, and extended with the ecosystems that were missing — most importantly
  `obj/` and `bin/` (a single `CategoriesAPI.Tests/obj` tree contributed 418
  drawers) plus the dependency lockfiles of 12 ecosystems.

  Every default is reversible from the project's `mempalace.yaml` without a
  code change (`exclude.generated_files: false`, or individual names under
  `exclude.allow_files` / `exclude.allow_dirs`), and `--include-ignored` still
  overrides per path. **Whether lockfiles should be excluded at all is the
  operator's policy call** — exclusion is the default because a lockfile's
  recall value is close to zero, not because the recall case was ruled out.

  Backup/variant directories (`*_backup_250522`, `backend-backup-git_broke`,
  `*-alpha-ver`, and directories that are suffixed forms of a sibling) are
  **reported and never auto-excluded**: a directory named `backup` can hold
  the only surviving copy of something, which is exactly why blanket
  exclusion is unsafe. New `mempalace variants DIR [--json]` and
  `mempalace exclusions DIR [--json]` surfaces, plus a report-only advisory in
  the `mempalace mine` header (`--no-variant-report` silences it).

  Nothing already in the palace is deleted or modified — this is purely about
  future ingest. Cleanup of existing duplicates stays under #19 and remains
  gated on a verified offsite backup. Documented in
  [`docs/MINE_EXCLUSIONS.md`](docs/MINE_EXCLUSIONS.md).

### Fixed

- **A redirected `dedup` run no longer dies on the module's own decoration
  (#32).** `_printable()` protected the arbitrary user content dedup prints, but
  was never applied to dedup's own literals: the horizontal rules and `->`
  arrows were literal `U+2500`/`U+2192`. Redirecting a run to a file on Windows
  (`python -m mempalace.dedup --progress > run.log`) gives a cp1252 stream, so
  the dry-run path aborted with `UnicodeEncodeError` immediately after the
  header — before a single result line and before the completion metrics. The
  module's own decoration is now ASCII, and an AST-level test asserts no printed
  literal in `dedup.py` needs a non-ASCII codepage. Found by running the real
  entry point with redirected streams, which `capsys` does not reproduce.

- **`dedup --stats` no longer dies on drawer text a cp1252 console cannot
  encode.** A live cross-source audit aborted with `UnicodeEncodeError` while
  printing a CSS chunk containing CJK font names, after the scan had already
  completed — losing the report. Printed source paths and text previews are now
  rendered through the active stdout encoding with replacement; returned data
  keeps the original text. The top-15 source listing had the same latent
  failure and is covered too.

- **SQLite-locked BM25 fallback completes as a degraded result (#28).** A
  temporary `locked`/`busy` error from the final read-only metadata query no
  longer escapes through the MCP HTTP request and consumes the caller's full
  transport timeout. The result is explicit and retryable; it does not change
  lock ownership, journal configuration, or stored data.

- **Conversation recovery now binds both managed collections.** Conversation
  mining can now reconcile a pending filesystem rewrite that spans drawers and
  closets without creating a closet collection on a fresh conversation-only
  palace. The prior drawers-only binding caused a safe fail-closed error even
  when the live closet collection existed; regression coverage and the live
  evidence are documented in
  `docs/research/conversation-recovery-collection-binding-2026-07-29.md`.

- **Conversation-mode palace-lock contention is a temporary failure (#26).**
  `mempalace mine --mode convos` now gives the CLI the same fail-closed lock
  contract as project mining: when another writer owns the palace lock, the
  command returns temporary-failure exit code `75` with a sanitized retry
  message. Library callers retain the historical clean-return behavior unless
  they explicitly request lock-conflict propagation. A live empty-source probe
  against an active writer returned `75` with zero input files, and the CLI
  regression suite passes `59` tests.

### Added

- **Read-only cross-source exact-duplicate audit for dedup (#19).**
  `python -m mempalace.dedup --stats --cross-source-duplicates` (alias
  `--cross-source`) hashes drawer text across the whole scoped set and groups by
  content hash *regardless of* `source_file`, then reports duplicate-set count,
  total redundant drawers (`N-1` per set), and the contributing source paths for
  each set. The existing `--exact-duplicates` mode groups within a single
  `source_file` and is therefore structurally blind to this palace's dominant
  duplication mode — the same project mined from several on-disk copies, each
  with its own source path. Measured on `--wing coding` (41,934 drawers, 102s):
  8,416 duplicate sets and 22,300 redundant drawers, of which 22,227 span 2+
  distinct source paths (21,017 of those in sets whose paths all share one
  filename, i.e. one file mined from several trees) and only 73 sit inside one
  path; the intra-source mode reports 83 redundant drawers over the same wing
  (0.21%). Each set also reports its distinct-filename count, separating a
  copied directory from different files that share a chunk (CSS boilerplate) —
  same byte-identical storage cost, very different deletion policy. The two
  figures are
  related but not interchangeable: intra-source redundancy that also appears in
  another source merges into one cross-path set instead of staying in the
  single-path bucket. Scoping now includes sources below `min_count`, because
  five copies of one file can be one drawer each. The mode is read-only: it
  never deletes, is rejected outside `--stats`, and deliberately does **not**
  choose a canonical copy — `redundant` is `N-1`, but which copy to keep is an
  operator policy decision, not a tool output. Document text is read in
  500-row batches and released, so the corpus is never resident and nothing
  spills to disk. `--progress` and `--max-sets` bound the output.

- **Immutable evaluation-corpus identity for MemSys gold baselines (#31).**
  - Added the read-only manifest producer. It snapshots `chroma.sqlite3` through SQLite's online backup API, hashes sorted logical drawer rows, validates the strict startup contract, and emits a separate provenance attestation without source rows, paths, or credentials. The producer now persists a completed, integrity-checked snapshot before scanning; the scan writes a source-revision- and snapshot-bound private id/hash shard chain that resumes safely after interruption. Finalization externally merges the shards and streams the historical canonical inventory hash, so incomplete/tampered work cannot publish a public identity and the full logical inventory is never retained in memory.
  - Snapshot-creator and scan/finalizer processor revisions are distinct,
    strict identities. This permits a completed immutable snapshot to be scanned
    after a compatible evaluator release while preventing a scanner/finalizer
    revision change mid-scan from silently altering a published result.
  Native MCP can now accept one startup-only, strict, secret-free evaluation
  manifest bound to its data-plane identity. It exposes only hashed corpus
  provenance after validating the manifest; omitted, malformed, tampered, or
  cross-palace manifests retain the existing fail-closed state or prevent
  startup. This adds no live palace scan, memory mutation, or path disclosure.

- **Crash-resumable source-manifest planning (#25).** Added
  `--plan-progress-jsonl` for project `--dry-run --plan-out` operations. The
  fsynced, hash-chained journal checkpoints directory discovery and every
  completed file descriptor, validates immutable planning identity, repairs
  only a torn non-newline tail, and resumes at the exact next directory/file
  without repeating already committed hashing. Normal append keeps a validated
  in-memory prefix so planning remains linear. Focused progress proof now passes
  `14` tests; the final repository gate passes `1,756` tests with `7` skipped
  and `106` intentionally deselected in `203.30s`. The design and community
  evidence are documented in
  `docs/research/resumable-source-plan-contract-2026-07-29.md`.

- **Crash-resumable deterministic project mining (#25).** Project sources are
  now ordered by normalized project-relative path and can be bound to an
  immutable `--plan-out` / `--manifest` source plan containing exact stat,
  content, parser/config, and miner-revision identities. `--progress-jsonl`
  appends one hash-chained, fsynced, path-free cursor record only after the
  source's managed `COMPLETE`/`ZERO_OUTPUT` receipt is reloaded and exactly
  represented; restart re-verifies the entire prefix against the selected
  palace before skipping it. `--start-index` cannot guess beyond or replay
  behind that prefix. A non-newline torn final append is truncated to the last
  committed record and replayed; complete corrupt/divergent progress, source
  drift, and cross-palace reuse fail closed. CLI palace-lock contention now
  exits with temporary-failure code `75` and a sanitized message. Focused
  hard-exit/restart, drift, lock, idempotency, no-secret, and uninterrupted-
  output-equivalence tests use only disposable palaces. The progress journal
  caches its validated prefix and invalidates that cache on file identity
  change, so repeated durable appends stay linear rather than rereading the
  entire JSONL prefix for every source (`11` focused tests passed); the
  expanded miner, lock, receipt, CLI, and progress regression passes `223`
  tests in `63.98s`.

- **Receipt-required project and conversation miner helpers (#22).** Retired
  the low-level non-dry fallback that could purge or upsert when
  `ReceiptStore` or `ManagedRunIdentity` was omitted. Both public and locked
  helper layers now fail before collection mutation unless the pair is valid
  and bound to the same receipt root; `ReceiptStore.begin_source()` enforces
  the same binding. Dry runs remain receipt-free, top-level CLI mining already
  supplies the managed pair, and the batched benchmark now creates a disposable
  run instead of relying on the retired bypass. The focused requirement and
  real-Chroma batch proof passes `5` tests in `1.31s` and preserves `2,2,1`
  bounded writes. The expanded receipt/miner/conversation/CLI proof passes
  `209` tests in `69.91s`; the final repository gate passes `1,735` tests with
  `7` skipped and `106` intentionally deselected in `211.25s`. No configured
  palace was opened.

- **Native authenticated Streamable HTTP MCP transport (#21).** Added
  `mempalace-mcp-http`, built on the stable Python MCP SDK 1.x low-level
  server/session manager. It binds to loopback by default, requires the
  existing bearer token from process environment, validates Host, and strictly
  parses at most one supplied Origin before SDK routing. MemPalace leaves MCP
  SDK 1.28.1's active-blind idle deadline disabled and instead applies
   active-aware five-minute idle cleanup, a 64-session per-process hard cap,
   pending-creation reservations without lifecycle-lock head-of-line blocking,
   bounded retry tombstones that retain failed terminations in cap accounting,
   header-safe raw-byte bearer comparison that returns 401 for malformed or
   non-ASCII values, and leak-free manager removal after successful MCP DELETE. The dependency and
  runtime are exact-gated to SDK 1.28.1 because this policy coordinates private
  manager maps. Synchronous palace calls remain
  bounded off the event loop, and stdio/HTTP share one transport-neutral tool
  dispatcher. Focused disposable official-client, concurrency, lifecycle,
  cancellation, and malformed-header tests pass. The final repository-wide
  release gate passes 1,673 tests with 7 platform skips, 106 intentional
  deselections, and 191 warnings. Independent transport review is green. The
  committed agent-settings launcher selected native HTTP with backend
  concurrency still serialized at one. The four-client live gate passed, then
  a six-wave `132.39s` sustained burn-in passed `24/24` authenticated read-only
  calls and cleanup with zero lingering workers and one stable exact bridge
  identity. Fresh transport decision
  `mempalace-bridge-transport-readiness-20260714T061355Z` is
  `native-transport-ready`; supergateway remains rollback-only.

- **Receipt-managed MCP drawer and diary writes (#22).** Added one private
  `ManagedMcpMutationService` over the existing managed adapter transaction.
  Drawer add, update, and delete now require a stable logical `source_id`,
  publish exact create/supersession/zero-output receipts, invalidate deleted
  predecessors, verify current collection state, and restore the prior row on
  a failed replacement. Legacy unreceipted rows remain readable but cannot be
  updated or deleted by assigning a new identity after the fact. MCP diary
  writes now publish the same receipts; callers may supply `source_id` as an
  idempotency key scoped by agent and wing, while omitted IDs preserve append
  behavior. Receipts now attest meaning-bearing metadata as well as document
  bytes, source locators are opaque, tombstone retries prove the target ID is
  absent, and old positional add calls cannot silently bind the wrong identity.
  Read/status cache misses no longer create collections or retrofit HNSW
  settings. The transport still exposes a direct read-capable Chroma handle, so
  full cache/facade retirement remains open. The focused receipt, HTTP,
  dispatch, source, and MCP-server suite passes 270 tests with one platform
  skip; the final repository gate passes 1,695 tests with 7 skips and 106
  intentional deselections.

- **Receipt-managed diary-file drawer and closet ingestion (#22).** Replaced
  size-only incremental checks and direct Chroma upserts with one managed
  source transaction per dated Markdown file. Exact source bytes and an
  output-affecting entity-registry/language digest now govern reuse; changes
  replace the complete day drawer and closet set, semantic small-file removal
  publishes a verified `ZERO_OUTPUT` successor, and handled failures restore
  the exact prior drawers, closets, embeddings, receipt head, and state file.
  State publication is atomic convenience state after receipt completion,
  malformed legacy scalar entries self-repair, and duplicate `(wing, date)`
  files fail before palace mutation, while undated-only input returns before
  palace creation. Missing or renamed files remain
  deliberately unpruned until an explicit deletion policy is approved. The
  same language snapshot governs drawer and closet extraction, non-object
  state roots self-repair after the managed commit, and the focused diary proof
  passes 13 tests. Exact snapshot/reuse reads now retry Chroma's classified
  delayed-vector visibility for at most two seconds, while unrelated errors or
  permanent absence still fail closed. The expanded receipt/diary proof passes
  207 tests, and the final repository gate passes 1,706 tests with 7 skips and
  106 intentional deselections.

- **Receipt-managed JSONL sweeper ingestion (#22).** Replaced direct
  timestamp-cursor upserts with one complete managed source transaction per
  physical JSONL file. An isolated sweeper URI prevents message rows from
  claiming or purging primary-miner chunks, while exact file bytes,
  source-namespaced deterministic message IDs, and a source-derived semantic
  metadata hash govern reuse and repair. Invalid UTF-8 and malformed or
  incomplete message records fail before replacement, an existing source
  cannot remove every row without explicit `--allow-zero-output`, and legacy
  unmanaged sweeper rows fail before receipt storage is initialized. Copied or
  renamed sources use disjoint lanes rather than colliding with the retained
  old path. Palace locking now precedes Chroma/receipt access, legacy detection
  covers conservative relative path equivalents for empty sources, and mixed
  JSON content blocks are preserved rather than dropped. Failable semantic
  checks occur before completion; after `COMPLETE`, the driver reloads the
  durable terminal journal event and verifies exact current representation
  before recovery cleanup. A verifier or finalization failure is reported as
  committed-but-unverified instead of throwing as though irreversible success
  rolled back. Terminal-manifest expected rows are reported separately from
  verifier-confirmed represented rows, so a committed-unverified result cannot
  inflate the represented count. Mixed directory runs retain a partial
  per-file-verifier count while zeroing the whole-run represented claim; the
  CLI prints both. Semantic updates, full rewrites, receipt
  rebindings, and total physical mutations are reported separately. Source
  changes during extraction and injected second-batch failures restore the full
  ID-joined predecessor
  lane with no replacement survivors. Windows path-case aliases reuse the same
  source and output semantics. The focused sweeper/CLI proof passes 35 tests;
  the expanded sweeper/CLI/receipt proof passes 204 tests in 100.85 seconds with
  no stderr. The full repository gate passes 1,733 tests with 7 skips and 106
  intentional deselections in 232.05 seconds, also with no stderr.

- **Managed source-write receipts and exact verification foundation (#22).**
  Managed project, conversation, and RFC 002 adapter outputs now emit local
  append-only receipts with exact output manifests. Source locks cover read,
  identity, normalization, and write; conversation runs also take the
  per-palace lock. Normalization errors end in `FAIL` without purging prior
  rows, successful semantic zero-output remains distinct, source metadata uses
  one canonical locator, and cleanup selects canonical paths, validated raw
  aliases, and receipt-stamped source identity. Drawers and closets written by
  the managed project/conversation paths or `managed_adapter_ingest()` are
  receipt-aware; adapter graph operations and writes before a source identity
   fail closed. Handled rewrite failures restore exact pre-purge documents,
   metadata, and embeddings, including bounded retry after a partial rollback
   delete. Before purge, managed rewrites now persist an immutable recovery
   snapshot; restart reconciliation either proves COMPLETE or exactly restores
   the predecessor representation and blocks new writes on corruption or
  baseline drift. Recovery publication now uses fail-closed OS durability:
  Windows `MoveFileExW` write-through plus reopened `FlushFileBuffers` and hash
  proof, or POSIX file/directory `fsync` plus hash proof. Immediately before
  purge, every managed collection must still expose exactly the snapshotted ID
  set. The promotion matrix also corrected a platform-biased failure-injection
  test: a final parent-sync error is now isolated from directory setup and must
  leave the session non-COMPLETE with `ReceiptDurabilityError`.
  Additions, removals, and duplicate pagination identities stop before
   deletion, and only validated IDs are deleted. Managed adapters serialize all
   source refs at the palace's HNSW write boundary. Current lookup treats the
   atomic source index as the head, repairs only one connected explicit
   successor lineage, ignores wall-clock ordering, and fails closed on malformed,
   contradictory, disconnected, or ambiguous index/journal state.
  Shared exports are explicitly pseudonymized: per-palace
  HMAC content/version/error identities and bucketed source size replace global
  hashes and exact bytes. Legacy `mempalace migrate` now refuses a non-dry run
  when managed receipt state exists because that rebuild path cannot preserve
  the journal yet. Other unmanaged mutation paths remain explicitly tracked in
  #22. Managed recovery deletion no longer sends a full-document regex to
  Chroma when the stored receipt content hash exactly matches the fetched
  document. It instead binds deletion to the validated row ID plus source,
  receipt, and content-hash metadata. This avoids Chroma/SQLite extended error
  `1043` for large provider-chat documents while preserving exact-regex checks
  for legacy or stale-hash rows and failing closed for an empty stale-hash row.
  A disposable real-Chroma regression deletes exactly one `393,216`-byte
  receipt-stamped row; the full receipt module passes `110` tests. Chroma
  `1.5.9` still reproduces the oversized-regex failure, so this is a managed
  compatibility boundary rather than an asserted upstream fix. Independent
  receipt re-review found no remaining implementation blocker. A new
  `python -m mempalace.receipt_restart_probe --json` operator probe now creates
  a synthetic Chroma database and uses four strictly sequential processes to
  prove the real hard-exit boundary: the rewrite child exits with code `73`
  after durable recovery publication and a partial replacement, a fresh child
  restores the exact document, metadata, and embedding, and another fresh child
  proves vector retrieval, zero residual recovery state, and SQLite integrity.
  The final guarded operator artifact completed in `7.3s` on Chroma `1.5.9`; it never
  opened a configured palace and removed its disposable database. No live
  historical recovery or cutover was performed. The proof does not claim
  power-loss durability, authorize the 18-source cohort, or make unmanaged
  writers receipt-aware.
  The remaining write boundary is now a tested machine-readable decision
  manifest: 21 surfaces resolve to 10 managed-receipt adaptations, six
  retirements of unmanaged mutation entry points, and five explicit separate
  contracts for non-drawer state. A second tested plan freezes the privacy-safe
  18-source historical cohort and its 22,220 projected rows. It remains `NO-GO`
  with eight pending gates, one-source attended checkpoints, no automatic
  advance, and no claim that a future replay can recreate old write-time
  provenance.

- **`mempalace warm [--json]` — pre-pay the post-mutation first-open cost.**
  Bulk mutations (dedup `--apply`, sqlite-replay) can leave heavy one-time
  work for the next palace open (measured `1,004.3s` after the 2026-07-06
  42,606-drawer dedup, vs `4.6s` warm). `warm` runs a single vector query so
  that cost lands at mutation time; `dedup --apply` now auto-warms after
  deletions. Emits `mempalace.warm.v1` JSON with `--json`. (#19)

- **`mempalace repair-status --json` — machine-readable read-only parity
  status.** Emits a single JSON object to stdout with a schema identifier
  (`mempalace.repair-status.v1`), palace path, UTC timestamp, and
  per-collection (drawers, closets) `sqlite_count`, `hnsw_count`,
  `divergence`, `status` (`OK`/`DIVERGED`), and `note` — so incident bundles
  and sidecar agents can capture exact SQLite-vs-HNSW parity counts without
  scraping console text or launching a replay dry-run. An optional
  `--artifact-dir` writes the same JSON to a timestamped
  `repair-status-<UTC>.json` file without ever creating a repair-run
  directory. The default human output is byte-identical when the flags are
  absent, and the probe stays dependency-light (works in lean runtimes
  without `chromadb`). (#18)

### Operations

- **2026-07-14: native loopback HTTP cutover and sustained burn-in completed
  (#21).** An exact attended restart moved the managed listener to the native
  MemPalace HTTP server without touching Router, QMD, Meili, Hindsight, Honcho,
  or code search. The initial four-client gate passed. Six additional
  four-client waves then ran for `132.39s`, producing `24/24` successful
  authenticated `mempalace_status` calls, `24/24` cleanup receipts, zero
  lingering workers, and no managed bridge identity change. Fresh readiness and
  transport-decision artifacts are `ok`; supergateway is retained only as an
  explicit rollback. No palace data, hosted service, or Railway resource was
  mutated by the transport smokes.

- **2026-07-12: historical write evidence bounded and recovery moved to #22.**
  A path-redacted read-only audit linked 25,448 of 29,449 retained source-ledger
  paths to current drawer metadata and separated 1,435 bootstrap-only rows,
  2,548 format exclusions, and 18 intact current-rule candidates. Those 18
  project to 22,220 rows and are classified as probable never-receipted output,
  not proven deletion or corruption. #22 now owns terminal source-write
  receipts, exact source-to-drawer verification, and any separately approved
  supervised recovery. No historical source was mined or written in this
  slice.

- **2026-07-11: native loopback HTTP MCP path selected (#21).** The owning
  implementation issue now specifies transport-neutral dispatch, native
  Streamable HTTP on `127.0.0.1:8787`, Origin/auth enforcement, bounded
  backend concurrency, and concurrent smoke. Current supergateway containment
  remains a temporary rollback path under agent-settings #209; no runtime
  transport was changed in this documentation slice.

- **2026-07-07: duplicate-drawer go/no-go tracking clarified (#19).**
  `OPEN_TASKS.md` now lists #19 as an active supervised decision lane, records
  the read-only `dedup --stats` estimate (`~292,998` heuristic remaining
  duplicates over the 825,422-drawer palace), and keeps live deletion gated on
  source-scoped dry-run review, fresh backup, artifact logging, and operator
  sign-off. The estimate is explicitly not a reviewed deletion list.

- **2026-07-04: drawers HNSW segment fully rebuilt and verified (#12 closed).**
  The supervised non-dry `repair --mode sqlite-replay` completed
  2026-07-03T18:13:13Z with `replayed=verified_count=856,510`, zero warnings,
  in ~5h10m. Post-replay `repair-status`: drawers `sqlite=861,715` /
  `hnsw=850,000` (divergence `11,715`, within flush-lag tolerance, down from
  `818,039`). #13 was retitled to track the remaining real blocker: the
  MemSys-side `mempalace_mcp_wrapper.py` still unconditionally forces
  keyword-only search (April 2026 chromadb-crash workaround), so restored
  vector data is not yet reachable through MCP search.

### Bug Fixes

- **Independent receipt/status review remediation (#21, #22).** Unknown or
  unreadable HNSW evidence now keeps vectors disabled; probe flights and
  callers have bounded lifetimes, stale late results are ignored, and cache
  identity includes DB file identity plus HNSW metadata evidence. Chroma client
  and collection opening/pinning are process-serialized and refresh their own
  post-open DB identity, preventing concurrent status deadlocks and
  self-triggered reconnects. Four concurrent status calls now pass through the
  real HTTP dispatcher on an ephemeral socket with one probe, and the low-level
  SDK path completes 30 sequential initialize/call/DELETE sessions.
- **Managed receipt boundaries now fail closed under reviewed races (#22).**
  Receipt verification uses a strictly non-mutating current-head lookup and
  rejects COMPLETE events without the durable publication marker. Adapter core
  orchestration keeps raw collection/graph objects in closure-owned weak
  registries and exports only narrow receipt-aware operations; there is no
  importable authority token, raw registry, or raw-handle-returning function.
  Identity-selected rows must match both source HMAC and source-file ownership. Managed and MCP
  mutations share the palace lock, existing-row and delete rechecks include
  embeddings, and exact document/metadata readback is separated from optional
  embedding readback. Real-Chroma visibility receives a bounded two-second
  exact retry window. File mtimes are canonicalized to Chroma's six fractional
  digits before exact readback, so permanent representation differences do not
  consume that window. Exact vectors now come only from Chroma's supported
  collection API. If its metadata/vector views remain divergent, the managed
  operation fails closed and restores its predecessor instead of opening the
  live Chroma SQLite/WAL from a second library connection. The full disposable
  suite remains a required release gate.
- **Chroma client shutdown is now explicit and testable (#22).**
  `close_palace()` and backend `close()` call the public Chroma client
  lifecycle instead of merely dropping Python references; duplicate aliases
  close one client once. A disposable and now automated real-Chroma regression
  proves three explicit vectors below the configured `50,000` sync threshold
  survive final-client close/reopen within `1e-6` float32 tolerance. Automatic
  cache refresh deliberately does not close the replaced client yet because
  callers may still hold collection handles backed by it; handle-aware refresh
  retirement remains tracked in #22.
- **`mempalace repair --mode sqlite-replay` now gives large diverged palaces a
  safe recovery path.** Dry-run reads the Chroma SQLite metadata segment without
  importing Chroma, reconstructs typed drawer documents/metadata, and reports
  replay scope before any destructive work. Approved runs snapshot
  `chroma.sqlite3`, rebuild only the `mempalace_drawers` collection, stream
  progress with ETA, and refuse large re-embedding runs unless
  `--confirm-large-reembed` is explicitly supplied. The focused repair tests
  also run with pytest's cache provider disabled so a broken local
  `.pytest_cache` ACL cannot mask repair-path regressions.
- **SQLite replay now has operator-grade bounds and artifacts.** `repair --mode
  sqlite-replay` accepts `--max-rows`, `--max-batches`, `--artifact-dir`, and
  `--json`; bounds abort before any Chroma collection is opened or deleted,
  every valid run writes `result.json` plus `events.jsonl`, and non-dry replay
  always reads from an immutable source snapshot even if `--no-backup` is
  supplied. Partial resume is explicitly unsupported (`resume_supported=false`)
  until a real checkpointed replay is implemented. (#16)
- **`mempalace repair-status` is now dependency-light too.** The HNSW capacity
  probe reads SQLite plus `index_metadata.pickle` locally instead of importing
  the Chroma backend package, so status still reports drawer/closet divergence
  in a lean Python runtime that lacks `chromadb`.
- **`mempalace status` no longer opens the crash-prone drawers HNSW segment just
  to print counts.** The status path now reads collection and room totals
  directly from `chroma.sqlite3` first, falling back to Chroma pagination only
  when SQLite metadata is unavailable. This keeps local status/reporting usable
  after a persisted HNSW segment is quarantined for a Chroma native crash; full
  historical vector rebuild is tracked separately in #12.
- **`mempalace status` no longer imports the mining/vector stack before the
  SQLite-first fallback can run.** The CLI now routes status through a
  dependency-light module, so lean local runtimes without `chromadb` can still
  report SQLite drawer totals during a vector incident. The regression tests pin
  both the lazy CLI import and the `METADATA`-segment ground-truth count. (#14)
- **Pre-push tests no longer depend on Chroma's default ONNX model download.**
  The miner tests that open raw Chroma collections now use the repo's
  deterministic test embedding fixture, keeping the suite offline-safe when TLS
  or model-cache state is unavailable.
- **Local hook governance.** Reinstalled the git-toolkit secrets filter and
  commit hooks, added the baseline `.git-secrets-ignore` deep-scan exclusions,
  and verified the governance audit is clean. (#10)
- **Repo baseline hygiene.** Added tracked `.gitattributes` secrets-filter
  rules and local `.git-secrets.json` ignore coverage so downstream repos and
  operator audits stop flagging the MemPalace checkout as governance-drifted.
- **`mempalace_diary_read` silently dropped entries on agent-name case mismatch.** `tool_diary_write` stored the `agent` metadata verbatim after `sanitize_name`, which preserves case, while `tool_diary_read` filtered by exact match. Writing as `"Claude"` and reading as `"claude"` (or vice-versa) returned zero rows. Both endpoints now lowercase `agent_name` immediately after sanitization, so reads are case-insensitive and the default per-agent wing slug is stable across casings. **Behavior change:** entries written prior to this fix under mixed-case agent names will not match the new lowercase filter; run `mempalace repair` if you need to migrate legacy diary metadata. (#1243)

### Documentation

- **HNSW incident tracking now reflects the final Codex provider-chat drain and
  fresh replay dry-run.** Current `repair-status` evidence reports drawers
  SQLite `856,510`, HNSW `38,471`, divergence `818,039`, and closets still
  within tolerance at SQLite `12,107`, HNSW `11,826`, divergence `281`.
  The fresh SQLite replay dry-run planned `856,510` rows in `857` batches,
  replayed `0`, and left the live collection unchanged. A post-drain palace
  backup is now verified tar-readable at
  `C:\Users\Milky\.mempalace\backups\palace-2026-07-03-1526-pre-hnsw-sqlite-replay-final-drain.tar.gz`
  (`14,636.8 MB` compressed in `986.5s`; total `1,062.6s`; upload disabled),
  satisfying the fresh-backup gate before any supervised non-dry replay.
- **HNSW incident tracking now reflects the post-provider-chat SQLite growth.**
  The local task index and GitHub issue readbacks were refreshed after the
  latest bounded Codex provider-chat drain window: drawers now show SQLite
  `851,964`, HNSW `33,982`, divergence `817,982`, and bridge fallback still
  `vector_disabled=true`.
  Filed #18 for a machine-readable `repair-status --json` proof path so future
  incident bundles do not have to scrape human text or run replay dry-runs just
  to capture parity counts.
- **Website SEO/GEO baseline.** Added VitePress sitemap configuration,
  per-page canonical and `og:url` metadata, absolute Open Graph image URLs,
  basic JSON-LD, and a public `robots.txt` pointing at the sitemap. Build-output
  validation is tracked in #11 because local website dependencies were absent
  during the automation dry run.
- **Repair CLI reference caught up with the SQLite replay workflow.** The CLI
  docs now show `repair-status`, `repair --mode sqlite-replay --dry-run`,
  `--batch-size`, `--max-rows`, `--max-batches`, `--artifact-dir`, `--json`,
  and `--confirm-large-reembed`, with the caveat that `--batch-size` is not a
  total replay limit. (#16)

---

## [3.3.4] — 2026-04-30

### Added

- **`mempalace init` now prompts to mine the same directory.** After entity confirmation, room detection, and gitignore guard, `init` shows a one-line scope estimate (e.g. `~423 files (~12 MB) would be mined into this palace.`) computed from its existing corpus walk, then asks `Mine this directory now? [Y/n]` (default yes) and runs `mine()` in-process if accepted. The estimate fires before the prompt so users on a real corpus aren't surprised by a minutes-long ChromaDB write. Declining prints the exact `mempalace mine <dir>` command for later. (#1181)
- **New `--auto-mine` flag on `mempalace init`** for the non-interactive path (`mempalace init --auto-mine <dir>` skips the mine prompt and runs mine directly). `--yes` retains its existing scope of entity auto-accept only and still prompts for the mine step, so existing scripted callers see no behaviour change; combining `--yes --auto-mine` gives a fully non-interactive setup. (#1181)
- **Cross-wing topic tunnels.** When two wings have confirmed `TOPIC` labels in common (the LLM-refine bucket from `mempalace init --llm`), the miner now drops a symmetric tunnel between them at mine time so the palace graph reflects shared themes (frameworks, vendors, recurring concepts). Tunnels are routed through the existing `create_tunnel` storage so they share dedup and persistence with explicit tunnels. Topic tunnels are stored under a synthetic `topic:<name>` room and tagged with `kind: "topic"` on the stored dict — this keeps them distinct from literal folder-derived rooms of the same name (a wing with both an `Angular` folder room and an `Angular` topic tunnel no longer collides at `follow_tunnels` read time) and gives LLMs scanning `list_tunnels` a visible discriminator. Threshold is configurable via `MEMPALACE_TOPIC_TUNNEL_MIN_COUNT` env var or `topic_tunnel_min_count` in `~/.mempalace/config.json` (default `1`). Manifest-dependency overlap and per-topic allow/deny lists remain out of scope. (#1180)
- **Context-aware corpus detection at `mempalace init`.** A new Pass 0 runs at the start of `init` — before entity detection — and answers one question: *is this corpus an AI-dialogue record, and if so, which platform and what persona names has the user assigned to the agents?* Tier 1 is a free regex heuristic (well-known AI brand terms + turn-marker patterns, with a co-occurrence rule that suppresses ambiguous terms like `Claude`/`Gemini`/`Haiku` when no unambiguous AI signal is present, so French novels and astrology forums don't false-positive). Tier 2 is an LLM call (~$0.01 with Anthropic Haiku, free with local Ollama/LM Studio/llama.cpp/vLLM) that extracts `user_name` and `agent_persona_names` from dialogue structure. Result is persisted to `<palace>/.mempalace/origin.json` with a `schema_version: 1` envelope so downstream tools can read it. Entity classification then routes names matching `agent_persona_names` (case-insensitive) into a new `agent_personas` bucket instead of `people`, so a Claude Code transcript no longer misclassifies the user's `Echo`/`Sparrow`/`Cipher` agents as biological people. `llm_refine` receives the same context as a system-prompt preamble so it can disambiguate other ambiguous candidates with corpus-level knowledge too. Backwards compatible: callers that don't pass `corpus_origin` see the v3.3.3 return shape unchanged. (#TBD)
- **`mempalace init` runs LLM-assisted refinement by default.** v3.3.3 made `--llm` opt-in; the LLM-assisted path is qualitatively better (extracts persona names, refines ambiguous classifications) so it now runs by default. Provider precedence is unchanged — Ollama at `http://localhost:11434` first, then openai-compat, then anthropic with API key. **Never blocks init on a missing LLM**: if no provider is reachable (Ollama not running, no API key set), init prints a one-line message pointing at `--no-llm` and falls through to the heuristic-only path. `--no-llm` is the new explicit opt-out. The legacy `--llm` flag is preserved as a deprecated alias of the default so scripted callers see no behaviour change. Cost story: zero for users with a local LLM (the majority on this repo), ~$0.01 per init for users with `ANTHROPIC_API_KEY` set who explicitly choose `--llm-provider anthropic`, zero for users with no LLM (graceful fallback). (#TBD)
- **`mempalace mine --redetect-origin` flag.** Re-runs corpus-origin detection on the current corpus state and overwrites `<palace>/.mempalace/origin.json`. Useful when the corpus has grown since `mempalace init` and the stored origin may be stale. Heuristic-only by design (the flag is meant to be cheap); re-run `mempalace init` for full Tier 2 LLM refinement. Default `mempalace mine` does not touch `origin.json` — the flag is opt-in. (#TBD)

### Bug Fixes

- **MCP server `tool_diary_write` SIGSEGV when default EF provider differs.** `mcp_server._get_collection` bypassed `ChromaBackend.get_collection` and called `client.get_collection` / `client.create_collection` without `embedding_function=`. ChromaDB 1.x persists the EF *identity* (its `name()`) with the collection but not the EF *instance/configuration*, so the MCP server's reopen silently bound chromadb's built-in `DefaultEmbeddingFunction` — its `name()` matches `mempalace.embedding`'s spoofed `"default"` so the identity check passes, but its provider list is chromadb's default rather than the user's resolved device. The miner / Stop hook ingest path routes through the backend helper and binds the configured EF instead. On bleeding-edge interpreters (python 3.14 + chromadb 1.5.x on Apple Silicon) the default provider selection could SIGSEGV the host process on first `col.add()`, killing the MCP stdio server and leaving every subsequent tool call returning `Connection closed` until Claude Code was relaunched. `_get_collection` now reuses `ChromaBackend._resolve_embedding_function()` on the reopen branches that actually open a collection (warm-cache reads stay zero-cost), matching the miner/backend path. (#1299, follow-up to #1262 / #1289)
- **Cross-wing topic tunnels for hyphenated dir names.** `mempalace init` recorded the `topics_by_wing` registry key under the raw directory name (e.g. `mempalace-public`), while `mempalace.yaml`'s `wing` field used the lower-cased + separator-collapsed slug (`mempalace_public`). At mine time the miner read the slug from the yaml and missed the registry, so `_compute_topic_tunnels_for_wing` returned `0` silently. Real-world: any project whose folder contained a hyphen or space lost every topic tunnel. Producer side: `cmd_init`, `room_detector_local`, `miner.load_config` no-yaml fallback, and `convo_miner` now all route through a shared `normalize_wing_name()` in `config.py` so future writes use the same key. Lookup side: `palace_graph.create_tunnel`, `list_tunnels`, `follow_tunnels`, and `find_tunnels` normalize incoming wing names too, so existing palaces with raw-name keys on disk also recover. (#1194, #1195, #1197, follow-up to #1180)
- **HNSW index bloat from repeated resize+persist cycles.** ChromaDB's HNSW segment was growing into the tens of GB on palaces past ~15K drawers because `link_lists.bin` was being re-allocated on every flush. Setting `hnsw:batch_size` and `hnsw:sync_threshold` on collection metadata via the new `_HNSW_BLOAT_GUARD` constant pins the segment to one allocation per batch instead. Empirical: a fresh 39,792-drawer palace went from 30 GB on disk and segfaulting `mempalace status` to 376 MB and instant. Migration note — already-bloated palaces still need a `mempalace repair` or full re-mine; HNSW config is honoured at collection-create time only. (#1191, supersedes #346)
- **`max_seq_id` poisoning from old `_fix_blob_seq_ids` shim.** The 0.6.x → 1.5.x BLOB-to-INTEGER migration was running `int.from_bytes(blob, 'big')` over chromadb 1.5.x's native `b'\x11\x11' + ASCII-digit` `max_seq_id` format, yielding ~1.23e18 integers that silently suppressed every subsequent `embeddings_queue` write for the affected segment. The shim is now narrowed to the `embeddings` table only, with an additional defense-in-depth guard that skips sysdb-10-prefixed BLOBs even there. New `mempalace repair --mode max-seq-id` un-poisons existing palaces either from a pre-corruption sidecar DB (exact restore) or heuristically (`MAX(embeddings.seq_id)` over the owning collection). (#1135)
- **Auto-ingest hooks now mine the active transcript as `--mode convos`.** Stop and PreCompact hooks were spawning `mempalace mine <transcript-dir>` without `--mode`, defaulting to `projects` — so Claude Code session JSONLs were being ingested as if they were source code via `READABLE_EXTENSIONS`. The hooks now thread the correct mode through every spawn, and `MEMPAL_DIR` (when set) becomes additive rather than overriding the transcript path: a user with `MEMPAL_DIR` pointed at their project still gets the active conversation mined verbatim. Shell hooks also gained the same `_validate_transcript_path` rejection logic the Python entry point already had (extension + `..` traversal). (#1230, #1231)
- **CLI `mempalace search` retrieval quality.** The CLI was using pure ChromaDB cosine distance with no BM25 rerank, so drawers containing every query term but embedding as noise (directory listings, diff output, shell logs) scored `Match: 0.0` alongside genuinely irrelevant results with no way to tell them apart. Wired the CLI through the same `_hybrid_rank` the `mempalace_search` MCP tool already used, and surfaced both `cosine=` and `bm25=` scores in the output so users see which component of the match is firing. MCP search was unaffected; this fixes the human-facing CLI parity gap.
- **Legacy-palace distance-metric warning.** CLI search now detects palaces created before `hnsw:space=cosine` was consistently set and prints a one-line notice pointing at `mempalace repair`. Without the warning such palaces silently used L2 distance, under which the similarity display floored every result to `Match: 0.0`. New palaces mined today already set cosine correctly and now have invariant tests pinning that behavior so future refactors can't silently regress it. (#1179)
- **Graceful Ctrl-C during `mempalace mine`.** Interrupting a long mine no longer dumps a multi-frame `KeyboardInterrupt` traceback. The main file-processing loop now catches the signal, prints `files_processed: N/M`, `drawers_filed: K`, and `last_file:` so the user knows what landed, then exits with code 130 (standard SIGINT). Already-filed drawers are upserted idempotently on re-mine via deterministic IDs, so resuming is safe. The hooks PID lock at `~/.mempalace/hook_state/mine.pid` is now also actively cleaned up in a `finally` when its entry points at us — clean exit, error, or interrupt — preventing the next hook fire from briefly waiting on a stale PID. (#1182)
- **`mempalace init` is now idempotent across re-runs.** Running `init` twice on the same project produced different `origin.json` results because the first run wrote `entities.json` into the project directory, and the second run's corpus-origin sampling included that file as corpus content — shifting Tier 1's character-density math. Sampling now skips the per-project artifacts (`entities.json`, `mempalace.yaml`), so re-running `init` produces the same classification it did the first time. Pinned by an integration test in `tests/test_corpus_origin_integration.py`. (#TBD)
- **HNSW divergence floor now scales with `hnsw:sync_threshold`.** The capacity probe added in #1227 hardcoded a 2,000-row floor for the "DIVERGED" decision, sized against chromadb's default `sync_threshold` of 1,000. The bloat-guard fix above (#1191) raised `hnsw:sync_threshold` to 50,000 without updating the divergence floor to track. Result: any new palace past ~100K drawers spent roughly 80% of each write cycle reporting `DIVERGED`, and `mcp_server._refresh_vector_disabled_flag` silently routed vector search to the BM25 fallback even though chromadb was behaving correctly. The floor now reads `hnsw:sync_threshold` from collection metadata and scales to `2 × sync_threshold`, preserving the legacy 2,000 fallback for older palaces that pre-date #1191. (#1287, fixes interaction between #1191 and #1227)
- **Stop hook no longer crashes ChromaDB on reopen.** `ChromaBackend.get_collection(create=True)` was calling `client.get_or_create_collection` with metadata on every open. In chromadb 1.5.x, when that metadata differs from the stored collection metadata the Rust binding SIGSEGVs with no traceback — the failure mode behind the session-end stop-hook crashes reported in #1089. The call is now split into `get_collection` first, falling back to `create_collection` only when the collection does not yet exist. Existing palaces open without touching their metadata; new ones are created with the full settings as before. The MCP server's `_get_collection(create=True)` path (reached by `tool_add_drawer` and `tool_diary_write`, the latter being what the Stop hook fires at session end) carried the same metadata payload at a parallel call site and got the same try/except split applied, closing the crash class on both reopen paths. (#1089, #1262, #1289)
- **`repair --mode max-seq-id` heuristic now decodes BLOB-typed `embeddings.seq_id` rows.** The recovery feature added in #1135 was running `int(row[0])` directly on the result of `MAX(e.seq_id)`. On palaces where chromadb 1.5.x has been writing seq_ids natively (8-byte big-endian uint64 BLOB), that raised `ValueError: invalid literal for int() with base 10: b'\x00\x00\x00\x00\x00\x00-\xae'` before the dry-run summary could print, leaving users with no path through the un-poison feature #1135 was specifically designed to provide. `_compute_heuristic_seq_id` now decodes BLOB return values via `int.from_bytes(val, "big")` and keeps the existing `int(val)` path for INTEGER rows. (#1254, #1288, follow-up to #1135)

---

## [3.3.3] — 2026-04-23

### Bug Fixes

- **Install regression** — `mempalace-mcp` console script is now declared in `pyproject.toml` alongside `.claude-plugin/plugin.json`'s reference to it. In v3.3.2 the two drifted apart (plugin.json shipped the new `"command": "mempalace-mcp"` form before the matching entry point landed), so every fresh `pip install mempalace==3.3.2` produced a Claude Code plugin config pointing at a binary that wasn't installed. (#1093, #340)
- Restore silent-save visibility after the Claude Code 2.1.114 client regression — production transcript saves were failing silently until this PR. (#1021)
- Paginate `status`-path metadata fetches so large palaces don't trip SQLite variable limits. (#851)
- Resolve the Claude plugin hook runner across platform / plugin-dir variations; previously broke on Windows and some macOS layouts. (#942)
- Real `python3` resolution for `.sh` hooks with a `MEMPAL_PYTHON` override path. (#833)
- Add optional `wing` parameter to `tool_diary_write` / `tool_diary_read` and derive per-project wing from the Claude Code transcript path when writing from the stop hook — diary entries from different projects no longer collapse into a shared default wing. (#659)
- Treat empty string as "no filter" in `mempalace_search` `wing`/`room`; LLM agents that default to filling every optional parameter with `""` no longer get bounced with `must be a non-empty string`. (#1097, #1084)
- Broaden `_wing_from_transcript_path` to handle Claude Code project folders without a `-Projects-` segment (e.g. `~/dev/<parent>/<project>`, `~/code/<project>`). The project name is now derived from the final dash-separated token of the encoded folder, so Linux users with code outside `~/Projects/` get per-project diary scoping instead of falling through to `wing_sessions`. (#1145, follow-up to #659)
- `mempalace_diary_read(wing="")` now returns diary entries from every wing this agent has written to, matching the #1097 "empty-string as no filter" pattern. Previously defaulted to `wing_<agent>`, siloing entries that hooks wrote to project-derived wings. (#1145)
- `mempalace mine` now skips the generated `entities.json` file so its contents aren't re-ingested as project content. (#1175)

### Improvements

- **Deterministic hook saves.** Save hook now uses a silent Python API path, so successive hook invocations produce reproducible results and zero data loss on the hot path. (#673)
- **Graph cache with write-invalidation** inside `build_graph()` — warm-path calls no longer rebuild the palace-graph per request. (#661)
- **`mempalace init` entity detection overhaul.** Canonical project names now come from package manifests (`package.json`, `pyproject.toml`, `Cargo.toml`, `go.mod`) and real people come from git commit authors, rather than being inferred from prose. Includes union-find dedup across name/email aliases, bot filtering that keeps `@users.noreply.github.com` humans, and automatic "mine" flagging by contribution share. (#1148)
- **Regex detector accuracy.** CamelCase extraction so `MemPalace`, `ChromaDB`, `OpenAI` aren't fragmented; tighter versioned/hyphenated pattern kills `context-manager` / `multi-word` false positives; dialogue `^NAME:\s` requires ≥2 hits so `Created: <date>` metadata stops classifying field names as people; expanded stopwords for common English participles and descriptors; high-pronoun signal classifies as person rather than dumping to uncertain. (#1148)
- **Init → miner wire-up.** Confirmed entities merge into `~/.mempalace/known_entities.json` on init, which the miner reads to tag drawer metadata for entity-filtered search. Previously init's output was not consumed by the miner; the per-project `entities.json` is kept as an audit trail. (#1157)
- **Case-insensitive project dedup** across manifest, git, and convo sources so casing variants of the same project name collapse into one review entry. (#1175)

### Added

- i18n: Belarusian translation. (#1051)
- i18n: entity detection for German, Spanish, and French locales. (#1001)
- i18n: Traditional + Simplified Chinese entity detection. (#945)
- **`mempalace init --llm`**: optional LLM-assisted entity classification. Defaults to local Ollama (zero-API); also supports any OpenAI-compatible endpoint (LM Studio, llama.cpp server, vLLM, OpenRouter, etc.) and the Anthropic Messages API. Runs interactively with a progress indicator; Ctrl-C cancels cleanly and returns partial results. Useful for prose-heavy folders where the regex detector struggles (diaries, transcripts, research notes). Opt-in only — default init path remains zero-API. (#1150)
- **Claude Code conversation scanner.** `~/.claude/projects/<slug>/` directories now contribute project entities using each session's authoritative `cwd` metadata, avoiding slug-decoding ambiguity. (#1150)

### Known — deferred to v3.3.4

- HNSW parallel-insert SIGSEGV when `hnsw:num_threads` is unset on collection creation (#974) — fix in-flight as #976, awaiting rebase against develop.

---

## [3.3.2] — 2026-04-19

### Bug Fixes

- Fix silent drop of `.jsonl` files in project miner; raise `MAX_FILE_SIZE` cap from 10 MB to 500 MB so large transcripts no longer fall through unnoticed. Adds a tandem **sweeper** — a message-level, timestamp-coordinated, idempotent safety net that catches anything the primary miner missed. (#998)
- `mempalace sweep <target>` CLI to run the sweeper on demand against a transcript file or a directory. (#998)
- Guard `Layer3.search_raw` against `None` doc/meta rows returned by ChromaDB — prevents `AttributeError` crashes on mixed-schema palaces. (#1011, #1013)
- Guard searcher API path, closet loop, and miner status histogram against `None` metadata; matching guards added to `tool_status` / `list_wings` / `list_rooms` / `get_taxonomy` in the MCP server. (#999)
- Upgrade `chromadb` floor to `>=1.5.4` for Python 3.13 / 3.14 compatibility and pin upper bound to `<2` so future breaking majors don't silently install. (#1010)
- Fix Unicode checkmark rendering on Windows terminals that can't encode the `✓` glyph — avoids `UnicodeEncodeError` crashes on first-run output. (#681)
- **`quarantine_stale_hnsw`** — on open, detect HNSW segment directories whose `data_level0.bin` is significantly older than `chroma.sqlite3` and rename them out of the way. Recovers cleanly from HNSW/sqlite drift that otherwise causes SIGSEGV on `count()` / `query(...)` (the chroma-core/chroma#2594 failure mode). Rebuilds the index lazily on next use. (#1000)
- **PID file guard** — `mine` writes a per-source-directory PID file and refuses to start if an existing mine is still running, preventing process stacking that bloats HNSW and wedges concurrent writes. Includes cross-platform PID liveness check (`os.kill(pid, 0)` terminates on Windows, so the guard falls back to a platform-aware probe). (#1023)

### Improvements

- **RFC 001 §10 — typed backend contracts.** `BaseBackend` now returns typed `QueryResult` / `GetResult` dataclasses and `PalaceRef` for palace identity; registry-based backend discovery. Internal refactor; no user-facing API change. (#995)
- **RFC 002 §9 — source adapter scaffolding.** Introduces `BaseSourceAdapter`, adapter registry, and `PalaceContext` — the plumbing that future pluggable ingest sources will target. Internal refactor; no user-facing API change yet. (#1014)

### Documentation

- **RFC 002** — full specification for the source adapter plugin system (future pluggable ingest). (#990)
- First-run help text and `README` now reference the real `~/.claude/projects/<project>/` path shape instead of the placeholder `/path/to/transcripts`. (#996, #1012)

### Internal

- Harden sweeper for production: verbatim tool blocks, full `session_id`, logged failures.
- Address Copilot review on #995: cursor tie-break, honest metrics, accurate comments.
- Test hygiene: avoid ONNX network download in update-length validation tests; dedup update-length-validation tests; fix Windows file-lock in cache-invalidation test.

---

## [3.3.1] — 2026-04-16

### New Features

**Multi-language entity detection** — lexical patterns (person verbs, pronouns, dialogue markers, project verbs, stopwords, candidate character classes) now live in the optional `entity` section of each locale JSON under `mempalace/i18n/<lang>.json`. Every public function in `entity_detector` accepts a `languages=` tuple and unions patterns across enabled locales. Default stays `("en",)` so existing English-only callers are unchanged. (#911)

- **Five new fully-supported locales** with CLI strings, AAAK compression instructions, and entity-detection patterns:
  - Brazilian Portuguese `pt-br` (#156)
  - Russian `ru` (#760)
  - Italian `it` (#907)
  - Hindi `hi` (#773)
  - Indonesian `id` (#778)
- **`MempalaceConfig.entity_languages`** — persistent palace-level language selection; `MEMPALACE_ENTITY_LANGUAGES` env override; `mempalace init --lang en,pt-br` flag that saves to `~/.mempalace/config.json` (#911)
- **Per-language `candidate_pattern`** — non-Latin scripts register their own character class, so names like `João`, `Инна`, `राज` are no longer silently dropped by the ASCII-only default (#911)
- **VSCode devcontainer** matching the CI environment (#881)
- `MEMPAL_VERBOSE` env toggle — developers see diaries surfaced in chat while the default remains silent (#871)
- `created_at` timestamps included in search results (#846)

### Bug Fixes

**i18n / Unicode**

- Script-aware word boundaries for combining-mark scripts — Python's `\b` fails on Devanagari vowel signs (`ा ी ु`), Arabic, Hebrew, Thai, Tamil, Khmer etc., truncating names like `अनीता` → `अनीत` and making person-verb patterns never fire. Locales now declare an optional `boundary_chars` field and the i18n loader expands `\b` into a script-aware lookaround boundary (#932)
- Case-insensitive BCP 47 language code resolution — `--lang PT-BR`, `zh-cn`, `Pt-Br` previously fell through to English silently; now resolve to the canonical locale file via lowercase matching, with the entity-pattern cache keyed on the canonical form so casing variations share one cache entry (#928)
- Wire i18n candidate patterns into `miner._extract_entities_for_metadata()`, `palace.build_closet_lines()`, and `entity_registry.extract_unknown_candidates()` — three code paths that still hardcoded ASCII-only `[A-Z][a-z]{2,}` and silently missed Cyrillic, accented Latin, and non-Latin entity metadata tags (#931)
- Explicit `encoding="utf-8"` on `Path.read_text()` calls across entity_registry, instructions_cli, split_mega_files, and onboarding tests — prevents Windows GBK (and other non-UTF-8) locales from corrupting UTF-8 files (#946, #776)
- `ko.json` `status_drawers` used `{drawers}` instead of `{count}`, showing the raw template string instead of the number (#758)
- Move `test_i18n.py` from inside the installed package into `tests/` so pytest actually collects it; remove the `sys.path.insert` hack (#758)
- `Dialect.from_config()` defaulted to `current_lang()` (module-global) when config had no `lang` key — replaced with explicit `"en"` fallback for determinism (#758)

**Other**

- Guard `KnowledgeGraph.close()` and `query_relationship`/`timeline`/`stats` methods with the instance lock to prevent concurrent-access corruption (#887, #884)
- Replace invalid `{"decision": "allow"}` with `{}` in hook responses — the string wasn't a valid decision value and triggered schema warnings (#885)
- `entity_registry.research()` defaults to local-only — previously made outbound Wikipedia HTTPS requests without explicit user opt-in; callers now must pass `allow_network=True` (#811)
- Precompact hook no longer blocks compaction when it fails or takes too long (#856, #858, #863)
- Redirect stdout to stderr during MCP server import so library logging can't corrupt the JSON-RPC channel (#225, #864)
- `mempalace init` auto-adds per-project files to `.gitignore` in git repositories so users don't accidentally commit `mempalace.yaml` / `entities.json` (#185, #866)
- Searcher guards against empty ChromaDB query results that previously raised on edge-case corpora (#195, #865)
- Return empty status instead of an error on a cold-start palace with no drawers yet (#830, #831)
- Restrict file permissions on sensitive palace data (#814)
- Slack transcript importer writes a provenance header and preserves speaker IDs (#815)
- Allow `mempalace mine` to run in directories without a local `mempalace.yaml` and surface the missing-yaml warning on stderr (#604)
- Security hook injection fix (#812)
- Save hook auto-mines transcripts even when `MEMPAL_DIR` is unset (#840)
- Pin the Pages custom domain via a shipped `CNAME` in the deploy artifact (#877)
- Version drift safeguard — sync pyproject + `version.py` + README badge in one place (#876)
- Deploy docs workflow now runs on `develop` only, preventing accidental main-branch deploys (#845)

### Improvements

- Regex compilation optimization for entity extraction — pre-compile per-entity pattern sets once and cache by `(name, languages)` tuple, so multi-language callers don't thrash the cache (#880)
- Knowledge-graph value sanitization now preserves natural punctuation (commas, colons, parentheses) that commonly appears in KG subject/object values (#873)

### Documentation

- Clarify that `mempalace init` requires a `<dir>` argument in CLI help text (#210, #862)
- Domain name and specific impostor sites called out in the scam-alert section (#869)
- Tightened `SECURITY.md` with a real version-support policy and the GHPVR-only reporting channel (#810)
- Fixed stale `pyproject.toml` URLs (#853)
- v4 planning prep (#852)

### Internal

- `palace_graph` tunnel helper test coverage (#908)

---

## [3.3.0] — 2026-04-13

### New Features
- Closet layer — a compact searchable index of pointers to verbatim drawers, enabling fast topical lookup without reading all content (#788)
- BM25 hybrid search — closets boost ranking, drawers remain the source of truth (#795, #829)
- Entity metadata on every drawer for filterable search (#829)
- Diary ingest — day-based rooms for conversation transcripts (#829)
- Cross-wing tunnels — explicit links between rooms in different wings for multi-project agents (#829)
- Drawer-grep — returns the best-matching chunk plus adjacent context drawers (#829)
- Offline fact checker against the entity registry and knowledge graph (#829)
- LLM-based closet regeneration — optional, bring-your-own endpoint, no mandatory API key (#793)
- Hall detection — routes drawer content to `emotions` / `technical` / `family` / `memory` / `identity` / `consciousness` / `creative` halls, enabling hall-based graph connectivity within wings (#835)

### Bug Fixes
- Set `hnsw:space=cosine` metadata on all collection creation sites — fixes broken similarity scoring under ChromaDB's default L2 distance (#807, #218)
- File-level locking prevents duplicate drawers when agents mine the same file concurrently (#784, #826)
- Hybrid closet+drawer retrieval — closets boost ranking, never gate results (#795)
- Stop hooks from making agents write in chat — saves tokens on every turn (#786)
- Strip system tags, hook output, and Claude UI chrome from drawers before filing (#785)
- Verbatim-safe `strip_noise` scoped to Claude Code JSONL only (#785)
- Prevent diary entry ID collisions via microsecond timestamp and full content hash (#819)
- Auto-rebuild stale drawers via `NORMALIZE_VERSION` schema gate
- Enforce atomic topics in closets and extract richer pointers
- Sync `version.py` to match `pyproject.toml` (#820)
- Remove unused `main` import from `mempalace/__init__.py` (#827)
- README audit — fix 7 stale claims (tool count, version badge, wake-up token cost, `dialect.py` lossless disclaimer, `pyproject.toml` version) with 42 regression-guard tests (#835)

### Improvements
- Optimize entity detection with regex caching and pre-compilation (#828)
- Extract locked filing block into helper to keep `mine_convos` under C901 complexity

### Documentation
- Add `docs/CLOSETS.md` — closet layer overview
- Fix stale `milla-jovovich/*` org URLs in website and plugin manifests (#787)
- Fix remaining stale org URLs in contributor docs (#808)
- Rewrite `README.md` and `mempalaceofficial.com` benchmark pages to remove category-error cross-system comparisons (R@5 retrieval recall had been listed next to competitor QA accuracy under one column), remove the retracted "+34% palace boost" claim from the surfaces where it had remained, replace the `100%` Haiku-rerank headline with the honest held-out `98.4%` R@5, drop the LoCoMo `100%` top-50 row (retrieval-bypass artefact), and fix the broken `aya-thekeeper/mempal` reproduction URL (#875)
- Add `docs/HISTORY.md` as the canonical home for corrections, retractions, and public notices; move the 2026-04-07 "Note from Milla & Ben" and the 2026-04-11 impostor-domain notice out of `README.md`
- Add v3.3.0 reproduction result JSONLs and the deterministic `seed=42` 50/450 LongMemEval split under `benchmarks/` — every BENCHMARKS.md claim reproduces exactly

### Internal
- Add test coverage for `mine_lock`, closets, entity metadata, BM25, and diary
- Verify `mine_lock` via disjoint critical-section intervals
- Serialize `mine_lock` concurrency test with multiprocessing
- Make diary state path assertion platform-neutral
- Add `TestTunnels` coverage for cross-wing tunnel operations
- Ruff format with CI-pinned version (0.4.x); format `mempalace/palace.py`

---

## [3.2.0] — 2026-04-12

### Packaging
- Remove `chromadb<0.7` upper bound — unblocks installs against chromadb 1.x palaces (#690)
- Bump version to 3.2.0 across `pyproject.toml`, `mempalace/version.py`, README badge, and OpenClaw SKILL (#761)

### Security
- Harden palace deletion, WAL redaction, and MCP search input handling (#739)
- Consistent input validation, argument whitelisting, concurrency safety, and WAL fixes (#647)
- Remove hardcoded credential paths from benchmark runners (#177)
- Remove global SSL verification bypass in convomem_bench (#176)

### Bug Fixes
- Parse Claude.ai privacy export with `messages` key and sender field (#685, #677)
- Detect mtime changes in `_get_client` to prevent stale HNSW index (#757)
- Hash full content in `tool_add_drawer` drawer ID — stable re-mines (#716)
- Remove 10k drawer cap from status display (#707, #603)
- Correct typo in entity_detector interactive classification prompt (#755)
- Prevent convo_miner from re-processing 0-chunk files on every run (#732, #654)
- Remove silent 8-line AI response truncation in convo_miner (#708, #692)
- Store full AI response in convo_miner exchange chunking (#695)
- Fix `mine --dry-run` TypeError on files with room=None (#687, #586)
- Skip arg whitelist for handlers accepting `**kwargs` (#684, #572)
- Allow Unicode in `sanitize_name()` — Latvian, CJK, Cyrillic (#683, #637)
- Auto-repair BLOB seq_ids from chromadb 0.6→1.5 migration (#664)
- Remove no-op `ORT_DISABLE_COREML` env var (#653, #397)
- Disambiguate hook block reasons to name MemPalace explicitly (#666)
- Use epsilon comparison for mtime to prevent unnecessary re-mining (#610)
- Correct token count estimate in compress summary (#609)
- Implement MCP ping health checks (#600)
- Align `cmd_compress` dict keys with `compression_stats()` return values (#569)
- Skip unreachable reparse points in `detect_rooms_from_folders` on Windows (#558)
- Prevent HNSW index bloat from duplicate `add()` calls (#544, #525)
- Purge stale drawers before re-mine to avoid hnswlib segfault (#544)
- Mitigate system prompt contamination in search queries (#385, #333)
- Count Codex `user_message` turns in `_count_human_messages` (#373, #347)
- Paginate large collection reads and surface errors in MCP tools (#371, #339, #338)
- Expand `~` in split command directory argument (#361)
- Ignore `wait_for_previous` argument to support Gemini MCP clients (#322)
- Close KnowledgeGraph SQLite connections in test fixtures (#450)
- Remove duplicate cache variable declarations in mcp_server.py (#449)
- Add `--yes` flag to init instructions for non-interactive use (#682, #534)
- Add `mcp` command with setup guidance (#315)

### New Features
- i18n support — 8 languages (en, es, fr, de, ja, ko, zh-CN, zh-TW) (#718)
- New MCP tools: get/list/update drawer, hook settings, export (#667, #635)
- `mempalace migrate` — recover palaces from different ChromaDB versions (#502)
- Add OpenClaw/ClawHub skill (#491)
- Backend seam for pluggable storage backends (#413)

### Improvements
- Disable broken auto-bump workflow (#414)
- Improve agent readiness — AGENTS.md, dependabot, CODEOWNERS, labels (#497)

### Documentation
- Add CLAUDE.md and mission/principles to AGENTS.md (#720)
- Add VitePress documentation site (#439)
- Add warning about fake MemPalace websites (#598)
- Fix stale org URLs and PR branch target in contributor docs (#679)
- Fix misaligned architecture diagram (#734, #733)
- Add ROADMAP.md — v3.1.1 stability patch and v4.0.0-alpha plan

### Internal
- ruff format convo_miner.py (#741)
- ruff format all Python files (#675)
- CI: trigger tests on develop branch PRs and pushes (#674)
- CI: fix GitHub Pages publishing (#691)

---

## [3.1.0] — 2026-04-09

### Security
- Harden inputs, fix shell injection, optimize DB access (#387)
- Sanitize SESSION_ID in save hook to prevent path traversal (#141)
- Sanitize error responses and remove `sys.exit` from library code (#139)
- Shell injection fix in hooks, Claude Code mining, chromadb pin (#114)

### Bug Fixes
- MCP null args hang, repair infinite recursion, OOM on large files (#399)
- Release ChromaDB handles before rmtree on Windows (#392)
- Use `os.utime` in mtime test for Windows compatibility (#392)
- Negotiate MCP protocol version instead of hardcoding (#324)
- Use upsert and deterministic IDs to prevent data stagnation (#140)
- Make `drawer_id` deterministic for idempotent writes (#387)
- Honest AAAK stats — word-based token estimator, lossy labels (#147)
- Room detection checks keywords against folder paths (#145)
- Use actual detected room in mine summary stats (#165)
- Honour `--palace` flag in mcp_server (#264)
- Preserve default KG path when `--palace` not passed (#270)
- `--yes` flag skips all interactive prompts in init (#123)
- Repair command, split args, Claude export, room keywords (#119)
- Replace Unicode separator in convo_miner.py for Windows compatibility (#129)
- Coerce MCP integer arguments to native Python int (#84)
- Batch ChromaDB reads to avoid SQLite variable limit (#66)
- Respect nested .gitignore rules during mining (#78)
- Narrow bare `except Exception` to specific types where safe (#54)
- Mark MD5 as non-security in miner drawer ID generation (#53)
- Remove dead code and duplicate set items in entity_registry.py (#42)
- Silence ChromaDB telemetry warnings and CoreML segfault on Apple Silicon (#236)
- Unify package and MCP version reporting (#16)
- Fix broken AAAK Dialect link in README (#238)
- Update input prompt for entity confirmation (#83)
- Preserve CLI exit codes, log tracebacks, sanitize search errors (#139)
- Enable SQLite WAL mode and add consistent LIMIT to KG timeline (#136)
- Add limit=10000 safety cap to all unbounded ChromaDB `.get()` calls (#137)
- Re-mine modified files, idempotent `add_drawer`, cleanup ChromaDB handles (#140)
- Resolve formatting, regression logic, and pytest defaults (#270)
- Use `parse_known_args` to allow importing mcp_server during pytest (#270)

### New Features
- Package MemPalace as standard Claude and Codex plugins (#270)
- Add OpenAI Codex CLI JSONL normalizer (#61)
- Add Codex plugin support with hooks, commands, and documentation (#270)
- Add command documentation for help, init, mine, search, and status (#270)

### Improvements
- Cache ChromaDB `PersistentClient` instead of re-instantiating per call (#135)
- Tighten chromadb version range and add `py.typed` marker (#142)
- Consolidate split known-names config loading (#22)
- CI: add separate jobs for Windows and macOS testing
- CI: Upgrade GitHub Actions for Node 24 compatibility (#55)

### Documentation
- Add Gemini CLI setup guide and integration section (#106)
- Add beginner-friendly hooks tutorial (#103)
- Align MCP setup examples with shipped server (#21)
- Honest README update — own the mistakes, fix the claims

### Internal
- Expand test coverage from 20 to 92 tests, migrate to uv (#131)
- Add scale benchmark suite — 106 tests (#223)
- Increase test coverage from 30% to 85%, fix Windows encoding bugs (#281)
- Add WAL mode and entity timeline limit assertions
- Add coverage for `file_already_mined` mtime check

---

## [3.0.0] — 2026-04-06

Initial public release.

- Palace architecture with day-based rooms, drawers (verbatim), and closets (searchable index)
- AAAK compression dialect for memory folding
- Knowledge graph with entity detection and timeline queries
- MCP server for Claude, Codex, and Gemini integration
- CLI: `init`, `mine`, `search`, `status`, `compress`, `repair`, `split`
- Benchmark suite with recall and scale tests
- README with MCP flow, local model flow, and specialist agent documentation

---

[Unreleased]: https://github.com/MemPalace/mempalace/compare/v3.2.0...HEAD
[3.2.0]: https://github.com/MemPalace/mempalace/compare/v3.1.0...v3.2.0
[3.1.0]: https://github.com/MemPalace/mempalace/compare/v3.0.0...v3.1.0
[3.0.0]: https://github.com/MemPalace/mempalace/releases/tag/v3.0.0
