from council_of_agents.scripts.context_tracker import ContextTracker


def test_overlapping_reservations_cannot_oversubscribe_budget():
    tracker = ContextTracker("run-1", budget_tokens=2_000)

    first = tracker.reserve("implementer", input_tokens=500, output_tokens=700)
    second = tracker.reserve("strategist", input_tokens=500, output_tokens=700)

    assert first is not None
    assert second is None
    assert tracker.reserved_tokens == 1_200
    assert tracker.budget_remaining() == 800

    tracker.release(first)
    assert tracker.reserve("strategist", input_tokens=500, output_tokens=700) is not None


def test_protected_reserve_is_only_available_to_protected_work():
    tracker = ContextTracker(
        "run-2", budget_tokens=2_000, protected_reserve_tokens=600
    )

    assert tracker.available_for() == 1_400
    assert tracker.reserve(
        "implementer", input_tokens=1_401, output_tokens=0
    ) is None
    protected = tracker.reserve(
        "manager", input_tokens=1_401, output_tokens=0, allow_protected=True
    )
    assert protected is not None
    assert tracker.available_for(allow_protected=True) == 599


def test_unlimited_tracker_still_tracks_and_releases_reservations():
    tracker = ContextTracker("run-3")
    reservation = tracker.reserve("chair", input_tokens=10, output_tokens=20)

    assert reservation is not None
    assert tracker.reserved_tokens == 30
    assert tracker.budget_remaining() == -1

    tracker.release(reservation)
    tracker.record("chair", input_tokens=10, output_tokens=20)
    summary = tracker.get_usage_summary()
    assert summary["reserved_tokens"] == 0
    assert summary["total_tokens"] == 30
    assert summary["by_role"]["chair"]["calls"] == 1

