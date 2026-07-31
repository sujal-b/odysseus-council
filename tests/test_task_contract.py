from types import SimpleNamespace
from pathlib import Path

import pytest

from council_of_agents.scripts.council_orchestrator import CouncilOrchestrator
from council_of_agents.scripts.council_schemas import validate_agent_output
from council_of_agents.scripts.prompt_composer import PromptComposer
from council_of_agents.scripts.task_dag import (
    TaskContractError,
    TaskDAG,
    TaskFailureCategory,
    TaskNode,
    normalize_verification,
)
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


def test_failure_terminal_policy_allows_one_informed_retry():
    """A guard rejection (scope/channel violation) must be retryable once with
    the rejection carried, then terminal — not instantly fatal (regression:
    the vertical slice died whenever the model tripped the write guard on its
    first attempt, e.g. calling bash for pytest in guarded execution)."""
    from council_of_agents.scripts.workspace_revision import WorkspaceScopeError
    first_trip = WorkspaceScopeError("tool 'bash' is not compatible with guarded workspace execution")
    assert CouncilOrchestrator._failure_is_terminal(first_trip, 0) is False
    assert CouncilOrchestrator._failure_is_terminal(first_trip, 1) is True
    # Unclassified failures stay non-terminal (retry machinery decides).
    assert CouncilOrchestrator._failure_is_terminal(RuntimeError("misc"), 0) is False
    assert CouncilOrchestrator._failure_is_terminal(RuntimeError("misc"), 5) is False


def test_execution_retry_carries_manager_revise_feedback_verbatim():
    """A task-gate Manager REVISE must reach the implementer's retry with the
    actual rejection text, not just a hash (regression: the vertical slice
    failed when the implementer re-attempted a REVISE'd task without the
    manager's correction — only error_fingerprint was emitted)."""
    task = TaskNode(id="T1", description="Add health endpoint", write_scope=["src/"])
    rejection = ('Manager rejected task T1: {"verdict": "REVISE", '
                 '"summary": "File is WSGI, not Flask; route PATH_INFO == \'/health\'."}')
    retry = CouncilOrchestrator._build_execution_retry(
        task, rejection, TaskFailureCategory.HANDOFF_CORRUPTION.value)
    assert retry["error"] == rejection
    assert "PATH_INFO" in retry["error"]
    assert len(retry["error_fingerprint"]) == 16


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

def test_plan_contract_verification_shape_is_translated_into_work_packet():
    """Regression: the Strategist contract emits verification as
    {"type": "shell", "command": "..."} but WorkPacket consumes
    {adapter, config}; the bridge must translate without crashing."""
    dag = _dag([{
        "id": "T1",
        "description": "Add a health endpoint and a regression test",
        "acceptance": "pytest passes",
        "read_scope": ["src/", "tests/"],
        "write_scope": ["src/", "tests/"],
        "verification": {"type": "shell", "command": "pytest -q tests/test_app.py::test_health"},
    }])
    dag.seal_contracts()
    packet = dag.build_work_packet("T1")
    assert packet.verification is not None
    assert packet.verification.adapter == "command"
    assert packet.verification.config["argv"] == ["pytest", "-q", "tests/test_app.py::test_health"]

def test_file_verification_shape_is_translated():
    dag = _dag([{
        "id": "T1",
        "description": "Create the service module",
        "acceptance": "module exists",
        "read_scope": [],
        "write_scope": ["src/"],
        "verification": {"type": "file", "path": "src/app.py", "exists": True},
    }])
    packet = dag.build_work_packet("T1")
    assert packet.verification.adapter == "file"
    assert packet.verification.config["path"] == "src/app.py"

def test_canonical_adapter_shape_passes_through_unchanged():
    dag = _dag([{
        "id": "T1",
        "description": "Run the suite",
        "acceptance": "suite green",
        "read_scope": ["src/"],
        "write_scope": ["tests/"],
        "verification": {"adapter": "command", "config": {"argv": ["pytest", "-q"], "timeout_seconds": 120}},
    }])
    packet = dag.build_work_packet("T1")
    assert packet.verification.adapter == "command"
    assert packet.verification.config["argv"] == ["pytest", "-q"]

def test_unknown_verification_shape_degrades_to_none_instead_of_crashing():
    dag = _dag([{
        "id": "T1",
        "description": "Whatever",
        "acceptance": "done",
        "read_scope": [],
        "write_scope": ["src/"],
        "verification": {"verifier": "custom-eval", "payload": 42},
    }])
    packet = dag.build_work_packet("T1")
    assert packet.verification is None


def test_normalize_verification_drops_malformed_command():
    """Regression: a verification command with an unbalanced quote must be
    dropped (bounded degradation), never crash packet build (slice run 7:
    the shadow ledger sync hit this ValueError first)."""
    assert normalize_verification(
        {"type": "shell", "command": 'pytest "unclosed'}
    ) is None


def test_normalize_verification_keeps_valid_command():
    assert normalize_verification(
        {"type": "command", "command": 'pytest -q "test_x.py"'}
    ) == {"adapter": "command",
          "config": {"argv": ["pytest", "-q", "test_x.py"], "timeout_seconds": 600}}


def test_accumulated_writes_serialize_and_survive_checkpoint_overlay():
    """Regression: a task's guard-approved write record must survive DAG
    serialization and the checkpoint-restore overlay, so a retry (or a
    restored run) can prove its attributable diff without a redundant second
    write (slice run 11: T1 wrote src/app.py on attempt 1, then died on
    attempt 2 for zero fresh diff)."""
    dag = _dag([{"id": "T1", "description": "x", "read_scope": [], "write_scope": ["src/"]}])
    dag._nodes["T1"].accumulated_writes = {"src/app.py"}
    node = [n for n in dag.to_dict()["nodes"] if n["id"] == "T1"][0]
    assert node["accumulated_writes"] == ["src/app.py"]

    restored = _dag([{"id": "T1", "description": "x", "read_scope": [], "write_scope": ["src/"]}])
    current = restored._nodes["T1"]
    current.status = node["status"]
    current.base_hashes = node.get("base_hashes") or {}
    current.failure_category = node.get("failure_category")
    current.execution_retry = node.get("execution_retry")
    current.accumulated_writes = set(node.get("accumulated_writes") or [])
    assert current.accumulated_writes == {"src/app.py"}
