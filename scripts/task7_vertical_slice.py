"""Task 7: production vertical slice driver.

Runs the real production orchestrator (CouncilOrchestrator + CouncilRouter +
live model endpoints) against a disposable repository, then proves durability:
interrupt immediately after durable Manager approval and restart from the
same workflow id without repeating completed roles or tool writes.

Phases (run with ``python scripts/task7_vertical_slice.py --phase <name>``):
  gate       - rerun the P2.6 planning gate for the greenfield-failure case
  workflow-a - uninterrupted production run to COMPLETE
  workflow-b - interrupt immediately after durable Manager approval
  restart    - resume workflow-b from its checkpoint
  verify     - verify artifacts, tests, checkpoint, and write discipline
  all        - gate + workflow-a + workflow-b + restart + verify

Exit code 0 only when every phase passes.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

EVIDENCE_ROOT = ROOT / "data" / "council_agent_evals" / "phase-a" / "task7-slice-runs"
SLICES_DIR = ROOT / "data" / "task7-slices"
EVIDENCE_DIR: Path | None = None


PROMPT = (
    "Extend the small service with a health endpoint and a regression test. "
    "The service is plain WSGI with no framework: src/app.py exposes app() "
    "returning a dict, and tests import functions directly from src.app. "
    "Add a health() function to src/app.py that returns a JSON-serializable "
    "dict, and add a test in tests/test_app.py that imports health from src.app "
    "and asserts its return value. The task that modifies the files must "
    "declare a verification command that runs pytest. Plan only file-modification "
    "tasks; do not plan inspection-only or test-execution tasks; verification "
    "is handled by the declared command."
)

SEED_APP = '''"""Small service entrypoint."""
def app():
    return {"status": "running"}
'''

SEED_TEST = '''"""Regression tests for the small service."""
from src.app import app


def test_index_returns_running():
    assert app() == {"status": "running"}
'''


def _seed_workspace(ws: Path) -> None:
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "src").mkdir(exist_ok=True)
    (ws / "tests").mkdir(exist_ok=True)
    (ws / "src" / "__init__.py").write_text("", encoding="utf-8")
    (ws / "tests" / "__init__.py").write_text("", encoding="utf-8")
    # Root conftest makes `pytest tests` put the workspace root on sys.path,
    # so `from src.app import ...` resolves inside the disposable workspace.
    (ws / "conftest.py").write_text("", encoding="utf-8")
    (ws / "src" / "app.py").write_text(SEED_APP, encoding="utf-8", newline="\n")
    (ws / "tests" / "test_app.py").write_text(SEED_TEST, encoding="utf-8", newline="\n")


def _tree_hashes(ws: Path) -> dict:
    out = {}
    for path in sorted(ws.rglob("*")):
        if path.is_file():
            rel = str(path.relative_to(ws)).replace("\\", "/")
            out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def _fresh_session_id(prefix: str) -> str:
    return f"{prefix}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"


def _evidence_path(root: Path, run_id: str) -> Path:
    """Return one confined, user-visible evidence directory for a run."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", run_id):
        raise ValueError("run id must contain only letters, digits, '.', '_' or '-'")
    return root / run_id


def _prepare_evidence_dir(run_id: str, *, fresh: bool) -> Path:
    path = _evidence_path(EVIDENCE_ROOT, run_id)
    if fresh and path.exists():
        raise FileExistsError(f"evidence run already exists: {path}")
    path.mkdir(parents=True, exist_ok=not fresh)
    return path


def _evidence_dir() -> Path:
    if EVIDENCE_DIR is None:
        raise RuntimeError("evidence directory is not configured; use main()")
    return EVIDENCE_DIR


def _write_evidence_json(name: str, payload: dict) -> Path:
    """Write one immutable evidence record; never replace a prior run record."""
    path = _evidence_dir() / name
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=1, ensure_ascii=False)
    return path


def _gate_audit_metadata(trace: dict, outcome) -> dict:
    """Hash live requests without persisting their contents."""
    requests = []
    for record in trace.get("trace") or []:
        messages = record.get("final_messages") or record.get("messages")
        if not messages:
            continue
        encoded = json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        requests.append({
            "agent": record.get("agent"),
            "stage": record.get("stage"),
            "provider": record.get("endpoint"),
            "model": record.get("model"),
            "payload_sha256": hashlib.sha256(encoded).hexdigest(),
        })
    trace_bytes = json.dumps(trace, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "requests": requests,
        "trace_sha256": hashlib.sha256(trace_bytes).hexdigest(),
        "outcome": outcome,
    }


_ALLOWED_GATE_HOSTS = {"integrate.api.nvidia.com", "opencode.ai"}


def _live_gate_context(case: dict) -> dict:
    """Only repository hints, never a local workspace path, leave this process."""
    return {"workspace": "", "repository_context": case.get("repository_context", "")}


def _approved_gate_endpoints(endpoints: set[str]) -> set[str]:
    unexpected = sorted(
        endpoint for endpoint in endpoints
        if urlsplit(endpoint).hostname not in _ALLOWED_GATE_HOSTS
    )
    if unexpected:
        raise RuntimeError("GATE3 provider is not approved")
    return endpoints


def _configure_context_trace() -> Path:
    os.environ.setdefault("COUNCIL_WORKFLOW_CHECKPOINT", "on")
    os.environ.setdefault("COUNCIL_LEDGER_MODE", "shadow")
    trace_dir = _evidence_dir() / "context-traces"
    trace_dir.mkdir(exist_ok=True)
    # Metrics traces preserve call/guard evidence without copying prompts or
    # responses into new artifacts.
    os.environ["COUNCIL_CONTEXT_TRACE"] = "metrics"
    os.environ["COUNCIL_CONTEXT_TRACE_DIR"] = str(trace_dir)
    return trace_dir


def _capsule(ws: Path) -> str:
    lines = ["<repository_capsule>",
             "Authoritative paths to inspect, not assumptions to blindly trust:"]
    for rel, _hash in _tree_hashes(ws).items():
        lines.append(f"- {rel}")
    lines.append("Framework: Python stdlib + pytest. Do not introduce a new dependency.")
    lines.append("</repository_capsule>")
    return "\n".join(lines)


def _new_state(session_id: str, ws: Path):
    from council_of_agents.scripts.session_store import SessionState
    return SessionState(
        session_id=session_id, owner="admin", user_prompt=PROMPT,
        workspace=str(ws), status="PENDING", repository_context=_capsule(ws),
    )


def _make_orchestrator():
    from council_of_agents.scripts.council_router import CouncilRouter
    from council_of_agents.scripts.council_orchestrator import CouncilOrchestrator
    return CouncilOrchestrator(CouncilRouter(str(ROOT / "council_of_agents" / "config" / "models.json")))


PLANNING_ROLES = {"chair", "strategist", "perspective_analyzer", "manager"}


def _approval_checkpoint_snapshot(session_id: str) -> dict:
    """Copy durable approval evidence at the point the run is interrupted."""
    cp = _checkpoint(session_id)
    data = json.loads(json.dumps(cp._data))
    stages = data.get("stages") or {}
    manager = stages.get("manager") or {}
    dag = data.get("dag")
    return {
        "path": str(cp.path),
        "manager_approval": manager.get("approval"),
        "manager_reply_persisted": bool(manager.get("reply")),
        "strategist_plan_persisted": bool((stages.get("strategist") or {}).get("reply")),
        "dag_persisted": bool(isinstance(dag, dict) and dag.get("nodes")),
        "task_count": len(data.get("tasks") or {}),
        "checkpoint": data,
    }


def _drain_context_traces() -> None:
    from src.context_trace import shutdown
    shutdown()


def _trace_files(session_id: str) -> list[Path]:
    _drain_context_traces()
    return sorted((_evidence_dir() / "context-traces").glob(f"{session_id}-*.jsonl"))


def _trace_records(paths: list[str | Path]) -> list[dict]:
    records = []
    for value in paths:
        path = Path(value)
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                records.append(record)
    return records


def _planning_request_fingerprints(records: list[dict]) -> list[dict]:
    fingerprints = {
        (str(record.get("agent") or ""), str(record.get("payload_hash") or ""))
        for record in records
        if record.get("kind") == "model_request"
        and record.get("agent") in PLANNING_ROLES
        and record.get("payload_hash")
    }
    return [
        {"agent": agent, "payload_hash": payload_hash}
        for agent, payload_hash in sorted(fingerprints)
    ]


def _scope_violations(records: list[dict]) -> list[dict]:
    return [record for record in records if record.get("kind") == "scope_violation"]


def _outcome_for(session_id: str) -> dict | None:
    from council_of_agents.scripts.council_outcomes import OutcomeStore
    for outcome in reversed(OutcomeStore().get_recent(1000)):
        if outcome.get("session_id") == session_id:
            return outcome
    return None


async def drive_run(state, interrupt: str | None, *, timeout_s: int = 2400) -> dict:
    """Drive one production run without bypassing a Manager decision."""
    orchestrator = _make_orchestrator()
    queue = asyncio.Queue()
    resume_event = asyncio.Event()
    run_task = asyncio.create_task(orchestrator.run(state, queue, resume_event))
    events = []
    done = None
    manager_approved = False
    manager_review_required = False
    interrupted_after_manager_approval = False
    task_execution_started = False
    approval_checkpoint = None
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=timeout_s)
            except asyncio.TimeoutError:
                raise RuntimeError(f"run stalled: no event within {timeout_s}s (state={state.status})")
            if item is None:
                break
            events.append(item)
            approval = _approval_checkpoint_snapshot(state.session_id)
            if approval["manager_approval"] == "APPROVED":
                manager_approved = True
                if interrupt == "manager_approved":
                    approval_checkpoint = approval
                    interrupted_after_manager_approval = True
                    run_task.cancel()
                    break
            if item.event == "review_required":
                manager_review_required = True
                verdict = str((item.extra or {}).get("manager_verdict") or "").upper()
                if interrupt == "gate":
                    run_task.cancel()
                    break
                if verdict == "APPROVED":
                    # This is normal human approval, never an override.
                    resume_event.set()
                else:
                    # No production run may continue on a non-approved Manager
                    # review without an explicit human override; the slice never
                    # supplies one.
                    run_task.cancel()
                    break
            elif item.event == "decision_required":
                state.decision_response = "Proceed"
                resume_event.set()
            if item.event == "task_status_update" or (
                item.event == "code_update" and item.agent == "implementer"
            ):
                task_execution_started = True
            if item.event == "complete":
                done = item
        try:
            await run_task
        except asyncio.CancelledError:
            pass
    finally:
        while not queue.empty():
            item = queue.get_nowait()
            if item is not None:
                events.append(item)
        _drain_context_traces()
    return {
        "session_id": state.session_id,
        "status": getattr(state, "status", "UNKNOWN"),
        "events": events,
        "terminal": done,
        "manager_approved": manager_approved,
        "manager_review_required": manager_review_required,
        "interrupted_after_manager_approval": interrupted_after_manager_approval,
        "approval_checkpoint": approval_checkpoint,
        "task_execution_started": task_execution_started,
        "override_used": bool(getattr(state, "manager_override", False)),
        "trace_files": [str(path) for path in _trace_files(state.session_id)],
        "orchestrator": orchestrator,
    }


def _serialize_event(ev) -> dict:
    payload = {
        "event": ev.event, "status": ev.status, "agent": ev.agent,
        "text": ev.text, "timestamp": ev.timestamp,
    }
    for key in ("complexity", "route", "action", "target"):
        value = getattr(ev, key, None)
        if value is not None:
            payload[key] = value
    if ev.code is not None:
        payload["code"] = ev.code
    if ev.file_path is not None:
        payload["file_path"] = ev.file_path
    if ev.extra:
        payload["extra"] = ev.extra
    return payload


# ------------------------------------------------------------------ phases

def _live_p26_gate_trace() -> dict:
    """Run GATE3 through the real bounded evaluator and configured providers."""
    from council_of_agents.scripts.role_eval import (
        _bounded_evaluate_trace,
        _load_role_configs,
        _resolve_api_key_for_endpoints,
    )
    from scripts.task6_planning_gates import GATE3, P26

    role_configs = _load_role_configs(ROOT / "council_of_agents" / "config" / "models.json")
    endpoints = {
        str((role_configs.get(role) or {}).get("endpoint_url") or "")
        for role in PLANNING_ROLES
    }
    endpoints.discard("")
    _approved_gate_endpoints(endpoints)
    api_key = _resolve_api_key_for_endpoints(endpoints)

    async def go():
        return await _bounded_evaluate_trace(
            GATE3["user_prompt"],
            role_configs=role_configs,
            api_key=api_key,
            **_live_gate_context(GATE3),
            run_id=f"{_evidence_dir().name}-p26-gate",
            max_plan_revisions=1,
            planning_only=True,
            scenario_id=GATE3["name"],
            scenario_rubric=GATE3.get("planning_rubric"),
            prompt_label="P2.6-live",
            handoff_mode="contract",
            prompts_dir=ROOT / P26,
        )

    return asyncio.run(go())


def phase_gate() -> int:
    from council_of_agents.scripts.role_eval import _expected_scenario_outcome

    trace = _live_p26_gate_trace()
    passed, outcome = _expected_scenario_outcome({"expected_final_verdict": "APPROVED"}, trace)
    summary = trace.get("summary") or {}
    readiness = trace.get("readiness_gate") or {}
    strategist = next(
        (record for record in trace.get("trace") or [] if record.get("agent") == "strategist"),
        {},
    )
    zero_repair = not any(
        record.get("schema_repair_attempted")
        or record.get("semantic_repair_attempted")
        or record.get("garbage_recovery_attempted")
        or record.get("recovery_used")
        for record in trace.get("trace") or []
    )
    grounded_plan = bool(strategist.get("contract_passed")) and bool(
        (strategist.get("semantic_quality") or {}).get("passed")
    )
    ok = (
        passed
        and grounded_plan
        and summary.get("contract_validity") == 1.0
        and zero_repair
        and not summary.get("provider_failures")
        and trace.get("termination") == "COMPLETE"
        and str(summary.get("final_manager_verdict") or "") == "APPROVED"
        and bool(readiness.get("passed"))
    )
    _write_evidence_json("planning-unknown-target-session-bug.json", {
        "live": True,
        "audit": _gate_audit_metadata(trace, outcome),
        "checks": {
            "grounded_plan": grounded_plan,
            "manager_approved": str(summary.get("final_manager_verdict") or "") == "APPROVED",
            "readiness_passed": bool(readiness.get("passed")),
            "zero_repair": zero_repair,
            "scenario_outcome": outcome,
        },
        "trace": trace,
    })
    print(f"P2.6 live GATE3 (greenfield-failure case): passed={ok} "
          f"grounded={grounded_plan} contract={summary.get('contract_validity')} "
          f"manager={summary.get('final_manager_verdict')} "
          f"readiness={readiness.get('passed')} zero_repair={zero_repair} "
          f"termination={trace.get('termination')}")
    return 0 if ok else 1


def phase_workflow_a() -> int:
    session_id = _fresh_session_id("wfa")
    ws = SLICES_DIR / _evidence_dir().name / session_id / "ws"
    _seed_workspace(ws)
    state = _new_state(session_id, ws)
    result = asyncio.run(drive_run(state, interrupt=None))
    out = {
        "phase": "workflow-a", "session_id": session_id, "workspace": str(ws),
        "status": result["status"],
        "manager_approved": bool(result.get("manager_approved")),
        "override_used": bool(result.get("override_used")),
        "trace_files": result["trace_files"],
        "events": [_serialize_event(e) for e in result["events"]],
    }
    _write_evidence_json("workflow-a.json", out)
    _write_evidence_json("wfa-session.json", {
        "session_id": session_id,
        "workspace": str(ws),
        "manager_approved": bool(result.get("manager_approved")),
        "override_used": bool(result.get("override_used")),
        "trace_files": result["trace_files"],
    })
    terminal = result["terminal"]
    ok = (
        terminal is not None and terminal.status == "COMPLETE"
        and result["status"] == "COMPLETE"
        and result["manager_approved"]
        and not result["override_used"]
    )
    print(f"workflow-a: status={result['status']} complete={ok} "
          f"manager_approved={result['manager_approved']} override={result['override_used']}")
    return 0 if ok else 1


def phase_workflow_b() -> int:
    session_id = _fresh_session_id("wfb")
    ws = SLICES_DIR / _evidence_dir().name / session_id / "ws"
    _seed_workspace(ws)
    seed_hashes = _tree_hashes(ws)
    state = _new_state(session_id, ws)
    result = asyncio.run(drive_run(state, interrupt="manager_approved"))
    interrupt_hashes = _tree_hashes(ws)
    trace_records = _trace_records(result["trace_files"])
    planning_requests = _planning_request_fingerprints(trace_records)
    scope_violations = _scope_violations(trace_records)
    implementer_calls = [
        record for record in trace_records
        if record.get("kind") == "model_request" and record.get("agent") == "implementer"
    ]
    approval = result.get("approval_checkpoint") or {}
    checkpoint_ready = (
        approval.get("manager_approval") == "APPROVED"
        and approval.get("manager_reply_persisted")
        and approval.get("strategist_plan_persisted")
        and approval.get("dag_persisted")
    )
    pre_workspace_unchanged = seed_hashes == interrupt_hashes
    out = {
        "phase": "workflow-b-interrupted-after-manager-approval",
        "session_id": session_id,
        "workspace": str(ws),
        "status": result["status"],
        "manager_approved": bool(result.get("manager_approved")),
        "interrupted_after_manager_approval": bool(result.get("interrupted_after_manager_approval")),
        "override_used": bool(result.get("override_used")),
        "task_execution_started": bool(result.get("task_execution_started")),
        "approval_checkpoint": approval,
        "checkpoint_ready": checkpoint_ready,
        "pre_interrupt_hashes": seed_hashes,
        "interrupt_hashes": interrupt_hashes,
        "pre_interrupt_workspace_unchanged": pre_workspace_unchanged,
        "trace_files": result["trace_files"],
        "planning_request_fingerprints": planning_requests,
        "scope_violations": scope_violations,
        "implementer_calls_before_interrupt": len(implementer_calls),
        "events": [_serialize_event(e) for e in result["events"]],
    }
    _write_evidence_json("workflow-b-interrupted.json", out)
    _write_evidence_json("wfb-session.json", {
        "session_id": session_id,
        "workspace": str(ws),
        "manager_approved": bool(result.get("manager_approved")),
        "override_used": bool(result.get("override_used")),
        "approval_checkpoint": approval,
        "pre_interrupt_hashes": seed_hashes,
        "trace_files": result["trace_files"],
        "planning_request_fingerprints": planning_requests,
    })
    ok = (
        result["status"] == "CANCELLED"
        and result["manager_approved"]
        and result["interrupted_after_manager_approval"]
        and not result["override_used"]
        and not result["task_execution_started"]
        and checkpoint_ready
        and approval.get("task_count") == 0
        and pre_workspace_unchanged
        and not scope_violations
        and not implementer_calls
    )
    print(f"workflow-b: status={result['status']} checkpoint_ready={checkpoint_ready} "
          f"task_execution_started={result['task_execution_started']} "
          f"scope_violations={len(scope_violations)}")
    return 0 if ok else 1


def _done_task_files(session_id: str, ws: Path) -> set:
    """Files delivered by DONE tasks, from the pre-restart checkpoint's DAG
    nodes (accumulated_writes are absolute paths)."""
    cp = _checkpoint(session_id)
    files = set()
    for node in (cp._data.get("dag") or {}).get("nodes") or []:
        if node.get("status") != "DONE":
            continue
        for f in node.get("accumulated_writes") or []:
            abs_f = os.path.abspath(str(f))
            try:
                rel = os.path.relpath(abs_f, str(ws)).replace("\\", "/")
            except ValueError:
                continue
            files.add(rel)
    return files


def phase_restart() -> int:
    meta = json.loads((_evidence_dir() / "wfb-session.json").read_text(encoding="utf-8"))
    session_id, ws = meta["session_id"], Path(meta["workspace"])
    before = _tree_hashes(ws)
    # Read the pre-restart checkpoint BEFORE the run: the restart writes to
    # the same session checkpoint, so reading after would show post-restart
    # statuses and corrupt the durability check.
    done_files = _done_task_files(session_id, ws)
    pre_trace_files = {str(Path(value)) for value in meta.get("trace_files") or []}
    pre_fingerprints = {
        (item["agent"], item["payload_hash"])
        for item in meta.get("planning_request_fingerprints") or []
    }
    state = _new_state(session_id, ws)
    result = asyncio.run(drive_run(state, interrupt=None))
    after = _tree_hashes(ws)
    all_trace_files = {str(path) for path in _trace_files(session_id)}
    restart_trace_files = sorted(all_trace_files - pre_trace_files)
    restart_records = _trace_records(restart_trace_files)
    restart_fingerprints = {
        (item["agent"], item["payload_hash"])
        for item in _planning_request_fingerprints(restart_records)
    }
    duplicate_planning_requests = [
        {"agent": agent, "payload_hash": payload_hash}
        for agent, payload_hash in sorted(pre_fingerprints & restart_fingerprints)
    ]
    done_unchanged = all(
        rel in after and after[rel] == h
        for rel, h in before.items()
        if rel in done_files
    )
    out = {
        "phase": "workflow-b-restart",
        "session_id": session_id,
        "workspace": str(ws),
        "status": result["status"],
        "manager_approved": bool(result.get("manager_approved")),
        "override_used": bool(result.get("override_used")),
        "pre_restart_hashes": before,
        "post_restart_hashes": after,
        "pre_interrupt_workspace_unchanged": before == meta.get("pre_interrupt_hashes"),
        "done_task_files_unchanged": done_unchanged,
        "done_task_files": sorted(done_files),
        "pre_trace_files": sorted(pre_trace_files),
        "restart_trace_files": restart_trace_files,
        "duplicate_planning_requests": duplicate_planning_requests,
        "scope_violations": _scope_violations(restart_records),
        "events": [_serialize_event(e) for e in result["events"]],
    }
    _write_evidence_json("workflow-b-restart.json", out)
    terminal = result["terminal"]
    ok = (
        terminal is not None and terminal.status == "COMPLETE"
        and result["status"] == "COMPLETE"
        and result["manager_approved"]
        and not result["override_used"]
        and out["pre_interrupt_workspace_unchanged"]
        and done_unchanged
        and not duplicate_planning_requests
        and not out["scope_violations"]
    )
    print(f"workflow-b-restart: status={result['status']} complete={ok} "
          f"duplicate_planning_requests={len(duplicate_planning_requests)} "
          f"scope_violations={len(out['scope_violations'])}")
    return 0 if ok else 1


def _checkpoint(session_id: str):
    from council_of_agents.scripts.workflow_checkpoint import WorkflowCheckpoint
    return WorkflowCheckpoint(session_id, base_dir=ROOT / "data" / "council_workflows")


def phase_verify() -> int:
    failures = []
    a_meta = json.loads((_evidence_dir() / "wfa-session.json").read_text(encoding="utf-8"))
    b_meta = json.loads((_evidence_dir() / "wfb-session.json").read_text(encoding="utf-8"))
    evidence = {"task_attempts": {}, "outcomes": {}, "traces": {}}

    for label, meta in (("a", a_meta), ("b", b_meta)):
        ws = Path(meta["workspace"])
        cp = _checkpoint(meta["session_id"])
        stages = cp._data.get("stages") or {}
        dag = cp.dag_snapshot()
        app = (ws / "src" / "app.py").read_text(encoding="utf-8")
        tests = (ws / "tests" / "test_app.py").read_text(encoding="utf-8")
        if "def health" not in app:
            failures.append(f"{label}: src/app.py missing health()")
        if "def test_health" not in tests:
            failures.append(f"{label}: tests/test_app.py missing test_health()")
        if meta.get("override_used"):
            failures.append(f"{label}: Manager override was used")
        if not meta.get("manager_approved") or not cp.approved():
            failures.append(f"{label}: Manager actual APPROVED checkpoint missing")
        if not (stages.get("strategist") or {}).get("reply"):
            failures.append(f"{label}: approved Strategist plan not persisted")
        if not isinstance(dag, dict) or not dag.get("nodes"):
            failures.append(f"{label}: approved DAG state not persisted")
        for stage in PLANNING_ROLES:
            entry = stages.get(stage)
            if not entry or entry.get("status") != "DONE":
                failures.append(f"{label}: stage {stage} not recorded DONE")
            elif int(entry.get("model_calls") or 0) != 1:
                failures.append(f"{label}: stage {stage} model_calls={entry.get('model_calls')} (expected 1)")
        final_state = cp.final_state()
        if not final_state or final_state.get("status") != "COMPLETE":
            failures.append(f"{label}: final state not COMPLETE: {final_state}")
        elif not final_state.get("artifact_paths"):
            failures.append(f"{label}: no final artifact paths recorded")
        if not cp._data.get("tasks"):
            failures.append(f"{label}: no persisted task attempts")
        evidence["task_attempts"][label] = {}
        for task_id, tstate in (cp._data.get("tasks") or {}).items():
            attempts = int(tstate.get("attempts") or 0)
            evidence["task_attempts"][label][task_id] = attempts
            # One bounded recovery attempt plus an interrupted/resumed handoff
            # must not become an open retry loop.
            if not (1 <= attempts <= 3):
                failures.append(f"{label}: task {task_id} attempts={attempts} (expected 1-3)")
            if tstate.get("status") != "DONE":
                failures.append(f"{label}: task {task_id} status={tstate.get('status')}")
            if not tstate.get("tool_results"):
                failures.append(f"{label}: task {task_id} has no tool_results")
        outcome = _outcome_for(meta["session_id"])
        evidence["outcomes"][label] = {
            "found": bool(outcome),
            "blocked_writes": (outcome or {}).get("blocked_writes") or [],
        }
        if outcome is None:
            failures.append(f"{label}: no production outcome guard evidence")
        elif outcome.get("blocked_writes"):
            failures.append(f"{label}: production write guard recorded blocked writes")
        trace_records = _trace_records(_trace_files(meta["session_id"]))
        scope_violations = _scope_violations(trace_records)
        terminals = [record for record in trace_records if record.get("kind") == "run_terminal"]
        evidence["traces"][label] = {
            "records": len(trace_records),
            "scope_violations": len(scope_violations),
            "terminal_statuses": [record.get("status") for record in terminals],
        }
        if not trace_records:
            failures.append(f"{label}: no production context trace")
        if scope_violations:
            failures.append(f"{label}: context trace recorded unauthorized write attempt")
        if not any(record.get("status") == "COMPLETE" for record in terminals):
            failures.append(f"{label}: complete terminal trace missing")
        pytest_cmd = None
        probe = subprocess.run(
            ["pytest", "--version"], capture_output=True, text=True, timeout=30,
        )
        if probe.returncode == 0:
            pytest_cmd = ["pytest", "-q", "tests"]
        else:
            for candidate in (sys.executable, "python", "py -3"):
                probe = subprocess.run(
                    [candidate, "-c", "import pytest"], capture_output=True,
                    text=True, timeout=30,
                )
                if probe.returncode == 0:
                    pytest_cmd = [candidate, "-m", "pytest", "-q", "tests"]
                    break
        if pytest_cmd is None:
            failures.append(f"{label}: no pytest-capable interpreter found")
        else:
            proc = subprocess.run(
                pytest_cmd, cwd=str(ws), capture_output=True, text=True, timeout=300,
            )
            if proc.returncode != 0:
                failures.append(f"{label}: independent pytest failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}")
        for path in sorted(ws.rglob("*")):
            if not path.is_file():
                continue
            try:
                path.resolve().relative_to(ws.resolve())
            except ValueError:
                failures.append(f"{label}: file outside workspace: {path}")

    interrupted = json.loads((_evidence_dir() / "workflow-b-interrupted.json").read_text(encoding="utf-8"))
    restart = json.loads((_evidence_dir() / "workflow-b-restart.json").read_text(encoding="utf-8"))
    approval = interrupted.get("approval_checkpoint") or {}
    if interrupted.get("status") != "CANCELLED":
        failures.append("restart: workflow-b was not cancelled at checkpoint")
    if not interrupted.get("interrupted_after_manager_approval"):
        failures.append("restart: workflow-b did not interrupt after Manager approval")
    if interrupted.get("override_used"):
        failures.append("restart: workflow-b used Manager override")
    if interrupted.get("task_execution_started") or interrupted.get("implementer_calls_before_interrupt"):
        failures.append("restart: implementation started before approval checkpoint interruption")
    if interrupted.get("scope_violations"):
        failures.append("restart: pre-restart guard recorded unauthorized write attempt")
    if not interrupted.get("pre_interrupt_workspace_unchanged"):
        failures.append("restart: workspace changed before restart")
    if not (
        approval.get("manager_approval") == "APPROVED"
        and approval.get("strategist_plan_persisted")
        and approval.get("dag_persisted")
        and approval.get("task_count") == 0
    ):
        failures.append("restart: approved plan/DAG checkpoint was incomplete")
    if restart.get("duplicate_planning_requests"):
        failures.append("restart: completed planning role call repeated after restart")
    if restart.get("scope_violations"):
        failures.append("restart: resumed guard recorded unauthorized write attempt")
    if not restart.get("done_task_files_unchanged", False):
        failures.append("restart: delivered DONE-task files changed after restart")

    report = {
        "pass": not failures,
        "failures": failures,
        "workflow_a": a_meta,
        "workflow_b": b_meta,
        "interrupted_checkpoint": approval,
        "restart_duplicate_planning_requests": restart.get("duplicate_planning_requests") or [],
        "evidence": evidence,
    }
    _write_evidence_json("verification.json", report)
    for failure in failures:
        print("VERIFY FAIL:", failure)
    print(f"verify: {'PASS' if not failures else 'FAIL'} ({len(failures)} failures)")
    return 0 if not failures else 1


def main() -> int:
    global EVIDENCE_DIR
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", default="all",
                        choices=["gate", "workflow-a", "workflow-b", "restart", "verify", "all"])
    parser.add_argument("--run-id", help="reuse one evidence run for separately invoked phases")
    args = parser.parse_args()
    if args.phase in {"restart", "verify"} and not args.run_id:
        parser.error(f"--run-id is required for phase {args.phase}")
    run_id = args.run_id or _fresh_session_id("task7")
    try:
        existing = _evidence_path(EVIDENCE_ROOT, run_id)
        if args.phase in {"restart", "verify"} and not existing.is_dir():
            raise FileNotFoundError(f"evidence run does not exist: {existing}")
        EVIDENCE_DIR = _prepare_evidence_dir(
            run_id, fresh=args.phase == "all" or not args.run_id,
        )
        _configure_context_trace()
    except (FileExistsError, FileNotFoundError, ValueError) as exc:
        print(f"EVIDENCE SETUP FAILED: {exc}")
        return 2
    phases = {
        "gate": phase_gate,
        "workflow-a": phase_workflow_a,
        "workflow-b": phase_workflow_b,
        "restart": phase_restart,
        "verify": phase_verify,
    }
    order = ["gate", "workflow-a", "workflow-b", "restart", "verify"] if args.phase == "all" else [args.phase]
    print(f"TASK 7 EVIDENCE RUN: {EVIDENCE_DIR}")
    for name in order:
        print(f"--- phase {name} ---")
        if phases[name]() != 0:
            print(f"PHASE FAILED: {name}")
            return 1
    print("TASK 7 VERTICAL SLICE: ALL PHASES PASS")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
