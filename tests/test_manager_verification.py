"""Rigorous verification test suite for Phase 4 Manager (Plan Gatekeeper) Optimization.

Verifies:
1. Prompt composition and byte-for-byte parity between composed and monolithic prompts.
2. Character and token metrics against target budgets (~2,400–2,800 chars, ~600–700 tokens).
3. fragments.json composition: shared/context_efficiency removed from manager.
4. Schema conformance of all example payloads with ManagerOutput.
5. Strict validation with validate_agent_output('manager', ..., strict=True).
6. Non-empty evidence and confidence on all issues.
7. Single task_id enforcement via validate_issue_task_id_shape.
8. Rejection of combined task IDs and malformed payloads.
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest
import tiktoken

from council_of_agents.scripts.council_schemas import (
    ManagerOutput,
    Verdict,
    validate_agent_output,
    validate_issue_task_id_shape,
)
from council_of_agents.scripts.prompt_composer import PromptComposer

PROMPTS_DIR = ROOT / "council_of_agents" / "prompts"


def test_manager_prompt_character_and_token_counts():
    """Verify prompt character and token counts fall in the optimized target range."""
    composer = PromptComposer(PROMPTS_DIR)
    composed = composer.compose("manager")
    monolithic = composer._load_monolithic("manager")

    # Byte-for-byte parity
    assert composed == monolithic, "Composed prompt does not match monolithic prompt byte-for-byte!"

    char_count = len(composed)
    enc = tiktoken.get_encoding("cl100k_base")
    token_count = len(enc.encode(composed))
    approx_tokens_4char = char_count / 4.0

    print(f"\n[METRICS] Composed Characters: {char_count}")
    print(f"[METRICS] Tiktoken (cl100k_base) Tokens: {token_count}")
    print(f"[METRICS] Approx (chars/4) Tokens: {approx_tokens_4char:.1f}")

    # Target: ~2,400–2,800 chars (~600–700 tokens), down from 7,187 characters
    assert 2400 <= char_count <= 2850, f"Character count {char_count} outside target window [2400, 2850]"
    assert 600 <= token_count <= 750, f"Token count {token_count} outside target window [600, 750]"


def test_composed_monolithic_byte_parity():
    """Verify byte-for-byte identical content between composed and monolithic."""
    composer = PromptComposer(PROMPTS_DIR)
    composed = composer.compose("manager")
    monolithic = (PROMPTS_DIR / "manager.md").read_text(encoding="utf-8")
    assert composed == monolithic


def test_registry_excludes_context_efficiency():
    """Verify shared/context_efficiency is removed from manager composition in fragments.json."""
    registry = json.loads((PROMPTS_DIR / "fragments.json").read_text(encoding="utf-8"))
    manager_composition = registry["compositions"]["manager"]

    assert "shared/context_efficiency" not in manager_composition, (
        "shared/context_efficiency must not be present in manager composition!"
    )
    expected = [
        "manager/identity",
        "manager/instructions",
        "manager/verdict_guidance",
        "manager/output_format",
        "manager/examples",
    ]
    assert manager_composition == expected


def test_all_manager_fragments_exist_and_nonempty():
    """Verify all 5 manager fragments exist and are non-empty."""
    manager_dir = PROMPTS_DIR / "fragments" / "manager"
    expected_files = [
        "identity.md",
        "instructions.md",
        "verdict_guidance.md",
        "output_format.md",
        "examples.md",
    ]
    for filename in expected_files:
        p = manager_dir / filename
        assert p.exists(), f"Fragment missing: {p}"
        content = p.read_text(encoding="utf-8").strip()
        assert len(content) > 0, f"Fragment empty: {p}"


def test_examples_payload_schema_conformance():
    """Verify all examples in examples.md conform to ManagerOutput schema and strict validation."""
    examples_path = PROMPTS_DIR / "fragments" / "manager" / "examples.md"
    examples_text = examples_path.read_text(encoding="utf-8")

    matches = list(re.finditer(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", examples_text))
    assert len(matches) == 3, f"Expected 3 examples (APPROVED, REVISE, BLOCKED), found {len(matches)}"

    expected_verdicts = [Verdict.APPROVED, Verdict.REVISE, Verdict.BLOCKED]

    for match, exp_verdict in zip(matches, expected_verdicts):
        raw_json = match.group(0)
        data = json.loads(raw_json)

        # 1. Pydantic validation
        model = ManagerOutput.model_validate(data)
        assert model.verdict == exp_verdict
        assert 0.0 <= model.confidence <= 1.0
        assert len(model.summary) > 0
        assert len(model.issues) >= 1

        for issue in model.issues:
            assert issue.severity in ("critical", "warning", "info")
            assert issue.task_id in ("T1", "T2", "ALL")
            assert len(issue.description.strip()) > 0
            assert len(issue.suggestion.strip()) > 0
            assert len(issue.evidence.strip()) > 0, "Issue evidence must be non-empty"

        # 2. Strict validate_agent_output
        val_res = validate_agent_output("manager", raw_json, strict=True)
        assert val_res.success is True, f"Strict validation failed: {val_res.error}"


def test_single_task_id_rule_enforcement():
    """Verify validate_issue_task_id_shape accepts valid IDs and rejects combined or empty IDs."""
    valid_ids = ["T1", "T2", "T10", "ALL"]
    for tid in valid_ids:
        ok, res = validate_issue_task_id_shape(tid)
        assert ok is True, f"Expected valid task_id for '{tid}'"

    invalid_ids = ["T1,T2", "T1;T2", "T1 T2", "T1/T2", "", "   "]
    for tid in invalid_ids:
        ok, reason = validate_issue_task_id_shape(tid)
        assert ok is False, f"Expected invalid task_id for '{tid}'"


def test_strict_manager_validation_rejects_combined_task_id():
    """Verify validate_agent_output rejects Manager issues with combined task IDs."""
    payload = {
        "verdict": "REVISE",
        "confidence": 0.5,
        "summary": "Combined task ID test",
        "issues": [
            {
                "severity": "critical",
                "task_id": "T1,T2",
                "description": "Invalid combined task ID",
                "suggestion": "Split into distinct issues",
                "evidence": "T1 and T2",
            }
        ],
    }
    res = validate_agent_output("manager", json.dumps(payload), strict=True)
    assert res.success is False
    assert "invalid task_id shape" in res.error


if __name__ == "__main__":
    pytest.main(["-v", str(Path(__file__))])
