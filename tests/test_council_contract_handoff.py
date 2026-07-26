"""Unit tests for CouncilOrchestrator._contract — the structured handoff that
passes an agent's decision/output downstream instead of its full reasoning
transcript (token-efficient, production orchestrator pattern).

Covers: chair contract fields, strategist DAG-verbatim preservation, no-DAG
fallback, mode='full' bypass, empty input, and parse-failure safety.
"""
import json
from unittest.mock import MagicMock

from council_of_agents.scripts.council_orchestrator import CouncilOrchestrator


def _orch(mode="contract"):
    orch = CouncilOrchestrator(MagicMock())
    orch._handoff_mode = mode
    return orch


CHAIR_REPLY = (
    "```json\n"
    + json.dumps({
        "complexity": "COMPLEX",
        "route": "PIPELINE",
        "action": "write",
        "target": "app.py",
        "reason": "Touches multiple backend modules and needs a task plan.",
    })
    + "\n```"
)

STRAT_REPLY = (
    "We will refactor the auth layer in two steps.\n"
    "```tasks\n"
    '[{"id": "t1", "description": "extract token check", "depends_on": []}]\n'
    "```\n"
    "## Risks\nLow."
)


def test_chair_contract_extracts_decision_fields():
    out = _orch()._contract("chair", CHAIR_REPLY)
    contract = json.loads(out)
    assert contract["complexity"] == "COMPLEX"
    assert contract["route"] == "PIPELINE"
    assert contract["action"] == "write"
    assert contract["target"] == "app.py"
    # The decision is far smaller than the raw reply.
    assert len(out) < len(CHAIR_REPLY) + 200


def test_strategist_contract_keeps_dag_verbatim():
    out = _orch()._contract("strategist", STRAT_REPLY)
    # The task DAG block is the actionable plan — must survive byte-for-byte.
    assert "```tasks" in out
    assert '"id": "t1"' in out
    assert '"extract token check"' in out
    # The short rationale is preserved.
    assert "refactor the auth layer" in out


def test_strategist_json_contract_is_compacted_for_production_handoff():
    raw = '{"tasks":[{"id":"t1","description":"Inspect the existing path","acceptance":"The path is understood","read_scope":["src/"],"write_scope":[]}],"risks":[]}'
    out = _orch()._contract("strategist", raw)
    assert json.loads(out) == {
        "tasks": [{
            "id": "t1",
            "description": "Inspect the existing path",
            "acceptance": "The path is understood",
            "read_scope": ["src/"],
            "write_scope": [],
        }]
    }
    assert len(out) < len(raw)


def test_strategist_no_dag_returns_raw():
    raw = "Just some prose with no task block at all."
    assert _orch()._contract("strategist", raw) == raw


def test_full_mode_bypasses_contract():
    orch = _orch(mode="full")
    assert orch._contract("chair", CHAIR_REPLY) == CHAIR_REPLY
    assert orch._contract("strategist", STRAT_REPLY) == STRAT_REPLY


def test_empty_reply_returns_empty():
    assert _orch()._contract("chair", "") == ""
    assert _orch()._contract("strategist", None) is None


def test_unknown_role_returns_raw():
    raw = "implementer output text"
    assert _orch()._contract("implementer", raw) == raw


def test_garbage_chair_reply_does_not_raise():
    # Parsers fall back to defaults; must never raise on malformed input.
    out = _orch()._contract("chair", "not json at all {{{")
    assert "complexity:" in out  # still produces a contract with fallbacks


def test_default_mode_is_contract():
    # Without the env override, handoff defaults to the contract path.
    import os
    prev = os.environ.pop("COUNCIL_HANDOFF_MODE", None)
    try:
        orch = CouncilOrchestrator(MagicMock())
        assert orch._handoff_mode == "contract"
    finally:
        if prev is not None:
            os.environ["COUNCIL_HANDOFF_MODE"] = prev
