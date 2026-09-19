"""Rigorous verification test suite for Phase 5 Validator Task (Task Quality Gate) Optimization.

Verifies:
1. Prompt composition and byte-for-byte parity between composed and monolithic prompts.
2. Character and token metrics against target budgets (~1,400–1,600 chars, ~350–400 tokens).
3. fragments.json composition: shared/context_efficiency and validator_task/issue_format removed.
4. Schema conformance of all example payloads with ManagerOutput and strict validate_agent_output.
5. ACCEPT / RETRY alias normalization to APPROVED / REVISE in ManagerOutput.extract.
6. Elimination of role inversion in council_orchestrator.py execute_task.
"""

import inspect
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
)
from council_of_agents.scripts.prompt_composer import PromptComposer
import council_of_agents.scripts.council_orchestrator as orchestrator_module

PROMPTS_DIR = ROOT / "council_of_agents" / "prompts"


def test_validator_task_prompt_character_and_token_counts():
    """Verify prompt character and token counts fall in the optimized target range."""
    composer = PromptComposer(PROMPTS_DIR)
    composed = composer.compose("validator_task")
    monolithic = composer._load_monolithic("validator_task")

    # Byte-for-byte parity
    assert composed == monolithic, "Composed prompt does not match monolithic prompt byte-for-byte!"

    char_count = len(composed)
    enc = tiktoken.get_encoding("cl100k_base")
    token_count = len(enc.encode(composed))
    approx_tokens_4char = char_count / 4.0

    print(f"\n[METRICS] Composed Characters: {char_count}")
    print(f"[METRICS] Tiktoken (cl100k_base) Tokens: {token_count}")
    print(f"[METRICS] Approx (chars/4) Tokens: {approx_tokens_4char:.1f}")

    # Target: ~1,400–1,600 chars (~350–400 tokens), down from 3,924 characters
    assert 1400 <= char_count <= 1650, f"Character count {char_count} outside target window [1400, 1650]"
    assert 350 <= token_count <= 410, f"Token count {token_count} outside target window [350, 410]"


def test_composed_monolithic_byte_parity():
    """Verify byte-for-byte identical content between composed and monolithic."""
    composer = PromptComposer(PROMPTS_DIR)
    composed = composer.compose("validator_task")
    monolithic = (PROMPTS_DIR / "validator_task.md").read_text(encoding="utf-8")
    assert composed == monolithic


def test_registry_validator_task_composition():
    """Verify shared/context_efficiency and validator_task/issue_format are removed from fragments.json."""
    registry = json.loads((PROMPTS_DIR / "fragments.json").read_text(encoding="utf-8"))
    vt_composition = registry["compositions"]["validator_task"]

    assert "shared/context_efficiency" not in vt_composition, (
        "shared/context_efficiency must not be present in validator_task composition!"
    )
    assert "validator_task/issue_format" not in vt_composition, (
        "validator_task/issue_format must not be present in validator_task composition!"
    )
    expected = [
        "validator_task/identity",
        "validator_task/instructions",
        "validator_task/output_format",
        "validator_task/examples",
    ]
    assert vt_composition == expected


def test_all_validator_task_fragments_exist_and_nonempty():
    """Verify all 4 registered validator_task fragments exist and are non-empty."""
    vt_dir = PROMPTS_DIR / "fragments" / "validator_task"
    expected_files = [
        "identity.md",
        "instructions.md",
        "output_format.md",
        "examples.md",
    ]
    for filename in expected_files:
        p = vt_dir / filename
        assert p.exists(), f"Fragment missing: {p}"
        content = p.read_text(encoding="utf-8").strip()
        assert len(content) > 0, f"Fragment empty: {p}"


def test_manager_output_accept_retry_alias_normalization():
    """Verify ACCEPT -> APPROVED and RETRY -> REVISE alias normalization in ManagerOutput."""
    # Dict input: ACCEPT
    res_accept = ManagerOutput.model_validate({"verdict": "ACCEPT", "summary": "Task complete."})
    assert res_accept.verdict == Verdict.APPROVED
    assert res_accept.verdict == "APPROVED"

    # Dict input: RETRY
    res_retry = ManagerOutput.model_validate({"verdict": "RETRY", "summary": "Need rework."})
    assert res_retry.verdict == Verdict.REVISE
    assert res_retry.verdict == "REVISE"

    # Lowercase variations
    res_lower_accept = ManagerOutput.model_validate({"verdict": "accept", "summary": "ok"})
    assert res_lower_accept.verdict == Verdict.APPROVED

    res_lower_retry = ManagerOutput.model_validate({"verdict": "retry", "summary": "fix"})
    assert res_lower_retry.verdict == Verdict.REVISE

    # JSON string input
    res_str_accept = ManagerOutput.model_validate('{"verdict": "ACCEPT", "summary": "ok"}')
    assert res_str_accept.verdict == Verdict.APPROVED

    res_str_retry = ManagerOutput.model_validate('{"verdict": "RETRY", "summary": "fix"}')
    assert res_str_retry.verdict == Verdict.REVISE

    # Standard APPROVED / REVISE remain unchanged
    res_approved = ManagerOutput.model_validate({"verdict": "APPROVED", "summary": "ok"})
    assert res_approved.verdict == Verdict.APPROVED

    res_revise = ManagerOutput.model_validate({"verdict": "REVISE", "summary": "fix"})
    assert res_revise.verdict == Verdict.REVISE

    # Strict validate_agent_output with ACCEPT and RETRY
    val_accept = validate_agent_output("manager", '{"verdict": "ACCEPT", "summary": "done"}', strict=True)
    assert val_accept.success is True
    assert val_accept.data["verdict"] == "APPROVED"

    val_retry = validate_agent_output("manager", '{"verdict": "RETRY", "summary": "fix"}', strict=True)
    assert val_retry.success is True
    assert val_retry.data["verdict"] == "REVISE"


def test_validator_task_examples_schema_conformance():
    """Verify all examples in examples.md are strictly schema-conformant with ManagerOutput."""
    examples_path = PROMPTS_DIR / "fragments" / "validator_task" / "examples.md"
    content = examples_path.read_text(encoding="utf-8")

    json_blocks = re.findall(r"```json\s*\n(.*?)\n```", content, re.DOTALL)
    assert len(json_blocks) >= 2, "Expected at least 2 example JSON blocks in examples.md"

    for i, block in enumerate(json_blocks):
        parsed_json = json.loads(block)
        # Validate through ManagerOutput model
        output_model = ManagerOutput.model_validate(parsed_json)
        assert output_model.verdict in (Verdict.APPROVED, Verdict.REVISE)

        # Validate through validate_agent_output (strict mode)
        val_res = validate_agent_output("manager", block, strict=True)
        assert val_res.success is True, f"Example {i+1} failed strict validation: {val_res.error}"
        assert val_res.data is not None

    # Example 1: APPROVED with empty issues
    ex1 = json.loads(json_blocks[0])
    assert ex1["verdict"] == "APPROVED"
    assert ex1["issues"] == []

    # Example 2: REVISE with actionable issue
    ex2 = json.loads(json_blocks[1])
    assert ex2["verdict"] == "REVISE"
    assert len(ex2["issues"]) >= 1
    assert ex2["issues"][0]["task_id"] != ""
    assert ex2["issues"][0]["severity"] in ("critical", "warning", "info")


def test_role_inversion_eliminated_in_orchestrator():
    """Verify execute_task formats the implementer output as user message rather than assistant."""
    source = Path(orchestrator_module.__file__).read_text(encoding="utf-8")
    # The review call must NOT pass {"role": "assistant", "content": impl_reply}
    assert '{"role": "assistant", "content": impl_reply}' not in source, (
        "Role inversion detected: impl_reply must not be passed as role 'assistant'!"
    )
    # Must use user message with <task_output> tags
    assert '<task_output>\\n{impl_reply}\\n</task_output>' in source or '<task_output>' in source, (
        "Expected impl_reply wrapped in <task_output> as user message in execute_task."
    )
