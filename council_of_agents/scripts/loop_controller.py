"""Deterministic state transitions and terminal invariants for RunLedger."""

from __future__ import annotations

from council_of_agents.scripts.ledger_models import RunLedger, RunStatus


class InvalidTransitionError(RuntimeError):
    pass


_ALLOWED = {
    RunStatus.PLANNING: {RunStatus.READY, RunStatus.BLOCKED, RunStatus.CANCELLED, RunStatus.FAILED},
    RunStatus.READY: {RunStatus.EXECUTING, RunStatus.BLOCKED, RunStatus.CANCELLED, RunStatus.FAILED},
    RunStatus.EXECUTING: {RunStatus.VERIFYING, RunStatus.DIAGNOSING, RunStatus.BLOCKED, RunStatus.CANCELLED, RunStatus.FAILED},
    RunStatus.VERIFYING: {RunStatus.CHECKPOINTED, RunStatus.DIAGNOSING, RunStatus.BLOCKED, RunStatus.FAILED},
    RunStatus.DIAGNOSING: {RunStatus.REPLANNING, RunStatus.BLOCKED, RunStatus.STAGNANT, RunStatus.FAILED},
    RunStatus.REPLANNING: {RunStatus.READY, RunStatus.BLOCKED, RunStatus.STAGNANT, RunStatus.FAILED},
    RunStatus.CHECKPOINTED: {RunStatus.READY, RunStatus.COMPLETE, RunStatus.BLOCKED, RunStatus.FAILED},
    RunStatus.BLOCKED: {RunStatus.READY, RunStatus.PLANNING, RunStatus.CANCELLED, RunStatus.FAILED},
}
_TERMINAL = {
    RunStatus.COMPLETE, RunStatus.BUDGET_EXHAUSTED,
    RunStatus.STAGNANT, RunStatus.CANCELLED, RunStatus.FAILED,
}
_COMMON_STOPS = {
    RunStatus.BLOCKED, RunStatus.BUDGET_EXHAUSTED,
    RunStatus.CANCELLED, RunStatus.FAILED,
}


class LoopController:
    def transition(self, ledger: RunLedger, target: RunStatus) -> RunLedger:
        if ledger.status in _TERMINAL:
            raise InvalidTransitionError(f"Terminal state cannot transition: {ledger.status.value}")
        if target not in _ALLOWED.get(ledger.status, set()) and target not in _COMMON_STOPS:
            raise InvalidTransitionError(
                f"Illegal transition: {ledger.status.value} -> {target.value}"
            )
        if target == RunStatus.COMPLETE and not ledger.all_mandatory_verified():
            raise InvalidTransitionError("Cannot complete with unverified mandatory criteria")
        ledger.status = target
        return ledger

    def next_status(self, ledger: RunLedger) -> RunStatus:
        budget = ledger.budget
        if budget.token_limit and budget.tokens_used >= budget.token_limit:
            return RunStatus.BUDGET_EXHAUSTED
        if budget.tool_call_limit and budget.tool_calls_used >= budget.tool_call_limit:
            return RunStatus.BUDGET_EXHAUSTED
        if budget.time_limit_seconds and budget.elapsed_seconds >= budget.time_limit_seconds:
            return RunStatus.BUDGET_EXHAUSTED
        if ledger.diagnostics and ledger.diagnostics[-1].stagnant:
            return RunStatus.STAGNANT
        if ledger.all_mandatory_verified():
            return RunStatus.COMPLETE
        if any(c.status.value == "failed" for c in ledger.acceptance_criteria.values()):
            return RunStatus.DIAGNOSING
        if ledger.tasks:
            return RunStatus.READY
        return RunStatus.PLANNING
