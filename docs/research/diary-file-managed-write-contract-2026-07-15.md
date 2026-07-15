# Diary-file managed-write contract for issue #22

Date: 2026-07-15

## Status

The `mempalace.diary_ingest` path now treats each dated Markdown file as one
managed source. A changed source is materialized as one day drawer plus the
complete current set of derived closets under the same source receipt. No live
or configured palace was opened while implementing or testing this tranche.

This completes the `diary-ingest` entry in
`managed-write-boundary-dispositions-2026-07-14.json`. It does not complete the
other writer dispositions and does not authorize historical replay.

## Why direct upsert was insufficient

The previous implementation used a deterministic drawer ID and Chroma
`upsert`. That made one record replaceable, but it did not prove which source
bytes generated the drawer and closets, did not bind both collections to one
terminal result, and could leave stale derived closets after an interrupted or
shorter rewrite.

It also used file length as the unchanged test. Different content with the
same byte count could therefore be skipped. Incremental closet handling wrote
only newly appended entries back from closet index zero, so an update could
overwrite earlier closet material instead of representing the complete day.

Chroma documents `upsert` as create-or-update for the supplied record IDs. It
does not claim source-level lineage, replacement of outputs omitted from the
new batch, or a transaction spanning multiple collections. Those additional
properties belong to MemPalace's managed-write layer.

## Managed source identity

- Source locator: the canonical absolute diary-file path.
- Source content identity: SHA-256 of the exact retained source bytes.
- Adapter: `diary-file` version `1.0.0`.
- Output-affecting configuration: wing, date, adapter version, declared
  transformations, the privacy-safe digest of the known-entity/language
  snapshot, and `mempalace-diary-file-managed-write/v1`.
- Drawer ID: the existing deterministic `(wing, date)` ID, preserving query
  compatibility.
- Closet IDs: the existing deterministic `(wing, date, closet number)` IDs.

The file is read only after the managed source lock is held. Exact source bytes
are hashed before any old output is purged. Entity extraction uses the same
snapshotted registry/language inputs represented by the run configuration, so
an entity-configuration change invalidates unchanged reuse without recording
the private registry names in the receipt. The same language snapshot drives
both drawer metadata extraction and closet topic/entity extraction; the latter
does not fall back to a stale process-wide default regex cache.

Two filenames in one input directory that begin with the same date would target
the same `(wing, date)` output IDs. The directory preflight rejects that
ambiguity before opening or mutating the palace. A conflicting source from a
different invocation is also rejected by the managed collection ownership
check before it can overwrite an existing row.

A directory containing Markdown but no dated diary filename returns an empty
result before opening or creating palace collections.

## Replacement lifecycle

For each valid dated file:

1. acquire the directory state-file lock, palace write lock, and source lock;
2. read and hash the exact source bytes;
3. begin a per-source receipt and reconcile pending recovery state;
4. reuse a represented receipt when source hash, adapter, routing, and
   configuration still match;
5. otherwise snapshot the prior drawer and closet rows durably;
6. purge only the exact prior source outputs;
7. write the complete current day drawer and all current closet lines through
   receipt-aware collection facades;
8. verify each collection write and publish the terminal receipt;
9. atomically update the convenience state file only after receipt completion.

If a closet write fails after the drawer changed, managed rollback restores the
exact predecessor drawer, closets, embeddings, and current receipt head. The
state file is not advanced.

Chroma can briefly expose a committed document/metadata row before its exact
vector segment is readable. Snapshot and unchanged-reuse reads now retry that
specific supported visibility error for at most two seconds, re-reading the
whole row set on every attempt. Unrelated backend errors still propagate, and
a vector view that never stabilizes still fails closed without claiming reuse.

## Unchanged, forced, and zero-output behavior

- An ordinary identical rerun publishes/reuses verified unchanged evidence and
  reports `days_updated=0`.
- `force=True` requests a complete verified rewrite even when the source hash
  is unchanged. It does not bypass rollback or receipt verification.
- A dated source that becomes shorter than the minimum accepted diary content
  publishes a `ZERO_OUTPUT` successor. Prior drawer and closet outputs are
  removed under the same recovery boundary instead of remaining stale.
- `closets_created` remains as a compatibility return field. New callers may
  use the equivalent and more accurate `closets_written` field.

The JSON state under `~/.mempalace/state/` is a convenience index, not write
authority. It now records source content hash and receipt ID, is published with
flush-plus-atomic-replace, and is serialized across concurrent directory runs.
Receipts remain authoritative if a process dies after receipt publication but
before the state file advances. A valid JSON state entry with a legacy or
malformed scalar value is normalized and replaced after the managed commit
instead of poisoning every retry. A valid JSON state file whose root is not an
object is likewise treated as empty convenience state and replaced after the
managed commit.

## Historical and deletion boundary

The first managed run may rewrite legacy rows selected by the same canonical
`source_file` into a new, honestly dated receipt. It does not fabricate an old
write-time receipt.

A file that still exists but becomes ineligible is handled by `ZERO_OUTPUT`.
Automatic pruning for files that disappear or are renamed is not implemented
in this tranche. The stale state entry remains visible until an explicit,
reviewable missing-source policy is added; absence is not treated as deletion
permission.

## Community and upstream basis

- [Chroma upsert documentation](https://docs.trychroma.com/reference/chroma-api/record/upsert-records)
  defines record-level create-or-update behavior for supplied IDs.
- [Chroma update guidance](https://docs.trychroma.com/docs/collections/update-data)
  states that documents updated without embeddings are re-embedded and that
  `upsert` creates missing IDs or updates existing IDs.
- [Chroma query/get guidance](https://docs.trychroma.com/docs/querying-collections/query-and-get)
  documents requesting exact embeddings through `include`. It does not define
  an immediate post-write vector-visibility barrier, so MemPalace treats only
  the backend's classified delayed-visibility errors as boundedly retryable.
- [W3C PROV-DM](https://www.w3.org/TR/prov-dm/Overview.html) models source
  entities, activities, generated entities, derivation, and invalidation as
  separate provenance concepts.
- [OpenLineage run-cycle guidance](https://openlineage.io/docs/1.47.1/spec/run-cycle/)
  distinguishes started, running, complete, and abnormal terminal states and
  associates runs with their input and output datasets.

MemPalace follows those principles locally without claiming protocol
conformance: exact source identity, run state, predecessor invalidation, and
generated output identities are distinct evidence.

## Focused validation

The `TestDiaryIngest` proof covers:

- drawer and closet output receipt representation;
- undated Markdown no-op without palace creation;
- unchanged receipt reuse;
- same-byte-count source changes;
- duplicate date-file rejection before palace mutation;
- entity-configuration changes invalidating unchanged reuse;
- entity-language snapshots governing both drawer and closet extraction;
- malformed legacy state-entry repair after a managed commit;
- non-object state-root repair after a managed commit;
- complete closet replacement;
- `ZERO_OUTPUT` stale-row cleanup and repeated idempotence;
- injected closet failure with exact drawer/closet/receipt rollback;
- no state-file advance on failure;
- state-file placement and cross-wing ID compatibility.

The focused class passes 13 tests. The expanded receipt/diary proof passes 207
tests in 59.03 seconds. The protected repository gate passes 1,706 tests with 7
skips and 106 intentional deselections in 141.05 seconds. Durable local logs are
recorded in `OPEN_TASKS.md`; no configured palace was opened by these tests.

## Remaining issue #22 work

- define an explicit missing/renamed-source disposition before any automatic
  diary deletion;
- adapt migration, repair, dedup, sweeper, compression, and closet-regeneration
  writers;
- retire the remaining unmanaged backend/context/CLI/cache mutation bypasses;
- define the five separate non-drawer contracts;
- prove Windows receipt-state DACL enforcement and keep historical recovery
  gated separately.
