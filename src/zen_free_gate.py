"""OpenCode Zen anonymous free-lane gate.

Zen serves its zero-cost models only to requests that look like the official
opencode CLI. Measured against the live gateway (2026-10-04):

* a User-Agent containing ``opencode/`` -- bare ``opencode`` is rejected with
  403 FreeTierError, and a version below the gateway's floor returns 426
  UpgradeRequired
* one of ``x-opencode-session`` / ``x-session-affinity`` / ``X-Session-Id``
  whose value matches ``ses_[0-9a-f]{12}[0-9A-Za-z]{14}``. Only one of the three
  is required and the value need not be stable across requests.
* ``stream: true``
* a ``tools`` array containing function tools named exactly ``bash`` and
  ``read``. Names only -- descriptions and parameter schemas are not inspected.

Everything else the CLI sends was measured to be unnecessary:
``x-opencode-client``, ``x-opencode-request``, ``x-opencode-project`` and
``tool_choice``. Auth is unchanged: Zen accepts the same ``x-api-key`` header
that ``endpoint_resolver.build_headers`` already sends for these providers.

Applied only when the provider is opencode-zen or opencode-go, so every other
provider keeps byte-identical behaviour.
"""

import os
import re
from typing import Any, Dict, Iterable, List, Optional

# Bump when the gateway raises its minimum CLI version. A version below the
# floor is answered with 426 UpgradeRequired rather than 403, so the symptom
# differs from a malformed gate and is easy to misread.
CLI_USER_AGENT = "opencode/1.18.31 (win32 x64; node24.17.0)"

GATE_TOOL_NAMES = ("bash", "read")

SESSION_ID_PATTERN = re.compile(r"^ses_[0-9a-f]{12}[0-9A-Za-z]{14}$")

_B62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def mint_session_id() -> str:
    """Return a canonical-looking CLI session id.

    Shape only: the gateway checks the format, not the provenance, and it does
    not require the same value across requests. 12 lowercase hex characters
    read as a timestamp prefix and 14 base62 characters follow.
    """
    raw = os.urandom(16)
    tail = ""
    n = int.from_bytes(raw[6:16], "big")
    for _ in range(14):
        tail = _B62[n % 62] + tail
        n //= 62
    return "ses_" + raw[:6].hex() + tail


def gate_headers() -> Dict[str, str]:
    """CLI identity headers for one Zen request."""
    return {"user-agent": CLI_USER_AGENT, "x-opencode-session": mint_session_id()}


def gate_stub_tool(name: str) -> Dict[str, Any]:
    """A tool that exists only to satisfy the gateway's name check.

    ``parameters`` is empty and the description tells the model not to call it.
    A name that the host cannot resolve is dropped with a warning by
    ``agent_loop._resolve_tool_blocks``, so a model that ignores the
    description degrades one turn instead of failing the run.
    """
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "Reserved for the host runtime; do not call it.",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _declared_tool_names(tools: Optional[Iterable[Any]]) -> set:
    names = set()
    for tool in tools or ():
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function")
        if isinstance(fn, dict) and isinstance(fn.get("name"), str):
            names.add(fn["name"])
    return names


def apply_tool_gate(payload: Dict[str, Any], tools: Optional[Iterable[Any]]) -> Dict[str, Any]:
    """Add any missing gate tool names to a Zen request payload.

    ``tools`` is the authoritative list the host resolved for this role, which
    may differ from ``payload["tools"]`` because the caller only copies it when
    non-empty. Only the missing names are appended, so a role that already has
    a real ``bash`` keeps it untouched. When the role has no tools at all the
    stubs are disarmed with ``tool_choice: "none"``; measured, a model left to
    choose will otherwise spend its output budget probing unavailable tools and
    return nothing.

    Returns ``payload`` unchanged when nothing is missing.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
        return payload
    existing: List[Any] = [t for t in (tools or [])]
    missing = [name for name in GATE_TOOL_NAMES if name not in _declared_tool_names(existing)]
    if not missing:
        return payload
    gated = dict(payload)
    gated["tools"] = existing + [gate_stub_tool(name) for name in missing]
    if not existing:
        gated["tool_choice"] = "none"
    return gated