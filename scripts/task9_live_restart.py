"""One approval-gated Task 9 production workflow; reuses Task 7 driver."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from council_of_agents.scripts.context_envelope import build_repository_capsule
from council_of_agents.scripts.session_store import SessionState
from council_of_agents.scripts.workflow_checkpoint import WorkflowCheckpoint
from scripts import task7_vertical_slice as slice

PROMPT = "Fix the session loader so sessions with zero messages remain visible after reload, and add the focused regression test. Use the repository evidence to select the target."
ROLES = ["chair", "strategist", "perspective_analyzer", "manager", "implementer", "completeness_auditor"]
CHAIR_CANDIDATE = {"endpoint_url": "https://integrate.api.nvidia.com/v1/chat/completions", "model": "nvidia/nemotron-3-super-120b-a12b", "temperature": 0.0, "max_tokens": 1024}


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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
        "    assert SessionManager().load_sessions([{'message_count': 1}]) == [{'message_count': 1}]\n\n"
        "def test_zero_message_session_remains_visible_after_reload():\n"
        "    row = {'message_count': 0}\n"
        "    assert SessionManager().load_sessions([row]) == [row]\n",
        encoding="utf-8",
    )


def _payload(facts: dict) -> str:
    from council_of_agents.scripts.council_orchestrator import CouncilOrchestrator
    from council_of_agents.scripts.council_schemas import build_response_format

    prompts = {
        role: _hash((ROOT / "council_of_agents" / "prompts" / f"{role}.md").read_text(encoding="utf-8"))
        for role in ROLES
    }
    schemas = {
        role: _hash(json.dumps(build_response_format(role) or {}, sort_keys=True, separators=(",", ":")))
        for role in ROLES
    }
    calls = {role: int(CouncilOrchestrator.AGENT_MAX_RETRIES.get(role, 1)) + 1 for role in ROLES}
    payload = {
        "user_task": PROMPT,
        "repository_capsule": {key: facts[key] for key in (
            "status", "search_terms", "selected_paths", "matching_symbols", "regression_tests",
            "test_commands", "frameworks", "allowed_workspace_scope", "discovery_required", "truncated", "limits",
        )},
        "prompt_hashes": prompts,
        "schema_hashes": schemas,
        "provider_hosts": ["integrate.api.nvidia.com", "opencode.ai"],
        "chair_route": {"host": urlsplit(CHAIR_CANDIDATE["endpoint_url"]).hostname, "model": CHAIR_CANDIDATE["model"]},
        "strategist_recovery_route": {"host": "opencode.ai", "model": "nemotron-3-ultra-free", "max_calls": 1},
        "expected_max_primary_calls": calls,
        "context_rules": ["relative paths only", "no source/diffs/credentials/environment values"],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _qualification_payload(facts: dict) -> str:
    payload = json.loads(_payload(facts))
    payload["kind"] = "chair_qualification"
    payload["provider_hosts"] = ["integrate.api.nvidia.com"]
    payload["expected_max_primary_calls"] = {"chair": 3}
    payload["chair_route"] = {"host": "integrate.api.nvidia.com", "model": CHAIR_CANDIDATE["model"]}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


async def _qualify_chair(session_id: str, workspace: Path, facts: dict) -> dict:
    from council_of_agents.scripts.council_orchestrator import CouncilOrchestrator
    from council_of_agents.scripts.council_recovery import RecoveryEvidence
    from council_of_agents.scripts.council_router import CouncilRouter
    from council_of_agents.scripts.council_schemas import validate_agent_output

    router = CouncilRouter(str(ROOT / "council_of_agents" / "config" / "models.json"))
    orchestrator = CouncilOrchestrator(router)
    orchestrator._trace_context = {"run_id": session_id, "session_id": session_id}
    messages = [
        {"role": "system", "content": orchestrator._load_prompt("chair")},
        {"role": "user", "content": orchestrator._envelope_user_msg(PROMPT, workspace=str(workspace), repository_context=facts["capsule"])},
    ]
    rows = []
    for rep in range(1, 4):
        row = {"rep": rep, "initial_contract_passed": False, "contract_passed": False, "semantic_quality": {"passed": False}, "provider_failed": False, "schema_repair_attempted": False, "error": ""}
        try:
            raw = await orchestrator._call_agent("chair", session_id, CHAIR_CANDIDATE, messages, disable_tools=True, workspace=str(workspace))
            validation = validate_agent_output("chair", raw or "", strict=True)
            data = validation.data or {}
            semantic = validation.success and data.get("route") == "PIPELINE" and data.get("action") == "write"
            row.update({"initial_contract_passed": bool(validation.success), "contract_passed": bool(validation.success), "semantic_quality": {"passed": bool(semantic), "checks": ["PIPELINE", "write"]}, "output_sha256": _hash(raw or "")})
            if not raw or not semantic:
                rows.append(row)
                break
        except Exception as exc:
            row.update({"provider_failed": True, "error": str(exc)[:1000]})
            rows.append(row)
            break
        rows.append(row)
    passed = len(rows) == 3 and all(row["initial_contract_passed"] and row["contract_passed"] and row["semantic_quality"]["passed"] and not row["provider_failed"] and not row["schema_repair_attempted"] for row in rows)
    evidence = RecoveryEvidence().record("chair", "chair", CHAIR_CANDIDATE["endpoint_url"], CHAIR_CANDIDATE["model"], rows) if passed else None
    return {"session_id": session_id, "candidate": {"host": "integrate.api.nvidia.com", "model": CHAIR_CANDIDATE["model"]}, "passed": passed, "rows": rows, "evidence": evidence}


def _state(session_id: str, workspace: Path) -> SessionState:
    return SessionState(session_id=session_id, owner="admin", user_prompt=PROMPT, workspace=str(workspace), status="PENDING")


def _write(name: str, data: dict) -> None:
    slice._write_evidence_json(name, data)

def _run_timeout(value: str) -> int:
    timeout = int(value)
    if not 125 <= timeout <= 2400:
        raise argparse.ArgumentTypeError("run timeout must be 125..2400 seconds")
    return timeout



def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--resume-workflow-id")
    parser.add_argument("--run-timeout-s", type=_run_timeout, default=2400)
    parser.add_argument("--approved-sha256")
    parser.add_argument("--approved-qualification-sha")
    parser.add_argument("--qualify-chair", action="store_true")
    parser.add_argument("--task10-preview", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    from council_of_agents.scripts.role_eval import _load_role_configs

    configs = _load_role_configs(ROOT / "council_of_agents" / "config" / "models.json")
    slice._approved_gate_endpoints({str((configs.get(role) or {}).get("endpoint_url") or "") for role in ROLES} - {""})
    slice.EVIDENCE_ROOT = ROOT / "data" / "council_agent_evals" / "phase-a" / "task9-slice-runs"
    slice.EVIDENCE_DIR = slice._prepare_evidence_dir(args.run_id, fresh=not bool(args.resume_workflow_id))
    slice._configure_context_trace()
    session_id = args.resume_workflow_id or f"task9-{int(time.time() * 1000)}"
    workspace = ROOT / "data" / "task9-slices" / args.run_id / session_id / "ws"
    if args.resume_workflow_id:
        checkpoint = WorkflowCheckpoint(session_id)
        stored = checkpoint.reconnaissance()
        if not workspace.is_dir() or not isinstance(stored, dict):
            print("RESUME CHECKPOINT MISMATCH: provider call stopped")
            return 2
        facts = build_repository_capsule(workspace, PROMPT)
        if facts.get("sha256") != stored.get("sha256"):
            print("RESUME CAPSULE MISMATCH: provider call stopped")
            return 2
    else:
        _seed(workspace)
        before = subprocess.run([sys.executable, "-m", "pytest", "-q", "tests"], cwd=workspace, capture_output=True, text=True)
        if before.returncode == 0:
            raise RuntimeError("synthetic regression must fail before workflow")
        facts = build_repository_capsule(workspace, PROMPT)
    payload = _payload(facts)
    digest = _hash(payload)
    if not args.resume_workflow_id:
        _write("payload-preview.json", {"sha256": digest, "payload": json.loads(payload)})
    qualification = _qualification_payload(facts)
    qualification_digest = _hash(qualification)
    if args.task10_preview:
        _write("chair-qualification-payload.json", {"sha256": qualification_digest, "payload": json.loads(qualification)})
        _write("workflow-payload.json", {"sha256": digest, "payload": json.loads(payload)})
        print(f"CHAIR QUALIFICATION SHA-256: {qualification_digest}")
        print(f"WORKFLOW SHA-256: {digest}")
        return 0
    if args.qualify_chair:
        if qualification_digest != args.approved_qualification_sha:
            print("QUALIFICATION HASH MISMATCH: provider call stopped")
            return 2
        result = asyncio.run(_qualify_chair(session_id, workspace, facts))
        _write("chair-qualification-result.json", result)
        print(f"CHAIR QUALIFICATION: passed={result['passed']} reps={len(result['rows'])}")
        return 0 if result["passed"] else 1
    if args.validate_only:
        print(f"PAYLOAD SHA-256: {digest}")
        return 0
    if digest != args.approved_sha256:
        print("PAYLOAD HASH MISMATCH: provider call stopped")
        return 2

    if args.resume_workflow_id:
        prior_traces = [str(path) for path in slice._trace_files(session_id)]
        resumed = asyncio.run(slice.drive_run(_state(session_id, workspace), interrupt=None, timeout_s=args.run_timeout_s))
        all_traces = [str(path) for path in slice._trace_files(session_id)]
        resumed_traces = sorted(set(all_traces) - set(prior_traces))
        resumed_records = slice._trace_records(resumed_traces)
        records = slice._trace_records(all_traces)
        test = subprocess.run([sys.executable, "-m", "pytest", "-q", "tests"], cwd=workspace, capture_output=True, text=True)
        checkpoint = WorkflowCheckpoint(session_id)
        final = checkpoint.final_state() or {}
        artifacts = final.get("artifact_paths") or []
        result = {
            "session_id": session_id, "status": resumed["status"],
            "manager_approved": resumed["manager_approved"],
            "resumed_trace_files": resumed_traces,
            "chair_calls_after_resume": sum(1 for item in resumed_records if item.get("kind") == "model_request" and item.get("agent") == "chair"),
            "reconnaissance_after_resume": sum(1 for item in resumed_records if item.get("kind") == "repository_reconnaissance"),
            "implementer_calls": sum(1 for item in records if item.get("kind") == "model_request" and item.get("agent") == "implementer"),
            "scope_violations": slice._scope_violations(records), "pytest_returncode": test.returncode,
            "pytest_tail": (test.stdout + test.stderr)[-2000:], "final": final, "artifacts": artifacts,
            "artifact_sha256": {path: _hash((workspace / path).read_text(encoding="utf-8")) for path in artifacts if (workspace / path).is_file()},
        }
        _write("resume-result.json", result)
        ok = resumed["status"] == "COMPLETE" and final.get("status") == "COMPLETE" and test.returncode == 0 and bool(artifacts) and result["implementer_calls"] == 1 and not result["chair_calls_after_resume"] and not result["reconnaissance_after_resume"] and not result["scope_violations"]
        print(f"TASK11 RESUME: status={resumed['status']} complete={ok} pytest={test.returncode}")
        return 0 if ok else 1
    first = asyncio.run(slice.drive_run(_state(session_id, workspace), interrupt="manager_approved", timeout_s=args.run_timeout_s))
    checkpoint = WorkflowCheckpoint(session_id)
    approval = first.get("approval_checkpoint") or {}
    traces = first["trace_files"]
    first_fingerprints = {(item["agent"], item["payload_hash"]) for item in slice._planning_request_fingerprints(slice._trace_records(traces))}
    _write("interrupted.json", {"session_id": session_id, "status": first["status"], "approval": approval, "trace_files": traces, "checkpoint": checkpoint._data})
    if not (first["status"] == "CANCELLED" and approval.get("manager_approval") == "APPROVED"):
        print("INTERRUPT CHECKPOINT FAILED: no restart or retry")
        return 1

    resumed = asyncio.run(slice.drive_run(_state(session_id, workspace), interrupt=None, timeout_s=args.run_timeout_s))
    all_traces = [str(path) for path in slice._trace_files(session_id)]
    resumed_traces = sorted(set(all_traces) - set(traces))
    resumed_fingerprints = {(item["agent"], item["payload_hash"]) for item in slice._planning_request_fingerprints(slice._trace_records(resumed_traces))}
    records = slice._trace_records(all_traces)
    test = subprocess.run([sys.executable, "-m", "pytest", "-q", "tests"], cwd=workspace, capture_output=True, text=True)
    checkpoint = WorkflowCheckpoint(session_id)
    final = checkpoint.final_state() or {}
    artifacts = final.get("artifact_paths") or []
    result = {
        "session_id": session_id, "status": resumed["status"], "manager_approved": resumed["manager_approved"],
        "implementer_calls": sum(1 for item in records if item.get("kind") == "model_request" and item.get("agent") == "implementer"),
        "duplicate_planning_requests": sorted(first_fingerprints & resumed_fingerprints),
        "scope_violations": slice._scope_violations(records), "pytest_returncode": test.returncode,
        "pytest_tail": (test.stdout + test.stderr)[-2000:], "final": final, "artifacts": artifacts,
        "artifact_sha256": {path: _hash((workspace / path).read_text(encoding="utf-8")) for path in artifacts if (workspace / path).is_file()},
    }
    _write("restart-result.json", result)
    ok = resumed["status"] == "COMPLETE" and final.get("status") == "COMPLETE" and test.returncode == 0 and bool(artifacts) and result["implementer_calls"] == 1 and not result["duplicate_planning_requests"] and not result["scope_violations"]
    print(f"TASK9 LIVE: status={resumed['status']} complete={ok} pytest={test.returncode}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
