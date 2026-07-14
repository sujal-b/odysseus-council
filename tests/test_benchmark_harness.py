from pathlib import Path

import pytest

from council_of_agents.scripts.benchmark_harness import (
    BenchmarkObservation, BenchmarkValidationError, QualityGateConfig,
    compute_metrics, evaluate_quality_gates, load_corpus, observation_from_ledger,
)


CORPUS = Path(__file__).parents[1] / "council_of_agents" / "benchmarks" / "council_v1.json"


def _observation(case_id, *, complete=True, declared=None, regression=False):
    return BenchmarkObservation(
        case_id=case_id,
        ledger_id=f"ledger-{case_id}",
        ledger_version=1,
        evidence_ids=[f"evidence-{case_id}"] if complete else [],
        declared_success=complete if declared is None else declared,
        verified_complete=complete,
        mandatory_criteria=2,
        verified_criteria=2 if complete else 0,
        regression_escaped=regression,
        tokens_used=1000,
        total_failed_actions=1,
        repeated_failed_actions=0,
        context_retention_score=1.0,
    )


def test_release_corpus_is_versioned_and_has_required_breadth():
    cases = load_corpus(CORPUS)
    categories = {case.category for case in cases}
    assert len(cases) == 30
    assert {
        "false_success", "security", "crash_recovery", "parallel_conflict",
        "context_loss", "budget", "ambiguity",
    } <= categories


def test_metrics_measure_evidence_and_false_success_independently():
    cases = load_corpus(CORPUS)
    observations = [_observation(case.id) for case in cases]
    observations[0] = _observation(cases[0].id, complete=False, declared=True)

    metrics = compute_metrics(cases, observations)
    assert metrics.coverage == 1.0
    assert metrics.false_success_rate == pytest.approx(1 / 30)
    assert metrics.evidence_coverage == pytest.approx(58 / 60)


def test_false_success_is_a_release_blocker_even_with_high_completion():
    cases = load_corpus(CORPUS)
    observations = [_observation(case.id) for case in cases]
    observations[0] = _observation(cases[0].id, complete=False, declared=True)

    report = evaluate_quality_gates(
        cases, observations,
        config=QualityGateConfig(require_significant_improvement=False),
    )
    assert report.passed is False
    assert "false-success rate exceeded" in report.failures


def test_significant_paired_improvement_passes_without_regressions():
    cases = load_corpus(CORPUS)
    baseline = [
        _observation(case.id, complete=index >= 6)
        for index, case in enumerate(cases)
    ]
    candidate = [_observation(case.id) for case in cases]

    report = evaluate_quality_gates(cases, candidate, baseline)
    assert report.passed is True
    assert report.paired_wins == 6
    assert report.paired_losses == 0
    assert report.improvement_p_value == pytest.approx(0.015625)


def test_any_paired_verified_regression_blocks_rollout():
    cases = load_corpus(CORPUS)
    baseline = [_observation(case.id) for case in cases]
    candidate = [_observation(case.id) for case in cases]
    candidate[0] = _observation(cases[0].id, complete=False, declared=False)

    report = evaluate_quality_gates(cases, candidate, baseline)
    assert report.passed is False
    assert report.paired_losses == 1
    assert "candidate regressed previously verified cases" in report.failures


def test_observation_rejects_internally_inconsistent_success():
    with pytest.raises(BenchmarkValidationError, match="contradicts"):
        BenchmarkObservation.from_dict({
            "case_id": "x", "ledger_id": "ledger-x", "ledger_version": 1,
            "evidence_ids": ["e1"], "declared_success": True, "verified_complete": True,
            "mandatory_criteria": 2, "verified_criteria": 1,
        })


def test_observation_is_derived_from_current_ledger_evidence_not_claim():
    from council_of_agents.scripts.ledger_models import (
        AcceptanceCriterion, CriterionStatus, Evidence, RunLedger, VerificationSpec,
    )
    criterion = AcceptanceCriterion(
        id="AC-1", claim="tests pass", status=CriterionStatus.VERIFIED,
        verification=VerificationSpec(adapter="command"),
        evidence_ids=["e1"], last_verified_revision="new-revision",
    )
    stale = Evidence(
        id="e1", criterion_id="AC-1", adapter="command", verifier="pytest",
        passed=True, workspace_revision="old-revision",
    )
    ledger = RunLedger(
        session_id="s1", goal="test", version=3,
        acceptance_criteria={"AC-1": criterion}, evidence={"e1": stale},
    )

    observation = observation_from_ledger(
        "false-success-01", ledger, declared_success=True
    )
    assert observation.declared_success is True
    assert observation.verified_complete is False
    assert observation.verified_criteria == 0
