import asyncio
import json
from types import SimpleNamespace

import pytest

from council_of_agents.scripts.agent_runner import AgentRunner
from council_of_agents.scripts.council_schemas import validate_agent_output


@pytest.mark.asyncio
async def test_schema_repair_is_compact_tool_free_and_preserves_original_messages():
    calls = []
    invalid = '{"severity":"critical","description":"not a Manager envelope"}'
    valid = '{"verdict":"APPROVED","confidence":0.9,"summary":"reviewed"}'

    class Orchestrator:
        AGENT_TIMEOUTS = {}
        AGENT_MAX_RETRIES = {"manager": 2}

        async def _call_agent(self, *args, **kwargs):
            calls.append((args, kwargs))
            return invalid if len(calls) == 1 else valid

    events = []

    async def emit(**kwargs):
        events.append(kwargs)

    state = SimpleNamespace(session_id="schema-repair", role_overrides={}, metadata={})
    runner = AgentRunner(Orchestrator(), state, emit, tracker=None)
    messages = [
        {"role": "system", "content": "large council instructions"},
        {"role": "user", "content": "review the implementation"},
        {"role": "tool", "content": "tool output that must not be replayed"},
    ]

    result = await runner.invoke(
        "manager", messages,
        required_contract={"task_id": "T1", "contract_hash": "sealed-hash"},
    )

    assert json.loads(result)["verdict"] == "APPROVED"
    assert len(calls) == 2
    first_messages = calls[0][0][3]
    repair_messages = calls[1][0][3]
    assert first_messages == messages
    assert all(message.get("role") != "tool" for message in repair_messages)
    assert "Validation error" in repair_messages[1]["content"]
    assert "verdict" in repair_messages[0]["content"]
    assert calls[0][1]["required_contract"]["contract_hash"] == "sealed-hash"
    assert "required_contract" not in calls[1][1]
    assert calls[1][1]["disable_tools"] is True
    assert any(event.get("event") == "log" for event in events)


@pytest.mark.asyncio
async def test_manager_schema_failure_returns_existing_blocked_gate_fallback():
    calls = []
    invalid = '{"severity":"critical","description":"missing verdict"}'

    class Orchestrator:
        AGENT_TIMEOUTS = {}
        AGENT_MAX_RETRIES = {"manager": 4}

        async def _call_agent(self, *args, **kwargs):
            calls.append((args, kwargs))
            return invalid

    events = []

    async def emit(**kwargs):
        events.append(kwargs)

    state = SimpleNamespace(session_id="manager-fallback", role_overrides={}, metadata={})
    runner = AgentRunner(Orchestrator(), state, emit, tracker=None)

    result = await runner.invoke(
        "manager",
        [{"role": "user", "content": "review"}],
    )

    payload = json.loads(result)
    assert payload["verdict"] == "BLOCKED"
    assert len(calls) == 2
    assert calls[1][1]["disable_tools"] is True
    blocked = [event for event in events if event.get("event") == "recovery_blocked"]
    assert blocked
    assert blocked[0]["status"] == "BLOCKED"
    assert blocked[0]["extra"]["safe_fallback"] == "MANAGER_BLOCKED"
    assert state.metadata["manager_schema_recovery"]["repair_used"] is True


@pytest.mark.asyncio
async def test_strategist_schema_repair_corrects_scope_before_plan_handoff():
    calls = []
    invalid = '```tasks\n[{"id":"T1","description":"Create the globe","write_scope":["src/App.tsx"]}]\n```'
    valid = '{"tasks":[{"id":"T1","description":"Create the globe","write_scope":["src/"]}]}'

    class Orchestrator:
        AGENT_TIMEOUTS = {}
        AGENT_MAX_RETRIES = {"strategist": 1}

        async def _call_agent(self, *args, **kwargs):
            calls.append((args, kwargs))
            return invalid if len(calls) == 1 else valid

    state = SimpleNamespace(session_id="strategist-scope-repair", role_overrides={}, metadata={})
    runner = AgentRunner(Orchestrator(), state, emit=None, tracker=None)
    result = await runner.invoke("strategist", [{"role": "user", "content": "plan the work"}])

    assert validate_agent_output("strategist", result).success
    assert len(calls) == 2
    assert calls[1][1]["disable_tools"] is True
    assert "workspace_root: true with write_scope: []" in calls[1][0][3][0]["content"]
    assert "(omitted; rebuild the compact plan from the decision context)" in calls[1][0][3][1]["content"]


def test_manager_schema_repair_uses_the_compact_contract():
    from council_of_agents.scripts.agent_runner import _schema_repair_messages

    repair = _schema_repair_messages([], "manager", "missing verdict", "not json")
    assert '"verdict":"APPROVED|REVISE|BLOCKED"' in repair[0]["content"]
    assert "properties" not in repair[0]["content"]


@pytest.mark.asyncio
async def test_runner_emits_liveness_during_a_silent_model_wait(monkeypatch):
    monkeypatch.setattr(AgentRunner, "LIVENESS_INTERVAL_SECONDS", 0.01)
    events = []

    class Orchestrator:
        AGENT_TIMEOUTS = {}
        AGENT_MAX_RETRIES = {"chair": 0}

        async def _call_agent(self, *args, **kwargs):
            await asyncio.sleep(0.025)
            return '{"complexity":"SIMPLE","route":"DIRECT","reason":"ok"}'

    async def emit(**event):
        events.append(event)

    state = SimpleNamespace(session_id="liveness", role_overrides={}, metadata={})
    await AgentRunner(Orchestrator(), state, emit, tracker=None).invoke(
        "chair", [{"role": "user", "content": "inspect"}], max_retries=0,
    )

    assert any(event.get("event") == "heartbeat" for event in events)


@pytest.mark.asyncio
async def test_runner_extends_active_timeout_on_real_progress():
    class Orchestrator:
        AGENT_TIMEOUTS = {"worker": 0.02}
        AGENT_HARD_TIMEOUTS = {"worker": 0.08}
        AGENT_MAX_RETRIES = {"worker": 0}

        async def _call_agent(self, *args, **kwargs):
            for _ in range(3):
                await asyncio.sleep(0.015)
                await kwargs["emit_cb"](
                    event="heartbeat", agent="worker", status="IN_PROGRESS",
                    text="thinking", extra={"phase": "model_thinking"},
                )
            return "done"

    state = SimpleNamespace(session_id="active-worker", role_overrides={}, metadata={})
    result = await AgentRunner(Orchestrator(), state, emit=None, tracker=None).invoke(
        "worker", [{"role": "user", "content": "plan"}], max_retries=0,
    )

    assert result == "done"


@pytest.mark.asyncio
async def test_runner_does_not_treat_heartbeats_as_model_progress(monkeypatch):
    monkeypatch.setattr(AgentRunner, "LIVENESS_INTERVAL_SECONDS", 0.005)
    events = []

    class Orchestrator:
        AGENT_TIMEOUTS = {"worker": 0.015}
        AGENT_HARD_TIMEOUTS = {"worker": 0.06}
        AGENT_MAX_RETRIES = {"worker": 0}

        async def _call_agent(self, *args, **kwargs):
            await asyncio.sleep(0.075)
            return "done"

    async def emit(**event):
        events.append(event)

    state = SimpleNamespace(session_id="silent-worker", role_overrides={}, metadata={})
    with pytest.raises(asyncio.TimeoutError, match="no complete model response"):
        await AgentRunner(Orchestrator(), state, emit, tracker=None).invoke(
            "worker", [{"role": "user", "content": "plan"}], max_retries=0,
        )

    assert any(event.get("event") == "heartbeat" for event in events)


@pytest.mark.asyncio
async def test_strategist_gets_hard_cap_before_first_model_output():
    class Orchestrator:
        AGENT_TIMEOUTS = {"strategist": 0.02}
        AGENT_HARD_TIMEOUTS = {"strategist": 0.08}
        AGENT_MAX_RETRIES = {"strategist": 0}

        async def _call_agent(self, *args, **kwargs):
            await asyncio.sleep(0.04)
            return '{"tasks":[{"id":"T1","description":"Create the globe","write_scope":["src/"]}]}'

    state = SimpleNamespace(session_id="strategist-first-response", role_overrides={}, metadata={})
    result = await AgentRunner(Orchestrator(), state, emit=None, tracker=None).invoke(
        "strategist", [{"role": "user", "content": "plan"}], max_retries=0,
    )

    assert validate_agent_output("strategist", result).success


@pytest.mark.asyncio
async def test_tool_output_starts_a_fresh_model_response_grace_period():
    class Orchestrator:
        AGENT_TIMEOUTS = {"strategist": 0.02}
        AGENT_HARD_TIMEOUTS = {"strategist": 0.08}
        AGENT_MAX_RETRIES = {"strategist": 0}

        async def _call_agent(self, *args, **kwargs):
            await asyncio.sleep(0.015)
            await kwargs["emit_cb"](event="tool_output", status="IN_PROGRESS", agent="strategist")
            await asyncio.sleep(0.04)
            return '{"tasks":[{"id":"T1","description":"Create the globe","write_scope":["src/"]}]}'

    state = SimpleNamespace(session_id="strategist-tool-response", role_overrides={}, metadata={})
    result = await AgentRunner(Orchestrator(), state, emit=None, tracker=None).invoke(
        "strategist", [{"role": "user", "content": "plan"}], max_retries=0,
    )

    assert validate_agent_output("strategist", result).success


@pytest.mark.asyncio
async def test_disable_tools_blocks_native_calls_without_executing_them(monkeypatch):
    import src.agent_loop as agent_loop

    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *args, **kwargs: 10, raising=False)
    executed = []

    async def fake_execute(block, *args, **kwargs):
        executed.append(block)
        return "bash", {"output": "should not run", "exit_code": 0}

    monkeypatch.setattr(agent_loop, "execute_tool_block", fake_execute, raising=False)
    calls = {"count": 0}

    async def fake_stream(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            yield 'data: {"type":"tool_calls","calls":[{"name":"bash","arguments":"{\\"command\\":\\"echo hi\\"}"}]}\n\n'
        else:
            yield 'data: {"delta":"done"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream, raising=False)
    chunks = [chunk async for chunk in agent_loop.stream_agent_loop(
        "https://api.openai.com/v1",
        "gpt-4o",
        [{"role": "user", "content": "repair the response"}],
        disable_tools=True,
        relevant_tools={"bash"},
        max_rounds=2,
    )]

    assert executed == []
    assert any('"delta":"done"' in chunk for chunk in chunks)
