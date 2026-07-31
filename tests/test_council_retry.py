"""Retry classification tests."""

import asyncio

from council_of_agents.scripts.council_retry import ErrorClass, classify_error


def test_workspace_guard_rejection_is_terminal_not_transient():
    """A deterministic workspace-guard rejection must not be blind-retried with
    identical context (regression: the vertical slice burned a full implementer
    invoke retrying the same blocked bash call before failing the task)."""
    from council_of_agents.scripts.workspace_revision import WorkspaceScopeError
    rejection = WorkspaceScopeError("tool 'bash' is not compatible with guarded workspace execution")
    assert classify_error(rejection) is ErrorClass.TERMINAL


def test_transient_and_permission_classes_unchanged():
    assert classify_error(asyncio.TimeoutError("no response")) is ErrorClass.TRANSIENT
    assert classify_error(ValueError("unknown")) is ErrorClass.TRANSIENT
