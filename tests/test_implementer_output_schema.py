"""Regression test for the P0 pipeline-killer: ImplementerOutput must not reject
the implementer's free-form code/prose output (missing `status` previously
raised SchemaValidationError and crashed every run)."""
from council_of_agents.scripts.council_schemas import validate_agent_output


def test_freeform_code_passes_validation():
    v = validate_agent_output("implementer", "def add(a, b):\n    return a + b\n# done")
    assert v.success, v.error
    assert v.data["status"] == "DONE"
    assert "def add" in v.data["notes"]


def test_json_with_status_still_parses():
    v = validate_agent_output("implementer", '{"status": "DONE", "files_created": ["a.py"]}')
    assert v.success
    assert v.data["status"] == "DONE"
    assert "a.py" in v.data["files_created"]


def test_json_without_status_defaults_done():
    v = validate_agent_output("implementer", '{"notes": "did stuff"}')
    assert v.success
    assert v.data["status"] == "DONE"


def test_empty_output_does_not_crash():
    v = validate_agent_output("implementer", "")
    assert v.success
