from abc import ABC, abstractmethod
from typing import Optional
from dataclasses import dataclass, field

@dataclass
class SessionState:
    session_id:     str
    owner:          str
    user_prompt:    str
    role_overrides: dict = field(default_factory=dict)
    status:         str  = "PENDING"
    complexity:     Optional[str] = None
    active_agent:   Optional[str] = None
    log:            list = field(default_factory=list)
    report:         str  = ""
    loop_count:     int  = 0
    created_at:     str  = ""
    dag:            Optional[dict] = None
    retry_count:      int  = 0
    feedback:         Optional[dict] = None
    lessons:          list = field(default_factory=list)
    workspace:            str  = ""
    route:                str  = ""
    success_skills:       list = field(default_factory=list)
    self_reflections:     list = field(default_factory=list)
    # Context window tracking (WS2)
    context_budget:       int  = 0   # token budget for this session (0 = unlimited)
    compact_count:        int  = 0   # number of times tool outputs were compacted
    # Evidence-driven run ledger linkage.  The ledger itself lives in its
    # transactional store; keeping only references prevents session JSON bloat.
    ledger_id:             Optional[str] = None
    ledger_version:        int = 0
    active_checkpoint_id:  Optional[str] = None
    run_status:            str = ""

class SessionStore(ABC):
    @abstractmethod
    def save(self, state: SessionState) -> None: ...
    @abstractmethod
    def load(self, session_id: str) -> Optional[SessionState]: ...
    @abstractmethod
    def delete(self, session_id: str) -> None: ...

import json, os, tempfile
from src.constants import DATA_DIR

class InMemorySessionStore(SessionStore):
    def __init__(self):
        self._dir = os.path.join(DATA_DIR, "council_sessions")
        os.makedirs(self._dir, exist_ok=True)
        self._cache: dict[str, SessionState] = {}

    def save(self, state: SessionState) -> None:
        self._cache[state.session_id] = state
        path = os.path.join(self._dir, f"{state.session_id}.json")
        data = {
            "session_id": state.session_id,
            "owner": state.owner,
            "user_prompt": state.user_prompt,
            "role_overrides": state.role_overrides,
            "status": state.status,
            "complexity": state.complexity,
            "active_agent": state.active_agent,
            "log": state.log,
            "report": state.report,
            "loop_count": state.loop_count,
            "created_at": state.created_at,
            "dag": state.dag,
            "retry_count": state.retry_count,
            "feedback": state.feedback,
            "lessons": state.lessons,
            "workspace": state.workspace,
            "route": state.route,
            "success_skills": state.success_skills,
            "self_reflections": state.self_reflections,
            # Context window tracking (WS2)
            "context_budget": state.context_budget,
            "compact_count": state.compact_count,
            "ledger_id": state.ledger_id,
            "ledger_version": state.ledger_version,
            "active_checkpoint_id": state.active_checkpoint_id,
            "run_status": state.run_status,
        }
        fd, tmp_path = tempfile.mkstemp(dir=self._dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def load(self, session_id: str) -> Optional[SessionState]:
        if session_id in self._cache:
            return self._cache[session_id]
        path = os.path.join(self._dir, f"{session_id}.json")
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            state = SessionState(
                session_id=data["session_id"],
                owner=data["owner"],
                user_prompt=data["user_prompt"],
                role_overrides=data.get("role_overrides", {}),
                status=data.get("status", "PENDING"),
                complexity=data.get("complexity"),
                active_agent=data.get("active_agent"),
                log=data.get("log", []),
                report=data.get("report", ""),
                loop_count=data.get("loop_count", 0),
                created_at=data.get("created_at", ""),
                dag=data.get("dag"),
                retry_count=data.get("retry_count", 0),
                feedback=data.get("feedback"),
                lessons=data.get("lessons", []),
                workspace=data.get("workspace", ""),
                route=data.get("route", ""),
                success_skills=data.get("success_skills", []),
                self_reflections=data.get("self_reflections", []),
                # Context window tracking (WS2)
                context_budget=data.get("context_budget", 0),
                compact_count=data.get("compact_count", 0),
                ledger_id=data.get("ledger_id"),
                ledger_version=data.get("ledger_version", 0),
                active_checkpoint_id=data.get("active_checkpoint_id"),
                run_status=data.get("run_status", ""),
            )
            self._cache[session_id] = state
            return state
        except Exception:
            return None

    def delete(self, session_id: str) -> None:
        self._cache.pop(session_id, None)
        path = os.path.join(self._dir, f"{session_id}.json")
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass
