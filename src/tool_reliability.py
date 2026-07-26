"""Small, deterministic reliability helpers for tool execution.

The agent still owns semantic recovery (for example, correcting a bad
regular expression).  This module owns only facts the runtime can determine
without another model call: failure classification, duplicate fingerprints,
and bounded diagnostics.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict


READ_ONLY_TOOLS = frozenset({"ls", "glob", "grep", "read_file", "bash", "python"})
TOOL_OUTPUT_LIMITS = {
    "ls": 8_000,
    "glob": 12_000,
    "grep": 16_000,
    "read_file": 32_000,
    "bash": 12_000,
    "python": 12_000,
}


def tool_call_fingerprint(tool: str, content: str) -> str:
    """Return a stable per-turn fingerprint without exposing raw arguments."""
    normalized = " ".join(str(content or "").split())
    payload = f"{tool.strip().lower()}\0{normalized}"
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:20]


def classify_tool_failure(tool: str, result: Dict[str, Any]) -> tuple[str, bool, str]:
    """Return ``(kind, retryable, remediation)`` for a failed result."""
    error = str(result.get("error") or result.get("output") or "").strip()
    lowered = error.casefold()
    exit_code = result.get("exit_code")

    if result.get("blocked") or "forbidden" in lowered or "disabled" in lowered:
        return "POLICY_BLOCKED", False, "Use an allowed purpose-built tool or request approval."
    if result.get("failure_kind") == "STALE_VERSION" or "stale" in lowered or "hash mismatch" in lowered:
        return "STALE_VERSION", False, "Re-read the file and issue a new range request using its latest hash."
    if "permission" in lowered or "access is denied" in lowered or "access denied" in lowered:
        return "PERMISSION", False, "Use the active workspace or wait for the permission decision."
    if exit_code == 124 or "timed out" in lowered or "timeout" in lowered:
        return "TIMEOUT", tool in READ_ONLY_TOOLS, "Retry once only if the operation is read-only and idempotent."
    if any(marker in lowered for marker in (
        "path is required", "pattern is required", "invalid argument", "malformed",
    )):
        return "INVALID_ARGUMENT", True, "Correct the tool arguments before retrying."
    if any(marker in lowered for marker in (
        "not recognized", "command not found", "no such file or directory",
    )):
        return "COMMAND_NOT_FOUND", tool in READ_ONLY_TOOLS, "Use the platform-aware dedicated tool or correct the executable."
    if "syntaxerror" in lowered or "syntax error" in lowered:
        return "SYNTAX_ERROR", True, "Use the diagnostic text to revise the command or code."
    return "EXECUTION_ERROR", tool in READ_ONLY_TOOLS, "Inspect the diagnostic and choose a materially different approach."


def annotate_tool_result(tool: str, result: Any, *, attempt_id: str) -> Dict[str, Any]:
    """Normalize a tool result into a bounded, machine-actionable contract."""
    normalized: Dict[str, Any] = dict(result) if isinstance(result, dict) else {
        "output": str(result or "")
    }
    _bound_result_text(tool, normalized)
    normalized["attempt_id"] = attempt_id
    exit_code = normalized.get("exit_code")
    ok = exit_code in (0, None) and not normalized.get("error")
    normalized["ok"] = bool(ok)
    if ok:
        return normalized

    kind, retryable, remediation = classify_tool_failure(tool, normalized)
    normalized["failure_kind"] = kind
    normalized["retryable"] = retryable
    normalized["remediation"] = remediation
    normalized["fingerprint"] = tool_call_fingerprint(
        tool,
        str(normalized.get("error") or normalized.get("output") or ""),
    )
    # This is deliberately short. Full stdout/stderr remains in diagnostics
    # and is not duplicated into the normal event payload.
    normalized["diagnostic"] = re.sub(r"\s+", " ", str(
        normalized.get("error") or normalized.get("output") or "tool failed"
    )).strip()[:240]
    return normalized


def _bound_result_text(tool: str, result: Dict[str, Any]) -> None:
    """Apply a per-tool output cap while retaining a diagnostic tail."""
    limit = TOOL_OUTPUT_LIMITS.get(tool)
    if not limit:
        return
    for key in ("output", "stdout", "stderr", "content", "results"):
        value = result.get(key)
        if not isinstance(value, str) or len(value) <= limit:
            continue
        head = int(limit * 0.72)
        omitted = len(value) - limit
        result[key] = (
            value[:head]
            + f"\n... [{omitted} chars omitted by {tool} context budget] ...\n"
            + value[-(limit - head):]
        )
        result["truncated"] = True
        result["total_output_chars"] = len(value)


def duplicate_tool_result(tool: str, *, prior_fingerprint: str, attempt_id: str) -> Dict[str, Any]:
    """Build the result returned when an exact failed call repeats in one turn."""
    return {
        "attempt_id": attempt_id,
        "ok": False,
        "exit_code": 1,
        "error": "The identical failed tool call was suppressed; revise its arguments or approach.",
        "failure_kind": "DUPLICATE_SUPPRESSED",
        "retryable": True,
        "remediation": "Change the command or use the dedicated tool suggested by the previous diagnostic.",
        "fingerprint": prior_fingerprint,
        "diagnostic": "identical failed call suppressed",
        "tool": tool,
    }


def compact_tool_metadata(result: Dict[str, Any]) -> Dict[str, Any]:
    """Return only safe, small metadata for event consumers."""
    return {
        key: result[key]
        for key in (
            "attempt_id", "ok", "failure_kind", "retryable", "remediation",
            "fingerprint", "read_mode", "total_lines", "total_chars", "file_hash",
            "returned_lines", "next_offset", "truncated",
        )
        if key in result
    }
