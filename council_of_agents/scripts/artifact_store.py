"""Immutable content-addressed storage for Council evidence artifacts."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any

from council_of_agents.scripts.ledger_models import ArtifactRef


DEFAULT_MAX_ARTIFACT_BYTES = 4 * 1024 * 1024


class ArtifactIntegrityError(RuntimeError):
    pass


class ArtifactStore:
    def __init__(
        self,
        root: str | os.PathLike[str] | None = None,
        max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
    ):
        if root is None:
            from src.constants import DATA_DIR

            root = Path(DATA_DIR) / "council_artifacts"
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_artifact_bytes = max(1024, int(max_artifact_bytes))

    def put_bytes(
        self,
        data: bytes,
        *,
        summary: str = "",
        media_type: str = "application/octet-stream",
        provenance: dict[str, Any] | None = None,
    ) -> ArtifactRef:
        if not isinstance(data, bytes):
            raise TypeError("artifact data must be bytes")
        original_size = len(data)
        original_sha = hashlib.sha256(data).hexdigest()
        captured, truncated = self._bounded_capture(data)
        sha256 = hashlib.sha256(captured).hexdigest()
        target = self._path_for_hash(sha256)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            fd, temp_path = tempfile.mkstemp(prefix="artifact-", dir=str(target.parent))
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(captured)
                    handle.flush()
                    os.fsync(handle.fileno())
                if not target.exists():
                    os.replace(temp_path, target)
                else:
                    os.unlink(temp_path)
            except Exception:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
                raise
        metadata = dict(provenance or {})
        metadata.update({
            "original_size_bytes": original_size,
            "original_sha256": original_sha,
            "truncated": truncated,
        })
        return ArtifactRef(
            id=f"artifact-{sha256}",
            path=f"sha256/{sha256}",
            sha256=sha256,
            size_bytes=len(captured),
            media_type=media_type,
            summary=summary,
            provenance=metadata,
        )

    def put_text(self, text: str, **kwargs) -> ArtifactRef:
        kwargs.setdefault("media_type", "text/plain; charset=utf-8")
        return self.put_bytes(str(text).encode("utf-8"), **kwargs)

    def read(self, artifact: ArtifactRef) -> bytes:
        path = self._path_for_hash(artifact.sha256)
        data = path.read_bytes()
        actual = hashlib.sha256(data).hexdigest()
        if actual != artifact.sha256:
            raise ArtifactIntegrityError(
                f"Artifact hash mismatch for {artifact.id}: {actual}"
            )
        return data

    def _path_for_hash(self, sha256: str) -> Path:
        if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
            raise ValueError("invalid SHA-256 artifact key")
        return self.root / sha256[:2] / sha256[2:]

    def _bounded_capture(self, data: bytes) -> tuple[bytes, bool]:
        if len(data) <= self.max_artifact_bytes:
            return data, False
        marker = b"\n...[artifact truncated by policy]...\n"
        available = self.max_artifact_bytes - len(marker)
        head = int(available * 0.75)
        tail = available - head
        return data[:head] + marker + data[-tail:], True

