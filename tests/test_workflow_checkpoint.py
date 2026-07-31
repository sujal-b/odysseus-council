"""Durable workflow checkpoint tests."""

from council_of_agents.scripts.workflow_checkpoint import WorkflowCheckpoint


def test_record_approval_records_model_calls(tmp_path):
    """The plan-approval stage must record model_calls so the vertical-slice
    verify phase can assert exactly one model call per stage (regression: the
    manager stage was recorded via record_approval without model_calls, which
    failed the verify gate)."""
    cp = WorkflowCheckpoint("wfb-check", base_dir=tmp_path)
    cp.record_approval("APPROVED", '{"verdict": "APPROVED"}')
    manager = cp._data["stages"]["manager"]
    assert manager["status"] == "DONE"
    assert manager["approval"] == "APPROVED"
    assert int(manager["model_calls"]) == 1
    assert cp.approved()
