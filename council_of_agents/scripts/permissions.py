import os
import json
import hashlib
from typing import Optional, Tuple, Dict, Any, List


def resolve_council_workspace(raw_path: str, *, allow_missing: bool = True) -> str:
    """Return a canonical, non-sensitive Council workspace path.

    A workspace explicitly supplied for a Council session is an authority
    boundary, not a hint.  Callers must validate it once and then pass this
    canonical path through every tool and permission check.  Relative paths
    are resolved by the caller's process, so the API should prefer absolute
    paths for reproducible sessions.
    """
    raw = str(raw_path or "").strip()
    if not raw:
        raise ValueError("workspace path is required")

    from src.tool_execution import _is_sensitive_path

    expanded = os.path.expanduser(raw)
    resolved = os.path.normcase(os.path.realpath(expanded))
    if _is_sensitive_path(resolved):
        raise ValueError("workspace is inside a sensitive directory or filename")

    if os.path.exists(resolved):
        if not os.path.isdir(resolved):
            raise ValueError("workspace path must be a directory")
    elif not allow_missing:
        raise ValueError("workspace directory does not exist")

    return resolved

class PermissionCheckFailed(Exception):
    def __init__(self, action: str, target: str, tool_block: Any):
        self.action = action
        self.target = target
        self.tool_block = tool_block
        super().__init__(f"Permission check failed for {action} on {target}")

class PermissionRequired(Exception):
    def __init__(self, action: str, target: str, tool_block: Any, permission_id: str,
                 round_response: str, native_tool_calls: list, round_num: int, round_reasoning: str = ""):
        self.action = action
        self.target = target
        self.tool_block = tool_block
        self.permission_id = permission_id
        self.round_response = round_response
        self.native_tool_calls = native_tool_calls
        self.round_num = round_num
        self.round_reasoning = round_reasoning
        super().__init__(f"Permission required for {action} on {target}")

def _path_contains(parent: str, child: str) -> bool:
    try:
        parent = os.path.normcase(os.path.realpath(parent))
        child = os.path.normcase(os.path.realpath(child))
        common = os.path.commonpath([parent, child])
        return common == parent
    except (ValueError, OSError):
        return False

class PermissionManager:
    def __init__(self, workspace_path: str, owner: str, is_admin: bool):
        self.workspace = resolve_council_workspace(workspace_path)
        self.owner = owner
        self.is_admin = is_admin
        self.session_permissions: set[str] = set()
        
        # Resolve workspace project hash
        self.workspace_hash = hashlib.sha256(self.workspace.encode("utf-8")).hexdigest()[:16]
        from src.constants import DATA_DIR
        self.project_perms_dir = os.path.join(DATA_DIR, "council_permissions", self.workspace_hash)
        self.project_perms_path = os.path.join(self.project_perms_dir, "permissions.json")
        self.global_perms_dir = os.path.join(DATA_DIR, "council_permissions")
        self.global_perms_path = os.path.join(self.global_perms_dir, "global_permissions.json")
        
        self.project_permissions = self._load_permissions(self.project_perms_path)
        self.global_permissions = self._load_permissions(self.global_perms_path)

    def _load_permissions(self, path: str) -> List[str]:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    values = json.load(f).get("allowed_paths", [])
                    return [self._resolve_target_path(value) for value in values if value]
            except Exception:
                pass
        return []

    def _resolve_target_path(self, target_path: str) -> str:
        """Resolve permission targets relative to the active workspace.

        Tool payloads commonly use ``.`` or a project-relative path.  Using
        the process cwd here made ``.`` point at the Odysseus service repo
        instead of the session project, causing false permission prompts and
        the old unsafe fallback behavior.
        """
        expanded = os.path.expanduser(str(target_path or "").strip())
        if not os.path.isabs(expanded):
            expanded = os.path.join(self.workspace, expanded)
        return os.path.normcase(os.path.realpath(expanded))

    def _save_permissions(self, path: str, perms: List[str]):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"allowed_paths": perms}, f, indent=2)
        except Exception:
            pass

    def check_path_allowed(self, target_path: str) -> Tuple[bool, str]:
        # Sensitive path/files check first
        from src.tool_execution import _is_sensitive_path
        resolved = self._resolve_target_path(target_path)
        
        if _is_sensitive_path(resolved):
            return False, "sensitive_path"

        # Explicitly whitelist the council's internal state directory.
        # This prevents the strategist from crashing when the project workspace
        # is a subdirectory and this path evaluates as "outside" it.
        from src.constants import DATA_DIR
        council_ws = os.path.normcase(os.path.realpath(os.path.join(DATA_DIR, "council_workspace")))
        if _path_contains(council_ws, resolved) or resolved == council_ws:
            return True, "council_internal"

        # Check standard workspace containment
        if _path_contains(self.workspace, resolved):
            return True, "within_workspace"

        # If outside workspace and owner is not admin, reject immediately
        if not self.is_admin:
            return False, "non_admin_escaped_jail"

        # Check existing permissions (session, project, global)
        for allowed in list(self.session_permissions) + self.project_permissions + self.global_permissions:
            if _path_contains(allowed, resolved):
                return True, "pre_approved"

        return False, "requires_approval"

    def grant(self, path: str, level: str):
        resolved = self._resolve_target_path(path)
        if level == "once":
            self.session_permissions.add(resolved)
        elif level == "project":
            if resolved not in self.project_permissions:
                self.project_permissions.append(resolved)
                self._save_permissions(self.project_perms_path, self.project_permissions)
        elif level == "global" and self.is_admin:
            if resolved not in self.global_permissions:
                self.global_permissions.append(resolved)
                self._save_permissions(self.global_perms_path, self.global_permissions)

# Global registry to hold pending Events and responses
class PermissionRegistry:
    def __init__(self):
        import asyncio
        self.pending_events: Dict[str, asyncio.Event] = {}
        self.results: Dict[str, dict] = {}
        # Permission IDs are globally unique, but the owner session still
        # matters when one Council run is cancelled while another is blocked.
        self.session_ids: Dict[str, str] = {}

GLOBAL_REGISTRY = PermissionRegistry()
