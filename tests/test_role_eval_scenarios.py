import asyncio
import json
from pathlib import Path

from council_of_agents.scripts.council_orchestrator import CouncilOrchestrator
from council_of_agents.scripts.prompt_composer import PromptComposer
from council_of_agents.scripts.role_eval import evaluate, main


SCENARIOS = json.loads(
    (Path(__file__).resolve().parents[1] / "council_of_agents/benchmarks/role_eval_scenarios.json")
    .read_text(encoding="utf-8")
)


def _reply(value):
    return value if isinstance(value, str) else json.dumps(value)


def _evaluate(agent, output, prompt, *, scenario_rubric=None, **handoffs):
    async def fake_call(**_kwargs):
        return _reply(output)

    return asyncio.run(evaluate(
        agent,
        prompt,
        endpoint="https://example.test/v1/chat/completions",
        model="mock",
        scenario_rubric=scenario_rubric,
        call=fake_call,
        **{key: _reply(value) for key, value in handoffs.items()},
    ))


def _run(case, agent, output, **handoffs):
    result = _evaluate(agent, output, case["user_prompt"], **handoffs)
    assert result["contract_passed"], f"{agent} contract failed: {result['error']}"
    assert result["output"] == _reply(output)
    assert result["request_chars"] > 0
    assert result["messages"][0]["role"] == "system"
    assert result["messages"][0]["content"].strip()
    return result


def _assert_handoff(result, marker, content=""):
    messages = result["messages"]
    assert any(
        marker in message["content"] and (not content or content in message["content"])
        for message in messages
    )


def _assert_dag(plan, expected_ready):
    dag, tasks = CouncilOrchestrator._task_dag_from_plan(_reply(plan))
    assert tasks
    assert [task.id for task in dag.get_ready_tasks()] == expected_ready
    return {task["id"] for task in tasks}


def _assert_issue_ids_exist(review, task_ids):
    assert all(issue["task_id"] == "ALL" or issue["task_id"] in task_ids for issue in review["issues"])


def test_mock_scenarios_exercise_real_prompts_handoffs_and_contract_gates():
    assert 5 <= len(SCENARIOS) <= 12

    for name, case in SCENARIOS.items():
        chair = _run(case, "chair", case["chair"])
        chair_data = json.loads(chair["output"])

        if case.get("terminal") == "clarification":
            assert chair_data["ambiguous"] is True
            assert chair_data["clarification"]
            assert len(chair_data["options"]) >= 2
            assert "strategist" not in case
            continue

        assert chair_data["route"] == "PIPELINE"
        assert chair_data["complexity"] in {"MEDIUM", "COMPLEX"}
        strategist = _run(case, "strategist", case["strategist"], chair_reply=chair["output"])
        _assert_handoff(strategist, "Chair decision data:", "complexity:")
        task_ids = _assert_dag(case["strategist"], case.get("expected_ready", ["T1"]))

        if "manager" not in case:
            continue

        manager = _run(
            case,
            "manager",
            case["manager"],
            chair_reply=chair["output"],
            strategist_reply=strategist["output"],
        )
        _assert_handoff(manager, "Strategist plan to review:", strategist["output"])
        review = json.loads(manager["output"])
        assert review["verdict"] in {"APPROVED", "REVISE", "BLOCKED"}
        _assert_issue_ids_exist(review, task_ids)

        if "revision" not in case:
            continue

        assert review["verdict"] == "REVISE"
        revision = _run(
            case,
            "strategist",
            case["revision"],
            chair_reply=chair["output"],
            strategist_reply=strategist["output"],
            manager_reply=manager["output"],
        )
        _assert_handoff(revision, "Previous Strategist plan:", strategist["output"])
        _assert_handoff(revision, "Manager feedback:", manager["output"])
        revised_task_ids = _assert_dag(case["revision"], case.get("revision_expected_ready", ["T1"]))

        final_manager = _run(
            case,
            "manager",
            case["final_manager"],
            chair_reply=chair["output"],
            strategist_reply=revision["output"],
        )
        _assert_handoff(final_manager, "Strategist plan to review:", revision["output"])
        final_review = json.loads(final_manager["output"])
        assert final_review["verdict"] == "APPROVED"
        _assert_issue_ids_exist(final_review, revised_task_ids)


def test_declared_scenario_rubrics_conform_to_fixture_plans():
    """Keep the fixed oracle honest as scenario plans evolve."""
    for name, case in SCENARIOS.items():
        rubric = case.get("planning_rubric")
        if not rubric:
            continue
        initial = _evaluate(
            "strategist",
            case["strategist"],
            case["user_prompt"],
            scenario_rubric=rubric,
            chair_reply=case["chair"],
        )
        if name == "manager_revision":
            assert not initial["semantic_quality"]["passed"]
            revised = _evaluate(
                "strategist",
                case["revision"],
                case["user_prompt"],
                scenario_rubric=rubric,
                chair_reply=case["chair"],
                strategist_reply=case["strategist"],
                manager_reply=case["manager"],
            )
            assert revised["semantic_quality"]["passed"]
        else:
            assert initial["semantic_quality"]["passed"], name


def test_adversarial_role_outputs_fail_at_the_strict_boundary():
    prompt = "Add a small feature."
    chair = _reply({
        "complexity": "SIMPLE",
        "route": "PIPELINE",
        "action": "write",
        "target": "feature",
        "reason": "small",
    })
    strategist = _reply({
        "tasks": [{"id": "T1", "description": "Add the feature."}],
    })

    prose_chair = _evaluate(
        "chair",
        "Here is the decision:\n" + chair,
        prompt,
    )
    assert not prose_chair["contract_passed"]

    missing_scope = _evaluate(
        "strategist",
        strategist,
        prompt,
        chair_reply=chair,
    )
    assert not missing_scope["contract_passed"]

    prose_manager = _evaluate(
        "manager",
        "```json\n{\"verdict\":\"APPROVED\",\"confidence\":1,\"summary\":\"ok\",\"issues\":[]}\n```",
        prompt,
        chair_reply=chair,
        strategist_reply=_reply({
            "tasks": [{"id": "T1", "description": "Add the feature.", "write_scope": ["src/"]}],
        }),
    )
    assert not prose_manager["contract_passed"]


def test_p1_chair_candidate_preserves_contract_and_default_to_action_rule():
    prompt_dir = Path(__file__).resolve().parents[1] / "data/council_agent_evals/phase-a/prompts/P1"
    live_variant = Path(__file__).resolve().parents[1] / "council_of_agents/prompts/variants/P1"
    for relative in (
        "chair/instructions.md",
        "chair/output_format.md",
        "chair/examples.md",
    ):
        assert (live_variant / relative).read_text(encoding="utf-8") == (prompt_dir / "fragments" / relative).read_text(encoding="utf-8")
    assert json.loads((live_variant / "manifest.json").read_text(encoding="utf-8"))["active"] is False
    graph = json.loads((prompt_dir.parents[1] / "prompt-version-graph.json").read_text(encoding="utf-8"))
    assert graph["versions"]["P1"]["live_variant_path"] == "council_of_agents/prompts/variants/P1"
    system_prompt = PromptComposer(prompt_dir).compose("chair")
    assert "requested outcome" in system_prompt
    assert "record the assumption" in system_prompt

    output = {
        "complexity": "MEDIUM",
        "route": "PIPELINE",
        "action": "write",
        "target": "flight tracker dashboard",
        "reason": "Proceed with a repository-compatible implementation; framework selection can be resolved during inspection.",
        "ambiguous": False,
        "clarification": "",
        "options": [],
    }

    async def fake_call(**_kwargs):
        return json.dumps(output)

    result = asyncio.run(evaluate(
        "chair",
        "Build a small flight tracker dashboard.",
        endpoint="https://example.test/v1/chat/completions",
        model="mock",
        prompts_dir=prompt_dir,
        call=fake_call,
    ))
    assert result["contract_passed"]
    assert json.loads(result["output"])["ambiguous"] is False


def test_cli_single_role_preserves_prompt_label_and_run_id(monkeypatch):
    captured = {}

    async def fake_evaluate(*_args, **kwargs):
        captured.update(kwargs)
        return {
            "agent": "chair",
            "model": "mock",
            "contract_passed": True,
            "duration_ms": 0,
            "failure_kind": "",
        }

    monkeypatch.setattr("council_of_agents.scripts.role_eval.evaluate", fake_evaluate)
    monkeypatch.setattr("council_of_agents.scripts.role_eval._write_jsonl", lambda *_args: None)

    assert main([
        "--agent", "chair",
        "--user", "Build a small dashboard.",
        "--endpoint", "https://example.test/v1/chat/completions",
        "--model", "mock",
        "--prompt-label", "P1",
        "--case", "cli-metadata-check",
    ]) == 0
    assert captured["prompt_label"] == "P1"
    assert captured["run_id"] == "cli-metadata-check"
