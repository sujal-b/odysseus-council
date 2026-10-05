import re

from src.zen_free_gate import (
    GATE_TOOL_NAMES,
    apply_tool_gate,
    gate_headers,
    gate_stub_tool,
    mint_session_id,
)

_ZEN_PROVIDERS = ("opencode-zen", "opencode-go")


def _tool(name):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "real tool",
            "parameters": {"type": "object", "properties": {"x": {"type": "string"}}},
        },
    }


def _payload(**extra):
    base = {"model": "some-model", "messages": [{"role": "user", "content": "hi"}], "stream": True}
    base.update(extra)
    return base


def test_minted_session_id_matches_gateway_shape():
    for _ in range(50):
        assert re.match(r"^ses_[0-9a-f]{12}[0-9A-Za-z]{14}$", mint_session_id())


def test_minted_session_ids_are_not_constant():
    assert len({mint_session_id() for _ in range(20)}) > 1


def test_gate_headers_carry_cli_identity():
    headers = gate_headers()
    assert "opencode/" in headers["user-agent"]
    assert re.match(r"^ses_[0-9a-f]{12}[0-9A-Za-z]{14}$", headers["x-opencode-session"])


def test_gate_tools_are_named_for_the_gateway_and_carry_empty_schemas():
    for name in GATE_TOOL_NAMES:
        stub = gate_stub_tool(name)
        assert stub["type"] == "function"
        assert stub["function"]["name"] in ("bash", "read")
        assert stub["function"]["parameters"] == {"type": "object", "properties": {}}


def test_both_stubs_added_and_disarmed_when_role_has_no_tools():
    payload = apply_tool_gate(_payload(), None)
    names = [t["function"]["name"] for t in payload["tools"]]
    assert names == ["bash", "read"]
    assert payload["tool_choice"] == "none"


def test_only_the_missing_name_is_added_and_real_tools_survive():
    tools = [_tool("bash"), _tool("write_file")]
    payload = apply_tool_gate(_payload(tools=tools), tools)
    names = [t["function"]["name"] for t in payload["tools"]]
    assert names == ["bash", "write_file", "read"]
    assert "tool_choice" not in payload
    assert payload["tools"][0] is tools[0]
    assert payload["tools"][1] is tools[1]


def test_untouched_when_both_names_already_declared():
    tools = [_tool("bash"), _tool("read")]
    payload = _payload(tools=tools)
    assert apply_tool_gate(payload, tools) is payload


def test_non_tool_shaped_payloads_are_left_alone():
    for bad in ({}, {"messages": None}, {"messages": "hi"}, []):
        assert apply_tool_gate(bad, None) is bad


def test_zen_provider_headers_gain_gate_identity():
    from src.llm_core import _provider_headers

    headers = _provider_headers("opencode-zen")
    assert "opencode/" in headers["user-agent"]
    assert re.match(r"^ses_[0-9a-f]{12}[0-9A-Za-z]{14}$", headers["x-opencode-session"])


def test_other_providers_are_byte_identical():
    from src.llm_core import _provider_headers

    for provider in ("openai", "anthropic", "ollama", "openrouter", "copilot", "groq", "nvidia"):
        headers = _provider_headers(provider)
        assert "user-agent" not in headers
        assert not any(k.startswith("x-opencode-") for k in headers)


def test_caller_supplied_user_agent_does_not_survive_for_zen():
    from src.llm_core import _provider_headers

    headers = _provider_headers("opencode-zen", {"user-agent": "odysseus/9", "x-api-key": "public"})
    assert "opencode/" in headers["user-agent"]
    assert headers["x-api-key"] == "public"