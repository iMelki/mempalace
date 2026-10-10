"""Owner identity for the Windows MemSys maintenance-lease contract.

No palace data, marker, service, or process state is changed by these probes.
"""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone


def _filetime_utc(value: int) -> str:
    """Preserve all 100 ns digits in the .NET UTC round-trip representation."""
    seconds, remainder = divmod(value, 10_000_000)
    instant = datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)
    return instant.strftime("%Y-%m-%dT%H:%M:%S") + f".{remainder:07d}Z"


def _windows_process_started_at_utc() -> str:
    """Read this process's native creation time; never infer it from wall time."""
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    current = kernel32.GetCurrentProcess
    current.argtypes = []
    current.restype = wintypes.HANDLE
    times = kernel32.GetProcessTimes
    times.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    times.restype = wintypes.BOOL
    created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
    if not times(current(), created, exited, kernel, user):
        raise ctypes.WinError(ctypes.get_last_error())
    return _filetime_utc((created.dwHighDateTime << 32) | created.dwLowDateTime)


def _windows_boot_id() -> str:
    """Use the shared PowerShell contract's normalized OS restart timestamp."""
    script = (
        "$ErrorActionPreference='Stop'; "
        "$boot=(Get-CimInstance -ClassName Win32_OperatingSystem).LastBootUpTime; "
        "if ($null -eq $boot) { throw 'boot identity unavailable' }; "
        "$boot.ToUniversalTime().ToString('o')"
    )
    try:
        result = subprocess.run(
            ["pwsh", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OSError("maintenance lease boot identity unavailable") from exc
    value = result.stdout.strip()
    if not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{7}Z", value):
        raise OSError("maintenance lease boot identity malformed")
    return value


def maintenance_owner_identity() -> dict[str, str | int]:
    """Return complete Windows identity, or preserve legacy explicit POSIX markers.

    The normal marker exists only on Windows. Explicit POSIX marker callers keep
    the existing legacy contract; they must be reported unverifiable by readers.
    """
    if os.name != "nt":
        return {}
    return {
        "leaseSchemaVersion": 1,
        "ownerPid": os.getpid(),
        "ownerProcessStartedAtUtc": _windows_process_started_at_utc(),
        "bootId": _windows_boot_id(),
    }
