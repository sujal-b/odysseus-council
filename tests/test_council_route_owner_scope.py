"""Route-level owner-scope tests for council session operations."""

import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from routes.council_routes import setup_council_routes


@pytest.fixture(autouse=True)
def _clear_store():
    from routes.council_routes import _store
    _store._cache.clear()
    for name in os.listdir(_store._dir):
        os.remove(os.path.join(_store._dir, name))


class _Request:
    """Minimal stand-in for FastAPI Request: exposes state and awaitable json()."""

    def __init__(self, user: str | None):
        self.state = SimpleNamespace(current_user=user)

    async def json(self):
        return {"choice": "approve", "notes": ""}


def _request(user: str | None):
    return _Request(user)


def _route(router, path: str, method: str):
    for route in router.routes:
        if getattr(route, "path", "") != path:
            continue
        if method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"{method} {path} route not registered")


def _seed_session(session_id: str, owner: str | None = "alice", **kw):
    from routes.council_routes import _store
    from council_of_agents.scripts.session_store import SessionState
    kw.setdefault("user_prompt", "test")
    state = SessionState(session_id=session_id, owner=owner or "", **kw)
    _store.save(state)
    return state


@pytest.fixture
def router():
    sm = MagicMock()
    return setup_council_routes(sm)


def test_respond_rejects_cross_owner(router):
    _seed_session("s1", owner="bob")
    target = _route(router, "/api/council/session/{session_id}/respond", "POST")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(target(session_id="s1", request=_request("alice")))
    assert exc.value.status_code == 403


def test_respond_allows_owner(router):
    _seed_session("s1", owner="alice")
    target = _route(router, "/api/council/session/{session_id}/respond", "POST")
    req = _request("alice")
    with patch.object(req.state, "current_user", "alice"):
        state = asyncio.run(target(session_id="s1", request=req))
    assert state == {"ok": True}


def test_feedback_rejects_cross_owner(router):
    _seed_session("s1", owner="bob")
    target = _route(router, "/api/council/session/{session_id}/feedback", "POST")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(target(session_id="s1", request=_request("alice")))
    assert exc.value.status_code == 403


def test_get_session_rejects_cross_owner(router, monkeypatch):
    _seed_session("s1", owner="bob")
    monkeypatch.setattr("core.database.SessionLocal", lambda: MagicMock())
    target = _route(router, "/api/council/session/{session_id}", "GET")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(target(session_id="s1", request=_request("alice")))
    assert exc.value.status_code == 403


def test_get_session_allows_owner(router, monkeypatch):
    _seed_session("s1", owner="alice")
    db_mock = MagicMock()
    db_mock.query.return_value.filter.return_value.first.return_value = ("s1",)
    monkeypatch.setattr("core.database.SessionLocal", lambda: db_mock)
    target = _route(router, "/api/council/session/{session_id}", "GET")
    req = _request("alice")
    result = asyncio.run(target(session_id="s1", request=req))
    assert result["session_id"] == "s1"


def test_patch_role_rejects_cross_owner(router):
    _seed_session("s1", owner="bob")
    target = _route(router, "/api/council/session/{session_id}/role/{role}", "PATCH")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(target(session_id="s1", role="chair", request=_request("alice")))
    assert exc.value.status_code == 403


def test_patch_role_allows_owner(router):
    _seed_session("s1", owner="alice")
    target = _route(router, "/api/council/session/{session_id}/role/{role}", "PATCH")
    req = _request("alice")
    with patch.object(req.state, "current_user", "alice"):
        state = asyncio.run(target(session_id="s1", role="chair", request=req))
    assert state == {"ok": True}


def test_respond_rejects_nonexistent_session(router):
    target = _route(router, "/api/council/session/{session_id}/respond", "POST")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(target(session_id="ghost", request=_request("alice")))
    assert exc.value.status_code == 404


def test_unauthenticated_access_allowed_when_no_owner():
    """No owner on session + no auth = allowed."""
    from routes.council_routes import _store
    _seed_session("s1", owner="")
    target = _route(
        setup_council_routes(MagicMock()),
        "/api/council/session/{session_id}/respond",
        "POST",
    )
    req = _request(None)
    with patch.object(req.state, "current_user", None):
        state = asyncio.run(target(session_id="s1", request=req))
    assert state == {"ok": True}


def test_respond_allows_owner_with_same_case_name(router):
    _seed_session("s1", owner="Alice")
    target = _route(router, "/api/council/session/{session_id}/respond", "POST")
    req = _request("alice")
    with patch.object(req.state, "current_user", "alice"):
        state = asyncio.run(target(session_id="s1", request=req))
    assert state == {"ok": True}
