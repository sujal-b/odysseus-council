import hashlib
import json

import pytest

from council_of_agents.scripts.context_envelope import build_repository_capsule
from council_of_agents.scripts.council_orchestrator import CouncilOrchestrator
from council_of_agents.scripts.task_dag import reconnaissance_plan_error
from council_of_agents.scripts.workflow_checkpoint import WorkflowCheckpoint


def _write(root, rel, text=""):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _session_tree(root):
    _write(root, "pyproject.toml", "[tool.pytest.ini_options]\ntestpaths = ['tests']\n")
    _write(root, "core/session_manager.py", "class SessionManager:\n    def load_sessions(self):\n        return self.message_count\n")
    _write(root, "tests/test_session_manager.py", "def test_message_count():\n    assert True\n")


def test_session_recon_finds_target_symbol_and_test(tmp_path):
    _session_tree(tmp_path)
    facts = build_repository_capsule(tmp_path, "Fix sessions reload when message count is zero.")
    assert "core/session_manager.py" in facts["selected_paths"]
    assert "core/session_manager.py:SessionManager.load_sessions" in facts["matching_symbols"]
    assert "tests/test_session_manager.py" in facts["regression_tests"]
    assert facts["allowed_workspace_scope"] == ["core/", "tests/"]
    assert facts["test_commands"] == ["python -m pytest -q tests/test_session_manager.py"]


def test_recon_keeps_multiple_matches_and_requires_selection_criteria(tmp_path):
    _write(tmp_path, "core/session_service.py", "def session(): pass\n")
    _write(tmp_path, "src/session_service.py", "def session(): pass\n")
    _write(tmp_path, "tests/test_session_service.py", "def test_session(): pass\n")
    facts = build_repository_capsule(tmp_path, "Fix session service.")
    assert {"core/session_service.py", "src/session_service.py"} <= set(facts["selected_paths"])
    plan = [{"id": "T1", "description": "Inspect evidence and select core/session_service.py using selection criteria.",
             "acceptance": "core target selected", "read_scope": ["core/"], "write_scope": ["core/"]}]
    assert reconnaissance_plan_error(plan, facts) is None


def test_zero_match_requires_bounded_discovery_dependency(tmp_path):
    _write(tmp_path, "core/service.py", "def service(): pass\n")
    _write(tmp_path, "tests/test_service.py", "def test_service(): pass\n")
    facts = build_repository_capsule(tmp_path, "Fix flibbertigibbet behavior.")
    assert facts["status"] == "discovery_required"
    plan = [
        {"id": "T1", "description": "Discover target in bounded repository evidence.", "acceptance": "Target output recorded.", "read_scope": ["core/"], "write_scope": []},
        {"id": "T2", "description": "Apply fix using discovered target output.", "acceptance": "Test passes.", "depends_on": ["T1"], "read_scope": ["core/", "tests/"], "write_scope": ["core/", "tests/"]},
    ]
    assert reconnaissance_plan_error(plan, facts) is None


def test_recon_excludes_secret_ignored_and_source_content(tmp_path):
    _session_tree(tmp_path)
    _write(tmp_path, ".gitignore", "data/\n")
    _write(tmp_path, "data/generated.py", "def message_count(): pass\n")
    _write(tmp_path, "core/secret_token.py", "token = 'hunter2'\n")
    _write(tmp_path, "core/config.py", "api_key = 'hunter2'\n")
    facts = build_repository_capsule(tmp_path, "Fix session message count.")
    rendered = facts["capsule"]
    assert all("secret" not in path and "data/" not in path for path in facts["selected_paths"])
    assert "hunter2" not in rendered
    assert "api_key" not in rendered


def test_recon_limits_are_deterministic(tmp_path):
    for number in range(80):
        _write(tmp_path, f"src/target_{number:03}.py", "def target(): pass\n")
    first = build_repository_capsule(tmp_path, "Fix target.")
    second = build_repository_capsule(tmp_path, "Fix target.")
    assert first == second
    assert first["truncated"] is True
    assert first["limits"] == {"files": 64, "results": 12, "bytes": 256 * 1024}
    assert len(first["selected_paths"]) <= 12


def test_capsule_checkpoint_survives_restart_without_repeat(tmp_path):
    _session_tree(tmp_path / "repo")
    facts = build_repository_capsule(tmp_path / "repo", "Fix session message count.")
    checkpoint = WorkflowCheckpoint("recon-restart", base_dir=tmp_path / "checkpoints")
    checkpoint.record_reconnaissance(facts)
    checkpoint.record_stage("strategist", json.dumps({"tasks": []}))
    checkpoint.record_task_start("T1")
    checkpoint.record_task_result("T1", status="DONE", artifact_paths=["core/session_manager.py"])
    restored = WorkflowCheckpoint("recon-restart", base_dir=tmp_path / "checkpoints")
    saved = restored.reconnaissance()
    assert saved["capsule"] == facts["capsule"]
    assert saved["sha256"] == hashlib.sha256(facts["capsule"].encode()).hexdigest()
    assert restored.stage_done("strategist")
    assert restored.task("T1")["attempts"] == 1


def test_invented_or_unsafe_target_is_blocked_before_manager():
    facts = {"status": "ok", "selected_paths": ["core/session_manager.py"], "allowed_workspace_scope": ["core/"], "discovery_required": False}
    plan = json.dumps({"tasks": [{"id": "T1", "description": "Modify invented routes/ghost.py.", "acceptance": "done", "read_scope": ["routes/"], "write_scope": ["routes/"]}]})
    with pytest.raises(ValueError, match="outside repository reconnaissance evidence"):
        CouncilOrchestrator._task_dag_from_plan(plan, "Fix session.", reconnaissance=facts)