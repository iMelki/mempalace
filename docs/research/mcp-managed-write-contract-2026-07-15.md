# MCP managed-write contract for issue #22

## Plain-English summary

MemPalace's MCP drawer and diary tools previously held the palace lock and
checked their immediate Chroma result, but they did not publish durable proof
that connected one logical source to the exact row they created, replaced, or
removed. The tools now delegate mutation to the same managed receipt driver as
other receipt-aware source adapters.

This change applies to new MCP-managed sources. It does not invent historical
write-time provenance for rows that predate receipts.

## Public behavior

### Drawer tools

`mempalace_add_drawer`, `mempalace_update_drawer`, and
`mempalace_delete_drawer` require `source_id` for every real mutation. The ID
means "this same real note, message, decision, or record" and must remain stable
while its content or classification changes.

- Add derives one deterministic drawer ID from `source_id` and writes one exact
  receipt-owned output.
- The source content hash binds the document and semantic metadata, including
  wing, room, `added_by`, source label, agent, and topic. Wall-clock fields and
  receipt stamps are deliberately excluded.
- Each row carries `mcp_semantic_metadata_hash`; retries compare the actual
  semantic fields and marker before they can be called unchanged.
- Repeating the same source and semantic payload is idempotent and publishes an
  `UNCHANGED` successor receipt.
- Updating the same source snapshots its current row, publishes a superseding
  receipt, and replaces the row under the palace lock.
- Deleting the same source snapshots and removes its row, publishes a
  `ZERO_OUTPUT` successor, and publishes an invalidation for the predecessor.
- Repeating an already-proven deletion returns the current zero-output receipt
  without creating another mutation.

The optional legacy `source_file` argument is no longer stored as an absolute
path. Only a bounded basename is retained as `source_name`; authoritative
ownership comes from the caller-selected logical `source_id` and the
per-palace HMAC source identity. The row's `source_file` locator contains only
an opaque SHA-256 token, not the caller's raw `source_id`.

The Python function preserves the old positional order
`(wing, room, content, source_file, added_by, source_id)`. Old callers therefore
receive a clear missing-`source_id` result instead of silently binding a legacy
`source_file` value as the new identity.

### Diary tool

`mempalace_diary_write` now uses the same receipt-aware service. Its optional
`source_id` is an idempotency key: retrying the same diary entry with the same
ID reuses the same row instead of appending a duplicate. Calls that omit it
keep append behavior by generating a unique entry/source ID. The receipt source
and row ID scope a supplied key by normalized agent and wing, so two agents or
two wings may safely use the same caller key without sharing a receipt head.

## Failure and recovery behavior

The managed service uses `managed_adapter_ingest()` and therefore inherits the
existing source transaction:

1. acquire the palace-wide managed write lock and source lock;
2. reconcile any pending rewrite recovery;
3. bind the source version to a content hash;
4. verify or snapshot the predecessor representation;
5. durably publish rewrite recovery before purge;
6. write and stamp the exact output;
7. verify the receipt across every managed collection available to the MCP
   process, then compare the exact document, semantic metadata, semantic marker,
   opaque source locator, and HMAC source identity;
8. publish the COMPLETE receipt, invalidation, and current-source head;
9. restore the predecessor on a failed replacement.

An integration test injects a failure after predecessor purge and before the
replacement add. The old document and current receipt are restored, and no
predecessor invalidation is published.

## Historical-row boundary

Rows with no managed receipt remain readable and searchable. Update and delete
reject them with an explicit provenance-migration error even if a caller
supplies a new `source_id`. Accepting that ID would falsely imply that the ID
was the source of the old write.

A future attended migration may connect retained source evidence to those rows
or emit a newly dated recovery receipt. It must not fabricate a past receipt.

## Transport and Chroma findings

The MCP tool schema exposes `source_id`, but temporarily leaves it out of the
JSON Schema `required` array so older generated clients remain callable. The
execution handler still fails closed before storage access when drawer mutation
omits it. This is a compatibility transition, not an optional identity policy.
The MCP specification requires servers to validate tool inputs and defines
`inputSchema` as JSON Schema; actionable tool execution errors are the normal
way to let clients correct invalid values.

Chroma's official collection API distinguishes `get_collection`, which only
retrieves an existing collection, from collection-creation APIs. MemPalace now
uses `get_collection` without HNSW pinning on read/cache misses, and status no
longer creates `mempalace_drawers` merely because `chroma.sqlite3` exists.
Collection creation and the HNSW thread retrofit remain explicit write-path
behavior. This matters because
upstream issue #7040 documents that opening a second local PersistentClient can
perform migration writes and wait on SQLite locking. Its proposed #7041 change
is still open and explicitly does not solve HNSW-file concurrency, so the report
supports conservative single-owner serialization rather than a claim that every
second opener hangs. Issue #6979 documents a separate Rust/HNSW crash class;
its root-cause analysis is reporter evidence, not a merged or maintainer-
confirmed fix. Upstream #6975 separately reports that successful
low-volume writes below `sync_threshold` may remain dependent on queue replay
instead of a freshly persisted HNSW snapshot. Its analysis and open #7047 fix
target `PersistentLocalHnswSegment`/`SegmentAPI`, so it is contextual risk and
evidence of a missing public flush surface, not proof of loss in MemPalace's
`RustBindingsAPI`. That distinction is why same-process readback is not
described as restart durability; MemPalace's synthetic
hard-process restart probe is the separate evidence for the managed recovery
path. The current single cached client, palace lock, and no-create-on-read branch
reduce these risks; they do not finish the separate
`mcp-direct-collection-cache` retirement. Moving all transport reads behind a
private read facade and making handle retirement explicit remains open.

Primary references:

- https://modelcontextprotocol.io/specification/2025-11-25/server/tools
- https://docs.trychroma.com/docs/collections/manage-collections
- https://github.com/chroma-core/chroma/issues/7040
- https://github.com/chroma-core/chroma/pull/7041
- https://github.com/chroma-core/chroma/issues/6975
- https://github.com/chroma-core/chroma/pull/7047
- https://github.com/chroma-core/chroma/issues/6979
- https://www.w3.org/TR/prov-dm/

## Validation

- `tests/test_mcp_server.py`: 91 passed, 1 platform skip.
- Combined receipt/HTTP/dispatch/source/MCP-server suite: 270 passed, 1
  platform skip.
- Final repository gate: 1,695 passed, 7 skipped, and 106 intentional
  deselections in 136.93 seconds.
- The tests cover create, unchanged retry, supersession, exact readback,
  semantic metadata tampering, zero-output deletion and invalidation, foreign
  target reuse after deletion, diary retry and agent/wing scoping, logical-
  source mismatch, old positional compatibility, no-create/no-pin reads,
  legacy-row rejection, and failed-rewrite rollback.

No configured palace, Railway service, hosted runtime, or historical source was
opened or mutated during this implementation and test pass.

## Remaining issue #22 work

- adapt diary-file drawer/closet ingestion;
- adapt sweeper, repair, deduplication, migration, compression, and closet
  regeneration paths;
- retire public unmanaged collection write handles and transport-owned backend
  access;
- finish handle-aware Chroma cache retirement and Windows receipt DACL proof;
- keep the 18-source historical recovery cohort at NO-GO until every reviewed
  backup, restore, equivalence, canary, and approval gate is fresh.
