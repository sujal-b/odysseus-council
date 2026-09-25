"""Plan validation auto-repairs file-creating tasks with empty write_scope.

Regression: a task like T2 {"description": "Create notes.py implementing CRUD
for JSON file notes.", "write_scope": []} passed plan validation and then
burned a Manager REVISE cycle ("T2's write_scope is empty... Add 'notes.py'").
The plan-acceptance path must repair the unambiguous case itself and return a
targeted revision request only for genuinely ambiguous ones.
"""

import json

import pytest

from council_of_agents.scripts.agent_runner import _strategist_plan_error
from council_of_agents.scripts.council_orchestrator import CouncilOrchestrator
from council_of_agents.scripts.task_dag import (
    TaskDAG,
    file_write_scope_error,
    repair_file_write_scopes,
)


def _t2():
    return {
        "id": "T2",
        "description": "Create notes.py implementing CRUD for JSON file notes.",
        "write_scope": [],
    }


def test_t2_root_file_repairs_to_canonical_root_form_and_validates():
    """The exact flaw shape: root file + empty scope repairs in place and the
    resulting DAG passes contract validation first time (no Manager REVISE)."""
    tasks = [_t2()]
    assert file_write_scope_error(tasks) is None
    # File scopes ("./notes.py") and "./" are rejected by contract, so the
    # canonical workspace-contained form for a root file is workspace_root.
    assert tasks[0].get("workspace_root") is True
    assert tasks[0]["write_scope"] == []
    dag = TaskDAG.from_task_list(tasks)
    dag.validate_contracts()  # must not raise: passes first time
    dag.seal_contracts()


def test_nested_file_repairs_to_parent_directory_scope():
    tasks = [{
        "id": "T1",
        "description": "Create src/notes.py implementing CRUD for JSON file notes.",
        "write_scope": [],
    }]
    assert file_write_scope_error(tasks) is None
    assert tasks[0]["write_scope"] == ["src/"]
    assert "workspace_root" not in tasks[0]
    TaskDAG.from_task_list(tasks).validate_contracts()


def test_quoted_filename_is_an_explicit_reference():
    tasks = [{
        "id": "T1",
        "description": 'Create "notes.py" for JSON file notes.',
        "write_scope": [],
    }]
    assert file_write_scope_error(tasks) is None
    assert tasks[0].get("workspace_root") is True


def test_repair_survives_end_to_end_plan_acceptance():
    """The orchestrator plan-acceptance path accepts the T2 plan JSON without
    raising, so no strategist retry and no Manager REVISE cycle is burned."""
    plan = json.dumps({"tasks": [_t2()], "risks": []})
    dag, tasks = CouncilOrchestrator._task_dag_from_plan(plan)
    assert tasks[0].get("workspace_root") is True
    dag.validate_contracts()


def test_agent_runner_gate_repairs_instead_of_rejecting():
    tasks = [_t2()]
    assert _strategist_plan_error(tasks, "Create a notes app") is None
    assert tasks[0].get("workspace_root") is True


def test_multiple_files_are_ambiguous_and_untouched():
    """Genuinely ambiguous: two files, one empty scope. Targeted revision
    naming the task, never a silent rewrite."""
    tasks = [{
        "id": "T2",
        "description": "Create notes.py and app.py for the workspace.",
        "write_scope": [],
    }]
    error = file_write_scope_error(tasks)
    assert error is not None
    assert "T2" in error
    assert "notes.py" in error and "app.py" in error
    assert tasks[0]["write_scope"] == []
    assert "workspace_root" not in tasks[0]


def test_no_explicit_file_is_ambiguous_and_untouched():
    tasks = [{
        "id": "T1",
        "description": "Create the persistence layer for the workspace.",
        "write_scope": [],
    }]
    error = file_write_scope_error(tasks)
    assert error is not None
    assert "T1" in error and "write_scope" in error
    assert tasks[0]["write_scope"] == []
    assert "workspace_root" not in tasks[0]


def test_traversal_reference_is_never_trusted():
    tasks = [{
        "id": "T2",
        "description": "Create ../evil.py for the workspace.",
        "write_scope": [],
    }]
    error = file_write_scope_error(tasks)
    assert error is not None
    assert "T2" in error
    assert tasks[0]["write_scope"] == []
    assert "workspace_root" not in tasks[0]


def test_read_only_task_with_filename_is_untouched():
    tasks = [{
        "id": "T1",
        "description": "Inspect notes.py and report the CRUD shape.",
        "write_scope": [],
    }]
    assert file_write_scope_error(tasks) is None
    assert tasks[0]["write_scope"] == []
    assert "workspace_root" not in tasks[0]
    assert repair_file_write_scopes(tasks) == {"repaired": [], "errors": []}


def test_fix_verb_task_stays_on_manager_path():
    """Non-creation verbs are not auto-repaired; the Manager REVISE path for
    genuinely flawed modification plans is untouched."""
    tasks = [{
        "id": "T1",
        "description": "Fix src/auth.py using repository evidence.",
        "acceptance": "src/auth.py fixed.",
        "read_scope": ["src/"],
        "write_scope": [],
    }]
    assert file_write_scope_error(tasks) is None
    assert tasks[0]["write_scope"] == []


def test_ambiguous_plan_raises_targeted_revision_in_acceptance_path():
    plan = json.dumps({"tasks": [{
        "id": "T2",
        "description": "Create notes.py and app.py for the workspace.",
        "write_scope": [],
    }]})
    with pytest.raises(ValueError, match="T2"):
        CouncilOrchestrator._task_dag_from_plan(plan)
