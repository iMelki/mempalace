"""Observe the nightly leak with legacy source, then canonical stale handling."""

import json
import os
import subprocess
import sys
from pathlib import Path


def main():
    mode, directory, consumer = sys.argv[1:]
    if os.name != "nt" or mode not in ("--legacy", "--restored"):
        raise RuntimeError("Windows lease probe requires legacy/restored mode")
    root = Path(directory)
    if root.exists():
        raise RuntimeError("proof requires fresh retained scratch")
    fixture = Path(__file__).with_name("snapshot_lease_release_probe.py")
    child_mode = "--abrupt-legacy" if mode == "--legacy" else "--abrupt"
    result = subprocess.run(
        [sys.executable, "-B", str(fixture), child_mode, str(root)],
        capture_output=True,
        timeout=90,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode == 124, f"fixture exit unexpected: {result.returncode}"
    marker = root / ".maintenance"
    content = marker.read_bytes()
    script = (
        ". $args[0]; Get-MemSysMaintenanceLease -Path $args[1] | "
        "Select-Object status,active,reason | ConvertTo-Json -Compress"
    )
    readback = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-NonInteractive",
            "-CommandWithArgs",
            script,
            consumer,
            str(marker),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=90,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    state = json.loads(readback.stdout)
    assert marker.read_bytes() == content, "stale reader changed lease bytes"
    print(f"fixture exit=124; reader={state}", flush=True)
    assert state == {"status": "stale", "active": False, "reason": "owner-pid-gone"}, (
        "nightly lease remains an active pause after owner abrupt exit"
    )
    print("PASS: abrupt owner exit is stale; exact marker preserved")


if __name__ == "__main__":
    main()
