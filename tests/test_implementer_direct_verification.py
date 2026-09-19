"""Rigorous verification test suite for Phase 6 Implementer Direct (Read-only Execution) Optimization.

Verifies:
1. Prompt composition and byte-for-byte parity between composed and monolithic prompts.
2. Character and token metrics against target budgets (~1,800–2,100 chars, ~450–525 tokens).
3. fragments.json composition: shared/context_efficiency, implementer_direct/execution_flow,
   and implementer_direct/permission_handling removed.
4. All required fragments exist and are non-empty.
5. Presence of required prompt tags (<identity>, <instructions>, <tool_selection>, <output_format>).
6. Enforcement of read-only exploration and prohibition of file mutation and JSON status blocks.
7. Runtime tool matrix verifies write_file and edit_file are dropped in DIRECT route.
"""

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest

from council_of_agents.scripts.council_orchestrator import (
    IMPLEMENTER_DIRECT_TOOLS,
    IMPLEMENTER_TOOLS,
    tools_for_role,
)
from council_of_agents.scripts.prompt_composer import PromptComposer

PROMPTS_DIR = ROOT / "council_of_agents" / "prompts"


def test_implementer_direct_prompt_character_and_token_counts():
    """Verify prompt character and token counts fall in the optimized target range."""
    composer = PromptComposer(PROMPTS_DIR)
    composed = composer.compose("implementer_direct")
    monolithic = composer._load_monolithic("implementer_direct")

    # Byte-for-byte parity
    assert composed == monolithic, "Composed prompt does not match monolithic prompt byte-for-byte!"

    char_count = len(composed)
    approx_tokens_4char = char_count / 4.0

    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        token_count = len(enc.encode(composed))
    except ImportError:
        token_count = int(approx_tokens_4char)

    print(f"\n[METRICS] Composed Characters: {char_count}")
    print(f"[METRICS] Tiktoken (cl100k_base) Tokens: {token_count}")
    print(f"[METRICS] Approx (chars/4) Tokens: {approx_tokens_4char:.1f}")

    # Target: ~1,800–2,100 chars (~450–525 tokens), down from 5,677 characters
    assert 1800 <= char_count <= 2100, f"Character count {char_count} outside target window [1800, 2100]"
    assert 400 <= token_count <= 525, f"Token count {token_count} outside target window [400, 525]"
    assert 450 <= approx_tokens_4char <= 525, f"Approx token count {approx_tokens_4char} outside target window [450, 525]"


def test_composed_monolithic_byte_parity():
    """Verify byte-for-byte identical content between composed and monolithic."""
    composer = PromptComposer(PROMPTS_DIR)
    composed = composer.compose("implementer_direct")
    monolithic = (PROMPTS_DIR / "implementer_direct.md").read_text(encoding="utf-8")
    assert composed == monolithic


def test_registry_implementer_direct_composition():
    """Verify fragments.json composition for implementer_direct."""
    registry = json.loads((PROMPTS_DIR / "fragments.json").read_text(encoding="utf-8"))
    composition = registry["compositions"]["implementer_direct"]

    assert "shared/context_efficiency" not in composition, (
        "shared/context_efficiency must not be in implementer_direct composition!"
    )
    assert "implementer_direct/execution_flow" not in composition, (
        "implementer_direct/execution_flow must not be in implementer_direct composition!"
    )
    assert "implementer_direct/permission_handling" not in composition, (
        "implementer_direct/permission_handling must not be in implementer_direct composition!"
    )

    expected = [
        "implementer_direct/identity",
        "implementer_direct/instructions",
        "implementer_direct/tool_selection",
        "implementer_direct/output_format",
    ]
    assert composition == expected


def test_all_implementer_direct_fragments_exist_and_nonempty():
    """Verify all 4 registered implementer_direct fragments exist and are non-empty."""
    id_dir = PROMPTS_DIR / "fragments" / "implementer_direct"
    expected_files = [
        "identity.md",
        "instructions.md",
        "tool_selection.md",
        "output_format.md",
    ]
    for filename in expected_files:
        p = id_dir / filename
        assert p.exists(), f"Fragment missing: {p}"
        content = p.read_text(encoding="utf-8").strip()
        assert len(content) > 0, f"Fragment empty: {p}"


def test_required_tags_and_forbidden_mutations():
    """Verify required XML sections and mutation guardrails in implementer_direct."""
    content = (PROMPTS_DIR / "implementer_direct.md").read_text(encoding="utf-8")
    assert "<identity>" in content
    assert "</identity>" in content
    assert "<instructions>" in content
    assert "</instructions>" in content
    assert "<tool_selection>" in content
    assert "</tool_selection>" in content
    assert "<output_format>" in content
    assert "</output_format>" in content

    # Mutation prohibition
    assert "write_file" in content and "FORBIDDEN" in content, "implementer_direct must forbid write_file"
    assert "edit_file" in content and "FORBIDDEN" in content, "implementer_direct must forbid edit_file"

    # Status block prohibition
    assert "MANDATORY JSON" not in content, "implementer_direct must not mandate JSON status block"
    assert "Prohibit JSON status blocks" in content


def test_direct_tools_matrix_restrictions():
    """Verify tool matrix drops mutation tools on DIRECT route."""
    pipeline_tools = tools_for_role("implementer", "PIPELINE")
    direct_tools = tools_for_role("implementer", "DIRECT")

    assert "write_file" in pipeline_tools and "edit_file" in pipeline_tools
    assert "write_file" not in direct_tools and "edit_file" not in direct_tools
    assert direct_tools == set(IMPLEMENTER_DIRECT_TOOLS)
    assert {"ls", "glob", "grep", "read_file", "bash", "python"} <= direct_tools
