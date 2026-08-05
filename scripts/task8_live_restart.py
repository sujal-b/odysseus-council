"""One approved Task 8 production workflow; reuses Task 7 driver internals."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from council_of_agents.scripts.context_envelope import build_repository_capsule
from council_of_agents.scripts.session_store import SessionState
from council_of_agents.scripts.workflow_checkpoint import WorkflowCheckpoint
from scripts import task7_vertical_slice as slice

PROMPT = "Fix the bug that hides Council sessions after reload when their message count is zero, and add a regression test."
APPROVED_SHA256 = "cb45f8345a381369e4fc4a9fdd35ae46df1551bd1825bdb46383e153688062c1"


def _seed(workspace: Path) -> None:
    (workspace / "core").mkdir(parents=True)
    (workspace / "tests").mkdir()
    (workspace / "pyproject.toml").write_text("[tool.pytest.ini_options]\ntestpaths = ['tests']\n", encoding="utf-8")
    (workspace / "core" / "session_manager.py").write_text(
        "class SessionManager:\n"
        "    def load_sessions(self, rows):\n"
        "        return [row for row in rows if row['message_count'] > 0]\n",
        encoding="utf-8",
    )
    (workspace / "tests" / "test_session_manager.py").write_text(
        "from core.session_manager import SessionManager\n\n"
        "def test_nonempty_session_is_loaded():\n"
        "    assert SessionManager().load_sessions([{'message_count': 1}]) == [{'message_count': 1}]\n",
        encoding="utf-8",
    )


def _approved_payload(facts: dict) -> str:
    payload = {
        "user_task": PROMPT,
        "repository_capsule": {key: facts[key] for key in (
            "status", "search_terms", "selected_paths", "matching_symbols", "regression_tests",
            "test_commands", "frameworks", "allowed_workspace_scope", "discovery_required", "truncated", "limits",
        )},
        "roles": ["chair", "strategist", "perspective_analyzer", "manager", "implementer", "completeness_auditor"],
        "context_rules": ["relative paths only", "no source/diffs/credentials/environment values", "frozen role prompts and JSON schemas only"],
        "provider_hosts": ["integrate.api.nvidia.com", "opencode.ai"],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _state(session_id: str, workspace: Path) -> SessionState:
    return SessionState(session_id=session_id, owner="admin", user_prompt=PROMPT, workspace=str(workspace), status="PENDING")


def _record(name: str, payload: dict) -> None:
    slice._write_evidence_json(name, payload)


def _fingerprints(paths: list[str]) -> list[dict]:
    return slice._planning_request_fingerprints(slice._trace_records(paths))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    from council_of_agents.scripts.role_eval import _load_role_configs
    role_configs = _load_role_configs(ROOT / "council_of_agents" / "config" / "models.json")
    slice._approved_gate_endpoints({str((role_configs.get(role) or {}).get("endpoint_url") or "") for role in ("chair", "strategist", "perspective_analyzer", "manager", "implementer", "completeness_auditor")} - {""})
    slice.EVIDENCE_ROOT = ROOT / "data" / "council_agent_evals" / "phase-a" / "task8-slice-runs"
    evidence = slice._prepare_evidence_dir(args.run_id, fresh=True)
    slice.EVIDENCE_DIR = evidence
    slice._configure_context_trace()
    session_id = f"task8-{int(time.time() * 1000)}"
    workspace = ROOT / "data" / "task8-slices" / args.run_id / session_id / "ws"
    _seed(workspace)
    facts = build_repository_capsule(workspace, PROMPT)
    payload = _approved_payload(facts)
    if _hash(payload) != APPROVED_SHA256:
        _record("payload-mismatch.json", {"approved_sha256": APPROVED_SHA256, "actual_sha256": _hash(payload), "facts": facts})
        print("PAYLOAD HASH MISMATCH: provider call stopped")
        return 2
    _record("payload-approved.json", {"sha256": _hash(payload), "facts": facts})
    if args.validate_only:
        print(f"PAYLOAD HASH OK: {_hash(payload)}")
        return 0

    first = asyncio.run(slice.drive_run(_state(session_id, workspace), interrupt="manager_approved"))
    approval = first.get("approval_checkpoint") or {}
    cp = WorkflowCheckpoint(session_id)
    before = slice._tree_hashes(workspace)
    first_trace = first["trace_files"]
    _record("interrupted.json", {
        "session_id": session_id, "status": first["status"], "approval": approval,
        "implementer_started": first["task_execution_started"], "trace_files": first_trace,
        "request_fingerprints": _fingerprints(first_trace), "checkpoint_reconnaissance": cp.reconnaissance(),
    })
    ready = first["status"] == "CANCELLED" and approval.get("manager_approval") == "APPROVED" and bool(cp.reconnaissance())
    if not ready:
        print("INTERRUPT CHECKPOINT FAILED: no restart or retry")
        return 1

    resumed = asyncio.run(slice.drive_run(_state(session_id, workspace), interrupt=None))
    all_traces = [str(path) for path in slice._trace_files(session_id)]
    resumed_paths = sorted(set(all_traces) - set(first_trace))
    before_fp = {(item["agent"], item["payload_hash"]) for item in _fingerprints(first_trace)}
    resume_fp = {(item["agent"], item["payload_hash"]) for item in _fingerprints(resumed_paths)}
    after = slice._tree_hashes(workspace)
    test = subprocess.run([sys.executable, "-m", "pytest", "-q", "tests"], cwd=workspace, text=True, capture_output=True, timeout=300)
    cp = WorkflowCheckpoint(session_id)
    final = cp.final_state() or {}
    artifacts = final.get("artifact_paths") or []
    result = {
        "session_id": session_id, "workspace": str(workspace), "status": resumed["status"],
        "manager_approved": resumed["manager_approved"], "checkpoint_reconnaissance": cp.reconnaissance(),
        "duplicate_planning_requests": sorted(before_fp & resume_fp),
        "pre_interrupt_hashes": before, "post_resume_hashes": after,
        "resume_trace_files": resumed_paths, "scope_violations": slice._scope_violations(slice._trace_records(all_traces)),
        "pytest_returncode": test.returncode, "pytest_tail": (test.stdout + test.stderr)[-2000:],
        "final": final, "artifacts": artifacts,
        "artifact_sha256": {path: _hash((workspace / path).read_text(encoding="utf-8")) for path in artifacts if (workspace / path).is_file()},
    }
    _record("restart-result.json", result)
    ok = (
        resumed["status"] == "COMPLETE" and resumed["manager_approved"] and final.get("status") == "COMPLETE"
        and not result["duplicate_planning_requests"] and not result["scope_violations"]
        and test.returncode == 0 and bool(artifacts)
    )
    print(f"TASK8 LIVE: status={resumed['status']} complete={ok} pytest={test.returncode} duplicates={len(result['duplicate_planning_requests'])}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())