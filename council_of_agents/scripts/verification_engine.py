"""Deterministic, bounded verification adapters for Council criteria."""

from __future__ import annotations

import asyncio
import ast
import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from council_of_agents.scripts.artifact_store import ArtifactStore
from council_of_agents.scripts.ledger_models import ArtifactRef
from council_of_agents.scripts.ledger_models import Evidence, VerificationSpec


DEFAULT_ALLOWED_EXECUTABLES = {
    "python", "python3", "py",
    "pytest", "ruff", "mypy", "npm", "pnpm", "yarn", "cargo", "go", "dotnet",
}
DEFAULT_OUTPUT_LIMIT = 16 * 1024
_SENSITIVE_TERMS = ("token", "password", "passwd", "secret", "api_key", "api-key")


class VerificationPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class VerificationPolicy:
    allowed_executables: frozenset[str] = field(
        default_factory=lambda: frozenset(DEFAULT_ALLOWED_EXECUTABLES)
    )
    max_timeout_seconds: float = 600.0
    output_limit_bytes: int = DEFAULT_OUTPUT_LIMIT
    inherited_environment: frozenset[str] = field(default_factory=lambda: frozenset({
        "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE"
    }))


def _executable_name(value: str) -> str:
    name = Path(value).name.lower()
    return name[:-4] if name.endswith(".exe") else name


def _bounded_text(data: bytes, limit: int) -> tuple[str, bool]:
    if len(data) <= limit:
        return data.decode("utf-8", errors="replace"), False
    marker = b"\n...[verification output truncated]...\n"
    available = max(0, limit - len(marker))
    head = int(available * 0.75)
    tail = available - head
    compacted = data[:head] + marker + data[-tail:]
    return compacted.decode("utf-8", errors="replace"), True


def _failure_signature(adapter: str, discriminator: str, details: str = "") -> str:
    digest = hashlib.sha256(details[-2000:].encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"{adapter}:{discriminator}:{digest}"


def _redact_argv(argv: list[str]) -> list[str]:
    redacted = []
    redact_next = False
    for value in argv:
        lower = value.lower()
        if redact_next:
            redacted.append("[REDACTED]")
            redact_next = False
            continue
        if any(lower == f"--{term}" for term in _SENSITIVE_TERMS):
            redacted.append(value)
            redact_next = True
            continue
        if "=" in value:
            key, _ = value.split("=", 1)
            if any(term in key.lower() for term in _SENSITIVE_TERMS):
                redacted.append(f"{key}=[REDACTED]")
                continue
        redacted.append(value)
    return redacted


def _redact_output(data: bytes, configured_values: Any = None) -> bytes:
    text = data.decode("utf-8", errors="replace")
    secrets = []
    values = configured_values or []
    values = [values] if isinstance(values, str) else values
    secrets.extend(str(value) for value in values if len(str(value)) >= 4)
    for key, value in os.environ.items():
        if any(term in key.lower() for term in _SENSITIVE_TERMS) and len(value) >= 6:
            secrets.append(value)
    for secret in sorted(set(secrets), key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    return text.encode("utf-8")


class VerificationEngine:
    def __init__(
        self,
        workspace: str | os.PathLike[str],
        policy: VerificationPolicy | None = None,
        artifact_store: ArtifactStore | None = None,
    ):
        self.workspace = Path(workspace).resolve()
        self.policy = policy or VerificationPolicy()
        self.artifact_store = artifact_store
        self.artifacts: dict[str, ArtifactRef] = {}

    async def verify(
        self,
        spec: VerificationSpec,
        *,
        criterion_id: str,
        task_id: str | None = None,
        verifier: str = "verification_engine",
        workspace_revision: str | None = None,
        workspace_file_hashes: dict[str, str] | None = None,
    ) -> Evidence:
        self.artifacts = {}
        started = asyncio.get_running_loop().time()
        try:
            if spec.adapter == "file":
                passed, details = self._verify_file(spec.config)
            elif spec.adapter == "command":
                passed, details = await self._verify_command(spec.config)
            else:
                raise VerificationPolicyError(f"Unsupported verification adapter: {spec.adapter}")
            signature = None if passed else _failure_signature(
                spec.adapter, str(details.get("reason", "failed")), str(details)
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            passed = False
            details = {"reason": "policy_or_adapter_error", "error": str(exc)}
            signature = _failure_signature(spec.adapter, type(exc).__name__, str(exc))
        duration_ms = int((asyncio.get_running_loop().time() - started) * 1000)
        return Evidence(
            id=f"evidence-{uuid4().hex}",
            criterion_id=criterion_id,
            task_id=task_id,
            adapter=spec.adapter,
            verifier=verifier,
            passed=passed,
            details=details,
            artifact_ids=list(self.artifacts),
            workspace_revision=workspace_revision,
            file_hashes=self._evidence_file_hashes(
                spec, passed, details, workspace_file_hashes or {}
            ),
            failure_signature=signature,
            duration_ms=duration_ms,
        )

    @staticmethod
    def _evidence_file_hashes(spec, passed, details, workspace_file_hashes):
        hashes = dict(workspace_file_hashes)
        if passed and spec.adapter == "file" and details.get("sha256"):
            hashes[details["path"]] = details["sha256"]
        return hashes

    @staticmethod
    def _validate_command_argv(argv: list[str]) -> None:
        if len(argv) >= 3 and argv[0].lower() in ("python", "python3", "py") and argv[1] == "-c":
            try:
                ast.parse(argv[2])
            except SyntaxError as exc:
                raise VerificationPolicyError(
                    "verification command is malformed: python -c code does not parse "
                    f"({exc.msg} at line {exc.lineno}); the task artifact cannot be "
                    "blamed for a broken verification spec"
                ) from exc

    def _scoped_path(self, raw_path: Any) -> Path:
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise VerificationPolicyError("file adapter requires a non-empty relative path")
        candidate = (self.workspace / raw_path).resolve()
        try:
            candidate.relative_to(self.workspace)
        except ValueError as exc:
            raise VerificationPolicyError("verification path escapes workspace") from exc
        return candidate

    def _verify_file(self, config: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        path = self._scoped_path(config.get("path"))
        should_exist = bool(config.get("exists", True))
        exists = path.exists()
        details: dict[str, Any] = {
            "path": str(path.relative_to(self.workspace)),
            "exists": exists,
            "expected_exists": should_exist,
        }
        if exists != should_exist:
            details["reason"] = "existence_mismatch"
            return False, details
        if not exists:
            return True, details
        if not path.is_file():
            details["reason"] = "not_a_file"
            return False, details

        raw = path.read_bytes()
        details["size"] = len(raw)
        details["sha256"] = hashlib.sha256(raw).hexdigest()
        if config.get("sha256") and details["sha256"] != config["sha256"]:
            details["reason"] = "hash_mismatch"
            return False, details
        if "min_size" in config and len(raw) < int(config["min_size"]):
            details["reason"] = "below_min_size"
            return False, details
        if "max_size" in config and len(raw) > int(config["max_size"]):
            details["reason"] = "above_max_size"
            return False, details
        text = raw.decode("utf-8", errors="replace")
        required = config.get("contains", [])
        required = [required] if isinstance(required, str) else list(required)
        missing = [needle for needle in required if str(needle) not in text]
        forbidden = config.get("not_contains", [])
        forbidden = [forbidden] if isinstance(forbidden, str) else list(forbidden)
        present = [needle for needle in forbidden if str(needle) in text]
        if missing or present:
            details.update({"reason": "content_mismatch", "missing": missing, "forbidden_present": present})
            return False, details
        return True, details

    async def _verify_command(self, config: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        argv = config.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(v, str) for v in argv):
            raise VerificationPolicyError("command adapter requires a non-empty string argv list")
        executable = _executable_name(argv[0])
        allowed = {_executable_name(v) for v in self.policy.allowed_executables}
        if executable not in allowed:
            raise VerificationPolicyError(f"verification executable is not allowed: {executable}")
        self._validate_command_argv(argv)
        timeout = float(config.get("timeout_seconds", self.policy.max_timeout_seconds))
        if timeout <= 0 or timeout > self.policy.max_timeout_seconds:
            raise VerificationPolicyError("verification timeout exceeds policy")
        env = {key: value for key, value in os.environ.items() if key in self.policy.inherited_environment}
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(self.workspace),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        timed_out = False
        try:
            stdout_raw, stderr_raw = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            timed_out = True
            proc.kill()
            stdout_raw, stderr_raw = await proc.communicate()
        stdout_raw = _redact_output(stdout_raw, config.get("redact_values"))
        stderr_raw = _redact_output(stderr_raw, config.get("redact_values"))
        safe_argv = _redact_argv(argv)
        stdout, stdout_truncated = _bounded_text(stdout_raw, self.policy.output_limit_bytes)
        stderr, stderr_truncated = _bounded_text(stderr_raw, self.policy.output_limit_bytes)
        if self.artifact_store is not None:
            for stream_name, raw in (("stdout", stdout_raw), ("stderr", stderr_raw)):
                if not raw:
                    continue
                artifact = self.artifact_store.put_bytes(
                    raw,
                    summary=f"Verification command {stream_name}",
                    media_type="text/plain; charset=utf-8",
                    provenance={"stream": stream_name, "argv": safe_argv},
                )
                self.artifacts[artifact.id] = artifact
            # Ledger details keep only a diagnostic preview; full bounded logs
            # are content-addressed artifacts.
            stdout, stdout_truncated = _bounded_text(stdout_raw, min(2048, self.policy.output_limit_bytes))
            stderr, stderr_truncated = _bounded_text(stderr_raw, min(2048, self.policy.output_limit_bytes))
        details = {
            "argv": safe_argv,
            "exit_code": proc.returncode,
            "timed_out": timed_out,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
        }
        expected_exit = int(config.get("expected_exit_code", 0))
        passed = not timed_out and proc.returncode == expected_exit
        if not passed:
            details["reason"] = "timeout" if timed_out else "exit_code_mismatch"
        return passed, details
