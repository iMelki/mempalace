# Advisory Lock-Path Retention Decision

Tracking: [mempalace#41](https://github.com/iMelki/mempalace/issues/41)

## Decision

`mine_lock` and `mine_palace_lock` keep their hash-keyed lock paths after the
descriptor closes. They release the OS lock, but do not unlink the path as
part of normal operation.

## Why

On POSIX, `flock` is associated with an open file description, not merely a
pathname. If one process has released a lock, another process already has the
old lock file open and is about to acquire it, and a third process unlinks and
recreates the pathname, the second and third processes can end up protecting
different file identities. Both may then enter what was meant to be one
critical section.

The Linux [`flock(2)` manual](https://man7.org/linux/man-pages/man2/flock.2.html)
documents the open-file-description boundary. The POSIX
[`unlink(3p)` specification](https://man7.org/linux/man-pages/man3/unlink.3p.html)
also treats the directory entry and open file description separately. Community
locking guidance reaches the same practical conclusion: do not delete a
`flock` lock path during normal concurrent use unless the implementation first
proves all contenders are coordinated on the same file identity.

Windows may refuse deletion while a handle is open, but that platform-specific
behavior is not a portable correctness guarantee. The shared Python code must
preserve exclusion on every supported platform.

## Scope and validation

- The change applies only to the lock sentinels under `~/.mempalace/locks/`.
  They contain no secret or user content.
- Descriptor close still releases the lock; normal reacquisition remains
  supported.
- `tests/test_palace_locks.py` asserts that both source and palace lock paths
  persist with the same file identity after release/reacquisition, preventing
  a future automatic-unlink regression.
- `mine_lock` records both successful and failed acquisition timing without a
  raw source path, so the Windows retry cliff remains diagnosable even when
  acquisition raises before entering the critical section.

## Follow-up boundary

Persistent paths do not solve directory growth. Any future retention cleanup
must be a separately reviewed, serialized operation with positive evidence
that no current or waiting contender can retain the old file identity. It must
not be folded into the normal unlock path or used as a reason to weaken the
cross-process lock contract.

## Related test-observability boundary

The load-sensitive test work also relates to
[#24](https://github.com/iMelki/mempalace/issues/24). Failure diagnostics use
pytest's report-section mechanism with a bounded pseudonymous metrics payload;
they never retain raw node IDs, tracebacks, source paths, or fixture values in
the repository. The incoming `.gitignore` change that would have hidden a
repo-root raw-diagnostics directory is intentionally not adopted.
