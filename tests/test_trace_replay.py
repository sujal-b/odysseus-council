import json
import pathlib
import pytest

from council_of_agents.scripts.trace_replay import (
    replay_role_eval_trace,
    replay_trace_directory,
    run_target_role_canary,
    TraceReplayError,
    main,
    _build_structured_repair_signature,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
P2_3 = ROOT / "data/council_agent_evals/phase-a/prompts/P2.3"
TRACES_DIR = ROOT / "data/council_agent_evals/phase-a/traces"
TMP_DIR = ROOT / "scratch/test_tmp"
TMP_DIR.mkdir(parents=True, exist_ok=True)


def test_offline_replay_zero_network_calls(monkeypatch):
    def _fail_call(*args, **kwargs):
        pytest.fail("Offline replay attempted a network LLM call!")

    monkeypatch.setattr("council_of_agents.scripts.trace_replay.llm_call_async", _fail_call)

    c7_file = TRACES_DIR / "P2.3-mixed-provider-canary-07.json"
    if not c7_file.exists():
        pytest.skip("Canary 07 trace file not found.")

    res = replay_role_eval_trace(c7_file, prompts_dir=P2_3)
    assert res["replayed"] is True
    assert "source_trace_hash" in res
    assert "contract_compatibility" in res
    assert "workflow_compatibility" in res
    assert res["certification_status"] == "LIVE_REQUIRED"


def test_canary07_08_contract_compatibility_pass_workflow_compatibility_fail():
    c7_file = TRACES_DIR / "P2.3-mixed-provider-canary-07.json"
    c8_file = TRACES_DIR / "P2.3-mixed-provider-canary-08.json"
    if not c7_file.exists() or not c8_file.exists():
        pytest.skip("Canary 07 or 08 trace files not found.")

    res7 = replay_role_eval_trace(c7_file, prompts_dir=P2_3)
    assert res7["contract_compatibility"] == "PASS"
    assert res7["workflow_compatibility"] == "PASS_REPLAY"
    assert res7["certification_status"] == "LIVE_REQUIRED"

    res8 = replay_role_eval_trace(c8_file, prompts_dir=P2_3)
    assert res8["contract_compatibility"] == "PASS"
    assert res8["workflow_compatibility"] == "FAIL_REPLAY"
    assert res8["certification_status"] == "LIVE_REQUIRED"


def test_historical_repair_recorded_current_initial_passes_zero_repair_attempts():
    # Historical trace recorded attempt_count=2 and schema_repair attempt, but initial_raw_response validates directly
    valid_chair = {
        "target": "strategist",
        "reason": "actionable prompt",
        "action": "analyze",
        "route": "PIPELINE",
        "complexity": "MEDIUM",
        "ambiguous": False
    }

    dummy_trace = {
        "user_prompt": "Test prompt",
        "kind": "role_eval_trace",
        "harness_fingerprint": "matching_fp",
        "trace": [
            {
                "agent": "chair",
                "attempt_count": 2,
                "contract_passed": True,
                "initial_raw_response": json.dumps(valid_chair),
                "canonical_output": valid_chair,
                "attempts_detail": [
                    {"kind": "initial", "raw_response": json.dumps(valid_chair)},
                    {"kind": "schema_repair", "raw_response": json.dumps(valid_chair)},
                ],
            }
        ],
    }
    tf = TMP_DIR / "dummy_initial_pass_repair_recorded.json"
    tf.write_text(json.dumps(dummy_trace), encoding="utf-8")

    res = replay_role_eval_trace(tf, prompts_dir=P2_3)
    stage = res["trace"][0]
    assert stage["contract_passed"] is True
    assert stage["accepted_attempt"] == "initial"
    assert stage["repair_attempted"] is False
    assert stage["repair_succeeded"] is False
    assert len(stage["attempts_detail"]) == 1


def test_repaired_canonical_output_missing_source_canonical_output_downstream_grounding_succeeds():
    # Source trace records canonical_output: None for Strategist, but replayed Strategist output validates and grounds Perspective
    strat_output = {"tasks": [{"id": "T1", "description": "Task 1", "acceptance": "Acc 1", "write_scope": []}]}
    persp_output = {
        "security": {"score": 0.9, "issues": [{"task_id": "T1", "severity": "medium", "description": "Check auth"}]},
        "performance": {"score": 0.9, "issues": []},
        "maintainability": {"score": 0.9, "issues": []},
        "overall_score": 0.9,
        "synthesis": "Plan looks sound."
    }

    dummy_trace = {
        "user_prompt": "Test prompt",
        "kind": "role_eval_trace",
        "harness_fingerprint": "matching_fp",
        "trace": [
            {
                "agent": "strategist",
                "stage": "strategist",
                "attempt_count": 1,
                "contract_passed": True,
                "initial_raw_response": json.dumps(strat_output),
                "canonical_output": None,  # Missing source canonical output!
            },
            {
                "agent": "perspective_analyzer",
                "stage": "perspective_analyzer",
                "attempt_count": 1,
                "contract_passed": True,
                "initial_raw_response": json.dumps(persp_output),
                "canonical_output": persp_output,
            }
        ],
    }
    tf = TMP_DIR / "dummy_missing_source_canonical.json"
    tf.write_text(json.dumps(dummy_trace), encoding="utf-8")

    res = replay_role_eval_trace(tf, prompts_dir=P2_3)
    p_stage = res["trace"][1]
    assert p_stage["contract_passed"] is True
    assert p_stage["semantic_quality"]["passed"] is True


def test_two_validation_failures_different_schema_locations_distinct_signatures():
    sig1 = _build_structured_repair_signature(
        agent="perspective_analyzer",
        repair_kind="schema_repair",
        failure_kind="schema_invalid",
        contract_error="overall_score\n  Field required",
        metadata={"raw_shape": "canonical_envelope"}
    )

    sig2 = _build_structured_repair_signature(
        agent="perspective_analyzer",
        repair_kind="schema_repair",
        failure_kind="schema_invalid",
        contract_error="synthesis\n  Field required",
        metadata={"raw_shape": "canonical_envelope"}
    )

    assert sig1 != sig2
    assert "overall_score" in sig1
    assert "synthesis" in sig2


def test_passing_bounded_revision_workflow_produces_pass_replay():
    strat_v1 = {"tasks": [{"id": "T1", "description": "Initial task", "acceptance": "Acc", "write_scope": []}]}
    persp_v1 = {
        "security": {"score": 0.5, "issues": [{"task_id": "T1", "severity": "high", "description": "Unsafe"}]},
        "performance": {"score": 0.9, "issues": []},
        "maintainability": {"score": 0.9, "issues": []},
        "overall_score": 0.6,
        "synthesis": "Unsafe plan."
    }
    mgr_v1 = {
        "verdict": "REVISE",
        "reason": "Fix security issue",
        "summary": "Security issue detected in T1.",
        "confidence": 0.9,
        "issues": [{"task_id": "T1", "severity": "high", "description": "Unsafe key", "suggestion": "Use env var", "evidence": "Line 5"}]
    }

    strat_v2 = {"tasks": [{"id": "T1", "description": "Hardened task", "acceptance": "Acc", "write_scope": []}]}
    persp_v2 = {
        "security": {"score": 0.95, "issues": []},
        "performance": {"score": 0.95, "issues": []},
        "maintainability": {"score": 0.95, "issues": []},
        "overall_score": 0.95,
        "synthesis": "Plan is safe."
    }
    mgr_v2 = {
        "verdict": "APPROVED",
        "reason": "Looks good",
        "summary": "Plan approved after revision.",
        "confidence": 0.95,
        "issues": []
    }

    dummy_trace = {
        "user_prompt": "Test prompt",
        "kind": "role_eval_trace",
        "harness_fingerprint": "matching_fp",
        "planning_only": True,
        "trace": [
            {"agent": "chair", "attempt_count": 1, "contract_passed": True, "initial_raw_response": json.dumps({"target": "strategist", "reason": "actionable", "action": "analyze", "route": "PIPELINE", "complexity": "MEDIUM"}), "canonical_output": {"target": "strategist", "reason": "actionable", "action": "analyze", "route": "PIPELINE", "complexity": "MEDIUM"}},
            {"agent": "strategist", "stage": "strategist", "attempt_count": 1, "contract_passed": True, "initial_raw_response": json.dumps(strat_v1), "canonical_output": strat_v1},
            {"agent": "perspective_analyzer", "stage": "perspective_analyzer", "attempt_count": 1, "contract_passed": True, "initial_raw_response": json.dumps(persp_v1), "canonical_output": persp_v1},
            {"agent": "manager", "stage": "manager", "attempt_count": 1, "contract_passed": True, "initial_raw_response": json.dumps(mgr_v1), "canonical_output": mgr_v1},
            {"agent": "strategist", "stage": "strategist_revision", "attempt_count": 1, "contract_passed": True, "initial_raw_response": json.dumps(strat_v2), "canonical_output": strat_v2},
            {"agent": "perspective_analyzer", "stage": "perspective_revision", "attempt_count": 1, "contract_passed": True, "initial_raw_response": json.dumps(persp_v2), "canonical_output": persp_v2},
            {"agent": "manager", "stage": "manager_revision_review", "attempt_count": 1, "contract_passed": True, "initial_raw_response": json.dumps(mgr_v2), "canonical_output": mgr_v2},
        ],
    }
    tf = TMP_DIR / "dummy_revision_pass_trace.json"
    tf.write_text(json.dumps(dummy_trace), encoding="utf-8")

    res = replay_role_eval_trace(tf, prompts_dir=P2_3)
    assert res["contract_compatibility"] == "PASS"
    assert res["workflow_compatibility"] == "PASS_REPLAY"
    assert res["certification_status"] == "LIVE_REQUIRED"


def test_targeted_canary_preserves_provider_call_timeout_seconds_and_planning_rubric(monkeypatch):
    called_evaluate = []

    async def mock_evaluate(**kwargs):
        called_evaluate.append(kwargs)
        return {
            "run_id": kwargs.get("run_id"),
            "contract_passed": True,
            "semantic_quality": {"passed": True},
            "provider_failed": False,
            "readiness_gate": {"passed": True},
        }

    monkeypatch.setattr("council_of_agents.scripts.trace_replay.evaluate", mock_evaluate)
    monkeypatch.setattr("council_of_agents.scripts.trace_replay._resolve_api_key_for_endpoints", lambda eps: "mock_db_key")

    dummy_scenarios = {
        "approved_plan": {
            "planning_rubric": {"min_overall_score": 0.8}
        }
    }
    sf = TMP_DIR / "dummy_scenarios.json"
    sf.write_text(json.dumps(dummy_scenarios), encoding="utf-8")

    dummy_trace = {
        "user_prompt": "Test prompt",
        "kind": "role_eval_trace",
        "scenario_id": "approved_plan",
        "trace": [
            {
                "agent": "perspective_analyzer",
                "stage": "perspective_analyzer",
                "model": "nemotron-3-ultra-free",
                "endpoint": "https://integrate.api.nvidia.com/v1",
                "temperature": 0.1,
                "max_tokens": 1024,
                "provider_call_timeout_seconds": 60.0,
                "handoff_mode": "contract",
            }
        ],
    }
    tf = TMP_DIR / "dummy_timeout_rubric_trace.json"
    tf.write_text(json.dumps(dummy_trace), encoding="utf-8")

    run_target_role_canary(
        source_trace_path=tf,
        target_stage="perspective_analyzer",
        count=1,
        prompts_dir=P2_3,
        scenarios_config=sf,
        output_dir=TMP_DIR,
    )

    assert len(called_evaluate) == 1
    call = called_evaluate[0]
    assert call["timeout"] == 60.0
    assert call["scenario_rubric"] == {"min_overall_score": 0.8}


def test_cli_exit_code_taxonomy(monkeypatch):
    async def mock_pass(**kwargs):
        return {"contract_passed": True, "semantic_quality": {"passed": True}, "provider_failed": False}

    monkeypatch.setattr("council_of_agents.scripts.trace_replay.evaluate", mock_pass)
    monkeypatch.setattr("council_of_agents.scripts.trace_replay._resolve_api_key_for_endpoints", lambda eps: "key")

    dummy_trace = {
        "user_prompt": "Test prompt",
        "kind": "role_eval_trace",
        "trace": [{"agent": "perspective_analyzer", "stage": "perspective_analyzer", "model": "m", "endpoint": "e"}],
    }
    tf = TMP_DIR / "dummy_cli_taxonomy_trace.json"
    tf.write_text(json.dumps(dummy_trace), encoding="utf-8")

    assert main(["--role-canary", "perspective_analyzer", "--trace", str(tf), "--count", "1", "--prompts-dir", str(P2_3)]) == 0

    async def mock_provider_fail(**kwargs):
        return {"contract_passed": False, "semantic_quality": {"passed": False}, "provider_failed": True}

    monkeypatch.setattr("council_of_agents.scripts.trace_replay.evaluate", mock_provider_fail)
    assert main(["--role-canary", "perspective_analyzer", "--trace", str(tf), "--count", "1", "--prompts-dir", str(P2_3)]) == 1

    async def mock_quality_fail(**kwargs):
        return {"contract_passed": False, "semantic_quality": {"passed": False}, "provider_failed": False}

    monkeypatch.setattr("council_of_agents.scripts.trace_replay.evaluate", mock_quality_fail)
    assert main(["--role-canary", "perspective_analyzer", "--trace", str(tf), "--count", "1", "--prompts-dir", str(P2_3)]) == 1

    with pytest.raises(SystemExit) as exc_info:
        main(["--role-canary", "perspective_analyzer", "--trace", str(tf), "--count", "0", "--prompts-dir", str(P2_3)])
    assert exc_info.value.code == 2
