"""Disposable lease lifecycle tests; no production snapshot or process kill."""

import os
import json
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from mempalace import backup_snapshot as snapshot


@pytest.fixture(autouse=True)
def fixture_lock(monkeypatch):
    @contextmanager
    def lock(_):
        yield

    monkeypatch.setattr(snapshot, "mine_palace_lock", lock)


@pytest.mark.parametrize(
    "failure", [None, RuntimeError, TimeoutError, KeyboardInterrupt, SystemExit]
)
def test_every_catchable_exit_retains_owned_release(tmp_path, failure):
    marker = tmp_path / ".maintenance"
    caught = None
    try:
        with snapshot.clean_client_lease(tmp_path / "palace", maintenance_marker=marker):
            payload = marker.read_bytes()
            if failure:
                raise failure("injected catchable exit")
    except BaseException as exc:
        caught = exc
    assert caught is None if failure is None else type(caught) is failure
    assert not marker.exists(), "owned active lease leaked on catchable exit"
    released = list(tmp_path.glob(".maintenance.released-*"))
    assert len(released) == 1, "exact release must retain its owned object, not unlink it"
    assert released[0].read_bytes() == payload


def test_snapshot_marker_has_canonical_owner_identity(tmp_path):
    if os.name != "nt":
        pytest.skip("normal MemSys maintenance contract is Windows-only")
    marker = tmp_path / ".maintenance"
    with snapshot.clean_client_lease(tmp_path / "palace", maintenance_marker=marker):
        payload = marker.read_text()
        assert f"ownerPid={os.getpid()}\n" in payload, "snapshot lease lacks canonical ownerPid"
        assert "ownerProcessStartedAtUtc=" in payload
        assert "bootId=" in payload


def test_release_preserves_foreign_replacement(tmp_path):
    marker = tmp_path / ".maintenance"
    with pytest.raises(snapshot.PalaceSnapshotError, match="ownership changed"):
        with snapshot.clean_client_lease(tmp_path / "palace", maintenance_marker=marker):
            marker.rename(tmp_path / "retired")
            marker.write_text("foreign owner")
    assert marker.read_text() == "foreign owner"
    assert not list(tmp_path.glob(".maintenance.released-*"))


@pytest.mark.parametrize("failure", [KeyboardInterrupt, SystemExit])
def test_interrupt_immediately_after_acquisition_releases(tmp_path, failure):
    marker = tmp_path / ".maintenance"

    def interrupt(frame, event, arg):
        if event == "line" and frame.f_code is snapshot.clean_client_lease.__wrapped__.__code__:
            if frame.f_locals.get("marker_identity") is not None:
                sys.settrace(None)
                raise failure("injected immediately after acquisition")
        return interrupt

    try:
        sys.settrace(interrupt)
        with pytest.raises(failure, match="immediately after acquisition"):
            with snapshot.clean_client_lease(tmp_path / "palace", maintenance_marker=marker):
                pytest.fail("interrupt must occur before workload")
    finally:
        sys.settrace(None)
    assert not marker.exists(), "catchable post-acquisition interrupt leaked active lease"
    assert len(list(tmp_path.glob(".maintenance.released-*"))) == 1


@pytest.mark.parametrize("failure", [KeyboardInterrupt, SystemExit])
def test_interrupt_at_acquisition_return_handoff_releases(tmp_path, failure):
    marker = tmp_path / ".maintenance"

    def interrupt(frame, event, arg):
        if event == "return" and frame.f_code is snapshot._create_maintenance_marker.__code__:
            sys.settrace(None)
            raise failure("injected acquisition return handoff")
        return interrupt

    try:
        sys.settrace(interrupt)
        with pytest.raises(failure, match="acquisition return handoff"):
            with snapshot.clean_client_lease(tmp_path / "palace", maintenance_marker=marker):
                pytest.fail("interrupt must occur before ownership assignment")
    finally:
        sys.settrace(None)
    assert not marker.exists(), "catchable acquisition-return interrupt leaked lease"
    assert len(list(tmp_path.glob(".maintenance.released-*"))) == 1


def test_release_cannot_replace_path_between_proof_and_mutation(tmp_path, monkeypatch):
    if os.name != "nt":
        pytest.skip("native no-delete-sharing proof is Windows-only")
    marker = tmp_path / ".maintenance"
    attempts = []
    real_rename = snapshot.rename_owned_marker

    def attack(path, identity, destination, **kwargs):
        if path != marker:
            return real_rename(path, identity, destination, **kwargs)

        def before_rename():
            with pytest.raises(PermissionError):
                path.write_text("foreign content")
            try:
                path.rename(tmp_path / "stolen")
            except PermissionError:
                attempts.append("replacement blocked by held handle")
            else:
                path.write_text("foreign owner")
                pytest.fail("release let a foreign path replace its held object")

        return real_rename(path, identity, destination, before_rename=before_rename)

    monkeypatch.setattr(snapshot, "rename_owned_marker", attack)
    with snapshot.clean_client_lease(tmp_path / "palace", maintenance_marker=marker):
        pass
    assert attempts == ["replacement blocked by held handle"]
    assert not marker.exists()


def test_failed_exact_release_is_loud_and_preserves_complete_marker(tmp_path, monkeypatch):
    marker = tmp_path / ".maintenance"
    real_rename = snapshot.rename_owned_marker

    def deny(path, *args, **kwargs):
        if path == marker:
            raise PermissionError("injected release denied")
        return real_rename(path, *args, **kwargs)

    monkeypatch.setattr(snapshot, "rename_owned_marker", deny)
    with pytest.raises(snapshot.PalaceSnapshotError, match="release failed"):
        with snapshot.clean_client_lease(tmp_path / "palace", maintenance_marker=marker):
            payload = marker.read_bytes()
    assert marker.read_bytes() == payload
    assert not list(tmp_path.glob(".maintenance.released-*"))


def test_native_release_preserves_existing_destination(tmp_path):
    if os.name != "nt":
        pytest.skip("native no-replace rename is Windows-only")
    from mempalace.snapshot_lease_release import rename_owned_marker

    marker = tmp_path / ".maintenance"
    marker.write_bytes(b"owned")
    identity = marker.stat()
    destination = tmp_path / "occupied"
    destination.write_bytes(b"foreign")
    with pytest.raises(OSError):
        rename_owned_marker(marker, identity, destination)
    assert marker.read_bytes() == b"owned"
    assert destination.read_bytes() == b"foreign"


def test_publication_failure_after_link_retains_exact_object(tmp_path, monkeypatch):
    marker = tmp_path / ".maintenance"
    real_link = snapshot.os.link

    def fail_after_link(source, target):
        real_link(source, target)
        raise OSError("injected post-publication failure")

    monkeypatch.setattr(snapshot.os, "link", fail_after_link)
    with pytest.raises(snapshot.PalaceSnapshotError, match="cannot be raised"):
        with snapshot.clean_client_lease(tmp_path / "palace", maintenance_marker=marker):
            pytest.fail("failed acquisition granted lease")
    assert not marker.exists()
    assert len(list(tmp_path.glob(".maintenance.released-*"))) == 1


def test_abrupt_exit_is_stale_in_actual_shared_reader(tmp_path):
    if os.name != "nt":
        pytest.skip("native Windows identity contract")
    consumer = os.environ.get("MEMSYS_MAINTENANCE_COMMON_PATH")
    if not consumer:
        pytest.skip("shared reader bundle not supplied; cross-repo stale proof unrun")
    fixture = Path(__file__).parent / "fixtures/snapshot_lease_release_probe.py"
    root = tmp_path / "abrupt"
    child = subprocess.run(
        [sys.executable, "-B", str(fixture), "--abrupt", str(root)],
        capture_output=True,
        timeout=90,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert child.returncode == 124
    marker = root / ".maintenance"
    before = marker.read_bytes()
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
        timeout=90,
        check=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert json.loads(readback.stdout) == {
        "status": "stale",
        "active": False,
        "reason": "owner-pid-gone",
    }
    assert marker.read_bytes() == before, "stale reader must preserve the exact marker"
