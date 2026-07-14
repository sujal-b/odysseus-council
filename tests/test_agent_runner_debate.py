import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock
from council_of_agents.scripts.council_schemas import validate_agent_output, _extract_json, wrap_for_agent
from council_of_agents.scripts.council_retry import compute_backoff, classify_error, retry_with_backoff, SchemaValidationError, ErrorClass
from council_of_agents.scripts.council_doom_loop import DoomLoopDetector
from council_of_agents.scripts.debate_protocol import DebateProtocol
from council_of_agents.scripts.agent_runner import AgentRunner
from council_of_agents.scripts.context_tracker import (
    ContextBudgetExceededError, ContextTracker,
)
from src.model_context import estimate_tokens


def test_extract_json_fences():
    text = "Here is some markdown prefix.\n```json\n{\"complexity\": \"SIMPLE\", \"route\": \"DIRECT\"}\n```\nAnd suffix."
    data = _extract_json(text)
    assert data.get("complexity") == "SIMPLE"
    assert data.get("route") == "DIRECT"


def test_pydantic_schema_validation():
    text_ok = "```json\n{\"verdict\": \"APPROVED\", \"confidence\": 0.9, \"summary\": \"looks good\"}\n```"
    res_ok = validate_agent_output("manager", text_ok)
    assert res_ok.success is True
    assert res_ok.data["verdict"] == "APPROVED"
    assert res_ok.data["confidence"] == 0.9

    text_bad = "```json\n{\"verdict\": \"NOT_A_VERDICT\"}\n```"
    res_bad = validate_agent_output("manager", text_bad)
    assert res_bad.success is False
    assert "validation" in res_bad.error or "Value" in res_bad.error


def test_retry_backoff_classification():
    err_transient = ConnectionError("timeout")
    assert classify_error(err_transient) == ErrorClass.TRANSIENT

    err_throttled = RuntimeError("429 rate limit exceeded")
    assert classify_error(err_throttled) == ErrorClass.THROTTLING

    err_schema = SchemaValidationError("invalid output schema")
    assert classify_error(err_schema) == ErrorClass.SCHEMA

    err_budget = ContextBudgetExceededError("budget exhausted")
    assert classify_error(err_budget) == ErrorClass.TERMINAL


def test_retry_backoff_calculations():
    delay_transient_0 = compute_backoff(ErrorClass.TRANSIENT, 0)
    assert 0.0 <= delay_transient_0 <= 0.05

    delay_throttled_0 = compute_backoff(ErrorClass.THROTTLING, 0)
    assert 0.0 <= delay_throttled_0 <= 1.0


def test_doom_loop_detector():
    detector = DoomLoopDetector()
    detector.set_mode("pipeline")
    
    # Check output loop repetition
    err1 = detector.check_output_loop("strategist", "output A")
    assert err1 is None
    err2 = detector.check_output_loop("strategist", "output A")
    assert err2 is None
    err3 = detector.check_output_loop("strategist", "output A")
    # Consecutive repeat threshold is 2 in pipeline mode, so 3rd consecutive triggers it
    assert err3 is not None
    assert "produced identical output 3 times" in err3


def test_debate_protocol_convergence():
    debate = DebateProtocol(max_rounds=3, confidence_threshold=0.7)
    
    # First manager feedback confidence is high
    assert debate.should_continue(1, 0.8) is False
    # First manager feedback confidence is low
    assert debate.should_continue(1, 0.5) is True
    # At max rounds, must arbitrate
    assert debate.should_continue(3, 0.5) is False
    assert debate.needs_arbitration(3) is True


@pytest.mark.asyncio
async def test_agent_runner_budget_checks():
    orchestrator = MagicMock()
    state = MagicMock()
    state.session_id = "test_session"
    emit = AsyncMock()

    # Mock budget context tracker
    tracker = MagicMock()
    tracker.budget_tokens = 100
    tracker.over_budget.return_value = True

    runner = AgentRunner(orchestrator, state, emit, tracker)
    
    with pytest.raises(ContextBudgetExceededError, match="exceeded its token budget"):
        await runner.invoke("chair", [{"role": "user", "content": "hello"}])


@pytest.mark.asyncio
async def test_agent_runner_rejects_attempt_before_llm_call():
    orchestrator = MagicMock()
    orchestrator.AGENT_TIMEOUTS = {}
    orchestrator.AGENT_MAX_RETRIES = {"chair": 3}
    orchestrator._call_agent = AsyncMock()
    state = MagicMock(session_id="test_session", role_overrides={})
    tracker = ContextTracker("test_session", budget_tokens=1_000)
    runner = AgentRunner(orchestrator, state, emit=None, tracker=tracker)

    with pytest.raises(ContextBudgetExceededError, match="attempt blocked"):
        await runner.invoke("chair", [{"role": "user", "content": "hello"}])

    assert orchestrator._call_agent.call_count == 0
    assert tracker.reserved_tokens == 0


@pytest.mark.asyncio
async def test_agent_runner_counts_every_real_retry_attempt():
    valid = '{"complexity":"SIMPLE","route":"DIRECT","reason":"ok"}'
    orchestrator = MagicMock()
    orchestrator.AGENT_TIMEOUTS = {}
    orchestrator.AGENT_MAX_RETRIES = {"chair": 1}
    orchestrator._call_agent = AsyncMock(
        side_effect=[ConnectionError("temporary"), valid]
    )
    state = MagicMock(session_id="test_session", role_overrides={})
    tracker = ContextTracker("test_session", budget_tokens=10_000)
    runner = AgentRunner(orchestrator, state, emit=None, tracker=tracker)
    messages = [{"role": "user", "content": "hello"}]

    assert await runner.invoke("chair", messages) == valid

    summary = tracker.get_usage_summary()
    assert orchestrator._call_agent.call_count == 2
    assert summary["by_role"]["chair"]["calls"] == 2
    assert summary["by_role"]["chair"]["input_tokens"] == 2 * estimate_tokens(messages)
    assert tracker.reserved_tokens == 0


@pytest.mark.asyncio
async def test_parallel_agent_attempts_cannot_oversubscribe_budget():
    entered = asyncio.Event()
    release = asyncio.Event()
    valid = '{"complexity":"SIMPLE","route":"DIRECT","reason":"ok"}'

    async def held_call(*args, **kwargs):
        entered.set()
        await release.wait()
        return valid

    orchestrator = MagicMock()
    orchestrator.AGENT_TIMEOUTS = {}
    orchestrator.AGENT_MAX_RETRIES = {"chair": 0}
    orchestrator._call_agent = AsyncMock(side_effect=held_call)
    state = MagicMock(session_id="test_session", role_overrides={})
    tracker = ContextTracker("test_session", budget_tokens=1_500)
    runner = AgentRunner(orchestrator, state, emit=None, tracker=tracker)
    messages = [{"role": "user", "content": "hello"}]

    first = asyncio.create_task(runner.invoke("chair", messages))
    await entered.wait()
    with pytest.raises(ContextBudgetExceededError):
        await runner.invoke("chair", messages)
    release.set()
    assert await first == valid

    assert orchestrator._call_agent.call_count == 1
    assert tracker.reserved_tokens == 0


@pytest.mark.asyncio
async def test_agent_runner_retry_with_null_emit():
    # Verify that when self.emit is None, retries do not raise TypeError
    orchestrator = MagicMock()
    orchestrator._run_context_tracker = None
    state = MagicMock()
    state.session_id = "test_session"
    state.role_overrides = {}
    
    orchestrator.AGENT_TIMEOUTS = {}
    orchestrator.AGENT_MAX_RETRIES = {"chair": 2}
    
    # Mock _call_agent to raise a connection error
    orchestrator._call_agent = AsyncMock(side_effect=ConnectionError("Temporary network issue"))
    
    runner = AgentRunner(orchestrator, state, emit=None, tracker=None)
    
    # Since ConnectionError is transient, max_retries = 2. It should try once and retry twice (total 3 calls), and then raise the ConnectionError
    with pytest.raises(ConnectionError, match="Temporary network issue"):
        await runner.invoke("chair", [{"role": "user", "content": "hello"}])
        
    assert orchestrator._call_agent.call_count == 3


def test_raise_for_error_chunk():
    from src.agent_loop import raise_for_error_chunk
    
    # 1. Test parsing valid JSON error details
    chunk_json = 'event: error\ndata: {"error": "Rate limit exceeded", "status": 429}\n\n'
    with pytest.raises(RuntimeError, match="LLM streaming error: Rate limit exceeded"):
        raise_for_error_chunk(chunk_json)

    # 2. Test fallback when JSON lacks "error" but has "text"
    chunk_text = 'event: error\ndata: {"text": "Internal error"}\n\n'
    with pytest.raises(RuntimeError, match="LLM streaming error: Internal error"):
        raise_for_error_chunk(chunk_text)

    # 3. Test non-JSON error raw text
    chunk_raw = 'event: error\ndata: Something went wrong\n\n'
    with pytest.raises(RuntimeError, match="LLM streaming error: Something went wrong"):
        raise_for_error_chunk(chunk_raw)
