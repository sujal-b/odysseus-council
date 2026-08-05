import json

import pytest

from council_of_agents.scripts.council_schemas import (
    StrategistTask,
    validate_agent_output,
)
from council_of_agents.scripts.task_dag import (
    TaskDAG,
    mutation_only_plan_error,
    normalize_verification,
    verification_command_error,
)


CORRUPT_COMMAND = (
    "python -c \"import src.app; assert hasattr(src.app, 'health'); "
    "assert src.app.health() == {'status': 'ok'}\"}}, {"
)
VALID_COMMAND = (
    "python -c \"import src.app; assert hasattr(src.app, 'health'); "
    "assert src.app.health() == {'status': 'ok'}\""
)


def _plan_with(command: str) -> str:
    """Serialize a plan envelope the way the model's raw reply would carry it:
    the JSON escaping round-trip is what produced the quote-bleed (the model
    emitted `\"` inside the command value; json.loads kept the bare quote)."""
    return json.dumps({
        "tasks": [{
            "id": "T2",
            "description": "write health() to src/app.py",
            "write_scope": ["src/"],
            "verification": {"type": "shell", "command": command},
        }]
    })


def test_verification_command_error_detects_quote_bleed():
    reason = verification_command_error(CORRUPT_COMMAND)
    assert reason is not None
    assert "does not parse" in reason


def test_verification_command_error_detects_unbalanced_quote():
    assert "unbalanced quotes" in verification_command_error('pytest "unclosed')


def test_verification_command_error_accepts_valid_commands():
    assert verification_command_error(VALID_COMMAND) is None
    assert verification_command_error("pytest -q tests/test_app.py") is None
    assert verification_command_error("test -f src/models/user.py") is None


def test_normalize_verification_drops_quote_bleed_command():
    """Run-10 regression: the bleed command must never become argv, or the
    execution engine burns every retry on a python SyntaxError rc=1."""
    assert normalize_verification({"type": "shell", "command": CORRUPT_COMMAND}) is None
    assert normalize_verification({"type": "command", "command": CORRUPT_COMMAND}) is None


def test_normalize_verification_keeps_valid_python_c():
    result = normalize_verification({"type": "shell", "command": VALID_COMMAND})
    assert result == {
        "adapter": "command",
        "config": {"argv": ["python", "-c",
                            "import src.app; assert hasattr(src.app, 'health'); "
                            "assert src.app.health() == {'status': 'ok'}"],
                   "timeout_seconds": 600},
    }


def test_strategist_plan_with_bleed_verification_is_rejected():
    """The plan gate: a plan whose verification command cannot parse is
    rejected at ingestion (strict path, as the agent runner validates)."""
    validation = validate_agent_output("strategist", _plan_with(CORRUPT_COMMAND), strict=True)
    assert validation.success is False
    assert "verification command is malformed" in validation.error


def test_strategist_plan_with_bleed_verification_rejected_non_strict():
    """Non-strict path is how _task_dag_from_plan gates the plan."""
    validation = validate_agent_output("strategist", _plan_with(CORRUPT_COMMAND))
    assert validation.success is False


def test_strategist_plan_with_unbalanced_quote_verification_is_rejected():
    validation = validate_agent_output(
        "strategist", _plan_with('pytest "unclosed'), strict=True
    )
    assert validation.success is False
    assert "verification command is malformed" in validation.error


def test_strategist_plan_with_valid_verification_is_accepted():
    validation = validate_agent_output("strategist", _plan_with(VALID_COMMAND), strict=True)
    assert validation.success is True
    task = validation.data["tasks"][0]
    assert task["verification"]["command"] == VALID_COMMAND


def test_strategist_task_model_rejects_bleed_command():
    with pytest.raises(ValueError):
        StrategistTask.model_validate({
            "id": "T2",
            "write_scope": ["src/"],
            "verification": {"type": "shell", "command": CORRUPT_COMMAND},
        })


def test_corrupt_verification_never_reaches_work_packet():
    """Defense in depth: even a DAG built without the schema gate (e.g. a
    legacy resume path) drops the broken command at packet build instead of
    shipping garbage argv to execution."""
    dag = TaskDAG.from_task_list([{
        "id": "T2",
        "description": "write health() to src/app.py",
        "write_scope": ["src/"],
        "verification": {"type": "shell", "command": CORRUPT_COMMAND},
    }])
    packet = dag.build_work_packet("T2")
    assert packet.verification is None


MUTATION_ONLY_DIRECTIVE = (
    "Plan only file-modification tasks. "
    "Do not plan inspection-only test-execution tasks."
)


def test_mutation_only_directive_rejects_empty_write_scope():
    error = mutation_only_plan_error(
        [{"id": "T1", "write_scope": []}], MUTATION_ONLY_DIRECTIVE
    )

    assert error is not None
    assert "T1" in error


def test_mutation_only_directive_accepts_a_write_scope():
    assert mutation_only_plan_error(
        [{"id": "T1", "write_scope": ["src/"]}], MUTATION_ONLY_DIRECTIVE
    ) is None


def test_mutation_only_policy_allows_ordinary_inspection():
    assert mutation_only_plan_error(
        [{"id": "T1", "write_scope": []}], "Inspect the repository before planning."
    ) is None
