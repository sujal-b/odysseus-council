"""Unified Agent Execution Runner for Odysseus Council of Agents.
Handles schema validation, retry backoffs, context budgets, and streaming extracts.
"""
import asyncio
import copy
import hashlib
import json
import logging
import time
from typing import Any, List, Dict, Optional
from council_of_agents.scripts.council_schemas import SCHEMA_MAP, validate_agent_output
from council_of_agents.scripts.council_retry import (
    retry_with_backoff,
    SchemaValidationError,
    ErrorClass,
    classify_error,
)
from council_of_agents.scripts.council_recovery import classify_failure
from council_of_agents.scripts.context_tracker import ContextBudgetExceededError
from src.model_context import estimate_tokens

logger = logging.getLogger(__name__)


def _strategist_plan_error(tasks, user_prompt) -> str | None:
    """Return the existing DAG/policy error before a plan reaches Manager."""
    from council_of_agents.scripts.task_dag import TaskDAG, mutation_only_plan_error
    error = mutation_only_plan_error(tasks, user_prompt)
    if error:
        return error
    try:
        TaskDAG.from_task_list(tasks or []).validate_contracts()
    except ValueError as exc:
        return str(exc)
    return None


def _output_diagnostic(error: SchemaValidationError) -> dict:
    raw = str(getattr(error, 'raw_text', '') or '')
    return {
        "output_chars": len(raw),
        "output_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "schema_error": str(getattr(error, 'validation_error', '') or str(error))[:1000],
    }


_REPAIR_CONTRACTS = {
    "chair": '{"complexity":"SIMPLE|MEDIUM|COMPLEX","route":"DIRECT|PIPELINE","action":"read|write|search|command|analyze|unknown","target":"...","reason":"..."}',
    "strategist": '{"tasks":[{"id":"T1","description":"...","depends_on":[],"acceptance":"...","write_scope":["src/"]}],"risks":[]}',
    "manager": '{"verdict":"APPROVED|REVISE|BLOCKED","confidence":0.0,"summary":"...","issues":[]}',
    "perspective_analyzer": '{"security":{"score":0.0,"issues":[]},"performance":{"score":0.0,"issues":[]},"maintainability":{"score":0.0,"issues":[]},"overall_score":0.0,"synthesis":"..."}',
    "completeness_auditor": '{"completeness":0.0,"done":false,"criteria":[]}',
}


def _repair_context(messages: List[Dict[str, str]], *, limit: int = 12000) -> str:
    """Keep only bounded, non-tool context for a schema repair attempt."""
    selected = []
    used = 0
    # The latest decision evidence is more useful than an old system prompt;
    # the repair system message already supplies the output contract. Walk
    # backwards, then restore chronological order for the model.
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "unknown")
        if role == "system" or role == "tool" or message.get("tool_calls") or message.get("tool_call_id"):
            continue
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        remaining = limit - used
        if remaining <= 0:
            break
        text = content.strip()[:min(remaining, 4000)]
        selected.append(f"[{role}]\n{text}")
        used += len(text)
    selected.reverse()
    return "\n\n".join(selected)


def _schema_repair_messages(
    messages: List[Dict[str, str]],
    validation_role: str,
    error: str,
    raw_text: str,
) -> List[Dict[str, str]]:
    schema_model = SCHEMA_MAP.get(validation_role)
    schema_text = _REPAIR_CONTRACTS.get(validation_role)
    if schema_text is None:
        schema = schema_model.model_json_schema() if schema_model else {}
        schema_text = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))[:6000]
    context = _repair_context(messages, limit=6000)
    scope_rule = (
        " For Strategist plans, write_scope may contain only workspace-relative "
        "directories ending in '/'. Never use './' or a filename there; use "
        "workspace_root: true with write_scope: [] when a task writes at the workspace root."
        if validation_role == "strategist" else ""
    )
    previous = str(raw_text or '')[:6000]
    if validation_role == "strategist":
        previous = "(omitted; rebuild the compact plan from the decision context)"
    task_requirement = " with at least one task" if validation_role == "strategist" else ""
    repair_instruction = (
        "Rebuild the smallest complete plan from the user request and Chair decision. "
        "The result must contain at least one valid task. Do not invent repository "
        "facts, filenames, or dependencies not supported by the context."
        if validation_role == "strategist" else
        "Use only the non-tool decision context below. The previous response failed "
        "validation. Correct it without inventing unsupported content."
    )
    return [
        {
            "role": "system",
            "content": (
                f"Repair the {validation_role} response. Return ONLY one valid JSON object{task_requirement}; "
                f"no markdown, prose, code fences, or tool calls.{scope_rule} "
                "Follow this JSON Schema exactly:\n"
                f"{schema_text}"
            ),
        },
        {
            "role": "user",
            "content": (
                f"{repair_instruction}\n\n"
                f"Decision context:\n{context}\n\n"
                f"Validation error:\n{str(error or '')[:3000]}\n\n"
                f"Previous response:\n{previous}"
            ),
        },
    ]


def _manager_blocked_fallback(error: str, raw_text: str) -> str:
    """Return a valid Manager envelope that routes to the existing review gate."""
    payload = {
        "verdict": "BLOCKED",
        "confidence": 0.0,
        "summary": "Manager output could not be validated; manual review is required.",
        "issues": [{
            "severity": "critical",
            "task_id": "ALL",
            "description": str(error or "Manager response schema validation failed")[:1000],
            "suggestion": "Retry Manager review with a corrected structured response.",
            "evidence": "Raw model output withheld.",
        }],
    }
    return json.dumps(payload, ensure_ascii=False)


class AgentRunner:
    LIVENESS_INTERVAL_SECONDS = 15
    OUTPUT_RESERVE_BY_ROLE = {
        "chair": 1024,
        "strategist": 2048,
        "manager": 1024,
        "validator_task": 1024,
        "completeness_auditor": 1536,
        "implementer": 4096,
    }
    PROTECTED_RESERVE_ROLES = {"manager", "validator_task", "completeness_auditor"}

    def __init__(self, orchestrator, state, emit, tracker=None):
        self.orchestrator = orchestrator
        self.state = state
        self.emit = emit
        self.tracker = tracker or getattr(orchestrator, "_run_context_tracker", None)
        self._recovery = None  # resolved per invoke (role-specific, evidence-gated)
        self._recovery_decision = "not evaluated"

    def _resolve_recovery(self, role=None, overrides=None):
        """One bounded, evidence-gated recovery candidate with its decision."""
        role = role or getattr(self.state, "active_role", None)
        self._recovery_decision = "recovery resolver unavailable"
        try:
            from council_of_agents.scripts.council_recovery import RecoveryResolver
            router = getattr(self.orchestrator, "_router", None)
            cfg = router.role_config(role, overrides or {}) if router is not None and role else None
            candidate, decision = RecoveryResolver().recovery_decision(
                role or "", overrides=overrides,
                primary_endpoint=getattr(cfg, "endpoint_url", None),
                primary_model=getattr(cfg, "model", None),
            )
            self._recovery_decision = decision
            return candidate
        except Exception as exc:
            self._recovery_decision = f"recovery resolution failed: {exc}"
            return None

    async def _recovery_hop(self, role, messages, validation_role, recovery, **kwargs):
        """Exactly ONE recovery attempt on a different provider.

        Bounded: a single `_call_agent` call, no repair recursion, no further
        hops. The result must pass the role's strict contract or the hop is a
        failure (fail closed downstream). Never converts a provider failure
        into a model success — callers still record the provider trigger.
        """
        overrides = dict(kwargs.get("overrides") or self.state.role_overrides.get(role, {}) or {})
        overrides["endpoint_url"] = recovery["endpoint_url"]
        overrides["model"] = recovery["model"]
        if recovery.get("temperature") is not None:
            overrides["temperature"] = recovery["temperature"]
        if recovery.get("max_tokens") is not None:
            overrides["max_tokens"] = recovery["max_tokens"]
        if self.emit:
            await self.emit(
                event="recovery_hop",
                status="IN_PROGRESS",
                text=f"{role} retrying once with recovery model {recovery['model']}.",
                agent=role,
                extra={
                    "trigger": kwargs.get("trigger", ""),
                    "failure_class": kwargs.get("failure_class", ""),
                    "recovery_endpoint": recovery["endpoint_url"],
                    "recovery_model": recovery["model"],
                },
            )
        raw = await self.orchestrator._call_agent(
            role,
            self.state.session_id,
            overrides,
            messages,
            on_chunk=None,
            emit_cb=self.emit,
            disable_tools=validation_role in SCHEMA_MAP and validation_role != "implementer",
            workspace=getattr(self.state, "workspace", None) or None,
        )
        if validation_role in SCHEMA_MAP and not raw:
            raise SchemaValidationError(
                f"{validation_role} schema invalid: empty response (recovery hop)",
                raw_text=raw or "",
                validation_error="response was empty",
            )
        if raw:
            validation = validate_agent_output(validation_role, raw, strict=True)
            if not validation.success:
                raise SchemaValidationError(
                    f"{validation_role} schema invalid (recovery hop): {validation.error}",
                    raw_text=raw,
                    validation_error=validation.error,
                )
            if validation_role == "strategist":
                policy_error = _strategist_plan_error(
                    (validation.data or {}).get("tasks"), getattr(self.state, "user_prompt", None)
                )
                if policy_error:
                    raise SchemaValidationError("strategist plan policy invalid (recovery hop)", raw, policy_error)
        return raw

    def _get_extractor(self, role: str):
        # Local import to prevent circular dependency
        from council_of_agents.scripts.council_orchestrator import StreamingJsonExtractor
        
        # Use target keys that align with the role output schemas
        if role == "chair":
            return StreamingJsonExtractor("reason")
        elif role == "chair_arbitration":
            return StreamingJsonExtractor("reasoning")
        elif role == "manager":
            return StreamingJsonExtractor("summary")
        elif role == "implementer":
            return StreamingJsonExtractor("notes")
        elif role == "perspective_analyzer":
            return StreamingJsonExtractor("synthesis")
        elif role == "debate_response":
            return StreamingJsonExtractor("reasoning")
        return None

    async def invoke(
        self,
        role: str,
        messages: List[Dict[str, str]],
        schema_role: Optional[str] = None,
        max_retries: Optional[int] = None,
        **kwargs
    ) -> str:
        """Invoke an agent with retry backoff, Pydantic validation, token tracking, and streaming JSON extraction."""
        context_fallback_attempted = bool(kwargs.pop("_context_fallback_attempted", False))
        active_context_fallback = kwargs.pop("_context_fallback", None)
        recovery_fallback = active_context_fallback
        if recovery_fallback is None and not context_fallback_attempted:
            resolver = getattr(self.orchestrator, "_context_fallback_for", None)
            if resolver:
                candidate = resolver(role, self.state.role_overrides.get(role, {}))
                if isinstance(candidate, dict):
                    recovery_fallback = candidate

        # 1. Budget check
        if self.tracker and self.tracker.budget_tokens > 0 and self.tracker.over_budget():
            msg = (
                f"[context-budget] {role} call blocked: session {self.state.session_id} has "
                f"exceeded its token budget ({self.tracker.total_input_tokens} / "
                f"{self.tracker.budget_tokens} tokens used)."
            )
            logger.warning(msg)
            if self.emit:
                await self.emit(event="error", status="FAILED", text=msg)
            raise ContextBudgetExceededError(msg)

        timeout = self.orchestrator.AGENT_TIMEOUTS.get(role, 300)
        hard_timeouts = getattr(self.orchestrator, "AGENT_HARD_TIMEOUTS", {})
        if not isinstance(hard_timeouts, dict):
            hard_timeouts = {}
        hard_timeout = max(float(timeout), float(hard_timeouts.get(role, timeout)))
        
        # Determine retry limit
        if max_retries is None:
            max_retries = self.orchestrator.AGENT_MAX_RETRIES.get(role, 1)

        validation_role = schema_role or role
        # Recovery is role-specific and evidence-gated: resolve it for THIS
        # invoke (production creates one runner per call, the canary reuses a
        # runner across roles, and state.active_role is never set).
        self._recovery = self._resolve_recovery(role, self.state.role_overrides.get(role, {}))
        original_messages = copy.deepcopy(messages)
        # Preserve the established first-attempt mutation semantics for normal
        # tool-enabled roles. Only a schema-repair pass switches to the clean
        # bounded copy, so existing task execution state is not disconnected.
        attempt_messages = messages
        schema_repair_used = False
        attempt_number = 0
        contract_attempts = []
        semantic_rejection = False

        async def operation():
            nonlocal attempt_messages, schema_repair_used, attempt_number, semantic_rejection
            attempt_number += 1
            reservation_id = None
            attempt_started = False
            result = None
            last_progress = time.monotonic()
            model_wait_started = last_progress
            awaiting_model = True

            async def emit_progress(**event):
                nonlocal last_progress, model_wait_started, awaiting_model
                extra = event.get("extra") if isinstance(event.get("extra"), dict) else {}
                event_type = event.get("event")
                now = time.monotonic()
                if event_type == "tool_output":
                    # The tool result starts a fresh, bounded model turn.
                    # Some providers take over a minute before their first
                    # streamed token, so the short idle timer is not valid yet.
                    model_wait_started = now
                    awaiting_model = True
                if event_type != "heartbeat" or extra.get("phase") == "model_thinking":
                    last_progress = now
                if event_type == "thought_delta" or extra.get("phase") == "model_thinking":
                    awaiting_model = False
                if self.emit:
                    await self.emit(**event)

            async def liveness_pulse():
                while True:
                    await asyncio.sleep(self.LIVENESS_INTERVAL_SECONDS)
                    idle_seconds = time.monotonic() - last_progress
                    if idle_seconds >= self.LIVENESS_INTERVAL_SECONDS:
                        await emit_progress(
                            event="heartbeat", status="IN_PROGRESS", agent=role,
                            text="Waiting for model response",
                            extra={"phase": "awaiting_model", "idle_seconds": int(idle_seconds)},
                        )

            input_tokens = estimate_tokens(attempt_messages) if self.tracker else 0
            if self.tracker:
                output_reserve = self.OUTPUT_RESERVE_BY_ROLE.get(validation_role, 1024)
                allow_protected = validation_role in self.PROTECTED_RESERVE_ROLES
                # A reservation can be temporarily unavailable while a
                # parallel agent is reconciling its pessimistic allocation.
                # Wait a bounded number of times; never spin indefinitely.
                for wait_s in (0.0, 0.05, 0.15, 0.30):
                    reservation_id = self.tracker.reserve(
                        role,
                        input_tokens=input_tokens,
                        output_tokens=output_reserve,
                        allow_protected=allow_protected,
                        priority="verification" if allow_protected else "in_progress",
                    )
                    if reservation_id is not None:
                        break
                    if wait_s:
                        await asyncio.sleep(wait_s)
                if reservation_id is None:
                    available = self.tracker.available_for(allow_protected=allow_protected)
                    metadata = {
                        "failure_kind": "CONTEXT_BUDGET_EXHAUSTED",
                        "role": role,
                        "priority": "verification" if allow_protected else "in_progress",
                        "requested_tokens": input_tokens + output_reserve,
                        "available_tokens": available,
                        "budget_tokens": self.tracker.budget_tokens,
                        "reserved_tokens": self.tracker.reserved_tokens,
                    }
                    raise ContextBudgetExceededError(
                        f"[context-budget] {role} attempt blocked: requires about "
                        f"{input_tokens + output_reserve} tokens, only {available} are available.",
                        metadata=metadata,
                    )
            extractor = self._get_extractor(validation_role)
            
            if extractor:
                async def on_chunk(c):
                    clean = extractor.feed_chunk(c)
                    if clean:
                        await emit_progress(event="thought_delta", agent=role, status="IN_PROGRESS", text=clean)
            else:
                async def on_chunk(c):
                    await emit_progress(event="thought_delta", agent=role, status="IN_PROGRESS", text=c)

            pulse_task = asyncio.create_task(liveness_pulse()) if self.emit else None
            call_task = None
            try:
                attempt_started = True
                # Delegate low-level LLM call back to orchestrator
                call_kwargs = dict(kwargs)
                call_kwargs.setdefault("workspace", getattr(self.state, "workspace", None) or None)
                if active_context_fallback is not None:
                    call_kwargs["context_fallback"] = active_context_fallback
                if schema_repair_used:
                    # A schema repair is a bounded correction pass, not a new
                    # investigation. It must not create more tool output or
                    # mutate the original failed conversation.
                    call_kwargs["disable_tools"] = True
                    # The repair formats an existing reply; it is not the
                    # implementer execution call that must carry the packet.
                    call_kwargs.pop("required_contract", None)
                call_task = asyncio.create_task(
                    self.orchestrator._call_agent(
                        role, self.state.session_id, self.state.role_overrides.get(role, {}),
                        attempt_messages, on_chunk=on_chunk, emit_cb=emit_progress, **call_kwargs
                    )
                )
                while not call_task.done():
                    now = time.monotonic()
                    response_age = now - model_wait_started
                    remaining = hard_timeout - response_age
                    if not awaiting_model:
                        remaining = min(remaining, float(timeout) - (now - last_progress))
                    await asyncio.wait({call_task}, timeout=max(0, remaining))
                    if call_task.done():
                        break
                    now = time.monotonic()
                    if now - model_wait_started >= hard_timeout:
                        raise asyncio.TimeoutError(
                            f"{role} received no complete model response within {hard_timeout:g}s"
                        )
                    if not awaiting_model and now - last_progress >= float(timeout):
                        raise asyncio.TimeoutError(
                            f"{role} made no progress for {float(timeout):g}s"
                        )
                result = call_task.result()

                # Perform schema validation. An empty response is also a
                # contract failure for structured Council roles; otherwise a
                # provider hiccup could silently reach the Manager gate.
                if validation_role in SCHEMA_MAP and not result:
                    raise SchemaValidationError(
                        f"{validation_role} schema invalid: empty response",
                        raw_text=result or "",
                        validation_error="response was empty",
                    )
                if result:
                    v = validate_agent_output(validation_role, result, strict=True)
                    if not v.success:
                        raise SchemaValidationError(
                            f"{validation_role} schema invalid: {v.error}",
                            raw_text=result,
                            validation_error=v.error
                        )
                    if validation_role == "strategist":
                        from council_of_agents.scripts.task_dag import mutation_only_plan_error
                        semantic_error = mutation_only_plan_error(
                            (v.data or {}).get("tasks"), getattr(self.state, "user_prompt", None)
                        )
                        if semantic_error:
                            semantic_rejection = True
                            raise SchemaValidationError("strategist plan semantic invalid", result, semantic_error)
                        policy_error = _strategist_plan_error(
                            (v.data or {}).get("tasks"), getattr(self.state, "user_prompt", None)
                        )
                        if policy_error:
                            raise SchemaValidationError("strategist plan policy invalid", result, policy_error)
                return result
            except SchemaValidationError as error:
                contract_attempts.append({"attempt": attempt_number, "phase": "repair" if schema_repair_used else "primary", **_output_diagnostic(error)})
                if not schema_repair_used:
                    schema_repair_used = True
                    attempt_messages = _schema_repair_messages(
                        original_messages,
                        validation_role,
                        error.validation_error,
                        error.raw_text,
                    )
                raise
            finally:
                if call_task is not None and not call_task.done():
                    call_task.cancel()
                    await asyncio.gather(call_task, return_exceptions=True)
                if pulse_task:
                    pulse_task.cancel()
                    await asyncio.gather(pulse_task, return_exceptions=True)
                if self.tracker and reservation_id is not None:
                    if attempt_started:
                        output_tokens = (
                            estimate_tokens([{"role": "assistant", "content": result}])
                            if result else 0
                        )
                        self.tracker.reconcile(
                            reservation_id,
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                        )
                    else:
                        self.tracker.release(reservation_id)

        async def on_retry_fn(rs):
            if self.emit:
                await self.emit(
                    event="log",
                    status="IN_PROGRESS",
                    text=f"{role} retry {rs.attempt} ({rs.last_class.value}): {str(rs.last_error)[:100]}",
                    agent=role
                )

        try:
            result, retry_state = await retry_with_backoff(
                operation,
                role=role,
                max_retries=max_retries,
                on_retry=on_retry_fn,
                schema_retries=1 if validation_role in SCHEMA_MAP else None,
            )

            # Save retry logs to state metadata for observability
            if retry_state.errors:
                if not hasattr(self.state, "metadata") or self.state.metadata is None:
                    self.state.metadata = {}
                self.state.metadata[f"{role}_retry_state"] = retry_state.errors
            if contract_attempts:
                self._record_contract_diagnostics(role, contract_attempts, schema_repair_used, "valid", False, "not_attempted", "", self._recovery)

            return result

        except SchemaValidationError as e:
            recovery_used = False
            recovery_error = ""
            if self._recovery and not semantic_rejection:
                try:
                    recovery_messages = _schema_repair_messages(
                        original_messages, validation_role, e.validation_error, e.raw_text,
                    )
                    recovered = await self._recovery_hop(
                        role, recovery_messages, validation_role, self._recovery,
                        trigger="invalid_output", failure_class="invalid_output",
                    )
                    self._record_contract_diagnostics(role, contract_attempts, schema_repair_used, "invalid", True, "valid", "", self._recovery)
                    self._record_recovery_state(
                        role, "invalid_output", schema_repair_used, recovery_used=True,
                        recovery=self._recovery, final_outcome="RECOVERED",
                    )
                    return recovered
                except Exception as recovery_exc:
                    recovery_used = True
                    recovery_error = str(recovery_exc)[:500]
            self._record_contract_diagnostics(role, contract_attempts, schema_repair_used, "invalid" if schema_repair_used else "not_attempted", recovery_used, "invalid" if recovery_used else "not_attempted", recovery_error or str(e.validation_error or ""), self._recovery)
            self._record_recovery_state(
                role, "invalid_output", schema_repair_used, recovery_used=recovery_used,
                recovery=self._recovery if not semantic_rejection else None, final_outcome="MODEL_FAILURE",
                recovery_error=recovery_error or str(e.validation_error or ""), attempts=attempt_number,
            )
            if hasattr(self.state, "metadata"):
                if self.state.metadata is None:
                    self.state.metadata = {}
                self.state.metadata[f"{role}_schema_recovery"] = {
                    "attempts": attempt_number,
                    "failure_kind": "SCHEMA_VALIDATION",
                    "repair_used": schema_repair_used,
                    "recovery_attempted": bool(self._recovery and not semantic_rejection),
                    "recovery_used": recovery_used,
                    "recovery_error": recovery_error[:1000],
                    "recovery_decision": self._recovery_decision,
                    "safe_fallback": "MANAGER_BLOCKED" if validation_role == "manager" else "",
                    "validation_error": str(e.validation_error or "")[:1000],
                }
            if validation_role == "manager":
                fallback = _manager_blocked_fallback(e.validation_error, e.raw_text)
                if self.emit:
                    await self.emit(
                        event="recovery_blocked",
                        status="BLOCKED",
                        text="Manager response could not be validated; routing to manual review.",
                        agent=role,
                        extra={
                            "failure_kind": "SCHEMA_VALIDATION",
                            "retryable": False,
                            "safe_fallback": "MANAGER_BLOCKED",
                            "attempts": attempt_number,
                            "recovery_attempted": bool(self._recovery and not semantic_rejection),
                        },
                    )
                return fallback
            if self.emit:
                await self.emit(
                    event="error",
                    status="FAILED",
                    text=f"{role} output format invalid after retries: {e.validation_error[:150]}",
                    agent=role
                )
            raise
        except asyncio.TimeoutError as error:
            if self._recovery:
                try:
                    recovered = await self._recovery_hop(
                        role, original_messages, validation_role, self._recovery,
                        trigger="provider", failure_class="provider",
                    )
                    self._record_recovery_state(
                        role, "provider", False, recovery_used=True,
                        recovery=self._recovery, final_outcome="RECOVERED",
                    )
                    return recovered
                except Exception as recovery_exc:
                    self._record_recovery_state(
                        role, "provider", False, recovery_used=True,
                        recovery=self._recovery, final_outcome="PROVIDER_INCONCLUSIVE",
                        recovery_error=str(recovery_exc)[:500],
                    )
            if not self._recovery:
                self._record_recovery_state(
                    role, "provider", False, recovery_used=False,
                    final_outcome="PROVIDER_INCONCLUSIVE", recovery_error=str(error), attempts=attempt_number,
                )
            if self.emit:
                await self.emit(
                    event="error",
                    status="FAILED",
                    text=str(error) or f"{role} timed out after {timeout}s",
                    agent=role
                )
            raise
        except ContextBudgetExceededError as e:
            if self.emit:
                await self.emit(
                    event="error",
                    status="FAILED",
                    text="Context budget exhausted for this agent attempt.",
                    agent=role,
                    extra={"error_kind": "CONTEXT_BUDGET_EXHAUSTED", **getattr(e, "metadata", {})},
                )
            raise
        except Exception as e:
            if classify_error(e) == ErrorClass.CONTEXT and not context_fallback_attempted and recovery_fallback:
                fallback_model = recovery_fallback.get("model", "configured recovery model")
                if self.emit:
                    await self.emit(
                        event="context_recovery",
                        status="IN_PROGRESS",
                        text=f"{role} context was too large; retrying once with {fallback_model}.",
                        agent=role,
                        extra={
                            "error_kind": "context_overflow",
                            "fallback_model": fallback_model,
                        },
                    )
                retry_kwargs = dict(kwargs)
                retry_kwargs["_context_fallback_attempted"] = True
                retry_kwargs["_context_fallback"] = recovery_fallback
                return await self.invoke(
                    role,
                    messages,
                    schema_role=schema_role,
                    max_retries=0,
                    **retry_kwargs,
                )
            failure_class = classify_failure(e)
            if failure_class in ("provider", "invalid_output") and self._recovery:
                try:
                    recovered = await self._recovery_hop(
                        role, original_messages, validation_role, self._recovery,
                        trigger=failure_class, failure_class=failure_class,
                    )
                    self._record_recovery_state(
                        role, failure_class, schema_repair_used, recovery_used=True,
                        recovery=self._recovery, final_outcome="RECOVERED",
                    )
                    return recovered
                except Exception as recovery_exc:
                    self._record_recovery_state(
                        role, failure_class, schema_repair_used, recovery_used=True,
                        recovery=self._recovery, final_outcome="PROVIDER_INCONCLUSIVE" if failure_class == "provider" else "MODEL_FAILURE",
                        recovery_error=str(recovery_exc)[:500],
                    )
            if self.emit:
                await self.emit(
                    event="error",
                    status="FAILED",
                    text=f"{role} failed: {e}",
                    agent=role
                )
            raise

    def _record_contract_diagnostics(self, role, attempts, repair_attempted, repair_result, recovery_attempted, recovery_result, final_reason, recovery):
        """Persist hash-only contract diagnostics in state and existing checkpoint."""
        router = getattr(self.orchestrator, "_router", None)
        cfg = router.role_config(role, self.state.role_overrides.get(role, {})) if router else None
        payload = {
            "role": role, "stage": role,
            "provider": getattr(cfg, "endpoint_url", ""), "model": getattr(cfg, "model", ""),
            "attempts": list(attempts), "repair_attempted": bool(repair_attempted),
            "repair_result": repair_result, "recovery_candidate": {
                "endpoint_url": (recovery or {}).get("endpoint_url", ""),
                "model": (recovery or {}).get("model", ""),
                "eligibility": self._recovery_decision,
            },
            "recovery_attempted": bool(recovery_attempted), "recovery_result": recovery_result,
            "final_reason": str(final_reason or '')[:1000],
        }
        if not hasattr(self.state, "metadata") or self.state.metadata is None:
            self.state.metadata = {}
        self.state.metadata[f"{role}_contract_diagnostics"] = payload
        checkpoint = getattr(self.orchestrator, "_checkpoint", None)
        if checkpoint is not None:
            checkpoint.record_diagnostic(role, payload)

    def _record_recovery_state(self, role, failure_class, repair_used, *, recovery_used,
                               recovery=None, final_outcome="", recovery_error="", attempts=0):
        """Persist one bounded recovery decision and terminal classification."""
        try:
            if not hasattr(self.state, "metadata") or self.state.metadata is None:
                self.state.metadata = {}
            router = getattr(self.orchestrator, "_router", None)
            cfg = router.role_config(role, self.state.role_overrides.get(role, {})) if router else None
            self.state.metadata[f"{role}_recovery_state"] = {
                "failure_class": failure_class, "schema_repair_used": bool(repair_used),
                "recovery_attempted": bool(recovery), "recovery_used": bool(recovery_used),
                "recovery_endpoint": (recovery or {}).get("endpoint_url", ""),
                "recovery_model": (recovery or {}).get("model", ""),
                "recovery_decision": self._recovery_decision, "final_outcome": final_outcome,
                "recovery_error": str(recovery_error or "")[:1000], "attempts": int(attempts or 0),
            }
            self.state.metadata[f"{role}_failure"] = {
                "role": role, "stage": role, "provider": getattr(cfg, "endpoint_url", ""),
                "model": getattr(cfg, "model", ""), "attempts": int(attempts or 0),
                "failure_class": failure_class, "reason": str(recovery_error or "")[:1000],
                "recovery_decision": self._recovery_decision, "checkpoint_eligible": False,
                "terminal_state": final_outcome,
            }
        except Exception:
            pass
