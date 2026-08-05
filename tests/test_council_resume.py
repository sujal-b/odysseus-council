"""Route restart regression: approved checkpoint resumes exactly once."""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import routes.council_routes as council_routes
from council_of_agents.scripts.session_store import SessionState
from council_of_agents.scripts.workflow_checkpoint import WorkflowCheckpoint


class _Request:
    def __init__(self, user: str):
        self.state = SimpleNamespace(current_user=user)


def _stream_endpoint(router):
    for route in router.routes:
        if getattr(route, "path", "") == "/api/council/stream/{session_id}":
            return route.endpoint
    raise AssertionError("stream route missing")


@pytest.mark.asyncio
async def test_stale_approved_checkpoint_resumes_once_without_replacing_live_run(
    tmp_path, monkeypatch,
):
    session_id = "resume-route"
    session_dir = tmp_path / "council_sessions"
    session_dir.mkdir()
    original_cache = dict(council_routes._store._cache)
    for collection in (
        council_routes._queues,
        council_routes._resumes,
        council_routes._running_sessions,
        council_routes._running_tasks,
    ):
        collection.clear()
    council_routes._store._cache.clear()
    monkeypatch.setattr(council_routes._store, "_dir", str(session_dir))
    monkeypatch.setattr(council_routes, "DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(council_routes, "fire_event", lambda *args, **kwargs: None)

    checkpoint = WorkflowCheckpoint(session_id)
    checkpoint.record_approval("APPROVED", '{"verdict":"APPROVED"}')
    state = SessionState(
        session_id=session_id,
        owner="owner",
        user_prompt="resume this workflow",
        status="IN_PROGRESS",
        report="keep prior report",
        log=[{"event": "manager_approved"}],
    )
    council_routes._store.save(state)

    started = asyncio.Event()
    release = asyncio.Event()
    calls = []
    task = None

    class FakeOrchestrator:
        def __init__(self, _router):
            self._workflow_checkpoint_enabled = False

        async def run(self, resumed_state, _queue, _resume_event):
            calls.append((resumed_state.session_id, self._workflow_checkpoint_enabled))
            started.set()
            await release.wait()

    monkeypatch.setattr(council_routes, "CouncilOrchestrator", FakeOrchestrator)
    try:
        router = council_routes.setup_council_routes(MagicMock())
        stream = _stream_endpoint(router)

        await stream(session_id, _Request("owner"))
        await asyncio.wait_for(started.wait(), timeout=1)
        task = council_routes._running_tasks[session_id]
        assert calls == [(session_id, True)]
        assert state.status == "IN_PROGRESS"
        assert state.report == "keep prior report"

        await stream(session_id, _Request("owner"))
        await asyncio.sleep(0)
        assert calls == [(session_id, True)]
    finally:
        release.set()
        if task is not None:
            await task
        for collection in (
            council_routes._queues,
            council_routes._resumes,
            council_routes._running_sessions,
            council_routes._running_tasks,
        ):
            collection.clear()
        council_routes._store._cache.clear()
        council_routes._store._cache.update(original_cache)
