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

Tests use deterministic embeddings and disposable stores under a fresh lane
TEMP root. Coverage includes size/mtime/hash drift, same-stat content change,
empty output, unchanged retry, failure before write, crash after COMPLETE,
deleted source and receipt-index classification. `.gate-evidence.json` records
actual missing-event and substituted-predecessor exit-1 probes and restored
exit-0 readbacks. Source tests are separate from installed and live proof.
