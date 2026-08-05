"""Task-gate verdict regression: REVISE without cited issues.

The per-task Manager review must not burn the bounded retry budget with an
un-actionable REVISE (no issues) when deterministic verification already
passed. Regression for the vertical-slice failure where a correct artifact
was rejected twice with empty-issues REVISE verdicts and the run FAILED.
"""

from unittest.mock import MagicMock

from council_of_agents.scripts.council_orchestrator import CouncilOrchestrator


def _orchestrator():
    router = MagicMock()
    router.get.return_value = MagicMock(escalation=MagicMock(max_loops=3, conflict_threshold=0.7))
    return CouncilOrchestrator(router)


def test_revise_with_current_or_missing_task_id_stays_revise():
    orch = _orchestrator()
    review = ('{"verdict": "REVISE", "confidence": 90, "summary": "bad", '
              '"issues": [{"task_id": "T1", "description": "missing return"}]}')
    assert orch._task_gate_verdict(review, True, "T1") == "REVISE"
    assert orch._task_gate_verdict(review, True) == "REVISE"
    missing_issue_task_id = ('{"verdict": "REVISE", "issues": '
                             '[{"description": "missing return"}]}')
    assert orch._task_gate_verdict(missing_issue_task_id, True, "T1") == "REVISE"


def test_revise_with_explicit_off_task_issue_requires_passed_evidence():
    orch = _orchestrator()
    review = ('{"verdict": "REVISE", "issues": '
              '[{"task_id": "T2", "description": "other task"}]}')
    assert orch._task_gate_verdict(review, True, "T1") == "APPROVED"
    assert orch._task_gate_verdict(review, False, "T1") == "REVISE"
    assert orch._task_gate_verdict(review, None, "T1") == "REVISE"


def test_revise_without_issues_approved_when_evidence_passed():
    orch = _orchestrator()
    review = ('{"verdict": "REVISE", "confidence": 90, "summary": "verify content", "issues": []}')
    assert orch._task_gate_verdict(review, True) == "APPROVED"


def test_revise_without_issues_stays_revise_without_evidence():
    orch = _orchestrator()
    review = '{"verdict": "REVISE", "summary": "verify content", "issues": []}'
    assert orch._task_gate_verdict(review, None) == "REVISE"
    assert orch._task_gate_verdict(review, False) == "REVISE"


def test_approved_stays_approved():
    orch = _orchestrator()
    assert orch._task_gate_verdict('{"verdict": "APPROVED", "issues": []}', True) == "APPROVED"


def _evidence(passed):
    ev = MagicMock()
    ev.passed = passed
    return ev


def test_regex_failure_suppressed_when_evidence_passed():
    orch = _orchestrator()
    failure = "tool result matched error pattern '\\\\bnot found\\\\b': 'edit_file: old_string not found'"
    assert orch._regex_failure_actionable(failure, _evidence(True)) is False


def test_regex_failure_actionable_without_passed_evidence():
    orch = _orchestrator()
    failure = "tool result matched error pattern '\\\\bnot found\\\\b'"
    assert orch._regex_failure_actionable(failure, _evidence(False)) is True
    assert orch._regex_failure_actionable(failure, None) is True
    assert orch._regex_failure_actionable("", _evidence(True)) is False


def test_evidence_line_lists_accumulated_writes():
    orch = _orchestrator()
    task = MagicMock()
    task.accumulated_writes = {"src/app.py", "tests/test_app.py"}
    line = orch._task_gate_evidence_line(set(), _evidence(True), task)
    assert "ALL attempts" in line
    assert "already delivered" in line
    assert "src/app.py" in line and "tests/test_app.py" in line
    assert "Judge the deliverable on disk" in line
    alone = orch._task_gate_evidence_line({"src/app.py"}, _evidence(True))
    assert "ALL attempts" not in alone
