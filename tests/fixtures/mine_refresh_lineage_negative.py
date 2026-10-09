"""Prove the pending-refresh ancestor gate catches its deliberate removal."""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest  # noqa: E402

from mempalace import mine_refresh_recovery  # noqa: E402


def accept_unrelated_head(current, _state, verification_args):
    return mine_refresh_recovery._verify_head(current, verification_args)


if __name__ == "__main__":
    tests = [
        "tests/test_mine_refresh.py::"
        "test_pending_refresh_rejects_head_without_matching_refresh_ancestor",
        "tests/test_mine_refresh_saved_lineage.py",
        "-q",
    ]
    if "--broken" in sys.argv:
        with patch.object(mine_refresh_recovery, "recover_pending_head", accept_unrelated_head):
            result = pytest.main(tests)
    else:
        result = pytest.main(tests)
    raise SystemExit(result)
