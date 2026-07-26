import hashlib
import json
from pathlib import Path

from council_of_agents.scripts.agent_runner import _schema_repair_messages
from council_of_agents.scripts.council_schemas import validate_agent_output
from council_of_agents.scripts.prompt_composer import PromptComposer
from council_of_agents.scripts.role_eval import _contract_validation


def _task(task_id="T1"):
    return {
        "id": task_id,
        "description": "Inspect and implement the bounded change.",
        "depends_on": [],
        "acceptance": "The requested result is verifiable.",
        "write_scope": ["src/"],
    }


def test_bare_task_array_is_normalized_before_strict_validation():
    result = validate_agent_output("strategist", json.dumps([_task()]), strict=True)

    assert result.success is True
    assert [item["id"] for item in result.data["tasks"]] == ["T1"]
    assert result.metadata == {
        "raw_shape": "bare_array",
        "normalized_shape": "canonical_envelope",
        "normalization_used": True,
    }


def test_task_keyed_dict_allows_only_known_special_keys_and_sorts_ids():
    raw = json.dumps({"T10": _task("T10"), "T2": _task("T2"), "risks": ["latency"]})
    result = validate_agent_output("strategist", raw, strict=True)

    assert result.success is True
    assert [item["id"] for item in result.data["tasks"]] == ["T2", "T10"]
    assert result.data["risks"] == ["latency"]
    assert result.metadata["raw_shape"] == "task_keyed_dict"


def test_markdown_tasks_block_is_normalized():
    raw = "```tasks\n" + json.dumps([_task()]) + "\n```"
    result = validate_agent_output("strategist", raw, strict=True)

    assert result.success is True
    assert result.metadata["raw_shape"] == "markdown_tasks_block"
    assert result.metadata["normalization_used"] is True


def test_empty_plan_remains_invalid_after_normalization():
    result = validate_agent_output("strategist", '{"tasks": [], "risks": []}', strict=True)

    assert result.success is False
    assert "at least 1 item" in result.error
    assert result.metadata["raw_shape"] == "canonical_envelope"


def test_ambiguous_task_dict_is_rejected():
    result = validate_agent_output(
        "strategist",
        json.dumps({"T1": _task(), "description": "unsupported top-level task"}),
        strict=True,
    )

    assert result.success is False
    assert result.metadata["raw_shape"] == "non_task_dict"


def test_role_eval_accepts_normalized_array_as_a_valid_contract():
    passed, data, error, failure_kind, metadata = _contract_validation(
        "strategist", json.dumps([_task()])
    )

    assert passed is True
    assert data["tasks"][0]["id"] == "T1"
    assert error == ""
    assert failure_kind == ""
    assert metadata["normalization_used"] is True


def test_strategist_repair_requirement_does_not_leak_to_other_roles():
    strategist = _schema_repair_messages([], "strategist", "empty plan", "{}")
    manager = _schema_repair_messages([], "manager", "missing verdict", "{}")

    assert "at least one task" in strategist[0]["content"]
    assert "at least one valid task" in strategist[1]["content"]
    assert "at least one task" not in manager[0]["content"]
    assert "at least one task" not in manager[1]["content"]


def test_p2_fragment_composition_matches_its_monolithic_fallback():
    root = Path(__file__).resolve().parents[1] / "data/council_agent_evals/phase-a/prompts/P2"

    assert PromptComposer(root).compose("strategist") == (root / "strategist.md").read_text(encoding="utf-8")


def test_p2_metadata_is_parented_and_tree_hash_is_current():
    root = Path(__file__).resolve().parents[1] / "data/council_agent_evals/phase-a/prompts"
    p1 = json.loads((root / "P1/parent.json").read_text(encoding="utf-8"))
    p2 = json.loads((root / "P2/parent.json").read_text(encoding="utf-8"))
    p2_root = root / "P2"
    digest = hashlib.sha256()
    entries = sorted(
        (
            path.relative_to(p2_root).as_posix().encode(),
            path.read_bytes(),
        )
        for path in p2_root.rglob("*")
        if path.is_file() and path.name not in {"parent.json", "changes.json"}
    )
    for relative, raw in entries:
        digest.update(relative)
        digest.update(bytes([0]))
        digest.update(raw)

    assert p2["version"] == "P2"
    assert p2["parent"] == "P1"
    assert p2["parent_sha256"] == p1["tree_sha256_excluding_metadata"]
    assert p2["tree_sha256_excluding_metadata"] == digest.hexdigest()
