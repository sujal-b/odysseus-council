"""Streaming canary: hash gate, stage detection, semantic checks, probe, rows."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from council_of_agents.scripts.streaming_canary import (
    _StreamProbe,
    _canary_semantic,
    _failing_stages,
    _latest_replies,
    _run_one_rep,
    evidence_gate,
)


def _trace(records):
    return {"schema_version": 2, "kind": "role_eval_trace", "user_prompt": "Build X.",
            "termination": "COMPLETE", "trace": records}


def _record(agent, stage, contract=True, initial=True, failure="", provider_failed=False,
            data=None, raw=None):
    rec = {
        "agent": agent, "stage": stage, "contract_passed": contract,
        "initial_contract_passed": initial, "provider_failed": provider_failed,
        "failure_kind": failure,
    }
    if data is not None:
        rec["contract_data"] = data
    if raw is not None:
        rec["raw_response"] = raw
    return rec


class TestEvidenceGate:
    def test_gate_fails_on_prompt_divergence(self, tmp_path, monkeypatch):
        import council_of_agents.scripts.streaming_canary as canary
        manifest = tmp_path / "manifest.json"
        manifest.write_text(json.dumps({
            "scenario_config_sha256": "A" * 64,
            "model_config_sha256": "B" * 64,
            "prompt_files": [{"filename": "strategist.md", "sha256": "C" * 64}],
        }), encoding="utf-8")
        prompts = tmp_path / "prompts"
        prompts.mkdir()
        (prompts / "parent.json").write_text(json.dumps({
            "role_prompt_sha256": {"strategist": "D" * 64},
        }), encoding="utf-8")
        scenarios = tmp_path / "scenarios.json"
        scenarios.write_bytes(bytes.fromhex("AA" * 32))
        gate = evidence_gate(prompts, scenarios, {"strategist"}, manifest_path=manifest)
        assert gate["passed"] is False
        assert gate["prompts_match_frozen"] is False

    def test_gate_passes_on_hash_match(self, tmp_path):
        import council_of_agents.scripts.streaming_canary as canary
        scenarios = tmp_path / "s.json"
        scenarios.write_bytes(bytes.fromhex("11" * 32))
        scenario_hash = canary._sha256_upper(scenarios)
        prompt_hash = "EF" * 32
        manifest = tmp_path / "manifest.json"
        manifest.write_text(json.dumps({
            "scenario_config_sha256": scenario_hash,
            "model_config_sha256": "B" * 64,
            "prompt_files": [{"filename": "strategist.md", "sha256": prompt_hash}],
        }), encoding="utf-8")
        prompts = tmp_path / "prompts"
        prompts.mkdir()
        (prompts / "parent.json").write_text(json.dumps({
            "role_prompt_sha256": {"strategist": prompt_hash},
        }), encoding="utf-8")
        gate = evidence_gate(prompts, scenarios, {"strategist"}, manifest_path=manifest)
        assert gate["passed"] is True
        assert gate["scenarios_match_frozen"] is True
        assert gate["prompts_match_frozen"] is True

    def test_missing_manifest_fails_closed(self, tmp_path):
        gate = evidence_gate(tmp_path / "p", tmp_path / "s", {"strategist"},
                             manifest_path=tmp_path / "nope.json")
        assert gate["passed"] is False


class TestStageDetection:
    def test_detects_schema_invalid_revision(self):
        trace = _trace([
            _record("chair", "chair"),
            _record("strategist", "strategist"),
            _record("strategist", "strategist_revision", contract=False, initial=False,
                    failure="schema_invalid"),
        ])
        stages = _failing_stages(trace)
        assert len(stages) == 1
        assert stages[0]["stage"] == "strategist_revision"
        assert stages[0]["failure_kind"] == "schema_invalid"

    def test_detects_provider_failed_revision(self):
        trace = _trace([
            _record("chair", "chair"),
            _record("perspective_analyzer", "perspective_revision", contract=False,
                    initial=False, provider_failed=True, failure="provider_error"),
        ])
        stages = _failing_stages(trace)
        assert stages[0]["provider_failed"] is True
        assert stages[0]["failure_kind"] == "provider_error"

    def test_passing_records_ignored(self):
        trace = _trace([_record("chair", "chair"), _record("strategist", "strategist")])
        assert _failing_stages(trace) == []

    def test_agent_filter(self):
        trace = _trace([
            _record("strategist", "strategist_revision", contract=False, failure="schema_invalid"),
            _record("perspective_analyzer", "perspective_revision", contract=False, failure="schema_invalid"),
        ])
        stages = _failing_stages(trace, only_agent="strategist")
        assert [s["role"] for s in stages] == ["strategist"]


class TestLatestReplies:
    def test_failed_stage_never_becomes_handoff(self):
        trace = _trace([
            _record("chair", "chair", data={"complexity": "SIMPLE", "route": "DIRECT", "action": "write", "target": "x", "reason": "r"}),
            _record("strategist", "strategist", data={"tasks": [{"id": "T1", "description": "d", "acceptance": "a", "write_scope": ["src/"]}], "risks": []}),
            _record("strategist", "strategist_revision", contract=False, data=None, raw="not json"),
        ])
        replies = _latest_replies(trace)
        assert "not json" not in replies.get("strategist", "")
        assert "T1" in replies["strategist"]

    def test_latest_valid_wins(self):
        trace = _trace([
            _record("strategist", "strategist", data={"tasks": [{"id": "T1"}]}),
            _record("strategist", "strategist_revision", data={"tasks": [{"id": "T2"}]}),
        ])
        replies = _latest_replies(trace)
        assert "T2" in replies["strategist"]


class TestCanarySemantic:
    def test_strategist_requires_tasks_with_acceptance(self):
        good = _canary_semantic("strategist", {
            "tasks": [{"id": "T1", "description": "d", "acceptance": "a", "write_scope": ["src/"]}],
            "risks": [],
        }, "raw")
        assert good["passed"] is True
        bad = _canary_semantic("strategist", {"tasks": [], "risks": []}, "raw")
        assert bad["passed"] is False
        bad_scope = _canary_semantic("strategist", {
            "tasks": [{"id": "T1", "description": "d", "acceptance": "a", "write_scope": ["src/App.tsx"]}],
            "risks": [],
        }, "raw")
        assert bad_scope["passed"] is False

    def test_manager_verdict_confidence_summary(self):
        good = _canary_semantic("manager", {"verdict": "APPROVED", "confidence": 0.9, "summary": "ok", "issues": []}, "raw")
        assert good["passed"] is True
        bad = _canary_semantic("manager", {"verdict": "APPROVED", "confidence": 1.5, "summary": "ok", "issues": []}, "raw")
        assert bad["passed"] is False

    def test_perspective_sections_and_overall(self):
        row = {
            "security": {"score": 0.8, "issues": []},
            "performance": {"score": 0.7, "issues": []},
            "maintainability": {"score": 0.6, "issues": []},
            "overall_score": 0.7,
        }
        assert _canary_semantic("perspective_analyzer", row, "raw")["passed"] is True
        assert _canary_semantic("perspective_analyzer", {**row, "overall_score": 2.0}, "raw")["passed"] is False

    def test_completeness_single_row_criteria(self):
        good = _canary_semantic("completeness_auditor",
                                {"completeness": 0.8, "done": False, "criteria": [{"criterion": "x"}]}, "raw")
        assert good["passed"] is True
        bad = _canary_semantic("completeness_auditor",
                               {"completeness": 0.8, "done": False, "criteria": []}, "raw")
        assert bad["passed"] is False


class TestStreamProbe:
    def test_captures_only_canary_run_id(self, monkeypatch):
        import src.llm_core as llm_core
        calls = []

        def fake_record(context, *, provider, endpoint, model, payload, attempt=1):
            calls.append((context.get("run_id"), payload.get("stream"), bool(payload.get("response_format"))))

        monkeypatch.setattr(llm_core, "record_model_request", fake_record)
        probe = _StreamProbe("canary-123")
        probe.install()
        try:
            llm_core.record_model_request(
                {"run_id": "canary-123", "agent": "strategist"},
                provider="nvidia", endpoint="https://e.example.com/v1", model="m",
                payload={"stream": True, "response_format": {"type": "json_object"}},
            )
            llm_core.record_model_request(
                {"run_id": "other-run"},
                provider="nvidia", endpoint="https://e.example.com/v1", model="m",
                payload={"stream": True},
            )
            assert probe.stream_seen("m") is True
            assert probe.contract_seen("m") is True
        finally:
            probe.restore()
        assert len(calls) == 2

    def test_restore_returns_original(self, monkeypatch):
        import src.llm_core as llm_core
        probe = _StreamProbe("canary-1")
        probe.install()
        probe.restore()
        assert llm_core.record_model_request.__name__ != "probe"


class TestRunOneRep:
    @pytest.mark.asyncio
    async def test_clean_rep_records_stream_and_contract(self):
        events = []
        state = SimpleNamespace(session_id="canary-x", role_overrides={}, metadata={})

        class StubRunner:
            def __init__(self):
                self.state = state

            async def invoke(self, role, messages, schema_role=None):
                return ('{"tasks":[{"id":"T1","description":"Build it","acceptance":"runs",'
                        '"write_scope":["src/"],"depends_on":[]}],"risks":[]}')

        row = await _run_one_rep("canary-x", object(), StubRunner(), "strategist",
                                 [{"role": "user", "content": "plan"}], "strategist_revision",
                                 "nemotron-3-ultra-free", "https://opencode.ai/zen/v1")
        assert row["contract_passed"] is True
        assert row["initial_contract_passed"] is True
        assert row["semantic_forced_pass"] is True
        assert row["provider_failed"] is False
        assert row["stage"] == "strategist_revision"

    @pytest.mark.asyncio
    async def test_failed_rep_records_failure_kind(self):
        state = SimpleNamespace(session_id="canary-y", role_overrides={}, metadata={})

        class StubRunner:
            def __init__(self):
                self.state = state

            async def invoke(self, role, messages, schema_role=None):
                raise asyncio.TimeoutError("provider timed out")

        row = await _run_one_rep("canary-y", object(), StubRunner(), "strategist",
                                 [{"role": "user", "content": "plan"}], "strategist_revision",
                                 "nemotron-3-ultra-free", "https://opencode.ai/zen/v1")
        assert row["contract_passed"] is False
        assert row["provider_failed"] is True
        assert row["failure_kind"] == "provider_error"

    @pytest.mark.asyncio
    async def test_repair_marks_initial_contract_failed(self):
        class StubRunner:
            def __init__(self):
                self.state = None

            async def invoke(self, role, messages, schema_role=None):
                self.state.metadata[f"{role}_schema_recovery"] = {"repair_used": True}
                return ('{"tasks":[{"id":"T1","description":"Build it","acceptance":"runs",'
                        '"write_scope":["src/"],"depends_on":[]}],"risks":[]}')

        runner = StubRunner()
        row = await _run_one_rep("canary-z", object(), runner, "strategist",
                                 [{"role": "user", "content": "plan"}], "strategist_revision",
                                 "nemotron-3-ultra-free", "https://opencode.ai/zen/v1")
        assert row["contract_passed"] is True
        assert row["initial_contract_passed"] is False
        assert row["post_repair_valid"] is True
