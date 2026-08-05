"""Resumed-run closure tests: deterministic restore of terminal tasks.

A resumed run skips the DAG execution loop entirely (``all_complete`` is
already true), so a task that FAILED before the interrupt or was BLOCKED by
failure propagation never re-executes and never re-verifies. Regression for
the vertical-slice restart failure (3/3 runs): T1 read-only FAILED (a
gate-loop artifact — the report was fine), T2 DONE, T3 BLOCKED (phantom
propagation from T1) while all artifacts sat complete on disk; the run then
FAILED after the completeness loop's gap-fill could not run pytest. The fix
closes verifiably-correct terminal tasks at restore time and reconciles
auditor-met read-only tasks so the run can complete.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from council_of_agents.scripts.council_orchestrator import CouncilOrchestrator
from council_of_agents.scripts.task_dag import TaskDAG, TaskNode


def _orchestrator():
    router = MagicMock()
    router.get.return_value = MagicMock(escalation=MagicMock(max_loops=3, conflict_threshold=0.7))
    orch = CouncilOrchestrator(router)
    orch._checkpoint = MagicMock()
    return orch


def _write_passing_slice(ws):
    (ws / "src").mkdir(parents=True)
    (ws / "tests").mkdir(parents=True)
    # Rootdir conftest presence makes pytest prepend the rootdir to sys.path,
    # exactly like the vertical-slice workspace, so `from src.app import` works.
    (ws / "conftest.py").write_text("", encoding="utf-8")
    (ws / "src" / "app.py").write_text(
        'def health():\n    return {"status": "healthy"}\n', encoding="utf-8"
    )
    (ws / "tests" / "test_app.py").write_text(
        "from src.app import health\n\n"
        "def test_health_returns_healthy():\n    assert health() == {'status': 'healthy'}\n",
        encoding="utf-8",
    )


def _restored_dag():
    dag = TaskDAG()
    dag.add_task(TaskNode(
        id="T1", description="Read src/app.py to see current content.",
        acceptance="File content known.",
        verification={"type": "shell", "command": "cat src/app.py"},
        read_scope=["src/app.py"], write_scope=[], status="FAILED",
        reason="Manager rejected task T1 (gate-loop artifact)",
    ))
    dag.add_task(TaskNode(
        id="T2", description="Read tests/test_app.py to see current content.",
        acceptance="File content known.",
        verification={"type": "shell", "command": "cat tests/test_app.py"},
        read_scope=["tests/test_app.py"], write_scope=[], status="DONE",
    ))
    dag.add_task(TaskNode(
        id="T3", description="Implement health() and its test.",
        acceptance="src/app.py defines health(), tests cover it, pytest passes.",
        verification={"type": "shell", "command": "pytest -q"},
        write_scope=["src/", "tests/"], depends_on=["T1"], status="BLOCKED",
        reason="Dependency T1 failed permanently",
    ))
    return dag


def test_restore_closure_closes_verifiable_terminal_tasks(tmp_path):
    """T3 (BLOCKED by phantom propagation) has a runnable verification spec
    that passes on disk, so restore closure marks it DONE. T1's `cat` spec
    cannot run on Windows, so its FAILED status is preserved."""
    _write_passing_slice(tmp_path)
    dag = _restored_dag()
    orch = _orchestrator()
    events = []

    async def emit(**kwargs):
        events.append(kwargs)

    asyncio.run(orch._close_restored_terminal_tasks(dag, str(tmp_path), emit))

    assert dag._nodes["T3"].status == "DONE"
    assert "T3" in orch._restore_verification_passed
    assert dag._nodes["T1"].status == "FAILED"
    assert dag._nodes["T2"].status == "DONE"
    assert any(ev.get("event") == "verification_result" for ev in events)


def test_restore_closure_keeps_failing_task_terminal(tmp_path):
    """A terminal task whose verification genuinely fails on disk stays
    terminal: the run fails honestly instead of papering over the gap."""
    _write_passing_slice(tmp_path)
    (tmp_path / "tests" / "test_app.py").write_text(
        "def test_fails():\n    assert False\n", encoding="utf-8"
    )
    dag = _restored_dag()
    orch = _orchestrator()

    async def emit(**kwargs):
        pass

    asyncio.run(orch._close_restored_terminal_tasks(dag, str(tmp_path), emit))

    assert dag._nodes["T3"].status == "BLOCKED"
    assert "T3" not in orch._restore_verification_passed


def test_restore_closure_skips_unverifiable_spec(tmp_path):
    """A terminal task with a verification shape that cannot run (unbalanced
    quotes drop to None via normalize_verification) is left untouched, not
    crashed."""
    dag = TaskDAG()
    dag.add_task(TaskNode(
        id="T1", description="read", acceptance="known",
        verification={"type": "shell", "command": "pytest -q \"unbalanced"},
        write_scope=[], status="FAILED",
    ))
    orch = _orchestrator()

    async def emit(**kwargs):
        pass

    asyncio.run(orch._close_restored_terminal_tasks(dag, str(tmp_path), emit))

    assert dag._nodes["T1"].status == "FAILED"
    assert not orch._restore_verification_passed


def test_ground_audit_pins_deterministically_closed_criteria(tmp_path):
    """Criteria whose task was deterministically verified at restore cannot
    be graded back to unmet by the auditor."""
    orch = _orchestrator()
    audit = {
        "completeness": 0.0,
        "done": False,
        "criteria": [
            {"id": "T3", "met": False, "gap_type": "fillable", "detail": "no pytest output seen"},
            {"id": "T1", "met": True, "gap_type": "fillable", "detail": "content read and reported"},
        ],
    }
    grounded = orch._ground_audit(audit, [], forced_met_ids={"T3"})
    by_id = {c["id"]: c for c in grounded["criteria"]}
    assert by_id["T3"]["met"] is True
    assert by_id["T3"]["gap_type"] == "verified"
    assert by_id["T1"]["met"] is True
    assert grounded["completeness"] == 1.0
    assert grounded["done"] is True


def test_ground_audit_without_forced_ids_is_unchanged(tmp_path):
    orch = _orchestrator()
    audit = {
        "criteria": [{"id": "T3", "met": False, "gap_type": "fillable", "detail": "no pytest"}],
    }
    grounded = orch._ground_audit(audit, [])
    assert grounded["criteria"][0]["met"] is False


def test_completeness_loop_reconciles_restored_readonly_task(tmp_path):
    """A restored read-only FAILED task whose criterion the audit judged met
    has no deliverable left to produce: the loop promotes it to DONE so the
    run completes. A mutation-required BLOCKED task without deterministic
    closure stays terminal."""
    _write_passing_slice(tmp_path)
    dag = _restored_dag()
    orch = _orchestrator()
    orch._run_completeness_audit = AsyncMock(return_value={
        "completeness": 1.0,
        "done": True,
        "criteria": [
            {"id": "T1", "met": True, "gap_type": "fillable", "detail": "content read and reported"},
            {"id": "T2", "met": True, "gap_type": "fillable", "detail": "content read and reported"},
            {"id": "T3", "met": True, "gap_type": "fillable", "detail": "health() implemented"},
        ],
    })

    class State:
        user_prompt = "restart slice"

    async def emit(**kwargs):
        pass

    impl_reply, metrics, cancelled = asyncio.run(orch._completeness_loop(
        State(), dag, "implementer reply", set(), set(), "PIPELINE",
        str(tmp_path), emit, None, asyncio.Event(),
    ))

    assert dag._nodes["T1"].status == "DONE"   # read-only, auditor met
    assert dag._nodes["T3"].status == "BLOCKED"  # mutation, no closure
    assert cancelled is False
    assert metrics["verified_complete"] is True


def test_completeness_loop_does_not_reconcile_unmet_criterion(tmp_path):
    """A read-only FAILED task whose criterion the audit did NOT judge met is
    not promoted: the loop keeps the honest failure."""
    dag = _restored_dag()
    orch = _orchestrator()
    orch._run_completeness_audit = AsyncMock(return_value={
        "completeness": 2 / 3,
        "done": False,
        "criteria": [
            {"id": "T1", "met": False, "gap_type": "broken", "detail": "report missing"},
            {"id": "T2", "met": True, "gap_type": "fillable", "detail": "read"},
            {"id": "T3", "met": True, "gap_type": "fillable", "detail": "implemented"},
        ],
    })

    class State:
        user_prompt = "restart slice"

    async def emit(**kwargs):
        pass

    asyncio.run(orch._completeness_loop(
        State(), dag, "reply", set(), set(), "PIPELINE",
        str(tmp_path), emit, None, asyncio.Event(),
    ))

    assert dag._nodes["T1"].status == "FAILED"


def test_restored_closure_integration_with_full_dag_state(tmp_path):
    """End-to-end shape: after restore closure + auditor grounding + loop
    reconciliation, no terminal task remains for the failed_tasks gate —
    the exact condition the resume run failed on."""
    _write_passing_slice(tmp_path)
    dag = _restored_dag()
    orch = _orchestrator()

    async def emit(**kwargs):
        pass

    asyncio.run(orch._close_restored_terminal_tasks(dag, str(tmp_path), emit))
    assert "T3" in orch._restore_verification_passed

    grounded = orch._ground_audit({
        "criteria": [
            {"id": "T1", "met": True, "gap_type": "fillable", "detail": "read"},
            {"id": "T2", "met": True, "gap_type": "fillable", "detail": "read"},
            {"id": "T3", "met": False, "gap_type": "fillable", "detail": "no pytest"},
        ],
    }, [], forced_met_ids=orch._restore_verification_passed)
    assert grounded["done"] is True

    orch._run_completeness_audit = AsyncMock(return_value=grounded)
    asyncio.run(orch._completeness_loop(
        type("State", (), {"user_prompt": "restart slice"})(), dag, "reply",
        set(), set(), "PIPELINE", str(tmp_path), emit, None, asyncio.Event(),
    ))

    failed = [n.id for n in dag._nodes.values() if n.status in ("FAILED", "BLOCKED")]
    assert failed == []


def test_cancel_after_durable_approval_stops_before_implementer(tmp_path):
    """A cancellation queued by the sealed-DAG checkpoint must run before a
    task wave can dispatch an Implementer model request."""
    orch = _orchestrator()
    orch._workflow_checkpoint_enabled = False
    state = MagicMock(
        session_id="cancel-after-dag",
        user_prompt="Inspect the service.",
        owner="test-user",
        workspace=str(tmp_path),
        role_overrides={},
        metadata={},
        status="IN_PROGRESS",
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "service.py").write_text("def service(): pass\n", encoding="utf-8")
    checkpoint = MagicMock()
    checkpoint.stage_done.return_value = False
    checkpoint.dag_snapshot.return_value = {}
    checkpoint.approved.return_value = True
    orch._checkpoint = checkpoint
    roles = []
    snapshots = []

    def record_dag(snapshot):
        snapshots.append(snapshot)
        run_task = asyncio.current_task()
        assert run_task is not None
        run_task.get_loop().call_soon(run_task.cancel)

    checkpoint.record_dag.side_effect = record_dag
    responses = {
        "chair": ('{"complexity":"MEDIUM","route":"PIPELINE","action":"read",'
                  '"target":"service","reason":"inspection"}'),
        "strategist": ('{"tasks":[{"id":"T1","description":"Inspect src/service.py",'
                       '"acceptance":"Findings documented","read_scope":["src/"],'
                       '"write_scope":[]}]}'),
        "perspective_analyzer": ('{"security":{"score":0.9,"issues":[]},'
                                 '"performance":{"score":0.9,"issues":[]},'
                                 '"maintainability":{"score":0.9,"issues":[]},'
                                 '"overall_score":0.9,"synthesis":"clear"}'),
        "manager": '{"verdict":"APPROVED","confidence":0.9,"summary":"ok"}',
    }

    async def invoke(role, *_args, **_kwargs):
        roles.append(role)
        return responses[role]

    async def run_and_catch_cancel():
        run_task = asyncio.create_task(orch.run(state, asyncio.Queue(), asyncio.Event()))
        try:
            await run_task
        except asyncio.CancelledError:
            pass

    with patch.object(orch, "_invoke_agent_safe", side_effect=invoke), \
         patch.object(orch, "_load_prompt", return_value="test prompt"), \
         patch("src.tool_security.blocked_tools_for_owner", return_value=set()), \
         patch("council_of_agents.scripts.permissions.resolve_council_workspace", return_value=str(tmp_path)), \
         patch("council_of_agents.scripts.session_store.InMemorySessionStore") as mock_store, \
         patch("services.memory.skills.SkillsManager") as mock_skills, \
         patch("council_of_agents.scripts.council_orchestrator.OutcomeStore") as mock_outcomes, \
         patch("council_of_agents.scripts.council_orchestrator.SessionLocal") as mock_session:
        mock_store.return_value.load.return_value = None
        mock_skills.return_value.get_relevant_skills.return_value = []
        mock_outcomes.return_value.get_skill_context.return_value = ""
        mock_outcomes.return_value.get_success_patterns.return_value = ""
        mock_session.return_value.query.return_value.filter.return_value.first.return_value = MagicMock(owner="test-user")
        asyncio.run(run_and_catch_cancel())

    assert checkpoint.record_approval.call_args.args[0] == "APPROVED"
    assert snapshots[0]["nodes"][0]["contract_hash"]
    assert "implementer" not in roles
    assert state.status == "CANCELLED"
