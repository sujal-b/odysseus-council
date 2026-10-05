"""Regression: a native tool call whose arguments fail to convert must not be
silently dropped.

Production symptom (council run 874d9856): the implementer's write_file
arguments were cut off mid-JSON by the output token cap, so the round ended
with zero executable blocks. The model only saw an empty turn and re-emitted
the identical oversized call on every retry — three writes, three silent drops,
no file, and the run died on stagnation with a visible inspect-only loop.

The loop must instead tell the model exactly what happened and give it one
bounded chance to retry with a smaller payload.
"""

import asyncio
import json

import src.agent_loop as al


def _collect(gen):
    async def _run():
        return [c async for c in gen]
    return asyncio.run(_run())


def _types(chunks):
    out = []
    for c in chunks:
        if c.startswith("data: ") and not c.startswith("data: [DONE]"):
            try:
                out.append(json.loads(c[6:]))
            except Exception:
                pass
    return out


def _patch_common(monkeypatch, exec_calls):
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    # Workspace security policy can deny write_file/bash for the current owner;
    # these tests are about conversion, not permission, so make the denylist empty.
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner=None: set())

    async def _fake_exec(block, *a, **k):
        exec_calls.append(block)
        return ("write_file", {"output": "ok", "exit_code": 0})
    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)


# Real captured failure: the JSON string never closes (truncated at the cap).
_TRUNCATED_ARGS = '{"path": "./index.html", "content": "<!DOCTYPE html>\\n<html>'


def _run(monkeypatch, exec_calls, request_log, *, bad_call_rounds, max_rounds=4):
    """Yield a truncated native call for the first ``bad_call_rounds`` rounds."""
    async def _fake_stream(_candidates, messages, **kwargs):
        request_log.append([dict(m) for m in messages])
        if len(request_log) <= bad_call_rounds:
            yield ('data: ' + json.dumps({
                "type": "tool_calls",
                "calls": [{"id": f"c{len(request_log)}", "name": "write_file",
                           "arguments": _TRUNCATED_ARGS}],
            }) + "\n\n")
        else:
            yield 'data: {"delta":"File written."}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    return _types(_collect(al.stream_agent_loop(
        "https://api.openai.com/v1", "test-model",
        [{"role": "user", "content": "Create index.html in the workspace."}],
        max_rounds=max_rounds,
        relevant_tools={"write_file"},
    )))


def _dropped_nudges(messages):
    """The system message(s) the loop injects after a call fails to convert.

    Takes ONE request's message list. Nudges accumulate in `messages`, so a
    later request repeats every earlier nudge — count them per request.
    """
    return [m for m in messages
            if m.get("role") == "system" and "did NOT run" in str(m.get("content", ""))]


def test_truncated_native_call_triggers_a_retry_with_feedback(monkeypatch):
    exec_calls, request_log = [], []
    _run(monkeypatch, exec_calls, request_log, bad_call_rounds=1)

    # Round 1 dropped the call, so the model MUST get another turn.
    assert len(request_log) >= 2, "a dropped native call must not end the turn"
    # And the retry turn must carry the explanation, not just be an empty loop.
    nudges = [m for req in request_log[1:] for m in _dropped_nudges(req)]
    assert nudges, "the model was never told its call was dropped"
    feedback = nudges[0]["content"]
    assert "write_file" in feedback
    assert "truncated" in feedback or "malformed" in feedback
    assert "smaller" in feedback


def test_dropped_call_nudge_is_bounded(monkeypatch):
    """A model that can never form valid arguments must still terminate."""
    exec_calls, request_log = [], []
    events = _run(monkeypatch, exec_calls, request_log,
                  bad_call_rounds=99, max_rounds=5)

    # Initial round + at most two nudges, then the pre-existing break path.
    # The round cap bounds it regardless.
    assert 2 <= len(request_log) <= 4, f"nudges were not bounded: {len(request_log)} rounds"
    assert len(request_log) <= 5
    # The turn still finishes normally rather than hanging.
    assert any(e.get("type") == "metrics" for e in events)
    # Nudges accumulate in `messages`, so the final request holds them all:
    # exactly the cap (2) — one per retry round, then the pre-existing break.
    final_nudges = _dropped_nudges(request_log[-1])
    assert 1 <= len(final_nudges) <= 2, f"nudge cap violated: {len(final_nudges)}"


def test_successful_conversion_is_untouched(monkeypatch):
    """No nudge when the call converts — the common path must not regress."""
    exec_calls, request_log = [], []
    _patch_common(monkeypatch, exec_calls)

    async def _fake_stream(_candidates, messages, **kwargs):
        request_log.append([dict(m) for m in messages])
        yield ('data: ' + json.dumps({
            "type": "tool_calls",
            "calls": [{"id": "c1", "name": "write_file",
                       "arguments": json.dumps({"path": "index.html", "content": "hi"})}],
        }) + "\n\n")
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    _types(_collect(al.stream_agent_loop(
        "https://api.openai.com/v1", "test-model",
        [{"role": "user", "content": "Create index.html."}],
        max_rounds=3, relevant_tools={"write_file"},
    )))
    assert exec_calls, "the converted call must still execute"
    assert not any(_dropped_nudges(req) for req in request_log), "a successful call must not be nudged"
