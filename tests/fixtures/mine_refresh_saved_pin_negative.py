"""Remove only saved-pin enforcement and observe attributed refusal-test failures."""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest  # noqa: E402

from mempalace import mine_refresh_recovery  # noqa: E402


if __name__ == "__main__":
    tests = [
        "tests/test_mine_refresh_saved_pin.py::test_saved_recovery_rejects_corrupted_pin",
        "-q",
    ]
    if "--broken" in sys.argv:
        with patch.object(
            mine_refresh_recovery, "_verify_saved_recovery_ancestor", lambda *_: None
        ):
            result = pytest.main(tests)
    else:
        result = pytest.main(tests)
    raise SystemExit(result)
