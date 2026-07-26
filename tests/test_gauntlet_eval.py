import tempfile
from pathlib import Path

import pytest

from council_of_agents.scripts.gauntlet_eval import load_scenarios, run_scenario


SCENARIOS = load_scenarios()


@pytest.mark.asyncio
async def test_full_gauntlet_scenarios_run_in_disposable_workspaces():
    assert len(SCENARIOS) == 5

    with tempfile.TemporaryDirectory(prefix="gauntlet-test-", dir="data") as root:
        root = Path(root)
        results = {}
        for name, scenario in SCENARIOS.items():
            results[name] = await run_scenario(name, scenario, root / name)
            assert results[name]["status"] == scenario["expected_status"]

        happy = results["happy_path"]
        assert {record["role"] for record in happy["trace"] if "role" in record} >= {
            "chair", "strategist", "perspective_analyzer", "manager", "implementer",
            "completeness_auditor",
        }
        assert all(
            record.get("contract_passed", True)
            for record in happy["trace"]
            if record["kind"] == "gauntlet_stage" and record["role"] != "implementer"
        )
        assert any("WorkPacket" in message["content"] for record in happy["trace"] if record["role"] == "implementer" for message in record["messages"])

        rejected = results["perspective_requires_revision"]
        assert not any(record["role"] == "implementer" for record in rejected["trace"])
        perspective = next(record for record in rejected["trace"] if record["stage"] == "perspective")
        assert any("Strategist plan to audit:" in message["content"] for message in perspective["messages"])

        incomplete = results["incomplete_then_gap_fill"]
        assert [record["stage"] for record in incomplete["trace"] if record["role"] == "implementer"] == [
            "implementer:T1:initial", "implementer:T1:gap-fill"
        ]
        assert any(record["stage"] == "completeness:2" for record in incomplete["trace"])

        unsafe = results["unsafe_scope_blocked"]
        blocked = next(record for record in unsafe["trace"] if record.get("status") == "BLOCKED")
        assert any(term in blocked["error"] for term in ("workspace scope escapes root", "outside declared scope"))
        assert not (root / "outside.txt").exists()

        false_success = results["false_success_no_diff"]
        failed = next(record for record in false_success["trace"] if record.get("status") == "FAILED")
        assert "zero_evidence_execution" in failed["error"]
