import json
import os
import tempfile
import pytest
from fastapi import HTTPException

from core.database import SessionLocal, Session as DbSession
from council_of_agents.scripts.session_store import InMemorySessionStore, SessionState
from council_of_agents.scripts.workflow_checkpoint import WorkflowCheckpoint
from routes.council_routes import (
    _rebuild_orphaned_council_sessions,
    _require_session,
    _COUNCIL_SESSION_PREFIX,
)


@pytest.fixture
def isolated_dirs(monkeypatch):
    tmp_base = os.path.abspath("data/.pytest-tmp") if os.path.exists("data/.pytest-tmp") else None
    with tempfile.TemporaryDirectory(dir=tmp_base) as tmp_dir:
        sessions_dir = os.path.join(tmp_dir, "council_sessions")
        workflows_dir = os.path.join(tmp_dir, "council_workflows")
        os.makedirs(sessions_dir, exist_ok=True)
        os.makedirs(workflows_dir, exist_ok=True)

        store = InMemorySessionStore()
        store._dir = sessions_dir
        store._cache.clear()
        monkeypatch.setattr("routes.council_routes._store", store)
        monkeypatch.setattr("council_of_agents.scripts.workflow_checkpoint.DATA_DIR", tmp_dir)
        yield {
            "root": tmp_dir,
            "sessions": sessions_dir,
            "workflows": workflows_dir,
            "store": store,
        }


def test_rebuild_orphaned_session_from_checkpoint(isolated_dirs, monkeypatch):
    sid = "test-rebuild-orphan-1"
    owner = "tester"
    title = f"{_COUNCIL_SESSION_PREFIX}Build a python app"

    db = SessionLocal()
    try:
        # Create DB catalogue entry
        db_sess = DbSession(
            id=sid,
            name=title,
            endpoint_url="",
            owner=owner,
            model="Council",
            mode="council",
        )
        db.merge(db_sess)
        db.commit()

        # Create checkpoint entry
        cp = WorkflowCheckpoint(sid, base_dir=isolated_dirs["workflows"])
        cp.record_stage("chair", '{"action": "write"}')
        cp.record_stage("strategist", '{"tasks": []}')
        cp.record_stage("perspective_analyzer", '{"audit": "ok"}')
        cp.record_stage("manager", '{"verdict": "APPROVED"}')

        # Ensure state file does not exist initially
        assert not isolated_dirs["store"].exists(sid)

        # Run rebuild
        monkeypatch.setattr("council_of_agents.scripts.workflow_checkpoint.DATA_DIR", isolated_dirs["root"])
        _rebuild_orphaned_council_sessions()

        # State must now exist
        assert isolated_dirs["store"].exists(sid)
        state = isolated_dirs["store"].load(sid)
        assert state is not None
        assert state.session_id == sid
        assert state.owner == owner
        assert state.user_prompt == "Build a python app"
        assert state.status == "FAILED"
        assert len(state.log) == 4
        assert [entry["agent"] for entry in state.log] == ["chair", "strategist", "perspective_analyzer", "manager"]
        assert "[System] Session state file was lost and has been rebuilt" in state.report

        # Idempotent: running again does not fail or duplicate
        _rebuild_orphaned_council_sessions()
        state2 = isolated_dirs["store"].load(sid)
        assert len(state2.log) == 4

    finally:
        try:
            db.rollback()
            db.query(DbSession).filter(DbSession.id == sid).delete()
            db.commit()
        except Exception:
            pass
        db.close()


def test_rebuild_logs_warning_when_checkpoint_missing(isolated_dirs, caplog):
    sid = "test-missing-both-1"
    db = SessionLocal()
    try:
        db_sess = DbSession(
            id=sid,
            name="Orphan with no checkpoint",
            endpoint_url="",
            owner="tester",
            model="Council",
            mode="council",
        )
        db.merge(db_sess)
        db.commit()

        with caplog.at_level("WARNING"):
            _rebuild_orphaned_council_sessions()

        assert any("neither state file nor workflow checkpoint" in record.message for record in caplog.records)
        assert not isolated_dirs["store"].exists(sid)
    finally:
        try:
            db.rollback()
            db.query(DbSession).filter(DbSession.id == sid).delete()
            db.commit()
        except Exception:
            pass
        db.close()


def test_require_session_logs_warning_on_missing_state(isolated_dirs, caplog):
    sid = "non-existent-session-id"
    with caplog.at_level("WARNING"):
        with pytest.raises(HTTPException) as exc_info:
            _require_session(sid, "tester")
        assert exc_info.value.status_code == 404

    assert any("could not be loaded from store" in record.message for record in caplog.records)


def test_store_delete_logs_removal(isolated_dirs, caplog):
    sid = "to-be-deleted-sid"
    state = SessionState(
        session_id=sid,
        owner="tester",
        user_prompt="Delete test",
    )
    isolated_dirs["store"].save(state)
    assert isolated_dirs["store"].exists(sid)

    with caplog.at_level("INFO"):
        isolated_dirs["store"].delete(sid)

    assert not isolated_dirs["store"].exists(sid)
    assert any("Deleted council session state file" in record.message for record in caplog.records)
