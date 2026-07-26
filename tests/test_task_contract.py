from types import SimpleNamespace
from pathlib import Path

import pytest

from council_of_agents.scripts.council_orchestrator import CouncilOrchestrator
from council_of_agents.scripts.council_schemas import validate_agent_output
from council_of_agents.scripts.prompt_composer import PromptComposer
from council_of_agents.scripts.task_dag import TaskContractError, TaskDAG, TaskNode
from src.llm_core import FinalContextContractError, _assert_required_contract


def _dag(tasks):
    return TaskDAG.from_task_list(tasks)


def test_sealed_contract_is_included_in_the_work_packet_and_detects_drift():
    dag = _dag([{
        "id": "T1",
        "description": "Implement the globe renderer",
        "acceptance": "A renderer module exists",
        "read_scope": ["src/"],
        "write_scope": ["src/"],
    }])

    dag.seal_contracts()
    packet = dag.build_work_packet("T1")
    assert packet.contract_hash == dag._nodes["T1"].contract_hash
    assert packet.workspace_root is False
    assert CouncilOrchestrator._handoff_contains_contract(
        packet, [{"role": "user", "content": packet.model_dump_json()}]
    )
    assert not CouncilOrchestrator._handoff_contains_contract(
        packet, [{"role": "user", "content": "missing work packet"}]
    )

    dag._nodes["T1"].description = "Write output.txt instead"
    with pytest.raises(TaskContractError, match="changed after approval"):
        dag.assert_contract("T1")


def test_contract_rejects_file_and_implicit_root_write_scopes():
    file_scope = _dag([{
        "id": "T1", "description": "x", "read_scope": [], "write_scope": ["src/a.py"],
    }])
    with pytest.raises(TaskContractError, match="directories ending"):
        file_scope.seal_contracts()

    root_scope = _dag([{
        "id": "T1", "description": "x", "read_scope": [], "write_scope": ["./"],
    }])
    with pytest.raises(TaskContractError, match="directories ending"):
        root_scope.seal_contracts()


def test_contract_validation_rejects_bad_scopes_before_approval_without_sealing():
    dag = _dag([{
        "id": "T1", "description": "x", "read_scope": [], "write_scope": ["src/a.py"],
    }])
    with pytest.raises(TaskContractError, match="directories ending"):
        dag.validate_contracts()
    assert dag._nodes["T1"].contract_hash == ""


def test_explicit_workspace_root_is_serial_and_preserved_in_contract():
    dag = _dag([
        {"id": "ROOT", "description": "scaffold", "read_scope": [], "write_scope": [], "workspace_root": True},
        {"id": "T2", "description": "component", "read_scope": ["src/"], "write_scope": ["src/"]},
    ])
    dag.seal_contracts()
    assert TaskDAG.tasks_conflict(dag._nodes["ROOT"], dag._nodes["T2"])
    assert dag.build_work_packet("ROOT").workspace_root is True


def test_workspace_root_is_serial_when_safe_parallelism_is_off(monkeypatch):
    dag = _dag([
        {"id": "ROOT", "description": "scaffold", "read_scope": [], "write_scope": [], "workspace_root": True},
        {"id": "T2", "description": "component", "read_scope": ["src/"], "write_scope": ["src/"]},
    ])
    monkeypatch.delenv("COUNCIL_SAFE_PARALLELISM", raising=False)
    assert [task.id for task in CouncilOrchestrator._execution_wave(dag, list(dag._nodes.values()))] == ["ROOT"]


def test_mutation_evidence_requires_an_attributable_diff_and_preserves_tool_diagnostics():
    task = TaskNode(id="T1", description="change source", write_scope=["src/"])
    before = SimpleNamespace(file_hashes={"src/app.js": "one"})
    unchanged = SimpleNamespace(file_hashes={"src/app.js": "one"})
    changed = SimpleNamespace(file_hashes={"src/app.js": "two"})

    error = CouncilOrchestrator._classify_task_evidence(
        task, before, unchanged, '```json\n{"files_created": []}\n```', set()
    )
    assert error is not None
    assert error.category.value == "zero_evidence_execution"
    assert CouncilOrchestrator._classify_task_evidence(
        task, before, unchanged, "src/app.js\n```js\ncode\n```", set()
    ) is None
    error = CouncilOrchestrator._classify_task_evidence(task, before, changed, "done", set())
    assert error.category.value == "tool_execution"
    assert CouncilOrchestrator._classify_task_evidence(task, before, changed, "done", {"src/app.js"}) is None


def test_final_provider_payload_requires_the_sealed_contract_hash():
    required = {"task_id": "T1", "contract_hash": "sealed-hash"}
    _assert_required_contract(
        {"messages": [{"role": "user", "content": '{"contract_hash":"sealed-hash"}'}]},
        {"required_contract": required},
    )
    with pytest.raises(FinalContextContractError, match="handoff_corruption"):
        _assert_required_contract({"messages": []}, {"required_contract": required})


def test_execution_retry_preserves_the_approved_task_and_changes_only_strategy():
    task = TaskNode(id="T1", description="Implement globe", acceptance="globe works", write_scope=["src/"])
    retry = CouncilOrchestrator._build_execution_retry(task, "no diff", "zero_evidence_execution")
    assert task.description == "Implement globe"
    assert retry["strategy"] == "write_immediately"
    assert "successful write" in retry["instruction"]


def test_pipeline_plan_requires_a_nonempty_valid_dag():
    assert not validate_agent_output("strategist", "Plan: make it better.").success


def test_strict_control_contract_rejects_json_wrapped_in_prose():
    reply = 'Here is the plan: {"tasks":[{"id":"T1","description":"x","depends_on":[],"acceptance":"x","write_scope":["src/"]}],"risks":[]}'
    assert validate_agent_output("strategist", reply).success
    assert not validate_agent_output("strategist", reply, strict=True).success
    with pytest.raises(ValueError, match="tasks"):
        CouncilOrchestrator._task_dag_from_plan("Plan: make it better.")

    dag, tasks = CouncilOrchestrator._task_dag_from_plan(
        '{"tasks":[{"id":"T1","description":"Create src/globe.ts","write_scope":["src/"]}]}'
    )
    assert [task.id for task in dag.get_ready_tasks()] == ["T1"]
    assert tasks[0]["id"] == "T1"


def test_strategist_schema_requires_explicit_directory_write_scopes():
    missing = validate_agent_output(
        "strategist", '{"tasks":[{"id":"T1","description":"Create the globe"}]}'
    )
    assert not missing.success
    assert "write_scope" in missing.error

    file_scope = validate_agent_output(
        "strategist", '{"tasks":[{"id":"T1","description":"Create the globe","write_scope":["src/App.tsx"]}]}'
    )
    assert not file_scope.success
    assert "directories ending" in file_scope.error

    implicit_root = validate_agent_output(
        "strategist", '{"tasks":[{"id":"T1","description":"Scaffold the project","write_scope":["./"]}]}'
    )
    assert not implicit_root.success

    explicit_root = validate_agent_output(
        "strategist", '{"tasks":[{"id":"T1","description":"Scaffold the project","write_scope":[],"workspace_root":true}]}'
    )
    assert explicit_root.success, explicit_root.error

    implicit_root = validate_agent_output(
        "strategist", '{"tasks":[{"id":"T1","description":"Scaffold the project","workspace_root":true}]}'
    )
    assert implicit_root.success, implicit_root.error

    valid = validate_agent_output(
        "strategist", '{"tasks":[{"id":"T1","description":"Create the globe","write_scope":["src/"]}]}'
    )
    assert valid.success, valid.error


def test_strategist_prompt_keeps_reasoning_private_and_scope_explicit():
    root = Path(__file__).resolve().parents[1]
    instructions = (root / "council_of_agents/prompts/fragments/strategist/instructions.md").read_text(encoding="utf-8")
    output_format = (root / "council_of_agents/prompts/fragments/strategist/output_format.md").read_text(encoding="utf-8")

    assert "Reason privately." in instructions
    assert "Do not restate the request" in instructions
    assert "never emit both fields together" in instructions
    assert '"workspace_root": true' in output_format
    composed = PromptComposer(root / "council_of_agents/prompts").compose("strategist")
    assert "read-first inspection task" in composed
    assert "include the relevant `read_scope`" in composed
    assert "Do not\ninclude `read_scope`" not in composed
