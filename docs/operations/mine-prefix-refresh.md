# Completed-prefix refresh on project mine resume

The project miner now refreshes changed completed sources before continuing
to the uncompleted suffix. Scope is [#69](https://github.com/iMelki/mempalace/issues/69),
the source implementation child of [MemSys #857](https://github.com/iMelki/memsys/issues/857).
This document does not authorize a live mine or runtime change.

The original manifest and contiguous progress stay immutable evidence. A
`<progress-jsonl>.refresh/` directory contains immutable numbered events with
the original manifest/progress binding, a hash chain, the planned replacement
descriptor and predecessor receipt ID. `prepared` records the source snapshot
before processing. `represented` records the independently verified successor
receipt and represented output count. This sidecar is part of resumable run
state and must travel with its original progress; do not delete it as scratch.

Changed bytes use existing managed rewrite recovery and explicitly supersede
the old receipt. Timestamp-only drift creates an UNCHANGED successor with
verified reused outputs. A crash after COMPLETE but before the sidecar commit
recovers that exact successor and records completion without another write.
Known failed attempts can retry through the normal managed recovery path. A
later source change is a separate snapshot and successor, retaining history.

## Recovery after an intervening mine (#75)

A journal-less project mine can publish another successor after a refresh process
exits between COMPLETE publication and `represented`. Recovery resolves the
authoritative current head without filtering away a different content/configuration
successor. It walks that head's stored `predecessor_receipt_id` chain and requires
a COMPLETE link whose predecessor is the recorded progress receipt, whose content
and version hashes match the pending snapshot, and whose configuration digest
matches the resume run. Matching bytes alone cannot prove that link.

The existing receipt reader still rejects disconnected branches, forks, missing
predecessors and invalid COMPLETE events. Recovery verifies exact current-head
outputs, including reused and zero-output representations, rather than expecting
superseded ancestor rows to survive. An unrelated successor or unrepresented head
is refused before writing refresh events or advancing progress.

The recovered `represented` event retains its original `prepared_digest` and pins
the actual head with `recovered_head.content_hash` and `config_digest`. Its optional
`item` records a fresh source descriptor only when the current source bytes match
that verified head; otherwise it retains the historical prepared snapshot. This
does not claim that the historical snapshot describes later edited bytes. A new
snapshot then refreshes those bytes normally. A different head configuration also
forces a managed successor under the resume configuration. The pinned head and
the original prepared receipt ancestry are reverified after an interruption between
recording recovery and that rewrite. A false but structurally valid saved recovery
claim does not grant authority to skip the ancestor check.
Existing events without these optional fields retain their original semantics.
Older readers can refuse the extended recovery state; do not mix miner versions
within a retained generation without its existing adoption review.

Snapshots and `process_file` use the same `normcase(canonical_source_locator(...))`
lock key. The existing palace lock still spans resume validation and processing;
this correction introduces no lock or lease mechanism.

Missing sources, changed path identity, corrupted sidecars, foreign receipts,
unverified outputs and changed uncompleted sources remain errors. Prefix
refresh does not relax the existing palace lock or receipt lineage checks.

The earlier October 2 indexed-event failure cannot be attributed from saved
diagnostics: its inner cause was truncated. Pure-file inspection of the retained
default-root checkpoint on October 9 found 484 internally consistent indexes
matching all 484 recorded receipts; this does not prove live row semantics or
every possible receipt-root override. Current validity does not explain a
historical failure. Do not repair valid receipt data on that unknown.

Future conflicts identify `stage`, `cause`, `errno` and `winerror` in the final
exception line without private paths or raw JSON. Fixtures distinguish missing
events, malformed indexes and mismatched hashes. The existing supervisor's
bounded final-tail diagnostic can retain these fields without expanding its
traceback capture. [agent-settings #1459](https://github.com/iMelki/agent-settings/issues/1459)
still owns general nested-cause preservation and the historical evidence gap.

## Adoption and proof limits

The retained run pins miner source SHA-256 in both its manifest and the grouped
wrapper generation. New source bytes require a separately reviewed migration
or new generation/replan. Never rewrite old manifest hashes or reset the old
cursor. Preserve old generations, quarantine receipts and every per-source
receipt. MemSys runtime ownership under #857 handles adoption after CTO source
review, existing lease work and explicit live-run authority.

Consumer scan: `miner.py` project prefix validation uses this sidecar;
`Mine-Knowledge.ps1` binds imported source and grouped run identity;
`MineKnowledgeDurable.Common.ps1` validates progress and sanitizes diagnostics.
Conversation mining uses separate contracts and receives no automatic change.

## Research and validation

Sources: [deterministic mining #25](https://github.com/iMelki/mempalace/issues/25),
[Python filesystem replacement semantics](https://docs.python.org/3/library/os.html#os.replace),
[SQLite transaction semantics](https://docs.python.org/3/library/sqlite3.html#transaction-control),
and [Chroma shared-directory concurrency #7040](https://github.com/chroma-core/chroma/issues/7040).
The repair reuses existing immutable durable publication and managed rewrite
primitives. It does not change database journal mode or create a new lease.

The #75 investigation read both independent [#70 reviews](https://github.com/iMelki/mempalace/pull/70)
and the [reported interleaving](https://github.com/iMelki/mempalace/issues/75).
Upstream-first research checked [changed-file refresh #645](https://github.com/MemPalace/mempalace/issues/645),
[refresh issues](https://github.com/MemPalace/mempalace/issues?q=refresh),
[receipt pull requests](https://github.com/MemPalace/mempalace/pulls?q=receipt),
[v3.10.0](https://github.com/MemPalace/mempalace/releases/tag/v3.10.0), and the
[upstream changelog](https://github.com/MemPalace/mempalace/blob/develop/CHANGELOG.md)
on 2026-10-09. That bounded search found no matching completed-prefix receipt-lineage
repair to port. Python's [hard-exit contract](https://docs.python.org/3/library/os.html#os._exit)
and [path normalization](https://docs.python.org/3/library/os.path.html#os.path.normcase)
inform the crash and cross-platform lock fixtures.

The child exits after real COMPLETE publication and before `represented`; the
parent independently reads the pending event and successor receipts. Cases cover
same bytes, further edits, changed configuration, two intervening successors,
another unmined edit, and interruption after recovery publication. The old
implementation failed the original three interleavings and the lock-key assertion.
Removing the ancestor gate deliberately makes initial and saved unrelated-head
refusal tests fail. See `tests/fixtures/mine_refresh_lineage_negative.py` and the attributed
before/restored results in `.gate-evidence.json`. Fixtures use disposable stores;
these source results do not prove installed or live adoption.

Tests use deterministic embeddings and disposable stores under a fresh lane
TEMP root. Coverage includes size/mtime/hash drift, same-stat content change,
empty output, unchanged retry, failure before write, crash after COMPLETE,
deleted source and receipt-index classification. `.gate-evidence.json` records
actual missing-event and substituted-predecessor exit-1 probes and restored
exit-0 readbacks. Source tests are separate from installed and live proof.

Snapshot fixture readers must release their own Chroma clients after each read,
including failed reads. Otherwise a peer can retain the shared native system and
prevent the existing backend reopen from replacing a broken vector view (#71).
The regression forces a stale view until actual system replacement, then reads
exact embeddings; it preserves production retry limits and failure behavior.
