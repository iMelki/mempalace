# Snapshot lease release and forced termination

Tracked by [MemPalace #67](https://github.com/iMelki/mempalace/issues/67) and
[MemSys #466](https://github.com/iMelki/memsys/issues/466). Source qualification
is separate from installation and a successful natural backup.

## Requested outcome and proof

Every catchable exit releases only this snapshot's exact maintenance object.
Forced termination leaves a complete identity-bound marker that the shared
reader classifies stale after owner death, without changing that marker.
Proof comes from the real `clean_client_lease` caller in retained disposable
scratch, native handle ownership tests, the actual shared reader, and later
installed-code/natural-run evidence owned by the runtime custodian.

## Research and observed cause

The October 7 snapshot child timed out at 13,500.164 seconds, exit 124. The
parent had zero-active-process proof but expected launcher PID 54700 while
the marker named Python PID 12308. It correctly preserved the mismatched
marker. The October 9 outer run `3510e9bc` ended at 06:45:37Z, exit 124,
14,405.41 seconds against its 14,400-second whole-run bound. It proved root
exit and zero active Job processes. The inner snapshot phase still said
running. Its lease named Python PID 29600, while its recorded launcher was
94660. Both quarantined markers were read only; no live state changed.

This is a two-level lifetime problem: the outer native timeout can terminate
the wrapper and Python before either cleanup path unwinds. Python's
[context manager documentation](https://docs.python.org/3/library/contextlib.html)
supports `try/finally` for catchable exceptions. Windows
[TerminateProcess](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-terminateprocess)
does not provide Python stack unwinding. A dead-PID-only marker is therefore
insufficient; making `finally` bigger cannot fix forced termination.

Upstream-first checks on October 9 inspected original
[issues](https://github.com/MemPalace/mempalace/issues?q=snapshot+maintenance+lease),
[PRs](https://github.com/MemPalace/mempalace/pulls?q=snapshot+maintenance+lease),
[releases](https://github.com/MemPalace/mempalace/releases) and the
[develop changelog](https://github.com/MemPalace/mempalace/blob/develop/CHANGELOG.md).
The original develop branch has no `backup_snapshot.py` at the checked API
path. Related upstream PR2305 concerns idle MCP writer locks; PR2569 concerns
legacy repair palace locking. Neither implements this fork's MemSys marker
release. Existing fork design and native primitives are reused instead.

Related research: [MemSys #567](https://github.com/iMelki/memsys/issues/567)
(output-cap rescue), [#572](https://github.com/iMelki/memsys/issues/572)
(reliability and current independent source custody),
[#585](https://github.com/iMelki/memsys/issues/585) (pin ordering),
[MemPalace #40](https://github.com/iMelki/mempalace/issues/40) (content counts),
[agent-settings #1506](https://github.com/iMelki/agent-settings/issues/1506) and
[PR1736](https://github.com/iMelki/agent-settings/pull/1736) (canonical lease).
These guards and timeout budgets are preserved; no snapshot reliability or
content-proof closure is claimed by a lease fix.

## Implementation and limits

`maintenance_identity.py` is reused unchanged from
[PR72](https://github.com/iMelki/mempalace/pull/72), exact source head
`37f6b4826e1a01ed9ac6987099fba63451ce0d08`. It implements PR1736's canonical
`ownerPid`, native exact `ownerProcessStartedAtUtc`, and CIM `bootId` contract,
while preserving the historical first line. This branch overlaps PR72 until
CTO integrates that dependency; it must not be merged independently as a
second identity implementation.

Only the historical first line is compatible with the old strict snapshot
recovery comparison. The augmented payload requires PR1736's format-neutral
generic recovery reader; the old full-text/256-byte path is not compatible.

Windows release opens the current name with read/delete access and read-only
sharing. It validates the acquired object's inode/device/size/mtime through
that handle and denies competing writes and rename/delete through the
mutation. It then uses no-replace `FILE_RENAME_INFO` and reads back the
destination identity before closing. This follows the existing shared
`StorageGovernance.NativeFileMetadata.RenameNoReplace` layout and
[CreateFileW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew) /
[FILE_RENAME_INFO](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_rename_info)
contracts. Foreign replacements and occupied destinations are preserved.

Release retains `.maintenance.released-<unique>` and prepared names retain
`.maintenance.retained-prepared-<unique>`. There is no deletion or automatic
retention policy. Permission/sharing/I/O denial is an explicit release failure,
not successful release; the complete marker permits conservative stale
classification after owner death. Explicit POSIX markers keep path-based
identity checking and have no hostile concurrent-replacement proof.

The scheduled task's windowless host selects the MemSys automation-capture
wrapper, which selects `scripts/evals/Invoke-MemPalaceSnapshotBackup.ps1`
from the live checkout. It launches the configured Python module. No schedule,
host file, checkout selection, timeout, service, mine, or live lease changed.

## Remaining acceptance

Final boundary testing also reproduced an interrupt at the acquisition function's
return, before the caller stores its returned identity. The writer now hands the
prepared object's identity to the caller before publication. Cleanup therefore
retains custody even when the return is interrupted. The pinned intermediate
source fails this assertion; restored source passes. Both KeyboardInterrupt and
SystemExit return-boundary regressions are covered by the 66-case focused suite.

CTO owns two independent reviews and merge ordering with PR72 and PR1736.
Runtime custodian must separately install/select exact writer and stale-reader
and generic recovery bytes and verify that the task really executes them. No new snapshot is
authorized here. Observe the next separately authorized natural run, its outer
terminal receipt, release/stale verdict, and useful recall. Backup integrity,
restore, freshness and three healthy runs remain under the existing issues.
