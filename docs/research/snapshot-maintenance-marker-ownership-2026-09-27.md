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
Python documents that `write_text()` overwrites an existing file. An initial
exclusive-open repair blocked a competing owner, but a write/flush/close error
after creation could leave a partial active marker, and its LF-only bytes did
not match the existing PowerShell recovery consumer's CRLF contract. The lease
also treated `marker_pre_existing=True` as acceptable and unlinked its own
marker by path on exit. [Python pathlib documentation](https://docs.python.org/3/library/pathlib.html),
[Python `os.link` documentation](https://docs.python.org/3/library/os.html#os.link),
[Microsoft `CreateFileW` sharing documentation](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew),
and [Microsoft `CreateHardLinkW` documentation](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-createhardlinkw)
are the current API authorities. The
[CPython source issue on `write_text`](https://github.com/python/cpython/issues/90712)
is community context, not the behavior authority.

## Fix / Action Status

The source mitigation makes the child fail closed on an existing marker. It
first prepares complete CRLF recovery-compatible bytes in a unique file in
the marker directory, then publishes them with a same-volume, no-replace hard
link. On Windows, the prepared file is held open with a native handle that
shares reads but denies competing writes and rename/delete through the link
call. A bounded C: test showed both operations blocked before link while
linking succeeded. The live marker is not exposed to an incomplete write on
that tested Windows path. A late foreign marker rejects publication; write,
flush, and fstat faults before publication leave no active pause marker.
Close failure after a successful link triggers an exact-identity release
attempt; if release itself fails, the remaining marker has complete CRLF
content and requires attended recovery. A conservative prepared-file artifact
may remain if identity cannot be established or safe cleanup fails; it is not
`.maintenance` and cannot be mistaken for an active lease. The
prepared marker's file identity is retained for release checks, and an
observed foreign replacement is preserved. On a filesystem without supported
hard links, acquisition fails closed without an overwrite or copy fallback.
The current Windows C: marker volume was tested with `os.link`: existing
destination failed without overwrite, and removing the prepared name left
the published link readable. This does not prove every volume supports links.
On POSIX, explicit-marker callers retain the previous pathname-based
no-replace hard-link behavior for compatibility with Ubuntu/macOS CI. That
fallback has no Windows-style object-binding proof against a hostile
before-link source-path replacement; it must not be described as closed.
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
- [x] Actual PowerShell recovery expected text matches emitted CRLF bytes;
      LF-only bytes fail the comparison.
- [x] Injected write, flush, and fstat errors before publication leave no
      active marker; injected close and post-link errors either release the
      exact owned marker or preserve a complete marker for attended recovery.
- [x] On tested Windows C:, attempted prepared-path rename and second-writer
      truncation immediately before link are blocked by the held handle.
- [ ] POSIX before-link source-path replacement has an object-bound protocol
      or a separately accepted weaker threat model.
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

- MemPalace focused `tests/test_backup_snapshot.py`: 39 passed after review
  changes, including the consumer-function contract and fault injection.
- A bounded C: Windows fixture proved `os.link` no-replace behavior and
  hard-link source removal/readback; unsupported filesystem behavior is
  intentionally fail-closed.
- MemSys early-admission tests: 6 focused passed; broader 105/106 passed with
  the single live task-slot conflict in MemSys #448, unrelated to this patch.
- No snapshot, task, marker, router, or palace data was changed by this work.
