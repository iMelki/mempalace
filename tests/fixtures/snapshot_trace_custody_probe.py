"""Prove interruption tests preserve an existing observer, using disposable markers."""

import importlib.util
import sys
from pathlib import Path

import pytest


def main():
    broken = "--broken" in sys.argv[1:]
    root = Path(sys.argv[-1])
    if root.exists():
        raise RuntimeError("proof requires fresh retained scratch")
    root.mkdir(parents=True)
    repo = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo))
    path = repo / "tests/test_snapshot_lease_release.py"
    spec = importlib.util.spec_from_file_location("lease_trace_tests", path)
    tests = importlib.util.module_from_spec(spec)
    source = path.read_text(encoding="utf-8")
    if broken:
        needle = "sys.settrace(previous_trace)"
        assert source.count(needle) == 2, "BREAK DID NOT APPLY: restore sites changed"
        source = source.replace(needle, "sys.settrace(None)")
    exec(compile(source, str(path), "exec"), tests.__dict__)
    patch = pytest.MonkeyPatch()
    tests.fixture_lock.__wrapped__(patch)
    try:
        cases = (
            (
                tests.test_interrupt_immediately_after_acquisition_releases,
                (KeyboardInterrupt, SystemExit),
            ),
            (
                tests.test_interrupt_at_acquisition_return_handoff_releases,
                (KeyboardInterrupt, SystemExit, OSError, TimeoutError),
            ),
        )
        count = 0
        for probe, failures in cases:
            for failure in failures:
                case = root / str(count)
                case.mkdir()
                tests.test_interrupt_probes_restore_trace_observer(case, probe, failure)
                count += 1
    finally:
        patch.undo()
    print(f"PASS: {count} interruption cases restore the observer and trace a later source call")


if __name__ == "__main__":
    main()
