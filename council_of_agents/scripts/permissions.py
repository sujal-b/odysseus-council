import os
import json
import hashlib
from typing import Optional, Tuple, Dict, Any, List

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
        self.workspace = os.path.normcase(os.path.realpath(workspace_path))
        self.owner = owner
        self.is_admin = is_admin
        self.session_permissions: set[str] = set()
        
        # Resolve workspace project hash
        self.workspace_hash = hashlib.sha256(self.workspace.encode("utf-8")).hexdigest()[:16]
        self.project_perms_dir = os.path.join("data", "council_permissions", self.workspace_hash)
        self.project_perms_path = os.path.join(self.project_perms_dir, "permissions.json")
        self.global_perms_dir = os.path.join("data", "council_permissions")
        self.global_perms_path = os.path.join(self.global_perms_dir, "global_permissions.json")
        
        self.project_permissions = self._load_permissions(self.project_perms_path)
        self.global_permissions = self._load_permissions(self.global_perms_path)

    def _load_permissions(self, path: str) -> List[str]:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f).get("allowed_paths", [])
            except Exception:
                pass
        return []

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
        resolved = os.path.normcase(os.path.realpath(target_path))
        
        if _is_sensitive_path(resolved):
            return False, "sensitive_path"

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
        resolved = os.path.normcase(os.path.realpath(path))
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

GLOBAL_REGISTRY = PermissionRegistry()
