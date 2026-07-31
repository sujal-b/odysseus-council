"""Shared bounded recovery resolver: evidence gate, deny list, one hop, fail closed."""
import json
import os

import pytest

from council_of_agents.scripts.council_recovery import (
    DENY_RECOVERY_MODELS,
    OUTCOME_CLEAN,
    OUTCOME_MODEL_FAILURE,
    OUTCOME_PROVIDER_INCONCLUSIVE,
    OUTCOME_RECOVERED,
    RecoveryEvidence,
    RecoveryResolver,
    classify_failure,
    classify_stage_outcome,
    classify_trace_outcome,
    endpoint_host,
    validate_recovery_config,
)
from council_of_agents.scripts.council_retry import SchemaValidationError


def _write_models(path, roles):
    path.write_text(json.dumps({"roles": roles}, ensure_ascii=False), encoding="utf-8")


def _good_row(initial=True):
    return {
        "initial_contract_passed": initial,
        "contract_passed": True,
        "provider_failed": False,
        "semantic_quality": {"passed": True},
    }


def _valid_models(tmp_path, role="strategist", recovery_model="other/model", recovery_url="https://other.example.com/v1"):
    return _write_models(tmp_path / "models.json", {
        role: {
            "endpoint_url": "https://integrate.api.nvidia.com/v1/chat/completions",
            "model": "nvidia/nemotron-3-super-120b-a12b",
            "recovery_fallbacks": [{"endpoint_url": recovery_url, "model": recovery_model}],
        }
    })


class TestEndpointHost:
    def test_host_parses_scheme_and_port(self):
        assert endpoint_host("https://opencode.ai/zen/v1/chat/completions") == "opencode.ai"
        assert endpoint_host("https://integrate.api.nvidia.com/v1/chat/completions") == "integrate.api.nvidia.com"
        assert endpoint_host("https://other.example.com:8443/v1") == "other.example.com:8443"

    def test_same_host_is_same_provider(self):
        assert endpoint_host("https://opencode.ai/zen/v1") == endpoint_host("https://opencode.ai/v1/chat")


class TestClassifyFailure:
    def test_provider_timeout_class(self):
        assert classify_failure(TimeoutError("timed out")) == "provider"

    def test_connection_reset_is_provider(self):
        class ConnReset(Exception):
            pass
        assert classify_failure(ConnReset("Connection reset by peer")) == "provider"

    def test_schema_validation_error_is_invalid_output(self):
        exc = SchemaValidationError("schema invalid: missing verdict", raw_text="", validation_error="missing verdict")
        assert classify_failure(exc) == "invalid_output"

    def test_generic_error_is_other(self):
        # classify_error defaults unknown errors to TRANSIENT (fail-open
        # retry); only terminal classes map to "other".
        assert classify_failure(ValueError("unauthorized: invalid api key")) == "other"

    def test_provider_keywords_from_text(self):
        from council_of_agents.scripts.council_recovery import classify_failure_from_text
        assert classify_failure_from_text("upstream request failed: 502") == "provider"
        assert classify_failure_from_text("rate limit exceeded") == "provider"
        assert classify_failure_from_text("model returned garbage") == "other"


class TestRecoveryEvidence:
    def test_three_three_marks_eligible(self, tmp_path):
        ledger = RecoveryEvidence(tmp_path / "recovery-evidence.json")
        entry = ledger.record(
            "strategist", "strategist_revision",
            "https://opencode.ai/zen/v1/chat/completions", "nemotron-3-ultra-free",
            [_good_row(), _good_row(), _good_row()],
        )
        assert entry["eligible"] is True
        assert ledger.eligible("strategist", "https://opencode.ai/zen/v1/chat/completions", "nemotron-3-ultra-free") is True

    def test_two_reps_never_eligible(self, tmp_path):
        ledger = RecoveryEvidence(tmp_path / "recovery-evidence.json")
        ledger.record("strategist", "s", "https://opencode.ai/v1", "m", [_good_row(), _good_row()])
        assert ledger.eligible("strategist", "https://opencode.ai/v1", "m") is False

    def test_provider_failure_poisons_evidence(self, tmp_path):
        ledger = RecoveryEvidence(tmp_path / "recovery-evidence.json")
        rows = [_good_row(), _good_row(), {**_good_row(), "provider_failed": True}]
        entry = ledger.record("strategist", "s", "https://opencode.ai/v1", "m", rows)
        assert entry["eligible"] is False

    def test_missing_evidence_file_fails_closed(self, tmp_path):
        _valid_models(tmp_path, recovery_model="other/model", recovery_url="https://other.example.com/v1")
        resolver = RecoveryResolver(tmp_path / "models.json", tmp_path / "missing-evidence.json")
        assert resolver.recovery_for("strategist", primary_endpoint="https://integrate.api.nvidia.com/v1") is None

    def test_corrupt_evidence_file_fails_closed(self, tmp_path):
        _valid_models(tmp_path, recovery_model="other/model", recovery_url="https://other.example.com/v1")
        evidence = tmp_path / "recovery-evidence.json"
        evidence.write_text("not json {", encoding="utf-8")
        resolver = RecoveryResolver(tmp_path / "models.json", evidence)
        assert resolver.recovery_for("strategist", primary_endpoint="https://integrate.api.nvidia.com/v1") is None


class TestRecoveryResolver:
    def test_no_candidate_when_no_evidence(self, tmp_path):
        _valid_models(tmp_path)
        resolver = RecoveryResolver(tmp_path / "models.json", tmp_path / "e.json")
        assert resolver.recovery_for("strategist", primary_endpoint="https://integrate.api.nvidia.com/v1") is None

    def test_returns_candidate_after_three_three_evidence(self, tmp_path):
        _valid_models(tmp_path)
        ledger = RecoveryEvidence(tmp_path / "e.json")
        ledger.record("strategist", "strategist_revision",
                      "https://other.example.com/v1", "other/model",
                      [_good_row(), _good_row(), _good_row()])
        resolver = RecoveryResolver(tmp_path / "models.json", tmp_path / "e.json")
        candidate = resolver.recovery_for(
            "strategist",
            primary_endpoint="https://integrate.api.nvidia.com/v1/chat/completions",
            primary_model="nvidia/nemotron-3-super-120b-a12b",
        )
        assert candidate is not None
        assert candidate["model"] == "other/model"
        assert candidate["endpoint_url"] == "https://other.example.com/v1"

    def test_bounded_single_candidate_only(self, tmp_path):
        models = tmp_path / "models.json"
        _write_models(models, {
            "strategist": {
                "endpoint_url": "https://integrate.api.nvidia.com/v1",
                "model": "nvidia/nemotron-3-super-120b-a12b",
                "recovery_fallbacks": [
                    {"endpoint_url": "https://a.example.com/v1", "model": "a-model"},
                    {"endpoint_url": "https://b.example.com/v1", "model": "b-model"},
                ],
            }
        })
        resolver = RecoveryResolver(models, tmp_path / "e.json")
        assert resolver.recovery_for("strategist") is None

    def test_same_provider_rejected(self, tmp_path):
        models = tmp_path / "models.json"
        _write_models(models, {
            "strategist": {
                "endpoint_url": "https://opencode.ai/zen/v1",
                "model": "nemotron-3-ultra-free",
                "recovery_fallbacks": [{"endpoint_url": "https://opencode.ai/other/v1", "model": "other-model"}],
            }
        })
        ledger = RecoveryEvidence(tmp_path / "e.json")
        ledger.record("strategist", "s", "https://opencode.ai/other/v1", "other-model",
                      [_good_row(), _good_row(), _good_row()])
        resolver = RecoveryResolver(models, tmp_path / "e.json")
        assert resolver.recovery_for("strategist", primary_endpoint="https://opencode.ai/zen/v1") is None

    def test_primary_model_duplicate_rejected(self, tmp_path):
        models = tmp_path / "models.json"
        _write_models(models, {
            "strategist": {
                "endpoint_url": "https://integrate.api.nvidia.com/v1",
                "model": "nvidia/nemotron-3-super-120b-a12b",
                "recovery_fallbacks": [{"endpoint_url": "https://other.example.com/v1", "model": "nvidia/nemotron-3-super-120b-a12b"}],
            }
        })
        ledger = RecoveryEvidence(tmp_path / "e.json")
        ledger.record("strategist", "s", "https://other.example.com/v1", "nvidia/nemotron-3-super-120b-a12b",
                      [_good_row(), _good_row(), _good_row()])
        resolver = RecoveryResolver(models, tmp_path / "e.json")
        assert resolver.recovery_for(
            "strategist",
            primary_endpoint="https://integrate.api.nvidia.com/v1",
            primary_model="nvidia/nemotron-3-super-120b-a12b",
        ) is None

    @pytest.mark.parametrize("role", ["strategist", "manager"])
    def test_nano_denied_without_fresh_evidence(self, tmp_path, role):
        models = tmp_path / "models.json"
        _write_models(models, {
            role: {
                "endpoint_url": "https://integrate.api.nvidia.com/v1",
                "model": "nvidia/nemotron-3-super-120b-a12b",
                "recovery_fallbacks": [{"endpoint_url": "https://other.example.com/v1", "model": "nvidia/nemotron-3-nano-30b-a3b"}],
            }
        })
        # No evidence at all: prior live failures keep Nano denied.
        resolver = RecoveryResolver(models, tmp_path / "e.json")
        assert resolver.recovery_for(role, primary_endpoint="https://integrate.api.nvidia.com/v1") is None

    def test_nano_allowed_with_fresh_three_three_evidence(self, tmp_path):
        models = tmp_path / "models.json"
        _write_models(models, {
            "strategist": {
                "endpoint_url": "https://integrate.api.nvidia.com/v1",
                "model": "nvidia/nemotron-3-super-120b-a12b",
                "recovery_fallbacks": [{"endpoint_url": "https://other.example.com/v1", "model": "nvidia/nemotron-3-nano-30b-a3b"}],
            }
        })
        ledger = RecoveryEvidence(tmp_path / "e.json")
        ledger.record("strategist", "s", "https://other.example.com/v1", "nvidia/nemotron-3-nano-30b-a3b",
                      [_good_row(), _good_row(), _good_row()])
        resolver = RecoveryResolver(models, tmp_path / "e.json")
        candidate = resolver.recovery_for("strategist", primary_endpoint="https://integrate.api.nvidia.com/v1")
        assert candidate is not None
        assert candidate["model"] == "nvidia/nemotron-3-nano-30b-a3b"

    def test_missing_role_returns_none(self, tmp_path):
        _valid_models(tmp_path)
        resolver = RecoveryResolver(tmp_path / "models.json", tmp_path / "e.json")
        assert resolver.recovery_for("chair_arbitration") is None

    def test_unreadable_models_config_fails_closed(self, tmp_path):
        models = tmp_path / "models.json"
        models.write_text("broken", encoding="utf-8")
        resolver = RecoveryResolver(models, tmp_path / "e.json")
        assert resolver.recovery_for("strategist") is None


class TestValidateConfig:
    def test_valid_single_candidate_passes(self, tmp_path):
        _valid_models(tmp_path, role="chair", recovery_model="other/model", recovery_url="https://other.example.com/v1")
        assert validate_recovery_config(tmp_path / "models.json", tmp_path / "e.json") == []

    def test_two_candidates_fail_startup(self, tmp_path):
        models = tmp_path / "models.json"
        _write_models(models, {
            "strategist": {
                "endpoint_url": "https://nvidia.example.com/v1",
                "model": "super",
                "recovery_fallbacks": [
                    {"endpoint_url": "https://a.example.com/v1", "model": "a"},
                    {"endpoint_url": "https://b.example.com/v1", "model": "b"},
                ],
            }
        })
        errors = validate_recovery_config(models, tmp_path / "e.json")
        assert any("at most one candidate" in error for error in errors)

    def test_same_provider_fails_startup(self, tmp_path):
        models = tmp_path / "models.json"
        _write_models(models, {
            "chair": {
                "endpoint_url": "https://opencode.ai/zen/v1",
                "model": "nemotron-3-ultra-free",
                "recovery_fallbacks": [{"endpoint_url": "https://opencode.ai/other/v1", "model": "other-model"}],
            }
        })
        errors = validate_recovery_config(models, tmp_path / "e.json")
        assert any("different provider" in error for error in errors)

    def test_context_fallback_duplicate_fails_startup(self, tmp_path):
        models = tmp_path / "models.json"
        _write_models(models, {
            "strategist": {
                "endpoint_url": "https://nvidia.example.com/v1",
                "model": "super",
                "context_fallbacks": [{"model": "nano"}],
                "recovery_fallbacks": [{"endpoint_url": "https://other.example.com/v1", "model": "nano"}],
            }
        })
        errors = validate_recovery_config(models, tmp_path / "e.json")
        assert any("context_fallback" in error and "separate" in error for error in errors)

    def test_missing_endpoint_fails_startup(self, tmp_path):
        models = tmp_path / "models.json"
        _write_models(models, {
            "chair": {
                "endpoint_url": "https://opencode.ai/zen/v1",
                "model": "nemotron-3-ultra-free",
                "recovery_fallbacks": [{"model": "other-model"}],
            }
        })
        errors = validate_recovery_config(models, tmp_path / "e.json")
        assert any("endpoint_url" in error for error in errors)


class TestOutcomeClassification:
    def test_clean(self):
        record = {
            "contract_passed": True, "initial_contract_passed": True,
            "provider_failed": False, "recovery_used": False,
        }
        assert classify_stage_outcome(record) == OUTCOME_CLEAN

    def test_recovered_via_recovery_hop(self):
        record = {
            "contract_passed": True, "initial_contract_passed": False,
            "provider_failed": True, "recovery_used": True, "recovery_succeeded": True,
        }
        assert classify_stage_outcome(record) == OUTCOME_RECOVERED

    def test_provider_inconclusive_after_failed_hop(self):
        record = {
            "contract_passed": False, "provider_failed": True,
            "recovery_used": True, "recovery_succeeded": False,
        }
        assert classify_stage_outcome(record) == OUTCOME_PROVIDER_INCONCLUSIVE

    def test_model_failure(self):
        record = {"contract_passed": False, "provider_failed": False}
        assert classify_stage_outcome(record) == OUTCOME_MODEL_FAILURE

    def test_provider_failure_never_becomes_model_success(self):
        # Provider trouble + a passing (non-recovery) path classifies as
        # RECOVERED, never CLEAN and never MODEL_FAILURE.
        record = {"contract_passed": True, "provider_failed": True, "recovery_used": False}
        assert classify_stage_outcome(record) == OUTCOME_RECOVERED

    def test_trace_outcome_prioritizes_harness_then_provider(self):
        trace = [
            {"status": "HARNESS_FAILURE", "failure_kind": "harness_error"},
            {"attempt_count": 1, "contract_passed": True, "initial_contract_passed": True, "provider_failed": False},
        ]
        assert classify_trace_outcome(trace)["outcome"] == "HARNESS_FAILURE"
        trace = [
            {"attempt_count": 1, "contract_passed": False, "provider_failed": True},
            {"attempt_count": 1, "contract_passed": True, "initial_contract_passed": True, "provider_failed": False},
        ]
        assert classify_trace_outcome(trace)["outcome"] == OUTCOME_PROVIDER_INCONCLUSIVE


def test_deny_map_covers_strategist_and_manager_only():
    assert "nvidia/nemotron-3-nano-30b-a3b" in DENY_RECOVERY_MODELS["strategist"]
    assert "nvidia/nemotron-3-nano-30b-a3b" in DENY_RECOVERY_MODELS["manager"]
    assert "chair" not in DENY_RECOVERY_MODELS
