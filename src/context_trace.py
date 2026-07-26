"""Passive Council context tracing for development diagnostics.

The tracer observes the final provider payload without putting anything back
into the agent context or the user-facing event stream. It is disabled by
default and uses a bounded background writer so diagnostics cannot become a
new source of execution latency.

Environment:
    COUNCIL_CONTEXT_TRACE=off      disabled (default)
    COUNCIL_CONTEXT_TRACE=metrics  structure and size metadata only
    COUNCIL_CONTEXT_TRACE=full     metadata plus redacted payloads
    COUNCIL_CONTEXT_TRACE_DIR      optional output directory
    COUNCIL_CONTEXT_TRACE_MAX_MB  per-run archive limit (default 64)
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import threading
import time
from pathlib import Path
from typing import Any, Mapping


_SCHEMA_VERSION = 1
_QUEUE_LIMIT = 256
_DEFAULT_MAX_BYTES = 64 * 1024 * 1024
_queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=_QUEUE_LIMIT)
_writer_started = False
_writer_lock = threading.Lock()
_file_locks: dict[str, threading.Lock] = {}
_file_bytes: dict[str, int] = {}
_limit_reported: set[str] = set()
_dropped: dict[str, int] = {}
_drop_lock = threading.Lock()


_SECRET_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"(?i)(api[_-]?key\s*[:=]\s*)[^\s,;\"']+"),
    re.compile(r"(?i)(access[_-]?token\s*[:=]\s*)[^\s,;\"']+"),
    re.compile(r"(?i)(password\s*[:=]\s*)[^\s,;\"']+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
)


def trace_mode() -> str:
    mode = os.environ.get("COUNCIL_CONTEXT_TRACE", "off").strip().lower()
    return mode if mode in {"off", "metrics", "full"} else "off"


def enabled() -> bool:
    return trace_mode() != "off"


def _trace_id(context: Mapping[str, Any] | None) -> str:
    value = str((context or {}).get("run_id") or (context or {}).get("session_id") or "unknown")
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return safe[:160] or "unknown"


def _trace_path(trace_id: str) -> Path:
    configured = os.environ.get("COUNCIL_CONTEXT_TRACE_DIR", "").strip()
    if configured:
        return Path(configured) / f"{trace_id}.jsonl"
    from src.constants import DATA_DIR

    return Path(DATA_DIR) / "council_context_traces" / f"{trace_id}.jsonl"


def _max_bytes() -> int:
    try:
        value = int(float(os.environ.get("COUNCIL_CONTEXT_TRACE_MAX_MB", "64")) * 1024 * 1024)
    except (TypeError, ValueError):
        value = _DEFAULT_MAX_BYTES
    return max(1 * 1024 * 1024, min(value, 1024 * 1024 * 1024))


def _redact_text(value: str) -> str:
    result = value
    for pattern in _SECRET_PATTERNS:
        if pattern.pattern.startswith(r"\bsk-"):
            result = pattern.sub("[REDACTED_SECRET]", result)
        else:
            result = pattern.sub(lambda match: f"{match.group(1)}[REDACTED]", result)
    return result


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return [_redact(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _redact(item) for key, item in value.items()}
    return value


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _content_text(message: Mapping[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    return _stable_json(content)


def _message_manifest(messages: Any) -> list[dict[str, Any]]:
    if not isinstance(messages, list):
        return []
    manifest = []
    for index, message in enumerate(messages):
        if not isinstance(message, Mapping):
            text = str(message)
            manifest.append({
                "index": index,
                "role": "unknown",
                "chars": len(text),
                "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            })
            continue
        text = _content_text(message)
        manifest.append({
            "index": index,
            "role": str(message.get("role") or "unknown"),
            "source": str(message.get("_trace_source") or "unknown"),
            "chars": len(text),
            "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        })
    return manifest


def _ensure_writer() -> None:
    global _writer_started
    if _writer_started:
        return
    with _writer_lock:
        if _writer_started:
            return
        thread = threading.Thread(target=_writer_loop, name="council-context-trace", daemon=True)
        thread.start()
        _writer_started = True


def _writer_loop() -> None:
    while True:
        record = _queue.get()
        try:
            if record is None:
                return
            trace_id = str(record.get("run_id") or "unknown")
            path = _trace_path(trace_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            line = _stable_json(record) + "\n"
            encoded = line.encode("utf-8")
            current = _file_bytes.get(trace_id, 0)
            if current + len(encoded) > _max_bytes():
                if trace_id not in _limit_reported:
                    _limit_reported.add(trace_id)
                    _write_record({
                        "schema_version": _SCHEMA_VERSION,
                        "kind": "trace_limit_reached",
                        "timestamp": time.time(),
                        "run_id": trace_id,
                        "max_bytes": _max_bytes(),
                    })
                continue
            _write_record(record, encoded=encoded)
            _file_bytes[trace_id] = current + len(encoded)
        except Exception:
            # Diagnostics are fail-open. The Council run must not fail because
            # a trace directory is unavailable or a record is malformed.
            pass
        finally:
            _queue.task_done()


def _write_record(record: dict[str, Any], encoded: bytes | None = None) -> None:
    trace_id = str(record.get("run_id") or "unknown")
    path = _trace_path(trace_id)
    lock = _file_locks.setdefault(trace_id, threading.Lock())
    with lock:
        with path.open("ab") as handle:
            handle.write(encoded if encoded is not None else (_stable_json(record) + "\n").encode("utf-8"))


def _enqueue(record: dict[str, Any]) -> None:
    _ensure_writer()
    try:
        _queue.put_nowait(record)
    except queue.Full:
        trace_id = str(record.get("run_id") or "unknown")
        with _drop_lock:
            _dropped[trace_id] = _dropped.get(trace_id, 0) + 1


def record_model_request(
    context: Mapping[str, Any] | None,
    *,
    provider: str,
    endpoint: str,
    model: str,
    payload: Any,
    attempt: int = 1,
) -> None:
    """Queue the exact final provider payload for an opted-in Council run."""
    mode = trace_mode()
    if mode == "off" or not isinstance(context, Mapping):
        return
    payload_json = _stable_json(payload)
    messages = payload.get("messages") if isinstance(payload, Mapping) else None
    record: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "kind": "model_request",
        "timestamp": time.time(),
        "run_id": _trace_id(context),
        "session_id": context.get("session_id"),
        "agent": context.get("agent"),
        "round": context.get("round"),
        "route": context.get("route"),
        "provider": provider,
        "endpoint_host": re.sub(r"^https?://([^/]+).*$", r"\1", str(endpoint)),
        "model": model,
        "attempt": attempt,
        "payload_chars": len(payload_json),
        "payload_hash": hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
        "message_count": len(messages) if isinstance(messages, list) else 0,
        "messages": _message_manifest(messages),
        "dropped_before_write": _dropped.get(_trace_id(context), 0),
    }
    if mode == "full":
        record["payload"] = _redact(payload)
    _enqueue(record)


def record_model_response(
    context: Mapping[str, Any] | None,
    *,
    provider: str,
    endpoint: str,
    model: str,
    output: str = "",
    status: str = "completed",
    error: str = "",
    attempt: int = 1,
) -> None:
    """Queue bounded metadata for the provider response.

    Response tracing is deliberately separate from request tracing so a
    provider failure is visible even when it produces no assistant text. Raw
    output is only retained in ``full`` mode and is redacted before enqueueing.
    """
    mode = trace_mode()
    if mode == "off" or not isinstance(context, Mapping):
        return
    text = str(output or "")
    safe_error = _redact_text(str(error or ""))[:2000]
    record: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "kind": "model_response",
        "timestamp": time.time(),
        "run_id": _trace_id(context),
        "session_id": context.get("session_id"),
        "agent": context.get("agent"),
        "round": context.get("round"),
        "route": context.get("route"),
        "provider": provider,
        "endpoint_host": re.sub(r"^https?://([^/]+).*$", r"\1", str(endpoint)),
        "model": model,
        "attempt": attempt,
        "status": status,
        "output_chars": len(text),
        "output_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "error": safe_error,
        "dropped_before_write": _dropped.get(_trace_id(context), 0),
    }
    if mode == "full":
        record["output"] = _redact_text(text)
    _enqueue(record)


def record_handoff(
    context: Mapping[str, Any] | None,
    *,
    from_agent: str,
    content: str,
    to_agent: str | None = None,
    handoff_mode: str = "contract",
) -> None:
    """Queue an agent-to-agent handoff without touching the live prompt."""
    if trace_mode() == "off" or not isinstance(context, Mapping):
        return
    text = str(content or "")
    record: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "kind": "agent_handoff",
        "timestamp": time.time(),
        "run_id": _trace_id(context),
        "session_id": context.get("session_id"),
        "from_agent": from_agent,
        "to_agent": to_agent,
        "handoff_mode": handoff_mode,
        "chars": len(text),
        "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }
    if trace_mode() == "full":
        record["content"] = _redact_text(text)
    _enqueue(record)


def record_scope_violation(
    context: Mapping[str, Any] | None,
    *,
    task_id: str = "",
    tool_type: str,
    attempted_path: str = "",
    content: str = "",
    category: str = "scope_violation",
    reason: str = "",
) -> None:
    """Record a blocked workspace mutation without changing Council control flow."""
    if trace_mode() == "off" or not isinstance(context, Mapping):
        return
    payload = str(content or "")
    record: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "kind": "scope_violation",
        "timestamp": time.time(),
        "run_id": _trace_id(context),
        "session_id": context.get("session_id"),
        "agent": context.get("agent"),
        "round": context.get("round"),
        "task_id": task_id or None,
        "tool_type": str(tool_type or ""),
        "attempted_path": str(attempted_path or ""),
        "category": str(category or "scope_violation"),
        "content_chars": len(payload),
        "content_hash": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    }
    if trace_mode() == "full":
        record["reason"] = _redact_text(str(reason or ""))
    _enqueue(record)


def record_run_terminal(
    context: Mapping[str, Any] | None,
    *,
    status: str,
    reason: str = "",
) -> None:
    """Record the final Council state so a trace has a provable end condition."""
    if trace_mode() == "off" or not isinstance(context, Mapping):
        return
    safe_reason = _redact_text(str(reason or ""))[:2000]
    record: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "kind": "run_terminal",
        "timestamp": time.time(),
        "run_id": _trace_id(context),
        "session_id": context.get("session_id"),
        "status": str(status or "UNKNOWN"),
        "reason_chars": len(safe_reason),
        "reason_hash": hashlib.sha256(safe_reason.encode("utf-8")).hexdigest(),
    }
    if trace_mode() == "full":
        record["reason"] = safe_reason
    _enqueue(record)


def shutdown() -> None:
    """Best-effort drain hook for controlled process shutdowns."""
    if _writer_started:
        try:
            _queue.join()
        except Exception:
            pass
