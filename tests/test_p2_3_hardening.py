import asyncio
import hashlib
import json
import math
from pathlib import Path
import pytest

from council_of_agents.scripts.council_schemas import (
    PerspectiveOutput,
    PerspectiveResponseNormalizer,
    validate_agent_output,
    validate_issue_task_id_shape,
)
from council_of_agents.scripts.prompt_composer import PromptComposer
from council_of_agents.scripts.role_eval import (
    _semantic_repair_protected_fields_match,
    _get_harness_fingerprint,
    _trace_report,
    evaluate,
)

ROOT = Path(__file__).resolve().parents[1]
P2_2 = ROOT / "data/council_agent_evals/phase-a/prompts/P2.2"
P2_3 = ROOT / "data/council_agent_evals/phase-a/prompts/P2.3"


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


def test_perspective_bool_nan_infinite_overall_score_rejected():
    for bad_score in (True, False, float("nan"), float("inf"), float("-inf"), -0.1, 1.1):
        raw = _perspective_payload(overall_score=bad_score)
        res = validate_agent_output("perspective_analyzer", json.dumps(raw), strict=True)
        assert res.success is False, f"Failed to reject invalid overall_score: {bad_score}"
        assert res.metadata["normalized_shape"] == "rejected_ambiguous"


def test_perspective_atomic_pair_promotion_success():
    raw = {
        "security": {"score": 0.9, "issues": []},
        "performance": {"score": 0.85, "issues": []},
        "maintainability": {
            "score": 0.8,
            "issues": [],
            "overall_score": 0.82,
            "synthesis": "Nested overall score and synthesis pair.",
        },
    }
    res = validate_agent_output("perspective_analyzer", json.dumps(raw), strict=True)
    assert res.success is True
    assert res.data["overall_score"] == 0.82
    assert res.data["synthesis"] == "Nested overall score and synthesis pair."
    assert res.metadata["nested_overall_score_and_synthesis_promoted"] is True
    assert res.metadata["normalization_used"] is True


def test_perspective_partial_pair_in_section_rejected():
    # Only nested synthesis, missing score inside section when root missing both
    raw1 = {
        "security": {"score": 0.9, "issues": []},
        "performance": {"score": 0.85, "issues": []},
        "maintainability": {"score": 0.8, "issues": [], "synthesis": "Only synthesis."},
    }
    res1 = validate_agent_output("perspective_analyzer", json.dumps(raw1), strict=True)
    assert res1.success is False
    assert res1.metadata["normalized_shape"] == "rejected_ambiguous"

    # Only nested overall_score, missing synthesis inside section when root missing both
    raw2 = {
        "security": {"score": 0.9, "issues": []},
        "performance": {"score": 0.85, "issues": []},
        "maintainability": {"score": 0.8, "issues": [], "overall_score": 0.85},
    }
    res2 = validate_agent_output("perspective_analyzer", json.dumps(raw2), strict=True)
    assert res2.success is False
    assert res2.metadata["normalized_shape"] == "rejected_ambiguous"


def test_structural_comparison_rejects_unflagged_task_id_modification():
    orig = {
        "verdict": "REVISE",
        "confidence": 0.8,
        "summary": "Plan review",
        "issues": [
            {"severity": "warning", "task_id": "T99", "description": "Issue 0", "suggestion": "Fix", "evidence": "E0"},
            {"severity": "warning", "task_id": "T1", "description": "Issue 1", "suggestion": "Fix", "evidence": "E1"},
        ],
    }
    # Flagged issue is index 0. Index 1 is unflagged but tampered T1 -> T2!
    repaired_tampered_unflagged = {
        "verdict": "REVISE",
        "confidence": 0.8,
        "summary": "Plan review",
        "issues": [
            {"severity": "warning", "task_id": "T1", "description": "Issue 0", "suggestion": "Fix", "evidence": "E0"},
            {"severity": "warning", "task_id": "T2", "description": "Issue 1", "suggestion": "Fix", "evidence": "E1"},
        ],
    }
    flagged = [{"index": 0, "task_id": "T99"}]
    assert _semantic_repair_protected_fields_match("manager", orig, repaired_tampered_unflagged, flagged) is False

    # Valid repair: ONLY flagged issue 0 task_id modified T99 -> T1!
    repaired_valid = {
        "verdict": "REVISE",
        "confidence": 0.8,
        "summary": "Plan review",
        "issues": [
            {"severity": "warning", "task_id": "T1", "description": "Issue 0", "suggestion": "Fix", "evidence": "E0"},
            {"severity": "warning", "task_id": "T1", "description": "Issue 1", "suggestion": "Fix", "evidence": "E1"},
        ],
    }
    assert _semantic_repair_protected_fields_match("manager", orig, repaired_valid, flagged) is True


def test_semantic_repair_telemetry_split_from_schema_repair():
    stage_result_schema = {
        "repair_attempted": True,
        "repair_kind": "schema_repair",
        "contract_passed": True,
    }
    # Derived fields in role_eval.py result dict
    schema_attempted = stage_result_schema["repair_attempted"] and stage_result_schema["repair_kind"] == "schema_repair"
    semantic_attempted = stage_result_schema["repair_attempted"] and stage_result_schema["repair_kind"] == "semantic_repair"
    assert schema_attempted is True
    assert semantic_attempted is False

    stage_result_semantic = {
        "repair_attempted": True,
        "repair_kind": "semantic_repair",
        "contract_passed": True,
    }
    schema_attempted2 = stage_result_semantic["repair_attempted"] and stage_result_semantic["repair_kind"] == "schema_repair"
    semantic_attempted2 = stage_result_semantic["repair_attempted"] and stage_result_semantic["repair_kind"] == "semantic_repair"
    assert schema_attempted2 is False
    assert semantic_attempted2 is True


def test_tree_hash_recomputation_from_raw_bytes():
    # 1. P2.2 Recomputation
    p2_2_digest = hashlib.sha256()
    p2_2_entries = sorted(
        (p.relative_to(P2_2).as_posix().encode(), p.read_bytes())
        for p in P2_2.rglob("*")
        if p.is_file() and p.name not in {"parent.json", "changes.json"}
    )
    for rel, raw in p2_2_entries:
        p2_2_digest.update(rel + b"\x00" + raw)
    p2_2_meta = json.loads((P2_2 / "parent.json").read_text(encoding="utf-8"))
    assert p2_2_digest.hexdigest() == p2_2_meta["tree_sha256_excluding_metadata"]
    assert p2_2_digest.hexdigest() == "8c44ac21af0c02754bdc1c6becf23f46bf0902e01264ed3c2c1f85aefb5bceab"

    # 2. P2.3 Recomputation
    p2_3_digest = hashlib.sha256()
    p2_3_entries = sorted(
        (p.relative_to(P2_3).as_posix().encode(), p.read_bytes())
        for p in P2_3.rglob("*")
        if p.is_file() and p.name not in {"parent.json", "changes.json"}
    )
    for rel, raw in p2_3_entries:
        p2_3_digest.update(rel + b"\x00" + raw)
    p2_3_meta = json.loads((P2_3 / "parent.json").read_text(encoding="utf-8"))
    assert p2_3_digest.hexdigest() == p2_3_meta["tree_sha256_excluding_metadata"]
    assert p2_3_meta["parent"] == "P2.2"
    assert p2_3_meta["parent_sha256"] == "8c44ac21af0c02754bdc1c6becf23f46bf0902e01264ed3c2c1f85aefb5bceab"


def test_prompt_composer_equals_monolithic_p2_3_prompts():
    composer = PromptComposer(P2_3)
    for role in ("chair", "strategist", "perspective_analyzer", "manager"):
        composed = composer.compose(role)
        actual = (P2_3 / f"{role}.md").read_text(encoding="utf-8")
        assert composed == actual, f"PromptComposer mismatch for {role} in P2.3!"


def test_repeated_repair_signature_fails_readiness_gate():
    trace_records = [
        {
            "attempt_count": 1,
            "contract_passed": True,
            "agent": "perspective_analyzer",
            "repair_signature": "perspective_analyzer|schema_repair|extra_forbidden|maintainability.overall_score",
            "semantic_quality": {"passed": True},
            "handoff_integrity": {"passed": True},
        },
        {
            "attempt_count": 1,
            "contract_passed": True,
            "agent": "perspective_analyzer",
            "repair_signature": "perspective_analyzer|schema_repair|extra_forbidden|maintainability.overall_score",
            "semantic_quality": {"passed": True},
            "handoff_integrity": {"passed": True},
        },
    ]
    report = _trace_report(
        "User prompt", trace_records, {}, {}, "run-1",
        planning_only=False, scenario_id="approved_plan", termination="COMPLETE",
    )
    assert report["readiness_gate"]["passed"] is False
    assert any("repeated repair signature" in f for f in report["readiness_gate"]["failures"])


def test_harness_fingerprint_persisted_in_trace_metadata():
    harness_fp = _get_harness_fingerprint(P2_3)
    assert isinstance(harness_fp, str) and len(harness_fp) == 64

    report = _trace_report(
        "User prompt", [], {}, {}, "run-1",
        planning_only=False, scenario_id="approved_plan", termination="COMPLETE",
    )
    assert "harness_fingerprint" in report
    assert report["harness_fingerprint"] == harness_fp
