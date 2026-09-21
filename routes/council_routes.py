import asyncio, json, time, uuid, logging, os, re
from dataclasses import asdict
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import StreamingResponse

from src.auth_helpers import get_current_user
from council_of_agents.scripts.council_router import CouncilRouter
from council_of_agents.scripts.council_orchestrator import CouncilOrchestrator, CouncilEvent
from council_of_agents.scripts.session_store import InMemorySessionStore, SessionState
from src.tool_security import owner_is_admin_or_single_user
from src.event_bus import fire_event
from src.constants import DATA_DIR
from council_of_agents.scripts.permissions import GLOBAL_REGISTRY, PermissionManager

import pathlib
logger = logging.getLogger(__name__)

_CONFIG_PATH = str(pathlib.Path(__file__).parent.parent / "council_of_agents" / "config" / "models.json")
_router_cfg   = CouncilRouter(_CONFIG_PATH)
_store        = InMemorySessionStore()
_queues:  dict[str, asyncio.Queue] = {}
_resumes: dict[str, asyncio.Event] = {}
_running_sessions: set[str] = set()
_running_tasks: dict[str, asyncio.Task] = {}


def _manager_verdict_from_review(reply: str) -> str:
    """Recover a durable gate's verdict without defaulting malformed text to approval."""
    text = str(reply or "").strip()
    try:
        clean = re.sub(r"\x60\x60\x60(?:json)?", "", text, flags=re.IGNORECASE).strip()
        if clean.startswith("{"):
            value = str(json.loads(clean).get("verdict", "")).upper().strip()
            if value in {"APPROVED", "ACCEPT"}:
                return "APPROVED"
            if value in {"REVISE", "RETRY"}:
                return "REVISE"
            if value in {"BLOCKED", "ESCALATE"}:
                return "BLOCKED"
    except Exception:
        pass
    upper = text.replace("*", "").upper()
    if upper.startswith(("APPROVED", "ACCEPT")):
        return "APPROVED"
    if upper.startswith(("REVISE", "RETRY")):
        return "REVISE"
    return "BLOCKED"


def _validate_manager_review_choice(gate: dict | None, choice: str) -> None:
    """Reject ordinary approval when the final Manager decision was non-approved."""
    if (
        isinstance(gate, dict)
        and gate.get("kind") == "review"
        and bool(gate.get("requires_override", False))
        and choice != "override"
    ):
        raise HTTPException(
            409,
            "Manager did not approve this plan; use explicit Override after reviewing the remaining defects.",
        )


def _normalize_review_gate(gate: dict) -> dict:
    """Backfill review safety fields, failing closed for old or malformed gates."""
    if gate.get("kind") != "review":
        return gate

    raw_verdict = str(gate.get("manager_verdict") or "").upper().strip()
    if raw_verdict in {"APPROVED", "ACCEPT"}:
        verdict = "APPROVED"
    elif raw_verdict in {"REVISE", "RETRY"}:
        verdict = "REVISE"
    elif raw_verdict in {"BLOCKED", "ESCALATE"}:
        verdict = "BLOCKED"
    else:
        verdict = _manager_verdict_from_review(gate.get("manager_review", ""))

    return {
        **gate,
        "manager_verdict": verdict,
        "requires_override": bool(gate.get("requires_override", False) or verdict != "APPROVED"),
    }


def _pending_gate_from_state(state):
    """Return the durable gate, with a compatibility fallback for old sessions."""
    gate = getattr(state, "pending_gate", None)
    if isinstance(gate, dict) and gate.get("kind"):
        return _normalize_review_gate(gate)

    # Older session files predate pending_gate. Recover only the last unresolved
    # gate so refreshes remain useful without treating historical prompts as
    # current actions.
    log = getattr(state, "log", None) or []
    recovered = None
    for item in log:
        if not isinstance(item, dict):
            continue
        event = item.get("event")
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        if event == "permission_request":
            recovered = {
                "kind": "permission",
                "permission_id": extra.get("permission_id") or item.get("permission_id"),
                "action": extra.get("action") or item.get("action"),
                "target": extra.get("target") or item.get("target"),
            }
        elif event == "review_required":
            manager_review = extra.get("manager_review", "")
            recovered_verdict = extra.get("manager_verdict") or _manager_verdict_from_review(manager_review)
            recovered = {
                "kind": "review",
                "plan": extra.get("plan", ""),
                "manager_review": manager_review,
                "manager_verdict": recovered_verdict,
                "requires_override": bool(
                    extra.get("requires_override", False)
                    or recovered_verdict != "APPROVED"
                ),
            }
        elif event == "decision_required":
            recovered = {
                "kind": "decision",
                "question": extra.get("question") or item.get("text", ""),
                "options": extra.get("options") or ["Proceed"],
                "criterion_id": extra.get("criterion_id", ""),
            }
        elif recovered and item.get("status") and item.get("status") != "BLOCKED":
            # A later non-blocked persisted event means the last recovered
            # gate was resolved; a later gate can replace it on the next loop.
            recovered = None
    return recovered


def _gate_event(state):
    """Build one replayable SSE event for a currently blocked session."""
    gate = _pending_gate_from_state(state)
    if not gate:
        return None
    kind = gate.get("kind")
    if kind == "permission":
        return CouncilEvent(
            event="permission_request",
            status="BLOCKED",
            text=f"Permission required for {gate.get('action')} on {gate.get('target')}",
            agent="implementer",
            extra={
                "permission_id": gate.get("permission_id"),
                "action": gate.get("action"),
                "target": gate.get("target"),
                "replay": True,
            },
        )
    if kind == "review":
        requires_override = bool(gate.get("requires_override", False))
        return CouncilEvent(
            event="review_required",
            status="BLOCKED",
            text=(
                "Manager is requesting approval before Implementer starts."
                if not requires_override
                else "Manager did not approve the plan; human Override is required."
            ),
            agent="manager",
            extra={
                "plan": gate.get("plan", ""),
                "manager_review": gate.get("manager_review", ""),
                "manager_verdict": gate.get("manager_verdict", "APPROVED"),
                "requires_override": requires_override,
                "replay": True,
            },
        )
    if kind == "decision":
        return CouncilEvent(
            event="decision_required",
            status="BLOCKED",
            text=gate.get("question", "A decision is required to proceed."),
            agent="completeness_auditor",
            extra={
                "question": gate.get("question", ""),
                "options": gate.get("options") or ["Proceed"],
                "criterion_id": gate.get("criterion_id", ""),
                "replay": True,
            },
        )
    return None

def cancel_active_council_session(session_id: str) -> None:
    task = _running_tasks.get(session_id)
    if task:
        logger.info(f"Cancelling active council task for session {session_id}")
        task.cancel()
        _running_tasks.pop(session_id, None)
    _running_sessions.discard(session_id)
    _resumes.pop(session_id, None)
    q = _queues.pop(session_id, None)
    if q is not None:
        try:
            q.put_nowait(None)
        except Exception:
            pass
    # Unblock any pending permission requests so the orchestrator can exit cleanly
    try:
        for perm_id, evt in list(GLOBAL_REGISTRY.pending_events.items()):
            if GLOBAL_REGISTRY.session_ids.get(perm_id) != session_id:
                continue
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

def _compact_task_label(description: str, task_id: str = "") -> str:
    """Return a deterministic 1–3 word label for the visible event DTO."""
    text = re.sub(r"[`\"'{}\[\]()]", " ", str(description or ""))
    text = re.sub(r"(?:[A-Za-z]:[\\/]|\b(?:projects|workspace|data|assets|src)[\\/])[^\s,;]+", " ", text, flags=re.I)
    lower = text.lower()
    rules = (
        (r"flight|airline|airport|route", "Flights"),
        (r"globe|three\.js|3d|earth|map", "Globe"),
        (r"theme|style|css|visual|color|font", "Theme"),
        (r"server|api|backend|endpoint|route", "Server"),
        (r"doc|readme|guide", "Docs"),
        (r"test|check|verify|lint|compile", "Checks"),
        (r"app|index|orchestrat|wire|integrat", "App"),
        (r"interface|ui|component|panel|layout", "Interface"),
        (r"scaffold|skeleton|project root|director|structure", "Scaffold"),
    )
    for pattern, label in rules:
        if re.search(pattern, lower):
            return label
    words = re.findall(r"[A-Za-z0-9]+", text)
    if words:
        return " ".join(word.capitalize() for word in words[:2])
    return "Task" if task_id else "Work"


def _compact_failure_reason(text: str, fallback: str = "execution issue") -> str:
    lower = str(text or "").lower()
    if any(token in lower for token in ("schema invalid", "validation error", "validation errors", "output format invalid", "write_scope")):
        return "invalid plan schema"
    if "timeout" in lower or "timed out" in lower:
        return "timeout"
    if any(token in lower for token in ("permission", "denied", "forbidden")):
        return "permission required"
    if any(token in lower for token in ("verification", "compiler", "syntax", "test")):
        return "verification failed"
    if any(token in lower for token in ("context", "token", "overflow")):
        return "context limit reached"
    return fallback


def _compact_event_presentation(event: str, status: str, agent: str | None, text: str, extra: dict) -> dict:
    """Build the backend-owned, display-safe event summary.

    The raw fields remain available to the execution engine and gate replay,
    while the browser receives one bounded presentation DTO for visible cards.
    """
    tool = str(extra.get("tool") or "").lower()
    task_id = str(extra.get("task_id") or "")
    if event in {"tool_start", "tool_output", "tool_progress"}:
        if tool in {"glob", "grep", "ls"}:
            summary = "Inspect workspace"
        elif tool == "read_file":
            summary = "Read source"
        elif tool in {"write_file", "edit_file"}:
            summary = "Update files"
        elif tool in {"bash", "python"}:
            args = extra.get("args") if isinstance(extra.get("args"), dict) else {}
            raw = str(extra.get("command") or args.get("command") or args.get("code") or "").lower()
            summary = "Run checks" if re.search(r"test|pytest|check|lint|compile|verify", raw) else "Run command"
        else:
            summary = "Run operation"
        return {"kind": "BUILD", "summary": summary, **({"task_id": task_id} if task_id else {})}
    if event == "task_status_update":
        label = _compact_task_label(extra.get("description") or text, task_id)
        task_status = str(extra.get("task_status") or status or "").upper()
        state_label = {"IN_PROGRESS": "Running", "DONE": "Approved", "FAILED": "Rejected"}.get(task_status, "Queued")
        return {"kind": "BUILD", "summary": f"{state_label} · {label}", **({"task_id": task_id, "task_label": label} if task_id else {})}
    if event in {"dag_update", "plan_created"}:
        nodes = extra.get("dag", {}).get("nodes", []) if isinstance(extra.get("dag"), dict) else []
        count = len(nodes) if isinstance(nodes, list) else 0
        return {"kind": "PLAN", "summary": f"{count} task{'s' if count != 1 else ''} planned" if count else "Constructing execution plan"}
    if event == "error":
        return {"kind": "BLOCKED", "summary": _compact_failure_reason(text), "code": "ERROR"}
    if event == "recovery_blocked":
        return {"kind": "BLOCKED", "summary": "Manual review required", "code": "RECOVERY_BLOCKED"}
    if event == "complete":
        return {"kind": "DONE" if status == "COMPLETE" else "BLOCKED", "summary": "Verified result available" if status == "COMPLETE" else "Execution stopped"}
    role = str(agent or "council").capitalize()
    return {"kind": role.upper(), "summary": f"{role} is working"}


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

    # Always attach the compact DTO, including malformed or absent extras.
    extra["presentation"] = _compact_event_presentation(event, status, agent, text, extra)

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

def _require_session(session_id: str, owner: str | None):
    """Load session and verify ownership. Returns state or raises 404/403."""
    state = _store.load(session_id)
    if not state:
        raise HTTPException(404, "Session not found")
    # Ownership is compared case-insensitively: the same account may be
    # written with different casing by the auth layer and by the session
    # creator, and casing drift must not lock the owner out of the session.
    if owner and state.owner.lower() != owner.lower():
        raise HTTPException(403, "Forbidden")
    return state


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
            extra = sanitized_item.extra if isinstance(sanitized_item.extra, dict) else {}
            if sanitized_item.event == "permission_request" and sanitized_item.status == "BLOCKED":
                self.state.pending_gate = {
                    "kind": "permission",
                    "permission_id": extra.get("permission_id"),
                    "action": extra.get("action"),
                    "target": extra.get("target"),
                }
            elif sanitized_item.event == "review_required" and sanitized_item.status == "BLOCKED":
                self.state.pending_gate = _normalize_review_gate({
                    "kind": "review",
                    "plan": extra.get("plan", ""),
                    "manager_review": extra.get("manager_review", ""),
                    "manager_verdict": extra.get("manager_verdict"),
                    "requires_override": bool(extra.get("requires_override", False)),
                })
            elif sanitized_item.event == "decision_required" and sanitized_item.status == "BLOCKED":
                self.state.pending_gate = {
                    "kind": "decision",
                    "question": extra.get("question") or sanitized_item.text,
                    "options": extra.get("options") or ["Proceed"],
                    "criterion_id": extra.get("criterion_id", ""),
                }
            elif (
                sanitized_item.status != "BLOCKED"
                and sanitized_item.event not in self._EPHEMERAL_EVENTS
            ):
                self.state.pending_gate = None
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
            # Council sessions are durable workflows. Keep checkpointing local
            # to this production route rather than relying on process env.
            orchestrator._workflow_checkpoint_enabled = True
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
            try:
                await proxy_queue.put(None)
            except Exception:
                pass
    return _run_orchestrator_wrapped

from council_of_agents.scripts.task_dag import TaskDAG


def _has_resumable_checkpoint(session_id: str) -> bool:
    """Resume only a Manager-approved workflow with no recorded final state."""
    try:
        from council_of_agents.scripts.workflow_checkpoint import WorkflowCheckpoint

        checkpoint = WorkflowCheckpoint(session_id)
        return checkpoint.path.exists() and checkpoint.approved() and checkpoint.final_state() is None
    except Exception:
        return False


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
                sid = data.get("session_id")
                if sid and _has_resumable_checkpoint(sid):
                    logger.warning("Resumable workflow checkpoint found: %s", sid)
                    continue
                data["status"] = "FAILED"
                data["report"] = data.get("report", "") + "\n\n[System] Interrupted by server restart."
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

def setup_council_routes(session_manager, webhook_manager=None) -> APIRouter:
    router = APIRouter(tags=["council"])
    _recover_orphaned_sessions()

    _run_orchestrator_wrapped = _make_orchestrator_wrapped(webhook_manager)

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
        # Verify workspace paths for admin users, gate for non-admins. The
        # canonical path is persisted so every later tool/permission check
        # uses the same authority boundary.
        req_workspace = body.get("workspace", "").strip()
        from council_of_agents.scripts.permissions import resolve_council_workspace
        is_admin = owner_is_admin_or_single_user(owner_name)
        if req_workspace and is_admin:
            try:
                state.workspace = resolve_council_workspace(req_workspace)
            except (OSError, ValueError) as workspace_error:
                raise HTTPException(400, f"Invalid workspace: {workspace_error}")
        else:
            state.workspace = resolve_council_workspace(os.path.join(DATA_DIR, "council_workspace"))

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
        state = _require_session(session_id, owner)

        queue = asyncio.Queue()
        _queues[session_id] = queue

        live_task = _running_tasks.get(session_id)
        is_running = live_task is not None and not live_task.done()
        # Reconnects never create a fresh run after emitted events. Only an
        # approved, nonterminal checkpoint may resume below.
        # All other stale sessions remain fail-closed.
        never_started = state.status == "PENDING" and not state.log
        # Replay exactly one durable gate only while a live worker still owns
        # the run. Stale approved checkpoints launch below; other stale gates
        # become an explicit interrupted failure instead of a dead prompt.
        if is_running and state.status == "BLOCKED":
            replay_gate = _gate_event(state)
            if replay_gate is not None:
                await queue.put(replay_gate)
        if not is_running:
            if live_task is not None:
                _running_tasks.pop(session_id, None)
            _running_sessions.discard(session_id)
            _resumes.pop(session_id, None)

            resumable = (
                state.status in ("IN_PROGRESS", "BLOCKED")
                and _has_resumable_checkpoint(session_id)
            )
            if never_started or resumable:
                if resumable:
                    # Checkpoint approval supersedes any stale in-memory gate.
                    state.status = "IN_PROGRESS"
                    state.pending_gate = None
                    _store.save(state)
                _running_sessions.add(session_id)
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
        owner = get_current_user(request)
        state = _require_session(session_id, owner)
        body  = await request.json()
        choice = body.get("choice", "approve")
        notes = body.get("notes", "")

        if choice == "cancel":
            state.status = "CANCELLED"
            state.pending_gate = None
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
            state.pending_gate = None
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
            state.pending_gate = None
            _store.save(state)
        elif choice in ("approve", "override"):
            # Manager review is a durable gate too.  Clear it before waking
            # the orchestrator so a refresh cannot replay an already accepted
            # plan while the next event is still being produced.
            gate = _pending_gate_from_state(state)
            if gate and gate.get("kind") == "review":
                _validate_manager_review_choice(gate, choice)
                state.manager_override = choice == "override"
                state.pending_gate = None
            state.status = "IN_PROGRESS"
            _store.save(state)
        elif choice in ("allow", "deny"):
            permission_id = body.get("permission_id")
            if not permission_id:
                raise HTTPException(400, "permission_id is required")
            is_admin = owner_is_admin_or_single_user(state.owner)
            ws = state.workspace or os.path.abspath(os.path.join(DATA_DIR, "council_workspace"))

            gate = _pending_gate_from_state(state)
            if (
                not gate
                or gate.get("kind") != "permission"
                or gate.get("permission_id") != permission_id
            ):
                raise HTTPException(409, "Permission request is stale or no longer pending")

            event = GLOBAL_REGISTRY.pending_events.get(permission_id)
            if event is None:
                raise HTTPException(409, "Permission worker is no longer active; retry the run")
            
            pm = PermissionManager(ws, state.owner, is_admin)
            if choice == "allow":
                # Never trust a client-submitted replacement target. The
                # server-side pending gate is the authority for what was
                # actually requested.
                target = gate.get("target")
                persist_level = body.get("persist_level", "once")
                if persist_level not in {"once", "project", "global"}:
                    raise HTTPException(400, "Invalid permission persistence level")
                if target:
                    pm.grant(target, persist_level)
                GLOBAL_REGISTRY.results[permission_id] = {"approved": True}
            else:
                GLOBAL_REGISTRY.results[permission_id] = {"approved": False}

            state.status = "IN_PROGRESS"
            state.pending_gate = None
            _store.save(state)
            event.set()
        elif choice == "decision":
            answer = body.get("answer", "")
            state.decision_response = answer
            state.pending_gate = None
            state.status = "IN_PROGRESS"
            _store.save(state)

        resume = _resumes.get(session_id)
        if resume:
            resume.set()
        return {"ok": True}

    @router.post("/api/council/session/{session_id}/feedback")
    async def feedback(session_id: str, request: Request):
        owner = get_current_user(request)
        state = _require_session(session_id, owner)
        body = await request.json()
        state.feedback = {"rating": body.get("rating", ""), "comment": body.get("comment", "")}
        _store.save(state)
        return {"ok": True}

    @router.get("/api/council/session/{session_id}")
    async def get_session(session_id: str, request: Request):
        owner = get_current_user(request)
        from core.database import Session as DbSession, SessionLocal
        db = SessionLocal()
        try:
            exists = db.query(DbSession.id).filter(DbSession.id == session_id).first()
        finally:
            db.close()
        if not exists:
            _store.delete(session_id)
            raise HTTPException(404, "Session not found")

        state = _require_session(session_id, owner)
        # Ensure log entries are sanitized before sending back to frontend
        if isinstance(state.log, list):
            state.log = [sanitize_council_event(item) for item in state.log if item is not None]
        # Migrate legacy review gates on read so refresh/reconnect exposes the
        # same fail-closed approval state as the response endpoint.
        normalized_gate = _pending_gate_from_state(state)
        if normalized_gate and normalized_gate != state.pending_gate:
            state.pending_gate = normalized_gate
            _store.save(state)
        return asdict(state)

    @router.get("/api/council/models")
    async def get_models(request: Request):
        return _router_cfg.get().model_dump()

    @router.patch("/api/council/session/{session_id}/role/{role}")
    async def patch_role(session_id: str, role: str, request: Request):
        owner = get_current_user(request)
        state = _require_session(session_id, owner)
        body = await request.json()
        state.role_overrides[role] = body
        _store.save(state)
        return {"ok": True}

    return router
