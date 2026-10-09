# Windows snapshot maintenance-lease identity

Tracking: [MemPalace #67](https://github.com/iMelki/mempalace/issues/67),
[MemSys #857](https://github.com/iMelki/memsys/issues/857), and
[agent-settings #1506](https://github.com/iMelki/agent-settings/issues/1506).

The snapshot writer belongs to `mempalace/backup_snapshot.py`; the MemSys
snapshot wrapper delegates to it. The old complete CRLF lease line remains
the first line. Windows writers append the shared lease contract fields:
`leaseSchemaVersion=1`, `ownerPid`, `ownerProcessStartedAtUtc`, and `bootId`.

The process timestamp comes from native
[GetProcessTimes](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getprocesstimes),
preserving FILETIME's 100 ns precision in UTC with seven fractional digits.
Boot identity uses
[Win32_OperatingSystem.LastBootUpTime](https://learn.microsoft.com/en-us/windows/win32/cimwin32prov/win32-operatingsystem),
normalized by PowerShell's UTC `ToString('o')`, matching the shared reader.
[Win32_Process](https://learn.microsoft.com/en-us/windows/win32/cimwin32prov/win32-process)
documents that process IDs are reused; a PID alone cannot bind ownership.

Process and boot evidence is obtained before preparing or publishing a marker.
Native/API error, missing PowerShell, malformed boot response or a 60-second
boot-probe timeout refuses acquisition. The helper subprocess is windowless
and queries only OS restart time. No credentials, process command lines, palace
content, service state or live marker are read by this helper.

The existing native no-write/no-delete-sharing handle, complete preparation,
no-replace hard-link publication and ownership-preserving cleanup are retained.
This change does not close the known graceful-release stat-to-unlink race.
Explicit POSIX marker callers keep the legacy contract; shared readers report
these markers as unverifiable and continue honoring them. Windows identity is
never synthesized from marker age, current wall time or an estimated uptime.

Focused tests cover complete CRLF fields, exact legacy first-line compatibility,
100 ns preservation, malformed/missing/timeout identity, publication refusal,
and the actual Windows native timestamp against independent PowerShell process
and boot readback. Existing snapshot race and fault tests remain regression
coverage. Source fixtures are independent of the real marker and palace.

The new shared reader must be adopted with this writer before claiming live
stale detection. Source publication does not prove installed consumption,
natural backup completion, live quarantine, query recovery or absence of
surviving children. Those remain with the runtime owner under the linked issues.
