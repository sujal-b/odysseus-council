"""Deterministic Council benchmark metrics and rollout quality gates."""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path


class BenchmarkValidationError(ValueError):
    pass


@dataclass(frozen=True)
class BenchmarkCase:
    id: str
    category: str
    prompt: str
    required_behaviors: list[str]
    forbidden_outcomes: list[str]
    adapter_types: list[str]


@dataclass(frozen=True)
class BenchmarkObservation:
    case_id: str
    ledger_id: str
    ledger_version: int
    evidence_ids: list[str]
    declared_success: bool
    verified_complete: bool
    mandatory_criteria: int
    verified_criteria: int
    regression_escaped: bool = False
    tokens_used: int = 0
    duration_ms: int = 0
    repeated_failed_actions: int = 0
    total_failed_actions: int = 0
    human_interventions: int = 0
    context_retention_score: float = 1.0

    def __post_init__(self):
        if not self.ledger_id or self.ledger_version < 1:
            raise BenchmarkValidationError(f"{self.case_id}: missing ledger provenance")
        if self.mandatory_criteria < 1:
            raise BenchmarkValidationError(f"{self.case_id}: mandatory_criteria must be positive")
        if not 0 <= self.verified_criteria <= self.mandatory_criteria:
            raise BenchmarkValidationError(f"{self.case_id}: invalid verified criterion count")
        if not 0.0 <= self.context_retention_score <= 1.0:
            raise BenchmarkValidationError(f"{self.case_id}: invalid context retention score")
        if self.verified_complete and self.verified_criteria != self.mandatory_criteria:
            raise BenchmarkValidationError(
                f"{self.case_id}: verified_complete contradicts criterion coverage"
            )
        if self.verified_criteria and not self.evidence_ids:
            raise BenchmarkValidationError(f"{self.case_id}: verified criteria lack evidence provenance")

    @classmethod
    def from_dict(cls, value: dict):
        return cls(**value)


@dataclass(frozen=True)
class BenchmarkMetrics:
    case_count: int
    coverage: float
    verified_completion_rate: float
    false_success_rate: float
    regression_escape_rate: float
    evidence_coverage: float
    tokens_per_verified_criterion: float
    repeated_failed_action_rate: float
    context_retention_score: float
    human_intervention_rate: float


@dataclass(frozen=True)
class QualityGateConfig:
    min_coverage: float = 1.0
    max_false_success_rate: float = 0.0
    max_regression_escape_rate: float = 0.02
    min_evidence_coverage: float = 0.95
    min_context_retention_score: float = 0.95
    max_repeated_failed_action_rate: float = 0.10
    max_human_intervention_rate: float = 0.25
    require_significant_improvement: bool = True
    significance_alpha: float = 0.05


@dataclass(frozen=True)
class QualityGateReport:
    passed: bool
    candidate: BenchmarkMetrics
    baseline: BenchmarkMetrics | None
    paired_wins: int = 0
    paired_losses: int = 0
    improvement_p_value: float = 1.0
    failures: list[str] = field(default_factory=list)


def load_corpus(path: str | Path) -> list[BenchmarkCase]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or not isinstance(data.get("cases"), list):
        raise BenchmarkValidationError("unsupported or malformed benchmark corpus")
    cases = [BenchmarkCase(**item) for item in data["cases"]]
    ids = [case.id for case in cases]
    if len(ids) != len(set(ids)):
        raise BenchmarkValidationError("benchmark case IDs must be unique")
    if len(cases) < 30:
        raise BenchmarkValidationError("release corpus must contain at least 30 cases")
    for case in cases:
        if not all((case.id, case.category, case.prompt, case.required_behaviors, case.forbidden_outcomes, case.adapter_types)):
            raise BenchmarkValidationError(f"incomplete benchmark case: {case.id}")
    return cases


def load_observations(path: str | Path) -> list[BenchmarkObservation]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    values = data.get("observations") if isinstance(data, dict) else data
    if not isinstance(values, list):
        raise BenchmarkValidationError("observations must be a list")
    observations = [BenchmarkObservation.from_dict(item) for item in values]
    ids = [item.case_id for item in observations]
    if len(ids) != len(set(ids)):
        raise BenchmarkValidationError("duplicate case observations")
    return observations


def observation_from_ledger(
    case_id,
    ledger,
    *,
    declared_success,
    regression_escaped=False,
    tokens_used=0,
    duration_ms=0,
    repeated_failed_actions=0,
    total_failed_actions=0,
    human_interventions=0,
    context_retention_score=1.0,
):
    """Derive completion from current evidence; never trust a success claim."""
    criteria = [c for c in ledger.acceptance_criteria.values() if c.mandatory]
    verified = 0
    evidence_ids = []
    for criterion in criteria:
        if getattr(criterion.status, "value", criterion.status) != "verified":
            continue
        passing = [
            ledger.evidence.get(evidence_id) for evidence_id in criterion.evidence_ids
        ]
        passing = [
            evidence for evidence in passing
            if evidence is not None
            and evidence.passed
            and evidence.criterion_id == criterion.id
            and criterion.last_verified_revision
            and evidence.workspace_revision == criterion.last_verified_revision
        ]
        if passing:
            verified += 1
            evidence_ids.extend(evidence.id for evidence in passing)
    return BenchmarkObservation(
        case_id=case_id,
        ledger_id=ledger.ledger_id,
        ledger_version=ledger.version,
        evidence_ids=sorted(set(evidence_ids)),
        declared_success=bool(declared_success),
        verified_complete=bool(criteria) and verified == len(criteria),
        mandatory_criteria=len(criteria),
        verified_criteria=verified,
        regression_escaped=bool(regression_escaped),
        tokens_used=tokens_used,
        duration_ms=duration_ms,
        repeated_failed_actions=repeated_failed_actions,
        total_failed_actions=total_failed_actions,
        human_interventions=human_interventions,
        context_retention_score=context_retention_score,
    )


def compute_metrics(cases, observations) -> BenchmarkMetrics:
    case_ids = {case.id for case in cases}
    observed = {item.case_id: item for item in observations}
    unknown = set(observed) - case_ids
    if unknown:
        raise BenchmarkValidationError(f"unknown benchmark cases: {sorted(unknown)}")
    values = list(observed.values())
    count = len(values)
    total_criteria = sum(item.mandatory_criteria for item in values)
    verified_criteria = sum(item.verified_criteria for item in values)
    failed_actions = sum(item.total_failed_actions for item in values)
    repeated = sum(item.repeated_failed_actions for item in values)
    return BenchmarkMetrics(
        case_count=count,
        coverage=count / len(cases) if cases else 0.0,
        verified_completion_rate=sum(item.verified_complete for item in values) / count if count else 0.0,
        false_success_rate=sum(item.declared_success and not item.verified_complete for item in values) / count if count else 0.0,
        regression_escape_rate=sum(item.regression_escaped for item in values) / count if count else 0.0,
        evidence_coverage=verified_criteria / total_criteria if total_criteria else 0.0,
        tokens_per_verified_criterion=(sum(item.tokens_used for item in values) / verified_criteria if verified_criteria else 0.0),
        repeated_failed_action_rate=repeated / failed_actions if failed_actions else 0.0,
        context_retention_score=sum(item.context_retention_score for item in values) / count if count else 0.0,
        human_intervention_rate=sum(item.human_interventions > 0 for item in values) / count if count else 0.0,
    )


def _one_sided_sign_test(wins: int, losses: int) -> float:
    n = wins + losses
    if n == 0 or wins <= losses:
        return 1.0
    return sum(math.comb(n, k) for k in range(wins, n + 1)) / (2 ** n)


def evaluate_quality_gates(cases, candidate, baseline=None, config=None):
    config = config or QualityGateConfig()
    candidate_metrics = compute_metrics(cases, candidate)
    baseline_metrics = compute_metrics(cases, baseline) if baseline is not None else None
    failures = []
    checks = [
        (candidate_metrics.coverage >= config.min_coverage, "incomplete benchmark coverage"),
        (candidate_metrics.false_success_rate <= config.max_false_success_rate, "false-success rate exceeded"),
        (candidate_metrics.regression_escape_rate <= config.max_regression_escape_rate, "regression escape rate exceeded"),
        (candidate_metrics.evidence_coverage >= config.min_evidence_coverage, "evidence coverage below gate"),
        (candidate_metrics.context_retention_score >= config.min_context_retention_score, "context retention below gate"),
        (candidate_metrics.repeated_failed_action_rate <= config.max_repeated_failed_action_rate, "repeated failed-action rate exceeded"),
        (candidate_metrics.human_intervention_rate <= config.max_human_intervention_rate, "human intervention rate exceeded"),
    ]
    failures.extend(reason for passed, reason in checks if not passed)
    wins = losses = 0
    p_value = 1.0
    if baseline is not None:
        baseline_by_id = {item.case_id: item for item in baseline}
        candidate_by_id = {item.case_id: item for item in candidate}
        paired = set(baseline_by_id) & set(candidate_by_id)
        wins = sum(candidate_by_id[i].verified_complete and not baseline_by_id[i].verified_complete for i in paired)
        losses = sum(baseline_by_id[i].verified_complete and not candidate_by_id[i].verified_complete for i in paired)
        p_value = _one_sided_sign_test(wins, losses)
        if losses:
            failures.append("candidate regressed previously verified cases")
        if config.require_significant_improvement and p_value > config.significance_alpha:
            failures.append("verified completion improvement is not statistically significant")
    return QualityGateReport(
        passed=not failures, candidate=candidate_metrics, baseline=baseline_metrics,
        paired_wins=wins, paired_losses=losses, improvement_p_value=p_value,
        failures=failures,
    )


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--baseline")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    cases = load_corpus(args.corpus)
    candidate = load_observations(args.candidate)
    baseline = load_observations(args.baseline) if args.baseline else None
    report = evaluate_quality_gates(cases, candidate, baseline)
    output = json.dumps(asdict(report), indent=2, allow_nan=False)
    if args.output:
        Path(args.output).write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
