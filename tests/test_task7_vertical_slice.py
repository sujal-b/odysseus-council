import pytest

from scripts import task7_vertical_slice as task7


def test_evidence_run_directory_is_confined_and_not_reused(monkeypatch, tmp_path):
    monkeypatch.setattr(task7, "EVIDENCE_ROOT", tmp_path)

    created = task7._prepare_evidence_dir("task7-run-1", fresh=True)

    assert created == tmp_path / "task7-run-1"
    with pytest.raises(FileExistsError):
        task7._prepare_evidence_dir("task7-run-1", fresh=True)
    assert task7._prepare_evidence_dir("task7-run-1", fresh=False) == created
    with pytest.raises(ValueError):
        task7._evidence_path(tmp_path, "../existing-run")


def test_live_gate_payload_is_hints_only_and_auditable():
    context = task7._live_gate_context({
        "workspace": "C:\\sensitive\\repo",
        "repository_context": "core/ tests/ message_count",
    })
    assert context == {"workspace": "", "repository_context": "core/ tests/ message_count"}

    audit = task7._gate_audit_metadata({"trace": [{
        "agent": "strategist",
        "stage": "strategist",
        "endpoint": "https://integrate.api.nvidia.com/v1",
        "model": "test-model",
        "messages": [{"role": "user", "content": "secret payload"}],
    }]}, {"verdict": "APPROVED"})

    assert audit["requests"][0]["provider"] == "https://integrate.api.nvidia.com/v1"
    assert len(audit["requests"][0]["payload_sha256"]) == 64
    assert len(audit["trace_sha256"]) == 64
    assert audit["outcome"] == {"verdict": "APPROVED"}
    assert "secret payload" not in str(audit)

def test_live_gate_accepts_only_approved_hosts():
    approved = {
        "https://integrate.api.nvidia.com/v1/chat/completions",
        "https://opencode.ai/zen/v1/chat/completions",
    }
    assert task7._approved_gate_endpoints(approved) == approved
    with pytest.raises(RuntimeError, match="not approved"):
        task7._approved_gate_endpoints({"https://example.invalid/v1"})