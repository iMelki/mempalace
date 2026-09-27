# Snapshot maintenance-marker ownership and recovery

Tracking: [MemPalace #67](https://github.com/iMelki/mempalace/issues/67).
Related: [MemSys #466](https://github.com/iMelki/memsys/issues/466).

## Plain-English Summary

A Palace snapshot could run while an older MemSys maintenance marker already
existed. The snapshot might complete, but the marker still paused MemSys
watchdogs and ordinary recall. A check-only-then-write marker creation also
could overwrite a marker created in the gap. This is a source-level safety
problem, not evidence that a backup is missing or a router is healthy.

## Current State

On 2026-09-27, the private runtime still held an exact pre-boot marker from
2026-09-26 with an absent owner PID. The guarded recovery tool classified it
as eligible for attended same-directory quarantine; no Apply occurred. The
trusted MemSys CLI passed its hash check, but `--no-auto-start` recall refused
connection to the router. A newer snapshot's complete receipt did not clear
the older pause.

The source previously used `Path.exists()` followed by `Path.write_text()`;
Python documents that `write_text()` overwrites an existing file. The lease
also treated `marker_pre_existing=True` as acceptable and unlinked its own
marker by path on exit. [Python pathlib documentation](https://docs.python.org/3/library/pathlib.html)
and [exclusive open-mode documentation](https://docs.python.org/3/library/functions.html#open)
support using `open('x')` to reject an existing path at the actual creation
boundary. The [CPython source issue on `write_text`](https://github.com/python/cpython/issues/90712)
is community context; the API documentation, not that issue, is the behavior
authority.

## Fix / Action Status

The source mitigation makes the child fail closed on an existing marker and
uses exclusive file creation, including when another owner creates the marker
after the MemSys wrapper's early preflight. It records file identity from the
created handle and preserves a replacement marker found at lease release.
The MemSys wrapper separately rejects an execute-time marker-path override
that the child would not honor. Both layers have focused negative tests.

This is **not** a completed live recovery or a proof of fully atomic release.
The graceful-release path still uses a path-based unlink after an identity
check; a non-cooperating process could replace the path in that last gap.
The remaining exact-object release design belongs here, with MemSys #466
tracking fleet recovery and natural-run proof. A test that moves the owned
marker and creates a foreign replacement shows the current guard preserves
the replacement; it does not prove every possible interleaving.

## Decision Needed

The old private runtime marker requires separate operator approval for the
existing exact-object quarantine tool. This source change grants no such
approval and does not authorize another snapshot, service start, task edit,
retention deletion, or live palace mutation.

## Recommended Default

Publish the fail-closed acquisition mitigation after review and tests. Keep
the exact-object release and natural scheduled-run acceptance open. Quarantine
the current marker only under its separately approved, freshly rechecked
identity contract.

## Acceptance Criteria

- [x] Existing marker blocks a new child lease without changing the marker.
- [x] A marker arriving at the exclusive-create boundary blocks the child.
- [x] A replacement observed at graceful release is preserved and reported.
- [ ] Replace path-based release with reviewed exact-object release or an
      equivalent cross-process ownership protocol and negative race proof.
- [ ] Attended recovery of the current stale marker and a normal routed query.
- [ ] Natural scheduled backup completion leaves no stale maintenance pause;
      prove on more than one natural run.

## Blocks / Not A Blocker

The current stale marker blocks normal router recovery. The source mitigation
does not by itself block read-only investigation or isolated tests. It must not
be described as installed, natural-run, or end-to-end recovery evidence.

## Validation / Evidence

- MemPalace focused `tests/test_backup_snapshot.py`: 29 passed.
- MemSys early-admission tests: 6 focused passed; broader 105/106 passed with
  the single live task-slot conflict in MemSys #448, unrelated to this patch.
- No snapshot, task, marker, router, or palace data was changed by this work.
