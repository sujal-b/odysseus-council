import json


def _read_trace(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_trace_is_disabled_by_default(monkeypatch, tmp_path):
    from src import context_trace

    monkeypatch.delenv("COUNCIL_CONTEXT_TRACE", raising=False)
    monkeypatch.setenv("COUNCIL_CONTEXT_TRACE_DIR", str(tmp_path))
    context_trace.record_model_request(
        {"run_id": "disabled", "session_id": "s", "agent": "chair"},
        provider="openai",
        endpoint="https://example.test/v1/chat/completions",
        model="test",
        payload={"messages": [{"role": "user", "content": "secret"}]},
    )
    assert not list(tmp_path.glob("*"))


def test_full_trace_captures_payload_and_handoff_without_secrets(monkeypatch, tmp_path):
    from src import context_trace

    monkeypatch.setenv("COUNCIL_CONTEXT_TRACE", "full")
    monkeypatch.setenv("COUNCIL_CONTEXT_TRACE_DIR", str(tmp_path))
    run = {"run_id": "run-1", "session_id": "s", "agent": "strategist", "round": 1}
    context_trace.record_model_request(
        run,
        provider="openai",
        endpoint="https://example.test/v1/chat/completions",
        model="test-model",
        payload={
            "messages": [
                {"role": "system", "content": "Use api_key=sk-12345678901234567890"},
                {"role": "user", "content": "Build the project"},
            ]
        },
    )
    context_trace.record_handoff(
        run,
        from_agent="chair",
        to_agent="strategist",
        content="Bearer abcdefghijklmnop123456",
    )
    context_trace.shutdown()

    records = _read_trace(tmp_path / "run-1.jsonl")
    assert [record["kind"] for record in records] == ["model_request", "agent_handoff"]
    request = records[0]
    assert request["message_count"] == 2
    assert request["messages"][0]["chars"] > 0
    assert "sk-12345678901234567890" not in json.dumps(request)
    assert "[REDACTED" in json.dumps(request)
    assert "Bearer abcdefghijklmnop123456" not in json.dumps(records[1])


def test_metrics_trace_omits_payload_content(monkeypatch, tmp_path):
    from src import context_trace

    monkeypatch.setenv("COUNCIL_CONTEXT_TRACE", "metrics")
    monkeypatch.setenv("COUNCIL_CONTEXT_TRACE_DIR", str(tmp_path))
    context_trace.record_model_request(
        {"run_id": "metrics", "session_id": "s", "agent": "manager"},
        provider="openai",
        endpoint="https://example.test/v1/chat/completions",
        model="test-model",
        payload={"messages": [{"role": "user", "content": "do not archive this"}]},
    )
    context_trace.shutdown()

    record = _read_trace(tmp_path / "metrics.jsonl")[0]
    assert "payload" not in record
    assert record["payload_chars"] > 0
    assert record["messages"][0]["content_hash"]


def test_full_trace_captures_model_response_and_metrics_omit_output(monkeypatch, tmp_path):
    from src import context_trace

    monkeypatch.setenv("COUNCIL_CONTEXT_TRACE", "full")
    monkeypatch.setenv("COUNCIL_CONTEXT_TRACE_DIR", str(tmp_path))
    context_trace.record_model_response(
        {"run_id": "response", "session_id": "s", "agent": "manager"},
        provider="openai",
        endpoint="https://example.test/v1/chat/completions",
        model="test-model",
        output='{"verdict":"BLOCKED","token":"sk-12345678901234567890"}',
        status="completed",
    )
    context_trace.shutdown()
    record = _read_trace(tmp_path / "response.jsonl")[0]
    assert record["kind"] == "model_response"
    assert record["status"] == "completed"
    assert "sk-12345678901234567890" not in json.dumps(record)
    assert "output" in record

    metrics_dir = tmp_path / "metrics"
    metrics_dir.mkdir()
    monkeypatch.setenv("COUNCIL_CONTEXT_TRACE", "metrics")
    monkeypatch.setenv("COUNCIL_CONTEXT_TRACE_DIR", str(metrics_dir))
    context_trace.record_model_response(
        {"run_id": "response-metrics", "session_id": "s", "agent": "manager"},
        provider="openai",
        endpoint="https://example.test/v1/chat/completions",
        model="test-model",
        output="private response",
    )
    context_trace.shutdown()
    metrics = _read_trace(metrics_dir / "response-metrics.jsonl")[0]
    assert "output" not in metrics
    assert metrics["output_chars"] == len("private response")


def test_scope_violation_trace_records_intent_without_raw_write_content(monkeypatch, tmp_path):
    from src import context_trace

    monkeypatch.setenv("COUNCIL_CONTEXT_TRACE", "full")
    monkeypatch.setenv("COUNCIL_CONTEXT_TRACE_DIR", str(tmp_path))
    context_trace.record_scope_violation(
        {"run_id": "scope", "session_id": "s", "agent": "implementer", "round": 2},
        task_id="T1",
        tool_type="write_file",
        attempted_path="output.txt",
        content="output.txt\napi_key=sk-12345678901234567890",
        category="scope_violation",
        reason="write target is outside declared scope",
    )
    context_trace.shutdown()

    record = _read_trace(tmp_path / "scope.jsonl")[0]
    assert record["kind"] == "scope_violation"
    assert record["task_id"] == "T1"
    assert record["attempted_path"] == "output.txt"
    assert record["content_hash"]
    assert "content" not in record
    assert "sk-12345678901234567890" not in str(record)


def test_terminal_trace_records_the_final_status(monkeypatch, tmp_path):
    from src import context_trace

    monkeypatch.setenv("COUNCIL_CONTEXT_TRACE", "full")
    monkeypatch.setenv("COUNCIL_CONTEXT_TRACE_DIR", str(tmp_path))
    context_trace.record_run_terminal(
        {"run_id": "terminal", "session_id": "s"}, status="FAILED", reason="tool timeout"
    )
    context_trace.shutdown()

    record = _read_trace(tmp_path / "terminal.jsonl")[0]
    assert record["kind"] == "run_terminal"
    assert record["status"] == "FAILED"
    assert record["reason"] == "tool timeout"
