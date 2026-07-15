# Managed JSONL sweeper write contract

Date: 2026-07-15
Owner: [mempalace #22](https://github.com/iMelki/mempalace/issues/22)
Status: implemented on local `dev`; focused and expanded receipt validation green
Mutation scope: disposable test palaces only; no configured or live palace was opened

## Plain-English Summary

The JSONL sweeper is no longer a sequence of direct Chroma upserts coordinated
only by a timestamp cursor. One physical JSONL file now owns one isolated,
receipt-managed sweeper lane. Every accepted source version either reuses the
exact represented message set or replaces that complete set under a durable
pre-purge snapshot, exact readback, and rollback.

The sweeper lane deliberately does not claim the physical `source_file` used by
the primary project/conversation miners. Their chunked rows and the sweeper's
message rows can coexist without either writer purging or claiming the other's
provenance.

## Source And Output Identity

- Exact source content is SHA-256 over every JSONL byte.
- The source locator is `mempalace://sweeper/jsonl/<path-hash>/<safe-name>`.
  The full local path does not enter the receipt locator, while
  `origin_source_file` remains available on local row metadata. Windows path
  case is normalized in both fields, so case aliases do not create a second
  semantic source for the same NTFS file.
- Managed message IDs use
  `sweep_<source-token>_<session>_<uuid>`. The source token prevents a copied
  or renamed file from colliding with the retained lane at its old path. The
  established `sweep_<session>_<uuid>` shape is now detection-only for legacy
  unmanaged rows.
- Output identity includes the exact role-prefixed document.
- `sweeper_semantic_metadata_hash` additionally binds the ID, document,
  session, UUID, timestamp, role, origin path, ingest mode, adapter version,
  and optional source label. Volatile receipt stamps and `filed_at` are not
  semantic inputs.
- The run configuration binds the adapter/transform versions, complete-lane
  materialization contract, optional source label, and explicit zero-output
  approval state.

An unchanged retry is accepted only when the full current ID set and every
source-derived semantic hash match. Generic receipt verification must also
report `represented`. A tampered non-first row therefore triggers repair; a
first-row metadata check is not treated as whole-source proof.

## Transaction And Lock Order

The sweeper acquires the managed per-palace scope before opening or creating a
Chroma collection, reading palace rows, or initializing receipt storage. It
then takes locks in this order:

1. per-palace managed write scope;
2. receipt source-URI lock;
3. normalized physical JSONL path lock.

The physical lock remains held while the source is hashed, scanned, written,
and re-hashed. The final digest, byte count, and message-ID set must still match
the pre-write scan. A detected change raises before terminal completion, and
the common rewrite journal restores the exact predecessor documents, metadata,
and embeddings.

Every source, semantic-row, and pre-commit managed-write check runs before
terminal success. After `COMPLETE` is durably published, the generic managed
driver reloads that event from its journal path and runs exact receipt
verification while the palace lock is still held. Verification covers current
source identity, every expected row, conflicts, excess rows, stale ownership,
and the authoritative receipt head. Recovery finalization then performs its
independent durable-COMPLETE and exact committed-state proof before deleting
the rollback snapshot.

The generic driver retains its default exception behavior for existing diary
and MCP callers. The sweeper explicitly selects the reporting policy because a
failure after `COMPLETE` cannot honestly be described as rolled back. A
terminal verification or recovery-finalization failure therefore returns
`committed=true`, `verification_status=committed-unverified`, and the exact
validation error. The CLI exits nonzero and states that mutation already
committed. A normal rewrite keeps unresolved recovery state for the next
reconciliation attempt; an unchanged rebind may already have passed recovery
finalization before its final generic receipt check, so it is still reported as
committed-unverified rather than being given a fictitious rollback claim.

Chroma and the receipt filesystem are not one native transaction. The durable
pre-purge snapshot, conditional exact purge, idempotent deterministic IDs, and
post-write verification are the application transaction.

## Input And Deletion Policy

- Non-message JSONL records remain valid noise and are ignored. Non-object
  values inside an accepted message's content-block list are serialized rather
  than silently discarded, preserving their JSON value alongside normal text
  and tool blocks.
- Invalid UTF-8, malformed JSON, a non-object record, or a `user`/`assistant`
  record missing its message, role, timestamp, UUID, session ID, or content
  fails before replacement. A corrupt or half-written line is not evidence
  that old messages vanished.
- A still-present source can remove individual messages through complete
  replacement.
- Replacing a non-empty managed source with zero messages requires the explicit
  `allow_zero_output=True` API option or CLI `--allow-zero-output` switch.
- A missing or renamed file is not automatic deletion authority. A renamed or
  copied file receives a disjoint managed lane, while the old lane remains
  visible until a reviewed cleanup/migration decision names it. This avoids
  foreign-ID failure without silently treating path absence as approval to
  delete old content.

## Legacy Boundary

Rows produced before receipt-managed sweeps are not silently adopted. Preflight
checks the old physical path, resolved path, optional source label, and every
legacy deterministic output ID. This first legacy check runs before receipt
storage is initialized, so refusal does not create a receipt root that could
interfere with legacy migration. The authoritative in-lock preflight repeats
the check. It also scans unmanaged sweep rows for conservative absolute,
relative-suffix, and basename path equivalence, which catches noise-only or
empty current files whose old message IDs can no longer be derived. A possible
match fails closed for migration review rather than creating a zero-output
receipt beside historical rows. An unmanaged `ingest_mode=sweep` row or foreign
managed-ID collision fails before a receipt starts or a palace row changes.

This is intentionally conservative. A future migration must inventory those
rows, prove their source/message identity, snapshot their vectors, rehearse
rollback, and publish honestly dated adoption receipts. This tranche does not
fabricate historical receipts.

## Operator Metrics

One file result reports:

- source byte count and tagged content hash;
- terminal-manifest expected, per-file verifier-confirmed, and represented counts, plus
  added, semantically updated, semantically unchanged, removed, rewritten,
  receipt-rebound, adapter-upserted, and total physical-mutation row counts. A
  committed-unverified result reports the expected count but conservatively
  reports zero represented; it never converts generated IDs into a verification
  claim. An unchanged source can still have receipt-metadata rebindings, so it
  is not mislabeled as zero physical work;
- prior per-session timestamp cursor as compatibility readback only;
- run ID, receipt ID, disposition, unchanged state, and verification status.

Directory mode keeps `drawers_verifier_confirmed` as the sum from individually
verified files. Its compatibility `drawers_represented` field is stricter: it
equals that sum only when every discovered file succeeded and verified;
otherwise it is zero because the directory run as a whole is not represented.
The CLI prints both the whole-run count and the partial per-file evidence.
Per-file receipt, count, verification, and failure details remain available.
`drawers_skipped` is a zero-valued compatibility field; the cursor no longer
decides completeness.

## Validation Contract

Required focused proofs:

- empty-palace materialization and exact receipt readback;
- unchanged idempotent replay;
- append and same-timestamp message growth;
- removal of stale messages;
- semantic metadata tamper repair;
- strict malformed-line refusal without mutation;
- invalid UTF-8 refusal before palace creation or replacement;
- preservation of mixed non-object message content blocks;
- explicit zero-output refusal and approved zero-output completion;
- CLI propagation of explicit zero-output approval;
- legacy unmanaged collision refusal before receipt-root creation, including a
  currently empty source and a historical relative path spelling;
- duplicate message identity refusal before mutation;
- immediate filtered and natural-language vector retrieval in the isolated
  source lane;
- renamed-source disjoint ID ownership with the retained old lane;
- palace-lock acquisition before Chroma or receipt side effects, including a
  busy brand-new palace;
- exact semantic readback before completion and managed recovery finalization
  while the per-palace lock remains held;
- durable terminal journal reload plus exact represented/missing/excess/
  conflict/stale verification while the lock remains held;
- injected post-COMPLETE verifier and recovery-finalization failures reported
  explicitly as committed-but-unverified without a misleading rollback claim;
- unchanged receipt rebind post-commit failure reporting after its recovery
  finalization has already completed;
- unchanged receipt rebind recovery-finalization failure reporting with the
  unresolved recovery record retained;
- expected-versus-verifier-confirmed metrics that never count an unverified
  terminal manifest as represented, including a mixed verified/unverified
  directory run and direct single-file CLI evidence;
- refusal of a concurrent second sweep without row or receipt-head mutation;
- source mutation during a multi-batch write with a full ID-joined lane
  comparison, no replacement survivors, and exact predecessor rollback;
- injected failure after the first of two write batches with exact predecessor
  document, metadata, vector, receipt-head, and recovery-queue restoration;
- ID-joined, rather than position-assumed, vector rollback comparison.

The focused sweeper/CLI proof passes 35 tests. The expanded sweeper, CLI,
managed-boundary, and receipt proof passes 204 tests in 100.85 seconds with no
stderr. The expanded proof includes the generic driver's default raise policy
as well as the sweeper's explicit committed-error report policy. All palaces
used by these proofs were disposable test directories. The final repository
gate passes 1,733 tests with 7 skips and 106 intentional deselections in 232.05
seconds with no stderr.

## Upstream And Community Evidence

- Chroma's official upsert contract creates missing IDs and updates existing
  IDs, but it is not a complete multi-call replacement transaction:
  https://docs.trychroma.com/reference/chroma-api/record/upsert-records
- Chroma `get` can return documents, metadata, and embeddings; result fields
  must be joined by ID rather than assumed request order:
  https://docs.trychroma.com/docs/querying-collections/query-and-get
- Chroma 1.5.9 tagged code applies one local log append in one SQLite
  transaction, but that does not span snapshot, multiple batches, purge, and
  verification:
  https://github.com/chroma-core/chroma/blob/1.5.9/rust/log/src/sqlite_log.rs
- Chroma's current system-constraints page explicitly says local Chroma is
  thread-safe but not process-safe for concurrent writers sharing one
  persistence path:
  https://cookbook.chromadb.dev/core/system_constraints/
- Chroma issue #7040 and PR #7041 were still open on 2026-07-15 and support
  conservative single-owner local sequencing; the proposed SQLite fix itself
  says HNSW multi-process safety remains out of scope:
  https://github.com/chroma-core/chroma/issues/7040
  https://github.com/chroma-core/chroma/pull/7041
- Chroma issue #7032 and PR #7043 were also still open on 2026-07-15 and show
  why immediate filtered-query/index consistency needs application readback
  rather than assumption:
  https://github.com/chroma-core/chroma/issues/7032
  https://github.com/chroma-core/chroma/pull/7043
- W3C PROV treats source entities, transforming activities, generated entities,
  revisions, and invalidations as distinct provenance facts:
  https://www.w3.org/TR/prov-dm/
- OpenLineage's run cycle separates START, RUNNING, COMPLETE, ABORT, and FAIL
  while naming input and output datasets:
  https://openlineage.io/docs/1.44.0/spec/run-cycle/

## Remaining Work

- Build and explicitly approve a legacy sweeper-row migration before claiming
  provenance for pre-receipt rows.
- Add a fresh-process forced-exit sweeper-specific rehearsal if issue #22 later
  requires path-specific proof beyond the common receipt restart probe.
- Decide whether source rename/copy cleanup gets an attended operator manifest
  or remains manual; the current safe behavior preserves both disjoint lanes.
- Keep compression-derived closets and LLM closet regeneration blocked from
  direct mutation until their whole-source successor design preserves current
  drawer/closet receipt validity.
