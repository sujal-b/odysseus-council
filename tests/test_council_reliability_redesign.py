import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from council_of_agents.scripts.agent_runner import AgentRunner
from council_of_agents.scripts.council_retry import ErrorClass, classify_error
from council_of_agents.scripts.permissions import (
    PermissionManager,
    resolve_council_workspace,
)
from council_of_agents.scripts.session_store import InMemorySessionStore, SessionState
import council_of_agents.scripts.session_store as session_store_module
import src.tool_execution as tool_execution
from src.agent_loop import _compact_tool_outputs
from src.context_compactor import ProtectedContextOverflowError
from src.agent_tools.filesystem_tools import GlobTool, GrepTool, LsTool, ReadFileTool
from src.tool_reliability import annotate_tool_result, duplicate_tool_result, tool_call_fingerprint
from council_of_agents.scripts.context_tracker import ContextTracker


def test_workspace_is_canonical_and_relative_permission_targets_use_it(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()

    resolved = resolve_council_workspace(str(workspace))
    assert resolved == str(workspace.resolve()).lower()

    manager = PermissionManager(resolved, "owner", False)
    allowed, reason = manager.check_path_allowed(".")
    assert allowed is True
    assert reason == "within_workspace"

    file_target = workspace / "not-a-directory"
    file_target.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a directory"):
        resolve_council_workspace(str(file_target))


def test_tool_output_compaction_uses_ninety_percent_of_usable_budget():
    messages = [{"role": "system", "content": "system"}]
    messages.extend(
        {"role": "user", "content": "[Tool execution results]\n\n" + ("x" * 8000)}
        for _ in range(4)
    )

    compacted = _compact_tool_outputs(
        messages,
        context_length=10000,
        max_tokens=1000,
        input_budget=9000,
        incoming_tokens=50,
        keep_recent=1,
    )

    assert compacted == 3
    assert messages[1]["content"].startswith("[compacted")
    assert messages[-1]["content"].startswith("[Tool execution results]")


def test_context_errors_are_not_retried_as_transient():
    assert classify_error(
        ProtectedContextOverflowError(
            "Protected context and current turn cannot fit the selected model context."
        )
    ) == ErrorClass.CONTEXT
    assert classify_error(RuntimeError("maximum context length exceeded")) == ErrorClass.CONTEXT


def test_pending_gate_survives_session_store_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(session_store_module, "DATA_DIR", str(tmp_path))
    store = InMemorySessionStore()
    state = SessionState(
        session_id="gate-session",
        owner="owner",
        user_prompt="build the project",
        status="BLOCKED",
        pending_gate={
            "kind": "permission",
            "permission_id": "perm-1",
            "action": "bash",
            "target": "mkdir css",
        },
    )
    store.save(state)
    store._cache.clear()

    restored = store.load(state.session_id)
    assert restored is not None
    assert restored.pending_gate == state.pending_gate


@pytest.mark.asyncio
async def test_code_navigation_dispatch_preserves_workspace(tmp_path, monkeypatch):
    seen = {}

    async def fake_direct(tool, content, **kwargs):
        seen["tool"] = tool
        seen["workspace"] = kwargs.get("workspace")
        return {"output": "ok", "exit_code": 0}

    monkeypatch.setattr(tool_execution, "_direct_fallback", fake_direct)
    monkeypatch.setattr(tool_execution, "is_public_blocked_tool", lambda tool: False)
    block = SimpleNamespace(tool_type="glob", content='{"pattern":"*.js"}')

    description, result = await tool_execution.execute_tool_block(
        block,
        owner="owner",
        workspace=str(tmp_path),
    )

    assert description.startswith("glob:")
    assert result["exit_code"] == 0
    assert seen == {"tool": "glob", "workspace": str(tmp_path)}


def test_durable_gate_is_preferred_over_historical_log():
    from routes.council_routes import _gate_event, _pending_gate_from_state

    state = SessionState(
        session_id="gate-session",
        owner="owner",
        user_prompt="build the project",
        status="BLOCKED",
        log=[
            {
                "event": "permission_request",
                "status": "BLOCKED",
                "extra": {"permission_id": "old", "action": "bash", "target": "old"},
            }
        ],
        pending_gate={
            "kind": "permission",
            "permission_id": "current",
            "action": "write_file",
            "target": "current.js",
        },
    )

    assert _pending_gate_from_state(state)["permission_id"] == "current"
    replay = _gate_event(state)
    assert replay is not None
    assert replay.extra["permission_id"] == "current"
    assert replay.extra["replay"] is True


def test_nonapproved_manager_gate_requires_explicit_override():
    from fastapi import HTTPException
    from routes.council_routes import _validate_manager_review_choice

    gate = {
        "kind": "review",
        "manager_verdict": "REVISE",
        "requires_override": True,
    }
    with pytest.raises(HTTPException) as exc_info:
        _validate_manager_review_choice(gate, "approve")
    assert exc_info.value.status_code == 409
    _validate_manager_review_choice(gate, "override")


def test_old_manager_gate_is_normalized_and_fails_closed():
    from routes.council_routes import _gate_event, _normalize_review_gate, _pending_gate_from_state

    state = SessionState(
        session_id="old-review-gate",
        owner="owner",
        user_prompt="fix the project",
        status="BLOCKED",
        pending_gate={
            "kind": "review",
            "plan": "legacy plan",
            "manager_review": "not valid structured output",
        },
    )

    gate = _pending_gate_from_state(state)
    assert gate["manager_verdict"] == "BLOCKED"
    assert gate["requires_override"] is True
    assert _gate_event(state).extra["requires_override"] is True
    assert _normalize_review_gate({"kind": "review", "manager_verdict": "garbage"})["manager_verdict"] == "BLOCKED"


def test_unknown_manager_verdict_fails_closed():
    from council_of_agents.scripts.council_orchestrator import CouncilOrchestrator

    orchestrator = CouncilOrchestrator.__new__(CouncilOrchestrator)
    assert orchestrator._parse_manager_verdict("model said maybe") == "BLOCKED"
    assert orchestrator._parse_manager_verdict("") == "BLOCKED"


def test_revision_progress_requires_a_changed_plan_and_defect_signal():
    from council_of_agents.scripts.council_orchestrator import CouncilOrchestrator

    plan = '{"tasks":[{"id":"T1","description":"Inspect existing code","write_scope":[]}]}'
    revised = '{"tasks":[{"id":"T1","description":"Inspect existing code and tests","write_scope":[]}]}'
    manager = '{"verdict":"REVISE","issues":[{"task_id":"T1","description":"Missing tests","suggestion":"Add tests","evidence":"No test task"}]}'
    same_manager = manager
    changed_manager = '{"verdict":"REVISE","issues":[{"task_id":"T1","description":"Missing verification","suggestion":"Add a command","evidence":"No verification"}]}'

    assert not CouncilOrchestrator._revision_has_progress(plan, plan, manager, changed_manager)
    assert not CouncilOrchestrator._revision_has_progress(plan, revised, manager, same_manager)
    assert CouncilOrchestrator._revision_has_progress(plan, revised, manager, changed_manager)


def test_cancelling_one_session_does_not_release_another_permission_waiter():
    from council_of_agents.scripts.permissions import GLOBAL_REGISTRY
    from routes.council_routes import cancel_active_council_session

    first = asyncio.Event()
    second = asyncio.Event()
    GLOBAL_REGISTRY.pending_events.clear()
    GLOBAL_REGISTRY.results.clear()
    GLOBAL_REGISTRY.session_ids.clear()
    GLOBAL_REGISTRY.pending_events.update({"perm-a": first, "perm-b": second})
    GLOBAL_REGISTRY.session_ids.update({"perm-a": "session-a", "perm-b": "session-b"})
    try:
        cancel_active_council_session("session-a")
        assert first.is_set()
        assert not second.is_set()
        assert GLOBAL_REGISTRY.results["perm-a"] == {"approved": False}
        assert "perm-b" not in GLOBAL_REGISTRY.results
    finally:
        GLOBAL_REGISTRY.pending_events.clear()
        GLOBAL_REGISTRY.results.clear()
        GLOBAL_REGISTRY.session_ids.clear()


@pytest.mark.asyncio
async def test_agent_runner_uses_one_context_fallback_without_replaying_primary():
    valid = '{"complexity":"SIMPLE","route":"DIRECT","reason":"ok"}'
    calls = []
    events = []

    class Orchestrator:
        AGENT_TIMEOUTS = {}
        AGENT_MAX_RETRIES = {"chair": 0}

        def _context_fallback_for(self, role, overrides):
            return {
                "endpoint_url": "https://fallback.invalid/v1/chat/completions",
                "model": "large-context-recovery",
            }

        async def _call_agent(self, *args, **kwargs):
            calls.append(kwargs.get("context_fallback"))
            if len(calls) == 1:
                raise ProtectedContextOverflowError("context window exceeded")
            return valid

    async def emit(**kwargs):
        events.append(kwargs)

    state = SimpleNamespace(session_id="session", role_overrides={}, metadata={})
    runner = AgentRunner(Orchestrator(), state, emit, tracker=None)

    result = await runner.invoke(
        "chair",
        [{"role": "user", "content": "inspect the project"}],
        max_retries=0,
    )

    assert result == valid
    assert calls == [None, {"endpoint_url": "https://fallback.invalid/v1/chat/completions", "model": "large-context-recovery"}]
    assert any(event.get("event") == "context_recovery" for event in events)


@pytest.mark.asyncio
async def test_code_navigation_blank_paths_use_the_active_workspace(tmp_path):
    (tmp_path / "main.py").write_text("needle\n", encoding="utf-8")

    ls = await LsTool().execute("", {"workspace": str(tmp_path)})
    glob = await GlobTool().execute('{"pattern":"*.py"}', {"workspace": str(tmp_path)})
    grep = await GrepTool().execute('{"pattern":"needle"}', {"workspace": str(tmp_path)})

    assert ls["exit_code"] == 0 and "main.py" in ls["output"]
    assert glob["exit_code"] == 0 and "main.py" in glob["output"]
    assert grep["exit_code"] == 0 and "needle" in grep["output"]


@pytest.mark.asyncio
async def test_trivial_bash_ls_uses_workspace_navigator(monkeypatch, tmp_path):
    (tmp_path / "main.py").write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(tool_execution, "is_public_blocked_tool", lambda tool: False)
    monkeypatch.setattr(tool_execution, "owner_is_admin_or_single_user", lambda owner: True)

    description, result = await tool_execution.execute_tool_block(
        SimpleNamespace(tool_type="bash", content="ls"),
        owner="owner",
        workspace=str(tmp_path),
        skip_workspace_check=True,
    )
    assert description == "ls: workspace discovery"
    assert result["exit_code"] == 0
    assert result["routing"] == "bash->ls"
    assert "main.py" in result["output"]


@pytest.mark.asyncio
async def test_line_range_read_rejects_stale_hash_and_reports_size(tmp_path):
    path = tmp_path / "module.py"
    path.write_text("one\ntwo\nthree\n", encoding="utf-8")
    first = await ReadFileTool().execute(
        json.dumps({"path": "module.py", "offset": 1, "limit": 1}),
        {"workspace": str(tmp_path)},
    )
    assert first["exit_code"] == 0
    assert first["file_hash"]
    assert first["total_lines"] == 3
    assert first["range_count"] == 1

    path.write_text("changed\ntwo\nthree\n", encoding="utf-8")
    stale = await ReadFileTool().execute(
        json.dumps({
            "path": "module.py",
            "offset": 1,
            "limit": 1,
            "expected_hash": first["file_hash"],
        }),
        {"workspace": str(tmp_path)},
    )
    assert stale["exit_code"] == 1
    assert stale["failure_kind"] == "STALE_VERSION"
    assert stale["file_hash"] != stale["expected_hash"]


def test_context_tracker_reservations_are_atomic_and_attributed():
    tracker = ContextTracker(
        "parallel",
        budget_tokens=1_000,
        protected_reserve_tokens=100,
        tool_quotas={"search": 300},
    )
    results = []

    def reserve_one():
        results.append(tracker.reserve("worker", input_tokens=100, output_tokens=200, tool_type="search"))

    threads = [threading.Thread(target=reserve_one) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(value is not None for value in results) == 1

    reservation = next(value for value in results if value is not None)
    tracker.reconcile(reservation, input_tokens=40, output_tokens=20)
    summary = tracker.get_usage_summary()
    assert summary["by_tool"]["search"]["input_tokens"] == 40
    assert summary["by_tool"]["search"]["output_tokens"] == 20
    assert summary["active_reservations"] == 0


def test_failure_contract_and_duplicate_suppression_are_bounded():
    result = annotate_tool_result(
        "ls",
        {"error": "ls: path is required", "exit_code": 1},
        attempt_id="attempt-1",
    )
    assert result["failure_kind"] == "INVALID_ARGUMENT"
    assert result["retryable"] is True
    assert len(result["diagnostic"]) <= 240
    suppressed = duplicate_tool_result(
        "ls",
        prior_fingerprint=result["fingerprint"],
        attempt_id="suppressed-1",
    )
    assert suppressed["failure_kind"] == "DUPLICATE_SUPPRESSED"
    assert suppressed["fingerprint"] == result["fingerprint"]
    assert len(tool_call_fingerprint("ls", "  .  ")) == 20
