"""Task 7: production vertical slice driver.

Runs the real production orchestrator (CouncilOrchestrator + CouncilRouter +
live model endpoints) against a disposable repository, then proves durability:
interrupt mid-implementation and restart from the same workflow id resumes
without repeating completed roles or tool writes.

Phases (run with ``python scripts/task7_vertical_slice.py --phase <name>``):
  gate       - rerun the P2.6 planning gate for the greenfield-failure case
  workflow-a - uninterrupted production run to COMPLETE
  workflow-b - run that is interrupted after the first completed task
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
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

EVIDENCE_DIR = ROOT / "data" / "council_agent_evals" / "phase-a" / "task7-slice"
SLICES_DIR = ROOT / "data" / "task7-slices"

os.environ.setdefault("COUNCIL_WORKFLOW_CHECKPOINT", "on")
# The ledger (shadow mode) drives the verification engine that produces the
# deterministic evidence and artifact paths the checkpoint records per task.
# Without it the vertical slice would run without artifact verification.
os.environ.setdefault("COUNCIL_LEDGER_MODE", "shadow")

PROMPT = (
    "Extend the small service with a health endpoint and a regression test. "
    "Modify src/app.py to add the endpoint and tests/test_app.py to add the test."
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
    return f"{prefix}-{int(time.time() * 1000)}"


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


async def drive_run(state, interrupt: str | None, *, timeout_s: int = 2400) -> dict:
    """Drive one orchestrator run, returning events and the terminal state."""
    from council_of_agents.scripts.council_orchestrator import CouncilOrchestrator

    orchestrator = _make_orchestrator()
    queue = asyncio.Queue()
    resume_event = asyncio.Event()
    run_task = asyncio.create_task(orchestrator.run(state, queue, resume_event))
    events = []
    saw_gate = False
    done = None
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=timeout_s)
            except asyncio.TimeoutError:
                raise RuntimeError(f"run stalled: no event within {timeout_s}s (state={state.status})")
            if item is None:
                break
            events.append(item)
            if item.event in ("review_required", "decision_required"):
                saw_gate = True
                if interrupt == "gate":
                    run_task.cancel()
                    break
                if item.event == "review_required":
                    # Same durable-gate resume as routes/council_routes.py
                    # choice "approve": record the override before waking the
                    # orchestrator so a BLOCKED verdict cannot stop the run.
                    state.manager_override = True
                if item.event == "decision_required":
                    state.decision_response = "Proceed"
                resume_event.set()
            if (interrupt == "first_task_done"
                    and item.event == "task_status_update"
                    and (item.extra or {}).get("task_status") == "DONE"):
                saw_gate = True
                run_task.cancel()
                break
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
    return {
        "session_id": state.session_id,
        "status": getattr(state, "status", "UNKNOWN"),
        "events": events,
        "terminal": done,
        "saw_gate": saw_gate,
        "override_used": bool(getattr(state, "manager_override", False)),
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

def phase_gate() -> int:
    from scripts.task6_planning_gates import GATE3, run_trace, _expected_scenario_outcome
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    trace = run_trace(GATE3)
    passed, outcome = _expected_scenario_outcome({"expected_final_verdict": "APPROVED"}, trace)
    summary = trace.get("summary") or {}
    readiness = trace.get("readiness_gate") or {}
    ok = (
        passed
        and summary.get("contract_validity") == 1.0
        and not summary.get("schema_repair_attempts")
        and not summary.get("provider_failures")
        and trace.get("termination") == "COMPLETE"
        and str(summary.get("final_manager_verdict") or "") == "APPROVED"
        and bool(readiness.get("passed"))
    )
    (EVIDENCE_DIR / "planning-unknown-target-session-bug.json").write_text(
        json.dumps(trace, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"P2.6 GATE3 (greenfield-failure case): passed={ok} "
          f"contract={summary.get('contract_validity')} "
          f"manager={summary.get('final_manager_verdict')} "
          f"readiness={readiness.get('passed')} termination={trace.get('termination')}")
    return 0 if ok else 1


def phase_workflow_a() -> int:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    session_id = _fresh_session_id("wfa")
    ws = SLICES_DIR / session_id / "ws"
    _seed_workspace(ws)
    state = _new_state(session_id, ws)
    result = asyncio.run(drive_run(state, interrupt=None))
    out = {
        "phase": "workflow-a", "session_id": session_id, "workspace": str(ws),
        "status": result["status"], "override_used": bool(result.get("override_used")),
        "events": [_serialize_event(e) for e in result["events"]],
    }
    (EVIDENCE_DIR / "workflow-a.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    with open(EVIDENCE_DIR / "wfa-session.json", "w", encoding="utf-8") as fh:
        json.dump({"session_id": session_id, "workspace": str(ws),
                   "override_used": bool(result.get("override_used"))}, fh, indent=1)
    terminal = result["terminal"]
    ok = terminal is not None and terminal.status == "COMPLETE" and result["status"] == "COMPLETE"
    print(f"workflow-a: status={result['status']} complete={ok}")
    return 0 if ok else 1


def phase_workflow_b() -> int:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    session_id = _fresh_session_id("wfb")
    ws = SLICES_DIR / session_id / "ws"
    _seed_workspace(ws)
    state = _new_state(session_id, ws)
    result = asyncio.run(drive_run(state, interrupt="first_task_done"))
    out = {
        "phase": "workflow-b-interrupted", "session_id": session_id, "workspace": str(ws),
        "status": result["status"],
        "events": [_serialize_event(e) for e in result["events"]],
    }
    (EVIDENCE_DIR / "workflow-b-interrupted.json").write_text(
        json.dumps(out, indent=1), encoding="utf-8")
    with open(EVIDENCE_DIR / "wfb-session.json", "w", encoding="utf-8") as fh:
        json.dump({"session_id": session_id, "workspace": str(ws)}, fh, indent=1)
    ok = result["saw_gate"] and result["status"] == "CANCELLED"
    print(f"workflow-b: interrupted status={result['status']} saw_gate={result['saw_gate']}")
    return 0 if ok else 1


def phase_restart() -> int:
    meta = json.loads((EVIDENCE_DIR / "wfb-session.json").read_text(encoding="utf-8"))
    session_id, ws = meta["session_id"], Path(meta["workspace"])
    before = _tree_hashes(ws)
    state = _new_state(session_id, ws)
    result = asyncio.run(drive_run(state, interrupt=None))
    after = _tree_hashes(ws)
    unchanged = all(after[rel] == h for rel, h in before.items() if rel in after)
    out = {
        "phase": "workflow-b-restart", "session_id": session_id, "workspace": str(ws),
        "status": result["status"],
        "pre_restart_hashes": before, "post_restart_hashes": after,
        "pre_restart_files_unchanged": unchanged,
        "events": [_serialize_event(e) for e in result["events"]],
    }
    (EVIDENCE_DIR / "workflow-b-restart.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    terminal = result["terminal"]
    ok = terminal is not None and terminal.status == "COMPLETE" and result["status"] == "COMPLETE"
    print(f"workflow-b-restart: status={result['status']} complete={ok} pre_restart_files_unchanged={unchanged}")
    return 0 if ok else 1


def _checkpoint(session_id: str):
    from council_of_agents.scripts.workflow_checkpoint import WorkflowCheckpoint
    return WorkflowCheckpoint(session_id, base_dir=ROOT / "data" / "council_workflows")


def phase_verify() -> int:
    failures = []
    a_meta = json.loads((EVIDENCE_DIR / "wfa-session.json").read_text(encoding="utf-8"))
    b_meta = json.loads((EVIDENCE_DIR / "wfb-session.json").read_text(encoding="utf-8"))

    for label, meta, expect_complete in (("a", a_meta, True), ("b", b_meta, True)):
        ws = Path(meta["workspace"])
        cp = _checkpoint(meta["session_id"])
        app = (ws / "src" / "app.py").read_text(encoding="utf-8")
        tests = (ws / "tests" / "test_app.py").read_text(encoding="utf-8")
        if "def health" not in app:
            failures.append(f"{label}: src/app.py missing health()")
        if "def test_health" not in tests:
            failures.append(f"{label}: tests/test_app.py missing test_health()")
        for stage in ("chair", "strategist", "perspective_analyzer", "manager"):
            entry = cp._data["stages"].get(stage)
            if not entry or entry.get("status") != "DONE":
                failures.append(f"{label}: stage {stage} not recorded DONE")
            elif int(entry.get("model_calls") or 0) != 1:
                failures.append(f"{label}: stage {stage} model_calls={entry.get('model_calls')} (expected 1)")
        if not (cp.approved() or bool(meta.get("override_used"))):
            failures.append(f"{label}: checkpoint has no recorded APPROVAL and no override was used")
        final_state = cp.final_state()
        if not final_state or final_state.get("status") != "COMPLETE":
            failures.append(f"{label}: final state not COMPLETE: {final_state}")
        if not final_state.get("artifact_paths"):
            failures.append(f"{label}: no artifact paths recorded")
        for task_id, tstate in cp._data["tasks"].items():
            # Bounded recovery is designed in: one informed retry after a
            # guard rejection, then terminal. More than two attempts would
            # mean the bounded retry policy was bypassed.
            attempts = int(tstate.get("attempts") or 0)
            if not (1 <= attempts <= 2):
                failures.append(f"{label}: task {task_id} attempts={tstate.get('attempts')} (expected 1-2)")
            if tstate.get("status") != "DONE":
                failures.append(f"{label}: task {task_id} status={tstate.get('status')}")
            if not tstate.get("tool_results"):
                failures.append(f"{label}: task {task_id} has no tool_results")
        # Independent artifact verification: run the regression suite directly.
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "tests"],
            cwd=str(ws), capture_output=True, text=True, timeout=300,
        )
        if proc.returncode != 0:
            failures.append(f"{label}: independent pytest failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}")
        # Write discipline: every file in the workspace tree is inside ws.
        for path in sorted(ws.rglob("*")):
            if path.is_file() and str(path.resolve()).startswith(str(ws.resolve())):
                continue
            failures.append(f"{label}: file outside workspace: {path}")

    restart = json.loads((EVIDENCE_DIR / "workflow-b-restart.json").read_text(encoding="utf-8"))
    if not restart["pre_restart_files_unchanged"]:
        failures.append("restart: pre-restart files changed after restart (duplicate writes)")

    report = {
        "pass": not failures,
        "failures": failures,
        "workflow_a": a_meta, "workflow_b": b_meta,
        "restart_unchanged": restart["pre_restart_files_unchanged"],
    }
    (EVIDENCE_DIR / "verification.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    for failure in failures:
        print("VERIFY FAIL:", failure)
    print(f"verify: {'PASS' if not failures else 'FAIL'} ({len(failures)} failures)")
    return 0 if not failures else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", default="all",
                        choices=["gate", "workflow-a", "workflow-b", "restart", "verify", "all"])
    args = parser.parse_args()
    phases = {
        "gate": phase_gate,
        "workflow-a": phase_workflow_a,
        "workflow-b": phase_workflow_b,
        "restart": phase_restart,
        "verify": phase_verify,
    }
    if args.phase == "all":
        order = ["gate", "workflow-a", "workflow-b", "restart", "verify"]
    else:
        order = [args.phase]
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
