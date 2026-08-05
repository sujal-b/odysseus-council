import sys

import pytest

from council_of_agents.scripts.artifact_store import ArtifactStore
from council_of_agents.scripts.ledger_models import VerificationSpec
from council_of_agents.scripts.verification_engine import (
    VerificationEngine,
    VerificationPolicy,
)


@pytest.mark.asyncio
async def test_file_adapter_records_hash_and_content_evidence(tmp_path):
    (tmp_path / "result.txt").write_text("ready\n", encoding="utf-8")
    engine = VerificationEngine(tmp_path)
    evidence = await engine.verify(
        VerificationSpec(adapter="file", config={
            "path": "result.txt", "contains": "ready", "not_contains": "TODO"
        }),
        criterion_id="AC-1",
    )
    assert evidence.passed is True
    assert len(evidence.details["sha256"]) == 64
    assert evidence.file_hashes["result.txt"] == evidence.details["sha256"]
    assert evidence.failure_signature is None


@pytest.mark.asyncio
async def test_command_rejects_corrupted_python_c_argv(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(
        "def health():\n    return {'status': 'ok'}\n", encoding="utf-8"
    )
    engine = VerificationEngine(tmp_path)
    corrupted_code = "import src.app; assert hasattr(src.app, 'health'); assert src.app.health() == {'status': 'ok'}}},"
    evidence = await engine.verify(
        VerificationSpec(adapter="command", config={
            "argv": ["python", "-c", corrupted_code, "{"],
        }),
        criterion_id="AC-9",
    )
    assert evidence.passed is False
    assert "malformed" in evidence.details["error"]


@pytest.mark.asyncio
async def test_command_rejects_syntax_error_in_python_c_code(tmp_path):
    engine = VerificationEngine(tmp_path)
    evidence = await engine.verify(
        VerificationSpec(adapter="command", config={
            "argv": ["python", "-c", "if True print('oops'"],
        }),
        criterion_id="AC-10",
    )
    assert evidence.passed is False
    assert "does not parse" in evidence.details["error"]


@pytest.mark.asyncio
async def test_command_accepts_valid_python_c_with_semicolons(tmp_path):
    engine = VerificationEngine(tmp_path)
    evidence = await engine.verify(
        VerificationSpec(adapter="command", config={
            "argv": ["python", "-c", "x = 1; assert x == 1; print('ok')"],
        }),
        criterion_id="AC-11",
    )
    assert evidence.passed is True
    assert evidence.details["exit_code"] == 0


@pytest.mark.asyncio
async def test_command_does_not_gate_non_python_argv(tmp_path):
    engine = VerificationEngine(tmp_path, _python_policy())
    evidence = await engine.verify(
        VerificationSpec(adapter="command", config={
            "argv": [sys.executable, "-c", "import sys; print(sys.version_info[0])"],
        }),
        criterion_id="AC-12",
    )
    assert evidence.passed is True


@pytest.mark.asyncio
async def test_file_adapter_rejects_workspace_escape(tmp_path):
    engine = VerificationEngine(tmp_path)
    evidence = await engine.verify(
        VerificationSpec(adapter="file", config={"path": "../secret.txt"}),
        criterion_id="AC-1",
    )
    assert evidence.passed is False
    assert "escapes workspace" in evidence.details["error"]


def _python_policy(timeout=2.0, output_limit=1024):
    return VerificationPolicy(
        allowed_executables=frozenset({"python"}),
        max_timeout_seconds=timeout,
        output_limit_bytes=output_limit,
    )


@pytest.mark.asyncio
async def test_command_adapter_uses_argv_without_shell(tmp_path):
    engine = VerificationEngine(tmp_path, _python_policy())
    evidence = await engine.verify(
        VerificationSpec(adapter="command", config={
            "argv": [sys.executable, "-c", "print('verified')"]
        }),
        criterion_id="AC-2",
    )
    assert evidence.passed is True
    assert evidence.details["stdout"].strip() == "verified"
    assert evidence.details["exit_code"] == 0


@pytest.mark.asyncio
async def test_command_adapter_rejects_unapproved_executable(tmp_path):
    engine = VerificationEngine(tmp_path, _python_policy())
    evidence = await engine.verify(
        VerificationSpec(adapter="command", config={"argv": ["cmd", "/c", "echo unsafe"]}),
        criterion_id="AC-3",
    )
    assert evidence.passed is False
    assert "not allowed" in evidence.details["error"]


@pytest.mark.asyncio
async def test_default_policy_allows_workspace_python_interpreter(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(
        "def health():\n    return {'status': 'ok'}\n", encoding="utf-8"
    )
    engine = VerificationEngine(tmp_path)
    evidence = await engine.verify(
        VerificationSpec(adapter="command", config={
            "argv": ["python", "-c",
                     "from src.app import health; assert health() == {'status': 'ok'}"]
        }),
        criterion_id="AC-8",
    )
    assert evidence.passed is True
    assert evidence.details["exit_code"] == 0


@pytest.mark.asyncio
async def test_command_timeout_is_failed_evidence(tmp_path):
    engine = VerificationEngine(tmp_path, _python_policy(timeout=0.05))
    evidence = await engine.verify(
        VerificationSpec(adapter="command", config={
            "argv": [sys.executable, "-c", "import time; time.sleep(1)"],
            "timeout_seconds": 0.05,
        }),
        criterion_id="AC-4",
    )
    assert evidence.passed is False
    assert evidence.details["timed_out"] is True
    assert evidence.failure_signature.startswith("command:timeout:")


@pytest.mark.asyncio
async def test_command_output_is_bounded(tmp_path):
    engine = VerificationEngine(tmp_path, _python_policy(output_limit=256))
    evidence = await engine.verify(
        VerificationSpec(adapter="command", config={
            "argv": [sys.executable, "-c", "print('x' * 5000)"]
        }),
        criterion_id="AC-5",
    )
    assert evidence.passed is True
    assert evidence.details["stdout_truncated"] is True
    assert len(evidence.details["stdout"].encode("utf-8")) <= 256


@pytest.mark.asyncio
async def test_command_logs_are_externalized_as_artifacts(tmp_path):
    artifact_store = ArtifactStore(tmp_path / "artifacts", max_artifact_bytes=1024)
    engine = VerificationEngine(
        tmp_path,
        _python_policy(output_limit=512),
        artifact_store=artifact_store,
    )
    evidence = await engine.verify(
        VerificationSpec(adapter="command", config={
            "argv": [sys.executable, "-c", "print('z' * 5000)"]
        }),
        criterion_id="AC-6",
        workspace_revision="revision-1",
        workspace_file_hashes={"target.py": "abc"},
    )

    assert evidence.passed is True
    assert evidence.workspace_revision == "revision-1"
    assert evidence.file_hashes == {"target.py": "abc"}
    assert len(evidence.artifact_ids) == 1
    artifact = engine.artifacts[evidence.artifact_ids[0]]
    assert len(artifact_store.read(artifact)) <= 1024


@pytest.mark.asyncio
async def test_command_secrets_are_redacted_before_persistence(tmp_path):
    artifact_store = ArtifactStore(tmp_path / "artifacts")
    engine = VerificationEngine(
        tmp_path, _python_policy(), artifact_store=artifact_store
    )
    secret = "super-secret-value"
    evidence = await engine.verify(
        VerificationSpec(adapter="command", config={
            "argv": [sys.executable, "-c", f"print('{secret}')", "--token", secret],
            "redact_values": [secret],
        }),
        criterion_id="AC-7",
    )

    assert secret not in evidence.details["stdout"]
    assert evidence.details["argv"][-1] == "[REDACTED]"
    artifact = engine.artifacts[evidence.artifact_ids[0]]
    assert secret.encode() not in artifact_store.read(artifact)
