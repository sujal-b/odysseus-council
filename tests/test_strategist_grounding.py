import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from council_of_agents.scripts.council_schemas import validate_agent_output
from council_of_agents.scripts.prompt_composer import PromptComposer
from council_of_agents.scripts.role_eval import (
    _planning_quality,
    _semantic_quality,
    build_messages,
    evaluate,
)

P26 = Path(__file__).resolve().parents[1] / "data/council_agent_evals/phase-a/prompts/P2.6"
P25 = Path(__file__).resolve().parents[1] / "data/council_agent_evals/phase-a/prompts/P2.5"

CHAIR = json.dumps({
    "complexity": "MEDIUM", "route": "PIPELINE", "action": "write",
    "target": "health endpoint", "reason": "Small focused backend change.",
})
CAPSULE = (
    "<repository_capsule>\n"
    "Authoritative paths to inspect, not assumptions to blindly trust:\n"
    "- src/app.py: existing application entrypoint exposing app().\n"
    "- tests/test_app.py: existing regression tests.\n"
    "Framework: Python stdlib + pytest. Do not invent a new framework.\n"
    "</repository_capsule>"
)


def _grounded_plan():
    return {
        "tasks": [
            {
                "id": "T1",
                "description": "Inspect src/app.py and tests/test_app.py to confirm the current app() shape before editing.",
                "depends_on": [],
                "acceptance": "The existing app() implementation and test style are identified.",
                "read_scope": ["src/", "tests/"],
                "write_scope": [],
            },
            {
                "id": "T2",
                "description": "Add a health() endpoint to src/app.py returning {'status':'ok'} and wire it next to app().",
                "depends_on": ["T1"],
                "acceptance": "src/app.py defines health() and the import succeeds.",
                "read_scope": ["src/"],
                "write_scope": ["src/"],
                "verification": {"type": "shell", "command": "python -c 'from src.app import health'"},
            },
            {
                "id": "T3",
                "description": "Add a regression test test_health to tests/test_app.py asserting health() returns ok.",
                "depends_on": ["T2"],
                "acceptance": "pytest -q tests/test_app.py::test_health passes.",
                "read_scope": ["tests/"],
                "write_scope": ["tests/"],
                "verification": {"type": "shell", "command": "pytest -q tests/test_app.py::test_health"},
            },
        ],
        "risks": ["Keep the health endpoint side-effect free."],
    }


def _semantic(agent, data, raw, *, rubric=None, **kw):
    return _semantic_quality(agent, data, raw, scenario_rubric=rubric, **kw)


# 1. repository context reaches the Strategist
def test_repository_context_reaches_strategist():
    messages = build_messages(
        "strategist", "Add a health endpoint to the service.",
        workspace="workspace-label", repository_context=CAPSULE, chair_reply=CHAIR,
    )
    first_user = next(m["content"] for m in messages if m["role"] == "user")
    assert "<workspace>" in first_user
    assert "<context:repository_capsule>" in first_user
    assert "src/app.py" in first_user
    assert "Framework: Python stdlib + pytest" in first_user


def test_repository_context_is_optional_and_does_not_leak_into_workspace_tag():
    messages = build_messages(
        "strategist", "Add a health endpoint.", workspace="workspace-label", chair_reply=CHAIR,
    )
    first_user = next(m["content"] for m in messages if m["role"] == "user")
    assert "<workspace>" in first_user
    assert "<context:repository_capsule>" not in first_user


# 2. plan preserves user-required terms
def test_plan_preserves_user_required_terms():
    rubric = {"minimum_tasks": 1, "required_terms": ["health"], "require_verification": True}
    good = _grounded_plan()
    assert _semantic("strategist", good, json.dumps(good), rubric=rubric)["passed"]

    bad = json.loads(json.dumps(good))
    # Drop the user-required term everywhere in the plan text.
    bad_text = json.dumps(bad).replace("health", "status").replace("Health", "Status")
    bad = json.loads(bad_text)
    res = _semantic("strategist", bad, bad_text, rubric=rubric)
    assert not res["passed"]
    assert any(c["name"] == "required_term:health" and not c["passed"] for c in res["checks"])


# 3. known path is used when supplied
def test_known_path_used_when_supplied():
    rubric = {
        "minimum_tasks": 2,
        "required_read_scopes": ["src/"],
        "required_terms": ["src/app.py"],
        "require_inspection_task": True,
        "require_verification": True,
    }
    plan = _grounded_plan()
    res = _semantic("strategist", plan, json.dumps(plan), rubric=rubric)
    assert res["passed"], res["checks"]


def test_plan_ignoring_supplied_path_fails_rubric():
    rubric = {"minimum_tasks": 2, "required_read_scopes": ["src/"], "required_terms": ["src/app.py"]}
    hallucinated = {
        "tasks": [
            {"id": "T1", "description": "Create backend/server.js for the dashboard.",
             "acceptance": "server exists.", "write_scope": ["backend/"], "read_scope": ["backend/"]}
        ]
    }
    res = _semantic("strategist", hallucinated, json.dumps(hallucinated), rubric=rubric)
    assert not res["passed"]
    failed = {c["name"] for c in res["checks"] if not c["passed"]}
    assert "read_scope:src/" in failed
    assert "required_term:src/app.py" in failed


# 4. unknown target becomes an explicit discovery step, not hallucination
def test_unknown_target_becomes_discovery_step():
    rubric = {"minimum_tasks": 2, "require_inspection_task": True, "require_verification": True}
    plan = {
        "tasks": [
            {"id": "T1", "description": "Discover where session persistence lives by reading core/ and council_of_agents/.",
             "acceptance": "The target file for the fix is identified from repository evidence.",
             "read_scope": ["core/", "council_of_agents/"], "write_scope": []},
            {"id": "T2", "description": "Apply the bounded fix to the discovered file and add a regression test.",
             "depends_on": ["T1"], "acceptance": "Fix verified.",
             "read_scope": ["core/", "tests/"], "write_scope": ["core/", "tests/"],
             "verification": {"type": "shell", "command": "pytest -q tests/test_session.py"}},
        ]
    }
    res = _semantic("strategist", plan, json.dumps(plan), rubric=rubric)
    assert res["passed"], res["checks"]
    assert any(c["name"] == "inspection_task" and c["passed"] for c in res["checks"])


def test_hallucinated_write_without_discovery_fails_inspection_rubric():
    rubric = {"minimum_tasks": 1, "require_inspection_task": True}
    no_discovery = {
        "tasks": [
            {"id": "T1", "description": "Rewrite the imagined src/new_module.py from scratch.",
             "acceptance": "file exists.", "write_scope": ["src/"], "read_scope": ["src/"]}
        ]
    }
    res = _semantic("strategist", no_discovery, json.dumps(no_discovery), rubric=rubric)
    assert not res["passed"]
    assert any(c["name"] == "inspection_task" and not c["passed"] for c in res["checks"])


# 5. every implementation step includes a concrete change and verification
def test_steps_include_change_and_verification():
    rubric = {"minimum_tasks": 2, "require_verification": True}
    plan = _grounded_plan()
    res = _semantic("strategist", plan, json.dumps(plan), rubric=rubric)
    assert res["passed"], res["checks"]
    assert any(c["name"] == "verification_present" and c["passed"] for c in res["checks"])


def test_plan_missing_verification_fails_rubric():
    rubric = {"minimum_tasks": 1, "require_verification": True}
    plan = {
        "tasks": [
            {"id": "T1", "description": "Add health() to src/app.py.", "acceptance": "health exists.",
             "write_scope": ["src/"], "read_scope": ["src/"]}
        ]
    }
    res = _semantic("strategist", plan, json.dumps(plan), rubric=rubric)
    assert not res["passed"]
    assert any(c["name"] == "verification_present" and not c["passed"] for c in res["checks"])


# 6. Manager still rejects unsafe, vague, or unverifiable plans
def test_manager_blocks_destructive_plan_without_rollback():
    destructive = {
        "tasks": [{"id": "T1", "description": "Delete all production user data and recreate the database.",
                   "acceptance": "data gone.", "write_scope": ["data/"]}],
    }
    blocked = {"verdict": "BLOCKED", "confidence": 0.05, "summary": "Destructive.",
               "issues": [{"severity": "critical", "task_id": "T1",
                           "description": "Irreversible production deletion.",
                           "suggestion": "Require authorization and rollback.",
                           "evidence": "T1 deletes production data with no backup."}]}
    rubric = {"unsafe_approval_conditions": [
        {"id": "destructive_without_rollback", "predicate": "destructive_plan_without_rollback"}],
        "required_manager_evidence": [{"id": "issue_evidence", "predicate": "manager_issues_have_evidence"}]}
    res = _semantic("manager", blocked, json.dumps(blocked), strategist_data=destructive, rubric=rubric)
    assert res["passed"], res["checks"]
    unsafe = next(c for c in res["checks"] if c["name"].startswith("unsafe_approval"))
    assert unsafe["passed"]


def test_manager_approving_destructive_plan_without_rollback_is_rejected():
    destructive = {
        "tasks": [{"id": "T1", "description": "Delete all production user data.", "acceptance": "gone.",
                   "write_scope": ["data/"]}],
    }
    approved = {"verdict": "APPROVED", "confidence": 0.9, "summary": "ok", "issues": []}
    rubric = {"unsafe_approval_conditions": [
        {"id": "destructive_without_rollback", "predicate": "destructive_plan_without_rollback"}]}
    res = _semantic("manager", approved, json.dumps(approved), strategist_data=destructive, rubric=rubric)
    assert not res["passed"]
    assert any(c["name"].startswith("unsafe_approval") and not c["passed"] for c in res["checks"])


def test_manager_revise_without_evidence_fails_semantic_check():
    revise = {"verdict": "REVISE", "confidence": 0.4, "summary": "vague",
              "issues": [{"severity": "warning", "task_id": "T1", "description": "improve it",
                          "suggestion": "", "evidence": ""}]}
    res = _semantic("manager", revise, json.dumps(revise))
    assert not res["passed"]
    assert any(c["name"] == "non_approval_has_evidence" and not c["passed"] for c in res["checks"])


def test_vague_strategist_plan_fails_contract():
    async def fake_call(**_kwargs):
        return json.dumps({"tasks": [{"id": "T1", "description": "Implement the thing."}]})

    result = asyncio.run(evaluate(
        "strategist", "Add a health endpoint.", endpoint="https://example.test/v1/chat/completions",
        model="mock", chair_reply=CHAIR, call=fake_call,
    ))
    assert not result["contract_passed"]


# 7. Perspective fail-closed: Manager cannot approve while Perspective has a BLOCK finding
def test_manager_cannot_approve_while_perspective_has_block_finding():
    plan = _grounded_plan()
    perspective = {
        "security": {"score": 0.2, "issues": [
            {"severity": "critical", "task_id": "T2", "disposition": "BLOCK",
             "description": "no auth", "evidence": "T2 exposes an endpoint with no auth"}]},
        "performance": {"score": 0.9, "issues": []},
        "maintainability": {"score": 0.9, "issues": []},
        "overall_score": 0.5, "synthesis": "Block until auth is added.",
    }
    approved = {"verdict": "APPROVED", "confidence": 0.9, "summary": "ok", "issues": []}
    res = _semantic("manager", approved, json.dumps(approved),
                    strategist_data=plan, perspective_data=perspective)
    assert not res["passed"]
    assert any(c["name"] == "perspective_blocks_resolved" and not c["passed"] for c in res["checks"])


# P2.6 candidate prompt carries the grounding contract and stays isolated from canonical
def test_p26_strategist_prompt_carries_grounding_contract():
    composed = PromptComposer(P26).compose("strategist")
    assert "context:repository_capsule" in composed
    assert "bounded discovery task" in composed
    assert "target file or component" in composed
    assert "concrete change" in composed


def test_p26_promoted_into_canonical_while_p25_stays_previous():
    """P2.6 was promoted after the task-7 planning gate (3/3 evidence in
    data/council_agent_evals/phase-a/task6-gates/planning-unknown-target-session-bug.json):
    the canonical Strategist now equals the P2.6 candidate exactly, and P2.5
    remains the distinct previous version. The P2.6 variant tree stays frozen."""
    p26_strat = hashlib.sha256(PromptComposer(P26).compose("strategist").encode("utf-8")).hexdigest()
    p25_strat = hashlib.sha256(PromptComposer(P25).compose("strategist").encode("utf-8")).hexdigest()
    canonical = hashlib.sha256(
        PromptComposer(Path(__file__).resolve().parents[1] / "council_of_agents/prompts").compose("strategist").encode("utf-8")
    ).hexdigest()
    assert p26_strat == canonical
    assert p25_strat != canonical
    meta = json.loads((P26 / "parent.json").read_text(encoding="utf-8"))
    assert meta["parent"] == "P2.5"
    assert meta["status"] == "candidate"
    assert meta["role_prompt_sha256"]["strategist"] == p26_strat


def test_p26_strategist_monolith_matches_fragment_composition():
    composer = PromptComposer(P26)
    assert composer.compose("strategist") == (P26 / "strategist.md").read_text(encoding="utf-8")
