import os
import json
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
import pytest

from council_of_agents.scripts.permissions import (
    PermissionManager,
    PermissionCheckFailed,
    PermissionRequired,
    GLOBAL_REGISTRY,
    _path_contains
)
from src.tool_execution import (
    execute_tool_block,
    _resolve_tool_path_in_workspace,
    _extract_tool_path
)
from src.agent_tools import ToolBlock
from src.agent_tools.filesystem_tools import LsTool, GlobTool, GrepTool

def test_path_contains(tmp_path):
    ws = str(tmp_path / "workspace")
    other_ws = str(tmp_path / "workspace-other")
    os.makedirs(ws, exist_ok=True)
    os.makedirs(other_ws, exist_ok=True)

    # 1. Normal case
    assert _path_contains(ws, os.path.join(ws, "sub", "file.txt")) is True
    # 2. Same path
    assert _path_contains(ws, ws) is True
    # 3. Escaping sibling check (like /project matching /project-other)
    assert _path_contains(ws, os.path.join(other_ws, "file.txt")) is False
    # 4. Sibling directory itself
    assert _path_contains(ws, other_ws) is False
    # 5. Parent directory
    assert _path_contains(os.path.join(ws, "sub"), ws) is False

def test_permission_manager_containment_and_gating(tmp_path):
    ws = os.path.normcase(os.path.realpath(str(tmp_path / "workspace")))
    os.makedirs(ws, exist_ok=True)

    # We patch project and global perms paths on PermissionManager so it uses temp files.
    pm_admin = PermissionManager(workspace_path=ws, owner="admin_user", is_admin=True)
    pm_admin.project_perms_path = str(tmp_path / "project_perms.json")
    pm_admin.global_perms_path = str(tmp_path / "global_perms.json")
    
    # Reload empty permissions
    pm_admin.project_permissions = []
    pm_admin.global_permissions = []

    pm_user = PermissionManager(workspace_path=ws, owner="user_user", is_admin=False)
    pm_user.project_perms_path = str(tmp_path / "project_perms.json")
    pm_user.global_perms_path = str(tmp_path / "global_perms.json")
    pm_user.project_permissions = []
    pm_user.global_permissions = []

    # Inside workspace path
    in_ws = os.path.join(ws, "file.txt")
    # Outside workspace path
    out_ws = os.path.join(str(tmp_path), "outside.txt")
    # Sensitive path
    sensitive_ws = os.path.join(ws, ".ssh", "id_rsa")

    # 1. Check inside workspace
    allowed, reason = pm_admin.check_path_allowed(in_ws)
    assert allowed is True
    assert reason == "within_workspace"

    allowed, reason = pm_user.check_path_allowed(in_ws)
    assert allowed is True
    assert reason == "within_workspace"

    # 2. Check sensitive path
    allowed, reason = pm_admin.check_path_allowed(sensitive_ws)
    assert allowed is False
    assert reason == "sensitive_path"

    allowed, reason = pm_user.check_path_allowed(sensitive_ws)
    assert allowed is False
    assert reason == "sensitive_path"

    # 3. Check outside workspace for non-admin user (should fail with non_admin_escaped_jail)
    allowed, reason = pm_user.check_path_allowed(out_ws)
    assert allowed is False
    assert reason == "non_admin_escaped_jail"

    # 4. Check outside workspace for admin user (should require approval)
    allowed, reason = pm_admin.check_path_allowed(out_ws)
    assert allowed is False
    assert reason == "requires_approval"

    # 5. Grant permissions and verify check
    # 5a. Session level (once)
    pm_admin.grant(out_ws, "once")
    allowed, reason = pm_admin.check_path_allowed(out_ws)
    assert allowed is True
    assert reason == "pre_approved"
    
    # Verify regular user does not have session permission of admin user
    allowed, reason = pm_user.check_path_allowed(out_ws)
    assert allowed is False
    assert reason == "non_admin_escaped_jail"

    # 5b. Project level
    pm_admin.grant(out_ws, "project")
    # Initialize a new PermissionManager for same workspace to test loading project perms
    pm_admin2 = PermissionManager(workspace_path=ws, owner="admin_user", is_admin=True)
    pm_admin2.project_perms_path = str(tmp_path / "project_perms.json")
    pm_admin2.global_perms_path = str(tmp_path / "global_perms.json")
    pm_admin2.project_permissions = pm_admin2._load_permissions(pm_admin2.project_perms_path)
    
    allowed, reason = pm_admin2.check_path_allowed(out_ws)
    assert allowed is True
    assert reason == "pre_approved"

    # 5c. Global level
    global_path = os.path.join(str(tmp_path), "global_file.txt")
    pm_admin.grant(global_path, "global")
    pm_admin3 = PermissionManager(workspace_path=ws, owner="admin_user", is_admin=True)
    pm_admin3.project_perms_path = str(tmp_path / "project_perms.json")
    pm_admin3.global_perms_path = str(tmp_path / "global_perms.json")
    pm_admin3.global_permissions = pm_admin3._load_permissions(pm_admin3.global_perms_path)

    allowed, reason = pm_admin3.check_path_allowed(global_path)
    assert allowed is True
    assert reason == "pre_approved"

@pytest.mark.asyncio
async def test_global_registry_wait_and_resume():
    permission_id = "test_perm_id"
    event = asyncio.Event()
    GLOBAL_REGISTRY.pending_events[permission_id] = event
    GLOBAL_REGISTRY.results.pop(permission_id, None)

    async def wait_task():
        await GLOBAL_REGISTRY.pending_events[permission_id].wait()
        return GLOBAL_REGISTRY.results.get(permission_id)

    task = asyncio.create_task(wait_task())
    await asyncio.sleep(0.01)  # allow task to yield and block

    # Simulate response from API
    GLOBAL_REGISTRY.results[permission_id] = {"approved": True}
    event.set()

    res = await task
    assert res == {"approved": True}
    GLOBAL_REGISTRY.pending_events.pop(permission_id, None)
    GLOBAL_REGISTRY.results.pop(permission_id, None)

@pytest.mark.asyncio
async def test_security_gating_tool_execution(tmp_path):
    ws = os.path.normcase(os.path.realpath(str(tmp_path / "workspace")))
    os.makedirs(ws, exist_ok=True)
    
    in_ws_file = os.path.join(ws, "test.txt")
    out_ws_file = os.path.join(str(tmp_path), "outside.txt")
    sensitive_file = os.path.join(ws, ".ssh", "id_rsa")

    block_read_in = ToolBlock("read_file", json.dumps({"path": in_ws_file}))
    block_read_out = ToolBlock("read_file", json.dumps({"path": out_ws_file}))
    block_read_sensitive = ToolBlock("read_file", json.dumps({"path": sensitive_file}))

    # Mock owner_is_admin_or_single_user so that "admin_user" is admin and "regular_user" is not
    with patch("src.tool_security.owner_is_admin_or_single_user", side_effect=lambda owner: owner == "admin_user"):
        # 1. Non-admin accessing outside workspace -> should raise PermissionError immediately
        with pytest.raises(PermissionError) as excinfo:
            await execute_tool_block(block_read_out, workspace=ws, owner="regular_user")
        assert "outside the workspace (non-admin)" in str(excinfo.value)

        # 2. Anyone accessing sensitive path -> should raise PermissionError immediately
        with pytest.raises(PermissionError) as excinfo:
            await execute_tool_block(block_read_sensitive, workspace=ws, owner="admin_user")
        assert "inside a sensitive directory" in str(excinfo.value)

        # 3. Admin accessing outside workspace -> should raise PermissionCheckFailed (so it gets caught and converted in loop)
        with pytest.raises(PermissionCheckFailed) as excinfo:
            await execute_tool_block(block_read_out, workspace=ws, owner="admin_user")
        assert excinfo.value.action == "read_file"
        assert excinfo.value.target == out_ws_file

@pytest.mark.asyncio
async def test_filesystem_tools_respect_workspace(tmp_path):
    ws = os.path.normcase(os.path.realpath(str(tmp_path / "workspace")))
    os.makedirs(ws, exist_ok=True)

    # Write a test file in workspace
    sub_dir = os.path.join(ws, "sub")
    os.makedirs(sub_dir, exist_ok=True)
    test_file = os.path.join(sub_dir, "test.txt")
    with open(test_file, "w", encoding="utf-8") as f:
        f.write("hello world in workspace")

    # 1. LsTool
    ls_tool = LsTool()
    res = await ls_tool.execute(json.dumps({"path": "sub"}), ctx={"workspace": ws})
    assert "error" not in res
    assert "test.txt" in res.get("output", "")

    # Test LsTool outside workspace escape attempt
    res = await ls_tool.execute(json.dumps({"path": "../outside"}), ctx={"workspace": ws})
    assert "error" in res
    assert "outside the workspace" in res["error"]

    # 2. GlobTool
    glob_tool = GlobTool()
    res = await glob_tool.execute(json.dumps({"path": "sub", "pattern": "*.txt"}), ctx={"workspace": ws})
    assert "error" not in res
    assert "test.txt" in res.get("output", "")

    res = await glob_tool.execute(json.dumps({"path": "../outside", "pattern": "*"}), ctx={"workspace": ws})
    assert "error" in res
    assert "outside the workspace" in res["error"]

    # 3. GrepTool
    grep_tool = GrepTool()
    res = await grep_tool.execute(json.dumps({"path": "sub", "pattern": "hello"}), ctx={"workspace": ws})
    assert "error" not in res
    assert "test.txt" in res.get("output", "")

    res = await grep_tool.execute(json.dumps({"path": "../outside", "pattern": "hello"}), ctx={"workspace": ws})
    assert "error" in res
    assert "outside the workspace" in res["error"]

@pytest.mark.asyncio
async def test_orchestrator_permission_retry_loop(tmp_path):
    # Mock stream_agent_loop to raise PermissionRequired, then succeed on retry.
    from council_of_agents.scripts.council_orchestrator import CouncilOrchestrator
    from council_of_agents.scripts.permissions import PermissionRequired
    
    ws = os.path.normcase(os.path.realpath(str(tmp_path / "workspace")))
    os.makedirs(ws, exist_ok=True)

    block = ToolBlock("write_file", json.dumps({"path": os.path.join(ws, "test.txt"), "content": "data"}))
    perm_id = "test-perm-uuid-123"

    mock_agent_loop_chunks = [
        "data: {\"type\": \"thought_delta\", \"delta\": \"thinking...\"}\n\n",
    ]

    emit_calls = []
    async def mock_emit_cb(event, **kwargs):
        emit_calls.append((event, kwargs))

    # First call throws PermissionRequired, second call (after approved and retry) finishes.
    loop_count = 0
    async def mock_stream_agent_loop(*args, **kwargs):
        nonlocal loop_count
        loop_count += 1
        if loop_count == 1:
            raise PermissionRequired(
                action="write_file",
                target="outside_path",
                tool_block=block,
                permission_id=perm_id,
                round_response="{}",
                native_tool_calls=[],
                round_num=1,
                round_reasoning="need to write file"
            )
        else:
            for chunk in mock_agent_loop_chunks:
                yield chunk

    # We need to mock stream_agent_loop inside council_orchestrator
    with patch("council_of_agents.scripts.council_orchestrator.stream_agent_loop", side_effect=mock_stream_agent_loop):
        # We also mock stream_llm_with_fallback to return a dummy generator
        async def dummy_llm_stream(*args, **kwargs):
            yield "data: {\"delta\": \"mocked assistant response\"}\n\n"
        
        with patch("council_of_agents.scripts.council_orchestrator.stream_llm_with_fallback", side_effect=dummy_llm_stream):
            # Patch InMemorySessionStore.load to return a state with our workspace
            from council_of_agents.scripts.session_store import SessionState
            dummy_state = SessionState(session_id="session_123", owner="admin_user", user_prompt="hello")
            dummy_state.workspace = ws
            
            with patch("council_of_agents.scripts.session_store.InMemorySessionStore.load", return_value=dummy_state):
                router_mock = MagicMock()
                
                # Mock config setup
                dummy_role_config = MagicMock()
                dummy_role_config.endpoint_url = "http://mocked"
                dummy_role_config.model = "mocked-model"
                dummy_role_config.temperature = 0.5
                dummy_role_config.max_tokens = 100
                router_mock.role_config.return_value = dummy_role_config
                
                # Instantiate orchestrator
                orchestrator = CouncilOrchestrator(router_mock)
                orchestrator._resolve_headers = MagicMock(return_value={})
                
                messages = []
                # Call orchestrator._call_agent within a task
                call_task = asyncio.create_task(
                    orchestrator._call_agent(
                        role="implementer",
                        session_id="session_123",
                        overrides={},
                        messages=messages,
                        emit_cb=mock_emit_cb,
                        owner="admin_user",
                    )
                )

                # Wait a bit for the first loop to raise PermissionRequired and wait on event
                await asyncio.sleep(0.05)

                # Verify emit_cb was called with blocked permission event
                assert any(item[0] == "permission_request" for item in emit_calls)
                perm_event_info = [item for item in emit_calls if item[0] == "permission_request"][0][1]
                assert perm_event_info["status"] == "BLOCKED"
                assert perm_event_info["extra"]["permission_id"] == perm_id

                # Approve the permission in registry
                GLOBAL_REGISTRY.results[perm_id] = {"approved": True}
                GLOBAL_REGISTRY.pending_events[perm_id].set()

                # Wait for task to finish
                await call_task
                
                # Verify the second loop succeeded (loop_count should be 2)
                assert loop_count == 2
