import json
import pathlib
import pytest

import council_of_agents.scripts.trace_replay as trace_replay_mod
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


DOC_RUBRIC = {
    "minimum_tasks": 2,
    "required_read_scopes": ["docs/"],
    "required_write_scopes": ["docs/"],
    "require_inspection_task": True,
    "require_verification": True,
    "required_terms": ["Windows", "start", "stop", "status"],
    "forbidden_terms": ["from scratch", "greenfield"],
}

# Write-only plan: hits docs/ write scope and verification, but declares no docs/
# read_scope and has no read-only inspection task, so exactly two rubric checks fail.
DOC_PLAN = {
    "tasks": [
        {
            "id": "T1",
            "description": "Create docs/windows-commands.md covering Windows local development.",
            "depends_on": [],
            "acceptance": "File docs/windows-commands.md exists.",
            "read_scope": [],
            "write_scope": ["docs/"],
            "workspace_root": False,
            "verification": {"type": "shell", "command": "test -f docs/windows-commands.md"},
        },
        {
            "id": "T2",
            "description": "Document the start, stop, and status commands for Windows local development.",
            "depends_on": ["T1"],
            "acceptance": "All three commands are documented with examples.",
            "read_scope": [],
            "write_scope": ["docs/"],
            "workspace_root": False,
            "verification": {"type": "shell", "command": "git diff --check"},
        },
    ],
}


def _write_doc_fixtures(name, scenario_id="documentation_update", trace_dir=None):
    """Synthetic role_eval trace + scenarios config, both under project-owned TMP_DIR."""
    chair = {
        "target": "strategist",
        "reason": "focused documentation change",
        "action": "write",
        "route": "PIPELINE",
        "complexity": "MEDIUM",
    }
    perspective = {
        "security": {"score": 0.95, "issues": []},
        "performance": {"score": 0.95, "issues": []},
        "maintainability": {"score": 0.9, "issues": []},
        "overall_score": 0.93,
        "synthesis": "Documentation-only change is low risk.",
    }
    manager = {
        "verdict": "APPROVED",
        "reason": "Small documentation task",
        "summary": "The documentation task has a concrete acceptance check.",
        "confidence": 0.97,
        "issues": [],
    }

    def stage(agent, stage_name, payload):
        return {
            "agent": agent,
            "stage": stage_name,
            "attempt_count": 1,
            "contract_passed": True,
            "initial_raw_response": json.dumps(payload),
            "canonical_output": payload,
        }

    trace = {
        "user_prompt": "Document the Windows startup, stop, and status commands for local development.",
        "kind": "role_eval_trace",
        "harness_fingerprint": "synthetic_fp",
        "scenario_id": scenario_id,
        "planning_only": True,
        "trace": [
            stage("chair", "chair", chair),
            stage("strategist", "strategist", DOC_PLAN),
            stage("perspective_analyzer", "perspective_analyzer", perspective),
            stage("manager", "manager", manager),
        ],
    }

    out_dir = trace_dir or TMP_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    trace_file = out_dir / f"{name}_trace.json"
    scenarios_file = TMP_DIR / f"{name}_scenarios.json"
    trace_file.write_text(json.dumps(trace), encoding="utf-8")
    scenarios_file.write_text(
        json.dumps({"documentation_update": {"planning_rubric": DOC_RUBRIC}}), encoding="utf-8"
    )
    return trace_file, scenarios_file


def test_offline_cli_forwards_scenario_rubric_to_replay(monkeypatch):
    """--scenarios-config/--scenario-id must reach replay_role_eval_trace, not be dropped."""
    trace_file, scenarios_file = _write_doc_fixtures("rubric_forward")

    def _fail_call(*args, **kwargs):
        pytest.fail("Offline CLI replay attempted a provider call!")

    monkeypatch.setattr("council_of_agents.scripts.trace_replay.llm_call_async", _fail_call)
    monkeypatch.setattr("council_of_agents.scripts.trace_replay.evaluate", _fail_call)

    captured = {}
    real_replay = trace_replay_mod.replay_role_eval_trace

    def spy(trace_path, prompts_dir=None, scenario_rubric=None):
        captured["scenario_rubric"] = scenario_rubric
        return real_replay(trace_path, prompts_dir=prompts_dir, scenario_rubric=scenario_rubric)

    monkeypatch.setattr("council_of_agents.scripts.trace_replay.replay_role_eval_trace", spy)

    exit_code = main([
        "--trace", str(trace_file),
        "--role-eval-trace",
        "--prompts-dir", str(P2_3),
        "--scenarios-config", str(scenarios_file),
        "--scenario-id", "documentation_update",
        "--output", str(TMP_DIR / "rubric_forward_replay.json"),
    ])

    assert captured["scenario_rubric"] == DOC_RUBRIC

    result = json.loads((TMP_DIR / "rubric_forward_replay.json").read_text(encoding="utf-8"))
    planning = result["summary"]["planning_quality"]
    failed = {c["name"] for c in planning["checks"] if not c["passed"]}

    assert len(planning["checks"]) == 14
    assert planning["score"] == 0.857
    assert failed == {"read_scope:docs/", "inspection_task"}
    assert result["contract_compatibility"] == "PASS"
    assert result["workflow_compatibility"] == "FAIL_REPLAY"
    assert result["readiness_gate"]["passed"] is False
    assert exit_code == 1


def test_offline_cli_rejects_unresolvable_scenario(monkeypatch):
    trace_file, scenarios_file = _write_doc_fixtures("unresolvable")

    def _fail_replay(*args, **kwargs):
        pytest.fail("Replay ran despite an unresolvable scenario!")

    monkeypatch.setattr("council_of_agents.scripts.trace_replay.replay_role_eval_trace", _fail_replay)

    with pytest.raises(SystemExit) as exc_info:
        main([
            "--trace", str(trace_file),
            "--role-eval-trace",
            "--prompts-dir", str(P2_3),
            "--scenarios-config", str(scenarios_file),
            "--scenario-id", "no_such_scenario",
        ])
    assert exc_info.value.code == 2


def test_corpus_replay_applies_per_trace_scenario_rubric(monkeypatch):
    """--trace-dir must resolve each trace's rubric, not evaluate rubric-free."""
    corpus_dir = TMP_DIR / "corpus_rubric"
    for stale in corpus_dir.glob("*.json"):
        stale.unlink()
    trace_file, scenarios_file = _write_doc_fixtures("corpus_doc", trace_dir=corpus_dir)

    def _fail_call(*args, **kwargs):
        pytest.fail("Corpus replay attempted a provider call!")

    monkeypatch.setattr("council_of_agents.scripts.trace_replay.llm_call_async", _fail_call)
    monkeypatch.setattr("council_of_agents.scripts.trace_replay.evaluate", _fail_call)

    output = TMP_DIR / "corpus_rubric_replay.json"
    exit_code = main([
        "--trace-dir", str(corpus_dir),
        "--prompts-dir", str(P2_3),
        "--scenarios-config", str(scenarios_file),
        "--output", str(output),
    ])

    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["replayed_role_eval_traces"] == 1
    assert result["contract_compatibility_passes"] == 1
    assert result["contract_compatibility_failures"] == 0
    assert result["workflow_compatibility_passes"] == 0
    assert result["workflow_compatibility_failures"] == 1
    assert result["certification_live_required"] == 1

    nested = result["traces"][0]
    planning = nested["summary"]["planning_quality"]
    failed = {c["name"] for c in planning["checks"] if not c["passed"]}

    assert len(planning["checks"]) == 14
    assert planning["score"] == 0.857
    assert failed == {"read_scope:docs/", "inspection_task"}
    assert nested["readiness_gate"]["passed"] is False
    assert nested["contract_compatibility"] == "PASS"
    assert nested["workflow_compatibility"] == "FAIL_REPLAY"
    assert nested["certification_status"] == "LIVE_REQUIRED"
    assert exit_code == 1


def test_corpus_replay_fails_closed_on_unresolvable_rubric(monkeypatch):
    corpus_dir = TMP_DIR / "corpus_unresolved"
    for stale in corpus_dir.glob("*.json"):
        stale.unlink()
    _, scenarios_file = _write_doc_fixtures(
        "corpus_missing", scenario_id="scenario_not_in_config", trace_dir=corpus_dir
    )

    def _fail_replay(*args, **kwargs):
        pytest.fail("Corpus replay ran a trace with an unresolved rubric!")

    monkeypatch.setattr("council_of_agents.scripts.trace_replay.replay_role_eval_trace", _fail_replay)

    output = TMP_DIR / "corpus_unresolved_replay.json"
    exit_code = main([
        "--trace-dir", str(corpus_dir),
        "--prompts-dir", str(P2_3),
        "--scenarios-config", str(scenarios_file),
        "--output", str(output),
    ])

    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["replayed_role_eval_traces"] == 0
    assert result["workflow_compatibility_passes"] == 0
    assert result["workflow_compatibility_failures"] == 1
    assert result["contract_compatibility_failures"] == 1
    assert result["malformed_trace_artifacts"] == 1
    assert "scenario_not_in_config" in result["malformed"][0]["error"]
    assert exit_code != 0


def test_cli_exit_code_taxonomy(monkeypatch):
    prod_trace_dir = pathlib.Path("data/council_agent_evals/phase-a/traces")
    files_before = set(prod_trace_dir.glob("*.json"))

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

    assert main(["--role-canary", "perspective_analyzer", "--trace", str(tf), "--count", "1", "--prompts-dir", str(P2_3), "--output-dir", str(TMP_DIR)]) == 0

    async def mock_provider_fail(**kwargs):
        return {"contract_passed": False, "semantic_quality": {"passed": False}, "provider_failed": True}

    monkeypatch.setattr("council_of_agents.scripts.trace_replay.evaluate", mock_provider_fail)
    assert main(["--role-canary", "perspective_analyzer", "--trace", str(tf), "--count", "1", "--prompts-dir", str(P2_3), "--output-dir", str(TMP_DIR)]) == 1

    async def mock_quality_fail(**kwargs):
        return {"contract_passed": False, "semantic_quality": {"passed": False}, "provider_failed": False}

    monkeypatch.setattr("council_of_agents.scripts.trace_replay.evaluate", mock_quality_fail)
    assert main(["--role-canary", "perspective_analyzer", "--trace", str(tf), "--count", "1", "--prompts-dir", str(P2_3), "--output-dir", str(TMP_DIR)]) == 1

    with pytest.raises(SystemExit) as exc_info:
        main(["--role-canary", "perspective_analyzer", "--trace", str(tf), "--count", "0", "--prompts-dir", str(P2_3), "--output-dir", str(TMP_DIR)])
    assert exc_info.value.code == 2

    files_after = set(prod_trace_dir.glob("*.json"))
    assert files_before == files_after, f"New files created in prod trace dir: {files_after - files_before}"



def test_terminal_clarification_scenario_replays_with_none_rubric(monkeypatch):
    """Hermetic test proving:
    A. corpus replay accepts terminal clarification scenario without rubric, forwards scenario_rubric=None, counts as replayed (not malformed);
    B. single-trace CLI accepts the same case without parser error.
    """
    def _fail_call(*args, **kwargs):
        pytest.fail("Replay attempted a provider call!")

    monkeypatch.setattr("council_of_agents.scripts.trace_replay.llm_call_async", _fail_call)
    monkeypatch.setattr("council_of_agents.scripts.trace_replay.evaluate", _fail_call)

    chair_payload = {
        "complexity": "COMPLEX",
        "route": "PIPELINE",
        "action": "write",
        "target": "analytics persistence",
        "reason": "Storage choice changes implementation.",
        "ambiguous": True,
        "clarification": "Which storage backend?",
        "options": ["SQLite", "PostgreSQL"],
    }
    trace = {
        "user_prompt": "Add persistent storage for analytics feature.",
        "kind": "role_eval_trace",
        "scenario_id": "ambiguous_storage_choice",
        "harness_fingerprint": "synthetic_fp",
        "trace": [
            {
                "agent": "chair",
                "stage": "chair",
                "attempt_count": 1,
                "contract_passed": True,
                "initial_raw_response": json.dumps(chair_payload),
                "canonical_output": chair_payload,
            }
        ],
    }
    scenarios = {
        "ambiguous_storage_choice": {
            "split": "holdout",
            "user_prompt": "Add persistent storage.",
            "terminal": "clarification",
        }
    }

    corpus_dir = TMP_DIR / "corpus_terminal_clarification"
    for stale in corpus_dir.glob("*.json"):
        stale.unlink()
    corpus_dir.mkdir(parents=True, exist_ok=True)
    trace_file = corpus_dir / "term_clar_trace.json"
    scenarios_file = TMP_DIR / "term_clar_scenarios.json"
    trace_file.write_text(json.dumps(trace), encoding="utf-8")
    scenarios_file.write_text(json.dumps(scenarios), encoding="utf-8")

    # A. Test corpus replay
    output_corpus = TMP_DIR / "corpus_term_clar_replay.json"
    exit_code_corpus = main([
        "--trace-dir", str(corpus_dir),
        "--prompts-dir", str(P2_3),
        "--scenarios-config", str(scenarios_file),
        "--output", str(output_corpus),
    ])

    result_corpus = json.loads(output_corpus.read_text(encoding="utf-8"))
    assert result_corpus["replayed_role_eval_traces"] == 1
    assert result_corpus["malformed_trace_artifacts"] == 0
    assert result_corpus["traces"][0]["workflow_compatibility"] == "PASS_REPLAY"
    assert exit_code_corpus == 0

    # B. Test single-trace CLI replay
    output_single = TMP_DIR / "single_term_clar_replay.json"
    exit_code_single = main([
        "--trace", str(trace_file),
        "--role-eval-trace",
        "--prompts-dir", str(P2_3),
        "--scenarios-config", str(scenarios_file),
        "--output", str(output_single),
    ])

    result_single = json.loads(output_single.read_text(encoding="utf-8"))
    assert result_single["workflow_compatibility"] == "PASS_REPLAY"
    assert exit_code_single == 0


def test_rubricless_non_terminal_scenario_fails_closed(monkeypatch):
    """Rubric-less or empty-rubric scenario without terminal=='clarification' must fail closed."""
    for key in ("planning_rubric", "rubric"):
        ok_empty, rubric_empty, err_empty = trace_replay_mod.resolve_scenario_rubric(
            {"empty_rubric": {"split": "holdout", key: {}, "terminal": "manager_blocked"}},
            "empty_rubric",
        )
        assert ok_empty is False, f"empty {key} was accepted"
        assert rubric_empty is None
        assert "unresolved scenario rubric" in err_empty

    # The terminal-clarification exception still permits scenario_rubric=None.
    ok_term, rubric_term, err_term = trace_replay_mod.resolve_scenario_rubric(
        {"clarify": {"split": "holdout", "planning_rubric": {}, "terminal": "clarification"}},
        "clarify",
    )
    assert (ok_term, rubric_term, err_term) == (True, None, "")

    corpus_dir = TMP_DIR / "corpus_rubricless_invalid"
    for stale in corpus_dir.glob("*.json"):
        stale.unlink()
    corpus_dir.mkdir(parents=True, exist_ok=True)

    trace = {
        "user_prompt": "Do something",
        "kind": "role_eval_trace",
        "scenario_id": "invalid_rubricless",
        "trace": [],
    }
    scenarios = {
        "invalid_rubricless": {
            "split": "holdout",
            "terminal": "manager_blocked",
        }
    }
    trace_file = corpus_dir / "bad_trace.json"
    scenarios_file = TMP_DIR / "bad_scenarios.json"
    trace_file.write_text(json.dumps(trace), encoding="utf-8")
    scenarios_file.write_text(json.dumps(scenarios), encoding="utf-8")

    output = TMP_DIR / "bad_corpus_replay.json"
    exit_code = main([
        "--trace-dir", str(corpus_dir),
        "--prompts-dir", str(P2_3),
        "--scenarios-config", str(scenarios_file),
        "--output", str(output),
    ])

    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["replayed_role_eval_traces"] == 0
    assert result["malformed_trace_artifacts"] == 1
    assert "unresolved scenario rubric" in result["malformed"][0]["error"]
    assert exit_code != 0
