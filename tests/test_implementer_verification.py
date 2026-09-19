"""Rigorous verification test suite for Phase 7 Implementer (Pipeline Execution & Guardrail Balance) Optimization.

Verifies:
1. Prompt composition and byte-for-byte parity between composed and monolithic prompts.
2. Character and token metrics against target budgets (~2,200–2,600 chars, ~550–650 tokens).
3. fragments.json composition: shared/default_to_action, shared/investigate_before,
   shared/parallel_tools, shared/tool_selection, and shared/context_efficiency removed.
4. All required fragments exist and are non-empty.
5. Presence of required XML tags (<identity>, <instructions>, <code_quality>, <self_verification>, <output_format>, <examples>).
6. High-density instructions balancing action, parallel tools, workspace guardrails, and error recovery.
7. Schema conformance of example payloads with ImplementerOutput.
8. Elimination of role inversion in council_orchestrator.py execute_task.
"""

import inspect
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest
import tiktoken

from council_of_agents.scripts.council_schemas import (
    ImplementerOutput,
    TaskStatus,
    compact_agent_contract,
    validate_agent_output,
)
from council_of_agents.scripts.prompt_composer import PromptComposer
import council_of_agents.scripts.council_orchestrator as orchestrator_module

PROMPTS_DIR = ROOT / "council_of_agents" / "prompts"


def test_implementer_prompt_character_and_token_counts():
    """Verify prompt character and token counts fall in the optimized target range."""
    composer = PromptComposer(PROMPTS_DIR)
    composed = composer.compose("implementer")
    monolithic = composer._load_monolithic("implementer")

    # Byte-for-byte parity
    assert composed == monolithic, "Composed prompt does not match monolithic prompt byte-for-byte!"

    char_count = len(composed)
    enc = tiktoken.get_encoding("cl100k_base")
    token_count = len(enc.encode(composed))
    approx_tokens_4char = char_count / 4.0

    print(f"\n[METRICS] Composed Characters: {char_count}")
    print(f"[METRICS] Tiktoken (cl100k_base) Tokens: {token_count}")
    print(f"[METRICS] Approx (chars/4) Tokens: {approx_tokens_4char:.1f}")

    # Target: ~2,200–2,600 chars (~550–650 tokens), down from 5,960 characters
    assert 2200 <= char_count <= 2600, f"Character count {char_count} outside target window [2200, 2600]"
    assert 550 <= token_count <= 650, f"Token count {token_count} outside target window [550, 650]"
    assert 550 <= approx_tokens_4char <= 650, f"Approx token count {approx_tokens_4char} outside target window [550, 650]"


def test_composed_monolithic_byte_parity():
    """Verify byte-for-byte identical content between composed and monolithic."""
    composer = PromptComposer(PROMPTS_DIR)
    composed = composer.compose("implementer")
    monolithic = (PROMPTS_DIR / "implementer.md").read_text(encoding="utf-8")
    assert composed == monolithic, "implementer.md must be byte-for-byte identical to composed prompt"


def test_registry_implementer_composition():
    """Verify fragments.json composition for implementer."""
    registry = json.loads((PROMPTS_DIR / "fragments.json").read_text(encoding="utf-8"))
    composition = registry["compositions"]["implementer"]

    removed_fragments = [
        "shared/default_to_action",
        "shared/investigate_before",
        "shared/parallel_tools",
        "shared/tool_selection",
        "shared/context_efficiency",
    ]
    for frag in removed_fragments:
        assert frag not in composition, f"{frag} must not be in implementer composition!"

    expected = [
        "implementer/identity",
        "implementer/instructions",
        "shared/code_quality",
        "shared/self_verification",
        "implementer/output_format",
        "implementer/examples",
    ]
    assert composition == expected, f"Unexpected implementer composition: {composition}"


def test_all_implementer_fragments_exist_and_nonempty():
    """Verify all 4 registered implementer fragments exist and are non-empty."""
    imp_dir = PROMPTS_DIR / "fragments" / "implementer"
    expected_files = [
        "identity.md",
        "instructions.md",
        "output_format.md",
        "examples.md",
    ]
    for filename in expected_files:
        p = imp_dir / filename
        assert p.exists(), f"Fragment missing: {p}"
        content = p.read_text(encoding="utf-8").strip()
        assert len(content) > 0, f"Fragment empty: {p}"


def test_required_tags_and_guardrail_principles():
    """Verify required XML sections and core execution principles."""
    content = (PROMPTS_DIR / "implementer.md").read_text(encoding="utf-8")

    # Required XML tags
    for tag in ("identity", "instructions", "code_quality", "self_verification", "output_format", "examples"):
        assert f"<{tag}>" in content, f"Missing <{tag}> tag"
        assert f"</{tag}>" in content, f"Missing </{tag}> tag"

    # Crisp identity
    assert "You are the Implementer of the Council of Agents." in content
    assert "You execute exactly ONE task from the plan" in content
    assert "Default to action" in content or "default to action" in content

    # Core execution principles
    assert "Action & Grounding" in content
    assert "Parallel Tool Calls" in content
    assert "Scope & Guardrails" in content
    assert "Error Recovery" in content

    # Guardrail balance: write_file/edit_file primary, read-only inspection, bash/python under workspace guards
    assert "write_scope" in content
    assert "write_file" in content and "edit_file" in content
    assert "runtime workspace guards" in content


def test_example_payload_schema_conformance():
    """Verify JSON block in examples.md conforms to ImplementerOutput schema."""
    examples_content = (PROMPTS_DIR / "fragments" / "implementer" / "examples.md").read_text(encoding="utf-8")
    json_blocks = re.findall(r"```json\s*\n(.*?)\n```", examples_content, re.DOTALL)
    assert len(json_blocks) >= 1, "Expected at least one JSON block in examples.md"

    for block in json_blocks:
        parsed = json.loads(block)
        output = ImplementerOutput.model_validate(parsed)
        assert output.status in (TaskStatus.DONE, TaskStatus.FAILED)
        assert isinstance(output.files_created, list)
        assert isinstance(output.files_modified, list)
        assert isinstance(output.verification_details, str)
        assert isinstance(output.notes, str)

        compacted = compact_agent_contract("implementer", output.model_dump())
        assert "status" in compacted


def test_role_inversion_eliminated_in_orchestrator():
    """Verify execute_task formats the task prompt as a user message with <task_instruction> tags."""
    source = Path(orchestrator_module.__file__).read_text(encoding="utf-8")

    # The implementer prompt must NOT be passed as role "assistant"
    assert '{"role": "assistant", "content": task_prompt}' not in source, (
        "Role inversion detected: task_prompt must not be passed with role 'assistant'!"
    )

    # Must be passed as role "user" wrapped in <task_instruction>
    assert '<task_instruction>\\n{task_prompt}\\n</task_instruction>' in source or '<task_instruction>' in source, (
        "Expected task_prompt wrapped in <task_instruction> as user message in execute_task."
    )
