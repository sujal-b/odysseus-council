from __future__ import annotations
import collections
import copy
"""Thin, trace-driven evaluation for Council roles.

This is the primary provider-readiness harness. It exercises role prompts and
contract handoffs without creating a workspace or invoking tools. The
disposable workspace gauntlet remains a secondary integration suite.
"""


import argparse
import traceback
import asyncio
import difflib
import hashlib
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from council_of_agents.scripts.agent_runner import _schema_repair_messages
from council_of_agents.scripts.context_envelope import build_context_envelope
from council_of_agents.scripts.council_schemas import (
    SCHEMA_MAP,
    build_response_format,
    compact_agent_contract,
    validate_agent_output,
)
from council_of_agents.scripts.prompt_composer import PromptComposer
from src.context_trace import _redact_text
from src.endpoint_resolver import build_headers
from src.llm_core import llm_call_async


ROLE_ORDER = (
    "chair",
    "strategist",
    "perspective_analyzer",
    "manager",
    "implementer",
    "completeness_auditor",
)
_ALIASES = {
    "perspective": "perspective_analyzer",
    "completeness": "completeness_auditor",
}
_GARBAGE = {
    "ok", "okay", "done", "complete", "completed", "n/a", "na", "null",
    "no response", "empty response", "the model returned an empty response.",
}
_REQUIRED_TOP_LEVEL_KEYS = {
    "perspective_analyzer": {"security", "performance", "maintainability", "overall_score", "synthesis"},
    "completeness_auditor": {"completeness", "done", "criteria"},
}
_HANDOFF_MODES = {"contract", "full"}
_HUMAN_REVIEW_FIELDS = ("plan_quality", "manager_correctness", "handoff_quality")
_HUMAN_REVIEW_LABELS = {
    "low": 1,
    "poor": 1,
    "incorrect": 1,
    "unsound": 1,
    "mixed": 3,
    "partial": 3,
    "partially_correct": 3,
    "high": 5,
    "correct": 5,
    "sound": 5,
    "excellent": 5,
}
_PROVIDER_RETRY_LIMIT = 1
# The controlled harness owns the visible retry budget. Keep the lower-level
# HTTP client to one request per harness attempt so provider retries cannot be
# hidden inside a single recorded attempt.
_PROVIDER_CALL_MAX_RETRIES = 1
# Bound each provider attempt so an unavailable endpoint becomes an explicit
# provider failure instead of holding a suite for the client's 300s stream timeout.
_PROVIDER_CALL_TIMEOUT = 30
# Phase A uses a compositional trace budget. Keep this override only for
# focused tests; production derives the deadline from the topology below.
_TRACE_TIMEOUT = None
_TRACE_MARGIN_SECONDS = 60
_PLANNING_BASE_STAGES = 4
_FULL_EXECUTION_BASE_STAGES = 6
_REVISION_STAGES = 3
_MAX_SCHEMA_REPAIRS = 1


class RoleEvalError(ValueError):
    pass

class EvalProviderError(RoleEvalError):
    """A known provider failure at the evaluator trust boundary."""

    def __init__(self, message: str, *, original: Exception | None = None):
        super().__init__(message)
        self.original = original


class EvalHarnessError(RoleEvalError):

    """An evaluator, manifest, serialization, or internal invariant failure."""


def _max_provider_calls_per_stage() -> int:
    return (_PROVIDER_RETRY_LIMIT + 1) * (_MAX_SCHEMA_REPAIRS + 1)


def _trace_stage_count(max_plan_revisions: int, planning_only: bool) -> int:
    base = _PLANNING_BASE_STAGES if planning_only else _FULL_EXECUTION_BASE_STAGES
    return base + (_REVISION_STAGES * max(0, int(max_plan_revisions)))


def _effective_max_timeout(role_configs: dict | None = None) -> float:
    if not role_configs:
        return float(_PROVIDER_CALL_TIMEOUT)
    timeouts = []
    for config in role_configs.values():
        if isinstance(config, dict):
            value = config.get("timeout", _PROVIDER_CALL_TIMEOUT)
        else:
            value = getattr(config, "timeout", _PROVIDER_CALL_TIMEOUT)
        try:
            timeout = float(value)
        except (TypeError, ValueError):
            timeout = float(_PROVIDER_CALL_TIMEOUT)
        if timeout > 0:
            timeouts.append(timeout)
    return max(timeouts, default=float(_PROVIDER_CALL_TIMEOUT))


def _trace_budget_seconds(
    max_plan_revisions: int,
    planning_only: bool,
    role_configs: dict | None = None,
) -> float:
    if _TRACE_TIMEOUT is not None:
        return float(_TRACE_TIMEOUT)
    return float(
        _trace_stage_count(max_plan_revisions, planning_only)
        * _max_provider_calls_per_stage()
        * _effective_max_timeout(role_configs)
        + _TRACE_MARGIN_SECONDS
    )


def _trace_budget_formula(
    planning_only: bool = True,
    role_configs: dict | None = None,
) -> str:
    base = _PLANNING_BASE_STAGES if planning_only else _FULL_EXECUTION_BASE_STAGES
    per_stage_budget = _max_provider_calls_per_stage() * _effective_max_timeout(role_configs)
    return f"({base} + {_REVISION_STAGES} × max_revisions) × {per_stage_budget:g} + {_TRACE_MARGIN_SECONDS:g}"


def _role(agent: str) -> str:
    value = _ALIASES.get(str(agent).strip().lower(), str(agent).strip().lower())
    if value not in ROLE_ORDER:
        raise RoleEvalError(f"Unsupported Council role: {agent}")
    return value


def _text(value) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, default=str)


def _jsonable(value):
    return json.loads(json.dumps(value, ensure_ascii=False, default=lambda item: getattr(item, "value", str(item))))


def _benchmark_fingerprint(scenarios: dict) -> str:
    """Return a stable, non-sensitive identity for the fixed scenario suite."""
    canonical = json.dumps(
        _jsonable(scenarios),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()[:16]


def _endpoint_label(endpoint: str) -> str:
    parsed = urlsplit(str(endpoint or ""))
    if parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    return str(endpoint or "")


def _resolve_db_api_key(endpoint_url: str) -> str:
    """Resolve an API key for an endpoint URL from the configured database
    endpoints — the same lookup the UI orchestrator uses via _resolve_headers().
    Returns empty string if no matching enabled endpoint is found."""
    if not endpoint_url:
        return ""
    try:
        from src.endpoint_resolver import normalize_base, resolve_endpoint_runtime
        from core.database import SessionLocal, ModelEndpoint

        base = normalize_base(endpoint_url)
        db = SessionLocal()
        try:
            for ep in db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True).all():
                if not ep.base_url:
                    continue
                if normalize_base(ep.base_url) == base:
                    _, key = resolve_endpoint_runtime(ep)
                    return str(key or "")
        finally:
            db.close()
    except Exception:
        return ""
    return ""


def _resolve_api_key_for_endpoints(endpoints: set[str]) -> str:
    """Return the first API key found for a set of endpoint URLs, matching
    against the DB just like the orchestrator does per-role. Returns empty
    string when no endpoint resolves."""
    for ep_url in sorted(endpoints):
        key = _resolve_db_api_key(ep_url)
        if key:
            return key
    return ""


def _configured_api_key(endpoint: str = "") -> str:
    """Resolve a configured endpoint credential without exposing it.
    When endpoint is empty, queries all enabled DB endpoints as fallback."""
    try:
        from src.endpoint_resolver import (
            build_chat_url,
            normalize_base,
            resolve_endpoint,
            resolve_endpoint_runtime,
        )

        # First pass: try DB lookup for the specific endpoint
        if endpoint:
            key = _resolve_db_api_key(endpoint)
            if key:
                return key

        # Second pass: fall back to resolve_endpoint("default") headers
        configured_url, _model, headers = resolve_endpoint("default")
        if not endpoint:
            # No specific endpoint — check the default endpoint's headers,
            # but also fall through to DB iteration below if nothing found
            if isinstance(headers, dict):
                for name in ("Authorization", "x-api-key", "api-key"):
                    value = str(headers.get(name) or "").strip()
                    if value:
                        return value[7:].strip() if value.lower().startswith("bearer ") else value
            # Try all enabled DB endpoints as last resort
            key = _resolve_api_key_for_endpoints(set())
            if not key:
                from core.database import SessionLocal, ModelEndpoint
                db = SessionLocal()
                try:
                    for row in db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True).all():
                        try:
                            _, key = resolve_endpoint_runtime(row)
                        except Exception:
                            continue
                        if key:
                            return str(key)
                finally:
                    db.close()
            return ""

        candidates = []
        if configured_url and normalize_base(endpoint) == normalize_base(configured_url):
            candidates.append(headers if isinstance(headers, dict) else {})
        if not candidates:
            from core.database import ModelEndpoint, SessionLocal
            db = SessionLocal()
            try:
                for row in db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True).all():
                    try:
                        base, key = resolve_endpoint_runtime(row)
                    except Exception:
                        continue
                    if key and (
                        normalize_base(endpoint) == normalize_base(base)
                        or normalize_base(endpoint) == normalize_base(build_chat_url(base))
                    ):
                        return str(key)
            finally:
                db.close()
        for candidate in candidates:
            for name in ("Authorization", "x-api-key", "api-key"):
                value = str(candidate.get(name) or "").strip()
                if value:
                    return value[7:].strip() if value.lower().startswith("bearer ") else value
    except Exception:
        return ""
    return ""


def _user_message(user_prompt: str, workspace: str = "") -> str:
    envelope = build_context_envelope(workspace=workspace)
    return f"{envelope}\n\n{user_prompt}" if envelope else user_prompt


def _chair_contract(reply: str) -> str:
    validation = validate_agent_output("chair", reply, strict=True)
    if not validation.success or not validation.data:
        raise RoleEvalError(f"Chair result is not valid JSON: {validation.error}")
    data = validation.data
    value = lambda key: getattr(data[key], "value", data[key])
    return "\n".join([
        "## Chair decision",
        f"- complexity: {value('complexity')}",
        f"- route: {value('route')}",
        f"- action: {value('action')}",
        f"- target: {data['target']}",
        f"- brief: {data['reason']}",
    ])


def _normalize_handoff_mode(mode: str) -> str:
    value = str(mode or "contract").strip().lower()
    if value not in _HANDOFF_MODES:
        raise RoleEvalError(f"Unsupported handoff mode: {mode}")
    return value


def _prompt_composer(prompts_dir: str | Path | None = None) -> PromptComposer:
    return PromptComposer(Path(prompts_dir) if prompts_dir else None)


def _contract_handoff(agent: str, reply: str) -> str:
    """Render a compact contract derived from the real validated reply."""
    if agent == "chair":
        validation = validate_agent_output("chair", reply, strict=True)
        if not validation.success or not validation.data:
            return reply
        return json.dumps(
            compact_agent_contract("chair", _jsonable(validation.data)),
            ensure_ascii=False,
            separators=(",", ":"),
        )
    validation = validate_agent_output(agent, reply, strict=agent != "implementer")
    if not validation.success or not validation.data:
        return reply
    return json.dumps(
        compact_agent_contract(agent, _jsonable(validation.data)),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _handoff_text(agent: str, reply: str, mode: str) -> str:
    if _normalize_handoff_mode(mode) == "full":
        return reply
    return _contract_handoff(_role(agent), reply)


def _chair_handoff(reply: str, contract: str, mode: str = "full") -> str:
    if _normalize_handoff_mode(mode) == "full":
        return f"Chair decision data:\n{contract}\n\nChair raw output:\n{reply}"
    return f"Chair validated decision:\n{_contract_handoff('chair', reply)}"


def _criteria_from_plan(reply: str) -> list[dict]:
    validation = validate_agent_output("strategist", reply, strict=True)
    if not validation.success or not validation.data:
        return []
    return [
        {
            "id": str(task.get("id") or ""),
            "description": str(task.get("description") or ""),
            "acceptance": str(task.get("acceptance") or ""),
        }
        for task in validation.data.get("tasks", [])
        if task.get("id")
    ]


def _criteria_text(criteria: list[dict]) -> str:
    if not criteria:
        return "(No explicit acceptance criteria were supplied.)"
    return "\n".join(
        f"- id={item.get('id', '')}: {item.get('description', '')} | acceptance: {item.get('acceptance', '')}"
        for item in criteria
    )


def _prompt_version(messages: list[dict]) -> str:
    system_prompt = next(
        (str(message.get("content") or "") for message in messages if message.get("role") == "system"),
        "",
    )
    return hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()[:16]


def _plan_delta(previous: str, current: str) -> dict:
    previous_text = str(previous or "")
    current_text = str(current or "")
    return {
        "changed": previous_text != current_text,
        "previous_chars": len(previous_text),
        "current_chars": len(current_text),
        "delta_chars": len(current_text) - len(previous_text),
        "similarity": round(difflib.SequenceMatcher(None, previous_text, current_text).ratio(), 3),
    }


def _contract_fingerprint(agent: str, reply: str) -> str:
    validation = validate_agent_output(agent, reply, strict=True)
    if not validation.success or not validation.data:
        return hashlib.sha256(str(reply or "").encode("utf-8")).hexdigest()
    payload = compact_agent_contract(agent, _jsonable(validation.data))
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _manager_defect_fingerprint(data: dict | None) -> str:
    issues = []
    for issue in (data or {}).get("issues") or []:
        if not isinstance(issue, dict):
            continue
        issues.append({
            key: issue.get(key)
            for key in ("severity", "task_id", "description", "suggestion", "evidence")
            if issue.get(key) not in (None, "", [], {})
        })
    return hashlib.sha256(
        json.dumps(issues, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _perspective_block_findings(data: dict | None) -> list[dict]:
    """Return current hard-block findings from a validated Perspective contract."""
    findings = []
    for section in ("security", "performance", "maintainability"):
        for issue in (data or {}).get(section, {}).get("issues", []) or []:
            if isinstance(issue, dict) and str(issue.get("disposition") or "").upper() == "BLOCK":
                findings.append(issue)
    return findings

def _scope_present(tasks: list[dict], field: str, required: str) -> bool:
    expected = str(required or "").strip().rstrip("/")
    if not expected:
        return True
    for task in tasks:
        for scope in task.get(field, []) or []:
            actual = str(scope or "").strip().rstrip("/")
            if actual == expected or actual.startswith(f"{expected}/") or expected.startswith(f"{actual}/"):
                return True
    return False


_PATH_REFERENCE = re.compile(r"(?<![\w/:])(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+")


def _task_scope_consistency(tasks: list[dict]) -> bool:
    """Reject unsafe or undeclared file references in task descriptions."""
    for task in tasks:
        description = str(task.get("description") or "")
        if re.search(r"(?:\.\./|^[A-Za-z]:[\\/]|/etc/|/root/)", description, re.IGNORECASE):
            return False
        if task.get("workspace_root"):
            continue
        scopes = [
            str(scope or "").strip().replace("\\", "/").rstrip("/")
            for field in ("read_scope", "write_scope")
            for scope in (task.get(field) or [])
        ]
        clean_desc = re.sub(r"https?://\S+", "", description)
        clean_desc = re.sub(r"(?:[\w-]+\.)+(?:org|com|io|net|edu|gov|dev)/[^\s)]+", "", clean_desc)
        references = _PATH_REFERENCE.findall(clean_desc)
        for reference in references:
            reference = reference.rstrip(".,;:)")
            if not any(reference == scope or reference.startswith(f"{scope}/") for scope in scopes if scope):
                return False
    return True


def _planning_quality(data: dict, raw: str, rubric: dict | None) -> list[dict]:
    """Apply deterministic scenario checks to a validated Strategist plan."""
    if not rubric:
        return []
    tasks = data.get("tasks") or []
    text = raw.lower()
    checks = [
        _check(
            "minimum_tasks",
            len(tasks) >= int(rubric.get("minimum_tasks", 1)),
            f"Plan has at least {rubric.get('minimum_tasks', 1)} task(s).",
        ),
    ]
    if rubric.get("scope_policy") == "declared_or_workspace_root":
        checks.extend([
            _check(
                "scope_policy",
                all(
                    "write_scope" in task
                    and (not task.get("workspace_root") or not task.get("write_scope"))
                    for task in tasks
                ),
                "Every task declares write_scope; read-only tasks use [] and root writes use workspace_root.",
            ),
            _check(
                "scope_path_consistency",
                _task_scope_consistency(tasks),
                "Referenced paths remain inside the task's declared read/write scopes.",
            ),
        ])
    for scope in rubric.get("required_read_scopes", []) or []:
        checks.append(_check(
            f"read_scope:{scope}",
            _scope_present(tasks, "read_scope", scope),
            f"Plan includes repository inspection coverage for {scope}.",
        ))
    for scope in rubric.get("required_write_scopes", []) or []:
        checks.append(_check(
            f"write_scope:{scope}",
            _scope_present(tasks, "write_scope", scope),
            f"Plan includes an in-scope change or test under {scope}.",
        ))
    if rubric.get("require_inspection_task"):
        checks.append(_check(
            "inspection_task",
            any((task.get("read_scope") or []) and not (task.get("write_scope") or []) for task in tasks),
            "Plan has a read-first inspection task before modification.",
        ))
    if rubric.get("require_verification"):
        checks.append(_check(
            "verification_present",
            any(isinstance(task.get("verification"), dict) and task.get("verification") for task in tasks),
            "Plan includes machine-checkable verification.",
        ))
    if rubric.get("require_risks"):
        checks.append(_check(
            "risks_present",
            bool(data.get("risks")),
            "Plan records scenario risks rather than hiding assumptions.",
        ))
    for term in rubric.get("required_terms", []) or []:
        needle = str(term).lower()
        checks.append(_check(
            f"required_term:{term}",
            needle in text,
            f"Plan contains evidence for required concept: {term}.",
        ))
    for alternatives in rubric.get("required_terms_any", []) or []:
        terms = alternatives if isinstance(alternatives, list) else [alternatives]
        checks.append(_check(
            f"required_term_any:{'|'.join(str(term) for term in terms)}",
            any(str(term).lower() in text for term in terms),
            f"Plan contains evidence for at least one of: {', '.join(str(term) for term in terms)}.",
        ))
    for term in rubric.get("forbidden_terms", []) or []:
        needle = str(term).lower()
        checks.append(_check(
            f"forbidden_term_absent:{term}",
            needle not in text,
            f"Plan does not treat the existing-codebase task as {term}.",
        ))
    return checks


def build_messages(
    agent: str,
    user_prompt: str,
    *,
    workspace: str = "",
    chair_reply: str = "",
    strategist_reply: str = "",
    perspective_reply: str = "",
    manager_reply: str = "",
    implementer_reply: str = "",
    criteria: list[dict] | None = None,
    evidence: str = "",
    implementer_task: str = "",
    require_manager_challenge: bool = False,
    handoff_mode: str = "full",
    prompts_dir: str | Path | None = None,
) -> list[dict]:
    """Build the same bounded handoffs used by the live Council path."""
    agent = _role(agent)
    handoff_mode = _normalize_handoff_mode(handoff_mode)
    prompt_composer = _prompt_composer(prompts_dir)
    messages = [
        {"role": "system", "content": prompt_composer.compose(agent)},
        {"role": "user", "content": _user_message(user_prompt, workspace)},
    ]
    if agent == "chair":
        return messages

    if agent in {"strategist", "perspective_analyzer", "manager", "implementer"}:
        if not chair_reply:
            raise RoleEvalError(f"{agent} requires the Chair output handoff.")
        chair = _chair_contract(chair_reply)

    if agent == "strategist":
        if manager_reply:
            review = validate_agent_output("manager", manager_reply, strict=True)
            if not review.success or not review.data or review.data.get("verdict") != "REVISE":
                raise RoleEvalError("Manager result must be a valid REVISE verdict.")
            plan = validate_agent_output("strategist", strategist_reply, strict=True)
            if not plan.success:
                raise RoleEvalError(f"Previous Strategist result is not valid JSON: {plan.error}")
            messages.extend([
                {"role": "user", "content": f"{_chair_handoff(chair_reply, chair, handoff_mode)}\n\nPrevious Strategist plan:\n{_handoff_text('strategist', strategist_reply, handoff_mode)}"},
                {"role": "user", "content": (
                    "Manager requested one plan revision. Apply the concrete feedback below and "
                    "return a complete replacement plan as the Strategist JSON contract.\n\n"
                    f"Manager feedback:\n{_handoff_text('manager', manager_reply, handoff_mode)}"
                )},
            ])
        else:
            messages.append({
                "role": "user",
                "content": f"{_chair_handoff(chair_reply, chair, handoff_mode)}\n\nReturn the Strategist JSON contract now.",
            })
        return messages

    if agent == "perspective_analyzer":
        if not strategist_reply:
            raise RoleEvalError("Perspective Analyzer requires the Strategist output handoff.")
        plan = validate_agent_output("strategist", strategist_reply, strict=True)
        if not plan.success:
            raise RoleEvalError(f"Strategist result is not valid JSON: {plan.error}")
        messages.append({
            "role": "user",
            "content": (
                f"{_chair_handoff(chair_reply, chair, handoff_mode)}\n\n"
                f"Strategist plan to audit:\n{_handoff_text('strategist', strategist_reply, handoff_mode)}\n\n"
                "Return the Perspective Analyzer JSON now."
            ),
        })
        return messages

    if agent == "manager":
        if not strategist_reply:
            raise RoleEvalError("Manager requires the Strategist output handoff.")
        plan = validate_agent_output("strategist", strategist_reply, strict=True)
        if not plan.success:
            raise RoleEvalError(f"Strategist result is not valid JSON: {plan.error}")
        messages.append({
            "role": "user",
            "content": (
                f"{_chair_handoff(chair_reply, chair, handoff_mode)}\n\n"
                f"Strategist plan to review:\n{_handoff_text('strategist', strategist_reply, handoff_mode)}\n\n"
                "Return the Manager JSON now."
            ),
        })
        if perspective_reply:
            perspective = validate_agent_output("perspective_analyzer", perspective_reply, strict=True)
            if not perspective.success:
                raise RoleEvalError(f"Perspective result is not valid JSON: {perspective.error}")
            messages.append({"role": "user", "content": f"Perspective analysis:\n{_handoff_text('perspective_analyzer', perspective_reply, handoff_mode)}"})
        if require_manager_challenge and not manager_reply:
            messages.append({
                "role": "user",
                "content": (
                    "This is the compulsory Plan-0 challenge round. Return REVISE with the single "
                    "most important evidence-based improvement required before approval. The issue "
                    "must cite the affected task, concrete plan evidence, and the exact change the "
                    "Strategist must make. Do not approve Plan-0 in this round."
                ),
            })
        if manager_reply:
            prior_review = validate_agent_output("manager", manager_reply, strict=True)
            if not prior_review.success:
                raise RoleEvalError(f"Previous Manager result is not valid JSON: {prior_review.error}")
            messages.append({
                "role": "user",
                "content": (
                    "This is a revision review. Re-check the revised plan against the previous Manager "
                    "decision below. Approve only if its concrete defects are resolved; otherwise return "
                    "REVISE with the remaining evidence-based defects.\n\n"
                    f"Previous Manager decision:\n{_handoff_text('manager', manager_reply, handoff_mode)}"
                ),
            })
        return messages

    if agent == "implementer":
        if not strategist_reply:
            raise RoleEvalError("Implementer requires the Strategist output handoff.")
        plan = validate_agent_output("strategist", strategist_reply, strict=True)
        if not plan.success:
            raise RoleEvalError(f"Strategist result is not valid JSON: {plan.error}")
        context = (
            f"{_chair_handoff(chair_reply, chair, handoff_mode)}\n\n"
            f"Approved Strategist plan:\n{_handoff_text('strategist', strategist_reply, handoff_mode)}\n\n"
            f"Manager review:\n{_handoff_text('manager', manager_reply, handoff_mode) if manager_reply else '(not supplied)'}\n\n"
            f"Perspective analysis:\n{_handoff_text('perspective_analyzer', perspective_reply, handoff_mode) if perspective_reply else '(not supplied)'}\n\n"
            f"Task to execute:\n{implementer_task or '(execute the first approved task)'}\n\n"
            "Execute the bounded task using the supplied plan. Do not invent a different scope. "
            "Return the Implementer JSON completion report now."
        )
        messages.append({"role": "user", "content": context})
        return messages

    # Completeness Auditor intentionally receives the raw implementation
    # report plus criteria/evidence; this is the quality boundary, not a tool
    # or workspace integration test.
    if not strategist_reply:
        raise RoleEvalError("Completeness Auditor requires the Strategist output handoff.")
    if not implementer_reply:
        raise RoleEvalError("Completeness Auditor requires the Implementer output handoff.")
    plan = validate_agent_output("strategist", strategist_reply, strict=True)
    if not plan.success:
        raise RoleEvalError(f"Strategist result is not valid JSON: {plan.error}")
    selected_criteria = criteria if criteria is not None else _criteria_from_plan(strategist_reply)
    messages.append({
        "role": "user",
        "content": (
            f"Acceptance criteria checklist:\n{_criteria_text(selected_criteria)}\n\n"
            f"Strategist plan:\n{_handoff_text('strategist', strategist_reply, handoff_mode)}\n\n"
            f"Perspective analysis:\n{_handoff_text('perspective_analyzer', perspective_reply, handoff_mode) if perspective_reply else '(not supplied)'}\n\n"
            f"Manager review:\n{_handoff_text('manager', manager_reply, handoff_mode) if manager_reply else '(not supplied)'}\n\n"
            f"Delivered artifact (Implementer output):\n{_handoff_text('implementer', implementer_reply, handoff_mode)}\n\n"
            f"Evidence snapshot:\n{evidence or '(no workspace evidence; judge the supplied report only)'}\n\n"
            "Grade only the supplied evidence. Output the strict JSON described in your instructions."
        ),
    })
    return messages


def _contract_validation(agent: str, output: str):
    raw = _text(output).strip()
    if not raw:
        return False, None, "empty response", "empty_response", {}
    if agent == "implementer":
        if raw.lower() in _GARBAGE or len(raw) < 12:
            return False, None, "garbage or non-actionable response", "garbage_response", {}
        validation = validate_agent_output(agent, raw)
    else:
        validation = validate_agent_output(agent, raw, strict=True)
    if not validation.success:
        return False, None, validation.error or "invalid structured response", "schema_invalid", validation.metadata
    required = _REQUIRED_TOP_LEVEL_KEYS.get(agent)
    if required:
        parsed = validation.data or {}
        missing = sorted(required - set(parsed)) if isinstance(parsed, dict) else sorted(required)
        if missing:
            return False, None, f"missing required fields: {', '.join(missing)}", "schema_invalid", validation.metadata
    return True, validation.data, "", "", validation.metadata


def _check(name: str, passed: bool, detail: str) -> dict:
    return {"name": name, "passed": bool(passed), "detail": detail}


def _manager_rubric_checks(
    data: dict,
    strategist_data: dict | None,
    perspective_data: dict | None,
    rubric: dict,
) -> list[dict]:
    def predicate(name: str) -> bool:
        if name == "manager_issues_have_evidence":
            verdict = str(getattr(data.get("verdict"), "value", data.get("verdict") or ""))
            if verdict not in {"REVISE", "BLOCKED"}:
                return True
            return all(
                str(issue.get("severity") or "info") == "info"
                or all(str(issue.get(field) or "").strip() for field in ("description", "suggestion", "evidence"))
                for issue in (data.get("issues") or [])
            )
        if name == "plan_has_verification":
            return any(
                isinstance(task.get("verification"), dict) and task.get("verification")
                for task in (strategist_data or {}).get("tasks", [])
            )
        if name == "perspective_contains_block":
            return any(
                str(issue.get("disposition") or "").upper() == "BLOCK"
                for section in ("security", "performance", "maintainability")
                for issue in (perspective_data or {}).get(section, {}).get("issues", [])
            )
        if name == "perspective_has_unresolved_actionable_finding":
            return any(
                str(issue.get("disposition") or "").upper() in {"MUST_FIX", "BLOCK"}
                for section in ("security", "performance", "maintainability")
                for issue in (perspective_data or {}).get(section, {}).get("issues", [])
            )
        if name == "destructive_plan_without_rollback":
            text = json.dumps(strategist_data or {}, ensure_ascii=False).lower()
            return any(term in text for term in ("delete", "destroy", "production")) and not any(
                term in text for term in ("backup", "rollback", "recovery")
            )
        raise RoleEvalError(f"Unknown Manager rubric predicate: {name}")

    checks = []
    for item in rubric.get("required_manager_evidence", []) or []:
        if not isinstance(item, dict) or not item.get("predicate"):
            raise RoleEvalError("required_manager_evidence entries need a predicate")
        identifier = str(item.get("id") or item["predicate"])
        passed = predicate(str(item["predicate"]))
        checks.append(_check(
            f"manager_evidence:{identifier}",
            passed,
            f"Manager evidence predicate {item['predicate']} is satisfied.",
        ))
    verdict = str(getattr(data.get("verdict"), "value", data.get("verdict") or ""))
    for item in rubric.get("unsafe_approval_conditions", []) or []:
        if not isinstance(item, dict) or not item.get("predicate"):
            raise RoleEvalError("unsafe_approval_conditions entries need a predicate")
        identifier = str(item.get("id") or item["predicate"])
        triggered = predicate(str(item["predicate"]))
        passed = verdict != "APPROVED" or not triggered
        checks.append(_check(
            f"unsafe_approval:{identifier}",
            passed,
            f"Manager does not approve while {item['predicate']} is true.",
        ))
    return checks


def _semantic_quality(
    agent: str,
    data: dict | None,
    raw: str,
    *,
    strategist_data: dict | None = None,
    perspective_data: dict | None = None,
    criteria: list[dict] | None = None,
    scenario_rubric: dict | None = None,
) -> dict:
    """Cheap deterministic quality checks; semantic correctness stays separate from schema validity."""
    if not data:
        return {"passed": False, "score": 0.0, "checks": [_check("validated_data", False, "No validated contract data.")]}
    checks: list[dict] = []
    if agent == "chair":
        checks.extend([
            _check("target_present", bool(str(data.get("target") or "").strip()), "Chair target is actionable."),
            _check("reason_present", bool(str(data.get("reason") or "").strip()), "Chair includes routing rationale."),
            _check(
                "clarification_coherent",
                not data.get("ambiguous") or (bool(str(data.get("clarification") or "").strip()) and len(data.get("options") or []) >= 2),
                "Ambiguous routing includes a question and at least two options.",
            ),
        ])
    elif agent == "strategist":
        tasks = data.get("tasks") or []
        ids = [str(task.get("id") or "") for task in tasks]
        checks.extend([
            _check("tasks_have_unique_ids", len(ids) == len(set(ids)) and all(ids), "Task IDs are present and unique."),
            _check("tasks_have_descriptions", all(str(task.get("description") or "").strip() for task in tasks), "Every task has a concrete description."),
            _check("tasks_have_acceptance", all(str(task.get("acceptance") or "").strip() for task in tasks), "Every task has an acceptance condition."),
        ])
        checks.extend(_planning_quality(data, raw, scenario_rubric))
    elif agent == "manager":
        confidence = data.get("confidence")
        verdict = str(getattr(data.get("verdict"), "value", data.get("verdict") or ""))
        checks.extend([
            _check("summary_present", bool(str(data.get("summary") or "").strip()), "Manager explains its verdict."),
            _check("confidence_bounded", isinstance(confidence, (int, float)) and 0 <= confidence <= 1, "Confidence is between 0 and 1."),
        ])
        if verdict in {"REVISE", "BLOCKED"}:
            issues = data.get("issues") or []
            actionable_issues = [issue for issue in issues if str(issue.get("severity") or "info") != "info"]
            checks.append(_check(
                "non_approval_has_evidence",
                bool(actionable_issues) and all(
                    bool(str(issue.get("description") or "").strip())
                    and bool(str(issue.get("suggestion") or "").strip())
                    and bool(str(issue.get("evidence") or "").strip())
                    for issue in actionable_issues
                ),
                "REVISE and BLOCKED decisions cite actionable evidence and a concrete change.",
            ))
        task_ids = set()
        issue_ids = {str(issue.get("task_id")) for issue in data.get("issues", [])}
        if strategist_data:
            task_ids = {str(task.get("id")) for task in strategist_data.get("tasks", [])}
            issue_ids = {str(issue.get("task_id")) for issue in data.get("issues", [])}
        checks.append(_check("issue_ids_grounded", not strategist_data or issue_ids <= ({"ALL"} | task_ids), "Manager issues reference known tasks."))
        if verdict == "APPROVED" and perspective_data is not None:
            checks.append(_check(
                "perspective_blocks_resolved",
                not _perspective_block_findings(perspective_data),
                "Manager cannot approve while the current Perspective contract contains a hard BLOCK finding.",
            ))
        checks.extend(_manager_rubric_checks(data, strategist_data, perspective_data, scenario_rubric or {}))
    elif agent == "perspective_analyzer":
        scores = [data.get(section, {}).get("score") for section in ("security", "performance", "maintainability")]
        scores.append(data.get("overall_score"))
        findings = []
        for section in ("security", "performance", "maintainability"):
            findings.extend(data.get(section, {}).get("issues") or [])
        dispositions = {"ADVISORY", "MUST_FIX", "BLOCK"}
        task_ids = set()
        if strategist_data:
            task_ids = {str(task.get("id")) for task in strategist_data.get("tasks", []) if task.get("id")}
        perspective_issue_ids = {str(issue.get("task_id")) for issue in findings if issue.get("task_id")}
        checks.extend([
            _check("scores_bounded", all(isinstance(score, (int, float)) and 0 <= score <= 1 for score in scores), "All perspective scores are between 0 and 1."),
            _check("synthesis_present", bool(str(data.get("synthesis") or "").strip()), "Perspective analysis includes a synthesis."),
            _check(
                "finding_dispositions_valid",
                all(str(issue.get("disposition") or "ADVISORY") in dispositions for issue in findings),
                "Perspective findings use ADVISORY, MUST_FIX, or BLOCK.",
            ),
            _check(
                "blocking_findings_have_evidence",
                all(
                    str(issue.get("disposition") or "ADVISORY") == "ADVISORY"
                    or bool(str(issue.get("evidence") or "").strip())
                    for issue in findings
                ),
                "MUST_FIX and BLOCK findings cite plan evidence.",
            ),
            _check(
                "perspective_issue_ids_grounded",
                not strategist_data or perspective_issue_ids <= ({"ALL"} | task_ids),
                "Perspective issues reference known tasks.",
            ),
        ])
    elif agent == "implementer":
        checks.extend([
            _check("report_nonempty", bool(raw.strip()), "Implementer returned a report."),
            _check("report_actionable", len(raw.strip()) >= 12 and raw.strip().lower() not in _GARBAGE, "Implementer report is not a placeholder."),
        ])
    elif agent == "completeness_auditor":
        items = data.get("criteria") or []
        expected = {str(item.get("id")) for item in (criteria or []) if item.get("id")}
        actual = {str(item.get("id")) for item in items if item.get("id")}
        met_count = sum(1 for item in items if item.get("met"))
        expected_ratio = met_count / len(items) if items else 0.0
        checks.extend([
            _check("criteria_coverage", not expected or actual == expected, "Auditor covers the supplied criteria exactly."),
            _check("completeness_matches_criteria", not items or abs(float(data.get("completeness", 0)) - expected_ratio) <= 0.01, "Completeness matches met criteria."),
            _check("done_consistent", (not items and not data.get("done") and float(data.get("completeness", 0)) == 0) or (bool(items) and bool(data.get("done")) == all(item.get("met") for item in items)), "Done flag matches criterion outcomes."),
        ])
    passed = sum(check["passed"] for check in checks)
    return {"passed": bool(checks) and passed == len(checks), "score": round(passed / len(checks), 3) if checks else 0.0, "checks": checks}


def _handoff_integrity(
    agent: str,
    messages: list[dict] | None = None,
    *,
    handoff_mode: str = "contract",
    **replies: str,
) -> dict:
    handoff_mode = _normalize_handoff_mode(handoff_mode)
    required = {
        "strategist": ("chair_reply",),
        "perspective_analyzer": ("chair_reply", "strategist_reply"),
        "manager": ("chair_reply", "strategist_reply", "perspective_reply"),
        "implementer": ("chair_reply", "strategist_reply", "perspective_reply", "manager_reply"),
        "completeness_auditor": ("strategist_reply", "perspective_reply", "manager_reply", "implementer_reply"),
    }.get(agent, ())
    missing = [name for name in required if not str(replies.get(name) or "").strip()]
    prompt_text = "\n".join(str(message.get("content") or "") for message in (messages or []))
    forwarded_chars = 0
    source_chars = 0
    for name in required:
        reply = str(replies.get(name) or "")
        if name in missing:
            continue
        source_chars += len(reply)
        source_agent = _role(name.removesuffix("_reply"))
        validation = validate_agent_output(
            source_agent,
            reply,
            strict=source_agent != "implementer",
        )
        if not validation.success:
            missing.append(f"{name}_invalid_contract")
            continue
        payload = _handoff_text(name.removesuffix("_reply"), reply, handoff_mode)
        forwarded_chars += len(payload)
        if payload not in prompt_text:
            missing.append(f"{name}_not_forwarded")
    return {
        "passed": not missing,
        "required": list(required),
        "missing": missing,
        "mode": handoff_mode,
        "source_chars": source_chars,
        "forwarded_chars": forwarded_chars,
        "compression_ratio": round(forwarded_chars / source_chars, 3) if source_chars else 1.0,
    }


def _repair_messages(
    agent: str,
    messages: list[dict],
    error: str,
    raw: str,
    prompts_dir: str | Path | None = None,
) -> list[dict]:
    if agent != "implementer":
        return _schema_repair_messages(messages, agent, error, raw)
    return [
        {"role": "system", "content": _prompt_composer(prompts_dir).compose("implementer")},
        {"role": "user", "content": (
            "The previous Implementer response was empty or non-actionable. "
            "Return a concise completion report for the exact bounded task. "
            "Do not change scope, ask a question, or return placeholder text.\n\n"
            f"Original execution context:\n{_repair_context(messages)}"
        )},
    ]


def _repair_context(messages: list[dict], limit: int = 9000) -> str:
    parts = []
    remaining = limit
    for message in messages:
        content = str(message.get("content") or "")
        if not content:
            continue
        content = content[:remaining]
        parts.append(f"[{message.get('role', 'unknown')}]\n{content}")
        remaining -= len(content)
        if remaining <= 0:
            break
    return "\n\n".join(parts)


def _semantic_repair_messages(
    agent: str,
    messages: list[dict],
    valid_task_ids: list[str],
    invalid_issues: list[dict],
    raw_output: str,
) -> list[dict]:
    repair_prompt = (
        f"Your structured output for {agent} passed JSON schema validation, but contained UNGROUNDED task_id values.\n\n"
        f"Valid task IDs for this plan stage: {json.dumps(valid_task_ids)}\n"
        f"Invalid issue task_id references detected: {json.dumps(invalid_issues)}\n\n"
        "INSTRUCTION: Return a corrected complete replacement JSON response.\n"
        "You MUST ONLY update the 'task_id' fields on the flagged issues to match valid task IDs (or 'ALL').\n"
        "CRITICAL: Do NOT change any verdicts, severity levels, scores, dispositions, or issue descriptions."
    )
    res = list(messages)
    res.append({"role": "assistant", "content": raw_output})
    res.append({"role": "user", "content": repair_prompt})
    return res


def _semantic_repair_protected_fields_match(
    agent: str,
    original: dict,
    repaired: dict,
    flagged_issues: list[dict] | None = None,
) -> bool:
    if not isinstance(original, dict) or not isinstance(repaired, dict):
        return False

    orig_copy = copy.deepcopy(original)
    rep_copy = copy.deepcopy(repaired)

    if flagged_issues:
        for item in flagged_issues:
            if agent == "manager":
                idx = item.get("index")
                if isinstance(idx, int):
                    if 0 <= idx < len(orig_copy.get("issues", [])):
                        orig_copy["issues"][idx].pop("task_id", None)
                    if 0 <= idx < len(rep_copy.get("issues", [])):
                        rep_copy["issues"][idx].pop("task_id", None)
            elif agent == "perspective_analyzer":
                sec = item.get("section")
                idx = item.get("index")
                if sec and isinstance(idx, int):
                    orig_sec_issues = orig_copy.get(sec, {}).get("issues", [])
                    rep_sec_issues = rep_copy.get(sec, {}).get("issues", [])
                    if 0 <= idx < len(orig_sec_issues):
                        orig_sec_issues[idx].pop("task_id", None)
                    if 0 <= idx < len(rep_sec_issues):
                        rep_sec_issues[idx].pop("task_id", None)
    else:
        # If no specific flagged issues list provided, strip task_id from all issues in copies
        if agent == "manager":
            for issue in orig_copy.get("issues", []):
                issue.pop("task_id", None)
            for issue in rep_copy.get("issues", []):
                issue.pop("task_id", None)
        elif agent == "perspective_analyzer":
            for sec in ("security", "performance", "maintainability"):
                for issue in orig_copy.get(sec, {}).get("issues", []):
                    issue.pop("task_id", None)
                for issue in rep_copy.get(sec, {}).get("issues", []):
                    issue.pop("task_id", None)

    return orig_copy == rep_copy


def _get_ungrounded_issues(agent: str, data: dict, valid_task_ids: set[str]) -> list[dict]:
    if not isinstance(data, dict):
        return []
    invalid = []
    allowed = {"ALL"} | set(valid_task_ids)
    if agent == "manager":
        for idx, issue in enumerate(data.get("issues") or []):
            tid = str(issue.get("task_id") or "").strip()
            if tid not in allowed:
                invalid.append({"index": idx, "task_id": tid, "description": issue.get("description", "")})
    elif agent == "perspective_analyzer":
        for sec in ("security", "performance", "maintainability"):
            for idx, issue in enumerate(data.get(sec, {}).get("issues") or []):
                tid = str(issue.get("task_id") or "").strip()
                if tid not in allowed:
                    invalid.append({"section": sec, "index": idx, "task_id": tid, "description": issue.get("description", "")})
    return invalid


def _get_harness_fingerprint(prompts_dir: str | Path | None = None) -> str:
    digest = hashlib.sha256()
    root = Path(__file__).resolve().parents[1]
    
    schemas_path = root / "scripts" / "council_schemas.py"
    if schemas_path.exists():
        digest.update(schemas_path.read_bytes())
        
    role_eval_path = Path(__file__).resolve()
    if role_eval_path.exists():
        digest.update(role_eval_path.read_bytes())
        
    models_path = root / "config" / "models.json"
    if models_path.exists():
        digest.update(models_path.read_bytes())
        
    scenarios_path = root / "benchmarks" / "role_eval_scenarios_p2_1.json"
    if not scenarios_path.exists():
        scenarios_path = root / "benchmarks" / "role_eval_scenarios.json"
    if scenarios_path.exists():
        digest.update(scenarios_path.read_bytes())
        
    p_dir = Path(prompts_dir).resolve() if prompts_dir else root.parent / "data" / "council_agent_evals" / "phase-a" / "prompts" / "P2.3"
    parent_json = p_dir / "parent.json"
    if parent_json.exists():
        try:
            meta = json.loads(parent_json.read_text(encoding="utf-8"))
            tree_hash = str(meta.get("tree_sha256_excluding_metadata") or "")
            digest.update(tree_hash.encode())
        except Exception:
            pass
        
    return digest.hexdigest()


async def evaluate(
    agent: str,
    user_prompt: str,
    *,
    endpoint: str,
    model: str,
    api_key: str = "",
    workspace: str = "",
    chair_reply: str = "",
    strategist_reply: str = "",
    perspective_reply: str = "",
    manager_reply: str = "",
    implementer_reply: str = "",
    criteria: list[dict] | None = None,
    evidence: str = "",
    implementer_task: str = "",
    require_manager_challenge: bool = False,
    temperature: float = 0.0,
    max_tokens: int = 4096,
    timeout: float | None = None,
    run_id: str = "",
    scenario_rubric: dict | None = None,
    prompt_label: str = "",
    handoff_mode: str = "full",
    prompts_dir: str | Path | None = None,
    call=llm_call_async,
    _include_raw: bool = False,
    use_structured_output: bool = False,
) -> dict:
    agent = _role(agent)
    replies = {
        "chair_reply": chair_reply,
        "strategist_reply": strategist_reply,
        "perspective_reply": perspective_reply,
        "manager_reply": manager_reply,
        "implementer_reply": implementer_reply,
    }
    messages = build_messages(
        agent, user_prompt, workspace=workspace, chair_reply=chair_reply,
        strategist_reply=strategist_reply, perspective_reply=perspective_reply,
        manager_reply=manager_reply, implementer_reply=implementer_reply,
        criteria=criteria, evidence=evidence, implementer_task=implementer_task,
        require_manager_challenge=require_manager_challenge,
        handoff_mode=handoff_mode,
        prompts_dir=prompts_dir,
    )
    handoff = _handoff_integrity(agent, messages=messages, handoff_mode=handoff_mode, **replies)
    started = time.monotonic()
    effective_timeout = float(timeout if timeout is not None else _PROVIDER_CALL_TIMEOUT)
    if effective_timeout <= 0:
        raise RoleEvalError("timeout must be greater than zero")
    attempts: list[dict] = []
    output = ""
    initial_output = ""
    validation_data = None
    initial_validation_data = None
    error = ""
    initial_error = ""
    failure_kind = ""
    initial_failure_kind = ""
    provider_failed = False
    initial_contract_passed = False
    repair_attempted = False
    repair_signature = None
    initial_normalization_metadata: dict = {}
    final_normalization_metadata: dict = {}
    final_messages = messages

    async def invoke_once(attempt_messages: list[dict], attempt_name: str) -> str:
        attempt_started = time.monotonic()
        request_chars = sum(len(str(message.get("content") or "")) for message in attempt_messages)
        try:
            response = await asyncio.wait_for(
                call(
                    url=endpoint,
                    model=model,
                    messages=attempt_messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    max_retries=_PROVIDER_CALL_MAX_RETRIES,
                    timeout=effective_timeout,
                    headers=build_headers(api_key or None, endpoint),
                    trace_context={
                        "run_id": run_id or f"role-eval-{agent}",
                        "agent": agent,
                        "route": "ROLE_EVAL",
                        "attempt": attempt_name,
                        **({"response_format": build_response_format(agent)} if use_structured_output else {}),
                    },
                ),
                timeout=effective_timeout,
            )
        except Exception as exc:
            error_text = (
                f"provider call exceeded {effective_timeout:g}s"
                if isinstance(exc, asyncio.TimeoutError)
                else str(exc)
            )
            attempts.append({
                "name": attempt_name,
                "status": "PROVIDER_ERROR",
                "duration_ms": round((time.monotonic() - attempt_started) * 1000),
                "request_chars": request_chars,
                "response_chars": 0,
                "error": _redact_text(error_text)[:2000],
            })
            raise EvalProviderError(error_text, original=exc) from exc
        value = _text(response)
        attempts.append({
            "name": attempt_name,
            "status": "COMPLETED",
            "duration_ms": round((time.monotonic() - attempt_started) * 1000),
            "request_chars": request_chars,
            "response_chars": len(value),
        })
        return value

    async def invoke(attempt_messages: list[dict], attempt_name: str) -> str:
        """Use one fixed provider retry without changing endpoint or model."""
        for retry_number in range(_PROVIDER_RETRY_LIMIT + 1):
            name = attempt_name if retry_number == 0 else f"{attempt_name}_provider_retry_{retry_number}"
            try:
                return await invoke_once(attempt_messages, name)
            except EvalProviderError:
                if retry_number == _PROVIDER_RETRY_LIMIT:
                    raise

    attempts_detail: list[dict] = []
    repair_kind = None
    try:
        output = await invoke(messages, "initial")
        initial_output = output
        (
            initial_contract_passed,
            validation_data,
            error,
            failure_kind,
            initial_normalization_metadata,
        ) = _contract_validation(agent, output)
        initial_validation_data = validation_data
        initial_failure_kind = failure_kind
        initial_error = error

        attempts_detail.append({
            "kind": "initial",
            "raw_response": _redact_text(initial_output),
            "canonical_output": _jsonable(initial_validation_data) if initial_validation_data else None,
            "normalization": initial_normalization_metadata,
            "contract_passed": initial_contract_passed,
            "semantic_passed": False,
            "repair_reason": initial_error if not initial_contract_passed else None,
        })

        if not initial_contract_passed:
            repair_attempted = True
            repair_kind = "schema_repair"
            repair_signature = f"{agent}|schema_repair|{initial_failure_kind or 'schema_invalid'}|{initial_error[:50]}"
            repair_messages = _repair_messages(agent, messages, error, output, prompts_dir=prompts_dir)
            final_messages = repair_messages
            output = await invoke(repair_messages, "schema_repair" if agent != "implementer" else "response_recovery")
            (
                contract_passed,
                validation_data,
                error,
                final_failure_kind,
                final_normalization_metadata,
            ) = _contract_validation(agent, output)
            failure_kind = "" if contract_passed else final_failure_kind
            if contract_passed:
                error = ""
            attempts_detail.append({
                "kind": "schema_repair",
                "raw_response": _redact_text(output),
                "canonical_output": _jsonable(validation_data) if validation_data else None,
                "normalization": final_normalization_metadata,
                "contract_passed": contract_passed,
                "semantic_passed": False,
                "repair_reason": error if not contract_passed else None,
                "repair_signature": repair_signature,
            })
        else:
            contract_passed = True
            final_normalization_metadata = initial_normalization_metadata

        # Check stage-local semantic grounding for task IDs
        perspective_data = None
        if perspective_reply:
            prior_perspective = validate_agent_output("perspective_analyzer", perspective_reply, strict=True)
            if prior_perspective.success:
                perspective_data = prior_perspective.data
        strategist_data = None
        if strategist_reply:
            prior = validate_agent_output("strategist", strategist_reply, strict=True)
            if prior.success:
                strategist_data = prior.data

        valid_task_ids = set()
        if strategist_data:
            valid_task_ids = {str(t.get("id")) for t in strategist_data.get("tasks", []) if t.get("id")}

        # Grounding repair check
        if contract_passed and agent in {"perspective_analyzer", "manager"} and valid_task_ids:
            ungrounded = _get_ungrounded_issues(agent, validation_data, valid_task_ids)
            if ungrounded and not repair_attempted:
                # Bounded semantic repair attempt
                repair_attempted = True
                repair_kind = "semantic_repair"
                ungrounded_tids = sorted(list({str(u.get("task_id")) for u in ungrounded}))
                repair_signature = f"{agent}|semantic_repair|ungrounded_task_id|{json.dumps(ungrounded_tids)}"
                sem_messages = _semantic_repair_messages(agent, messages, sorted(list(valid_task_ids)), ungrounded, output)
                final_messages = sem_messages
                sem_raw = await invoke(sem_messages, "semantic_repair")
                sem_passed, sem_val_data, sem_err, sem_fail_kind, sem_norm_meta = _contract_validation(agent, sem_raw)
                
                # Re-grounding check after semantic repair
                post_ungrounded = _get_ungrounded_issues(agent, sem_val_data, valid_task_ids) if sem_passed else []

                if (
                    sem_passed
                    and not post_ungrounded
                    and _semantic_repair_protected_fields_match(agent, validation_data, sem_val_data, ungrounded)
                ):
                    output = sem_raw
                    validation_data = sem_val_data
                    final_normalization_metadata = sem_norm_meta
                    error = ""
                else:
                    contract_passed = False
                    error = f"semantic repair failed: {sem_err or ('ungrounded task_ids remain' if post_ungrounded else 'protected fields modified')}"
                    failure_kind = "semantic_repair_failed"

                attempts_detail.append({
                    "kind": "semantic_repair",
                    "raw_response": _redact_text(sem_raw),
                    "canonical_output": _jsonable(sem_val_data) if sem_val_data else None,
                    "normalization": sem_norm_meta,
                    "contract_passed": sem_passed,
                    "semantic_passed": contract_passed,
                    "repair_reason": error if not contract_passed else None,
                    "repair_signature": repair_signature,
                })

    except EvalProviderError as exc:
        contract_passed = False
        provider_failed = True
        failure_kind = "provider_error"
        error = _redact_text(str(exc))[:2000] or (attempts[-1].get("error", "") if attempts else "")

    perspective_data = None
    if perspective_reply:
        prior_perspective = validate_agent_output("perspective_analyzer", perspective_reply, strict=True)
        if prior_perspective.success:
            perspective_data = prior_perspective.data
    strategist_data = None
    if strategist_reply:
        prior = validate_agent_output("strategist", strategist_reply, strict=True)
        if prior.success:
            strategist_data = prior.data
    semantic = _semantic_quality(
        agent, validation_data, output,
        strategist_data=strategist_data, perspective_data=perspective_data,
        criteria=criteria if criteria is not None else _criteria_from_plan(strategist_reply),
        scenario_rubric=scenario_rubric if agent in {"strategist", "manager"} else None,
    )
    if attempts_detail:
        attempts_detail[-1]["semantic_passed"] = bool(semantic.get("passed"))
    public_output = _redact_text(output)
    canonical_output = _jsonable(validation_data) if validation_data else None
    prompt_version = _prompt_version(messages)
    final_prompt_version = _prompt_version(final_messages)
    system_prompt_chars = len(str(next((message.get("content") for message in messages if message.get("role") == "system"), "") or ""))
    final_system_prompt_chars = len(str(next((message.get("content") for message in final_messages if message.get("role") == "system"), "") or ""))
    result = {
        "schema_version": 2,
        "kind": "role_eval",
        "timestamp": time.time(),
        "agent": agent,
        "model": model,
        "endpoint": _endpoint_label(endpoint),
        "temperature": temperature,
        "max_tokens": max_tokens,
        "prompt_label": prompt_label or "unlabeled",
        "prompt_source": str(Path(prompts_dir).resolve()) if prompts_dir else "configured",
        "handoff_mode": _normalize_handoff_mode(handoff_mode),
        "prompt_version": prompt_version,
        "final_prompt_version": final_prompt_version,
        "system_prompt_chars": system_prompt_chars,
        "final_system_prompt_chars": final_system_prompt_chars,
        "duration_ms": round((time.monotonic() - started) * 1000),
        "initial_contract_passed": initial_contract_passed,
        "contract_passed": contract_passed,
        "schema_repair_attempted": repair_attempted and repair_kind == "schema_repair",
        "schema_repair_succeeded": repair_attempted and repair_kind == "schema_repair" and contract_passed,
        "semantic_repair_attempted": repair_attempted and repair_kind == "semantic_repair",
        "semantic_repair_succeeded": repair_attempted and repair_kind == "semantic_repair" and contract_passed,
        "repair_signature": repair_signature,
        "harness_fingerprint": _get_harness_fingerprint(prompts_dir),
        "garbage_recovery_attempted": repair_attempted and initial_failure_kind in {"garbage_response", "empty_response", "schema_invalid"},
        "garbage_recovery_succeeded": repair_attempted and contract_passed,
        "normalization": {
            "initial": initial_normalization_metadata,
            "final": final_normalization_metadata,
        },
        "normalization_used": bool(
            initial_normalization_metadata.get("normalization_used")
            or final_normalization_metadata.get("normalization_used")
        ),
        "provider_failed": provider_failed,
        "failure_kind": failure_kind,
        "error": _redact_text(error)[:2000],
        "initial_error": _redact_text(initial_error)[:2000],
        "handoff_integrity": handoff,
        "semantic_quality": semantic,
        "request_chars": sum(len(str(message.get("content") or "")) for message in final_messages),
        "response_chars": len(public_output),
        "attempt_count": len(attempts),
        "provider_retry_limit": _PROVIDER_RETRY_LIMIT,
        "provider_call_max_retries": _PROVIDER_CALL_MAX_RETRIES,
        "provider_call_timeout_seconds": effective_timeout,
        "provider_error_count": sum(attempt.get("status") == "PROVIDER_ERROR" for attempt in attempts),
        "provider_retry_recovered": any(attempt.get("status") == "PROVIDER_ERROR" for attempt in attempts) and contract_passed,
        "attempts": attempts,
        "attempts_detail": attempts_detail,
        "repair_kind": repair_kind,
        "messages": [{**message, "content": _redact_text(str(message.get("content") or ""))} for message in messages],
        "final_messages": [{**message, "content": _redact_text(str(message.get("content") or ""))} for message in final_messages],
        "initial_raw_response": _redact_text(initial_output),
        "initial_canonical_output": _jsonable(initial_validation_data) if initial_validation_data else None,
        "raw_response": public_output,
        "output": public_output,
        "canonical_output": canonical_output,
        "contract_data": canonical_output,
    }
    if _include_raw:
        result["_raw_output"] = output
    return result


def _config_value(config, key: str, default=None):
    if isinstance(config, dict):
        return config.get(key, default)
    return getattr(config, key, default)


def _role_config(
    role: str,
    role_configs: dict | None,
    endpoint: str,
    model: str,
) -> tuple[str, str, float, int, float]:
    config = (role_configs or {}).get(role, {})
    return (
        str(endpoint or _config_value(config, "endpoint", _config_value(config, "endpoint_url", "")) or ""),
        str(model or _config_value(config, "model", "") or ""),
        float(_config_value(config, "temperature", 0.0) or 0.0),
        int(_config_value(config, "max_tokens", 4096) or 4096),
        float(_config_value(config, "timeout", _PROVIDER_CALL_TIMEOUT) or _PROVIDER_CALL_TIMEOUT),
    )


async def evaluate_trace(
    user_prompt: str,
    *,
    endpoint: str = "",
    model: str = "",
    role_configs: dict | None = None,
    api_key: str = "",
    workspace: str = "",
    evidence: str = "",
    run_id: str = "",
    max_plan_revisions: int = 2,
    planning_only: bool = False,
    scenario_id: str = "",
    scenario_rubric: dict | None = None,
    prompt_label: str = "",
    handoff_mode: str = "contract",
    prompts_dir: str | Path | None = None,
    call=llm_call_async,
    _trace_state: list[dict] | None = None,
    _trace_budget: float | None = None,
) -> dict:
    """Run an isolated trace with a configurable, approval-gated plan loop."""
    if max_plan_revisions < 0:
        raise RoleEvalError("max_plan_revisions must be zero or greater")
    run_id = run_id or f"role-eval-trace-{int(time.time() * 1000)}"
    trace: list[dict] = _trace_state if _trace_state is not None else []
    trace_budget = float(
        _trace_budget
        if _trace_budget is not None
        else _trace_budget_seconds(max_plan_revisions, planning_only, role_configs)
    )
    outputs: dict[str, str] = {}
    stage_data: dict[str, dict] = {}
    criteria: list[dict] = []
    plan_versions: list[dict] = []
    decision_history: list[dict] = []
    consecutive_provider_failures: dict[tuple[str, str, str], int] = {}

    async def stage(agent: str, stage_name: str | None = None, trace_metadata: dict | None = None, **kwargs):
        stage_label = stage_name or agent
        marker = {
            "schema_version": 2, "kind": "role_eval", "agent": agent,
            "stage": stage_label, "status": "IN_PROGRESS",
            "contract_passed": False, "initial_contract_passed": False,
            "provider_failed": False, "failure_kind": "", "error": "",
            "handoff_integrity": {"passed": True, "required": []},
            "semantic_quality": {"passed": False, "score": 0.0, "checks": []},
            "attempt_count": 0, "provider_error_count": 0,
            "duration_ms": 0, "request_chars": 0, "response_chars": 0,
            "output": "", "messages": [], "final_messages": [],
        }
        if trace_metadata:
            marker["trace_metadata"] = trace_metadata
        marker_index = len(trace)
        trace.append(marker)
        try:
            role_endpoint, role_model, temperature, max_tokens, role_timeout = _role_config(
                agent, role_configs, endpoint, model
            )
            if not role_endpoint or not role_model:
                raise RoleEvalError(f"No endpoint/model configured for {agent}.")
            result = await evaluate(
                agent, user_prompt, endpoint=role_endpoint, model=role_model,
                api_key=api_key, workspace=workspace, temperature=temperature,
                max_tokens=max_tokens, timeout=role_timeout, run_id=run_id,
                scenario_rubric=scenario_rubric,
                prompt_label=prompt_label, handoff_mode=handoff_mode,
                prompts_dir=prompts_dir,
                call=call, _include_raw=True, **kwargs,
            )
        except EvalProviderError as exc:
            marker.update(status="PROVIDER_ERROR", provider_failed=True, failure_kind="provider_error",
                          error=_redact_text(str(exc))[:2000], provider_error_count=1)
            stage_data[agent] = marker
            return marker
        except EvalHarnessError as exc:
            marker.update(status="HARNESS_FAILURE", failure_kind="harness_error",
                          error=_redact_text(str(exc))[:2000])
            raise
        except Exception as exc:
            marker.update(status="HARNESS_FAILURE", failure_kind="harness_error",
                          error=_redact_text(str(exc))[:2000])
            raise
        provider_key = (agent, role_endpoint, role_model)
        if result.get("provider_failed"):
            failure_count = consecutive_provider_failures.get(provider_key, 0) + 1
            consecutive_provider_failures[provider_key] = failure_count
            result["consecutive_provider_failures"] = failure_count
            if failure_count >= 2:
                result["provider_circuit_breaker_tripped"] = True
                result["provider_circuit_breaker_reason"] = (
                    f"{agent} provider failed {failure_count} consecutive times"
                )
        else:
            consecutive_provider_failures.pop(provider_key, None)
        raw = str(result.pop("_raw_output", "") or "")
        result["stage"] = stage_label
        result["status"] = "COMPLETED"
        if trace_metadata:
            result["trace_metadata"] = trace_metadata
        if result.get("contract_passed"):
            outputs[agent] = raw
        stage_data[agent] = result
        trace[marker_index] = result
        return result

    def report(**kwargs):
        return _trace_report(
            user_prompt, trace, stage_data, outputs, run_id,
            plan_versions=plan_versions,
            decision_history=decision_history,
            planning_only=planning_only,
            scenario_id=scenario_id,
            trace_budget_seconds=trace_budget,
            max_plan_revisions=max_plan_revisions,
            prompts_dir=prompts_dir,
            **kwargs,
        )

    chair = await stage("chair")
    if not chair.get("contract_passed"):
        return report()
    chair_data = validate_agent_output("chair", outputs["chair"], strict=True).data or {}
    if chair_data.get("ambiguous"):
        return report(human_escalation_required=True, termination_reason="clarification_required")

    strategist = await stage("strategist", chair_reply=outputs["chair"])
    if not strategist.get("contract_passed"):
        return report()
    criteria = _criteria_from_plan(outputs["strategist"])
    plan_versions.append({"version": "v1", "stage": "strategist", "output": _redact_text(outputs["strategist"])})

    perspective = await stage(
        "perspective_analyzer", chair_reply=outputs["chair"], strategist_reply=outputs["strategist"],
    )
    if not perspective.get("contract_passed"):
        return report()

    manager = await stage(
        "manager", chair_reply=outputs["chair"], strategist_reply=outputs["strategist"],
        perspective_reply=outputs["perspective_analyzer"], require_manager_challenge=True,
    )
    if not manager.get("contract_passed"):
        return report()

    manager_data = validate_agent_output("manager", outputs["manager"], strict=True).data or {}
    decision_history.append(_manager_decision_record("manager", "v1", manager_data))
    if manager_data.get("verdict") != "REVISE":
        return report(
            human_escalation_required=True,
            termination_reason="compulsory_revision_not_requested",
        )
    revision_number = 0
    while manager_data.get("verdict") == "REVISE" and revision_number < max_plan_revisions:
        revision_number += 1
        previous_plan = outputs["strategist"]
        previous_manager = outputs["manager"]
        previous_manager_data = manager_data
        revised_stage = "strategist_revision" if revision_number == 1 else f"strategist_revision_{revision_number}"
        revised = await stage(
            "strategist",
            stage_name=revised_stage,
            chair_reply=outputs["chair"], strategist_reply=outputs["strategist"],
            manager_reply=previous_manager,
            trace_metadata={
                "revision_number": revision_number,
                "previous_plan_version": f"v{revision_number}",
                "new_plan_version": f"v{revision_number + 1}",
            },
        )
        if not revised.get("contract_passed"):
            return report(human_escalation_required=True, termination_reason="strategist_revision_failed")
        revised_plan = outputs["strategist"]
        plan_versions.append({
            "version": f"v{revision_number + 1}",
            "stage": revised_stage,
            "output": _redact_text(revised_plan),
            "manager_feedback": _redact_text(previous_manager),
            "delta": _plan_delta(previous_plan, revised_plan),
        })
        criteria = _criteria_from_plan(revised_plan)
        # Perspective findings cite the plan they inspected. A revised plan
        # invalidates that evidence, so refresh it before Manager re-review;
        # do not silently reuse a stale analysis.
        perspective_recheck = await stage(
            "perspective_analyzer",
            stage_name="perspective_revision" if revision_number == 1 else f"perspective_revision_{revision_number}",
            chair_reply=outputs["chair"],
            strategist_reply=revised_plan,
            trace_metadata={
                "revision_number": revision_number,
                "plan_version": f"v{revision_number + 1}",
                "recheck": True,
            },
        )
        if not perspective_recheck.get("contract_passed"):
            return report(
                human_escalation_required=True,
                termination_reason="perspective_revision_failed",
            )
        final_manager = await stage(
            "manager",
            stage_name="manager_revision_review" if revision_number == 1 else f"manager_revision_review_{revision_number}",
            chair_reply=outputs["chair"], strategist_reply=revised_plan,
            perspective_reply=outputs["perspective_analyzer"], manager_reply=previous_manager,
            trace_metadata={
                "revision_number": revision_number,
                "plan_version": f"v{revision_number + 1}",
            },
        )
        if not final_manager.get("contract_passed"):
            return report(human_escalation_required=True, termination_reason="manager_revision_review_failed")
        manager_data = validate_agent_output("manager", outputs["manager"], strict=True).data or {}
        decision_record = _manager_decision_record(
            "manager_revision_review" if revision_number == 1 else f"manager_revision_review_{revision_number}",
            f"v{revision_number + 1}",
            manager_data,
        )
        revision_delta = plan_versions[-1].get("delta") or _plan_delta(previous_plan, revised_plan)
        plan_changed = _contract_fingerprint("strategist", previous_plan) != _contract_fingerprint("strategist", revised_plan)
        manager_defect_changed = _manager_defect_fingerprint(previous_manager_data) != _manager_defect_fingerprint(manager_data)
        decision_record["revision_progress"] = {
            "plan_changed": plan_changed,
            "manager_defect_changed": manager_defect_changed,
            "delta": revision_delta,
        }
        decision_history.append(decision_record)
        if not plan_changed or (manager_data.get("verdict") == "REVISE" and not manager_defect_changed):
            return report(
                human_escalation_required=True,
                termination_reason="revision_no_progress",
            )

    final_verdict = str(getattr(manager_data.get("verdict"), "value", manager_data.get("verdict") or ""))
    if final_verdict != "APPROVED":
        return report(
            human_escalation_required=True,
            termination_reason="revision_budget_exhausted" if final_verdict == "REVISE" else "manager_blocked",
        )
    if len(plan_versions) < 2:
        return report(
            human_escalation_required=True,
            termination_reason="compulsory_revision_missing",
        )
    if planning_only:
        return report(termination_reason="planning_approved")

    plan_data = validate_agent_output("strategist", outputs["strategist"], strict=True).data or {}
    first_task = (plan_data.get("tasks") or [{}])[0]
    implementer = await stage(
        "implementer", chair_reply=outputs["chair"], strategist_reply=outputs["strategist"],
        perspective_reply=outputs["perspective_analyzer"], manager_reply=outputs["manager"],
        implementer_task=json.dumps(first_task, ensure_ascii=False),
    )
    if not implementer.get("contract_passed"):
        return report(termination_reason="implementer_contract_failed")

    await stage(
        "completeness_auditor", strategist_reply=outputs["strategist"],
        perspective_reply=outputs["perspective_analyzer"], manager_reply=outputs["manager"],
        implementer_reply=outputs["implementer"],
        criteria=criteria, evidence=evidence,
    )
    return report(termination_reason="implementation_evaluated")


def _trace_report(
    user_prompt: str,
    trace: list[dict],
    stage_data: dict,
    outputs: dict[str, str],
    run_id: str,
    *,
    plan_versions: list[dict] | None = None,
    decision_history: list[dict] | None = None,
    planning_only: bool = False,
    scenario_id: str = "",
    human_escalation_required: bool = False,
    termination_reason: str = "",
    termination: str = "",
    trace_budget_seconds: float | None = None,
    max_plan_revisions: int = 2,
    prompts_dir: Path | str | None = None,
) -> dict:
    attempted = [record for record in trace if record.get("attempt_count", 0) > 0]
    contract_passed = [record for record in attempted if record.get("contract_passed")]
    semantic_passed = [record for record in attempted if record.get("semantic_quality", {}).get("passed")]
    repairs = [record for record in attempted if record.get("schema_repair_attempted")]
    recoveries = [record for record in attempted if record.get("garbage_recovery_attempted")]
    provider_failures = [record for record in trace if record.get("provider_failed")]
    provider_errors = sum(int(record.get("provider_error_count") or 0) for record in trace)
    provider_inconclusive = [
        f"{record.get('agent', 'unknown')}: provider failure"
        for record in provider_failures
    ]
    termination = termination or ("PROVIDER_INCONCLUSIVE" if provider_inconclusive else "COMPLETE")
    trace_budget_seconds = float(trace_budget_seconds if trace_budget_seconds is not None else _trace_budget_seconds(max_plan_revisions, planning_only))
    handoffs = [record for record in trace if record.get("handoff_integrity", {}).get("required")]
    strategist_records = [record for record in trace if record.get("agent") == "strategist" and record.get("contract_passed")]
    final_strategist = strategist_records[-1] if strategist_records else None
    gate_failures = []
    for record in trace:
        role = record.get("agent", "unknown")
        if not record.get("contract_passed") and not record.get("provider_failed"):
            gate_failures.append(f"{role}: contract failed")
        # A baseline Strategist plan may intentionally contain the defect that
        # Manager is expected to send back for revision. Only the final plan is
        # a readiness gate; all intermediate quality results remain recorded.
        if (
            record.get("contract_passed")
            and not record.get("semantic_quality", {}).get("passed")
            and (role != "strategist" or record is final_strategist)
        ):
            gate_failures.append(f"{role}: semantic checks failed")
        if not record.get("handoff_integrity", {}).get("passed", True):
            gate_failures.append(f"{role}: handoff failed")
    manager_decisions = [
        item for item in (decision_history or [])
        if item.get("verdict")
    ]
    if manager_decisions and manager_decisions[-1].get("verdict") != "APPROVED":
        gate_failures.append(f"manager: final verdict is {manager_decisions[-1].get('verdict')}")
    if human_escalation_required:
        gate_failures.append("human escalation required")

    repair_signatures = [
        str(record.get("repair_signature"))
        for record in trace
        if record.get("repair_signature")
    ]
    sig_counts = collections.Counter(repair_signatures)
    for sig, count in sig_counts.items():
        if count > 1:
            gate_failures.append(f"repeated repair signature: {sig}")
    return {
        "schema_version": 2,
        "kind": "role_eval_trace",
        "timestamp": time.time(),
        "run_id": run_id,
        "scenario_id": scenario_id,
        "harness_fingerprint": _get_harness_fingerprint(prompts_dir),
        "termination": termination,
        "trace_budget_seconds": trace_budget_seconds,
        "trace_stage_count": _trace_stage_count(max_plan_revisions, planning_only),
        "max_plan_revisions": max_plan_revisions,
        "max_provider_calls_per_stage": _max_provider_calls_per_stage(),
        "user_prompt": _redact_text(user_prompt),
        "roles": [record.get("agent") for record in trace],
        "trace": trace,
        "outputs": {role: _redact_text(output) for role, output in outputs.items()},
        "plan_versions": plan_versions or [],
        "decision_history": decision_history or [],
        "summary": {
            "stages_attempted": len(attempted),
            "contract_validity": round(len(contract_passed) / len(attempted), 3) if attempted else 0.0,
            "initial_contract_validity": round(sum(bool(record.get("initial_contract_passed")) for record in attempted) / len(attempted), 3) if attempted else 0.0,
            "schema_repair_attempts": len(repairs),
            "schema_repair_successes": sum(bool(record.get("schema_repair_succeeded")) for record in repairs),
            "garbage_recovery_attempts": len(recoveries),
            "garbage_recovery_successes": sum(bool(record.get("garbage_recovery_succeeded")) for record in recoveries),
            "handoff_integrity": round(sum(bool(record.get("handoff_integrity", {}).get("passed")) for record in handoffs) / len(handoffs), 3) if handoffs else 1.0,
            "semantic_quality": round(sum(record.get("semantic_quality", {}).get("score", 0.0) for record in attempted) / len(attempted), 3) if attempted else 0.0,
            "latency_ms": sum(int(record.get("duration_ms") or 0) for record in trace),
            "prompt_chars": sum(int(record.get("request_chars") or 0) for record in trace),
            "provider_failures": len(provider_failures),
            "provider_errors": provider_errors,
            "provider_retry_recoveries": sum(bool(record.get("provider_retry_recovered")) for record in trace),
            "provider_circuit_breaker_trips": sum(
                bool(record.get("provider_circuit_breaker_tripped")) for record in trace
            ),
            "response_chars": sum(int(record.get("response_chars") or 0) for record in trace),
            "handoff_chars": sum(int(record.get("handoff_integrity", {}).get("forwarded_chars") or 0) for record in trace),
            "handoff_source_chars": sum(int(record.get("handoff_integrity", {}).get("source_chars") or 0) for record in trace),
            "handoff_compression_ratio": round(
                sum(int(record.get("handoff_integrity", {}).get("forwarded_chars") or 0) for record in trace)
                / max(1, sum(int(record.get("handoff_integrity", {}).get("source_chars") or 0) for record in trace)),
                3,
            ),
            "handoff_modes": sorted({str(record.get("handoff_mode")) for record in trace if record.get("handoff_mode")} ),
            "prompt_versions": sorted({str(record.get("prompt_version")) for record in trace if record.get("prompt_version")}),
            "prompt_labels": sorted({str(record.get("prompt_label")) for record in trace if record.get("prompt_label")}),
            "prompt_sources": sorted({str(record.get("prompt_source")) for record in trace if record.get("prompt_source")}),
            "model_assignments": {str(record.get("agent")): str(record.get("model")) for record in trace if record.get("model")},
            "plan_revision_attempts": sum(str(record.get("stage", "")).startswith("strategist_revision") for record in trace),
            "plan_revision_successes": sum(str(record.get("stage", "")).startswith("strategist_revision") and record.get("contract_passed") for record in trace),
            "perspective_rechecks": sum(str(record.get("stage", "")).startswith("perspective_revision") for record in trace),
            "plan_revision_deltas": [item.get("delta") for item in (plan_versions or []) if item.get("delta")],
            "plan_versions": len(plan_versions or []),
            "manager_decisions": len(manager_decisions),
            "final_manager_verdict": manager_decisions[-1].get("verdict") if manager_decisions else "",
            "human_escalation_required": human_escalation_required,
            "termination_reason": termination_reason,
            "termination": termination,
            "trace_budget_seconds": trace_budget_seconds,
            "trace_stage_count": _trace_stage_count(max_plan_revisions, planning_only),
            "max_provider_calls_per_stage": _max_provider_calls_per_stage(),
            "planning_only": planning_only,
            "planning_quality": {
                "passed": bool(final_strategist and final_strategist.get("semantic_quality", {}).get("passed")),
                "score": float(final_strategist.get("semantic_quality", {}).get("score", 0.0)) if final_strategist else 0.0,
                "checks": (final_strategist.get("semantic_quality", {}).get("checks", []) if final_strategist else []),
            },
        },
        "readiness_gate": {
            "passed": termination == "COMPLETE" and not gate_failures and not provider_inconclusive and bool(trace),
            "failures": gate_failures,
            "provider_inconclusive": provider_inconclusive,
            "classification": ("harness-failure" if termination == "HARNESS_FAILURE" else ("provider-inconclusive" if provider_inconclusive else ("pass" if not gate_failures else "failed"))),
            "human_escalation_required": human_escalation_required,
        },
    }


def _partial_trace(
    trace_state: list[dict],
    *,
    status: str,
    provider_failed: bool,
    failure_kind: str,
    error: str,
) -> list[dict]:
    partial = [dict(record) for record in trace_state]
    active = next(
        (record for record in reversed(partial) if record.get("status") == "IN_PROGRESS"),
        None,
    )
    if active is None:
        active = {
            "schema_version": 2, "kind": "role_eval", "agent": "unknown",
            "stage": "unknown", "status": "IN_PROGRESS",
            "contract_passed": False, "initial_contract_passed": False,
            "handoff_integrity": {"passed": True, "required": []},
            "semantic_quality": {"passed": False, "score": 0.0, "checks": []},
            "attempt_count": 0, "provider_error_count": 0,
            "duration_ms": 0, "request_chars": 0, "response_chars": 0,
            "output": "", "messages": [], "final_messages": [],
        }
        partial.append(active)
    active.update(
        status=status,
        provider_failed=provider_failed,
        failure_kind=failure_kind,
        error=_redact_text(error)[:2000],
        provider_error_count=1 if provider_failed else 0,
    )
    return partial


def _partial_failure_trace(
    user_prompt: str,
    *,
    trace_state: list[dict],
    run_id: str,
    scenario_id: str,
    planning_only: bool,
    prompt_label: str,
    prompts_dir: str | Path | None,
    trace_budget_seconds: float,
    max_plan_revisions: int,
    status: str,
    provider_failed: bool,
    failure_kind: str,
    error: str,
    termination: str,
    termination_reason: str,
    exception: BaseException | None = None,
) -> dict:
    trace = _partial_trace(
        trace_state,
        status=status,
        provider_failed=provider_failed,
        failure_kind=failure_kind,
        error=error,
    )
    result = _trace_report(
        user_prompt, trace, {}, {}, run_id,
        planning_only=planning_only,
        scenario_id=scenario_id,
        termination=termination,
        termination_reason=termination_reason,
        trace_budget_seconds=trace_budget_seconds,
        max_plan_revisions=max_plan_revisions,
    )
    if termination == "HARNESS_FAILURE":
        source = exception or RuntimeError(error)
        result["failure_source"] = {
            "exception_type": type(source).__name__,
            "exception_message": str(source),
            "traceback": traceback.format_exc(),
        }
    return result


async def _bounded_evaluate_trace(user_prompt: str, **kwargs) -> dict:
    kwargs = dict(kwargs)
    max_plan_revisions = int(kwargs.get("max_plan_revisions", 2))
    planning_only = bool(kwargs.get("planning_only"))
    role_configs = kwargs.get("role_configs")
    trace_budget_seconds = _trace_budget_seconds(max_plan_revisions, planning_only, role_configs)
    trace_state = kwargs.pop("_trace_state", None)
    if trace_state is None:
        trace_state = []
    kwargs["_trace_state"] = trace_state
    kwargs["_trace_budget"] = trace_budget_seconds
    try:
        return await asyncio.wait_for(
            evaluate_trace(user_prompt, **kwargs),
            timeout=trace_budget_seconds,
        )
    except asyncio.TimeoutError as exc:
        return _partial_failure_trace(
            user_prompt,
            trace_state=trace_state,
            run_id=str(kwargs.get("run_id") or ""),
            scenario_id=str(kwargs.get("scenario_id") or ""),
            planning_only=planning_only,
            prompt_label=str(kwargs.get("prompt_label") or ""),
            prompts_dir=kwargs.get("prompts_dir"),
            trace_budget_seconds=trace_budget_seconds,
            max_plan_revisions=max_plan_revisions,
            status="PROVIDER_INCONCLUSIVE",
            provider_failed=True,
            failure_kind="provider_timeout",
            error=f"planning trace exceeded {trace_budget_seconds:g}s: {exc}",
            termination="PROVIDER_INCONCLUSIVE",
            termination_reason="provider_timeout",
        )
    except EvalProviderError as exc:
        return _partial_failure_trace(
            user_prompt,
            trace_state=trace_state,
            run_id=str(kwargs.get("run_id") or ""),
            scenario_id=str(kwargs.get("scenario_id") or ""),
            planning_only=planning_only,
            prompt_label=str(kwargs.get("prompt_label") or ""),
            prompts_dir=kwargs.get("prompts_dir"),
            trace_budget_seconds=trace_budget_seconds,
            max_plan_revisions=max_plan_revisions,
            status="PROVIDER_ERROR",
            provider_failed=True,
            failure_kind="provider_error",
            error=str(exc),
            termination="PROVIDER_INCONCLUSIVE",
            termination_reason="provider_error",
        )
    except asyncio.CancelledError as exc:
        return _partial_failure_trace(
            user_prompt,
            trace_state=trace_state,
            run_id=str(kwargs.get("run_id") or ""),
            scenario_id=str(kwargs.get("scenario_id") or ""),
            planning_only=planning_only,
            prompt_label=str(kwargs.get("prompt_label") or ""),
            prompts_dir=kwargs.get("prompts_dir"),
            trace_budget_seconds=trace_budget_seconds,
            max_plan_revisions=max_plan_revisions,
            status="HARNESS_FAILURE",
            provider_failed=False,
            failure_kind="harness_error",
            error=f"asyncio.CancelledError escaped budget mechanism: {exc}",
            exception=exc,
            termination="HARNESS_FAILURE",
            termination_reason="harness_failure",
        )
    except Exception as exc:
        return _partial_failure_trace(
            user_prompt,
            trace_state=trace_state,
            run_id=str(kwargs.get("run_id") or ""),
            scenario_id=str(kwargs.get("scenario_id") or ""),
            planning_only=planning_only,
            prompt_label=str(kwargs.get("prompt_label") or ""),
            prompts_dir=kwargs.get("prompts_dir"),
            trace_budget_seconds=trace_budget_seconds,
            max_plan_revisions=max_plan_revisions,
            status="HARNESS_FAILURE",
            provider_failed=False,
            failure_kind="harness_error",
            error=str(exc),
            exception=exc,
            termination="HARNESS_FAILURE",
            termination_reason="harness_failure",
        )


def compare_trace_reports(baseline: dict, candidate: dict) -> dict:
    """Compare two isolated traces without treating latency as quality."""
    baseline_summary = baseline.get("summary") or {}
    candidate_summary = candidate.get("summary") or {}

    def artifact_termination(artifact: dict) -> str:
        termination = str(artifact.get("termination") or "")
        if termination:
            return termination
        classification = str((artifact.get("readiness_gate") or {}).get("classification") or "")
        return {
            "harness-failure": "HARNESS_FAILURE",
            "provider-inconclusive": "PROVIDER_INCONCLUSIVE",
        }.get(classification, "")

    baseline_termination = artifact_termination(baseline)
    candidate_termination = artifact_termination(candidate)
    def metric_value(summary: dict, name: str) -> float:
        value = summary.get(name, 0) or 0
        if isinstance(value, dict):
            value = value.get("score", 0)
        return float(value or 0)

    metric_names = (
        "contract_validity", "handoff_integrity", "semantic_quality", "planning_quality",
        "latency_ms", "prompt_chars", "response_chars", "provider_failures",
    )
    deltas = {
        name: round(metric_value(candidate_summary, name) - metric_value(baseline_summary, name), 3)
        for name in metric_names
    }
    hard_regressions = []
    provider_inconclusive = []
    if baseline_summary.get("provider_failures") or baseline_termination == "PROVIDER_INCONCLUSIVE":
        provider_inconclusive.append("baseline: provider failure")
    if candidate_summary.get("provider_failures") or candidate_termination == "PROVIDER_INCONCLUSIVE":
        provider_inconclusive.append("candidate: provider failure")
    harness_failure = []
    if baseline_termination == "HARNESS_FAILURE":
        harness_failure.append("baseline: harness failure")
    if candidate_termination == "HARNESS_FAILURE":
        harness_failure.append("candidate: harness failure")
    baseline_prompt_versions = set(str(item) for item in (baseline_summary.get("prompt_versions") or []) if item)
    candidate_prompt_versions = set(str(item) for item in (candidate_summary.get("prompt_versions") or []) if item)
    prompt_versions_changed = bool(
        baseline_prompt_versions and candidate_prompt_versions
        and baseline_prompt_versions != candidate_prompt_versions
    )
    if not provider_inconclusive and (
        baseline_summary.get("prompt_labels")
        and candidate_summary.get("prompt_labels")
        and baseline_summary.get("prompt_labels") != candidate_summary.get("prompt_labels")
        and baseline_prompt_versions
        and candidate_prompt_versions
        and not prompt_versions_changed
    ):
        hard_regressions.append("prompt labels changed but prompt versions did not")
    baseline_handoff_modes = set(str(item) for item in (baseline_summary.get("handoff_modes") or []) if item)
    candidate_handoff_modes = set(str(item) for item in (candidate_summary.get("handoff_modes") or []) if item)
    if not provider_inconclusive and baseline_handoff_modes and candidate_handoff_modes and baseline_handoff_modes != candidate_handoff_modes:
        hard_regressions.append("handoff mode changed between baseline and candidate")
    baseline_models = baseline_summary.get("model_assignments") or {}
    candidate_models = candidate_summary.get("model_assignments") or {}
    if not provider_inconclusive and baseline_models and candidate_models and baseline_models != candidate_models:
        hard_regressions.append("model assignments changed between baseline and candidate")
    if not provider_inconclusive:
        for name in ("contract_validity", "handoff_integrity", "semantic_quality", "planning_quality"):
            if deltas[name] < 0:
                hard_regressions.append(f"{name} decreased")
    if not provider_inconclusive and baseline_summary.get("final_manager_verdict") == "APPROVED" and candidate_summary.get("final_manager_verdict") != "APPROVED":
        hard_regressions.append("approved Manager decision was lost")
    return {
        "schema_version": 1,
        "kind": "role_eval_comparison",
        "baseline_label": (baseline_summary.get("prompt_labels") or ["baseline"])[0],
        "candidate_label": (candidate_summary.get("prompt_labels") or ["candidate"])[0],
        "prompt_versions_changed": prompt_versions_changed,
        "deltas": deltas,
        "hard_regressions": hard_regressions,
        "provider_inconclusive": provider_inconclusive,
        "harness_failure": harness_failure,
        "termination": "HARNESS_FAILURE" if harness_failure else ("PROVIDER_INCONCLUSIVE" if provider_inconclusive else "COMPLETE"),
        "classification": "harness-failure" if harness_failure else ("provider-inconclusive" if provider_inconclusive else ("pass" if not hard_regressions else "failed")),
        "quality_improved": not harness_failure and not provider_inconclusive and not hard_regressions and any(
            deltas[name] > 0 for name in ("contract_validity", "handoff_integrity", "semantic_quality", "planning_quality")
        ),
        "candidate_readiness": not harness_failure and not provider_inconclusive and bool((candidate.get("readiness_gate") or {}).get("passed")) and not hard_regressions,
    }


def _manager_decision_record(stage: str, plan_version: str, data: dict) -> dict:
    """Keep the Manager's decision auditable without retaining unbounded prompt text."""
    issues = _jsonable(data.get("issues") or [])
    evidence_refs = sorted({
        str(issue.get("evidence") or "").strip()
        for issue in issues
        if isinstance(issue, dict) and str(issue.get("evidence") or "").strip()
    })
    return {
        "stage": stage,
        "plan_version": plan_version,
        "verdict": str(getattr(data.get("verdict"), "value", data.get("verdict") or "")),
        "confidence": data.get("confidence"),
        "summary": str(data.get("summary") or ""),
        "issues": issues,
        "evidence_refs": evidence_refs,
    }


def _suite_metric_average(cases: list[dict], metric: str) -> float:
    values = []
    for case in cases:
        value = (case.get("trace", {}).get("summary", {}) or {}).get(metric, 0)
        if isinstance(value, dict):
            value = value.get("score", 0)
        values.append(float(value or 0))
    return round(sum(values) / len(values), 3) if values else 0.0


def _expected_scenario_outcome(scenario: dict, trace: dict) -> tuple[bool, str]:
    """Return whether a trace met the scenario's declared terminal behavior."""
    terminal = str(scenario.get("terminal") or "").strip().lower()
    summary = trace.get("summary") or {}
    records = trace.get("trace") or []

    def valid_role(role: str) -> bool:
        return any(
            record.get("agent") == role
            and record.get("contract_passed")
            and (record.get("semantic_quality") or {}).get("passed")
            for record in records
        )

    if terminal == "clarification":
        actual = str(summary.get("termination_reason") or "")
        return actual == "clarification_required" and valid_role("chair"), actual
    if terminal == "manager_blocked":
        actual = str(summary.get("termination_reason") or "")
        return actual == "manager_blocked" and valid_role("manager"), actual
    expected_verdict = str(scenario.get("expected_final_verdict") or "").strip().upper()
    if expected_verdict:
        actual = str(summary.get("final_manager_verdict") or "").strip().upper()
        return actual == expected_verdict and valid_role("manager") and bool((trace.get("readiness_gate") or {}).get("passed")), actual
    passed = bool((trace.get("readiness_gate") or {}).get("passed"))
    return passed, "ready" if passed else str(summary.get("termination_reason") or "not_ready")


def _human_review_sample_ids(scenario_ids, sample_size: int, seed: str) -> list[str]:
    if sample_size < 0:
        raise RoleEvalError("human_review_sample_size must be zero or greater")
    ranked = sorted(
        (hashlib.sha256(f"{seed}:{scenario_id}".encode("utf-8")).hexdigest(), str(scenario_id))
        for scenario_id in scenario_ids
    )
    return [scenario_id for _, scenario_id in ranked[:sample_size]]


def _human_review_score(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 2) if 1 <= float(value) <= 5 else None
    normalized = str(value or "").strip().lower().replace(" ", "_")
    return float(_HUMAN_REVIEW_LABELS[normalized]) if normalized in _HUMAN_REVIEW_LABELS else None


def _human_review_report(
    scenario_ids,
    *,
    sample_size: int,
    seed: str,
    annotations: dict | None = None,
) -> dict:
    annotations = annotations if isinstance(annotations, dict) else {}
    sampled = _human_review_sample_ids(scenario_ids, sample_size, seed)
    reviews = []
    for scenario_id in sampled:
        supplied = annotations.get(scenario_id)
        supplied = supplied if isinstance(supplied, dict) else {}
        status = str(supplied.get("status") or "pending").strip().lower()
        if status not in {"pending", "reviewed"}:
            status = "pending"
        scores = {field: _human_review_score(supplied.get(field)) for field in _HUMAN_REVIEW_FIELDS}
        if status == "reviewed" and not all(score is not None for score in scores.values()):
            status = "pending"
        reviews.append({
            "scenario_id": scenario_id,
            "status": status,
            **scores,
            "comments": str(supplied.get("comments") or "")[:4000],
            "reviewer": str(supplied.get("reviewer") or "")[:200],
        })
    complete_reviews = [item for item in reviews if item["status"] == "reviewed"]
    score_summary = {
        field: round(sum(item[field] for item in complete_reviews) / len(complete_reviews), 2)
        if complete_reviews else None
        for field in _HUMAN_REVIEW_FIELDS
    }
    score_values = [value for value in score_summary.values() if value is not None]
    score_summary["overall"] = round(sum(score_values) / len(score_values), 2) if score_values else None
    return {
        "sampling_seed": seed,
        "score_scale": {"min": 1, "max": 5},
        "sample_size": len(reviews),
        "sampled_scenarios": sampled,
        "status": (
            "not_requested" if not reviews
            else "complete" if all(item["status"] == "reviewed" for item in reviews)
            else "pending"
        ),
        "reviews": reviews,
        "score_summary": score_summary,
    }


async def evaluate_scenario_suite(
    scenarios: dict,
    *,
    endpoint: str = "",
    model: str = "",
    role_configs: dict | None = None,
    api_key: str = "",
    workspace: str = "",
    run_id: str = "",
    max_plan_revisions: int = 2,
    planning_only: bool = True,
    prompt_label: str = "",
    handoff_mode: str = "contract",
    prompts_dir: str | Path | None = None,
    human_review_sample_size: int = 2,
    human_review_seed: str = "role-eval",
    human_review_annotations: dict | None = None,
    scenario_version: str = "",
    scenario_split: str = "all",
    repetitions: int = 1,
    checkpoint_path: str | Path | None = None,
    call=llm_call_async,
) -> dict:
    """Run every fixed controller scenario through isolated trace evaluation."""
    if not isinstance(scenarios, dict) or not scenarios:
        raise RoleEvalError("Scenario suite must contain at least one scenario.")
    if repetitions < 1:
        raise RoleEvalError("repetitions must be one or greater")
    run_id = run_id or f"role-eval-suite-{int(time.time() * 1000)}"
    benchmark_fingerprint = _benchmark_fingerprint(scenarios)
    cases = []
    trace_budget_seconds = _trace_budget_seconds(max_plan_revisions, planning_only, role_configs)
    stop_due_harness = False
    for repetition in range(1, repetitions + 1):
        for scenario_id, scenario in scenarios.items():
            if not isinstance(scenario, dict):
                raise RoleEvalError(f"Scenario {scenario_id!r} must be an object.")
            scenario_workspace = str(scenario.get("workspace_context") or workspace or "")
            trace = await _bounded_evaluate_trace(
                str(scenario.get("user_prompt") or ""),
                endpoint=endpoint,
                model=model,
                role_configs=role_configs,
                api_key=api_key,
                workspace=scenario_workspace,
                run_id=f"{run_id}:r{repetition}:{scenario_id}",
                max_plan_revisions=max_plan_revisions,
                planning_only=planning_only,
                scenario_id=str(scenario_id),
                scenario_rubric=scenario.get("planning_rubric"),
                prompt_label=prompt_label,
                handoff_mode=handoff_mode,
                prompts_dir=prompts_dir,
                call=call,
            )
            passed, actual_outcome = _expected_scenario_outcome(scenario, trace)
            cases.append({
                "scenario_id": str(scenario_id),
                "repetition": repetition,
                "passed": passed,
                "expected_terminal": str(scenario.get("terminal") or ""),
                "actual_outcome": actual_outcome,
                "provider_inconclusive": trace.get("termination") == "PROVIDER_INCONCLUSIVE" or bool((trace.get("summary") or {}).get("provider_failures")),
                "harness_failure": trace.get("termination") == "HARNESS_FAILURE",
                "trace": trace,
            })
            if checkpoint_path:
                _write_jsonl(Path(checkpoint_path), {
                    "schema_version": 2,
                    "kind": "role_eval_suite_progress",
                    "timestamp": time.time(),
                    "run_id": run_id,
                    "benchmark_fingerprint": benchmark_fingerprint,
                    "scenario_version": scenario_version or benchmark_fingerprint,
                    "scenario_split": scenario_split,
                    "scenario_count": len(scenarios),
                    "repetitions": repetitions,
                    "completed_cases": len(cases),
                    "total_cases": len(scenarios) * repetitions,
                    "planning_only": planning_only,
                    "max_plan_revisions": max_plan_revisions,
                    "trace_timeout_seconds": trace_budget_seconds,
                    "termination": trace.get("termination", "COMPLETE"),
                    "prompt_label": prompt_label,
                    "prompt_source": str(Path(prompts_dir).resolve()) if prompts_dir else "configured",
                    "cases": cases,
                })
            if trace.get("termination") == "HARNESS_FAILURE":
                stop_due_harness = True
                break
        if stop_due_harness:
            break

    scenario_runs: dict[str, list[dict]] = {}
    for case in cases:
        scenario_runs.setdefault(case["scenario_id"], []).append(case)
    unstable_scenarios = sorted(
        scenario_id
        for scenario_id, runs in scenario_runs.items()
        if len({
            (
                bool(case.get("passed")),
                bool(case.get("provider_inconclusive")),
                str(case.get("actual_outcome") or ""),
                str((case.get("trace", {}).get("summary", {}) or {}).get("final_manager_verdict") or ""),
            )
            for case in runs
        }) > 1
    )

    summary = {
        "scenario_count": len(scenarios),
        "repetition_count": repetitions,
        "unstable_scenarios": unstable_scenarios,
        "case_count": len(cases),
        "termination": "HARNESS_FAILURE" if stop_due_harness else ("PROVIDER_INCONCLUSIVE" if any(case.get("provider_inconclusive") for case in cases) else "COMPLETE"),
        "harness_failure_cases": [case["scenario_id"] for case in cases if case.get("harness_failure")],
        "trace_budget_seconds": trace_budget_seconds,
        "passed_cases": sum(bool(case.get("passed")) for case in cases),
        "failed_cases": [
            case["scenario_id"] for case in cases
            if not case.get("passed") and not case.get("provider_inconclusive")
        ],
        "provider_inconclusive_cases": [
            case["scenario_id"] for case in cases if case.get("provider_inconclusive")
        ],
        "pass_rate": round(sum(bool(case.get("passed")) for case in cases) / len(cases), 3),
        "contract_validity": _suite_metric_average(cases, "contract_validity"),
        "handoff_integrity": _suite_metric_average(cases, "handoff_integrity"),
        "semantic_quality": _suite_metric_average(cases, "semantic_quality"),
        "latency_ms": sum(int((case.get("trace", {}).get("summary", {}) or {}).get("latency_ms", 0) or 0) for case in cases),
        "prompt_chars": sum(int((case.get("trace", {}).get("summary", {}) or {}).get("prompt_chars", 0) or 0) for case in cases),
        "response_chars": sum(int((case.get("trace", {}).get("summary", {}) or {}).get("response_chars", 0) or 0) for case in cases),
        "handoff_chars": sum(int((case.get("trace", {}).get("summary", {}) or {}).get("handoff_chars", 0) or 0) for case in cases),
        "handoff_source_chars": sum(int((case.get("trace", {}).get("summary", {}) or {}).get("handoff_source_chars", 0) or 0) for case in cases),
        "provider_failures": sum(int((case.get("trace", {}).get("summary", {}) or {}).get("provider_failures", 0) or 0) for case in cases),
        "provider_errors": sum(int((case.get("trace", {}).get("summary", {}) or {}).get("provider_errors", 0) or 0) for case in cases),
        "provider_retry_recoveries": sum(int((case.get("trace", {}).get("summary", {}) or {}).get("provider_retry_recoveries", 0) or 0) for case in cases),
        "schema_repair_attempts": sum(int((case.get("trace", {}).get("summary", {}) or {}).get("schema_repair_attempts", 0) or 0) for case in cases),
        "garbage_recovery_attempts": sum(int((case.get("trace", {}).get("summary", {}) or {}).get("garbage_recovery_attempts", 0) or 0) for case in cases),
        "human_escalations": sum(bool((case.get("trace", {}).get("summary", {}) or {}).get("human_escalation_required")) for case in cases),
        "plan_revision_attempts": sum(int((case.get("trace", {}).get("summary", {}) or {}).get("plan_revision_attempts", 0) or 0) for case in cases),
        "perspective_rechecks": sum(int((case.get("trace", {}).get("summary", {}) or {}).get("perspective_rechecks", 0) or 0) for case in cases),
        "planning_quality": _suite_metric_average(cases, "planning_quality"),
        "prompt_labels": sorted({
            label
            for case in cases
            for label in ((case.get("trace", {}).get("summary", {}) or {}).get("prompt_labels") or [])
        }),
        "prompt_sources": sorted({
            source
            for case in cases
            for source in ((case.get("trace", {}).get("summary", {}) or {}).get("prompt_sources") or [])
        }),
        "prompt_versions": sorted({
            version
            for case in cases
            for version in ((case.get("trace", {}).get("summary", {}) or {}).get("prompt_versions") or [])
        }),
        "handoff_modes": sorted({
            mode
            for case in cases
            for mode in ((case.get("trace", {}).get("summary", {}) or {}).get("handoff_modes") or [])
        }),
        "model_assignments": {
            agent: model_name
            for case in cases
            for agent, model_name in ((case.get("trace", {}).get("summary", {}) or {}).get("model_assignments") or {}).items()
        },
        "handoff_compression_ratio": round(
            sum(int((case.get("trace", {}).get("summary", {}) or {}).get("handoff_chars", 0) or 0) for case in cases)
            / max(1, sum(int((case.get("trace", {}).get("summary", {}) or {}).get("handoff_source_chars", 0) or 0) for case in cases)),
            3,
        ),
    }
    failures = [
        f"{case['scenario_id']}: {case.get('actual_outcome') or 'scenario expectation failed'}"
        for case in cases if not case.get("passed") and not case.get("provider_inconclusive")
    ]
    provider_inconclusive_cases = [
        f"{case['scenario_id']}: provider failure"
        for case in cases if case.get("provider_inconclusive")
    ]
    human_review = _human_review_report(
        [case["scenario_id"] for case in cases],
        sample_size=human_review_sample_size,
        seed=human_review_seed,
        annotations=human_review_annotations,
    )
    summary["human_review_status"] = human_review["status"]
    summary["human_review_sample_count"] = human_review["sample_size"]
    suite_termination = "HARNESS_FAILURE" if stop_due_harness else ("PROVIDER_INCONCLUSIVE" if provider_inconclusive_cases else "COMPLETE")
    provider_status = "harness-failure" if stop_due_harness else ("provider-inconclusive" if provider_inconclusive_cases else ("pass" if not failures else "failed"))
    planning_roles = ("chair", "strategist", "perspective_analyzer", "manager")
    role_settings = {}
    for role in planning_roles:
        role_endpoint, role_model, temperature, max_tokens, role_timeout = _role_config(
            role, role_configs, endpoint, model
        )
        role_settings[role] = {
            "endpoint": _endpoint_label(role_endpoint),
            "model": role_model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "timeout": role_timeout,
        }
    timestamp = time.time()
    return {
        "schema_version": 2,
        "kind": "role_eval_suite",
        "timestamp": timestamp,
        "run_id": run_id,
        "benchmark_fingerprint": benchmark_fingerprint,
        "scenario_version": scenario_version or benchmark_fingerprint,
        "scenario_split": scenario_split,
        "scenario_count": len(scenarios),
        "repetitions": repetitions,
        "planning_only": planning_only,
        "max_plan_revisions": max_plan_revisions,
        "trace_timeout_seconds": trace_budget_seconds,
        "trace_budget_seconds": trace_budget_seconds,
        "trace_stage_count": _trace_stage_count(max_plan_revisions, planning_only),
        "max_provider_calls_per_stage": _max_provider_calls_per_stage(),
        "termination": suite_termination,
        "handoff_mode": _normalize_handoff_mode(handoff_mode),
        "cases": cases,
        "summary": summary,
        "human_review": human_review,
        "run_manifest": {
            "timestamp": timestamp,
            "run_id": run_id,
            "scenario_version": scenario_version or benchmark_fingerprint,
            "scenario_split": scenario_split,
            "repetitions": repetitions,
            "benchmark_fingerprint": benchmark_fingerprint,
            "role_settings": role_settings,
            "prompt_label": prompt_label,
            "prompt_source": str(Path(prompts_dir).resolve()) if prompts_dir else "configured",
            "prompt_hashes": summary["prompt_versions"],
            "handoff_mode": _normalize_handoff_mode(handoff_mode),
            "planning_only": planning_only,
            "max_plan_revisions": max_plan_revisions,
            "provider_retry_limit": _PROVIDER_RETRY_LIMIT,
            "provider_call_max_retries": _PROVIDER_CALL_MAX_RETRIES,
            "provider_call_timeout_seconds": _PROVIDER_CALL_TIMEOUT,
            "model_fallbacks": 0,
            "trace_timeout_seconds": trace_budget_seconds,
            "trace_budget_seconds": trace_budget_seconds,
            "trace_budget_formula": _trace_budget_formula(planning_only, role_configs),
            "trace_stage_count": _trace_stage_count(max_plan_revisions, planning_only),
            "max_provider_calls_per_stage": _max_provider_calls_per_stage(),
            "schema_repair_limit": _MAX_SCHEMA_REPAIRS,
            "provider_status": provider_status,
        },
        "readiness_gate": {
            "passed": suite_termination == "COMPLETE" and not failures and not provider_inconclusive_cases,
            "failures": failures,
            "provider_inconclusive": provider_inconclusive_cases,
            "classification": provider_status,
        },
    }


def _suite_case_key(case: dict) -> str:
    scenario_id = str(case.get("scenario_id") or "")
    repetition = case.get("repetition")
    return f"{scenario_id}::r{repetition}" if repetition is not None else scenario_id


def compare_trace_suites(baseline: dict, candidate: dict) -> dict:
    """Compare matching scenarios and surface both quality and coverage regressions."""
    baseline_cases = {
        _suite_case_key(case): case
        for case in (baseline.get("cases") or [])
    }
    candidate_cases = {
        _suite_case_key(case): case
        for case in (candidate.get("cases") or [])
    }
    comparisons = []
    hard_regressions = []
    provider_inconclusive = []
    harness_failure = []
    for scenario_id in sorted(set(baseline_cases) | set(candidate_cases)):
        before = baseline_cases.get(scenario_id)
        after = candidate_cases.get(scenario_id)
        if before is None or after is None:
            hard_regressions.append(f"scenario coverage changed: {scenario_id}")
            continue
        comparison = compare_trace_reports(before.get("trace") or {}, after.get("trace") or {})
        comparison["scenario_id"] = scenario_id
        comparison["expected_outcome_preserved"] = bool(before.get("passed")) == bool(after.get("passed")) or bool(after.get("passed"))
        after_provider_inconclusive = bool(after.get("provider_inconclusive")) or bool(
            ((after.get("trace") or {}).get("summary") or {}).get("provider_failures")
        )
        if before.get("passed") and not after.get("passed") and not after_provider_inconclusive:
            hard_regressions.append(f"scenario expectation failed: {scenario_id}")
        hard_regressions.extend(f"{scenario_id}: {item}" for item in comparison.get("hard_regressions", []))
        provider_inconclusive.extend(f"{scenario_id}: {item}" for item in comparison.get("provider_inconclusive", []))
        harness_failure.extend(f"{scenario_id}: {item}" for item in comparison.get("harness_failure", []))
        comparisons.append(comparison)

    baseline_summary = baseline.get("summary") or {}
    candidate_summary = candidate.get("summary") or {}
    if baseline_summary.get("provider_failures"):
        provider_inconclusive.append("suite baseline: provider failure")
    if candidate_summary.get("provider_failures"):
        provider_inconclusive.append("suite candidate: provider failure")
    for label, artifact in (("baseline", baseline), ("candidate", candidate)):
        termination = str(artifact.get("termination") or "")
        classification = str((artifact.get("readiness_gate") or {}).get("classification") or "")
        if termination == "HARNESS_FAILURE" or classification == "harness-failure":
            harness_failure.append(f"suite {label}: harness failure")
        if termination == "PROVIDER_INCONCLUSIVE" or classification == "provider-inconclusive":
            provider_inconclusive.append(f"suite {label}: provider failure")
    comparison_warnings = []
    baseline_fingerprint = str(baseline.get("benchmark_fingerprint") or "")
    candidate_fingerprint = str(candidate.get("benchmark_fingerprint") or "")
    if baseline_fingerprint and candidate_fingerprint:
        if baseline_fingerprint != candidate_fingerprint:
            hard_regressions.append("benchmark fingerprint changed between baseline and candidate")
    else:
        comparison_warnings.append("benchmark fingerprint missing from one or both artifacts")
    baseline_planning_only = baseline.get("planning_only")
    candidate_planning_only = candidate.get("planning_only")
    if baseline_planning_only is not None and candidate_planning_only is not None:
        if bool(baseline_planning_only) != bool(candidate_planning_only):
            hard_regressions.append("planning-only mode changed between baseline and candidate")
    else:
        comparison_warnings.append("planning-only mode missing from one or both artifacts")
    baseline_revision_budget = baseline.get("max_plan_revisions")
    candidate_revision_budget = candidate.get("max_plan_revisions")
    if baseline_revision_budget is not None and candidate_revision_budget is not None:
        if int(baseline_revision_budget) != int(candidate_revision_budget):
            hard_regressions.append("plan revision budget changed between baseline and candidate")
    else:
        comparison_warnings.append("plan revision budget missing from one or both artifacts")
    baseline_repetitions = int(baseline.get("repetitions", 1) or 1)
    candidate_repetitions = int(candidate.get("repetitions", 1) or 1)
    if baseline_repetitions != candidate_repetitions:
        hard_regressions.append("suite repetition count changed between baseline and candidate")
    aggregate_metrics = (
        "contract_validity", "handoff_integrity", "semantic_quality", "planning_quality",
        "latency_ms", "prompt_chars", "response_chars", "handoff_chars",
        "handoff_source_chars", "handoff_compression_ratio", "provider_failures",
    )
    deltas = {
        metric: round(float(candidate_summary.get(metric, 0) or 0) - float(baseline_summary.get(metric, 0) or 0), 3)
        for metric in aggregate_metrics
    }
    baseline_prompt_versions = set(str(item) for item in (baseline_summary.get("prompt_versions") or []) if item)
    candidate_prompt_versions = set(str(item) for item in (candidate_summary.get("prompt_versions") or []) if item)
    prompt_versions_changed = bool(
        baseline_prompt_versions and candidate_prompt_versions
        and baseline_prompt_versions != candidate_prompt_versions
    )
    if (
        baseline_summary.get("prompt_labels")
        and candidate_summary.get("prompt_labels")
        and baseline_summary.get("prompt_labels") != candidate_summary.get("prompt_labels")
        and baseline_prompt_versions
        and candidate_prompt_versions
        and not prompt_versions_changed
    ):
        hard_regressions.append("prompt labels changed but prompt versions did not")
    baseline_handoff_mode = str(baseline.get("handoff_mode") or "")
    candidate_handoff_mode = str(candidate.get("handoff_mode") or "")
    if baseline_handoff_mode and candidate_handoff_mode and baseline_handoff_mode != candidate_handoff_mode:
        hard_regressions.append("handoff mode changed between baseline and candidate")
    baseline_models = baseline_summary.get("model_assignments") or {}
    candidate_models = candidate_summary.get("model_assignments") or {}
    if baseline_models and candidate_models and baseline_models != candidate_models:
        hard_regressions.append("model assignments changed between baseline and candidate")
    candidate_human_review = candidate.get("human_review") or {}
    if (
        isinstance(candidate_human_review, dict)
        and int(candidate_human_review.get("sample_size", 0) or 0) > 0
        and candidate_human_review.get("status") != "complete"
    ):
        hard_regressions.append("human review pending")
    baseline_human_review = baseline.get("human_review") or {}
    baseline_review_seed = str(baseline_human_review.get("sampling_seed") or "")
    candidate_review_seed = str(candidate_human_review.get("sampling_seed") or "")
    if baseline_review_seed and candidate_review_seed and baseline_review_seed != candidate_review_seed:
        hard_regressions.append("human review sampling seed changed between baseline and candidate")
    elif not baseline_review_seed or not candidate_review_seed:
        comparison_warnings.append("human review sampling seed missing from one or both artifacts")
    baseline_review_size = baseline_human_review.get("sample_size")
    candidate_review_size = candidate_human_review.get("sample_size")
    if baseline_review_size is not None and candidate_review_size is not None:
        if int(baseline_review_size or 0) != int(candidate_review_size or 0):
            hard_regressions.append("human review sample size changed between baseline and candidate")
    elif baseline_review_size is None or candidate_review_size is None:
        comparison_warnings.append("human review sample size missing from one or both artifacts")
    baseline_scores = baseline_human_review.get("score_summary") or {}
    candidate_scores = candidate_human_review.get("score_summary") or {}
    human_review_deltas = {}
    if (
        baseline_human_review.get("status") == "complete"
        and candidate_human_review.get("status") == "complete"
    ):
        for field in (*_HUMAN_REVIEW_FIELDS, "overall"):
            before = baseline_scores.get(field)
            after = candidate_scores.get(field)
            if before is None or after is None:
                continue
            human_review_deltas[field] = round(float(after) - float(before), 2)
            if human_review_deltas[field] < 0:
                hard_regressions.append(f"human review {field} decreased")
    return {
        "schema_version": 1,
        "kind": "role_eval_suite_comparison",
        "baseline_label": ((baseline_summary.get("prompt_labels") or ["baseline"])[0]),
        "candidate_label": ((candidate_summary.get("prompt_labels") or ["candidate"])[0]),
        "benchmark_fingerprints": {
            "baseline": baseline_fingerprint,
            "candidate": candidate_fingerprint,
        },
        "comparison_warnings": sorted(set(comparison_warnings)),
        "prompt_versions_changed": prompt_versions_changed,
        "human_review_status": candidate_human_review.get("status", "not_recorded"),
        "human_review_deltas": human_review_deltas,
        "case_comparisons": comparisons,
        "deltas": deltas,
        "hard_regressions": sorted(set(hard_regressions)),
        "provider_inconclusive": sorted(set(provider_inconclusive)),
        "harness_failure": sorted(set(harness_failure)),
        "termination": "HARNESS_FAILURE" if harness_failure else ("PROVIDER_INCONCLUSIVE" if provider_inconclusive else "COMPLETE"),
        "classification": "harness-failure" if harness_failure else ("provider-inconclusive" if provider_inconclusive else ("pass" if not hard_regressions else "failed")),
        "candidate_readiness": not harness_failure and not provider_inconclusive and bool((candidate.get("readiness_gate") or {}).get("passed")) and not hard_regressions,
        "quality_improved": not harness_failure and not provider_inconclusive and not hard_regressions and any(
            deltas[name] > 0 for name in ("contract_validity", "handoff_integrity", "semantic_quality", "planning_quality")
        ),
    }


def _read_prompt(args, scenario: dict | None = None) -> str:
    if scenario is not None:
        if args.user or args.user_file:
            raise RoleEvalError("Use --scenario by itself, or use --user/--user-file.")
        return str(scenario.get("user_prompt") or "")
    if bool(args.user) == bool(args.user_file):
        raise RoleEvalError("Use exactly one of --user or --user-file.")
    return args.user if args.user else args.user_file.read_text(encoding="utf-8")


def _previous_output(path: Path) -> str:
    record = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
    return str(record.get("output") or "")


def _load_role_configs(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload.get("roles", payload)


def _load_scenarios(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RoleEvalError("Scenario file must contain a JSON object.")
    return payload


def _read_checkpoint(path: Path | None) -> dict | None:
    if not path or not path.exists():
        return None
    try:
        records = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return json.loads(records[-1]) if records else None
    except Exception:
        return None


def _harness_failure_artifact(
    kind: str,
    exc: BaseException,
    *,
    run_id: str = "",
    scenario_id: str = "",
    partial_trace=None,
    checkpoint=None,
) -> dict:
    return {
        "schema_version": 2,
        "kind": kind,
        "timestamp": time.time(),
        "run_id": run_id,
        "scenario_id": scenario_id,
        "termination": "HARNESS_FAILURE",
        "failure_source": {
            "exception_type": type(exc).__name__,
            "exception_message": str(exc),
            "traceback": traceback.format_exc(),
        },
        "partial_trace": partial_trace or [],
        "checkpoint": checkpoint,
        "summary": {
            "termination": "HARNESS_FAILURE",
            "termination_reason": "harness_failure",
            "provider_failures": 0,
        },
        "readiness_gate": {
            "passed": False,
            "failures": ["harness failure"],
            "provider_inconclusive": [],
            "classification": "harness-failure",
        },
    }


def _exit_code(result: dict) -> int:
    termination = str(result.get("termination") or "")
    if termination == "HARNESS_FAILURE":
        return 2
    if termination == "PROVIDER_INCONCLUSIVE":
        return 1
    if result.get("provider_failed"):
        return 1
    outcome = result.get("scenario_outcome")
    if outcome is not None:
        return 0 if outcome.get("passed") else 1
    if "hard_regressions" in result:
        return 0 if not result.get("hard_regressions") else 1
    if "contract_passed" in result:
        return 0 if result.get("contract_passed") else 1
    gate = result.get("readiness_gate") or {}
    if gate.get("classification") in {"provider-inconclusive", "harness-failure"}:
        return 2 if gate.get("classification") == "harness-failure" else 1
    return 0 if gate.get("passed") else 1


def _write_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, default=str) + "\n", encoding="utf-8")


def _write_artifact(path: Path, record: dict, *, jsonl: bool = False) -> bool:
    try:
        if jsonl:
            _write_jsonl(path, record)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(record, ensure_ascii=False, indent=2, default=str) + "\n",
                encoding="utf-8",
            )
        return True
    except Exception as exc:
        print(f"FATAL: Could not write evaluation artifact {path}: {exc}", file=sys.stderr)
        return False


def _run_async_or_harness_failure(
    awaitable,
    *,
    kind: str,
    run_id: str,
    scenario_id: str = "",
    checkpoint_path: Path | None = None,
) -> dict | None:
    try:
        return asyncio.run(awaitable)
    except KeyboardInterrupt:
        print("Interrupted by user.", file=sys.stderr)
        return None
    except asyncio.CancelledError as exc:
        checkpoint = _read_checkpoint(checkpoint_path)
        return _harness_failure_artifact(
            kind,
            exc,
            run_id=run_id,
            scenario_id=scenario_id,
            partial_trace=(checkpoint or {}).get("cases", []),
            checkpoint=checkpoint,
        )
    except Exception as exc:
        checkpoint = _read_checkpoint(checkpoint_path)
        return _harness_failure_artifact(
            kind,
            exc,
            run_id=run_id,
            scenario_id=scenario_id,
            partial_trace=(checkpoint or {}).get("cases", []),
            checkpoint=checkpoint,
        )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", action="store_true", help="Run the six-role isolated workflow (primary readiness gate).")
    parser.add_argument("--planning-only", action="store_true", help="Stop after an approved Manager plan; skip Implementer and Auditor stages.")
    parser.add_argument("--plan-revisions", type=int, default=2, help="Normal plan revision budget for the trace (default: 2).")
    parser.add_argument("--handoff-mode", choices=sorted(_HANDOFF_MODES), default="contract", help="Model-facing handoff: compact validated contract or raw output.")
    parser.add_argument("--prompt-label", default="current", help="Label recorded with the prompt hash for baseline/candidate comparisons.")
    parser.add_argument("--prompts-dir", type=Path, help="Optional prompt directory used for this run; enables causal baseline/candidate comparisons.")
    parser.add_argument("--human-review-sample-size", type=int, default=2, help="Stable fixed-suite sample count for human calibration (default: 2).")
    parser.add_argument("--human-review-json", type=Path, help="Optional sampled-review JSON keyed by scenario id; score plan_quality, manager_correctness, and handoff_quality from 1-5.")
    parser.add_argument("--repetitions", type=int, default=3, help="Runs per scenario for a fixed suite (default: 3). Increase to 5 for unstable results.")
    parser.add_argument("--agent", choices=ROLE_ORDER)
    parser.add_argument("--user")
    parser.add_argument("--user-file", type=Path)
    parser.add_argument("--workspace", default="")
    parser.add_argument("--endpoint")
    parser.add_argument("--model")
    parser.add_argument("--timeout", type=float, help="Per-provider-attempt timeout for single-role evaluation.")
    parser.add_argument(
        "--models-config", type=Path,
        default=Path(__file__).resolve().parents[1] / "config" / "models.json",
    )
    parser.add_argument("--api-key-env", default="DIRECT_MODEL_API_KEY")
    parser.add_argument("--use-configured-credentials", action="store_true", help="Opt in to the configured Odysseus default endpoint credential when the API-key environment variable is unset; never prints or persists it.")
    parser.add_argument("--case", default="manual")
    parser.add_argument("--scenario", help="Run a named controller scenario, or 'all' for the fixed suite.")
    parser.add_argument("--scenario-split", choices=("all", "tuning", "holdout"), default="all", help="Run only the fixed tuning or holdout partition.")
    parser.add_argument(
        "--scenarios-config", type=Path,
        default=Path(__file__).resolve().parents[1] / "benchmarks" / "role_eval_scenarios_p2_1.json",
    )
    parser.add_argument("--trace-out", type=Path)
    parser.add_argument("--compare-baseline", type=Path, help="Compare a baseline suite JSON artifact.")
    parser.add_argument("--compare-candidate", type=Path, help="Compare a candidate suite JSON artifact.")
    parser.add_argument("--chair-result", type=Path)
    parser.add_argument("--strategist-result", type=Path)
    parser.add_argument("--perspective-result", type=Path)
    parser.add_argument("--manager-result", type=Path)
    parser.add_argument("--implementer-result", type=Path)
    args = parser.parse_args(argv)
    if args.compare_baseline or args.compare_candidate:
        if not args.compare_baseline or not args.compare_candidate:
            parser.error("--compare-baseline and --compare-candidate must be supplied together.")
        output = args.trace_out or Path("data/council_agent_evals/role-eval-comparison.json")
        try:
            baseline = json.loads(args.compare_baseline.read_text(encoding="utf-8"))
            candidate = json.loads(args.compare_candidate.read_text(encoding="utf-8"))
            result = compare_trace_suites(baseline, candidate)
        except KeyboardInterrupt:
            print("Interrupted by user.", file=sys.stderr)
            return 130
        except Exception as exc:
            result = _harness_failure_artifact(
                "role_eval_comparison", exc, run_id=args.case,
            )
        if not _write_artifact(output, result):
            return 2
        print(json.dumps(result, ensure_ascii=False))
        return _exit_code(result)
    scenario = None
    scenario_suite = None
    scenario_version = ""
    if args.scenario:
        scenarios = _load_scenarios(args.scenarios_config)
        scenario_version = _benchmark_fingerprint(scenarios)
        if args.scenario.lower() == "all":
            scenario_suite = {
                scenario_id: value
                for scenario_id, value in scenarios.items()
                if args.scenario_split == "all" or value.get("split") == args.scenario_split
            }
            if not scenario_suite:
                parser.error(f"No scenarios in split: {args.scenario_split}")
        else:
            scenario = scenarios.get(args.scenario)
        if scenario is None and scenario_suite is None:
            parser.error(f"Unknown scenario: {args.scenario}")
    if scenario_suite is not None:
        if not args.trace:
            parser.error("--scenario all requires --trace.")
        if args.user or args.user_file:
            parser.error("--scenario all uses the prompts in the scenario file; omit --user/--user-file.")
        user_prompt = ""
    else:
        user_prompt = _read_prompt(args, scenario)
    api_key = os.environ.get(args.api_key_env, "")
    if not api_key and args.use_configured_credentials:
        api_key = _configured_api_key(args.endpoint or "")
    if not api_key and not args.trace and args.endpoint:
        # Keep isolated role evaluations on the same endpoint-key resolution
        # path as full traces and the UI orchestrator.
        api_key = _resolve_api_key_for_endpoints({args.endpoint})
    human_review_annotations = _load_scenarios(args.human_review_json) if args.human_review_json else None

    if args.trace:
        role_configs = _load_role_configs(args.models_config) if args.models_config else None
        # Resolve an API key from any known endpoint in the role configs so
        # the eval harness picks up the same DB-stored credential the UI
        # orchestrator uses.  This matches the orchestrator's _resolve_headers()
        # pattern and means --use-configured-credentials is no longer required
        # for eval runs against a known endpoint.
        if not api_key and role_configs:
            endpoints = set()
            for role in ("chair", "strategist", "perspective_analyzer", "manager",
                         "implementer", "completeness_auditor"):
                ep, _, _, _, _ = _role_config(role, role_configs, args.endpoint or "", args.model or "")
                if ep:
                    endpoints.add(ep)
            api_key = _resolve_api_key_for_endpoints(endpoints)
        if scenario_suite is not None:
            checkpoint_path = Path(str(args.trace_out or (Path("data/council_agent_evals") / f"{args.case}-suite.json")) + ".progress.json")
            result = _run_async_or_harness_failure(evaluate_scenario_suite(
                scenario_suite, endpoint=args.endpoint or "", model=args.model or "",
                role_configs=role_configs, api_key=api_key, workspace=args.workspace,
                run_id=args.case, max_plan_revisions=args.plan_revisions,
                planning_only=True, prompt_label=args.prompt_label, handoff_mode=args.handoff_mode,
                prompts_dir=args.prompts_dir,
                human_review_sample_size=args.human_review_sample_size,
                human_review_annotations=human_review_annotations,
                scenario_version=scenario_version,
                scenario_split=args.scenario_split,
                repetitions=args.repetitions,
                checkpoint_path=checkpoint_path,
            ), kind="role_eval_suite", run_id=args.case, checkpoint_path=checkpoint_path)
            output = args.trace_out or (Path("data/council_agent_evals") / f"{args.case}-suite.json")
        else:
            result = _run_async_or_harness_failure(_bounded_evaluate_trace(
                user_prompt, endpoint=args.endpoint or "", model=args.model or "",
                role_configs=role_configs, api_key=api_key, workspace=args.workspace,
                run_id=args.case,
                max_plan_revisions=args.plan_revisions, planning_only=args.planning_only,
                scenario_id=args.scenario or "",
                scenario_rubric=(scenario or {}).get("planning_rubric"),
                prompt_label=args.prompt_label, handoff_mode=args.handoff_mode,
                prompts_dir=args.prompts_dir,
            ), kind="role_eval_trace", run_id=args.case, scenario_id=args.scenario or "")
            output = args.trace_out or (Path("data/council_agent_evals") / f"{args.case}-trace.json")
        if result is None:
            return 130
        if scenario is not None:
            outcome_passed, actual_outcome = _expected_scenario_outcome(scenario, result)
            result["scenario_outcome"] = {
                "passed": outcome_passed,
                "expected_terminal": str(scenario.get("terminal") or ""),
                "actual_outcome": actual_outcome,
            }
        if not _write_artifact(output, result):
            return 2
        print(json.dumps(result.get("summary") or result, ensure_ascii=False))
        return _exit_code(result)

    if not args.agent or not args.endpoint or not args.model:
        parser.error("single-role evaluation requires --agent, --endpoint, and --model; use --trace for the full workflow")
    chair_reply = _previous_output(args.chair_result) if args.chair_result else ""
    strategist_reply = _previous_output(args.strategist_result) if args.strategist_result else ""
    perspective_reply = _previous_output(args.perspective_result) if args.perspective_result else ""
    manager_reply = _previous_output(args.manager_result) if args.manager_result else ""
    implementer_reply = _previous_output(args.implementer_result) if args.implementer_result else ""
    suffix = "strategist-revision" if args.agent == "strategist" and manager_reply else args.agent
    output = Path("data/council_agent_evals") / f"{args.case}-{suffix}.jsonl"
    try:
        result = asyncio.run(evaluate(
            args.agent, user_prompt, endpoint=args.endpoint, model=args.model,
            api_key=api_key, workspace=args.workspace, chair_reply=chair_reply,
            strategist_reply=strategist_reply, perspective_reply=perspective_reply,
            manager_reply=manager_reply, implementer_reply=implementer_reply,
            handoff_mode=args.handoff_mode, prompts_dir=args.prompts_dir,
            prompt_label=args.prompt_label, run_id=args.case, timeout=args.timeout,
        ))
    except KeyboardInterrupt:
        print("Interrupted by user.", file=sys.stderr)
        return 130
    except Exception as exc:
        result = _harness_failure_artifact(
            "role_eval", exc, run_id=args.case, scenario_id=args.scenario or "",
        )
    if not _write_artifact(output, result, jsonl=True):
        return 2
    print(json.dumps({key: result.get(key) for key in ("agent", "model", "contract_passed", "duration_ms", "failure_kind")}))
    return _exit_code(result)


if __name__ == "__main__":
    raise SystemExit(main())