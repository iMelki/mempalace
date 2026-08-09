"""Explicit-only child-pytest fixture for safe failure-report integration."""


def test_prior_lock_activity_is_not_attributed_to_a_later_failure():
    from mempalace.palace import mine_lock

    with mine_lock("logical://fixture/prior-activity"):
        pass


def test_intentionally_fails_after_a_mine_lock_attempt():
    from mempalace.palace import mine_lock

    with mine_lock("logical://fixture/failure-diagnostics"):
        pass
    assert False, "synthetic-private-fixture-value"
