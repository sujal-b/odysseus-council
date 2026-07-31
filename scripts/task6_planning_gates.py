"""Task 6 deterministic planning gates + execution proof.

Runs three distinct planning cases against the P2.6 Strategist candidate with a
stubbed provider (deterministic, no network), then runs one approved plan
through the gauntlet Implementer + Completeness workflow in a disposable
workspace with real file changes.

Exit 0 only when every gate passes.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from council_of_agents.scripts.gauntlet_eval import load_scenarios, run_scenario
from council_of_agents.scripts.role_eval import _bounded_evaluate_trace, _expected_scenario_outcome

P26 = Path("data/council_agent_evals/phase-a/prompts/P2.6")
EVIDENCE_DIR = Path("data/council_agent_evals/phase-a/task6-gates")
ENDPOINT = "https://example.test/v1/chat/completions"
MODEL = "mock"

CHAIR_MEDIUM = json.dumps({
    "complexity": "MEDIUM", "route": "PIPELINE", "action": "write",
    "target": "health endpoint and regression test",
    "reason": "The change needs a plan, implementation, and verification.",
})
CHAIR_MEDIUM_FLIGHT = json.dumps({
    "complexity": "MEDIUM", "route": "PIPELINE", "action": "write",
    "target": "flight tracker dashboard",
    "reason": "Requires a small frontend and data integration.",
})

PERSPECTIVE_CLEAN = json.dumps({
    "security": {"score": 0.95, "issues": []},
    "performance": {"score": 0.95, "issues": []},
    "maintainability": {"score": 0.9, "issues": []},
    "overall_score": 0.93,
    "synthesis": "The plan is small, scoped, and directly verifiable.",
})


def make_stub(plan_v1: str, plan_v2: str, chair: str, manager_issue: dict) -> callable:
    """Build a deterministic provider stub that serves each planning stage."""

    perspective = PERSPECTIVE_CLEAN
    manager_challenge = json.dumps({
        "verdict": "REVISE", "confidence": 0.6,
        "summary": "One evidence-based improvement is required before approval.",
        "issues": [manager_issue],
    })
    manager_approved = json.dumps({
        "verdict": "APPROVED", "confidence": 0.95,
        "summary": "The revision resolves the cited defect; the plan is grounded and executable.",
        "issues": [],
    })

    async def stub_call(**_kwargs):
        messages = _kwargs.get("messages") or []
        joined = "\n".join(str(m.get("content") or "") for m in messages)
        if "Manager requested one plan revision" in joined:
            return plan_v2
        if "Return the Strategist JSON contract now." in joined:
            return plan_v1
        if "Strategist plan to audit:" in joined:
            return perspective
        if "compulsory Plan-0 challenge round" in joined:
            return manager_challenge
        if "This is a revision review" in joined:
            return manager_approved
        return chair

    return stub_call


def run_trace(case: dict) -> dict:
    """Run one deterministic planning trace under the P2.6 candidate."""
    async def go():
        return await _bounded_evaluate_trace(
            case["user_prompt"],
            endpoint=ENDPOINT,
            model=MODEL,
            workspace=case.get("workspace", ""),
            repository_context=case.get("repository_context", ""),
            run_id=f"task6-gate-{case['name']}",
            max_plan_revisions=1,
            planning_only=True,
            scenario_id=case["name"],
            scenario_rubric=case.get("planning_rubric"),
            prompt_label="P2.6-candidate",
            handoff_mode="contract",
            prompts_dir=P26,
            call=make_stub(case["plan_v1"], case["plan_v2"], case["chair"], case["manager_issue"]),
        )

    return asyncio.run(go())


def assert_planning_gate(case: dict) -> dict:
    trace = run_trace(case)
    summary = trace.get("summary") or {}
    readiness = trace.get("readiness_gate") or {}
    passed, outcome = _expected_scenario_outcome({"expected_final_verdict": "APPROVED"}, trace)
    failures = []
    if summary.get("contract_validity") != 1.0:
        failures.append(f"contract_validity={summary.get('contract_validity')}")
    if summary.get("schema_repair_attempts"):
        failures.append(f"repair_attempts={summary.get('schema_repair_attempts')}")
    if summary.get("provider_failures"):
        failures.append(f"provider_failures={summary.get('provider_failures')}")
    if trace.get("termination") not in ("COMPLETE",):
        failures.append(f"termination={trace.get('termination')}")
    if str(summary.get("final_manager_verdict") or "") != "APPROVED":
        failures.append(f"final_manager_verdict={summary.get('final_manager_verdict')}")
    if not readiness.get("passed"):
        failures.append(f"readiness_failures={readiness.get('failures')}")
    if not passed:
        failures.append(f"scenario_outcome={outcome}")
    strategist_stage = next(
        (r for r in trace.get("trace") or [] if r.get("agent") == "strategist"),
        {},
    )
    if not (strategist_stage.get("semantic_quality") or {}).get("passed"):
        failures.append("strategist_semantic_quality=failed")
    return {"trace": trace, "failures": failures}


# ---------------------------------------------------------------- gate cases

GATE1 = {
    "name": "backend-health-endpoint",
    "user_prompt": "Add a health endpoint and a regression test to the small service.",
    "workspace": "/tmp/ws-backend",
    "repository_context": (
        "<repository_capsule>\n"
        "Authoritative paths to inspect, not assumptions to blindly trust:\n"
        "- src/app.py: existing application entrypoint exposing app().\n"
        "- tests/test_app.py: existing regression tests.\n"
        "Framework: Python stdlib + pytest. Do not introduce a new dependency.\n"
        "</repository_capsule>"
    ),
    "planning_rubric": {
        "minimum_tasks": 3,
        "scope_policy": "declared_or_workspace_root",
        "require_inspection_task": True,
        "require_verification": True,
        "require_risks": True,
        "required_read_scopes": ["src/", "tests/"],
        "required_write_scopes": ["src/", "tests/"],
        "required_terms": ["inspect", "regression", "src/app.py"],
        "forbidden_terms": ["from scratch", "greenfield"],
    },
    "chair": CHAIR_MEDIUM,
    "manager_issue": {
        "severity": "warning", "task_id": "T2",
        "description": "T2 verification only imports health; it does not run the regression suite.",
        "suggestion": "Change T2 verification to run the focused pytest for the health test.",
        "evidence": "T2 verification is an import check; the plan must prove the change with the regression test.",
    },
    "plan_v1": json.dumps({
        "tasks": [
            {"id": "T1",
             "description": "Inspect src/app.py and tests/test_app.py to confirm the existing app() shape and test style before editing.",
             "depends_on": [], "acceptance": "The existing implementation and regression style are identified.",
             "read_scope": ["src/", "tests/"], "write_scope": []},
            {"id": "T2",
             "description": "Add a health endpoint function health() to src/app.py returning {'status':'ok'} and wire it next to app().",
             "depends_on": ["T1"], "acceptance": "src/app.py defines health() and imports cleanly.",
             "read_scope": ["src/"], "write_scope": ["src/"],
             "verification": {"type": "shell", "command": "python -c 'from src.app import health'"}},
            {"id": "T3",
             "description": "Add a regression test test_health to tests/test_app.py asserting health() returns ok.",
             "depends_on": ["T2"], "acceptance": "pytest -q tests/test_app.py::test_health passes.",
             "read_scope": ["tests/"], "write_scope": ["tests/"],
             "verification": {"type": "shell", "command": "pytest -q tests/test_app.py::test_health"}},
        ],
        "risks": ["Keep the health endpoint side-effect free so the regression test stays deterministic."],
    }),
    "plan_v2": json.dumps({
        "tasks": [
            {"id": "T1",
             "description": "Inspect src/app.py and tests/test_app.py to confirm the existing app() shape and test style before editing.",
             "depends_on": [], "acceptance": "The existing implementation and regression style are identified.",
             "read_scope": ["src/", "tests/"], "write_scope": []},
            {"id": "T2",
             "description": "Add a health endpoint function health() to src/app.py returning {'status':'ok'}, then run the focused regression suite.",
             "depends_on": ["T1"], "acceptance": "src/app.py defines health() and the focused health regression passes.",
             "read_scope": ["src/"], "write_scope": ["src/"],
             "verification": {"type": "shell", "command": "pytest -q tests/test_app.py::test_health"}},
            {"id": "T3",
             "description": "Add a regression test test_health to tests/test_app.py asserting health() returns ok.",
             "depends_on": ["T2"], "acceptance": "pytest -q tests/test_app.py::test_health passes.",
             "read_scope": ["tests/"], "write_scope": ["tests/"],
             "verification": {"type": "shell", "command": "pytest -q tests/test_app.py::test_health"}},
        ],
        "risks": ["Keep the health endpoint side-effect free so the regression test stays deterministic."],
    }),
}

GATE2 = {
    "name": "happy-path-flight-dashboard",
    "user_prompt": "Build a small flight tracker dashboard.",
    "workspace": "/tmp/ws-dashboard",
    "repository_context": "",
    "planning_rubric": {
        "minimum_tasks": 2,
        "scope_policy": "declared_or_workspace_root",
        "require_verification": True,
        "required_terms": ["flight"],
        "required_terms_any": [["dashboard", "flight-tracker"]],
        "forbidden_terms": ["production data", "delete"],
    },
    "chair": CHAIR_MEDIUM_FLIGHT,
    "manager_issue": {
        "severity": "warning", "task_id": "T1",
        "description": "T1 has no verification command proving the server starts.",
        "suggestion": "Add a command that verifies package.json and the server entrypoint exist.",
        "evidence": "T1 acceptance only states the project is initialized; verification is missing.",
    },
    "plan_v1": json.dumps({
        "tasks": [
            {"id": "T1",
             "description": "Initialize the Node.js project at the workspace root: create package.json, install express, and create src/server.js serving static files from public/.",
             "depends_on": [], "acceptance": "package.json and src/server.js exist and express is installed.",
             "workspace_root": True, "write_scope": [],
             "verification": {"type": "shell", "command": "test -f package.json && test -f src/server.js"}},
            {"id": "T2",
             "description": "Create public/index.html for the flight-tracker dashboard with a map container and a script tag loading script.js.",
             "depends_on": ["T1"], "acceptance": "public/index.html exists and references script.js.",
             "read_scope": ["public/"], "write_scope": ["public/"],
             "verification": {"type": "shell", "command": "test -f public/index.html"}},
            {"id": "T3",
             "description": "Create public/script.js that fetches flight data from /api/flights and renders flight rows in the dashboard.",
             "depends_on": ["T2"], "acceptance": "public/script.js exists and defines the render function.",
             "read_scope": ["public/"], "write_scope": ["public/"],
             "verification": {"type": "shell", "command": "grep -q renderFlights public/script.js"}},
        ],
        "risks": ["Keep the initial data source local and bounded until live API requirements are explicit."],
    }),
    "plan_v2": json.dumps({
        "tasks": [
            {"id": "T1",
             "description": "Initialize the Node.js project at the workspace root: create package.json, install express, and create src/server.js serving static files from public/.",
             "depends_on": [], "acceptance": "package.json and src/server.js exist, express is installed, and the server starts.",
             "workspace_root": True, "write_scope": [],
             "verification": {"type": "shell", "command": "test -f package.json && test -f src/server.js && node -c src/server.js"}},
            {"id": "T2",
             "description": "Create public/index.html for the flight-tracker dashboard with a map container and a script tag loading script.js.",
             "depends_on": ["T1"], "acceptance": "public/index.html exists and references script.js.",
             "read_scope": ["public/"], "write_scope": ["public/"],
             "verification": {"type": "shell", "command": "test -f public/index.html"}},
            {"id": "T3",
             "description": "Create public/script.js that fetches flight data from /api/flights and renders flight rows in the dashboard.",
             "depends_on": ["T2"], "acceptance": "public/script.js exists and defines the render function.",
             "read_scope": ["public/"], "write_scope": ["public/"],
             "verification": {"type": "shell", "command": "grep -q renderFlights public/script.js"}},
        ],
        "risks": ["Keep the initial data source local and bounded until live API requirements are explicit."],
    }),
}

GATE3 = {
    "name": "unknown-target-session-bug",
    "user_prompt": "Fix the bug that hides Council sessions after reload when their message count is zero, and add a regression test.",
    "workspace": "/tmp/ws-session",
    "repository_context": (
        "<repository_capsule>\n"
        "Authoritative paths to inspect, not assumptions to blindly trust:\n"
        "- core/ holds session persistence; the exact file and function are NOT supplied.\n"
        "- tests/ holds existing regression coverage.\n"
        "Constraint: locate the message_count filter inside core/ before planning any write.\n"
        "</repository_capsule>"
    ),
    "planning_rubric": {
        "minimum_tasks": 3,
        "require_inspection_task": True,
        "require_verification": True,
        "require_risks": True,
        "required_read_scopes": ["core/", "tests/"],
        "required_write_scopes": ["core/", "tests/"],
        "required_terms": ["inspect", "regression", "message_count"],
        "forbidden_terms": ["from scratch", "greenfield"],
    },
    "chair": json.dumps({
        "complexity": "COMPLEX", "route": "PIPELINE", "action": "write",
        "target": "Council session reload bug",
        "reason": "The fix spans session loading and regression coverage.",
    }),
    "manager_issue": {
        "severity": "warning", "task_id": "T2",
        "description": "T2 does not verify the fix with a regression run.",
        "suggestion": "Add a verification command that runs the focused regression test file.",
        "evidence": "T2 acceptance describes the fix but has no machine-checkable verification.",
    },
    "plan_v1": json.dumps({
        "tasks": [
            {"id": "T1",
             "description": "Inspect core/ and tests/ to locate the session-loading path that filters rows by message_count, and confirm the zero-message Council reload case.",
             "depends_on": [], "acceptance": "The failure path and the smallest safe fix are identified from repository evidence.",
             "read_scope": ["core/", "tests/"], "write_scope": []},
            {"id": "T2",
             "description": "Update the session loading path in core/ so valid zero-message Council sessions remain visible after reload.",
             "depends_on": ["T1"], "acceptance": "Reloading preserves valid zero-message Council sessions.",
             "read_scope": ["core/"], "write_scope": ["core/"]},
            {"id": "T3",
             "description": "Add a regression test for zero-message Council session visibility under tests/.",
             "depends_on": ["T2"], "acceptance": "The regression test fails before the fix and passes after it.",
             "read_scope": ["core/", "tests/"], "write_scope": ["tests/"],
             "verification": {"type": "shell", "command": "pytest -q tests/test_archived_sessions_model_filter.py"}},
        ],
        "risks": ["Preserve Council sessions with zero messages without making ordinary empty sessions permanent."],
    }),
    "plan_v2": json.dumps({
        "tasks": [
            {"id": "T1",
             "description": "Inspect core/ and tests/ to locate the session-loading path that filters rows by message_count, and confirm the zero-message Council reload case.",
             "depends_on": [], "acceptance": "The failure path and the smallest safe fix are identified from repository evidence.",
             "read_scope": ["core/", "tests/"], "write_scope": []},
            {"id": "T2",
             "description": "Update the session loading path in core/ so valid zero-message Council sessions remain visible after reload, then run the focused regression test file.",
             "depends_on": ["T1"], "acceptance": "Reloading preserves valid zero-message Council sessions and the regression file passes.",
             "read_scope": ["core/", "tests/"], "write_scope": ["core/"],
             "verification": {"type": "shell", "command": "pytest -q tests/test_archived_sessions_model_filter.py"}},
            {"id": "T3",
             "description": "Add a regression test for zero-message Council session visibility under tests/.",
             "depends_on": ["T2"], "acceptance": "The regression test fails before the fix and passes after it.",
             "read_scope": ["core/", "tests/"], "write_scope": ["tests/"],
             "verification": {"type": "shell", "command": "pytest -q tests/test_archived_sessions_model_filter.py"}},
        ],
        "risks": ["Preserve Council sessions with zero messages without making ordinary empty sessions permanent."],
    }),
}


def gate_matrix(results: dict) -> str:
    lines = []
    lines.append(f"{'gate':<38} {'contract':>8} {'semantics':>9} {'manager':>8} {'readiness':>9} {'scenario':>8} {'repair':>6} {'provider':>8}")
    for name, entry in results.items():
        t = entry["trace"]
        s = t.get("summary") or {}
        lines.append(f"{name:<38} {s.get('contract_validity'):>8} {str((next((r for r in t.get('trace') or [] if r.get('agent')=='strategist'), {}).get('semantic_quality') or {}).get('passed')):>9} {str(s.get('final_manager_verdict')):>8} {str((t.get('readiness_gate') or {}).get('passed')):>9} {str(not entry['failures']):>8} {s.get('schema_repair_attempts', 0):>6} {s.get('provider_failures', 0):>8}")
    return "\n".join(lines)


def main() -> int:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    results = {}
    for case in (GATE1, GATE2, GATE3):
        entry = assert_planning_gate(case)
        results[case["name"]] = entry
        (EVIDENCE_DIR / f"planning-{case['name']}.json").write_text(
            json.dumps(entry["trace"], indent=1, ensure_ascii=False), encoding="utf-8")
        if entry["failures"]:
            print(f"GATE FAILED: {case['name']}")
            for f in entry["failures"]:
                print("  -", f)
            print(gate_matrix(results))
            return 1
    print("PLANNING GATES 3/3 PASS")
    print(gate_matrix(results))
    (EVIDENCE_DIR / "planning-matrix.txt").write_text(
        gate_matrix(results) + "\n", encoding="utf-8")

    # Execution proof: one approved plan through Implementer + Completeness.
    scenarios = load_scenarios()
    happy = scenarios["happy_path"]
    root = Path(tempfile.mkdtemp(prefix="task6-exec-", dir="data"))
    try:
        result = asyncio.run(run_scenario("happy_path", happy, root / "ws"))
        (EVIDENCE_DIR / "execution-happy-path.json").write_text(
            json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
        status = result["status"]
        if status != happy["expected_status"]:
            print("EXECUTION FAILED: status", status, "expected", happy["expected_status"])
            return 1
        app = (root / "ws" / "src" / "app.py").read_text(encoding="utf-8")
        tests = (root / "ws" / "tests" / "test_app.py").read_text(encoding="utf-8")
        if "def health" not in app:
            print("EXECUTION FAILED: src/app.py has no health()")
            return 1
        if "def test_health" not in tests:
            print("EXECUTION FAILED: tests/test_app.py has no test_health()")
            return 1
        roles = {r["role"] for r in result["trace"] if "role" in r}
        if not {"implementer", "completeness_auditor"} <= roles:
            print("EXECUTION FAILED: missing Implementer or Completeness stage")
            return 1
        audits = [r for r in result["trace"] if r.get("role") == "completeness_auditor"]
        final_audit = json.loads(audits[-1]["output"]) if audits else {}
        if not final_audit.get("done"):
            print("EXECUTION FAILED: Completeness did not reach DONE")
            return 1
        print("EXECUTION COMPLETE: real diff, authorized scope, Completeness DONE")
        print("  workspace:", root / "ws")
        print("  src/app.py contains health():", "def health" in app)
        print("  tests/test_app.py contains test_health():", "def test_health" in tests)
        print("  final verification passed: src/app.py def health + tests/test_app.py def test_health")
        artifact_proof = {
            "scenario": "happy_path",
            "status": status,
            "workspace": str(root / "ws"),
            "src_app_py": app,
            "tests_test_app_py": tests,
            "health_present": "def health" in app,
            "test_health_present": "def test_health" in tests,
            "completeness_done": bool(final_audit.get("done")),
            "completeness_output": final_audit,
            "implementer_stages": [r.get("stage") for r in result["trace"] if r.get("role") == "implementer"],
        }
        (EVIDENCE_DIR / "execution-artifact-proof.json").write_text(
            json.dumps(artifact_proof, indent=1, ensure_ascii=False), encoding="utf-8")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
