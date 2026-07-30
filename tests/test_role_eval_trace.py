import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest

import council_of_agents.scripts.role_eval as role_eval
from council_of_agents.scripts.role_eval import (
    build_messages,
    compare_trace_reports,
    compare_trace_suites,
    evaluate,
    evaluate_scenario_suite,
    evaluate_trace,
    _configured_api_key,
    _benchmark_fingerprint,
    _expected_scenario_outcome,
    _human_review_report,
)


def test_configured_credentials_are_opt_in_and_not_exposed():
    with patch(
        "src.endpoint_resolver.resolve_endpoint",
        return_value=(
            "https://opencode.ai/zen/v1/chat/completions",
            "mock",
            {"Authorization": "Bearer secret-token"},
        ),
    ):
        assert _configured_api_key("https://opencode.ai/zen/v1/chat/completions") == "secret-token"
        assert _configured_api_key("https://other.example/v1/chat/completions") == ""


CHAIR = json.dumps({
    "complexity": "MEDIUM", "route": "PIPELINE", "action": "write",
    "target": "feature", "reason": "bounded implementation",
})
CHAIR_AMBIGUOUS = json.dumps({
    "complexity": "COMPLEX", "route": "PIPELINE", "action": "write",
    "target": "storage", "reason": "Backend choice changes the implementation.",
    "ambiguous": True, "clarification": "Which storage backend should be used?",
    "options": ["SQLite", "PostgreSQL"],
})
STRATEGIST = json.dumps({
    "tasks": [{
        "id": "T1", "description": "Implement the feature", "acceptance": "Feature works",
        "write_scope": ["src/"],
    }],
})
PERSPECTIVE = json.dumps({
    "security": {"score": 0.9, "issues": []},
    "performance": {"score": 0.9, "issues": []},
    "maintainability": {"score": 0.9, "issues": []},
    "overall_score": 0.9, "synthesis": "The bounded plan is sound.",
})
PERSPECTIVE_BLOCK = json.dumps({
    "security": {"score": 0.2, "issues": [{"disposition": "BLOCK", "description": "Unsafe write scope.", "task_id": "T1", "evidence": "T1 allows a root-scoped write."}]},
    "performance": {"score": 0.9, "issues": []},
    "maintainability": {"score": 0.9, "issues": []},
    "overall_score": 0.5, "synthesis": "The plan has a hard safety finding.",
})
PERSPECTIVE_RECHECK = json.dumps({
    "security": {"score": 0.9, "issues": []},
    "performance": {"score": 0.9, "issues": []},
    "maintainability": {"score": 0.9, "issues": []},
    "overall_score": 0.9, "synthesis": "The revised plan's evidence was re-checked.",
})
MANAGER = json.dumps({
    "verdict": "APPROVED", "confidence": 0.9, "summary": "Plan is ready.", "issues": [],
})
MANAGER_REVISE = json.dumps({
    "verdict": "REVISE", "confidence": 0.4, "summary": "Add verification.",
    "issues": [{"severity": "warning", "task_id": "T1", "description": "Verification is missing.", "suggestion": "Add a focused verification command.", "evidence": "T1 has no verification field."}],
})
MANAGER_REVISE_2 = json.dumps({
    "verdict": "REVISE", "confidence": 0.5, "summary": "Add regression coverage.",
    "issues": [{"severity": "warning", "task_id": "T1", "description": "Regression coverage is missing.", "suggestion": "Add a focused regression test.", "evidence": "The revised plan has verification but no regression test task."}],
})
STRATEGIST_REVISION_1 = json.dumps({
    "tasks": [{
        "id": "T1", "description": "Implement the feature and add a focused verification command",
        "acceptance": "The feature works and the verification command passes.",
        "write_scope": ["src/"],
        "verification": {"adapter": "command", "config": {"argv": ["pytest", "-q"]}},
    }],
})
STRATEGIST_REVISION_2 = json.dumps({
    "tasks": [{
        "id": "T1", "description": "Implement the feature with verification and a regression test",
        "acceptance": "The feature works and the regression test prevents recurrence.",
        "write_scope": ["src/"],
        "verification": {"adapter": "command", "config": {"argv": ["pytest", "-q"]}},
    }],
})
UNGROUNDED_STRATEGIST = json.dumps({
    "tasks": [{
        "id": "T1", "description": "Build a new application from scratch.",
        "acceptance": "The new application works.", "write_scope": ["src/"],
    }],
})
GROUNDED_STRATEGIST = json.dumps({
    "tasks": [
        {
            "id": "T1",
            "description": "Inspect core/session_manager.py and the existing session regression tests for the message_count filter.",
            "acceptance": "The current failure path and smallest safe fix are identified.",
            "read_scope": ["core/", "tests/"],
            "write_scope": [],
        },
        {
            "id": "T2",
            "description": "Update the existing session loading condition in core/.",
            "acceptance": "Valid zero-message Council sessions remain visible after reload.",
            "read_scope": ["core/"],
            "write_scope": ["core/"],
            "verification": {"adapter": "command", "config": {"argv": ["pytest", "-q", "tests/"]}},
        },
        {
            "id": "T3",
            "description": "Add a regression test in tests/ for the zero-message Council session case.",
            "acceptance": "The regression test fails before the fix and passes after it.",
            "read_scope": ["core/", "tests/"],
            "write_scope": ["tests/"],
            "verification": {"adapter": "command", "config": {"argv": ["pytest", "-q", "tests/"]}},
        },
    ],
    "risks": ["Do not delete ordinary empty sessions while preserving Council sessions."],
})
GROUNDING_RUBRIC = {
    "minimum_tasks": 3,
    "required_read_scopes": ["core/", "tests/"],
    "required_write_scopes": ["core/", "tests/"],
    "require_inspection_task": True,
    "require_verification": True,
    "require_risks": True,
    "required_terms": ["inspect", "regression", "message_count"],
    "forbidden_terms": ["from scratch", "greenfield"],
}
IMPLEMENTER = json.dumps({
    "status": "DONE", "files_modified": ["src/feature.py"],
    "verification_details": "Targeted checks passed.", "notes": "Implemented T1.",
})
AUDIT = json.dumps({
    "completeness": 1.0, "done": True,
    "criteria": [{"id": "T1", "met": True, "gap_type": "fillable", "detail": "Feature works."}],
})


def test_all_role_messages_include_real_upstream_outputs():
    cases = {
        "perspective_analyzer": {"chair_reply": CHAIR, "strategist_reply": STRATEGIST},
        "manager": {"chair_reply": CHAIR, "strategist_reply": STRATEGIST, "perspective_reply": PERSPECTIVE},
        "implementer": {"chair_reply": CHAIR, "strategist_reply": STRATEGIST, "perspective_reply": PERSPECTIVE, "manager_reply": MANAGER},
        "completeness_auditor": {"strategist_reply": STRATEGIST, "manager_reply": MANAGER, "implementer_reply": IMPLEMENTER},
    }
    for role, handoffs in cases.items():
        messages = build_messages(role, "Build the feature.", **handoffs)
        prompt = "\n".join(message["content"] for message in messages)
        for output in handoffs.values():
            assert output in prompt


def test_schema_repair_is_bounded_and_recorded():
    responses = iter(["not json", CHAIR])

    async def fake_call(**_kwargs):
        return next(responses)

    result = asyncio.run(evaluate(
        "chair", "Route this feature.", endpoint="https://example.test/v1/chat/completions",
        model="mock", call=fake_call,
    ))

    assert result["contract_passed"] is True
    assert result["initial_contract_passed"] is False
    assert result["schema_repair_attempted"] is True
    assert result["schema_repair_succeeded"] is True
    assert result["garbage_recovery_attempted"] is True
    assert result["garbage_recovery_succeeded"] is True
    assert result["attempt_count"] == 2


def test_provider_failure_is_not_counted_as_garbage_recovery():
    async def failing_call(**_kwargs):
        raise RuntimeError("upstream unavailable")

    result = asyncio.run(evaluate(
        "chair", "Route this feature.", endpoint="https://example.test/v1/chat/completions",
        model="mock", call=failing_call,
    ))

    assert result["contract_passed"] is False
    assert result["provider_failed"] is True
    assert result["provider_error_count"] == 2
    assert result["provider_retry_limit"] == 1
    assert result["failure_kind"] == "provider_error"
    assert result["garbage_recovery_attempted"] is False


def test_provider_retry_is_fixed_visible_and_does_not_fallback():
    calls = []

    async def flaky_call(**kwargs):
        calls.append((kwargs["url"], kwargs["model"], kwargs["max_retries"], kwargs["timeout"]))
        if len(calls) == 1:
            raise RuntimeError("temporary provider failure")
        return CHAIR

    result = asyncio.run(evaluate(
        "chair", "Route this feature.", endpoint="https://example.test/v1/chat/completions",
        model="mock", call=flaky_call,
    ))

    assert result["contract_passed"] is True
    assert result["provider_failed"] is False
    assert result["provider_error_count"] == 1
    assert result["provider_retry_recovered"] is True
    assert calls == [
        ("https://example.test/v1/chat/completions", "mock", 1, 30),
        ("https://example.test/v1/chat/completions", "mock", 1, 30),
    ]


def test_provider_timeout_is_hard_bounded_and_recorded(monkeypatch):
    monkeypatch.setattr(role_eval, "_PROVIDER_CALL_TIMEOUT", 0.01)

    async def hanging_call(**_kwargs):
        await asyncio.sleep(1)

    result = asyncio.run(evaluate(
        "chair", "Route this feature.", endpoint="https://example.test/v1/chat/completions",
        model="mock", call=hanging_call,
    ))

    assert result["contract_passed"] is False
    assert result["provider_failed"] is True
    assert result["provider_error_count"] == 2
    assert all("exceeded 0.01s" in attempt["error"] for attempt in result["attempts"])


def test_trace_gate_separates_provider_inconclusive_from_quality_failure():
    async def fail_after_chair(**kwargs):
        if kwargs["trace_context"]["agent"] == "chair":
            return CHAIR
        raise RuntimeError("upstream unavailable")

    result = asyncio.run(evaluate_trace(
        "Build a small feature.",
        endpoint="https://example.test/v1/chat/completions",
        model="mock",
        scenario_id="provider-separation",
        planning_only=True,
        call=fail_after_chair,
    ))

    assert result["summary"]["provider_failures"] == 1
    assert result["readiness_gate"]["classification"] == "provider-inconclusive"
    assert result["readiness_gate"]["provider_inconclusive"] == ["strategist: provider failure"]
    assert result["readiness_gate"]["failures"] == []
    assert result["readiness_gate"]["passed"] is False


def test_complete_trace_timeout_emits_provider_inconclusive(monkeypatch):
    monkeypatch.setattr(role_eval, "_TRACE_TIMEOUT", 0.01)

    async def hanging_call(**_kwargs):
        await asyncio.sleep(1)

    result = asyncio.run(role_eval._bounded_evaluate_trace(
        "Build a small feature.",
        endpoint="https://example.test/v1/chat/completions",
        model="mock",
        scenario_id="trace-timeout",
        run_id="trace-timeout-run",
        planning_only=True,
        prompt_label="P0",
        call=hanging_call,
    ))

    assert result["summary"]["provider_failures"] == 1
    assert result["summary"]["termination_reason"] == "provider_timeout"
    assert result["readiness_gate"]["classification"] == "provider-inconclusive"
    assert result["readiness_gate"]["failures"] == []
    assert result["readiness_gate"]["provider_inconclusive"] == ["chair: provider failure"]
    assert result["run_id"] == "trace-timeout-run"


def test_suite_records_trace_timeout_as_provider_inconclusive(monkeypatch):
    monkeypatch.setattr(role_eval, "_TRACE_TIMEOUT", 0.01)

    async def hanging_call(**_kwargs):
        await asyncio.sleep(1)

    result = asyncio.run(evaluate_scenario_suite(
        {"case": {"user_prompt": "Build a small feature.", "split": "tuning"}},
        endpoint="https://example.test/v1/chat/completions",
        model="mock",
        planning_only=True,
        repetitions=1,
        call=hanging_call,
    ))

    assert result["summary"]["provider_inconclusive_cases"] == ["case"]
    assert result["readiness_gate"]["classification"] == "provider-inconclusive"
    assert result["trace_timeout_seconds"] == 0.01
    assert result["run_manifest"]["trace_timeout_seconds"] == 0.01


def test_suite_writes_progress_checkpoint_after_completed_case(tmp_path, monkeypatch):
    monkeypatch.setattr(role_eval, "_TRACE_TIMEOUT", 0.01)

    async def hanging_call(**_kwargs):
        await asyncio.sleep(1)

    checkpoint = tmp_path / "suite.progress.json"
    result = asyncio.run(evaluate_scenario_suite(
        {"case": {"user_prompt": "Build a small feature.", "split": "tuning"}},
        endpoint="https://example.test/v1/chat/completions",
        model="mock",
        planning_only=True,
        repetitions=1,
        run_id="checkpoint-run",
        checkpoint_path=checkpoint,
        call=hanging_call,
    ))

    progress = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert result["readiness_gate"]["classification"] == "provider-inconclusive"
    assert progress["kind"] == "role_eval_suite_progress"
    assert progress["run_id"] == "checkpoint-run"
    assert progress["completed_cases"] == 1
    assert progress["total_cases"] == 1
    assert progress["cases"][0]["provider_inconclusive"] is True


def test_planning_rubric_identifies_greenfield_grounding_failure():
    async def fake_call(**_kwargs):
        return UNGROUNDED_STRATEGIST

    result = asyncio.run(evaluate(
        "strategist",
        "Fix the existing session reload bug.",
        endpoint="https://example.test/v1/chat/completions",
        model="mock",
        chair_reply=CHAIR,
        scenario_rubric=GROUNDING_RUBRIC,
        call=fake_call,
    ))

    assert result["contract_passed"] is True
    assert result["semantic_quality"]["passed"] is False
    assert any(check["name"] == "inspection_task" and not check["passed"] for check in result["semantic_quality"]["checks"])
    assert result["prompt_version"]
    assert result["system_prompt_chars"] > 0


def test_planning_rubric_accepts_repository_grounded_plan():
    async def fake_call(**_kwargs):
        return GROUNDED_STRATEGIST

    result = asyncio.run(evaluate(
        "strategist",
        "Fix the existing session reload bug.",
        endpoint="https://example.test/v1/chat/completions",
        model="mock",
        chair_reply=CHAIR,
        scenario_rubric=GROUNDING_RUBRIC,
        call=fake_call,
    ))

    assert result["contract_passed"] is True
    assert result["semantic_quality"]["passed"] is True
    assert result["handoff_integrity"]["passed"] is True


def test_trace_runs_all_roles_and_reports_readiness_metrics():
    responses = iter([
        CHAIR, STRATEGIST, PERSPECTIVE, MANAGER_REVISE,
        STRATEGIST_REVISION_1, PERSPECTIVE_RECHECK, MANAGER,
        IMPLEMENTER, AUDIT,
    ])

    async def fake_call(**_kwargs):
        return next(responses)

    result = asyncio.run(evaluate_trace("Build the feature.", endpoint="https://example.test/v1/chat/completions", model="mock", call=fake_call))

    assert result["roles"] == [
        "chair", "strategist", "perspective_analyzer", "manager",
        "strategist", "perspective_analyzer", "manager",
        "implementer", "completeness_auditor",
    ]
    assert result["summary"]["stages_attempted"] == 9
    assert result["summary"]["provider_failures"] == 0
    assert result["summary"]["handoff_integrity"] == 1.0
    assert result["readiness_gate"]["passed"] is True


def test_primary_trace_uses_compact_contract_handoffs_and_retains_raw_outputs():
    responses = iter([CHAIR, STRATEGIST, PERSPECTIVE, MANAGER])

    async def fake_call(**_kwargs):
        return next(responses)

    result = asyncio.run(evaluate_trace(
        "Build the feature.",
        endpoint="https://example.test/v1/chat/completions",
        model="mock",
        planning_only=True,
        call=fake_call,
    ))

    manager_trace = next(record for record in result["trace"] if record["agent"] == "manager")
    manager_prompt = "\n".join(message["content"] for message in manager_trace["messages"])
    assert manager_trace["handoff_mode"] == "contract"
    assert result["outputs"]["strategist"] == STRATEGIST
    assert STRATEGIST not in manager_prompt
    assert result["summary"]["handoff_source_chars"] > result["summary"]["handoff_chars"]
    assert result["summary"]["handoff_compression_ratio"] < 1.0


def test_trace_prompt_source_changes_hash_for_causal_baseline_candidate_comparison(tmp_path):
    roles = ("chair", "strategist", "perspective_analyzer", "manager")
    baseline_dir = tmp_path / "baseline"
    candidate_dir = tmp_path / "candidate"
    baseline_dir.mkdir()
    candidate_dir.mkdir()
    for role in roles:
        (baseline_dir / f"{role}.md").write_text(f"{role} baseline prompt", encoding="utf-8")
        (candidate_dir / f"{role}.md").write_text(f"{role} candidate prompt", encoding="utf-8")

    async def run(prompts_dir, label):
        responses = iter([CHAIR, STRATEGIST, PERSPECTIVE, MANAGER])

        async def fake_call(**_kwargs):
            return next(responses)

        return await evaluate_trace(
            "Fix the existing feature.",
            endpoint="https://example.test/v1/chat/completions",
            model="mock",
            planning_only=True,
            prompt_label=label,
            prompts_dir=prompts_dir,
            call=fake_call,
        )

    baseline = asyncio.run(run(baseline_dir, "baseline"))
    candidate = asyncio.run(run(candidate_dir, "candidate"))
    comparison = compare_trace_reports(baseline, candidate)

    assert baseline["summary"]["prompt_sources"] == [str(baseline_dir.resolve())]
    assert candidate["summary"]["prompt_sources"] == [str(candidate_dir.resolve())]
    assert baseline["summary"]["prompt_versions"] != candidate["summary"]["prompt_versions"]
    assert comparison["hard_regressions"] == []
def test_manager_cannot_approve_current_perspective_block():
    async def fake_call(**_kwargs):
        return MANAGER

    result = asyncio.run(evaluate(
        "manager", "Review the plan.",
        endpoint="https://example.test/v1/chat/completions",
        model="mock",
        chair_reply=CHAIR,
        strategist_reply=STRATEGIST,
        perspective_reply=PERSPECTIVE_BLOCK,
        call=fake_call,
    ))

    assert result["contract_passed"] is True
    assert result["semantic_quality"]["passed"] is False
    assert any(
        check["name"] == "perspective_blocks_resolved" and not check["passed"]
        for check in result["semantic_quality"]["checks"]
    )


def test_handoff_integrity_rejects_manager_without_perspective_contract():
    async def fake_call(**_kwargs):
        return MANAGER

    result = asyncio.run(evaluate(
        "manager", "Review the plan.",
        endpoint="https://example.test/v1/chat/completions",
        model="mock",
        chair_reply=CHAIR,
        strategist_reply=STRATEGIST,
        call=fake_call,
    ))

    assert result["contract_passed"] is True
    assert result["handoff_integrity"]["passed"] is False
    assert "perspective_reply" in result["handoff_integrity"]["missing"]


def test_trace_runs_one_bounded_plan_revision_before_implementation():
    responses = iter([CHAIR, STRATEGIST, PERSPECTIVE, MANAGER_REVISE, STRATEGIST_REVISION_1, PERSPECTIVE_RECHECK, MANAGER, IMPLEMENTER, AUDIT])

    async def fake_call(**_kwargs):
        return next(responses)

    result = asyncio.run(evaluate_trace("Build the feature.", endpoint="https://example.test/v1/chat/completions", model="mock", call=fake_call))

    assert [record["stage"] for record in result["trace"]] == [
        "chair", "strategist", "perspective_analyzer", "manager",
        "strategist_revision", "perspective_revision", "manager_revision_review", "implementer", "completeness_auditor",
    ]
    assert result["summary"]["plan_revision_attempts"] == 1
    assert result["summary"]["plan_revision_successes"] == 1
    assert result["summary"]["perspective_rechecks"] == 1
    revision_manager = next(record for record in result["trace"] if record["stage"] == "manager_revision_review")
    revision_prompt = "\n".join(message["content"] for message in revision_manager["messages"])
    assert "The revised plan's evidence was re-checked." in revision_prompt
    assert "The bounded plan is sound." not in revision_prompt
    assert result["readiness_gate"]["passed"] is True


def test_trace_escalates_when_manager_skips_compulsory_plan_zero_challenge():
    responses = iter([CHAIR, STRATEGIST, PERSPECTIVE, MANAGER])

    async def fake_call(**_kwargs):
        return next(responses)

    result = asyncio.run(evaluate_trace(
        "Build the feature.",
        endpoint="https://example.test/v1/chat/completions",
        model="mock",
        planning_only=True,
        call=fake_call,
    ))

    manager_trace = next(record for record in result["trace"] if record["agent"] == "manager")
    manager_prompt = "\n".join(message["content"] for message in manager_trace["messages"])
    assert "compulsory Plan-0 challenge round" in manager_prompt
    assert result["summary"]["termination_reason"] == "compulsory_revision_not_requested"
    assert result["summary"]["human_escalation_required"] is True
    assert result["readiness_gate"]["passed"] is False


def test_trace_supports_two_revision_quality_loop_and_planning_only_mode():
    responses = iter([
        CHAIR, STRATEGIST, PERSPECTIVE, MANAGER_REVISE,
        STRATEGIST_REVISION_1, PERSPECTIVE_RECHECK, MANAGER_REVISE_2,
        STRATEGIST_REVISION_2, PERSPECTIVE_RECHECK, MANAGER,
    ])

    async def fake_call(**_kwargs):
        return next(responses)

    result = asyncio.run(evaluate_trace(
        "Fix the existing feature without rebuilding it.",
        endpoint="https://example.test/v1/chat/completions",
        model="mock",
        max_plan_revisions=2,
        planning_only=True,
        call=fake_call,
    ))

    assert [record["stage"] for record in result["trace"]] == [
        "chair", "strategist", "perspective_analyzer", "manager",
        "strategist_revision", "perspective_revision", "manager_revision_review",
        "strategist_revision_2", "perspective_revision_2", "manager_revision_review_2",
    ]
    assert result["summary"]["plan_revision_attempts"] == 2
    assert result["summary"]["perspective_rechecks"] == 2
    assert result["summary"]["plan_versions"] == 3
    assert result["summary"]["manager_decisions"] == 3
    assert result["summary"]["final_manager_verdict"] == "APPROVED"
    assert result["summary"]["planning_only"] is True
    assert len(result["summary"]["plan_revision_deltas"]) == 2
    assert result["decision_history"][0]["summary"] == "Add verification."
    assert result["decision_history"][0]["evidence_refs"] == ["T1 has no verification field."]
    assert result["decision_history"][-1]["evidence_refs"] == []
    assert result["readiness_gate"]["passed"] is True


def test_revision_scenario_allows_expected_baseline_defect_but_gates_final_plan():
    responses = iter([
        CHAIR, UNGROUNDED_STRATEGIST, PERSPECTIVE, MANAGER_REVISE,
        GROUNDED_STRATEGIST, PERSPECTIVE_RECHECK, MANAGER,
    ])

    async def fake_call(**_kwargs):
        return next(responses)

    result = asyncio.run(evaluate_trace(
        "Fix the existing session reload bug.",
        endpoint="https://example.test/v1/chat/completions",
        model="mock",
        max_plan_revisions=1,
        planning_only=True,
        scenario_rubric=GROUNDING_RUBRIC,
        call=fake_call,
    ))

    strategist_records = [record for record in result["trace"] if record["agent"] == "strategist"]
    assert strategist_records[0]["semantic_quality"]["passed"] is False
    assert strategist_records[-1]["semantic_quality"]["passed"] is True
    assert result["readiness_gate"]["passed"] is True


def test_trace_escalates_when_revision_makes_no_progress():
    responses = iter([CHAIR, STRATEGIST, PERSPECTIVE, MANAGER_REVISE, STRATEGIST, PERSPECTIVE_RECHECK, MANAGER_REVISE])

    async def fake_call(**_kwargs):
        return next(responses)

    result = asyncio.run(evaluate_trace(
        "Fix the existing feature without rebuilding it.",
        endpoint="https://example.test/v1/chat/completions",
        model="mock",
        max_plan_revisions=2,
        planning_only=True,
        call=fake_call,
    ))

    assert result["summary"]["termination_reason"] == "revision_no_progress"
    assert result["summary"]["human_escalation_required"] is True
    assert result["summary"]["plan_revision_attempts"] == 1
    assert result["decision_history"][-1]["revision_progress"] == {
        "plan_changed": False,
        "manager_defect_changed": False,
        "delta": result["decision_history"][-1]["revision_progress"]["delta"],
    }
    assert result["readiness_gate"]["passed"] is False


def test_trace_escalates_when_revision_budget_is_exhausted():
    responses = iter([CHAIR, STRATEGIST, PERSPECTIVE, MANAGER_REVISE, STRATEGIST_REVISION_1, PERSPECTIVE_RECHECK, MANAGER_REVISE_2])

    async def fake_call(**_kwargs):
        return next(responses)

    result = asyncio.run(evaluate_trace(
        "Fix the existing feature without rebuilding it.",
        endpoint="https://example.test/v1/chat/completions",
        model="mock",
        max_plan_revisions=1,
        planning_only=True,
        call=fake_call,
    ))

    assert result["summary"]["final_manager_verdict"] == "REVISE"
    assert result["summary"]["termination_reason"] == "revision_budget_exhausted"
    assert result["summary"]["human_escalation_required"] is True
    assert result["readiness_gate"]["passed"] is False
    assert result["readiness_gate"]["human_escalation_required"] is True
    assert all(record["agent"] not in {"implementer", "completeness_auditor"} for record in result["trace"])


def test_trace_uses_chair_as_grill_me_clarification_gate():
    responses = iter([CHAIR_AMBIGUOUS])

    async def fake_call(**_kwargs):
        return next(responses)

    result = asyncio.run(evaluate_trace(
        "Add persistent storage for analytics.",
        endpoint="https://example.test/v1/chat/completions",
        model="mock",
        planning_only=True,
        call=fake_call,
    ))

    assert [record["stage"] for record in result["trace"]] == ["chair"]
    assert result["summary"]["termination_reason"] == "clarification_required"
    assert result["summary"]["human_escalation_required"] is True
    assert result["readiness_gate"]["passed"] is False


def test_trace_comparison_blocks_quality_regression_and_reports_prompt_labels():
    baseline = {
        "summary": {
            "prompt_labels": ["baseline"], "contract_validity": 1.0,
            "handoff_integrity": 1.0, "semantic_quality": 0.5,
            "latency_ms": 100, "prompt_chars": 1000,
            "response_chars": 300, "provider_failures": 0,
            "final_manager_verdict": "APPROVED",
        },
        "readiness_gate": {"passed": True},
    }
    candidate = {
        "summary": {
            "prompt_labels": ["grounding-v1"], "contract_validity": 1.0,
            "handoff_integrity": 1.0, "semantic_quality": 0.8,
            "latency_ms": 125, "prompt_chars": 1200,
            "response_chars": 350, "provider_failures": 0,
            "final_manager_verdict": "APPROVED",
        },
        "readiness_gate": {"passed": True},
    }

    comparison = compare_trace_reports(baseline, candidate)

    assert comparison["baseline_label"] == "baseline"
    assert comparison["candidate_label"] == "grounding-v1"
    assert comparison["deltas"]["semantic_quality"] == 0.3
    assert comparison["hard_regressions"] == []
    assert comparison["quality_improved"] is True


def test_trace_comparison_separates_provider_inconclusive_from_quality_regression():
    baseline = {
        "summary": {
            "prompt_labels": ["P0"], "prompt_versions": ["p0"],
            "contract_validity": 1.0, "handoff_integrity": 1.0,
            "semantic_quality": 1.0, "provider_failures": 0,
        },
        "readiness_gate": {"passed": True},
    }
    candidate = {
        "summary": {
            "prompt_labels": ["P1"], "prompt_versions": ["p1"],
            "contract_validity": 0.0, "handoff_integrity": 1.0,
            "semantic_quality": 0.0, "provider_failures": 1,
        },
        "readiness_gate": {"passed": False, "classification": "provider-inconclusive"},
    }

    comparison = compare_trace_reports(baseline, candidate)

    assert comparison["classification"] == "provider-inconclusive"
    assert comparison["provider_inconclusive"] == ["candidate: provider failure"]
    assert comparison["hard_regressions"] == []
    assert comparison["quality_improved"] is False
    assert comparison["candidate_readiness"] is False


def test_trace_comparison_surfaces_harness_failure_as_exit_code_two_signal():
    comparison = compare_trace_reports(
        {"termination": "COMPLETE", "readiness_gate": {"passed": True}},
        {"termination": "HARNESS_FAILURE", "readiness_gate": {"passed": False}},
    )

    assert comparison["termination"] == "HARNESS_FAILURE"
    assert comparison["classification"] == "harness-failure"
    assert comparison["harness_failure"] == ["candidate: harness failure"]
    assert role_eval._exit_code(comparison) == 2


def test_scenario_suite_accepts_expected_clarification_and_reports_aggregates():
    async def fake_call(**kwargs):
        agent = kwargs["trace_context"]["agent"]
        prompt = "\n".join(message["content"] for message in kwargs["messages"] if message.get("role") == "user")
        if agent == "chair":
            return CHAIR_AMBIGUOUS if "persistent storage" in prompt else CHAIR
        if agent == "strategist":
            return STRATEGIST_REVISION_1 if "Manager feedback" in prompt else GROUNDED_STRATEGIST
        if agent == "perspective_analyzer":
            return PERSPECTIVE
        if agent == "manager":
            return MANAGER if "revision review" in prompt else MANAGER_REVISE
        raise AssertionError(f"Unexpected role in planning-only suite: {agent}")

    result = asyncio.run(evaluate_scenario_suite(
        {
            "grounded": {"user_prompt": "Fix the existing session reload bug."},
            "needs_clarification": {"user_prompt": "Add persistent storage for analytics.", "terminal": "clarification"},
        },
        endpoint="https://example.test/v1/chat/completions",
        model="mock",
        planning_only=True,
        call=fake_call,
    ))

    assert result["summary"]["case_count"] == 2
    assert result["summary"]["passed_cases"] == 2
    assert result["summary"]["human_escalations"] == 1
    assert result["human_review"]["sample_size"] == 2
    assert result["human_review"]["status"] == "pending"
    assert result["benchmark_fingerprint"] == _benchmark_fingerprint({
        "grounded": {"user_prompt": "Fix the existing session reload bug."},
        "needs_clarification": {"user_prompt": "Add persistent storage for analytics.", "terminal": "clarification"},
    })
    assert result["scenario_version"] == result["benchmark_fingerprint"]
    assert result["scenario_split"] == "all"
    assert result["run_manifest"]["provider_retry_limit"] == 1
    assert result["run_manifest"]["schema_repair_limit"] == 1
    assert result["run_manifest"]["planning_only"] is True
    assert result["run_manifest"]["role_settings"]["manager"]["model"] == "mock"
    assert result["run_manifest"]["model_fallbacks"] == 0
    assert result["readiness_gate"]["passed"] is True
    assert result["cases"][1]["actual_outcome"] == "clarification_required"


def test_scenario_suite_repeats_cases_and_separates_provider_inconclusive_runs():
    async def failing_call(**_kwargs):
        raise RuntimeError("provider unavailable")

    result = asyncio.run(evaluate_scenario_suite(
        {"one": {"user_prompt": "Inspect the existing feature."}},
        endpoint="https://example.test/v1/chat/completions",
        model="mock",
        planning_only=True,
        repetitions=3,
        call=failing_call,
    ))

    assert result["scenario_count"] == 1
    assert result["repetitions"] == 3
    assert result["summary"]["case_count"] == 3
    assert result["summary"]["failed_cases"] == []
    assert len(result["summary"]["provider_inconclusive_cases"]) == 3
    assert result["readiness_gate"]["passed"] is False
    assert result["readiness_gate"]["classification"] == "provider-inconclusive"


def test_human_review_sample_is_stable_and_annotations_are_bounded():
    first = _human_review_report(["a", "b", "c"], sample_size=2, seed="fixed")
    second = _human_review_report(["c", "a", "b"], sample_size=2, seed="fixed")
    reviewed = _human_review_report(
        ["a", "b", "c"],
        sample_size=2,
        seed="fixed",
        annotations={
            scenario_id: {
                "status": "reviewed",
                "plan_quality": "high",
                "manager_correctness": "correct",
                "handoff_quality": "sound",
                "comments": "verified against the rubric",
                "reviewer": "human",
            }
            for scenario_id in first["sampled_scenarios"]
        },
    )

    assert first["sampled_scenarios"] == second["sampled_scenarios"]
    assert first["status"] == "pending"
    assert reviewed["status"] == "complete"
    assert reviewed["score_summary"]["overall"] == 5.0
    assert all(len(item["comments"]) <= 4000 for item in reviewed["reviews"])
    incomplete = _human_review_report(
        ["a", "b", "c"],
        sample_size=2,
        seed="fixed",
        annotations={first["sampled_scenarios"][0]: {"status": "reviewed"}},
    )
    assert incomplete["status"] == "pending"
    invalid = _human_review_report(
        ["a"],
        sample_size=1,
        seed="fixed",
        annotations={"a": {
            "status": "reviewed",
            "plan_quality": 6,
            "manager_correctness": 5,
            "handoff_quality": 5,
        }},
    )
    assert invalid["status"] == "pending"


def test_terminal_scenario_requires_valid_semantics_not_only_termination_label():
    trace = {
        "summary": {"termination_reason": "manager_blocked"},
        "trace": [{
            "agent": "manager",
            "contract_passed": True,
            "semantic_quality": {"passed": False},
        }],
    }

    passed, actual = _expected_scenario_outcome({"terminal": "manager_blocked"}, trace)

    assert passed is False
    assert actual == "manager_blocked"


def test_suite_comparison_carries_case_regressions_and_aggregate_deltas():
    baseline = {
        "summary": {"prompt_labels": ["baseline"], "contract_validity": 1.0, "handoff_integrity": 1.0, "semantic_quality": 0.5, "planning_quality": 0.5, "latency_ms": 100, "prompt_chars": 1000, "response_chars": 300, "provider_failures": 0},
        "cases": [{
            "scenario_id": "bug_fix",
            "passed": True,
            "trace": {"summary": {"prompt_labels": ["baseline"], "contract_validity": 1.0, "handoff_integrity": 1.0, "semantic_quality": 0.5, "latency_ms": 100, "prompt_chars": 1000, "response_chars": 300, "provider_failures": 0, "final_manager_verdict": "APPROVED"}, "readiness_gate": {"passed": True}},
        }],
        "readiness_gate": {"passed": True},
    }
    candidate = {
        "summary": {"prompt_labels": ["grounding-v1"], "contract_validity": 1.0, "handoff_integrity": 1.0, "semantic_quality": 0.8, "planning_quality": 0.8, "latency_ms": 125, "prompt_chars": 1200, "response_chars": 350, "provider_failures": 0},
        "cases": [{
            "scenario_id": "bug_fix",
            "passed": True,
            "trace": {"summary": {"prompt_labels": ["grounding-v1"], "contract_validity": 1.0, "handoff_integrity": 1.0, "semantic_quality": 0.8, "latency_ms": 125, "prompt_chars": 1200, "response_chars": 350, "provider_failures": 0, "final_manager_verdict": "APPROVED"}, "readiness_gate": {"passed": True}},
        }],
        "readiness_gate": {"passed": True},
    }

    comparison = compare_trace_suites(baseline, candidate)

    assert comparison["candidate_label"] == "grounding-v1"
    assert comparison["deltas"]["planning_quality"] == 0.3
    assert comparison["hard_regressions"] == []
    assert comparison["quality_improved"] is True


def test_comparison_rejects_mislabeled_runs_with_identical_prompt_versions():
    baseline = {"summary": {"prompt_labels": ["baseline"], "prompt_versions": ["same-hash"]}}
    candidate = {"summary": {"prompt_labels": ["candidate"], "prompt_versions": ["same-hash"]}}

    comparison = compare_trace_reports(baseline, candidate)

    assert comparison["prompt_versions_changed"] is False
    assert "prompt labels changed but prompt versions did not" in comparison["hard_regressions"]


def test_comparison_rejects_model_or_handoff_confounds():
    baseline = {
        "summary": {
            "prompt_labels": ["baseline"],
            "prompt_versions": ["baseline-hash"],
            "handoff_modes": ["contract"],
            "model_assignments": {"manager": "mimo-v2.5-free"},
        }
    }
    candidate = {
        "summary": {
            "prompt_labels": ["candidate"],
            "prompt_versions": ["candidate-hash"],
            "handoff_modes": ["full"],
            "model_assignments": {"manager": "other-model"},
        }
    }

    comparison = compare_trace_reports(baseline, candidate)

    assert "handoff mode changed between baseline and candidate" in comparison["hard_regressions"]
    assert "model assignments changed between baseline and candidate" in comparison["hard_regressions"]


def test_suite_comparison_requires_completed_sampled_human_review():
    baseline = {"summary": {"prompt_labels": ["baseline"]}, "cases": []}
    candidate = {
        "summary": {"prompt_labels": ["candidate"]},
        "cases": [],
        "human_review": {"sample_size": 1, "status": "pending"},
        "readiness_gate": {"passed": True},
    }

    comparison = compare_trace_suites(baseline, candidate)

    assert "human review pending" in comparison["hard_regressions"]
    assert comparison["candidate_readiness"] is False


def test_suite_comparison_blocks_human_quality_regression():
    review = lambda score: {
        "status": "complete",
        "sample_size": 1,
        "score_summary": {
            "plan_quality": score,
            "manager_correctness": score,
            "handoff_quality": score,
            "overall": score,
        },
    }
    comparison = compare_trace_suites(
        {"summary": {}, "cases": [], "human_review": review(5), "readiness_gate": {"passed": True}},
        {"summary": {}, "cases": [], "human_review": review(4), "readiness_gate": {"passed": True}},
    )
    assert "human review overall decreased" in comparison["hard_regressions"]
    assert comparison["human_review_deltas"]["overall"] == -1.0
    assert comparison["candidate_readiness"] is False


def test_benchmark_fingerprint_is_order_independent_and_changes_with_rubric():
    first = {
        "bug_fix": {"user_prompt": "Fix it.", "planning_rubric": {"min_score": 2}},
        "audit": {"user_prompt": "Inspect it."},
    }
    reordered = {
        "audit": {"user_prompt": "Inspect it."},
        "bug_fix": {"planning_rubric": {"min_score": 2}, "user_prompt": "Fix it."},
    }
    changed = {
        **first,
        "bug_fix": {"user_prompt": "Fix it.", "planning_rubric": {"min_score": 3}},
    }

    assert _benchmark_fingerprint(first) == _benchmark_fingerprint(reordered)
    assert _benchmark_fingerprint(first) != _benchmark_fingerprint(changed)


def test_suite_comparison_rejects_experiment_confounds_and_reports_legacy_warnings():
    baseline = {
        "benchmark_fingerprint": "fixed-v1",
        "planning_only": True,
        "max_plan_revisions": 2,
        "human_review": {"sampling_seed": "role-eval", "sample_size": 2},
        "summary": {}, "cases": [],
    }
    candidate = {
        "benchmark_fingerprint": "fixed-v2",
        "planning_only": False,
        "max_plan_revisions": 3,
        "human_review": {"sampling_seed": "changed", "sample_size": 1},
        "summary": {}, "cases": [],
    }

    comparison = compare_trace_suites(baseline, candidate)

    assert "benchmark fingerprint changed between baseline and candidate" in comparison["hard_regressions"]
    assert "planning-only mode changed between baseline and candidate" in comparison["hard_regressions"]
    assert "plan revision budget changed between baseline and candidate" in comparison["hard_regressions"]
    assert "human review sampling seed changed between baseline and candidate" in comparison["hard_regressions"]
    assert "human review sample size changed between baseline and candidate" in comparison["hard_regressions"]
    legacy = compare_trace_suites({"summary": {}, "cases": []}, {"summary": {}, "cases": []})
    assert legacy["hard_regressions"] == []
    assert "benchmark fingerprint missing from one or both artifacts" in legacy["comparison_warnings"]


def test_exit_code_separates_complete_provider_and_harness_results():
    assert role_eval._exit_code({"termination": "COMPLETE", "readiness_gate": {"passed": True}}) == 0
    assert role_eval._exit_code({"termination": "PROVIDER_INCONCLUSIVE"}) == 1
    assert role_eval._exit_code({"termination": "HARNESS_FAILURE"}) == 2
    assert role_eval._exit_code({"readiness_gate": {"classification": "harness-failure"}}) == 2


def test_trace_budget_manifest_formula_matches_topology():
    assert role_eval._trace_budget_seconds(2, True) == 1260.0
    assert role_eval._trace_budget_formula(True) == "(4 + 3 × max_revisions) × 120 + 60"
    assert role_eval._trace_budget_formula(False) == "(6 + 3 × max_revisions) × 120 + 60"


def test_trace_budget_uses_the_largest_configured_role_timeout():
    role_configs = {"strategist": {"timeout": 90}, "chair": {"timeout": 30}}

    assert role_eval._trace_budget_seconds(2, True, role_configs) == 3660.0
    assert role_eval._trace_budget_formula(True, role_configs) == "(4 + 3 × max_revisions) × 360 + 60"


def test_phase_a_role_routing_uses_nvidia_for_strategist_and_manager_only():
    config_path = Path(__file__).resolve().parents[1] / "council_of_agents" / "config" / "models.json"
    roles = json.loads(config_path.read_text(encoding="utf-8"))["roles"]
    nvidia_endpoint = "https://integrate.api.nvidia.com/v1/chat/completions"
    zen_endpoint = "https://opencode.ai/zen/v1/chat/completions"

    assert roles["strategist"]["endpoint_url"] == nvidia_endpoint
    assert roles["strategist"]["model"] == "nvidia/nemotron-3-super-120b-a12b"
    assert roles["strategist"]["timeout"] == 90
    assert roles["strategist"]["context_fallbacks"][0]["model"] == "nvidia/nemotron-3-nano-30b-a3b"
    assert roles["manager"]["endpoint_url"] == nvidia_endpoint
    assert roles["manager"]["model"] == "nvidia/nemotron-3-super-120b-a12b"
    assert roles["manager"]["context_fallbacks"][0]["model"] == "nvidia/nemotron-3-nano-30b-a3b"
    assert roles["chair"]["endpoint_url"] == zen_endpoint
    assert roles["implementer"]["endpoint_url"] == zen_endpoint
    assert roles["perspective_analyzer"]["endpoint_url"] == zen_endpoint
    assert roles["completeness_auditor"]["endpoint_url"] == zen_endpoint


def test_async_harness_failure_preserves_checkpoint_cases(tmp_path):
    async def broken_workflow():
        raise ValueError("contract evaluator crashed")

    checkpoint = {
        "cases": [{"scenario_id": "completed-case", "passed": True}],
        "completed_cases": 1,
    }
    checkpoint_path = tmp_path / "test-role-eval.progress.json"
    role_eval._write_jsonl(checkpoint_path, checkpoint)
    result = role_eval._run_async_or_harness_failure(
        broken_workflow(),
        kind="role_eval_suite",
        run_id="harness-test",
        checkpoint_path=checkpoint_path,
    )

    assert result["termination"] == "HARNESS_FAILURE"
    assert result["failure_source"]["exception_type"] == "ValueError"
    assert result["partial_trace"] == checkpoint["cases"]
    assert result["checkpoint"]["completed_cases"] == 1


# --- Single named --scenario outcome gate (P2.3-R1) ---


def _chair_stage_record():
    return {
        "agent": "chair",
        "stage": "chair",
        "contract_passed": True,
        "semantic_quality": {"passed": True, "score": 1.0, "checks": []},
    }


def _run_named_scenario_cli(tmp_path, monkeypatch, scenarios, scenario_id, trace):
    cfg = tmp_path / "scenarios.json"
    cfg.write_text(json.dumps(scenarios), encoding="utf-8")
    out = tmp_path / "trace.json"
    monkeypatch.setattr(role_eval, "_load_role_configs", lambda *_a, **_k: {})
    monkeypatch.setattr(
        role_eval,
        "_resolve_api_key_for_endpoints",
        lambda *_a, **_k: pytest.fail("provider credential resolution must not run"),
    )

    async def fake_trace(*_a, **_k):
        return dict(trace)

    monkeypatch.setattr(role_eval, "_bounded_evaluate_trace", fake_trace)
    code = role_eval.main([
        "--trace",
        "--planning-only",
        "--scenario", scenario_id,
        "--scenarios-config", str(cfg),
        "--trace-out", str(out),
        "--case", "test-single-scenario",
    ])
    return code, json.loads(out.read_text(encoding="utf-8"))


def test_single_scenario_terminal_mismatch_exits_one(tmp_path, monkeypatch):
    scenarios = {"ambiguous_storage_choice": {
        "user_prompt": "Add persistent storage for the new analytics feature.",
        "terminal": "clarification",
    }}
    trace = {
        "termination": "COMPLETE",
        "trace": [
            _chair_stage_record(),
            {"agent": "strategist", "stage": "strategist", "contract_passed": True,
             "semantic_quality": {"passed": True}},
        ],
        "summary": {"termination_reason": "planning_approved", "final_manager_verdict": "APPROVED"},
        "readiness_gate": {"passed": True, "classification": "pass"},
    }

    code, artifact = _run_named_scenario_cli(tmp_path, monkeypatch, scenarios, "ambiguous_storage_choice", trace)

    assert code == 1
    outcome = artifact["scenario_outcome"]
    assert outcome["passed"] is False
    assert outcome["expected_terminal"] == "clarification"
    assert outcome["actual_outcome"] == "planning_approved"
    assert artifact["readiness_gate"]["passed"] is True


def test_single_scenario_expected_clarification_exits_zero(tmp_path, monkeypatch):
    scenarios = {"ambiguous_storage_choice": {
        "user_prompt": "Add persistent storage for the new analytics feature.",
        "terminal": "clarification",
    }}
    trace = {
        "termination": "COMPLETE",
        "trace": [_chair_stage_record()],
        "summary": {"termination_reason": "clarification_required"},
        "readiness_gate": {"passed": False, "classification": "failed"},
    }

    code, artifact = _run_named_scenario_cli(tmp_path, monkeypatch, scenarios, "ambiguous_storage_choice", trace)

    assert code == 0
    outcome = artifact["scenario_outcome"]
    assert outcome["passed"] is True
    assert outcome["expected_terminal"] == "clarification"
    assert outcome["actual_outcome"] == "clarification_required"
    assert artifact["readiness_gate"]["passed"] is False
    assert [record["agent"] for record in artifact["trace"]] == ["chair"]


def test_single_scenario_harness_failure_precedence_exits_two(tmp_path, monkeypatch):
    scenarios = {"ambiguous_storage_choice": {
        "user_prompt": "Add persistent storage for the new analytics feature.",
        "terminal": "clarification",
    }}
    trace = {
        "termination": "HARNESS_FAILURE",
        "trace": [],
        "summary": {},
        "readiness_gate": {"passed": False, "classification": "harness-failure"},
    }

    code, _artifact = _run_named_scenario_cli(tmp_path, monkeypatch, scenarios, "ambiguous_storage_choice", trace)

    assert code == 2


def test_single_scenario_provider_inconclusive_precedence_exits_one(tmp_path, monkeypatch):
    scenarios = {"ambiguous_storage_choice": {
        "user_prompt": "Add persistent storage for the new analytics feature.",
        "terminal": "clarification",
    }}
    trace = {
        "termination": "PROVIDER_INCONCLUSIVE",
        "trace": [],
        "summary": {"provider_failures": 1},
        "readiness_gate": {"passed": False, "classification": "provider-inconclusive"},
    }

    code, _artifact = _run_named_scenario_cli(tmp_path, monkeypatch, scenarios, "ambiguous_storage_choice", trace)

    assert code == 1


def test_single_scenario_nonterminal_compatibility(tmp_path, monkeypatch):
    scenarios = {"grounded": {"user_prompt": "Fix the existing session reload bug."}}
    passing_trace = {
        "termination": "COMPLETE",
        "trace": [_chair_stage_record()],
        "summary": {"termination_reason": "planning_approved", "final_manager_verdict": "APPROVED"},
        "readiness_gate": {"passed": True, "classification": "pass"},
    }

    code, artifact = _run_named_scenario_cli(tmp_path, monkeypatch, scenarios, "grounded", passing_trace)
    assert code == 0
    assert artifact["scenario_outcome"]["passed"] is True

    out2 = tmp_path / "trace2.json"

    async def failing_trace(*_a, **_k):
        return {
            "termination": "COMPLETE",
            "trace": [_chair_stage_record()],
            "summary": {"termination_reason": "planning_approved"},
            "readiness_gate": {"passed": False, "classification": "failed"},
        }

    monkeypatch.setattr(role_eval, "_bounded_evaluate_trace", failing_trace)
    code = role_eval.main([
        "--trace",
        "--planning-only",
        "--scenario", "grounded",
        "--scenarios-config", str(tmp_path / "scenarios.json"),
        "--trace-out", str(out2),
        "--case", "test-single-scenario-fail",
    ])
    artifact2 = json.loads(out2.read_text(encoding="utf-8"))
    assert code == 1
    assert artifact2["scenario_outcome"]["passed"] is False
