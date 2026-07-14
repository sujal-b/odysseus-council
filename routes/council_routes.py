import asyncio, json, time, uuid, logging, os
from dataclasses import asdict
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import StreamingResponse

from src.auth_helpers import get_current_user
from council_of_agents.scripts.council_router import CouncilRouter
from council_of_agents.scripts.council_orchestrator import CouncilOrchestrator, CouncilEvent
from council_of_agents.scripts.session_store import InMemorySessionStore, SessionState
from src.tool_security import owner_is_admin_or_single_user
from src.event_bus import fire_event

import pathlib
logger = logging.getLogger(__name__)

_CONFIG_PATH = str(pathlib.Path(__file__).parent.parent / "council_of_agents" / "config" / "models.json")
_router_cfg   = CouncilRouter(_CONFIG_PATH)
_store        = InMemorySessionStore()
_queues:  dict[str, asyncio.Queue] = {}
_resumes: dict[str, asyncio.Event] = {}
_running_sessions: set[str] = set()
_running_tasks: dict[str, asyncio.Task] = {}

def cancel_active_council_session(session_id: str) -> None:
    task = _running_tasks.get(session_id)
    if task:
        logger.info(f"Cancelling active council task for session {session_id}")
        task.cancel()
        _running_tasks.pop(session_id, None)
    _running_sessions.discard(session_id)
    _resumes.pop(session_id, None)
    # Unblock any pending permission requests so the orchestrator can exit cleanly
    try:
        from council_of_agents.scripts.permissions import GLOBAL_REGISTRY
        for perm_id, evt in list(GLOBAL_REGISTRY.pending_events.items()):
            GLOBAL_REGISTRY.results[perm_id] = {"approved": False}
            evt.set()
    except Exception:
        pass
    try:
        _store.delete(session_id)
    except Exception:
        pass

async def cancel_all_active_council_sessions() -> None:
    for session_id, task in list(_running_tasks.items()):
        logger.info(f"Cancelling active council task for session {session_id} during bulk deletion")
        task.cancel()
    for q in _queues.values():
        try:
            await q.put(None)
        except Exception:
            pass
    _running_tasks.clear()
    _running_sessions.clear()
    _queues.clear()
    _resumes.clear()

def sanitize_council_event(item) -> dict:
    if item is None:
        return {}
    
    # Convert CouncilEvent or raw dict to a sanitized dict
    raw = asdict(item) if hasattr(item, "__dataclass_fields__") else dict(item)
    
    # Coerce fields to expected types
    event = str(raw.get("event") or "log")
    status = str(raw.get("status") or "IN_PROGRESS")
    text = str(raw.get("text") or "")
    agent = str(raw.get("agent")) if raw.get("agent") is not None else None
    complexity = str(raw.get("complexity")) if raw.get("complexity") is not None else None
    code = str(raw.get("code")) if raw.get("code") is not None else None
    file_path = str(raw.get("file_path")) if raw.get("file_path") is not None else None
    timestamp = str(raw.get("timestamp") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    
    extra = raw.get("extra")
    if not isinstance(extra, dict):
        extra = {}
    else:
        # Sanitize extra fields to ensure it is JSON serializable and doesn't contain weird types
        sanitized_extra = {}
        for k, v in extra.items():
            if k == "dag" and isinstance(v, dict):
                # Sanitize DAG nodes and edges
                nodes = v.get("nodes")
                edges = v.get("edges")
                sanitized_nodes = []
                sanitized_edges = []
                if isinstance(nodes, list):
                    for node in nodes:
                        if isinstance(node, dict) and "id" in node:
                            sanitized_nodes.append({
                                "id": str(node["id"]),
                                "description": str(node.get("description") or ""),
                                "depends_on": [str(d) for d in node.get("depends_on") or [] if d is not None],
                                "status": str(node.get("status") or "PENDING"),
                                "output": str(node.get("output") or ""),
                                "reason": str(node.get("reason") or "")
                            })
                if isinstance(edges, list):
                    for edge in edges:
                        if isinstance(edge, dict) and "from" in edge and "to" in edge:
                            sanitized_edges.append({
                                "from": str(edge["from"]),
                                "to": str(edge["to"])
                            })
                sanitized_extra["dag"] = {
                    "nodes": sanitized_nodes,
                    "edges": sanitized_edges
                }
            elif k == "exit_code" and v is not None:
                try:
                    sanitized_extra["exit_code"] = int(v)
                except (ValueError, TypeError):
                    sanitized_extra["exit_code"] = None
            else:
                sanitized_extra[str(k)] = v
        extra = sanitized_extra

    return {
        "event": event,
        "status": status,
        "text": text,
        "agent": agent,
        "complexity": complexity,
        "code": code,
        "file_path": file_path,
        "timestamp": timestamp,
        "extra": extra
    }

class SessionQueueProxy:
    # Events that reflect transient liveness only — never persisted to the
    # session log (otherwise a refresh would replay thousands of them).
    _EPHEMERAL_EVENTS = {"thought_delta", "tool_progress", "heartbeat"}

    def __init__(self, session_id: str, state):
        self.session_id = session_id
        self.state = state

    async def put(self, item):
        if item is not None:
            # Enforce backend sanitization contract (DTO Pattern)
            sanitized_dict = sanitize_council_event(item)
            sanitized_item = CouncilEvent(**sanitized_dict)
            
            self.state.status = sanitized_item.status
            if sanitized_item.event not in self._EPHEMERAL_EVENTS:
                self.state.log.append(asdict(sanitized_item))
            if sanitized_item.event == "task_status_update" and sanitized_item.extra.get("task_status") == "DONE":
                fire_event("council_task_done", self.state.owner)
            _store.save(self.state)
            item = sanitized_item
        q = _queues.get(self.session_id)
        if q is not None:
            await q.put(item)

def _make_orchestrator_wrapped(webhook_manager=None):
    async def _run_orchestrator_wrapped(session_id: str, state, proxy_queue, resume_event):
        try:
            orchestrator = CouncilOrchestrator(_router_cfg)
            await orchestrator.run(state, proxy_queue, resume_event)
            fire_event("council_completed", state.owner)
            if webhook_manager:
                webhook_manager.fire_and_forget("council.completed", {
                    "session_id": session_id,
                    "status": state.status or "COMPLETE",
                    "owner": state.owner,
                })
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Council orchestrator exception for session {session_id}: {e}", exc_info=True)
            state.status = "FAILED"
            state.report = (state.report or "") + f"\n\n[System Error] {str(e)}"
            
            # Append error event to state log so it is restored on page load/refresh
            err_evt = CouncilEvent(
                event="error",
                status="FAILED",
                text=f"Council execution encountered an error: {str(e)}",
                agent="system"
            )
            state.log.append(sanitize_council_event(err_evt))
            _store.save(state)

            fire_event("council_completed", state.owner)
            if webhook_manager:
                webhook_manager.fire_and_forget("council.completed", {
                    "session_id": session_id,
                    "status": "FAILED",
                    "owner": state.owner,
                })
            raise
        finally:
            _running_sessions.discard(session_id)
            _resumes.pop(session_id, None)
            _running_tasks.pop(session_id, None)
    return _run_orchestrator_wrapped

from src.constants import DATA_DIR
from council_of_agents.scripts.task_dag import TaskDAG

def _recover_orphaned_sessions():
    session_dir = os.path.join(DATA_DIR, "council_sessions")
    if not os.path.exists(session_dir):
        return
    for fname in os.listdir(session_dir):
        if not fname.endswith(".json"):
            continue
        path = os.path.join(session_dir, fname)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("status") in ("IN_PROGRESS", "BLOCKED"):
                data["status"] = "FAILED"
                data["report"] = data.get("report", "") + "\n\n[System] Interrupted by server restart."
                sid = data.get("session_id")
                if sid:
                    state = _store.load(sid)
                    if state:
                        state.status = "FAILED"
                        state.report = data["report"]
                        _store.save(state)
                    else:
                        with open(path, "w", encoding="utf-8") as wf:
                            json.dump(data, wf, indent=2, ensure_ascii=False)
                logger.warning("Recovered orphaned session: %s", data.get("session_id"))
        except Exception as e:
            logger.error("Failed to recover %s: %s", fname, e)

async def _stuck_session_watchdog(webhook_manager=None):
    while True:
        try:
            await asyncio.sleep(60)
            for session_id in list(_running_sessions):
                state = _store.load(session_id)
                if state and state.status == "IN_PROGRESS":
                    task = _running_tasks.get(session_id)
                    if task and task.done():
                        state.status = "FAILED"
                        state.report += "\n\n[System] Session watchdog detected stuck state."
                        _store.save(state)
                        _running_sessions.discard(session_id)
                        _running_tasks.pop(session_id, None)
                        fire_event("council_completed", state.owner)
                        if webhook_manager:
                            webhook_manager.fire_and_forget("council.completed", {
                                "session_id": session_id,
                                "status": "FAILED",
                                "owner": state.owner,
                            })
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Error in stuck session watchdog: %s", e)

def setup_council_routes(session_manager, webhook_manager=None) -> APIRouter:
    router = APIRouter(tags=["council"])
    _recover_orphaned_sessions()

    _run_orchestrator_wrapped = _make_orchestrator_wrapped(webhook_manager)

    @router.on_event("startup")
    async def on_startup():
        asyncio.create_task(_stuck_session_watchdog(webhook_manager))

    @router.post("/api/council/session")
    async def create_session(request: Request):
        owner = get_current_user(request)
        body  = await request.json()
        prompt = (body.get("prompt") or "").strip()
        if not prompt:
            raise HTTPException(400, "prompt is required")
        sid   = str(uuid.uuid4())
        owner_name = owner or "anonymous"
        # Create database session row so it appears in sidebar/list_sessions
        try:
            session_manager.create_session(
                session_id=sid,
                name=f"⚖ Council: {prompt[:40]}",
                endpoint_url="",
                model="Council",
                rag=False,
                owner=owner_name,
                mode="council",
            )
        except Exception as e:
            logger.error(f"Failed to create DB session row for council: {e}")

        try:
            budget = int(body.get("context_budget") or 0)
        except (ValueError, TypeError):
            budget = 0

        state = SessionState(
            session_id=sid, owner=owner_name,
            user_prompt=prompt,
            role_overrides=body.get("role_overrides", {}),
            created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            context_budget=budget,
        )
        # Verify workspace paths for admin users, gate for non-admins
        req_workspace = body.get("workspace", "").strip()
        from src.constants import DATA_DIR
        is_admin = owner_is_admin_or_single_user(owner_name)
        if req_workspace and is_admin:
            state.workspace = os.path.abspath(req_workspace)
        else:
            state.workspace = os.path.abspath(os.path.join(DATA_DIR, "council_workspace"))

        _store.save(state)
        fire_event("council_created", owner_name)
        if webhook_manager:
            webhook_manager.fire_and_forget("council.created", {
                "session_id": sid,
                "prompt": prompt,
                "owner": owner_name,
            })
        return {"session_id": sid}

    @router.get("/api/council/stream/{session_id}")
    async def stream_session(session_id: str, request: Request):
        owner = get_current_user(request)
        state = _store.load(session_id)
        if not state:
            raise HTTPException(404, "Session not found")
        if owner and state.owner != owner:
            raise HTTPException(403, "Forbidden")

        queue = asyncio.Queue()
        _queues[session_id] = queue

        is_running = session_id in _running_sessions
        # A run is auto-launched ONLY when the session has never started
        # (status PENDING with an empty log). Reconnecting to a session that has
        # already produced events — e.g. after a page refresh — must never spawn
        # a fresh orchestrator run: the orchestrator has no mid-pipeline resume
        # and would re-execute from the Chair. That re-execution is exactly the
        # "workflow restarts on refresh" bug.
        never_started = state.status == "PENDING" and not state.log
        if not is_running:
            if never_started:
                _running_sessions.add(session_id)
                if session_id not in _resumes:
                    _resumes[session_id] = asyncio.Event()

                proxy_queue = SessionQueueProxy(session_id, state)
                task = asyncio.create_task(_run_orchestrator_wrapped(session_id, state, proxy_queue, _resumes[session_id]))
                _running_tasks[session_id] = task
            elif state.status in ("IN_PROGRESS", "BLOCKED"):
                # The session claims to be active but no live task is driving it:
                # the previous run ended without reaching a terminal status, or
                # the worker was recycled. Surface a clear, terminal "stopped"
                # state instead of silently restarting the whole pipeline.
                state.status = "FAILED"
                note = "Run was interrupted and did not finish. Start a new run to try again."
                if note not in (state.report or ""):
                    state.report = (state.report or "") + f"\n\n[System] {note}"
                err_evt = CouncilEvent(event="error", status="FAILED", text=note, agent="system")
                state.log.append(sanitize_council_event(err_evt))
                _store.save(state)
                await queue.put(err_evt)
                await queue.put(None)

        async def sse_generator():
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        # Yield a keep-alive comment to prevent gateways/proxies from timing out
                        yield ": ping\n\n"
                        # ALSO yield a structured heartbeat event to update the frontend's last active timer
                        heartbeat = CouncilEvent(
                            event="heartbeat",
                            status=state.status or "IN_PROGRESS",
                            text="ping"
                        )
                        yield f"event: council_event\ndata: {json.dumps(asdict(heartbeat))}\n\n"
                        continue

                    if event is None:
                        yield "data: [DONE]\n\n"
                        break
                    yield f"event: council_event\ndata: {json.dumps(asdict(event))}\n\n"
            finally:
                if _queues.get(session_id) is queue:
                    _queues.pop(session_id, None)

        return StreamingResponse(sse_generator(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @router.post("/api/council/session/{session_id}/respond")
    async def respond(session_id: str, request: Request):
        state = _store.load(session_id)
        if not state:
            raise HTTPException(404, "Session not found")
        body  = await request.json()
        choice = body.get("choice", "approve")
        notes = body.get("notes", "")

        if choice == "cancel":
            state.status = "CANCELLED"
            _store.save(state)
            fire_event("council_cancelled", state.owner)
            if webhook_manager:
                webhook_manager.fire_and_forget("council.cancelled", {
                    "session_id": session_id,
                    "status": "CANCELLED",
                    "owner": state.owner,
                })
            cancel_active_council_session(session_id)
        elif choice == "retry_task":
            task_id = notes
            if state.dag:
                dag = TaskDAG.from_task_list(state.dag["nodes"])
                if task_id in dag._nodes:
                    node = dag._nodes[task_id]
                    node.status = "PENDING"
                    node.reason = ""
                    # Also unblock any blocked downstream tasks
                    for n in dag._nodes.values():
                        if n.status == "BLOCKED" and f"Dependency {task_id}" in n.reason:
                            n.status = "PENDING"
                            n.reason = ""
                    state.dag = dag.to_dict()
            state.status = "IN_PROGRESS"
            _store.save(state)
        elif choice == "skip_task":
            task_id = notes
            if state.dag:
                dag = TaskDAG.from_task_list(state.dag["nodes"])
                if task_id in dag._nodes:
                    dag.mark_done(task_id, output="[Skipped by user]")
                    for n in dag._nodes.values():
                        if n.status == "BLOCKED" and f"Dependency {task_id}" in n.reason:
                            n.status = "PENDING"
                            n.reason = ""
                    state.dag = dag.to_dict()
            state.status = "IN_PROGRESS"
            _store.save(state)
        elif choice in ("allow", "deny"):
            permission_id = body.get("permission_id")
            if not permission_id:
                raise HTTPException(400, "permission_id is required")
            
            from council_of_agents.scripts.permissions import GLOBAL_REGISTRY, PermissionManager
            from src.constants import DATA_DIR
            
            is_admin = owner_is_admin_or_single_user(state.owner)
            ws = state.workspace or os.path.abspath(os.path.join(DATA_DIR, "council_workspace"))
            
            pm = PermissionManager(ws, state.owner, is_admin)
            if choice == "allow":
                target = body.get("target")
                persist_level = body.get("persist_level", "once")
                if target:
                    pm.grant(target, persist_level)
                GLOBAL_REGISTRY.results[permission_id] = {"approved": True}
            else:
                GLOBAL_REGISTRY.results[permission_id] = {"approved": False}
            
            event = GLOBAL_REGISTRY.pending_events.get(permission_id)
            if event:
                event.set()
            
            state.status = "IN_PROGRESS"
            _store.save(state)
        elif choice == "decision":
            answer = body.get("answer", "")
            state.decision_response = answer
            _store.save(state)

        resume = _resumes.get(session_id)
        if resume:
            resume.set()
        return {"ok": True}

    @router.post("/api/council/session/{session_id}/feedback")
    async def feedback(session_id: str, request: Request):
        state = _store.load(session_id)
        if not state:
            raise HTTPException(404, "Session not found")
        body = await request.json()
        state.feedback = {"rating": body.get("rating", ""), "comment": body.get("comment", "")}
        _store.save(state)
        return {"ok": True}

    @router.get("/api/council/session/{session_id}")
    async def get_session(session_id: str, request: Request):
        from core.database import Session as DbSession, SessionLocal
        db = SessionLocal()
        try:
            exists = db.query(DbSession.id).filter(DbSession.id == session_id).first()
        finally:
            db.close()
        if not exists:
            _store.delete(session_id)
            raise HTTPException(404, "Session not found")

        state = _store.load(session_id)
        if not state:
            raise HTTPException(404, "Session not found")
        # Ensure log entries are sanitized before sending back to frontend
        if isinstance(state.log, list):
            state.log = [sanitize_council_event(item) for item in state.log if item is not None]
        return asdict(state)

    @router.get("/api/council/models")
    async def get_models(request: Request):
        return _router_cfg.get().model_dump()

    @router.patch("/api/council/session/{session_id}/role/{role}")
    async def patch_role(session_id: str, role: str, request: Request):
        state = _store.load(session_id)
        if not state:
            raise HTTPException(404, "Session not found")
        body = await request.json()
        state.role_overrides[role] = body
        _store.save(state)
        return {"ok": True}

    return router
