"""Prove that forgetting original preparation breaks pending-rewrite recovery."""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest  # noqa: E402

from mempalace.mine_refresh import PrefixRefreshJournal  # noqa: E402


if __name__ == "__main__":
    tests = [
        "tests/test_mine_refresh_recovery_interleavings.py",
        "-k",
        "prepared and further-edit",
        "-q",
    ]
    if "--broken" in sys.argv:
        original = PrefixRefreshJournal._accept_represented
        changes = []

        def forget_original(event, state):
            pending = state["pending"]
            original(event, state)
            if event.get("recovered_head") is not None and pending is not None:
                if state["recovery_pending"] != pending:
                    changes.append(event["event_digest"])
                state["recovery_pending"] = pending

        with patch.object(
            PrefixRefreshJournal, "_accept_represented", staticmethod(forget_original)
        ):
            result = pytest.main(tests)
        assert changes, "deliberate anchor loss never applied"
    else:
        result = pytest.main(tests)
    raise SystemExit(result)
