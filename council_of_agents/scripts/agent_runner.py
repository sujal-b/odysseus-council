"""Unified Agent Execution Runner for Odysseus Council of Agents.
Handles schema validation, retry backoffs, context budgets, and streaming extracts.
"""
import asyncio
import logging
from typing import Any, List, Dict, Optional
from council_of_agents.scripts.council_schemas import validate_agent_output
from council_of_agents.scripts.council_retry import retry_with_backoff, SchemaValidationError
from council_of_agents.scripts.context_tracker import ContextBudgetExceededError
from src.model_context import estimate_tokens

logger = logging.getLogger(__name__)


class AgentRunner:
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
        
        # Determine retry limit
        if max_retries is None:
            max_retries = self.orchestrator.AGENT_MAX_RETRIES.get(role, 1)

        input_tokens = estimate_tokens(messages) if self.tracker else 0
        validation_role = schema_role or role

        async def operation():
            reservation_id = None
            attempt_started = False
            result = None
            if self.tracker:
                output_reserve = self.OUTPUT_RESERVE_BY_ROLE.get(validation_role, 1024)
                reservation_id = self.tracker.reserve(
                    role,
                    input_tokens=input_tokens,
                    output_tokens=output_reserve,
                    allow_protected=validation_role in self.PROTECTED_RESERVE_ROLES,
                )
                if reservation_id is None:
                    raise ContextBudgetExceededError(
                        f"[context-budget] {role} attempt blocked: requires about "
                        f"{input_tokens + output_reserve} tokens, only "
                        f"{self.tracker.available_for(allow_protected=validation_role in self.PROTECTED_RESERVE_ROLES)} "
                        "are available."
                    )
            extractor = self._get_extractor(validation_role)
            
            if extractor:
                async def on_chunk(c):
                    clean = extractor.feed_chunk(c)
                    if clean and self.emit:
                        await self.emit(event="thought_delta", agent=role, status="IN_PROGRESS", text=clean)
            else:
                async def on_chunk(c):
                    if self.emit:
                        await self.emit(event="thought_delta", agent=role, status="IN_PROGRESS", text=c)

            try:
                attempt_started = True
                # Delegate low-level LLM call back to orchestrator
                result = await asyncio.wait_for(
                    self.orchestrator._call_agent(
                        role, self.state.session_id, self.state.role_overrides.get(role, {}),
                        messages, on_chunk=on_chunk, emit_cb=self.emit, **kwargs
                    ),
                    timeout=timeout,
                )

                # Perform schema validation
                if result:
                    v = validate_agent_output(validation_role, result)
                    if not v.success:
                        raise SchemaValidationError(
                            f"{validation_role} schema invalid: {v.error}",
                            raw_text=result,
                            validation_error=v.error
                        )
                return result
            finally:
                if self.tracker and reservation_id is not None:
                    self.tracker.release(reservation_id)
                    if attempt_started:
                        output_tokens = (
                            estimate_tokens([{"role": "assistant", "content": result}])
                            if result else 0
                        )
                        self.tracker.record(
                            role,
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                        )

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
            )

            # Save retry logs to state metadata for observability
            if retry_state.errors:
                if not hasattr(self.state, "metadata") or self.state.metadata is None:
                    self.state.metadata = {}
                self.state.metadata[f"{role}_retry_state"] = retry_state.errors

            return result

        except SchemaValidationError as e:
            if self.emit:
                await self.emit(
                    event="error",
                    status="FAILED",
                    text=f"{role} output format invalid after retries: {e.validation_error[:150]}",
                    agent=role
                )
            raise
        except asyncio.TimeoutError:
            if self.emit:
                await self.emit(
                    event="error",
                    status="FAILED",
                    text=f"{role} timed out after {timeout}s",
                    agent=role
                )
            raise
        except Exception as e:
            if self.emit:
                await self.emit(
                    event="error",
                    status="FAILED",
                    text=f"{role} failed: {e}",
                    agent=role
                )
            raise
