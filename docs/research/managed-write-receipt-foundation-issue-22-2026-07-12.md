# Managed-write receipt foundation for issue #22

Date: 2026-07-12; review remediation updated 2026-07-14
Status: implementation is committed on `dev` in `04f5bf3`; the final full-suite
release gate, independent re-review, and disposable real-Chroma process-restart
proof are GO; historical recovery remains NO-GO and was not performed

## Decision

MemPalace now models each managed source write as an append-only sequence of
`mempalace-source-write-receipt/v1` events. A receipt binds one pseudonymous
source identity and source version to one managed run and an exact,
content-bound output manifest. The five supported states are `START`,
`RUNNING`, `COMPLETE`, `ABORT`, and `FAIL`.

The foundation is intentionally narrower than issue #22 as a whole. It does
not ingest or recover the 18 historical sources, access a live palace, add a
hosted service, or claim provenance for writes that happened before receipts
existed.

## Research basis

### W3C PROV

[PROV-DM](https://www.w3.org/TR/prov-dm/) defines entities with fixed aspects,
activities that use or generate entities over time, and agents responsible for
activities. It also defines generation, derivation, revision, association, and
invalidation. The [PROV Primer](https://www.w3.org/TR/prov-primer/) is explicit
that each revision is a new entity, while specialization can connect versions
to a stable thing. [PROV-O](https://www.w3.org/TR/prov-o/) provides
`wasGeneratedBy`, `used`, `wasAssociatedWith`, `wasRevisionOf`,
`invalidatedAtTime`, and `wasInvalidatedBy`.

The receipt mapping is:

| Receipt concept | PROV interpretation |
|---|---|
| Stable HMAC source identity | Stable source entity |
| Content/version hashes | Fixed source-version entity |
| Managed receipt/run ID | Write activity |
| Caller and package identity | Associated agents/software agent |
| Drawer/sentinel identities | Generated output entities |
| `reuses_receipt_id` | Unchanged specialization observed by a later activity |
| `supersedes` | Revision/successor relation |
| Invalidation record | Prior output entity invalidation by the rewrite activity |

This is a PROV-aligned application model, not a PROV-N, PROV-JSON, or RDF
serialization.

### OpenLineage

The current [OpenLineage run cycle](https://openlineage.io/docs/spec/run-cycle/)
defines `START`, `RUNNING`, `COMPLETE`, `ABORT`, and `FAIL`; `COMPLETE`,
`ABORT`, and `FAIL` are terminal. Run updates are cumulative observations, and
metadata may become available as execution proceeds. Its
[object model](https://openlineage.io/docs/spec/object-model/) associates a
run with a job and input/output datasets. Its
[facet model](https://openlineage.io/docs/spec/facets/) separates what ran, how
it ran, what it used, and what it produced.

MemPalace adopts that lifecycle and cumulative-event behavior. It does not
emit OpenLineage wire events or claim OpenLineage schema compatibility.

## Managed write paths reviewed

### Project mining

`mempalace/miner.py` follows:

1. `scan_project()` discovers readable files.
2. `mine()` / `_mine_impl()` take the per-palace lock and create collections.
3. `process_file()` takes the canonical per-source lock before reading, then
   routes, chunks, and serializes one source without releasing that lock.
4. `_persist_file_chunks()` snapshots drawers and closets selected by canonical
   `source_file`, validated raw aliases, or receipt source identity; purges and
   rewrites both collections; and restores both snapshots on handled failure.

The source lock starts before exact source bytes are read. The receipt starts
after those locked bytes are readable and hashed, but before any source-row
mutation. Successful upsert batches are added to the manifest only after the
collection call returns and exact document/metadata readback stabilizes within
the bounded verification window. Embeddings are read separately only when the
caller supplied them, and those values must also match exactly.

Managed rewrites now treat both collection snapshots and purges as write
preconditions. Every drawer and closet write made by the managed project path
is validated, receipt-stamped, and included in the exact manifest. This is not
a statement about every MemPalace writer. A handled delete, upsert, or
COMPLETE-journal failure restores all successfully purged collections before
terminal `FAIL`. Legacy or direct unmanaged writes retain their previous
behavior and can invalidate receipt claims until they are adapted or retired.

A project source that changes to fewer than `MIN_CHUNK_SIZE` characters follows
the same locked rewrite protocol. It supersedes the latest prior COMPLETE
receipt, snapshots and purges stamped rows plus known canonical/raw legacy
aliases in both collections, and only then completes with an exact empty
manifest and `ZERO_OUTPUT` disposition. Its invalidation hook is queued in the
COMPLETE event and published to the redundant invalidation path afterward.

### Conversation mining

`mempalace/convo_miner.py` follows:

1. `scan_convos()` discovers candidate transcript files.
2. `mine_convos()` takes the per-palace cross-process lock and creates one
   managed run.
3. `_process_conversation_file()` takes the canonical per-source lock before it
   reads, hashes, normalizes, chunks, or writes, and owns the receipt lifecycle.
4. `_file_chunks_locked()` performs the source purge and bounded drawer
   upserts.
5. `_register_file()` writes the existing zero-output sentinel.

The managed path passes those already-hashed bytes into
`normalize(..., source_bytes=...)`, so normalization cannot silently consume a
different second read of a concurrently changing file.

An `OSError` or `ValueError` from normalization emits terminal `FAIL` and
returns without purge, invalidation, sentinel creation, or current-index
promotion. Any other normalization exception also emits `FAIL` before purge and
then propagates. A successful normalization that yields no semantic output
remains a separate `ZERO_OUTPUT` path.

The sentinel is an exact `kind=sentinel` manifest item. Counts therefore show
zero expected/written drawers and one written item.

Both normal conversation rewrites and zero-output sentinel rewrites now purge
inside the per-source lock before any new upsert. Delete exceptions emit
`FAIL`, propagate to the caller, leave the prior COMPLETE receipt as the source
index head, and cannot write a new chunk or sentinel. A successful zero-output
rewrite removes prior and receipt-less legacy rows, records
supersession/invalidation, then writes the single sentinel.

### RFC 002 source adapters

`mempalace/provenance.py::managed_adapter_ingest()` drives
`BaseSourceAdapter.ingest()` and requires each `SourceItemMetadata` to include
a tagged SHA-256 `content_hash` before any `DrawerRecord` can be persisted.
The per-palace HNSW safety lock is acquired before the canonical `SourceRef`
lock and before the adapter generator can read. Both locks remain held through
COMPLETE or rollback. Consequently, adapters for different source refs cannot
overlap writes against the same palace, while adapters targeting different
palaces remain independent. Both
`PalaceContext.drawer_collection` and `closet_collection` are receipt-aware
facades during managed ingest. Direct adapter `add`, `upsert`, or
document-bearing `update` calls are validated, stamped, and added to the active
manifest; mutation before a source identity and adapter-side delete both fail
closed. `upsert_drawer()` uses the same facade. Knowledge-graph operations fail
closed because V1 cannot attest graph mutations. Missing source content
identity fails closed before a write.

Incremental reuse requires both the adapter's `is_current()` decision and an
exact represented verifier result. Otherwise the managed driver purges rows for
that logical `source_file`, records supersession/invalidation when a prior
receipt exists, and re-extracts the item.

The adapter driver snapshots every exposed managed collection before purge.
Extraction, collection-write, or COMPLETE publication failure restores prior
rows, leaves the prior source index head current, and publishes no invalidation.

### Existing invalidation hooks

The source-specific purge hooks in project mining, conversation mining, and
managed adapter re-extraction are queued only after all replacement outputs
exist. COMPLETE embeds the path-redacted hook record, then the store publishes
the redundant immutable invalidation file. Readback reconciles missing hook
files from authoritative COMPLETE events, covering a crash between those two
publications. Failed or rolled-back attempts do not appear in invalidation
readback. Knowledge-graph invalidation, deduplication, repair deletion, diary
ingest, sweeper writes, and MCP drawer mutations are separate paths and were
not changed in this scope.

## V1 event shape

Each local event contains these required identity groups:

```json
{
  "schema": "mempalace-source-write-receipt/v1",
  "receipt_id": "uuid",
  "event_id": "uuid",
  "sequence": 2,
  "state": "COMPLETE",
  "run": {
    "id": "uuid",
    "caller": {"identity": "hmac-sha256:..."},
    "mode": "project"
  },
  "producer": {
    "package": {
      "name": "mempalace",
      "version": "3.3.4",
      "source_digest": "sha256:..."
    },
    "git": {"state": "available", "commit": "...", "dirty": true},
    "config": {"digest": "sha256:..."}
  },
  "source": {
    "identity": "hmac-sha256:...",
    "content_hash": "sha256:...",
    "version_hash": "sha256:...",
    "shared_content_identity": "hmac-sha256:...",
    "shared_version_identity": "hmac-sha256:...",
    "size_bytes": 1234,
    "size_bucket": "1024-2047",
    "adapter": {"name": "filesystem", "version": "2"}
  },
  "outputs": {
    "count": 2,
    "manifest_digest": "sha256:...",
    "identities": [
      {
        "collection": "drawers",
        "id": "drawer-id",
        "kind": "drawer",
        "content_hash": "sha256:...",
        "producer_receipt_id": "uuid"
      }
    ]
  },
  "counts": {},
  "errors": [],
  "relations": {}
}
```

Every event is cumulative. `START` proves that an identified activity began;
`RUNNING` records the expected work before collection mutation; a terminal
event contains the exact successful outputs known to the process. Errors retain
type, stage, a local exact message digest, and a per-palace HMAC message
identity used by the shared projection.

## Identity and privacy

- Raw source bytes are SHA-256 hashed before normalization or chunking.
- Built-in file miners use the content hash as the source version hash.
- RFC 002 adapters retain their source-side version token only as a SHA-256
  version hash and must separately provide a content hash.
- Source locators and caller labels are HMAC-SHA-256 pseudonyms under a random
  per-palace key. Raw values are never written to receipt events.
- Config values are canonicalized and hashed; only the digest is retained.
- Package identity includes version and a digest over runtime package source
  and resources. Git identity adds commit and dirty state when a checkout is
  available.
- Local exact receipts retain drawer IDs because verification needs them. IDs
  can expose wing/room taxonomy, so shared projections remove them.
- The shared projection is named `pseudonymized-shared`, not privacy-safe or
  anonymous. It replaces global source content/version and error-message hashes
  with domain-separated HMAC-SHA-256 identities under the per-palace key,
  replaces exact source bytes with a power-of-two size bucket, and removes
  `counts.source_bytes`.
- The projection still retains pseudonymous source/caller identity,
  producer/config identity, UUIDs, timestamps, output counts, terminal state,
  and manifest digest. Those fields can correlate activity and taxonomy, so the
  artifact is for bounded sharing with trusted reviewers, not public release.

This narrows disclosure from the shared projection. Local exact receipts retain
global hashes and exact size for verification. Chroma `source_file` metadata
and the conversation sentinel document retain a canonical local source path;
they are local operational state and are not part of the shared projection.

## Storage and durability

Default root:

```text
<palace>/.mempalace/write-receipts/v1/
  identity.key
  identity-key.json
  events/<source-hmac>/<run-id>/<receipt-id>/<sequence>-<state>.json
  sources/<source-hmac>.json
  invalidations/<invalidated-receipt-id>/<invalidation-id>.json
  recoveries/<source-hmac>/<receipt-id>.json
```

Receipt and invalidation event files are create-only inside `ReceiptStore`.
START, RUNNING, ABORT, FAIL, and invalidation writes use the ordinary
create-only event path. This does not make the containing filesystem immune to
an external delete or an older whole-palace rebuild. Legacy `mempalace migrate`
therefore fails closed when the managed receipt root exists until that rebuild
can preserve provenance. COMPLETE is
different: it carries `mempalace-durable-complete-publication/v1` and uses the
same fail-closed file/directory durability primitives as rewrite recovery.
Finalization and crash reconciliation re-flush and exact-read that marked
COMPLETE before recovery removal. Any uncertainty retains recovery. The
append-only event journal and explicit DAG determine the current COMPLETE; the
mutable source index is a checked, reconstructable accelerator, not an
independent timestamp authority.
`find_current()` validates that an index entry and target event identify the
requested source. It follows explicit predecessor/supersession/reuse links and
repairs the index only when the journal proves one unambiguous successor. It
never chooses the greatest timestamp. A mismatched index, conflicting COMPLETE,
fork, cycle, or ambiguous missing-index lineage raises instead of guessing. A
legacy global scan remains available for pre-partition COMPLETE events. A
successful COMPLETE journal append therefore remains discoverable if the
subsequent mutable-index refresh fails, provided its successor relation is
unambiguous.

The identity key must be exactly 32 bytes. Once receipt state exists, a missing
key fails closed rather than silently rotating source identities. A local
`identity-key.json` fingerprint detects accidental replacement; legacy stores
with an existing key backfill that metadata without rotating the key. This is
continuity detection, not a defense against an actor able to replace both key
and metadata.

The implementation requests owner-only POSIX mode bits for the journal and
HMAC key. Windows does not enforce Unix mode bits as an ACL boundary, so the
Windows journal inherits the palace directory's NTFS permissions. Treat the
local journal and `identity.key` as restricted operational state; do not call
the artifact access-controlled until explicit Windows DACL enforcement and
readback are implemented and independently proven. That remains open follow-up,
not a property established by this work.

There is no atomic transaction spanning Chroma and the receipt filesystem.
Instead, every managed rewrite now publishes a create-only recovery record
before the first purge. That record contains the exact pre-purge IDs,
documents, metadata, optional embeddings, per-collection digests, source HMAC,
attempt receipt ID, authoritative predecessor receipt ID, and private exact
`source_file` selector coverage. The selector coverage is required even when
the baseline contains zero rows, so a late receiptless legacy row cannot escape
commit or rollback reconciliation. These raw selectors stay only in the local
recovery file and never enter the shared projection.

Recovery and COMPLETE publication have a stricter durability path than
ordinary non-COMPLETE lifecycle events:

- The canonical JSON is written to a same-directory temporary file, flushed,
  and `fsync`ed before publication.
- On Windows, every journal directory in the first-use recovery path receives a
  retained marker published with `MoveFileExW(MOVEFILE_WRITE_THROUGH)`, followed
  by target `FlushFileBuffers` and byte/hash readback. The recovery file uses the
  same write-through move and reopened-file verification. Windows has no POSIX
  directory-`fsync` equivalent in the documented user-mode API, so this is the
  strongest file/write-through path evidence implemented here, not a claim that
  Windows exposes a general directory flush guarantee. Microsoft documents the
  write-through move and file-flush primitives here:
  [MoveFileExW](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-movefileexw),
  [FlushFileBuffers](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-flushfilebuffers).
- On POSIX, every directory entry newly needed from `.mempalace` through the
  per-source recovery directory is created one level at a time and both that
  directory and its parent are `fsync`ed. Create-only link or atomic replacement
  is then followed by reopening and `fsync`ing the target, `fsync`ing the
  containing directory, and exact length plus SHA-256 validation. POSIX requires
  `fsync` to wait for synchronized I/O completion or report an error:
  [POSIX fsync](https://pubs.opengroup.org/onlinepubs/009695399/functions/fsync.html).
- Any move, link, replace, file flush, directory sync, reopen, size, or hash
  failure raises `ReceiptDurabilityError`. The managed path cannot proceed to
  purge without a verified `DurablePublicationProof` for the expected path.
- The 2026-07-14 promotion matrix exposed a cross-platform test defect at this
  boundary. The test replaced every POSIX directory sync, so Linux and macOS
  failed during the pre-publication directory proof instead of the claimed
  post-publication sync. It also expected success despite the fail-closed
  contract above. Publication now has an explicit test seam for the final
  parent sync; the regression injects the error only there and requires
  `ReceiptDurabilityError` while the session remains non-COMPLETE. This follows
  the same temp-file/file-sync/rename/parent-sync sequence documented by
  [python-atomicwrites](https://python-atomicwrites.readthedocs.io/en/latest/)
  and the POSIX rationale for syncing affected directories after rename or
  link operations.
- Recovery removal uses POSIX directory `fsync`. On Windows, a fresh
  same-directory barrier is published with `MOVEFILE_WRITE_THROUGH` and exact
  file readback after unlink. That is the strongest implemented ordering
  evidence, not a Windows directory-`fsync` claim. If the barrier fails, core
  attempts to durably republish recovery and reports failure.

Each collection purge now requires a private capability that is created only by
loading the durable recovery record and matching its source, receipt,
collection, selectors, snapshot digest, exact row contents, live collection
object, and the unforgeable nonce of the currently held palace-write scope.
Callers cannot pass a hand-built ID list or use the old broad purge helper.
Immediately before each delete, the row is read again and must still match the
capability. Chroma receives the exact ID together with ownership metadata,
content-hash metadata when present, and an anchored escaped document regex.
The row is then read back as absent. Added, removed, replaced, empty/invalid, or
duplicate pagination results fail closed. A concurrent row that does not match
the validated content and ownership predicate survives; the recovery record is
retained and that source remains blocked for reconciliation. This bounds the
destructive race without pretending Chroma and the receipt filesystem share a
transaction.

Chroma does not expose an embedding predicate for delete. Embedding-only races
are therefore prevented among supported managed writers by holding one
exclusive cross-process palace lock from source read through validation,
delete, replacement, durable COMPLETE, and recovery cleanup. A capability
cannot be consumed after that scope is released. Empty documents are accepted
only when content-hash metadata supplies the exact content predicate; a legacy
empty row without it fails closed. Code that deliberately reaches a private
raw handle or writes Chroma without the palace lock is outside this trusted
in-process adapter boundary. This is serialization, not backend atomicity.

Reconciliation runs under the applicable palace/source write boundary at miner
startup and before another write for that source:

- If the attempt has a matching COMPLETE event, `find_current()` must also prove
  that exact COMPLETE is the unique authoritative DAG head before the recovery
  record can be removed. Its durable publication is re-proven and every source
  selector must resolve exactly to its COMPLETE output IDs/content/ownership.
  A disconnected, forked, cyclic, superseded, or incompletely represented
  COMPLETE does not authorize cleanup.
- Without COMPLETE, reconciliation requires the atomic current index to still
  match the recorded predecessor. It removes partial replacement rows, restores
  the exact snapshots, verifies documents, metadata, embeddings, and absence of
  extra source-stamped rows, then removes the recovery record.
- Corrupt recovery data, an unavailable collection, or a changed baseline fails
  closed before restoration. The recovery record remains, and `begin_source()`
  refuses a new attempt until reconciliation succeeds.
- Before deleting anything, every surviving same-source row must be either an
  exact baseline row or stamped by the interrupted receipt. Reconciliation
  stores that exact validated interrupted-row set and deletes only it; it never
  runs a second source-wide query to construct a broader deletion set. A row
  arriving after validation survives and causes final verification to retain
  the recovery record.
- The current COMPLETE resolver evaluates every candidate lineage even when an
  atomic index exists. A disconnected root, fork, cycle, or otherwise ambiguous
  COMPLETE branch fails closed instead of being hidden behind the indexed path.
  A declared predecessor absent from the candidate journal also fails closed;
  legacy truncation is accepted only with the explicit
  `mempalace-explicit-legacy-missing-predecessor/v1` compatibility relation.

A second interruption during reconciliation is retryable because the immutable
recovery record is deleted only after exact verification. These records
temporarily duplicate raw drawer/closet content inside the palace and therefore
inherit the palace's confidentiality boundary; they must never be copied into a
shared receipt projection.

Managed `add`, `upsert`, and `update` calls run only inside that palace-write
scope and inspect every requested existing ID before mutation. An ID with
another source HMAC, another canonical `source_file`, or unverifiable ownership
is rejected before the collection write. `upsert` is split into `update` for
verified same-source existing IDs and duplicate-rejecting `add` for absent IDs,
so an insertion race cannot be unconditionally overwritten. Every write is
exact-read back before receipt output is recorded. In-process rollback deletes
only exact interrupted-attempt rows, creates only missing baseline IDs, refuses
to overwrite a concurrent survivor, and performs a second exact readback across
every recovery collection before the recovery file can be discarded.

## Idempotence, supersession, and invalidation

An unchanged rerun is a no-op only when all of these match:

- source HMAC identity
- source content and version hashes
- output-affecting config digest
- exact current output representation verified from Chroma

The later COMPLETE receipt preserves the content-bound output identities,
rebinds every current row and manifest entry to the new terminal
`producer_receipt_id`, and records the prior terminal receipt in
`reuses_receipt_id`. The rebind changes receipt metadata only; content and
embeddings are preserved.

Changed bytes create a new source version. A rewrite records the previous
receipt in `relations.supersedes`; after all collection writes succeed it queues
an invalidation record for the prior output manifest. COMPLETE is published
first, then the reconstructable invalidation hook and mutable source index.

This also applies when the new representation has zero semantic drawers. The
empty project manifest or conversation sentinel is authoritative only after
all old source rows have been deleted. Shrinking chunk counts use the same
purge-first path, so deterministic IDs from now-absent higher chunk indexes
cannot survive a COMPLETE rewrite.

On a managed purge error, no replacement-output upsert is attempted. Core
re-upserts the exact pre-purge documents, metadata, and embeddings so a backend
that deleted partially before raising cannot lose prior rows. Rollback purges
retry once before restoring that snapshot. The attempt ends in `FAIL`, the
previous COMPLETE source index entry remains current, and no invalidation hook
is written.
Legacy unmanaged miners preserve their prior best-effort delete behavior.

Existing Chroma rows without a trusted matching receipt are rewritten only when
their source enters one of the managed mine paths and ownership checks can
prove that the rows belong to that source. Foreign or contradictory HMAC/path
stamps fail closed rather than being adopted. This is intentional: observing
old rows cannot be upgraded into write-time provenance. No historical rewrite
was run as part of this implementation.

## Verifier

`mempalace/receipt_verifier.py::verify_receipt()` is read-only and reports all
five outcome families:

| Outcome | Meaning |
|---|---|
| `represented` | ID exists and content plus receipt/source metadata match |
| `missing` | Expected manifest ID is absent |
| `excess` | Same source/version has a current stamped ID outside the manifest |
| `conflict` | Expected ID exists with different content or provenance identity |
| `stale` | Source hash changed, a newer version supersedes it, or it was invalidated |

The aggregate status uses `conflict`, `missing`, `excess`, `stale`, then
`represented` precedence, while preserving every individual count. The
verifier rejects shared projections, malformed hashes, absent run/package/git/
config/source identity, non-COMPLETE events, duplicate identities, and manifest
digest mismatches. COMPLETE also requires its durable publication marker.
Verifier current-head resolution scans and validates the authoritative lineage
without repairing or creating the mutable source index. Missing identity or
publication evidence can never yield `represented`.

## Verification evidence

Focused commands run after independent NO-GO remediation:

```powershell
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests/test_write_receipts.py
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests/test_mcp_server.py
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests/test_mcp_http.py
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
```

Current result for this remediation pass: the affected receipt, backend, HTTP,
transport-dispatch, and MCP-server suite passes `279` tests with one platform
skip. The final repository-wide release gate passes `1,673` tests with `7`
skips, `106` intentional deselections, and `191` warnings in `136.68s` of pytest
time (`139.526s` wall time); its durable run record is
`%LOCALAPPDATA%\MemSys\eval-artifacts\mempalace-finish-line\full-suite-final-latest-run.json`.
All exercised stores are disposable temporary fixtures; no live/private palace
data was mutated. The installed Chroma client is 1.5.7. Chroma delete accepts IDs,
metadata filters, and document filters together but does not expose an
embedding predicate. Exact post-write document/metadata verification therefore
retries stale or temporarily missing rows for at most two seconds. Filesystem
mtimes are rounded to Chroma's six fractional digits before write so permanent
representation differences are not mistaken for visibility lag. Embeddings
are fetched separately and must also match exactly. When Chroma 1.5.x has not
made an exact vector visible through its supported collection API, the managed
readback loop retries for the same bounded window and then fails closed. It does
not open the live Chroma SQLite/WAL through Python's separate `sqlite3` driver
or return a receipt based on an unverified operation-log row. The predecessor
snapshot remains the rollback authority.
Independent receipt re-review found no remaining implementation blocker.
Disposable crash/restart proof is green. NTFS DACL proof, unmanaged-writer
disposition, a reviewed 18-source plan, and live cutover remain pending.
Historical recovery remains NO-GO until its separate backup/restore, expected-
output manifest, and operator-approval gates are complete.

The focused receipt tests cover:

- zero-output conversation sentinel
- changed project source to zero output, including legacy drawer and closet cleanup
- changed conversation source to zero output before sentinel replacement
- multi-chunk exact manifests
- unchanged rerun with exact output reuse and metadata-only producer rebind
- changed source version, supersession, and invalidation
- project and conversation rewrites with fewer chunks and no stale higher IDs
- project and conversation purge failures with no replacement-output upsert and terminal FAIL
- partial-delete failure after backend mutation with exact prior-snapshot restoration
- partial rollback-delete failure retaining recovery, followed by exact retry
  and embedding restoration
- zero-output sentinel purge failure with prior-snapshot restoration and no sentinel upsert
- normalization `OSError`/`ValueError` terminal FAIL with no purge or invalidation
- managed source-adapter purge failure with prior-snapshot restoration and no new drawer
- direct managed adapter collection writes stamped into the exact manifest and
  pre-identity mutation rejected
- unmanaged project/conversation best-effort purge compatibility
- handled interruption (`ABORT`) and write error (`FAIL`)
- represented, missing, excess, conflict, and stale verifier outcomes
- paginated verification across a manifest larger than one 1,000-ID batch
- fail-closed missing identity and shared-projection verification
- pseudonymized shared projection with per-palace HMAC content/version/error
  identities and bucketed source size
- canonical local-path persistence plus alias-row shrink cleanup
- lock-before-read concurrent source ordering and per-palace conversation lock
- same-palace cross-source adapter serialization
- authoritative-index behavior under a reversed wall clock, unambiguous
  post-COMPLETE index repair, fail-closed mismatched-index detection, and
  rejection of indexed-but-disconnected COMPLETE branches
- durable recovery before purge, after partial replacement, and after COMPLETE
  but before cleanup, corrupt-recovery no-mutation proof, real local durable
  publication readback, injected publication/flush failures, and direct-writer
  addition/removal/duplicate-ID pre-purge rejection
- exact conditional deletion under a true between-validation-and-delete
  replacement, including a replacement containing the full old document
- exact metadata-bound deletion of a receipt-stamped `393,216`-byte row through
  real Chroma without compiling the full document as a regex, plus legacy,
  stale-hash, empty-row, and replacement-race fail-closed coverage
- crash reconciliation deleting only its validated interrupted-row set, with a
  later row surviving and keeping recovery open
- authoritative-DAG cleanup refusal, Windows first-use directory markers,
  durable COMPLETE re-proof, empty-baseline selector coverage, missing-
  predecessor refusal, forged/escaped-capability rejection, exact rollback
  readback, and cross-source ID ownership rejection before mutation
- contradictory source-file/foreign-HMAC rejection before snapshot, managed
  empty-document deletion, embedding-race lock exclusion, and duplicate-safe
  absent-ID upsert race handling
- identity-selected foreign source-file rejection, exact embedding-aware
  existing-ID and final-delete rechecks, and shared managed/MCP palace locking
- strictly read-only verifier lineage resolution, missing durable COMPLETE
  marker rejection, and removal of the raw adapter collection escape
- real-Chroma document/metadata readback without unconditional embedding fetch,
  supported-API-only exact embedding readback, bounded transient-visibility
  stabilization, and permanent-mismatch refusal
- managed RFC 002 adapter writes and unchanged reuse
- receipt-aware project/adapter closets and graph fail-closed behavior
- drawer/closet rollback after write, extraction, and COMPLETE failures
- exact key length, missing-key refusal, fingerprint continuity, and legacy
  fingerprint backfill
- create-only immutable event conflict handling and legacy event-layout fallback
- atomic event paths with no surviving temporary files
- fail-closed COMPLETE publication and re-proof under simulated sync failure

## Integration limits and follow-up surfaces

1. Receipts are per source item, not one aggregate receipt for an empty source
   directory or entire multi-file run.
2. Managed project and adapter closets are included in the V1 output manifest
   and verifier. Unmanaged closet-producing paths remain outside this boundary.
3. The V1 inventory is intentionally explicit. `migrate.py`, `repair.py`,
   `dedup.py`, `sweeper.py`, compression, diary ingestion, closet regeneration,
   topic-tunnel writes, knowledge-graph writes, MCP drawer mutations, project
   sidecars, backend open-time repair/configuration, direct `ChromaBackend` or
   `palace.get_collection()` use, and unmanaged `PalaceContext` calls do not
   emit V1 receipts. MCP palace mutations now share the process/cross-process
   palace lock with managed rewrites and perform exact process-local row
   rechecks; this is serialization, not provenance or a Chroma transaction.
   `mempalace migrate` now blocks non-dry rebuilding when a receipt root exists,
   but receipt-aware migration and invalidation from the other mutation paths
   remain open work in #22. Their reviewed dispositions are frozen in
   `managed-write-boundary-dispositions-2026-07-14.json`: 10 adapt to managed
   receipts, six retire only their unmanaged mutation surface, and five use a
   named separate contract because they are not source-derived drawer state.
4. Source adapters must use `managed_adapter_ingest()`. During that driver,
   both public PalaceContext collections are receipt-aware; mutation without an
   active source receipt fails closed, and graph operations are rejected until
   a receipt representation exists. Adapters are trusted in-process code, not a
   sandbox. The former `_receipt_collections()` raw-handle accessor is removed;
   core keeps raw collection and graph capabilities in closure-owned weak
   registries and exports only narrow receipt-aware operations. There is no
   importable authority token, module-global raw registry, or function that
   returns a backend handle. Adapter-facing proxy objects expose no raw handle
   in their attributes or `__dict__`. Deliberate process introspection or a
   separately constructed Chroma client can still bypass this trusted
   in-process boundary; such writes are not protected by managed receipts.
5. The verifier is a Python API foundation; no CLI command or MCP transport
   surface was added in this scope.
6. Chroma and the receipt filesystem are not one transaction. Hard-process
   interruption is covered by durable pre-purge snapshots and automatic
   fail-closed reconciliation, and the disposable real-Chroma restart proof is
   green. An operator-facing inspection/repair command and the separately
   reviewed historical-recovery gates remain before any live cohort mutation.
   Host-restart and power-loss protection is as strong as the documented OS
   primitive and the underlying filesystem/storage stack actually honor. Drive
   write-back caches without power-loss protection, network filesystems, VM
   layers, firmware faults, and hardware that falsely acknowledges flushes can
   still violate persistence. Only a destructive platform-specific power-cut
   rehearsal can characterize that final hardware boundary.
7. No raw-path restricted mapping was necessary for verification because exact
   output IDs and stamped HMAC source identity are sufficient. Existing Chroma
   metadata remains the local operational path mapping.
8. No Railway, live MemPalace, historical recovery, or 18-source cohort action
   occurred.
9. An unreadable source cannot provide the required content hash, so the
   implementation does not fabricate a source receipt for it. Aggregate
   discovery/run receipts remain a future layer above these per-source events.
10. Windows DACL enforcement and readback for the journal/key are unproven and
    remain explicit follow-up. POSIX mode requests do not establish that
    boundary on NTFS.
11. Canonical paths, aliases supplied by the current ingest, and stamped source
    identities are purged. Arbitrary historical receiptless aliases that are not
   derivable from those values require an explicit bounded migration inventory;
   core does not perform a collision-prone palace-wide guess.

## Chroma visibility research update (2026-07-14)

The installed `chromadb==1.5.7` local client uses `RustBindingsAPI`; that API
exposes collection `get()` but no supported Python operation-log consumer. The
newer upstream 1.5.8 report
[`chroma-core/chroma#7032`](https://github.com/chroma-core/chroma/issues/7032)
independently reproduces temporary metadata-versus-HNSW divergence after bulk
writes on Windows. Upstream issue
[`#7040`](https://github.com/chroma-core/chroma/issues/7040) also documents
severe shared-persist-directory lock behavior when another local client opens
the same database. The current 1.5.9 release notes do not claim either issue is
fixed. Those findings support one conservative boundary: retry the owning
Chroma collection API only for the typed/recognized uninitialized-vector-view
conditions, then fail closed. Closed-client, schema, and unrelated backend
errors propagate immediately. They do not support attaching a second SQLite
reader or constructing a second client against a live palace.

Upstream [`#6975`](https://github.com/chroma-core/chroma/issues/6975) and
[`#7047`](https://github.com/chroma-core/chroma/pull/7047) document the remaining
durability gap: below-threshold in-memory HNSW state has no public flush API.
MemPalace configures `hnsw:sync_threshold=50000`. The first disposable probe on
2026-07-14 incorrectly used exact Python-float equality and produced a preserved
false-negative artifact. The corrected probe used `1e-6` float32 tolerance:
three IDs and all three vectors survived final-client close/reopen, cleanup was
clean, and the wrapper exited `0` in `1.68s`. Evidence:
`%LOCALAPPDATA%\MemSys\eval-artifacts\mempalace-finish-line\chroma-below-threshold-close-reopen-20260714T041251Z-run.json`.
That case is now an automated real-Chroma regression, and MemPalace cache
shutdown paths call the public client `close()` method. Automatic cache
replacement does not yet close the displaced client: a full-suite experiment
proved that doing so invalidates collection handles already returned to callers
(`RustBindingsAPI` loses `bindings`). Handle-aware retirement is therefore
tracked separately rather than hidden behind unsafe eager shutdown. Historical
recovery is still not authorized: a durable interrupted managed rewrite must be
restored and reverified after a true client/process restart before the recovery
cohort can run.

### Disposable hard-exit and process-restart proof (2026-07-14)

The process-restart gate is now green. The reusable command is:

```powershell
python -m mempalace.receipt_restart_probe --json
```

The normal probe command accepts no palace path. It creates one synthetic
Chroma database in a unique temporary directory; internal child phases require
the orchestrator's per-run marker and nonce, and the seed phase refuses a
pre-existing palace directory. It then runs four non-overlapping child
processes:

1. `seed` writes one managed source receipt, exact document, metadata, and an
   explicit unit vector, verifies representation, and cleanly closes Chroma.
2. `interrupt` reopens the database, verifies the baseline, durably publishes
   the exact pre-purge recovery snapshot, purges it, writes one partial
   replacement, reads that state back, and calls `os._exit(73)` without closing
   Chroma or publishing a terminal receipt.
3. `recover` opens a fresh client, requires the old receipt to remain the
   authoritative source head, removes only the validated interrupted row,
   restores the snapshotted document, metadata, and embedding, and removes the
   recovery record only after exact readback.
4. `verify` opens another fresh client, verifies the authoritative receipt,
   exact vector, top-1 vector query, absence of the partial row and recovery
   manifests, and `PRAGMA integrity_check = ok` before clean close.

The 2026-07-14 operator run used Python `3.13.2` and Chroma `1.5.9`. All four
expected exit boundaries matched (`0`, `73`, `0`, `0`), no phase timed out,
the disposable marker was validated, the total duration was `7.3s`, and
temporary cleanup succeeded. Canonical
local evidence:

`%LOCALAPPDATA%\MemSys\eval-artifacts\mempalace-write-receipt-restart\20260714T143903Z-2b73fba1\probe-result.json`

This closes the specific software-process question: after an abrupt managed
rewrite process death, a later process can use the durable snapshot to restore
the exact predecessor representation and retrieve it. It does not simulate
power removal, broken drive write caches, firmware lying about flushes, network
filesystem semantics, a concurrently open second `PersistentClient`, or an
unmanaged writer. It also does not inspect, ingest, repair, or authorize any
historical source. Those boundaries remain separate because upstream Chroma
issue #7040 supports strict single-client sequencing, while issues #6975 and
PR #7047 leave low-volume HNSW persistence dependent on the supported close
boundary and underlying storage guarantees.

### Chroma large document delete correction (2026-07-14)

The failed Claude provider rewrite supplied the already validated source row's
entire escaped document as a `where_document` regex during conditional delete.
Chroma's 1.5.7 frontend resolves filtered deletes before deleting IDs, its
SQLite metadata layer translates `$regex` into the SQLite `REGEXP` function,
and SQLx compiles that expression with Rust `regex`. SQLite extended result
code `1043` is `SQLITE_CONSTRAINT_FUNCTION`. The relevant upstream paths are:

- [Chroma 1.5.7 filtered-delete implementation](https://github.com/chroma-core/chroma/blob/1.5.7/rust/frontend/src/impls/service_based_frontend.rs#L1413-L1485)
- [Chroma 1.5.7 SQLite regex translation](https://github.com/chroma-core/chroma/blob/1.5.7/rust/segment/src/sqlite_metadata.rs#L676-L710)
- [SQLx 0.8.3 SQLite regex function](https://github.com/launchbadge/sqlx/blob/v0.8.3/sqlx-sqlite/src/regexp.rs#L904-L918)
- [SQLite extended result code 1043](https://www.sqlite.org/rescode.html#constraint_function)
- [Rust regex compiled-size limit](https://docs.rs/regex/1.11.1/regex/struct.RegexBuilder.html#method.size_limit)

Disposable reproduction established the boundary rather than inferring it from
the live error. Chroma 1.5.7 deleted a `320,000`-byte document but returned code
`1043` for `330,000`, `393,216`, and `524,287` byte documents after regex
escaping; Chroma 1.5.9 returned the same error for the `393,216`-byte fixture.
In every failing reproduction the row survived. Deleting the same validated ID
with its source, receipt, and content-hash metadata but no `where_document`
deleted exactly one row.

The managed path now recomputes SHA-256 from the fetched document and compares
it with the receipt-stamped output hash. When they match, deletion remains bound
to the exact ID, source ownership, receipt ID, and content hash while omitting
the redundant large regex. Missing or stale hashes keep the exact anchored
document regex; malformed hashes and empty stale-hash rows fail closed. The
pre-delete snapshot comparison, exclusive managed-write scope, exactly-one
delete accounting, and survivor readback remain unchanged. This protects
cooperating managed writers; Chroma still does not provide a compare-and-swap
delete for an out-of-band writer that changes only the document while preserving
all old metadata inside the final read/delete window.

Validation used only disposable databases: the `393,216`-byte regression and
focused cases passed, the full `tests/test_write_receipts.py` module passed
`110` tests in `49.73s`, and Ruff was clean. This correction enables a bounded
managed provider-chat canary. It does not establish historical provenance,
authorize historical recovery, or remove the separate restart and operator
approval gates.
