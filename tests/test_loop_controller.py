import pytest

from council_of_agents.scripts.ledger_models import (
    AcceptanceCriterion,
    CriterionStatus,
    RunLedger,
    RunStatus,
    VerificationSpec,
)
from council_of_agents.scripts.loop_controller import (
    InvalidTransitionError,
    LoopController,
)


def _ledger():
    criterion = AcceptanceCriterion(
        id="AC-1",
        claim="tests pass",
        verification=VerificationSpec(adapter="command", config={"argv": ["pytest"]}),
    )
    return RunLedger(
        session_id="s1", goal="ship", acceptance_criteria={"AC-1": criterion}
    )


def test_legal_path_requires_verified_terminal_state():
    ledger = _ledger()
    controller = LoopController()
    for target in (
        RunStatus.READY, RunStatus.EXECUTING, RunStatus.VERIFYING,
        RunStatus.CHECKPOINTED,
    ):
        controller.transition(ledger, target)
    with pytest.raises(InvalidTransitionError, match="unverified"):
        controller.transition(ledger, RunStatus.COMPLETE)

    ledger.acceptance_criteria["AC-1"].status = CriterionStatus.VERIFIED
    controller.transition(ledger, RunStatus.COMPLETE)
    assert ledger.status == RunStatus.COMPLETE


def test_illegal_transition_is_rejected():
    ledger = _ledger()
    with pytest.raises(InvalidTransitionError, match="Illegal transition"):
        LoopController().transition(ledger, RunStatus.VERIFYING)


def test_blocked_run_can_resume():
    ledger = _ledger()
    controller = LoopController()
    controller.transition(ledger, RunStatus.BLOCKED)
    controller.transition(ledger, RunStatus.PLANNING)
    assert ledger.status == RunStatus.PLANNING


def test_next_status_honors_budget_before_work():
    ledger = _ledger()
    ledger.budget.token_limit = 100
    ledger.budget.tokens_used = 100
    assert LoopController().next_status(ledger) == RunStatus.BUDGET_EXHAUSTED

