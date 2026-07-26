import asyncio
import json
import re
from pathlib import Path
import pytest

from council_of_agents.scripts.council_schemas import (
    PerspectiveOutput,
    PerspectiveScore,
    PerspectiveIssue,
    validate_agent_output,
    validate_issue_task_id_shape,
)
from council_of_agents.scripts.role_eval import (
    _semantic_repair_protected_fields_match,
    _semantic_quality,
    evaluate,
)

ROOT = Path(__file__).resolve().parents[1]
P2_1 = ROOT / "data/council_agent_evals/phase-a/prompts/P2.1"
P2_2 = ROOT / "data/council_agent_evals/phase-a/prompts/P2.2"


def _perspective_payload(**overrides):
    base = {
        "security": {
            "score": 0.9,
            "issues": [
                {
                    "severity": "info",
                    "disposition": "ADVISORY",
                    "description": "Consider secret rotation.",
                    "task_id": "T1",
                    "suggestion": "Add rotation config.",
                    "evidence": "T1 creates auth route.",
                }
            ],
        },
        "performance": {"score": 0.85, "issues": []},
        "maintainability": {"score": 0.8, "issues": []},
        "overall_score": 0.85,
        "synthesis": "Overall solid design with clear task scoping.",
    }
    base.update(overrides)
    return base


def test_perspective_single_nested_synthesis_promoted():
    raw = {
        "security": {"score": 0.9, "issues": [], "synthesis": "Nested security synthesis text."},
        "performance": {"score": 0.85, "issues": []},
        "maintainability": {"score": 0.8, "issues": []},
        "overall_score": 0.85,
    }
    res = validate_agent_output("perspective_analyzer", json.dumps(raw), strict=True)
    assert res.success is True
    assert res.data["synthesis"] == "Nested security synthesis text."
    assert res.data["security"].get("synthesis") is None
    assert res.metadata["nested_synthesis_promoted"] is True
    assert res.metadata["normalization_used"] is True


def test_perspective_root_plus_nested_synthesis_rejected_even_if_identical():
    raw = {
        "security": {"score": 0.9, "issues": [], "synthesis": "Identical synthesis text."},
        "performance": {"score": 0.85, "issues": []},
        "maintainability": {"score": 0.8, "issues": []},
        "overall_score": 0.85,
        "synthesis": "Identical synthesis text.",
    }
    res = validate_agent_output("perspective_analyzer", json.dumps(raw), strict=True)
    assert res.success is False
    assert res.metadata["normalized_shape"] == "rejected_ambiguous"


def test_perspective_missing_blank_nonstring_synthesis_rejected():
    for bad in ("", "   ", 123, None):
        raw = _perspective_payload(synthesis=bad)
        res = validate_agent_output("perspective_analyzer", json.dumps(raw), strict=True)
        assert res.success is False, f"Failed to reject synthesis: {bad}"
        assert res.metadata["normalized_shape"] == "rejected_ambiguous"


def test_perspective_multiple_nested_synthesis_rejected():
    raw = {
        "security": {"score": 0.9, "issues": [], "synthesis": "Synthesis A."},
        "performance": {"score": 0.85, "issues": [], "synthesis": "Synthesis B."},
        "maintainability": {"score": 0.8, "issues": []},
        "overall_score": 0.85,
    }
    res = validate_agent_output("perspective_analyzer", json.dumps(raw), strict=True)
    assert res.success is False
    assert res.metadata["normalized_shape"] == "rejected_ambiguous"


def test_perspective_unknown_extra_fields_rejected_at_all_three_levels():
    # 1. Output level
    raw1 = _perspective_payload(unexpected_top_level="bad")
    res1 = validate_agent_output("perspective_analyzer", json.dumps(raw1), strict=True)
    assert res1.success is False

    # 2. Section level
    raw2 = _perspective_payload()
    raw2["security"]["unexpected_section_field"] = "bad"
    res2 = validate_agent_output("perspective_analyzer", json.dumps(raw2), strict=True)
    assert res2.success is False

    # 3. Issue level
    raw3 = _perspective_payload()
    raw3["security"]["issues"][0]["unexpected_issue_field"] = "bad"
    res3 = validate_agent_output("perspective_analyzer", json.dumps(raw3), strict=True)
    assert res3.success is False


def test_valid_task_id_shapes_pass_validation():
    for valid_id in ("ALL", "T1", "T1a", "task_login", "task-login"):
        valid, reason = validate_issue_task_id_shape(valid_id)
        assert valid is True, f"Failed valid task_id: {valid_id} ({reason})"


def test_combined_and_invalid_task_id_shapes_rejected_without_coercion():
    for bad_id in ("T1,T2", "T1b,T1c", "T1;T2", "T1 T2", "T1/T2", "", "   ", "\t"):
        valid, reason = validate_issue_task_id_shape(bad_id)
        assert valid is False, f"Failed to reject task_id: {bad_id}"
        assert reason in ("rejected_combined_id", "empty_task_id", "invalid_task_id_shape")


def test_unknown_safe_task_id_fails_semantic_grounding():
    manager_data = {
        "verdict": "APPROVED",
        "confidence": 0.9,
        "summary": "Plan is good.",
        "issues": [{"severity": "info", "task_id": "T99", "description": "Minor issue", "suggestion": "Fix it", "evidence": "File"}],
    }
    strategist_data = {"tasks": [{"id": "T1", "description": "Inspect.", "acceptance": "Ok", "write_scope": ["src/"]}]}
    sem = _semantic_quality("manager", manager_data, json.dumps(manager_data), strategist_data=strategist_data)
    checks = {c["name"]: c["passed"] for c in sem["checks"]}
    assert checks.get("issue_ids_grounded") is False


def test_semantic_repair_protected_fields_matching():
    orig_manager = {
        "verdict": "REVISE",
        "issues": [{"task_id": "T99", "description": "Auth bug"}],
    }
    # Protected field modified: verdict changed REVISE -> APPROVED!
    repaired_tampered = {
        "verdict": "APPROVED",
        "issues": [{"task_id": "T1", "description": "Auth bug"}],
    }
    assert _semantic_repair_protected_fields_match("manager", orig_manager, repaired_tampered) is False

    # Valid semantic repair: ONLY task_id updated!
    repaired_valid = {
        "verdict": "REVISE",
        "issues": [{"task_id": "T1", "description": "Auth bug"}],
    }
    assert _semantic_repair_protected_fields_match("manager", orig_manager, repaired_valid) is True


def test_p2_1_tree_hash_is_unchanged():
    p2_1_meta = json.loads((P2_1 / "parent.json").read_text(encoding="utf-8"))
    assert p2_1_meta["version"] == "P2.1"
    assert p2_1_meta["tree_sha256_excluding_metadata"] == "cf6ead275affca391172c60a6feb6098674793e4ef65203f379735ce3db1dd35"


def test_p2_2_snapshot_parentage_and_tree_hash_valid():
    p2_2_meta = json.loads((P2_2 / "parent.json").read_text(encoding="utf-8"))
    assert p2_2_meta["version"] == "P2.2"
    assert p2_2_meta["parent"] == "P2.1"
    assert p2_2_meta["parent_sha256"] == "cf6ead275affca391172c60a6feb6098674793e4ef65203f379735ce3db1dd35"
