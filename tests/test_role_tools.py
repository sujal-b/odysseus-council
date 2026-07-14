"""Tests for the per-role tool policy (single source of truth).

Locks in the capability matrix: reviewer/auditor roles can READ (to ground
verdicts) but never mutate; only the Implementer writes; DIRECT route is
read-only; unknown roles get nothing.
"""
from council_of_agents.scripts.council_orchestrator import (
    tools_for_role,
    READ_ONLY_TOOLS,
    IMPLEMENTER_TOOLS,
)

WRITE_OR_EXEC = {"write_file", "edit_file", "bash", "python"}
READER_ROLES = ["perspective_analyzer", "validator_task", "completeness_auditor"]
INSPECT_ROLES = ["strategist", "manager"]


def test_chair_has_no_tools():
    # Chair is a pure router/classifier — it must delegate file work, never read
    # or write files itself.
    assert tools_for_role("chair") == set()
    assert not tools_for_role("chair")


def test_reviewer_roles_can_read_not_mutate():
    for role in READER_ROLES:
        tools = tools_for_role(role)
        assert "read_file" in tools and "grep" in tools and "glob" in tools
        assert tools == set(READ_ONLY_TOOLS)
        assert not (tools & WRITE_OR_EXEC)          # never write/exec


def test_inspect_roles_are_read_only():
    for role in INSPECT_ROLES:
        tools = tools_for_role(role)
        assert "read_file" in tools
        assert not (tools & WRITE_OR_EXEC)


def test_implementer_full_pipeline():
    tools = tools_for_role("implementer", "PIPELINE")
    assert tools == set(IMPLEMENTER_TOOLS)
    assert {"write_file", "edit_file", "bash"} <= tools


def test_implementer_direct_is_read_only_investigation():
    tools = tools_for_role("implementer", "DIRECT")
    assert "write_file" not in tools and "edit_file" not in tools
    assert {"read_file", "bash", "grep"} <= tools     # can still investigate


def test_unknown_role_gets_nothing():
    assert tools_for_role("nope") == set()
    assert tools_for_role("debate_response") == set()
    # Empty set is falsy → matches the old dict.get(role) skip semantics.
    assert not tools_for_role("nope")


def test_returns_fresh_mutable_set_isolated_from_source():
    a = tools_for_role("manager")
    a.add("sentinel")
    b = tools_for_role("manager")
    assert "sentinel" not in b                        # source not mutated
    assert isinstance(b, set)


def test_completeness_auditor_can_open_files():
    # The auditor must read files to ground the completeness loop (anti-hallucination).
    assert "read_file" in tools_for_role("completeness_auditor")
