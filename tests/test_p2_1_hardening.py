import asyncio
import json
import re
from pathlib import Path

import pytest

from council_of_agents.scripts.council_schemas import (
    StrategistTask,
    validate_agent_output,
)
from council_of_agents.scripts.role_eval import _planning_quality


ROOT = Path(__file__).resolve().parents[1]
P2_1 = ROOT / "data/council_agent_evals/phase-a/prompts/P2.1"
OVERLAY = ROOT / "council_of_agents/benchmarks/role_eval_scenarios_p2_1.json"


def _task(task_id="T1", **overrides):
    value = {
        "id": task_id,
        "description": "Create the bounded dashboard change.",
        "depends_on": [],
        "acceptance": "The requested result is verifiable.",
        "write_scope": ["src/"],
    }
    value.update(overrides)
    return value


def test_risk_objects_flatten_only_known_values_and_record_metadata():
    result = validate_agent_output(
        "strategist",
        json.dumps({
            "tasks": [_task()],
            "risks": [{"description": "API may change.", "level": "low"}],
        }),
        strict=True,
    )

    assert result.success is True
    assert result.data["risks"] == ["API may change."]
    assert result.metadata["risk_objects_flattened"] is True
    assert result.metadata["normalization_used"] is True


def test_unknown_or_empty_risk_objects_are_rejected_as_ambiguous():
    for risk in ({}, {"level": "low"}, {"description": "ok", "unexpected": True}):
        result = validate_agent_output(
            "strategist",
            json.dumps({"tasks": [_task()], "risks": [risk]}),
            strict=True,
        )
        assert result.success is False
        assert result.metadata["normalization_shape"] == "rejected_ambiguous"


def test_directory_scope_without_slash_is_canonicalized_and_file_scope_is_rejected():
    normalized = validate_agent_output(
        "strategist",
        json.dumps({"tasks": [_task(write_scope=["src"])]}),
        strict=True,
    )
    assert normalized.success is True
    assert normalized.data["tasks"][0]["write_scope"] == ["src/"]
    assert normalized.metadata["scope_slash_added"] is True
    assert normalized.metadata["normalization_used"] is True

    for scope in ("src/app.py", "../secret", "/etc/passwd", "C:\\secret"):
        result = validate_agent_output(
            "strategist",
            json.dumps({"tasks": [_task(write_scope=[scope])]}),
            strict=True,
        )
        assert result.success is False, scope
        assert result.metadata["normalization_shape"] == "rejected_ambiguous"


def test_p2_1_flexible_scope_rubric_accepts_root_and_declared_directories():
    rubric = {"minimum_tasks": 2, "scope_policy": "declared_or_workspace_root"}
    checks = _planning_quality(
        {
            "tasks": [
                _task("T1", description="Create the project shell.", write_scope=[], workspace_root=True),
                _task("T2", description="Inspect the existing dashboard.", write_scope=[]),
                _task("T3", description="Create src/dashboard.tsx.", write_scope=["src/"]),
            ],
        },
        "dashboard flight",
        rubric,
    )
    assert all(check["passed"] for check in checks), checks


def test_p2_1_scope_rubric_rejects_out_of_scope_references():
    checks = _planning_quality(
        {"tasks": [_task(description="Create core/dashboard.py.", write_scope=["src/"])]},
        "dashboard flight",
        {"minimum_tasks": 1, "scope_policy": "declared_or_workspace_root"},
    )
    by_name = {check["name"]: check for check in checks}
    assert by_name["scope_path_consistency"]["passed"] is False


def test_p2_1_rubric_accepts_semantic_term_alternative_after_revision():
    checks = _planning_quality(
        {"tasks": [_task(description="Create the flight-tracker UI.")]},
        "flight-tracker",
        {"minimum_tasks": 1, "required_terms_any": [["dashboard", "flight-tracker"]]},
    )
    assert all(check["passed"] for check in checks), checks


def _json_fences(text):
    return re.findall(r"```(?:json|tasks)\s*\n(.*?)```", text, re.DOTALL | re.IGNORECASE)


def test_p2_1_positive_contract_examples_validate():
    candidates = []
    for path in P2_1.rglob("*.md"):
        if path.name not in {"examples.md", "output_format.md"}:
            continue
        text = path.read_text(encoding="utf-8")
        for block in _json_fences(text):
            try:
                payload = json.loads(block)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and "tasks" in payload:
                candidates.append((path, payload))

    assert candidates, "No Strategist contract examples were found"
    for path, payload in candidates:
        result = validate_agent_output("strategist", json.dumps(payload), strict=True)
        assert result.success, f"{path}: {result.error}"


def test_p2_1_labeled_negative_task_example_is_rejected():
    examples = (P2_1 / "fragments/strategist/examples.md").read_text(encoding="utf-8")
    block = re.search(r"\*\*Bad task description.*?```\s*\n(.*?)```", examples, re.DOTALL)
    assert block
    task = json.loads(block.group(1))
    with pytest.raises(Exception):
        StrategistTask.model_validate(task)


def test_p2_1_overlay_keeps_frozen_cases_and_adds_explicit_scope_case():
    scenarios = json.loads(OVERLAY.read_text(encoding="utf-8"))
    frozen = json.loads((ROOT / "council_of_agents/benchmarks/role_eval_scenarios.json").read_text(encoding="utf-8"))

    assert scenarios["approved_plan"]["user_prompt"] == frozen["approved_plan"]["user_prompt"]
    assert "required_write_scopes" not in scenarios["approved_plan"]["planning_rubric"]
    assert scenarios["approved_plan"]["planning_rubric"]["scope_policy"] == "declared_or_workspace_root"
    assert scenarios["approved_plan_src_explicit"]["planning_rubric"]["required_write_scopes"] == ["src/"]


def test_perspective_timeout_is_role_specific_and_reflects_live_evidence():
    models = json.loads((ROOT / "council_of_agents/config/models.json").read_text(encoding="utf-8"))
    assert models["roles"]["chair"]["timeout"] == 60
    assert models["roles"]["perspective_analyzer"]["timeout"] == 90
    assert models["roles"]["manager"]["timeout"] == 90
