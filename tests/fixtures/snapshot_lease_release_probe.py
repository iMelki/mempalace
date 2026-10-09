"""Bounded stdlib-only lease proof: no Chroma/model/store/snapshot work.

The actual snapshot module and clean_client_lease caller execute unchanged.
Only its palace lock dependency is replaced by a fixture lock. Scratch is
explicit, retained, and fresh. --broken removes release to prove the assertion.
"""

import os
import subprocess
import sys
import types
from contextlib import contextmanager
from pathlib import Path


def load_snapshot(legacy=False):
    repo = Path(__file__).resolve().parents[2]
    package = types.ModuleType("mempalace")
    package.__path__ = [str(repo / "mempalace")]
    palace = types.ModuleType("mempalace.palace")

    @contextmanager
    def lock(_):
        yield

    palace.mine_palace_lock = lock
    palace.MineAlreadyRunning = type("MineAlreadyRunning", (RuntimeError,), {})
    sys.modules["mempalace"] = package
    sys.modules["mempalace.palace"] = palace
    if legacy:
        source = subprocess.run(
            ["git", "show", "origin/dev:mempalace/backup_snapshot.py"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
            **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
        ).stdout
        backup_snapshot = types.ModuleType("mempalace.backup_snapshot")
        backup_snapshot.__package__ = "mempalace"
        backup_snapshot.__file__ = str(repo / "mempalace/backup_snapshot.py")
        sys.modules[backup_snapshot.__name__] = backup_snapshot
        exec(compile(source, backup_snapshot.__file__, "exec"), backup_snapshot.__dict__)
    else:
        from mempalace import backup_snapshot

    return backup_snapshot


def main():
    mode, directory = sys.argv[1:]
    root = Path(directory)
    if root.exists():
        raise RuntimeError("proof requires a fresh scratch directory")
    root.mkdir(parents=True)
    snapshot = load_snapshot(legacy=mode in ("--abrupt-legacy", "--legacy-interrupt"))
    if mode in ("--legacy-interrupt", "--restored-interrupt"):
        marker = root / ".maintenance"

        def interrupt(frame, event, arg):
            if event == "line" and frame.f_code is snapshot.clean_client_lease.__wrapped__.__code__:
                if frame.f_locals.get("marker_identity") is not None:
                    sys.settrace(None)
                    raise KeyboardInterrupt("gate-proof-post-acquisition-interrupt")
            return interrupt

        try:
            sys.settrace(interrupt)
            with snapshot.clean_client_lease(root / "palace", maintenance_marker=marker):
                raise AssertionError("interrupt did not apply before workload")
        except KeyboardInterrupt as exc:
            assert str(exc) == "gate-proof-post-acquisition-interrupt"
        finally:
            sys.settrace(None)
        assert not marker.exists(), "catchable post-acquisition interrupt leaked active lease"
        assert len(list(root.glob(".maintenance.released-*"))) == 1
        print("PASS: caught acquisition-boundary interrupt released exact object")
        return
    if mode == "--broken":
        original = snapshot._retain_owned_marker
        snapshot._retain_owned_marker = lambda *args, **kwargs: None
        assert snapshot._retain_owned_marker is not original, "BREAK DID NOT APPLY"
    elif mode in ("--abrupt", "--abrupt-legacy"):
        with snapshot.clean_client_lease(root / "palace", maintenance_marker=root / ".maintenance"):
            os._exit(124)  # Fixture-owned process; simulates non-unwinding native termination.
    elif mode not in ("--baseline", "--restored"):
        raise RuntimeError("unknown proof mode")
    for index, failure in enumerate(
        (None, RuntimeError, TimeoutError, KeyboardInterrupt, SystemExit)
    ):
        case = root / str(index)
        case.mkdir()
        marker = case / ".maintenance"
        caught = None
        try:
            with snapshot.clean_client_lease(case / "palace", maintenance_marker=marker):
                payload = marker.read_bytes()
                if failure:
                    raise failure("injected catchable exit")
        except BaseException as exc:
            caught = exc
        assert caught is None if failure is None else type(caught) is failure
        assert not marker.exists(), "owned active lease leaked on catchable exit"
        released = list(case.glob(".maintenance.released-*"))
        assert len(released) == 1, "exact release must retain its owned object, not unlink it"
        assert released[0].read_bytes() == payload
        if os.name == "nt":
            assert f"ownerPid={os.getpid()}\r\n".encode() in payload, "canonical identity missing"
            assert b"ownerProcessStartedAtUtc=" in payload and b"bootId=" in payload
    print("PASS: 5 catchable exits; exact object retained; canonical identity present")


if __name__ == "__main__":
    main()
