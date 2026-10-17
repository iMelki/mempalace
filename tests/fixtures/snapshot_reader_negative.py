"""Deliberately retain snapshot readers and prove native-view recovery fails."""

from pathlib import Path
import sys

import pytest


class BrokenReaderClosure:
    def pytest_runtest_setup(self, item):
        if item.name == "test_snapshot_readers_do_not_pin_a_broken_native_view":
            item.module.close_chroma_client = lambda *args, **kwargs: False


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    raise SystemExit(
        pytest.main(
            [
                "tests/test_mine_progress.py::test_snapshot_readers_do_not_pin_a_broken_native_view",
                "-q",
            ],
            plugins=[BrokenReaderClosure()],
        )
    )
