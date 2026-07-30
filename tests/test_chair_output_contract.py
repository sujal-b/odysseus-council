import json
import pytest
from council_of_agents.scripts.council_schemas import validate_agent_output
from council_of_agents.scripts.bench_models import is_valid_chair
from council_of_agents.scripts.role_eval import evaluate


def _base_chair_payload(**overrides):
    base = {
        "complexity": "SIMPLE",
        "route": "PIPELINE",
        "action": "write",
        "target": "src/main.py",
        "reason": "Add feature",
    }
    base.update(overrides)
    return base


# 1. Legacy response with ambiguity fields omitted
def test_case_01_legacy_omitted():
    payload = _base_chair_payload()
    raw = json.dumps(payload)
    val = validate_agent_output("chair", raw, strict=True)
    bench = is_valid_chair(payload)
    assert val.success is True
    assert bench is True
    assert val.data["ambiguous"] is False
    assert val.data["clarification"] == ""
    assert val.data["options"] == []


# 2. ambiguous=false with empty clarification/options
def test_case_02_ambiguous_false_empty():
    payload = _base_chair_payload(ambiguous=False, clarification="   ", options=[])
    raw = json.dumps(payload)
    val = validate_agent_output("chair", raw, strict=True)
    bench = is_valid_chair(payload)
    assert val.success is True
    assert bench is True
    assert val.data["ambiguous"] is False
    assert val.data["clarification"] == ""
    assert val.data["options"] == []


# 3. ambiguous=true with two distinct options
def test_case_03_ambiguous_true_two_options():
    payload = _base_chair_payload(
        ambiguous=True,
        clarification="Which database to use?",
        options=["SQLite", "PostgreSQL"],
    )
    raw = json.dumps(payload)
    val = validate_agent_output("chair", raw, strict=True)
    bench = is_valid_chair(payload)
    assert val.success is True
    assert bench is True
    assert val.data["ambiguous"] is True
    assert val.data["clarification"] == "Which database to use?"
    assert val.data["options"] == ["SQLite", "PostgreSQL"]


# 4. ambiguous=true with four distinct options
def test_case_04_ambiguous_true_four_options():
    payload = _base_chair_payload(
        ambiguous=True,
        clarification="Which storage engine?",
        options=["SQLite", "PostgreSQL", "MySQL", "MongoDB"],
    )
    raw = json.dumps(payload)
    val = validate_agent_output("chair", raw, strict=True)
    bench = is_valid_chair(payload)
    assert val.success is True
    assert bench is True
    assert len(val.data["options"]) == 4


# 5. ambiguous=true with empty clarification
def test_case_05_ambiguous_true_empty_clarification():
    payload = _base_chair_payload(
        ambiguous=True,
        clarification="   ",
        options=["Option A", "Option B"],
    )
    raw = json.dumps(payload)
    val = validate_agent_output("chair", raw, strict=True)
    bench = is_valid_chair(payload)
    assert val.success is False
    assert bench is False


# 6. ambiguous=true with one option
def test_case_06_ambiguous_true_one_option():
    payload = _base_chair_payload(
        ambiguous=True,
        clarification="Which database?",
        options=["SQLite"],
    )
    raw = json.dumps(payload)
    val = validate_agent_output("chair", raw, strict=True)
    bench = is_valid_chair(payload)
    assert val.success is False
    assert bench is False


# 7. ambiguous=true with five options
def test_case_07_ambiguous_true_five_options():
    payload = _base_chair_payload(
        ambiguous=True,
        clarification="Which message broker?",
        options=["Outbox", "Kafka", "RabbitMQ", "SQS", "Webhooks"],
    )
    raw = json.dumps(payload)
    val = validate_agent_output("chair", raw, strict=True)
    bench = is_valid_chair(payload)
    assert val.success is False
    assert bench is False


# 8. ambiguous=true with a blank option
def test_case_08_ambiguous_true_blank_option():
    payload = _base_chair_payload(
        ambiguous=True,
        clarification="Which option?",
        options=["Option A", "   "],
    )
    raw = json.dumps(payload)
    val = validate_agent_output("chair", raw, strict=True)
    bench = is_valid_chair(payload)
    assert val.success is False
    assert bench is False


# 9. ambiguous=true with duplicate options
def test_case_09_ambiguous_true_duplicate_options():
    payload = _base_chair_payload(
        ambiguous=True,
        clarification="Which database?",
        options=["SQLite", "SQLite"],
    )
    raw = json.dumps(payload)
    val = validate_agent_output("chair", raw, strict=True)
    bench = is_valid_chair(payload)
    assert val.success is False
    assert bench is False


# 10. Case-insensitive / whitespace-equivalent duplicate options
def test_case_10_ambiguous_true_casefold_duplicate_options():
    payload = _base_chair_payload(
        ambiguous=True,
        clarification="Which database?",
        options=["SQLite", "  sqlite  "],
    )
    raw = json.dumps(payload)
    val = validate_agent_output("chair", raw, strict=True)
    bench = is_valid_chair(payload)
    assert val.success is False
    assert bench is False


# 11. ambiguous=false with a clarification
def test_case_11_ambiguous_false_with_clarification():
    payload = _base_chair_payload(
        ambiguous=False,
        clarification="Unneeded question",
        options=[],
    )
    raw = json.dumps(payload)
    val = validate_agent_output("chair", raw, strict=True)
    bench = is_valid_chair(payload)
    assert val.success is False
    assert bench is False


# 12. ambiguous=false with options
def test_case_12_ambiguous_false_with_options():
    payload = _base_chair_payload(
        ambiguous=False,
        clarification="",
        options=["Option A", "Option B"],
    )
    raw = json.dumps(payload)
    val = validate_agent_output("chair", raw, strict=True)
    bench = is_valid_chair(payload)
    assert val.success is False
    assert bench is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"options": ""},
        {"options": None},
        {"clarification": None},
        {"clarification": 42},
        {"ambiguous": True, "clarification": "Which option?", "options": ["A", 2]},
        {"ambiguous": "false"},
        {"ambiguous": "true"},
        {"ambiguous": 0},
        {"ambiguous": 1},
    ],
)
def test_malformed_ambiguity_types_are_rejected_by_both_contracts(overrides):
    payload = _base_chair_payload(**overrides)
    validation = validate_agent_output("chair", json.dumps(payload), strict=True)

    assert validation.success is False
    assert is_valid_chair(payload) is False


# Bounded-repair test
def test_bounded_repair_five_options_to_four_options():
    import asyncio

    invalid_5_opts = json.dumps(_base_chair_payload(
        ambiguous=True,
        clarification="Which message broker?",
        options=["Outbox", "Kafka", "RabbitMQ", "SQS", "Webhooks"],
    ))
    valid_4_opts = json.dumps(_base_chair_payload(
        ambiguous=True,
        clarification="Which message broker?",
        options=["Kafka", "RabbitMQ", "SQS", "Webhooks"],
    ))

    calls = []

    async def fake_call(**kwargs):
        calls.append(kwargs)
        return invalid_5_opts if len(calls) == 1 else valid_4_opts

    res = asyncio.run(evaluate(
        agent="chair",
        user_prompt="Add durable event delivery",
        endpoint="https://mock.endpoint/v1",
        model="mock-model",
        call=fake_call,
    ))

    assert len(calls) == 2, f"Expected exactly 2 provider calls (initial + repair), got {len(calls)}"
    assert res["contract_passed"] is True
    assert res["schema_repair_attempted"] is True
    assert res["schema_repair_succeeded"] is True
    assert res["canonical_output"]["ambiguous"] is True
    assert len(res["canonical_output"]["options"]) == 4
