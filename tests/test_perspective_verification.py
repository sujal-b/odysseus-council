"""Rigorous verification test suite for Phase 3 Perspective Analyzer Optimization.

Verifies:
1. Prompt composition and byte-for-byte parity between composed and monolithic prompts.
2. Character and token metrics against target budgets (~1,700–1,900 chars, ~425–475 tokens).
3. Schema conformance with PerspectiveOutput, PerspectiveScore, and PerspectiveIssue.
4. Normalization conformance with PerspectiveResponseNormalizer.
5. Strict validation with validate_agent_output('perspective_analyzer', ...).
6. Single task_id enforcement via _normalize_task_id / validate_issue_task_id_shape.
7. Rejection of combined task IDs and extra forbidden fields.
8. Evidence classification via CouncilOrchestrator._classify_perspective_evidence.
"""

import json
import sys
from pathlib import Path

# Ensure root is in sys.path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest
import tiktoken

from council_of_agents.scripts.council_orchestrator import CouncilOrchestrator
from council_of_agents.scripts.council_schemas import (
    PerspectiveIssue,
    PerspectiveOutput,
    PerspectiveResponseNormalizer,
    PerspectiveScore,
    _normalize_task_id,
    validate_agent_output,
    validate_issue_task_id_shape,
)
from council_of_agents.scripts.prompt_composer import PromptComposer


def test_prompt_character_and_token_counts():
    """Verify prompt character and token counts fall in the optimized target range."""
    prompts_dir = ROOT / "council_of_agents" / "prompts"
    composer = PromptComposer(prompts_dir)
    composed = composer.compose("perspective_analyzer")
    monolithic = composer._load_monolithic("perspective_analyzer")

    # Byte-for-byte parity
    assert composed == monolithic, "Composed prompt does not match monolithic prompt byte-for-byte!"

    char_count = len(composed)
    enc = tiktoken.get_encoding("cl100k_base")
    token_count = len(enc.encode(composed))
    approx_tokens_4char = char_count / 4.0

    print(f"\n[METRICS] Composed Characters: {char_count}")
    print(f"[METRICS] Tiktoken (cl100k_base) Tokens: {token_count}")
    print(f"[METRICS] Approx (chars/4) Tokens: {approx_tokens_4char:.1f}")

    # Target: ~1,700–1,900 chars (~425–475 tokens), down from 3,659 chars
    assert 1700 <= char_count <= 1950, f"Character count {char_count} outside target window [1700, 1950]"
    assert 420 <= token_count <= 500, f"Token count {token_count} outside target window [420, 500]"


def test_composed_monolithic_byte_parity():
    """Verify byte-for-byte identical content between composed and monolithic."""
    prompts_dir = ROOT / "council_of_agents" / "prompts"
    composer = PromptComposer(prompts_dir)
    composed = composer.compose("perspective_analyzer")
    monolithic = (prompts_dir / "perspective_analyzer.md").read_text(encoding="utf-8")
    assert composed == monolithic


def test_example_payload_schema_conformance():
    """Verify the canonical example in output_format conforms to PerspectiveOutput schema."""
    prompts_dir = ROOT / "council_of_agents" / "prompts"
    output_format_text = (prompts_dir / "fragments" / "perspective_analyzer" / "output_format.md").read_text(encoding="utf-8")

    start = output_format_text.find("{")
    end = output_format_text.rfind("}") + 1
    raw_json = output_format_text[start:end]

    # 1. Valid JSON parse
    data = json.loads(raw_json)

    # 2. Pydantic validation
    model = PerspectiveOutput.model_validate(data)
    assert model.overall_score == 0.85
    assert model.synthesis == "Plan is sound; one parallelism fix needed."
    assert len(model.performance.issues) == 1
    issue = model.performance.issues[0]
    assert issue.task_id == "T2"
    assert issue.disposition == "MUST_FIX"
    assert issue.severity == "warning"
    assert issue.suggestion == "Remove depends_on: ['T1'] to run in parallel"
    assert issue.evidence == "T2 depends_on: ['T1'] has no data dependency"

    # 3. Response normalizer
    norm, meta = PerspectiveResponseNormalizer.normalize(raw_json)
    assert norm is not None
    assert meta["raw_shape"] == "json_dict"

    # 4. Strict agent output validation
    val_res = validate_agent_output("perspective_analyzer", raw_json, strict=True)
    assert val_res.success is True
    assert val_res.data["overall_score"] == 0.85


def test_normalize_task_id_valid_and_invalid():
    """Verify _normalize_task_id and validate_issue_task_id_shape enforce single ID rules."""
    valid_ids = ["T1", "T2", "T1a", "task_build", "task-deploy", "ALL"]
    for tid in valid_ids:
        ok, res = _normalize_task_id(tid)
        assert ok is True, f"Expected {tid} to be valid, got {res}"
        ok2, res2 = validate_issue_task_id_shape(tid)
        assert ok2 is True

    invalid_combined = ["T1,T2", "T1b,T1c", "T1;T2", "T1 T2", "T1/T2", "T1\\T2"]
    for tid in invalid_combined:
        ok, reason = _normalize_task_id(tid)
        assert ok is False, f"Expected combined ID {tid} to be rejected"
        assert reason == "rejected_combined_id"

    invalid_empty = ["", "   ", "\t", "\n"]
    for tid in invalid_empty:
        ok, reason = _normalize_task_id(tid)
        assert ok is False, f"Expected empty ID to be rejected"
        assert reason == "empty_task_id"

    invalid_shape = ["T1@prod", "T1#sub", "T1$"]
    for tid in invalid_shape:
        ok, reason = _normalize_task_id(tid)
        assert ok is False, f"Expected malformed shape {tid} to be rejected"
        assert reason == "invalid_task_id_shape"


def test_extra_forbidden_fields_rejected():
    """Verify extra fields at root, section, and issue levels are rejected by schema."""
    canonical = {
        "security": {"score": 1.0, "issues": []},
        "performance": {"score": 1.0, "issues": []},
        "maintainability": {"score": 1.0, "issues": []},
        "overall_score": 1.0,
        "synthesis": "Clean.",
    }

    # Root level extra field
    bad_root = dict(canonical)
    bad_root["extra_field"] = "bad"
    res = validate_agent_output("perspective_analyzer", json.dumps(bad_root), strict=True)
    assert res.success is False

    # Section level extra field
    bad_sec = {
        "security": {"score": 1.0, "issues": [], "extra_sec": True},
        "performance": {"score": 1.0, "issues": []},
        "maintainability": {"score": 1.0, "issues": []},
        "overall_score": 1.0,
        "synthesis": "Clean.",
    }
    res = validate_agent_output("perspective_analyzer", json.dumps(bad_sec), strict=True)
    assert res.success is False

    # Issue level extra field
    bad_issue = {
        "security": {
            "score": 0.5,
            "issues": [{
                "severity": "warning",
                "disposition": "MUST_FIX",
                "description": "desc",
                "task_id": "T1",
                "suggestion": "fix",
                "evidence": "file",
                "unexpected": "rejected",
            }],
        },
        "performance": {"score": 1.0, "issues": []},
        "maintainability": {"score": 1.0, "issues": []},
        "overall_score": 0.5,
        "synthesis": "Clean.",
    }
    res = validate_agent_output("perspective_analyzer", json.dumps(bad_issue), strict=True)
    assert res.success is False


def test_orchestrator_classify_perspective_evidence():
    """Verify orchestrator approval gate classifies clear, block, invalid, empty correctly."""
    clear_payload = json.dumps({
        "security": {"score": 1.0, "issues": []},
        "performance": {"score": 1.0, "issues": []},
        "maintainability": {"score": 1.0, "issues": []},
        "overall_score": 1.0,
        "synthesis": "All clear.",
    })
    assert CouncilOrchestrator._classify_perspective_evidence(clear_payload) == "clear"

    block_payload = json.dumps({
        "security": {
            "score": 0.2,
            "issues": [{
                "severity": "critical",
                "disposition": "BLOCK",
                "description": "Hard safety violation: command injection in task",
                "task_id": "T1",
                "suggestion": "Sanitize inputs",
                "evidence": "T1 command field interpolates user parameter directly",
            }],
        },
        "performance": {"score": 1.0, "issues": []},
        "maintainability": {"score": 1.0, "issues": []},
        "overall_score": 0.4,
        "synthesis": "Severe injection flaw.",
    })
    assert CouncilOrchestrator._classify_perspective_evidence(block_payload) == "block"

    advisory_payload = json.dumps({
        "security": {
            "score": 0.9,
            "issues": [{
                "severity": "info",
                "disposition": "ADVISORY",
                "description": "Consider adding rate limiting",
                "task_id": "T1",
                "suggestion": "Add token bucket",
                "evidence": "Endpoint is unthrottled",
            }],
        },
        "performance": {"score": 1.0, "issues": []},
        "maintainability": {"score": 1.0, "issues": []},
        "overall_score": 0.9,
        "synthesis": "Minor advisory finding.",
    })
    assert CouncilOrchestrator._classify_perspective_evidence(advisory_payload) == "clear"

    assert CouncilOrchestrator._classify_perspective_evidence("") == "empty"
    assert CouncilOrchestrator._classify_perspective_evidence(None) == "empty"
    assert CouncilOrchestrator._classify_perspective_evidence("not a json") == "invalid"
    assert CouncilOrchestrator._classify_perspective_evidence('{"random": "dict"}') == "invalid"


if __name__ == "__main__":
    pytest.main(["-v", str(Path(__file__))])
