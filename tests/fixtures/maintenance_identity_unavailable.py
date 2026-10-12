"""Caller-faithful snapshot identity gate: broken probe fails, complete probe passes.

Invoke with --broken/--restored and a NEW retained scratch directory. No live
marker or palace is involved. The intentional exception gives the red exit code.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from mempalace import backup_snapshot  # noqa: E402


def incomplete_identity():
    raise OSError("gate-proof-owner-identity-unavailable")


def complete_identity():
    return {
        "leaseSchemaVersion": 1,
        "ownerPid": os.getpid(),
        "ownerProcessStartedAtUtc": "2026-10-09T00:00:00.0000001Z",
        "bootId": "2026-10-08T00:00:00.0000000Z",
    }


def main():
    mode, root = sys.argv[1:]
    marker = Path(root) / ".maintenance"
    if mode not in ("--broken", "--restored") or marker.exists():
        raise RuntimeError("gate proof requires a fresh isolated marker path")
    marker.parent.mkdir(parents=True, exist_ok=True)
    backup_snapshot.maintenance_owner_identity = (
        incomplete_identity if mode == "--broken" else complete_identity
    )
    assert backup_snapshot.maintenance_owner_identity is (
        incomplete_identity if mode == "--broken" else complete_identity
    ), "BREAK DID NOT APPLY"
    try:
        backup_snapshot._create_maintenance_marker(marker)
    except OSError:
        assert not marker.exists(), "failed identity published a marker"
        raise
    content = marker.read_text(encoding="utf-8")
    for key in complete_identity():
        assert f"{key}=" in content, f"missing lease field: {key}"
    print("gate-proof-restored-complete-identity-published")


if __name__ == "__main__":
    main()
