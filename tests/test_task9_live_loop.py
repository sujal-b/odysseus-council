import asyncio
from argparse import ArgumentTypeError
import json
from pathlib import Path

import pytest

from council_of_agents.scripts.agent_runner import AgentRunner
from council_of_agents.scripts.council_recovery import RecoveryResolver, validate_recovery_config
from council_of_agents.scripts.session_store import SessionState
from council_of_agents.scripts.workflow_checkpoint import WorkflowCheckpoint


ROOT = Path(__file__).resolve().parents[1]


def test_task8_replay_records_exact_unqualified_recovery_rejection(tmp_path):
    trace = ROOT / "data/council_agent_evals/phase-a/task8-slice-runs/task8-prod-20260806-a/context-traces/task8-1785960080701-1785960080969.jsonl"
    records = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    requests = [record for record in records if record.get("kind") == "model_request"]
    assert [(record["agent"], record["endpoint_host"], record["model"]) for record in requests] == [
        ("chair", "opencode.ai", "nemotron-3-ultra-free"),
        ("strategist", "integrate.api.nvidia.com", "nvidia/nemotron-3-super-120b-a12b"),
    ]
    assert records[-1]["status"] == "PENDING"
    models = tmp_path / "models.json"
    models.write_text(json.dumps({"roles": {"chair": {
        "endpoint_url": "https://opencode.ai/zen/v1/chat/completions",
        "model": "nemotron-3-ultra-free",
        "recovery_fallbacks": [{
            "endpoint_url": "https://integrate.api.nvidia.com/v1/chat/completions",
            "model": "nvidia/nemotron-3-super-120b-a12b",
        }],
    }}}), encoding="utf-8")
    candidate, reason = RecoveryResolver(models, tmp_path / "evidence.json").recovery_decision(
        "chair", primary_endpoint="https://opencode.ai/zen/v1/chat/completions",
        primary_model="nemotron-3-ultra-free",
    )
    assert candidate is None
    assert reason == "nvidia/nemotron-3-super-120b-a12b lacks 3/3 initial-contract+semantic evidence"


def test_required_role_without_primary_route_fails_startup(tmp_path):
    models = tmp_path / "models.json"
    models.write_text(json.dumps({"roles": {"chair": {"endpoint_url": "https://x", "model": "m"}}}), encoding="utf-8")
    errors = validate_recovery_config(models, tmp_path / "evidence.json", required_roles=("chair", "manager"))
    assert errors == ["manager: required role lacks a usable primary route"]


@pytest.mark.asyncio
async def test_schema_exhaustion_persists_model_failure_without_recovery(monkeypatch):
    class Router:
        def role_config(self, *_args):
            return type("Config", (), {"endpoint_url": "https://opencode.ai/v1", "model": "model"})()

    class Orchestrator:
        _router = Router()
        AGENT_TIMEOUTS = {"chair": 1}
        AGENT_HARD_TIMEOUTS = {"chair": 1}
        AGENT_MAX_RETRIES = {"chair": 0}

        async def _call_agent(self, *_args, **_kwargs):
            return "not JSON"

    state = SessionState(session_id="task9-schema", owner="test", user_prompt="fix", status="PENDING")
    runner = AgentRunner(Orchestrator(), state, emit=None)
    with pytest.raises(Exception):
        await runner.invoke("chair", [{"role": "user", "content": "fix"}], max_retries=0)
    failure = state.metadata["chair_failure"]
    assert failure["terminal_state"] == "MODEL_FAILURE"
    assert failure["reason"]
    assert failure["checkpoint_eligible"] is False


def test_terminal_failure_and_checkpoint_keep_nonempty_reason(tmp_path):
    from council_of_agents.scripts.council_orchestrator import CouncilOrchestrator

    state = SessionState(session_id="task9-terminal", owner="test", user_prompt="fix", status="PENDING")
    state.metadata = {"strategist_failure": {
        "role": "strategist", "terminal_state": "MODEL_FAILURE", "reason": "schema invalid",
    }}
    failure = CouncilOrchestrator._terminal_failure(state, "MODEL_FAILURE", "fallback")
    checkpoint = WorkflowCheckpoint("task9-terminal", base_dir=tmp_path)
    checkpoint.record_final(failure["status"], "", [], failure=failure)
    assert failure["reason"] == "schema invalid"
    assert checkpoint.final_state()["failure"]["reason"] == "schema invalid"

def test_chair_primary_is_qualified_nvidia():
    from council_of_agents.scripts.council_router import CouncilRouter

    router = CouncilRouter(str(ROOT / "council_of_agents/config/models.json"))
    chair = router.role_config("chair")
    assert chair.endpoint_url == "https://integrate.api.nvidia.com/v1/chat/completions"
    assert chair.model == "nvidia/nemotron-3-super-120b-a12b"

def test_terminal_failure_uses_last_emitted_error():
    from council_of_agents.scripts.council_orchestrator import CouncilOrchestrator

    state = SessionState(session_id="task11-terminal", owner="test", user_prompt="fix", status="IN_PROGRESS")
    state.metadata = {"_last_error": {"role": "strategist", "stage": "strategist", "reason": "plan does not select a target from repository reconnaissance evidence"}}
    failure = CouncilOrchestrator._terminal_failure(state, "MODEL_FAILURE", "run exited without terminal state")
    assert failure["role"] == "strategist"
    assert failure["reason"] == "plan does not select a target from repository reconnaissance evidence"

def test_launcher_timeout_rejects_executor_kill_window():
    from scripts.task9_live_restart import _run_timeout

    assert _run_timeout("2400") == 2400
    with pytest.raises(ArgumentTypeError):
        _run_timeout("124")