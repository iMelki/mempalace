"""Process/boot identity and complete snapshot-lease publication contract."""

import os
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from mempalace import backup_snapshot, maintenance_identity


def test_filetime_preserves_native_100_nanosecond_precision():
    assert maintenance_identity._filetime_utc(1) == "1601-01-01T00:00:00.0000001Z"
    assert maintenance_identity._filetime_utc(10_000_007) == "1601-01-01T00:00:01.0000007Z"


@pytest.mark.skipif(os.name != "nt", reason="Windows CIM boot identity")
@pytest.mark.parametrize("stdout", ["", "wrong", "2026-10-09T00:00:00Z"])
def test_boot_probe_rejects_incomplete_identity(monkeypatch, stdout):
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=stdout))
    with pytest.raises(OSError, match="boot identity malformed"):
        maintenance_identity._windows_boot_id()


@pytest.mark.skipif(os.name != "nt", reason="Windows CIM boot identity")
def test_boot_probe_is_bounded_windowless_and_read_only(monkeypatch):
    def probe(args, **kwargs):
        assert args[:3] == ["pwsh", "-NoProfile", "-NonInteractive"]
        assert "Get-CimInstance -ClassName Win32_OperatingSystem" in args[-1]
        assert kwargs["timeout"] == 60
        assert kwargs["creationflags"] == subprocess.CREATE_NO_WINDOW
        assert kwargs["check"] is True
        return SimpleNamespace(stdout="2026-10-08T00:00:00.0000000Z\n")

    monkeypatch.setattr(subprocess, "run", probe)
    assert maintenance_identity._windows_boot_id() == "2026-10-08T00:00:00.0000000Z"


@pytest.mark.skipif(os.name != "nt", reason="Windows CIM boot identity")
@pytest.mark.parametrize("error", [OSError("missing pwsh"), subprocess.TimeoutExpired("pwsh", 60)])
def test_boot_lookup_failure_refuses_identity(monkeypatch, error):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(OSError, match="boot identity unavailable"):
        maintenance_identity.maintenance_owner_identity()


def test_identity_probe_failure_never_creates_a_marker(tmp_path, monkeypatch):
    def fail():
        raise OSError("injected owner identity unavailable")

    monkeypatch.setattr(backup_snapshot, "maintenance_owner_identity", fail)
    marker = tmp_path / ".maintenance"
    with pytest.raises(OSError, match="injected owner identity unavailable"):
        backup_snapshot._create_maintenance_marker(marker)
    assert not marker.exists()
    assert not list(tmp_path.glob(".maintenance.prepared-*"))


def test_snapshot_marker_records_all_identity_fields(tmp_path, monkeypatch):
    identity = {
        "leaseSchemaVersion": 1,
        "ownerPid": os.getpid(),
        "ownerProcessStartedAtUtc": "2026-10-09T00:00:00.0000001Z",
        "bootId": "2026-10-08T00:00:00.0000000Z",
    }
    monkeypatch.setattr(backup_snapshot, "maintenance_owner_identity", lambda: identity)
    marker = tmp_path / ".maintenance"
    backup_snapshot._create_maintenance_marker(marker)
    content = marker.read_bytes()
    assert content.startswith(b"mempalace backup-snapshot lease ")
    for key, value in identity.items():
        assert f"{key}={value}\r\n".encode() in content
    assert b"\n" not in content.replace(b"\r\n", b"")


def test_snapshot_marker_matches_shared_lease_parser(tmp_path, monkeypatch):
    consumer_root = os.environ.get("MEMSYS_MAINTENANCE_COMMON_PATH")
    if not consumer_root:
        pytest.skip("shared lease parser path not explicitly supplied")
    consumer = Path(consumer_root)
    powershell = shutil.which("pwsh")
    if not consumer.is_file() or powershell is None:
        pytest.fail("explicit shared lease parser path or pwsh is unavailable")
    identity = {
        "leaseSchemaVersion": 1,
        "ownerPid": os.getpid(),
        "ownerProcessStartedAtUtc": "2026-10-09T00:00:00.0000001Z",
        "bootId": "2026-10-08T00:00:00.0000000Z",
    }
    monkeypatch.setattr(backup_snapshot, "maintenance_owner_identity", lambda: identity)
    marker = tmp_path / ".maintenance"
    backup_snapshot._create_maintenance_marker(marker)
    script = (
        ". $args[0]; "
        "$lease=ConvertFrom-MemSysMaintenanceLease -Content "
        "(Get-Content -LiteralPath $args[1] -Raw); "
        "[ordered]@{ownerPid=$lease.ownerPid; "
        "ownerProcessStartedAtUtc=$lease.ownerProcessStartedAtUtc.UtcDateTime.ToString('o'); "
        "bootId=$lease.bootId.UtcDateTime.ToString('o')} | ConvertTo-Json -Compress"
    )
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-CommandWithArgs",
            script,
            str(consumer),
            str(marker),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
        **kwargs,
    )
    parsed = json.loads(result.stdout)
    assert parsed["ownerPid"] == identity["ownerPid"]
    assert parsed["ownerProcessStartedAtUtc"] == identity["ownerProcessStartedAtUtc"]
    assert parsed["bootId"] == identity["bootId"]


@pytest.mark.skipif(os.name != "nt", reason="Actual Windows native process/boot smoke")
def test_actual_windows_identity_matches_powershell_process_and_boot():
    identity = maintenance_identity.maintenance_owner_identity()
    script = (
        f"(Get-Process -Id {os.getpid()}).StartTime.ToUniversalTime().ToString('o'); "
        "(Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().ToString('o')"
    )
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.stdout.splitlines() == [identity["ownerProcessStartedAtUtc"], identity["bootId"]]
    assert identity["ownerPid"] == os.getpid()
