# Conversation Recovery Collection Binding

Date: 2026-07-29

## Finding

The bounded Codex provider-chat retry reached MemPalace after the scheduled
writer was stopped, but exited with:

`mempalace.write_receipts.ReceiptRecoveryError: recovery collections are unavailable: closets`

The live palace was not missing the collection. Read-only `repair-status` and
the Chroma SQLite inventory both showed `mempalace_closets` present. The one
pending recovery record was created for the setup-file rewrite and named both
`closets` and `drawers`:

- recovery receipt: `94d19376-4462-560f-91a8-8156c58c0179`
- source selectors: `claw/docs/getting-started/SETUP.md` and its normalized alias
- recovery file: `%USERPROFILE%\\.mempalace\\palace\\.mempalace\\write-receipts\\v1\\recoveries\\a2ff23a47dc1eff9cbd7b02e34ae3991a63d010bee1dfd89f4acb1ca7dd4aebf\\94d19376-4462-560f-91a8-8156c58c0179.json`

`convo_miner.py` supplied only the drawer collection to its palace-wide
`reconcile_pending_rewrites()` call. The filesystem miner already supplied
both collections. This made a conversation run fail closed even though the
physical recovery target existed.

## Change

Conversation mining now binds the existing closet collection when present and
passes it alongside drawers for palace-wide recovery. A fresh
conversation-only palace may not have a closet collection yet, so the lookup
uses `create=False` and omits the optional collection on Chroma's not-found
signal. A pending recovery that requires closets still fails closed rather
than creating an unrelated empty collection.

The conversation output contract remains drawers-only. This change only makes
the recovery input complete; it does not create conversation closets or change
retrieval ranking.

## Validation

- Focused tests: `21 passed` in `tests/test_convo_miner_unit.py` and
  `tests/test_convo_miner.py`.
- Regression coverage asserts that both managed collection handles are passed
  to recovery reconciliation when a closet collection exists.
- The live pending recovery remains unchanged until the bounded retry reads
  the repaired code path. No X-drive publication or Router restart was used.

## Design evidence

Chroma's collection contract distinguishes `get_collection()` (retrieve an
existing collection) from `get_or_create_collection()` (create when absent).
The implementation follows that distinction: recovery may use an existing
closet, but a conversation-only palace must not silently create one merely to
clear a recovery queue.

- [Chroma collection management](https://docs.trychroma.com/docs/collections/manage-collections)
- [MemPalace closet layer](../CLOSETS.md)
- [Deterministic mine progress and receipt contract](deterministic-mine-progress-contract-2026-07-29.md)

## Follow-up

Run one attended, bounded provider-chat retry after the code is committed. A
successful run must show the existing recovery reconciled, one source file
processed or explicitly skipped by an authoritative receipt, and a terminal
receipt artifact. A failure must preserve the recovery record and remain
fail-closed.
