from council_of_agents.scripts.progress_policy import (
    ProgressPolicy,
    normalize_failure_signature,
)


def test_failure_signature_normalizes_paths_ids_and_numbers():
    first = normalize_failure_signature(
        "C:\\work\\a.py:123 Assertion 9 failed id deadbeef1234", "T1"
    )
    second = normalize_failure_signature(
        "D:\\other\\a.py:456 Assertion 77 failed id cafebabe9999", "T1"
    )
    assert first == second


def test_identical_failure_and_action_becomes_stagnant():
    policy = ProgressPolicy(stagnant_repeat_threshold=2)
    first = policy.build_diagnostic(
        task_id="T1", attempt=1, error="same failure", action="same action"
    )
    second = policy.build_diagnostic(
        task_id="T1", attempt=2, error="same failure", action="same action"
    )
    assert first.stagnant is False
    assert second.stagnant is True
    assert second.repeated_failure_count == 2
    assert second.do_not_repeat == ["same action"]


def test_new_artifact_and_revision_count_as_progress():
    policy = ProgressPolicy()
    policy.build_diagnostic(
        task_id="T1", attempt=1, error="same failure", action="first",
        workspace_revision="r1", artifact_ids=["a1"],
    )
    second = policy.build_diagnostic(
        task_id="T1", attempt=2, error="same failure", action="second",
        workspace_revision="r2", artifact_ids=["a1", "a2"],
    )
    assert second.progress_score > 0
    assert second.stagnant is False


def test_new_verified_criterion_prevents_false_stagnation():
    policy = ProgressPolicy()
    policy.build_diagnostic(
        task_id="T1", attempt=1, error="same failure", action="same", verified_count=0
    )
    second = policy.build_diagnostic(
        task_id="T1", attempt=2, error="same failure", action="same", verified_count=1
    )
    assert second.progress_score > 0
    assert second.stagnant is False

