"""Proof tests for the in-workflow completeness loop (self-improvisation).

The money shot: a partial artifact (2/3 acceptance criteria) is driven to
complete (3/3) by re-dispatching the implementer at the detected gap — and the
loop only acts when gaps exist (ablation). Also covers the needs_user gate and
the pure helpers (_collect_criteria, _ground_audit).
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from council_of_agents.scripts.council_orchestrator import CouncilOrchestrator
from council_of_agents.scripts.task_dag import TaskDAG


def _orch():
    return CouncilOrchestrator(MagicMock())


class _State:
    def __init__(self):
        self.user_prompt = "build a thing"
        self.session_id = "s1"
        self.status = "IN_PROGRESS"
        self.decision_response = ""


def _events_collector():
    events = []
    async def emit(**kw):
        events.append(kw)
    return events, emit


def _audit(crits):
    total = len(crits)
    met = sum(1 for c in crits if c["met"])
    return {"completeness": met / total if total else 0.0,
            "done": total > 0 and met == total, "criteria": crits}


# ── Pure helpers ────────────────────────────────────────────────────────────

def test_collect_criteria_pulls_acceptance_with_description_fallback():
    dag = TaskDAG.from_task_list([
        {"id": "T1", "description": "a", "acceptance": "file a.py exists"},
        {"id": "T2", "description": "b", "acceptance": ""},          # falls back to description
        {"id": "T3", "description": "c", "depends_on": ["T1"], "acceptance": "tests pass"},
    ])
    by = {c["id"]: c for c in CouncilOrchestrator._collect_criteria(dag)}
    assert set(by) == {"T1", "T2", "T3"}                 # none silently dropped
    assert by["T1"]["acceptance"] == "file a.py exists"  # explicit kept
    assert by["T2"]["acceptance"] == "b"                 # description fallback
    assert all(c["acceptance"] for c in by.values())


def test_collect_criteria_skips_fully_empty_task():
    dag = TaskDAG.from_task_list([{"id": "T1", "description": "", "acceptance": ""}])
    assert CouncilOrchestrator._collect_criteria(dag) == []


def test_collect_criteria_none_dag():
    assert CouncilOrchestrator._collect_criteria(None) == []


def test_ground_audit_recomputes_and_demotes_stub():
    o = _orch()
    audit = {"completeness": 1.0, "done": True, "criteria": [
        {"id": "T1", "met": True, "gap_type": "fillable", "detail": "done"},
        {"id": "T2", "met": True, "gap_type": "fillable", "detail": "left a TODO stub"},  # lie
    ]}
    grounded = o._ground_audit(audit, written_paths=set())
    assert grounded["criteria"][1]["met"] is False          # demoted
    assert grounded["criteria"][1]["gap_type"] == "broken"
    assert grounded["completeness"] == 0.5                    # recomputed, not trusted
    assert grounded["done"] is False


# ── The loop (proof) ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_loop_drives_partial_to_complete():
    o = _orch()
    # Pass 1 = 2/3 with one fillable gap; pass 2 = 3/3 done.
    partial = _audit([
        {"id": "T1", "met": True, "gap_type": "fillable", "detail": "ok"},
        {"id": "T2", "met": True, "gap_type": "fillable", "detail": "ok"},
        {"id": "T3", "met": False, "gap_type": "fillable", "detail": "endpoint not wired"},
    ])
    complete = _audit([{"id": x, "met": True, "gap_type": "fillable", "detail": "ok"}
                       for x in ("T1", "T2", "T3")])
    o._run_completeness_audit = AsyncMock(side_effect=[partial, complete])
    o._invoke_agent_safe = AsyncMock(return_value="wired the endpoint")

    events, emit = _events_collector()
    impl, metrics, cancelled = await o._completeness_loop(
        _State(), dag=None, impl_reply="initial", written_paths=set(),
        used_tools=set(), route="PIPELINE", workspace="", emit=emit,
        owner="u", resume_event=asyncio.Event())

    assert cancelled is False
    assert "gap-fill" in impl                          # implementer re-dispatched
    assert o._invoke_agent_safe.await_count == 1       # exactly one gap-fill
    assert metrics["before"] == pytest.approx(2 / 3)
    assert metrics["after"] == 1.0                      # driven to complete
    assert metrics["gaps_closed"] == 1
    assert metrics["loops"] == 2
    assert any(e["event"] == "completeness_update" for e in events)


@pytest.mark.asyncio
async def test_loop_noop_when_already_complete_ablation():
    """If the first audit is already done, the loop must NOT re-dispatch —
    proves the driver acts on gaps, not blindly."""
    o = _orch()
    o._run_completeness_audit = AsyncMock(return_value=_audit(
        [{"id": "T1", "met": True, "gap_type": "fillable", "detail": "ok"}]))
    o._invoke_agent_safe = AsyncMock(return_value="should not be called")

    events, emit = _events_collector()
    impl, metrics, cancelled = await o._completeness_loop(
        _State(), None, "done already", set(), set(), "PIPELINE", "", emit, "u", asyncio.Event())

    assert o._invoke_agent_safe.await_count == 0
    assert metrics["gaps_closed"] == 0
    assert metrics["after"] == 1.0
    assert "gap-fill" not in impl


@pytest.mark.asyncio
async def test_attempted_gap_is_not_counted_closed_without_reverification(monkeypatch):
    monkeypatch.setenv("COUNCIL_COMPLETENESS_MAX_LOOPS", "1")
    o = _orch()
    partial = _audit([
        {"id": "T1", "met": False, "gap_type": "fillable", "detail": "missing"},
    ])
    o._run_completeness_audit = AsyncMock(return_value=partial)
    o._invoke_agent_safe = AsyncMock(return_value="attempted a fix")
    _, emit = _events_collector()

    _, metrics, cancelled = await o._completeness_loop(
        _State(), None, "initial", set(), set(), "PIPELINE", "", emit, "u",
        asyncio.Event(),
    )

    assert cancelled is False
    assert metrics["gaps_attempted"] == 1
    assert metrics["gaps_closed"] == 0
    assert metrics["verified_complete"] is False


@pytest.mark.asyncio
async def test_non_dag_run_gets_user_request_acceptance_fallback():
    o = _orch()
    o._run_completeness_audit = AsyncMock(return_value=_audit([
        {"id": "USER_REQUEST", "met": True, "gap_type": "fillable", "detail": "done"},
    ]))
    _, emit = _events_collector()

    await o._completeness_loop(
        _State(), None, "result", set(), set(), "PIPELINE", "", emit, "u",
        asyncio.Event(),
    )

    criteria = o._run_completeness_audit.await_args.args[1]
    assert criteria == [{
        "id": "USER_REQUEST",
        "description": "build a thing",
        "acceptance": "The delivered work satisfies the user's request.",
    }]


@pytest.mark.asyncio
async def test_loop_escalates_needs_user_decision():
    o = _orch()
    needs = _audit([
        {"id": "T1", "met": True, "gap_type": "fillable", "detail": "ok"},
        {"id": "T2", "met": False, "gap_type": "needs_user", "detail": "DB unspecified",
         "question": "Which database?"},
    ])
    done = _audit([{"id": x, "met": True, "gap_type": "fillable", "detail": "ok"}
                   for x in ("T1", "T2")])
    o._run_completeness_audit = AsyncMock(side_effect=[needs, done])
    o._invoke_agent_safe = AsyncMock(return_value="used Postgres")

    state = _State()
    resume = asyncio.Event()
    events, emit = _events_collector()

    async def driver():                       # simulate the user answering
        await asyncio.sleep(0.01)
        state.decision_response = "Postgres"
        resume.set()

    (impl, metrics, cancelled), _ = await asyncio.gather(
        o._completeness_loop(state, None, "initial", set(), set(), "PIPELINE", "",
                             emit, "u", resume),
        driver())

    assert any(e["event"] == "decision_required" for e in events)   # user gated
    assert cancelled is False
    assert "gap-fill" in impl
    assert metrics["after"] == 1.0


@pytest.mark.asyncio
async def test_loop_cancel_returns_cancelled():
    o = _orch()
    o._run_completeness_audit = AsyncMock(return_value=_audit(
        [{"id": "T1", "met": False, "gap_type": "needs_user", "detail": "?",
          "question": "Q?"}]))
    o._invoke_agent_safe = AsyncMock(return_value="x")
    state = _State()
    resume = asyncio.Event()
    _, emit = _events_collector()

    async def driver():
        await asyncio.sleep(0.01)
        state.status = "CANCELLED"
        resume.set()

    (impl, metrics, cancelled), _ = await asyncio.gather(
        o._completeness_loop(state, None, "initial", set(), set(), "PIPELINE", "",
                             emit, "u", resume),
        driver())
    assert cancelled is True
