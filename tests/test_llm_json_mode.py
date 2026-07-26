from src.llm_core import _requires_json_object


def test_control_roles_use_json_mode_only_without_tools():
    assert _requires_json_object({"agent": "strategist"})
    assert not _requires_json_object({"agent": "strategist"}, [{"type": "function"}])
    assert not _requires_json_object({"agent": "implementer"})
