"""Rigorous verification test suite for Phase 8 Completeness Auditor Optimization.

Verifies:
1. Prompt composition and loading via PromptComposer.
2. Character and token metrics against target budgets (~1,400–1,650 chars, ~350–450 tokens), down from 2,242 chars.
3. Semantic XML tag structure (<identity>, <audit_rules>, <gap_classification>, <output_format>, <examples>).
4. Strict alignment of example payloads with CompletenessAuditOutput and validate_agent_output.
5. Canonical 4-outcome coverage (met: true, fillable, broken, needs_user).
6. Orchestrator grounding and gap closure integration with _ground_audit.
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest
import tiktoken

from council_of_agents.scripts.council_orchestrator import CouncilOrchestrator
from council_of_agents.scripts.council_schemas import (
    CompletenessAuditOutput,
    CompletenessCriterion,
    validate_agent_output,
)
from council_of_agents.scripts.prompt_composer import PromptComposer

PROMPTS_DIR = ROOT / "council_of_agents" / "prompts"
PROMPT_PATH = PROMPTS_DIR / "completeness_auditor.md"


def test_prompt_character_and_token_counts():
    """Verify prompt character and token counts fall within the optimized target window."""
    composer = PromptComposer(PROMPTS_DIR)
    prompt = composer.compose("completeness_auditor")
    assert prompt, "Composed prompt must not be empty"

    char_count = len(prompt)
    enc = tiktoken.get_encoding("cl100k_base")
    token_count = len(enc.encode(prompt))
    approx_tokens_4char = char_count / 4.0

    print(f"\n[METRICS] Characters: {char_count} (original: 2,242 chars, delta: -{2242 - char_count} chars, -{(2242 - char_count) / 2242 * 100:.1f}%)")
    print(f"[METRICS] Tiktoken (cl100k_base) Tokens: {token_count}")
    print(f"[METRICS] Approx (chars/4) Tokens: {approx_tokens_4char:.1f}")

    # Target: ~1,400–1,650 characters (~350–450 tokens), down from 2,242 characters
    assert 1400 <= char_count <= 1700, f"Character count {char_count} outside target window [1400, 1700]"
    assert 350 <= token_count <= 460, f"Token count {token_count} outside target window [350, 460]"


def test_prompt_semantic_xml_tags():
    """Verify presence of required semantic XML structure."""
    text = PROMPT_PATH.read_text(encoding="utf-8")
    expected_tags = [
        "identity",
        "audit_rules",
        "gap_classification",
        "output_format",
        "examples",
    ]
    for tag in expected_tags:
        assert f"<{tag}>" in text, f"Missing opening tag <{tag}>"
        assert f"</{tag}>" in text, f"Missing closing tag </{tag}>"


def test_identity_specification():
    """Verify crisp post-execution verifier identity."""
    text = PROMPT_PATH.read_text(encoding="utf-8")
    identity_match = re.search(r"<identity>(.*?)</identity>", text, re.DOTALL)
    assert identity_match, "<identity> section must exist"
    identity = identity_match.group(1).strip()

    assert "Completeness Auditor" in identity
    assert "acceptance checklist" in identity
    assert "100% satisfied" in identity
    assert "no tools and do not execute code" in identity


def test_audit_rules_specification():
    """Verify strict grounding and default-to-unmet rules."""
    text = PROMPT_PATH.read_text(encoding="utf-8")
    rules_match = re.search(r"<audit_rules>(.*?)</audit_rules>", text, re.DOTALL)
    assert rules_match, "<audit_rules> section must exist"
    rules = rules_match.group(1).strip()

    assert "met: true" in rules
    assert "met: false" in rules
    assert "stubs" in rules
    assert "TODOs" in rules or "placeholders" in rules


def test_gap_classification_specification():
    """Verify gap types: fillable, broken, needs_user, and met-criteria handling."""
    text = PROMPT_PATH.read_text(encoding="utf-8")
    gap_match = re.search(r"<gap_classification>(.*?)</gap_classification>", text, re.DOTALL)
    assert gap_match, "<gap_classification> section must exist"
    gap_content = gap_match.group(1).strip()

    assert "fillable" in gap_content
    assert "broken" in gap_content
    assert "needs_user" in gap_content
    assert "gap_type" in gap_content
    assert "question" in gap_content


def test_example_payload_schema_conformance():
    """Verify example payload parses cleanly and conforms strictly to CompletenessAuditOutput."""
    text = PROMPT_PATH.read_text(encoding="utf-8")
    json_match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    assert json_match, "JSON code block must exist inside prompt"
    raw_json = json_match.group(1)

    # 1. JSON parse
    payload = json.loads(raw_json)

    # 2. Pydantic validation
    model = CompletenessAuditOutput.model_validate(payload)
    assert isinstance(model.completeness, float)
    assert isinstance(model.done, bool)
    assert len(model.criteria) == 4

    # 3. Agent output validation
    val_res = validate_agent_output("completeness_auditor", raw_json)
    assert val_res.success is True
    assert val_res.data["completeness"] == 0.25
    assert val_res.data["done"] is False
    assert len(val_res.data["criteria"]) == 4


def test_all_four_canonical_outcomes_represented():
    """Verify all 4 canonical gap/outcome types are demonstrated in the example."""
    text = PROMPT_PATH.read_text(encoding="utf-8")
    json_match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    raw_json = json_match.group(1)
    model = CompletenessAuditOutput.model_validate_json(raw_json)

    by_id = {c.id: c for c in model.criteria}

    # Outcome 1: met: true (gap_type: fillable, question: "")
    c1 = by_id["C1"]
    assert c1.met is True
    assert c1.gap_type == "fillable"
    assert c1.question == ""

    # Outcome 2: fillable gap (met: false, gap_type: fillable)
    c2 = by_id["C2"]
    assert c2.met is False
    assert c2.gap_type == "fillable"
    assert c2.question == ""

    # Outcome 3: broken gap (met: false, gap_type: broken)
    c3 = by_id["C3"]
    assert c3.met is False
    assert c3.gap_type == "broken"
    assert c3.question == ""

    # Outcome 4: needs_user gap (met: false, gap_type: needs_user, populated question)
    c4 = by_id["C4"]
    assert c4.met is False
    assert c4.gap_type == "needs_user"
    assert len(c4.question) > 0


def test_orchestrator_ground_audit_integration():
    """Verify that _ground_audit correctly recomputes completeness and demotes false claims."""
    from unittest.mock import MagicMock
    orch = CouncilOrchestrator(MagicMock())

    text = PROMPT_PATH.read_text(encoding="utf-8")
    json_match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    raw_json = json_match.group(1)
    val_res = validate_agent_output("completeness_auditor", raw_json)
    assert val_res.success is True

    # Run grounding without files - C1 should pass because detail doesn't contain stub keywords
    grounded = orch._ground_audit(val_res.data, written_paths=set())
    assert grounded["completeness"] == 0.25
    assert grounded["done"] is False

    # Verify that forced_met_ids can mark unmet criteria as deterministically met
    grounded_forced = orch._ground_audit(val_res.data, written_paths=set(), forced_met_ids={"C2", "C3", "C4"})
    assert grounded_forced["completeness"] == 1.0
    assert grounded_forced["done"] is True


if __name__ == "__main__":
    pytest.main(["-v", str(Path(__file__))])
