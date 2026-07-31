"""Stable, scope-bounded workspace revision hashes for verification evidence."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path


class WorkspaceScopeError(ValueError):
    pass


class WorkspaceConflictError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkspaceRevision:
    revision: str
    file_hashes: dict[str, str]


class WorkspaceWriteGuard:
    """Optimistic, scope-bound guard checked immediately before each write."""

    def __init__(
        self, workspace, write_scopes, base_hashes, *, workspace_root=False,
        task_id="", enforce_channels=False,
    ):
        self.root = Path(workspace).resolve()
        self.write_scopes = [self.root] if workspace_root else [self._resolve(scope) for scope in write_scopes]
        self.expected = dict(base_hashes)
        self.task_id = str(task_id or "")
        self.workspace_root = bool(workspace_root)
        self.enforce_channels = bool(enforce_channels)

    def _resolve(self, value):
        candidate = (self.root / str(value)).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise WorkspaceScopeError(f"workspace scope escapes root: {value}") from exc
        return candidate

    @staticmethod
    def _raw_path(tool, content):
        text = str(content or "").strip()
        if text.startswith("{"):
            try:
                payload = json.loads(text)
                if isinstance(payload, dict) and payload.get("path"):
                    return str(payload["path"])
            except Exception:
                pass
            # Some models emit JSON with unescaped quotes inside string values
            # (e.g. docstring-style `"""`). The path is still declared in the
            # text, and the scope/conflict checks below are the real
            # authorization, so extract it leniently instead of hard-stopping
            # the task on JSON cosmetics.
            match = re.search(r'"path"\s*:\s*"([^"]+)"', text)
            if match:
                return match.group(1)
        return text.split("\n", 1)[0].strip() if tool == "write_file" else ""

    def attempted_path(self, tool, content):
        """Best-effort relative path for passive diagnostics; never authorizes a write."""
        try:
            raw_path = self._raw_path(tool, content)
            if not raw_path:
                return ""
            target = self._resolve(raw_path)
            return target.relative_to(self.root).as_posix()
        except Exception:
            return str(self._raw_path(tool, content) or "")

    def _target(self, tool, content):
        raw_path = self._raw_path(tool, content)
        if not raw_path:
            raise WorkspaceScopeError(f"{tool} call has no parseable path")
        target = self._resolve(raw_path)
        if not any(target == scope or scope in target.parents for scope in self.write_scopes):
            raise WorkspaceScopeError(f"write target is outside declared scope: {raw_path}")
        return target

    def _current_hash(self, target):
        if not target.exists():
            return "<missing>"
        if not target.is_file():
            raise WorkspaceScopeError(f"write target is not a file: {target}")
        return hashlib.sha256(target.read_bytes()).hexdigest()

    def check_before_write(self, tool, content):
        target = self._target(tool, content)
        rel = target.relative_to(self.root).as_posix()
        current = self._current_hash(target)
        expected = self.expected.get(rel, "<missing>")
        if current != expected:
            raise WorkspaceConflictError(
                f"stale workspace write blocked for {rel}: expected {expected}, found {current}"
            )
        return rel

    def check_tool_channel(self, tool):
        safe = {"read_file", "ls", "glob", "grep", "write_file", "edit_file"}
        if tool not in safe:
            raise WorkspaceScopeError(
                f"tool '{tool}' is not compatible with guarded workspace execution"
            )

    def record_after_write(self, tool, content):
        target = self._target(tool, content)
        rel = target.relative_to(self.root).as_posix()
        self.expected[rel] = self._current_hash(target)


def snapshot_workspace(
    workspace: str | Path,
    scopes: list[str],
    *,
    max_files: int = 5000,
) -> WorkspaceRevision:
    root = Path(workspace).resolve()
    files: dict[str, str] = {}
    for scope in sorted(set(str(value) for value in scopes if str(value).strip())):
        candidate = (root / scope).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise WorkspaceScopeError(f"workspace scope escapes root: {scope}") from exc
        if not candidate.exists():
            rel = Path(scope).as_posix()
            files[rel] = "<missing>"
            continue
        candidates = [candidate] if candidate.is_file() else sorted(
            path for path in candidate.rglob("*") if path.is_file()
        )
        for path in candidates:
            resolved = path.resolve()
            try:
                rel = resolved.relative_to(root).as_posix()
            except ValueError as exc:
                raise WorkspaceScopeError(f"workspace symlink escapes root: {path}") from exc
            files[rel] = hashlib.sha256(resolved.read_bytes()).hexdigest()
            if len(files) > max_files:
                raise WorkspaceScopeError(
                    f"workspace revision exceeds {max_files} files; narrow the task scope"
                )
    digest = hashlib.sha256()
    for rel, file_hash in sorted(files.items()):
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_hash.encode("ascii"))
        digest.update(b"\n")
    return WorkspaceRevision(revision=digest.hexdigest(), file_hashes=files)
