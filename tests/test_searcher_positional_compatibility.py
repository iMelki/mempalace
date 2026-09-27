"""Keep the established positional search API compatible with additive receipts."""

import pytest

from mempalace.searcher import search_memories


def test_eighth_positional_argument_still_selects_candidate_strategy(tmp_path):
    """The legacy eighth argument must reach strategy validation."""
    with pytest.raises(ValueError, match="candidate_strategy"):
        search_memories("query", str(tmp_path), None, None, 5, 0.0, False, "invalid")
