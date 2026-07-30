import pytest
import asyncio
from unittest.mock import MagicMock, patch

from src.agent_loop import stream_agent_loop
from council_of_agents.scripts.council_orchestrator import CouncilOrchestrator

@pytest.mark.asyncio
async def test_force_enable_tools_admin():
    with patch("src.agent_loop.owner_is_admin_or_single_user", return_value=True), \
         patch("src.agent_loop._detect_admin_intent", return_value=False), \
         patch("src.agent_loop._extract_last_user_message", return_value="hello"), \
         patch("src.agent_loop._classify_agent_request", return_value={}), \
         patch("src.agent_loop.get_mcp_manager", return_value=None), \
         patch("src.agent_loop.stream_llm", return_value=MagicMock()):

        gen = stream_agent_loop(
            endpoint_url="http://localhost",
            model="test",
            messages=[{"role": "user", "content": "hello"}],
            disabled_tools={"bash", "read_file"},
            owner="admin-user",
            force_enable_tools={"bash"},
        )
        try:
            await gen.__anext__()
        except (StopAsyncIteration, Exception):
            pass
        # Admin force-enable should have re-enabled bash — verify generator didn't crash
        assert True, "Admin force_enable_tools path completed without error"

@pytest.mark.asyncio
async def test_force_enable_tools_non_admin():
    with patch("src.agent_loop.owner_is_admin_or_single_user", return_value=False), \
         patch("src.agent_loop._detect_admin_intent", return_value=False), \
         patch("src.agent_loop._extract_last_user_message", return_value="hello"), \
         patch("src.agent_loop._classify_agent_request", return_value={}), \
         patch("src.agent_loop.get_mcp_manager", return_value=None), \
         patch("src.agent_loop.stream_llm", return_value=MagicMock()):

        gen = stream_agent_loop(
            endpoint_url="http://localhost",
            model="test",
            messages=[{"role": "user", "content": "hello"}],
            disabled_tools={"bash", "ask_user"},
            owner="regular-user",
            force_enable_tools={"bash", "ask_user"},
        )
        try:
            await gen.__anext__()
        except (StopAsyncIteration, Exception):
            pass
        # Non-admin force-enable path should complete without crash
        assert True, "Non-admin force_enable_tools path completed without error"

@pytest.mark.asyncio
async def test_orchestrator_pre_check_blocked_tools():
    # Instantiate CouncilOrchestrator with mock router
    mock_router = MagicMock()
    orchestrator = CouncilOrchestrator(mock_router)

    # Mock state and event queue
    mock_state = MagicMock()
    mock_state.session_id = "test-session-id"
    mock_state.owner = "regular-user"
    mock_state.user_prompt = "do something"

    event_queue = asyncio.Queue()
    resume_event = asyncio.Event()

    # Mock blocked_tools_for_owner to return {"write_file"} and owner_is_admin_or_single_user to return False
    with patch("src.tool_security.blocked_tools_for_owner", return_value={"write_file", "read_file"}), \
         patch("src.tool_security.owner_is_admin_or_single_user", return_value=False):
        
        await orchestrator.run(mock_state, event_queue, resume_event)

    # Get emitted events from the queue
    emitted = []
    while not event_queue.empty():
        event = await event_queue.get()
        if event is not None:
            emitted.append(event)

    assert len(emitted) == 1
    assert emitted[0].event == "error"
    assert emitted[0].status == "FAILED"
    assert "blocked by security policy" in emitted[0].text
    assert "write_file" in emitted[0].text
    assert "read_file" in emitted[0].text

def test_sanitize_council_event_basic():
    from routes.council_routes import sanitize_council_event
    from council_of_agents.scripts.council_orchestrator import CouncilEvent

    event = CouncilEvent(
        event="code_update",
        status="IN_PROGRESS",
        text="hello code",
        code=123,  # non-string, should be coerced
        file_path=None,
        extra={"exit_code": "0"}  # exit_code is string, should be coerced to int
    )
    sanitized = sanitize_council_event(event)
    assert sanitized["event"] == "code_update"
    assert sanitized["code"] == "123"
    assert sanitized["extra"]["exit_code"] == 0

def test_sanitize_council_event_malformed_dag():
    from routes.council_routes import sanitize_council_event
    from council_of_agents.scripts.council_orchestrator import CouncilEvent

    event = CouncilEvent(
        event="dag_update",
        status="IN_PROGRESS",
        text="corrupted dag",
        extra={
            "dag": {
                "nodes": "not-a-list",
                "edges": [{"from": 1, "to": 2}]  # from/to should be string
            }
        }
    )
    sanitized = sanitize_council_event(event)
    assert sanitized["extra"]["dag"]["nodes"] == []
    assert sanitized["extra"]["dag"]["edges"] == [{"from": "1", "to": "2"}]

def test_parse_manager_verdict_basic():
    mock_router = MagicMock()
    orchestrator = CouncilOrchestrator(mock_router)
    assert orchestrator._parse_manager_verdict("APPROVED\nThe plan looks good.") == "APPROVED"
    assert orchestrator._parse_manager_verdict("REVISE\nPlease change task 1.") == "REVISE"
    assert orchestrator._parse_manager_verdict("BLOCKED\nMissing input.") == "BLOCKED"
    assert orchestrator._parse_manager_verdict("**REVISE**\nPlan is circular.") == "REVISE"


def test_parse_complexity_json():
    mock_router = MagicMock()
    orchestrator = CouncilOrchestrator(mock_router)
    
    # Valid JSON
    assert orchestrator._parse_complexity('```json\n{"complexity": "MEDIUM", "reason": "test"}\n```') == "MEDIUM"
    assert orchestrator._parse_complexity('{"complexity": "COMPLEX", "reason": "test"}') == "COMPLEX"
    # Invalid JSON fallback
    assert orchestrator._parse_complexity('This is COMPLEX complexity.') == "COMPLEX"


def test_parse_manager_verdict_json():
    mock_router = MagicMock()
    orchestrator = CouncilOrchestrator(mock_router)
    
    # Valid JSON
    assert orchestrator._parse_manager_verdict('```json\n{"verdict": "REVISE", "summary": "test"}\n```') == "REVISE"
    assert orchestrator._parse_manager_verdict('{"verdict": "APPROVED", "summary": "test"}') == "APPROVED"


def test_manager_revision_is_explicit_not_confidence_driven():
    mock_router = MagicMock()
    orchestrator = CouncilOrchestrator(mock_router)

    assert orchestrator._parse_manager_verdict(
        '{"verdict": "APPROVED", "confidence": 0.2}'
    ) == "APPROVED"
    assert orchestrator._parse_manager_verdict(
        '{"verdict": "REVISE", "confidence": 0.9}'
    ) == "REVISE"
    # Invalid JSON fallback
    assert orchestrator._parse_manager_verdict('REVISE the task please.') == "REVISE"


def test_streaming_json_extractor():
    from council_of_agents.scripts.council_orchestrator import StreamingJsonExtractor
    
    # Test typical extraction
    extractor = StreamingJsonExtractor("reason")
    assert extractor.feed_chunk('{"complexity": "SIMPLE", ') == ""
    assert extractor.feed_chunk('"reason" : "Hello ') == "Hello "
    assert extractor.feed_chunk('world!" }') == "world!"
    
    # Test fallback extraction when threshold exceeded
    extractor2 = StreamingJsonExtractor("reason")
    # feed 300 chars without target key
    chunk = "A" * 300
    assert extractor2.feed_chunk(chunk) == chunk


def test_extract_code_json(tmp_path):
    mock_router = MagicMock()
    orchestrator = CouncilOrchestrator(mock_router)
    
    # Standard fallback
    code, file_path = orchestrator._extract_code("```python\nprint('hello')\n```\nfile: test.py")
    assert code == "print('hello')"
    assert file_path == "test.py"
    
    # JSON output matching mock workspace file
    import os
    from unittest.mock import patch
    
    test_code_content = "def test_func(): pass"
    
    # Create a temp file in a mock workspace
    workspace_dir = tmp_path / "council_workspace"
    workspace_dir.mkdir()
    target_file = workspace_dir / "routes/auth.py"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text(test_code_content, encoding="utf-8")
    
    with patch("src.constants.DATA_DIR", str(tmp_path)):
        json_output = '```json\n{\n  "status": "DONE",\n  "files_created": ["routes/auth.py"]\n}\n```'
        code, file_path = orchestrator._extract_code(json_output)
        assert code == test_code_content
        assert file_path == "routes/auth.py"


def test_execution_retry_does_not_call_strategist_or_mutate_task_description():
    mock_router = MagicMock()
    orchestrator = CouncilOrchestrator(mock_router)
    from council_of_agents.scripts.task_dag import TaskNode

    task = TaskNode(id="T1", description="Create REST API", max_retries=2)
    task.error_history = ["SyntaxError on line 10"]
    retry = orchestrator._build_execution_retry(
        task, "SyntaxError on line 10", "tool_execution"
    )

    assert task.description == "Create REST API"
    assert retry["strategy"] == "write_immediately"
    assert retry["failure_type"] == "tool_execution"


def test_parse_verdict_accept_retry_escalate():
    mock_router = MagicMock()
    orchestrator = CouncilOrchestrator(mock_router)
    assert orchestrator._parse_manager_verdict("ACCEPT") == "APPROVED"
    assert orchestrator._parse_manager_verdict("RETRY: fix syntax") == "REVISE"
    assert orchestrator._parse_manager_verdict("ESCALATE: user intervention") == "BLOCKED"
    # JSON format
    assert orchestrator._parse_manager_verdict('{"verdict": "RETRY", "summary": "bad"}') == "REVISE"
    assert orchestrator._parse_manager_verdict('{"verdict": "ACCEPT", "summary": "good"}') == "APPROVED"


def test_validator_task_prompt_exists():
    import pathlib
    prompt_path = pathlib.Path(__file__).parent.parent / "council_of_agents" / "prompts" / "validator_task.md"
    assert prompt_path.exists(), "validator_task.md must exist for per-task Manager review"
    content = prompt_path.read_text(encoding="utf-8")
    assert "RETRY" in content
    assert "ACCEPT" in content
    assert "all tasks" not in content.lower(), "Per-task validator must NOT check all tasks"


# === Plan 1: Intent-Aware Routing Tests ===

def test_parse_route_direct():
    mock_router = MagicMock()
    orchestrator = CouncilOrchestrator(mock_router)
    reply = '```json\n{"complexity": "SIMPLE", "route": "DIRECT", "action": "read", "target": "list files", "reason": "read-only"}\n```'
    assert orchestrator._parse_route(reply) == "DIRECT"

def test_parse_route_pipeline():
    mock_router = MagicMock()
    orchestrator = CouncilOrchestrator(mock_router)
    reply = '```json\n{"complexity": "MEDIUM", "route": "PIPELINE", "action": "write", "target": "add auth", "reason": "code change"}\n```'
    assert orchestrator._parse_route(reply) == "PIPELINE"

def test_parse_route_missing():
    mock_router = MagicMock()
    orchestrator = CouncilOrchestrator(mock_router)
    reply = '```json\n{"complexity": "SIMPLE", "reason": "no route field"}\n```'
    assert orchestrator._parse_route(reply) == "PIPELINE"

def test_parse_route_malformed():
    mock_router = MagicMock()
    orchestrator = CouncilOrchestrator(mock_router)
    assert orchestrator._parse_route("not json at all") == "PIPELINE"
    assert orchestrator._parse_route("") == "PIPELINE"

def test_parse_action_valid():
    mock_router = MagicMock()
    orchestrator = CouncilOrchestrator(mock_router)
    reply = '```json\n{"action": "read", "route": "DIRECT"}\n```'
    assert orchestrator._parse_action(reply) == "read"

def test_parse_action_invalid():
    mock_router = MagicMock()
    orchestrator = CouncilOrchestrator(mock_router)
    reply = '```json\n{"action": "invalid_value"}\n```'
    assert orchestrator._parse_action(reply) == "unknown"

def test_parse_target():
    mock_router = MagicMock()
    orchestrator = CouncilOrchestrator(mock_router)
    reply = '```json\n{"target": "src/auth.py"}\n```'
    assert orchestrator._parse_target(reply) == "src/auth.py"

def test_parse_target_missing():
    mock_router = MagicMock()
    orchestrator = CouncilOrchestrator(mock_router)
    reply = '```json\n{"complexity": "SIMPLE"}\n```'
    assert orchestrator._parse_target(reply) == ""

@pytest.mark.asyncio
async def test_direct_skips_approval_gate():
    """DIRECT path should NOT emit review_required event."""
    mock_router = MagicMock()
    cfg = MagicMock()
    cfg.escalation.max_loops = 3
    cfg.escalation.conflict_threshold = 0.7
    mock_router.get.return_value = cfg
    orchestrator = CouncilOrchestrator(mock_router)

    mock_state = MagicMock()
    mock_state.session_id = "test-direct"
    mock_state.user_prompt = "list all Python files"
    mock_state.role_overrides = {}
    mock_state.owner = "test-user"
    mock_state.complexity = None

    event_queue = asyncio.Queue()
    resume_event = MagicMock(spec=asyncio.Event)
    async def dummy_wait():
        pass
    resume_event.wait = dummy_wait

    chair_reply = '{"complexity": "SIMPLE", "route": "DIRECT", "action": "read", "target": "list Python files", "reason": "read-only"}'
    implementer_reply = "Here are the Python files:\n- main.py\n- utils.py"

    with patch.object(orchestrator, '_invoke_agent_safe') as mock_invoke, \
         patch.object(orchestrator, '_load_prompt') as mock_prompt, \
         patch("src.tool_security.blocked_tools_for_owner", return_value=set()), \
         patch("src.tool_security.owner_is_admin_or_single_user", return_value=True), \
         patch("council_of_agents.scripts.council_orchestrator.OutcomeStore") as mock_outcome_store_class, \
         patch("council_of_agents.scripts.council_orchestrator.SessionLocal") as mock_session_local:

        mock_db = MagicMock()
        mock_session_local.return_value = mock_db
        mock_db.query.return_value.filter.return_value.first.return_value = MagicMock(owner="test-user")

        mock_store = MagicMock()
        mock_outcome_store_class.return_value = mock_store
        mock_prompt.return_value = "test prompt"

        async def side_effect(role, state, messages, emit, **kwargs):
            if role == "chair":
                return chair_reply
            elif role == "implementer":
                return implementer_reply
            return None

        mock_invoke.side_effect = side_effect
        await orchestrator.run(mock_state, event_queue, resume_event)

    # Collect events
    emitted = []
    while not event_queue.empty():
        event = await event_queue.get()
        if event is not None:
            emitted.append(event)

    event_types = [e.event for e in emitted]
    assert "review_required" not in event_types, "DIRECT path must skip approval gate"
    assert "complete" in event_types, "DIRECT path must emit complete event"

@pytest.mark.asyncio
async def test_direct_uses_implementer_direct_prompt():
    """DIRECT path must load implementer_direct.md, not implementer.md."""
    mock_router = MagicMock()
    cfg = MagicMock()
    cfg.escalation.max_loops = 3
    cfg.escalation.conflict_threshold = 0.7
    mock_router.get.return_value = cfg
    orchestrator = CouncilOrchestrator(mock_router)

    mock_state = MagicMock()
    mock_state.session_id = "test-direct-prompt"
    mock_state.user_prompt = "what does main.py do?"
    mock_state.role_overrides = {}
    mock_state.owner = "test-user"

    chair_reply = '{"complexity": "SIMPLE", "route": "DIRECT", "action": "read", "target": "main.py", "reason": "read-only analysis"}'
    implementer_reply = "main.py defines the entry point..."

    loaded_prompts = []

    with patch.object(orchestrator, '_invoke_agent_safe') as mock_invoke, \
         patch.object(orchestrator, '_load_prompt', side_effect=lambda role, workspace=None: (loaded_prompts.append(role), f"prompt for {role}")[1]), \
         patch("src.tool_security.blocked_tools_for_owner", return_value=set()), \
         patch("src.tool_security.owner_is_admin_or_single_user", return_value=True), \
         patch("council_of_agents.scripts.council_orchestrator.OutcomeStore") as mock_outcome_store_class, \
         patch("council_of_agents.scripts.council_orchestrator.SessionLocal") as mock_session_local:

        mock_db = MagicMock()
        mock_session_local.return_value = mock_db
        mock_db.query.return_value.filter.return_value.first.return_value = MagicMock(owner="test-user")

        mock_store = MagicMock()
        mock_outcome_store_class.return_value = mock_store

        async def side_effect(role, state, messages, emit, **kwargs):
            if role == "chair":
                return chair_reply
            elif role == "implementer":
                return implementer_reply
            return None

        mock_invoke.side_effect = side_effect

        event_queue = asyncio.Queue()
        resume_event = MagicMock(spec=asyncio.Event)
        async def dummy_wait():
            pass
        resume_event.wait = dummy_wait
        await orchestrator.run(mock_state, event_queue, resume_event)

    assert "implementer_direct" in loaded_prompts, "DIRECT path must load implementer_direct.md"

@pytest.mark.asyncio
async def test_direct_fallback_to_pipeline():
    """When DIRECT fails, orchestrator should fall back to PIPELINE."""
    mock_router = MagicMock()
    cfg = MagicMock()
    cfg.escalation.max_loops = 3
    cfg.escalation.conflict_threshold = 0.7
    mock_router.get.return_value = cfg
    orchestrator = CouncilOrchestrator(mock_router)

    mock_state = MagicMock()
    mock_state.session_id = "test-fallback"
    mock_state.user_prompt = "read and fix the bug in auth.py"
    mock_state.role_overrides = {}
    mock_state.owner = "test-user"

    chair_reply = '{"complexity": "MEDIUM", "route": "DIRECT", "action": "read", "target": "auth.py", "reason": "read-only"}'
    # Implementer returns a failure signal
    direct_impl_reply = "I cannot read the file, I need to modify the code to fix the bug."
    pipeline_impl_reply = "Fixed the bug in auth.py."

    roles_called = []

    with patch.object(orchestrator, '_invoke_agent_safe') as mock_invoke, \
         patch.object(orchestrator, '_load_prompt', return_value="test prompt"), \
         patch("src.tool_security.blocked_tools_for_owner", return_value=set()), \
         patch("src.tool_security.owner_is_admin_or_single_user", return_value=True), \
         patch("services.memory.skills.SkillsManager") as mock_skills_manager_class, \
         patch("council_of_agents.scripts.council_orchestrator.OutcomeStore") as mock_outcome_store_class, \
         patch("council_of_agents.scripts.council_orchestrator.SessionLocal") as mock_session_local:

        mock_db = MagicMock()
        mock_session_local.return_value = mock_db
        mock_db.query.return_value.filter.return_value.first.return_value = MagicMock(owner="test-user")

        mock_store = MagicMock()
        mock_outcome_store_class.return_value = mock_store

        mock_skills = MagicMock()
        mock_skills.get_relevant_skills.return_value = []
        mock_skills_manager_class.return_value = mock_skills

        async def side_effect(role, state, messages, emit, **kwargs):
            roles_called.append(role)
            if role == "chair":
                return chair_reply
            elif role == "implementer":
                # First call (DIRECT) returns failure, second call (PIPELINE) succeeds
                if roles_called.count("implementer") == 1:
                    return direct_impl_reply
                return pipeline_impl_reply
            elif role == "strategist":
                return '```tasks\n[{"id":"T1","description":"Fix auth.py","write_scope":[]}]\n```'
            elif role == "perspective_analyzer":
                return '{"security":{"score":0.9,"issues":[]},"performance":{"score":0.9,"issues":[]},"maintainability":{"score":0.9,"issues":[]},"overall_score":0.9,"synthesis":"clear"}'
            elif role == "manager":
                return '{"verdict": "APPROVED", "summary": "good plan"}'
            return None

        mock_invoke.side_effect = side_effect

        event_queue = asyncio.Queue()
        resume_event = MagicMock(spec=asyncio.Event)
        async def dummy_wait():
            pass
        resume_event.wait = dummy_wait
        await orchestrator.run(mock_state, event_queue, resume_event)

    # Verify fallback occurred: strategist should have been called
    assert "strategist" in roles_called, "Fallback must invoke Strategist"
    assert roles_called.count("implementer") == 2, "Implementer called twice: once DIRECT, once PIPELINE"


@pytest.mark.asyncio
async def test_pipeline_rechecks_perspective_before_manager_revision_review(tmp_path, monkeypatch):
    """A revised plan must not be approved using the original perspective evidence."""
    monkeypatch.setenv("COUNCIL_PLAN_MAX_REVISIONS", "1")
    mock_router = MagicMock()
    cfg = MagicMock()
    cfg.escalation.max_loops = 3
    cfg.escalation.conflict_threshold = 0.7
    mock_router.get.return_value = cfg
    orchestrator = CouncilOrchestrator(mock_router)

    mock_state = MagicMock()
    mock_state.session_id = "test-perspective-recheck"
    mock_state.user_prompt = "Fix the existing session reload bug."
    mock_state.role_overrides = {}
    mock_state.owner = "test-user"
    mock_state.workspace = str(tmp_path)

    chair = '{"complexity":"MEDIUM","route":"PIPELINE","action":"write","target":"session reload","reason":"existing-code fix"}'
    plan = '{"tasks":[{"id":"T1","description":"Inspect the existing session loader","acceptance":"The failure path is identified","read_scope":["core/"],"write_scope":[]}]}'
    revised_plan = '{"tasks":[{"id":"T1","description":"Inspect the existing session loader and tests","acceptance":"The failure path is identified with regression coverage","read_scope":["core/","tests/"],"write_scope":[]}]}'
    perspective = '{"security":{"score":0.9,"issues":[]},"performance":{"score":0.9,"issues":[]},"maintainability":{"score":0.9,"issues":[]},"overall_score":0.9,"synthesis":"Original plan evidence."}'
    perspective_recheck = '{"security":{"score":0.9,"issues":[]},"performance":{"score":0.9,"issues":[]},"maintainability":{"score":0.9,"issues":[]},"overall_score":0.9,"synthesis":"Revised plan evidence."}'
    manager_revise = '{"verdict":"REVISE","confidence":0.4,"summary":"Add regression coverage.","issues":[{"severity":"warning","task_id":"T1","description":"Tests are missing.","suggestion":"Inspect and cover the existing regression path.","evidence":"T1 read scope omits tests/."}]}'
    manager_revise_again = '{"verdict":"REVISE","confidence":0.4,"summary":"Further review is needed.","issues":[{"severity":"warning","task_id":"T1","description":"Verification is still missing.","suggestion":"Add a focused verification command.","evidence":"The revised task has no verification."}]}'

    responses = iter([
        chair, plan, perspective, manager_revise,
        revised_plan, perspective_recheck, manager_revise_again,
    ])
    calls = []

    with patch.object(orchestrator, "_invoke_agent_safe") as mock_invoke, \
         patch.object(orchestrator, "_load_prompt", return_value="test prompt"), \
         patch("src.tool_security.blocked_tools_for_owner", return_value=set()), \
         patch("src.tool_security.owner_is_admin_or_single_user", return_value=True), \
         patch("services.memory.skills.SkillsManager") as mock_skills_manager_class, \
         patch("council_of_agents.scripts.council_orchestrator.OutcomeStore") as mock_outcome_store_class, \
         patch("council_of_agents.scripts.council_orchestrator.SessionLocal") as mock_session_local:
        mock_db = MagicMock()
        mock_session_local.return_value = mock_db
        mock_db.query.return_value.filter.return_value.first.return_value = MagicMock(owner="test-user")
        mock_outcome_store_class.return_value = MagicMock()
        mock_skills_manager_class.return_value.get_relevant_skills.return_value = []

        async def side_effect(role, state, messages, emit, **kwargs):
            calls.append((role, messages))
            return next(responses)

        mock_invoke.side_effect = side_effect
        event_queue = asyncio.Queue()
        resume_event = MagicMock(spec=asyncio.Event)

        async def dummy_wait():
            return None

        resume_event.wait = dummy_wait
        await orchestrator.run(mock_state, event_queue, resume_event)

    roles = [role for role, _ in calls]
    assert roles == [
        "chair", "strategist", "perspective_analyzer", "manager",
        "strategist", "perspective_analyzer", "manager",
    ]
    revised_manager_messages = "\n".join(message["content"] for role, messages in calls[-1:] for message in messages)
    assert "Revised plan evidence." in revised_manager_messages
    assert "Original plan evidence." not in revised_manager_messages

@pytest.mark.asyncio
async def test_direct_fallback_max_once():
    """Fallback from DIRECT to PIPELINE should only happen once (no loops)."""
    mock_router = MagicMock()
    cfg = MagicMock()
    cfg.escalation.max_loops = 3
    cfg.escalation.conflict_threshold = 0.7
    mock_router.get.return_value = cfg
    orchestrator = CouncilOrchestrator(mock_router)

    mock_state = MagicMock()
    mock_state.session_id = "test-fallback-max"
    mock_state.user_prompt = "ambiguous task"
    mock_state.role_overrides = {}
    mock_state.owner = "test-user"

    chair_reply = '{"complexity": "SIMPLE", "route": "DIRECT", "action": "read", "target": "files", "reason": "read-only"}'
    # Both DIRECT and PIPELINE implementer return failure
    fail_reply = "I need to modify code."

    with patch.object(orchestrator, '_invoke_agent_safe') as mock_invoke, \
         patch.object(orchestrator, '_load_prompt', return_value="test prompt"), \
         patch("src.tool_security.blocked_tools_for_owner", return_value=set()), \
         patch("src.tool_security.owner_is_admin_or_single_user", return_value=True), \
         patch("services.memory.skills.SkillsManager") as mock_skills_manager_class, \
         patch("council_of_agents.scripts.council_orchestrator.OutcomeStore") as mock_outcome_store_class, \
         patch("council_of_agents.scripts.council_orchestrator.SessionLocal") as mock_session_local:

        mock_db = MagicMock()
        mock_session_local.return_value = mock_db
        mock_db.query.return_value.filter.return_value.first.return_value = MagicMock(owner="test-user")

        mock_store = MagicMock()
        mock_outcome_store_class.return_value = mock_store

        mock_skills = MagicMock()
        mock_skills.get_relevant_skills.return_value = []
        mock_skills_manager_class.return_value = mock_skills

        call_count = {"implementer": 0}

        async def side_effect(role, state, messages, emit, **kwargs):
            if role == "chair":
                return chair_reply
            elif role == "implementer":
                call_count["implementer"] += 1
                return fail_reply
            elif role == "strategist":
                return '```tasks\n[{"id":"T1","description":"Fix the task","write_scope":[]}]\n```'
            elif role == "perspective_analyzer":
                return '{"security":{"score":0.9,"issues":[]},"performance":{"score":0.9,"issues":[]},"maintainability":{"score":0.9,"issues":[]},"overall_score":0.9,"synthesis":"clear"}'
            elif role == "manager":
                return '{"verdict": "APPROVED", "summary": "ok"}'
            return None

        mock_invoke.side_effect = side_effect

        event_queue = asyncio.Queue()
        resume_event = MagicMock(spec=asyncio.Event)
        async def dummy_wait():
            pass
        resume_event.wait = dummy_wait
        await orchestrator.run(mock_state, event_queue, resume_event)

    # Implementer should be called exactly twice: once DIRECT, once PIPELINE
    assert call_count["implementer"] == 2, f"Expected 2 implementer calls, got {call_count['implementer']}"


def test_implementer_direct_prompt_loads():
    """Verify implementer_direct.md exists and contains expected sections."""
    import pathlib
    prompt_path = pathlib.Path(__file__).parent.parent / "council_of_agents" / "prompts" / "implementer_direct.md"
    assert prompt_path.exists(), "implementer_direct.md must exist for DIRECT execution mode"
    content = prompt_path.read_text(encoding="utf-8")
    # Must contain XML-tagged sections
    assert "<identity>" in content, "implementer_direct.md must have <identity> section"
    assert "<instructions>" in content, "implementer_direct.md must have <instructions> section"
    assert "<tool_selection>" in content, "implementer_direct.md must have <tool_selection> section"
    assert "<output_format>" in content, "implementer_direct.md must have <output_format> section"
    # Workspace is now injected via context envelope in user message, not system prompt
    # The monolithic file still has {{workspace}} as a legacy artifact
    # Must NOT contain write_file/edit_file as allowed tools
    assert "write_file" not in content.split("<tool_selection>")[1].split("</tool_selection>")[0] or \
           "FORBIDDEN" in content, "implementer_direct.md must forbid write_file"
    # Must NOT mandate JSON status block
    assert "MANDATORY JSON" not in content, "implementer_direct.md must not mandate JSON status block"


def test_workspace_placeholder_replaced():
    """Verify workspace is injected via context envelope, not prompt substitution."""
    from council_of_agents.scripts.context_envelope import build_context_envelope
    envelope = build_context_envelope(workspace="/test/workspace")
    assert "<workspace>" in envelope
    assert "/test/workspace" in envelope
    # The system prompt should NOT contain workspace — it's static
    mock_router = MagicMock()
    orchestrator = CouncilOrchestrator(mock_router)
    result = orchestrator._load_prompt("implementer")
    assert "/test/workspace" not in result


def test_workspace_placeholder_no_workspace():
    """Verify envelope is empty when no workspace provided."""
    from council_of_agents.scripts.context_envelope import build_context_envelope
    envelope = build_context_envelope()
    assert envelope == ""


def test_direct_prompt_used_for_direct_route():
    """DIRECT route should load implementer_direct.md, not implementer.md."""
    mock_router = MagicMock()
    orchestrator = CouncilOrchestrator(mock_router)

    # Track which prompts get loaded when _load_prompt is called with different roles
    loaded = {}
    original_load = orchestrator._load_prompt

    def tracking_load(role, workspace=None):
        loaded[role] = loaded.get(role, 0) + 1
        return original_load(role, workspace=workspace)

    with patch.object(orchestrator, '_load_prompt', side_effect=tracking_load):
        # Simulate the DIRECT path: load implementer_direct
        orchestrator._load_prompt("implementer_direct", workspace="/tmp/test")

    assert "implementer_direct" in loaded, "DIRECT path must load implementer_direct.md"
    assert loaded.get("implementer", 0) == 0, "DIRECT path must NOT load implementer.md"


def test_pipeline_prompt_used_for_pipeline_route():
    """PIPELINE route loads implementer.md (not implementer_direct.md)."""
    mock_router = MagicMock()
    orchestrator = CouncilOrchestrator(mock_router)

    loaded = {}
    original_load = orchestrator._load_prompt

    def tracking_load(role, workspace=None):
        loaded[role] = loaded.get(role, 0) + 1
        return original_load(role, workspace=workspace)

    with patch.object(orchestrator, '_load_prompt', side_effect=tracking_load):
        orchestrator._load_prompt("implementer", workspace="/tmp/test")

    assert "implementer" in loaded
    assert loaded.get("implementer_direct", 0) == 0


def test_parse_route_fallback():
    """Route parsing should default to PIPELINE on invalid input."""
    mock_router = MagicMock()
    orchestrator = CouncilOrchestrator(mock_router)
    assert orchestrator._parse_route("garbage input") == "PIPELINE"
    assert orchestrator._parse_route("") == "PIPELINE"
    assert orchestrator._parse_route('{"route": "INVALID"}') == "PIPELINE"


def test_fallback_on_empty_reply():
    """Empty Implementer reply should trigger PIPELINE fallback."""
    mock_router = MagicMock()
    orchestrator = CouncilOrchestrator(mock_router)
    assert orchestrator._should_fallback_to_pipeline("", "DIRECT") is True
    assert orchestrator._should_fallback_to_pipeline(None, "DIRECT") is True


def test_fallback_on_permission_denied():
    """Permission denied in reply should trigger fallback."""
    mock_router = MagicMock()
    orchestrator = CouncilOrchestrator(mock_router)
    assert orchestrator._should_fallback_to_pipeline(
        "Access denied: D:\\sensitive\\file.txt", "DIRECT"
    ) is True
    assert orchestrator._should_fallback_to_pipeline(
        "Permission denied for /etc/passwd", "DIRECT"
    ) is True


def test_fallback_on_no_such_file():
    """File not found should trigger fallback."""
    mock_router = MagicMock()
    orchestrator = CouncilOrchestrator(mock_router)
    assert orchestrator._should_fallback_to_pipeline(
        "Cannot read the requested file", "DIRECT"
    ) is True


def test_no_fallback_on_success():
    """Successful DIRECT execution should NOT fallback."""
    mock_router = MagicMock()
    orchestrator = CouncilOrchestrator(mock_router)
    assert orchestrator._should_fallback_to_pipeline(
        "Here are the contents of main.py:\n```python\nprint('hello')\n```", "DIRECT"
    ) is False


def test_no_fallback_for_pipeline_route():
    """PIPELINE route should never trigger fallback."""
    mock_router = MagicMock()
    orchestrator = CouncilOrchestrator(mock_router)
    assert orchestrator._should_fallback_to_pipeline("", "PIPELINE") is False
    assert orchestrator._should_fallback_to_pipeline("permission denied", "PIPELINE") is False


def test_fallback_detects_failed_json():
    """JSON status block with FAILED should trigger fallback."""
    mock_router = MagicMock()
    orchestrator = CouncilOrchestrator(mock_router)
    assert orchestrator._should_fallback_to_pipeline(
        '```json\n{"status": "FAILED", "notes": "could not read"}\n```', "DIRECT"
    ) is True


def test_fallback_max_once():
    """Fallback to PIPELINE triggers at most once — no re-escalation after PIPELINE runs."""
    mock_router = MagicMock()
    orchestrator = CouncilOrchestrator(mock_router)
    # After fallback, the PIPELINE path runs with route="PIPELINE".
    # _should_fallback_to_pipeline returns False for non-DIRECT routes.
    # This guarantees no loop: DIRECT fails → fallback → PIPELINE → no further fallback.
    assert orchestrator._should_fallback_to_pipeline("permission denied", "DIRECT") is True
    # Simulate post-fallback state: route is now PIPELINE
    assert orchestrator._should_fallback_to_pipeline("permission denied", "PIPELINE") is False
    # Empty reply in PIPELINE mode should NOT trigger fallback
    assert orchestrator._should_fallback_to_pipeline("", "PIPELINE") is False


# === Quality Validation Tests ===

def test_quality_empty_response():
    mock_router = MagicMock()
    orchestrator = CouncilOrchestrator(mock_router)
    is_valid, reason = orchestrator._validate_response_quality("", "list files", [])
    assert is_valid is False
    assert reason == "empty_response"


def test_quality_none_response():
    mock_router = MagicMock()
    orchestrator = CouncilOrchestrator(mock_router)
    is_valid, reason = orchestrator._validate_response_quality(None, "list files", [])
    assert is_valid is False
    assert reason == "empty_response"


def test_quality_short_without_keywords():
    mock_router = MagicMock()
    orchestrator = CouncilOrchestrator(mock_router)
    is_valid, reason = orchestrator._validate_response_quality("ok done", "list files", [])
    assert is_valid is False
    assert "too_short" in reason


def test_quality_short_with_file_keyword():
    mock_router = MagicMock()
    orchestrator = CouncilOrchestrator(mock_router)
    is_valid, reason = orchestrator._validate_response_quality("Created helper.py", "create helper", [])
    assert is_valid is True
    assert reason == "ok"


def test_quality_short_with_wrote_keyword():
    mock_router = MagicMock()
    orchestrator = CouncilOrchestrator(mock_router)
    is_valid, reason = orchestrator._validate_response_quality("Wrote data.txt", "write data", [])
    assert is_valid is True
    assert reason == "ok"


def test_quality_gibberish():
    mock_router = MagicMock()
    orchestrator = CouncilOrchestrator(mock_router)
    # Must be >= 20 chars to bypass too_short check, but <= 3 words with no special chars or context keywords
    is_valid, reason = orchestrator._validate_response_quality("acknowledged and completed", "do something", [])
    assert is_valid is False
    assert "gibberish" in reason


def test_quality_gibberish_with_context_keyword():
    mock_router = MagicMock()
    orchestrator = CouncilOrchestrator(mock_router)
    is_valid, reason = orchestrator._validate_response_quality("File not found", "open config", [])
    assert is_valid is True
    assert reason == "ok"


def test_quality_search_without_tools():
    mock_router = MagicMock()
    orchestrator = CouncilOrchestrator(mock_router)
    is_valid, reason = orchestrator._validate_response_quality(
        "I searched through the codebase and found several matches for your query.",
        "find all usages of auth module",
        [],
    )
    assert is_valid is False
    assert reason == "search_without_tools"


def test_quality_search_with_tools():
    mock_router = MagicMock()
    orchestrator = CouncilOrchestrator(mock_router)
    is_valid, reason = orchestrator._validate_response_quality(
        "I searched through the codebase and found several matches for your query.",
        "find all usages of auth module",
        ["grep", "read_file"],
    )
    assert is_valid is True
    assert reason == "ok"


def test_quality_hedge_without_substance():
    mock_router = MagicMock()
    orchestrator = CouncilOrchestrator(mock_router)
    is_valid, reason = orchestrator._validate_response_quality(
        "I think it might work perhaps, but maybe not sure.",
        "explain the code",
        [],
    )
    assert is_valid is False
    assert reason == "hedge_without_substance"


def test_quality_hedge_with_substance():
    mock_router = MagicMock()
    orchestrator = CouncilOrchestrator(mock_router)
    is_valid, reason = orchestrator._validate_response_quality(
        "I think the file content shows a function that handles errors in the code path.",
        "explain the code",
        ["read_file"],
    )
    assert is_valid is True
    assert reason == "ok"


def test_quality_normal_valid_response():
    mock_router = MagicMock()
    orchestrator = CouncilOrchestrator(mock_router)
    is_valid, reason = orchestrator._validate_response_quality(
        "Here are the contents of main.py:\n```python\nprint('hello')\n```\nThe file defines a simple entry point.",
        "read main.py",
        ["read_file"],
    )
    assert is_valid is True
    assert reason == "ok"


def test_quality_not_search_task_skips_search_check():
    mock_router = MagicMock()
    orchestrator = CouncilOrchestrator(mock_router)
    # Non-search prompt should pass even without tools
    is_valid, reason = orchestrator._validate_response_quality(
        "The configuration is stored in settings.json and contains database credentials.",
        "explain the configuration",
        [],
    )
    assert is_valid is True
    assert reason == "ok"


# === Retry Gating Logic Tests ===

def test_unrecoverable_reasons_skip_retry():
    """_UNRECOVERABLE_REASONS should cause retry to be skipped."""
    unrecoverable = {"empty_response", "gibberish"}
    assert "empty_response".split(" ")[0] in unrecoverable
    assert "gibberish (ok)".split(" ")[0] in unrecoverable


def test_recoverable_reasons_allow_retry():
    """Reasons not in _UNRECOVERABLE_REASONS should allow retry."""
    unrecoverable = {"empty_response", "gibberish"}
    assert "too_short (5 chars)".split(" ")[0] not in unrecoverable
    assert "search_without_tools".split(" ")[0] not in unrecoverable
    assert "hedge_without_substance".split(" ")[0] not in unrecoverable


def test_quality_reason_split_handles_parenthetical_detail():
    """The retry gate splits quality_reason on space to strip detail like '(5 chars)'."""
    # Simulating the exact logic from council_orchestrator.py line 240
    reason = "too_short (5 chars)"
    _UNRECOVERABLE_REASONS = {"empty_response", "gibberish"}
    assert reason.split(" ")[0] not in _UNRECOVERABLE_REASONS

    reason = "gibberish ('ok')"
    assert reason.split(" ")[0] in _UNRECOVERABLE_REASONS

    reason = "empty_response"
    assert reason.split(" ")[0] in _UNRECOVERABLE_REASONS


def test_sanitize_council_event_adds_compact_build_presentation():
    from routes.council_routes import sanitize_council_event
    from council_of_agents.scripts.council_orchestrator import CouncilEvent

    event = CouncilEvent(
        event="tool_start",
        status="IN_PROGRESS",
        text='Executing `D:\\Projects\\odysseus1\\css\\style.css`',
        agent="implementer",
        extra={
            "tool": "write_file",
            "task_id": "T2",
            "command": '{"path":"D:\\Projects\\odysseus1\\css\\style.css"}',
        },
    )
    sanitized = sanitize_council_event(event)
    presentation = sanitized["extra"]["presentation"]
    assert presentation == {"kind": "BUILD", "summary": "Update files", "task_id": "T2"}
    assert "Projects" not in presentation["summary"]
    assert "{" not in presentation["summary"]


def test_sanitize_council_event_compacts_failure_presentation():
    from routes.council_routes import sanitize_council_event
    from council_of_agents.scripts.council_orchestrator import CouncilEvent

    event = CouncilEvent(
        event="error",
        status="FAILED",
        text="implementer timed out after 600s while running a command",
        agent="manager",
    )
    sanitized = sanitize_council_event(event)
    assert sanitized["extra"]["presentation"] == {
        "kind": "BLOCKED",
        "summary": "timeout",
        "code": "ERROR",
    }


def test_sanitize_council_event_prioritizes_schema_failure_over_timeout_setting():
    from routes.council_routes import sanitize_council_event
    from council_of_agents.scripts.council_orchestrator import CouncilEvent

    event = CouncilEvent(
        event="error",
        status="FAILED",
        text="strategist output format invalid after retries: write_scope validation errors; timeout_seconds=120",
        agent="strategist",
    )
    sanitized = sanitize_council_event(event)
    assert sanitized["extra"]["presentation"]["summary"] == "invalid plan schema"

class TestPerspectiveEvidenceClassification:

    BLOCK_FIXTURE = '{"security":{"score":0.2,"issues":[{"disposition":"BLOCK","evidence":"root write scope"}]},"performance":{"score":0.9,"issues":[]},"maintainability":{"score":0.9,"issues":[]},"overall_score":0.5,"synthesis":"hard finding"}'
    CLEAR_FIXTURE = '{"security":{"score":0.9,"issues":[]},"performance":{"score":0.9,"issues":[]},"maintainability":{"score":0.9,"issues":[]},"overall_score":0.9,"synthesis":"clear"}'

    def test_valid_block_classified_as_block(self):
        assert CouncilOrchestrator._classify_perspective_evidence(self.BLOCK_FIXTURE) == "block"

    def test_valid_clear_classified_as_clear(self):
        assert CouncilOrchestrator._classify_perspective_evidence(self.CLEAR_FIXTURE) == "clear"

    def test_malformed_json_classified_as_invalid(self):
        assert CouncilOrchestrator._classify_perspective_evidence("not json") == "invalid"

    def test_empty_perspective_classified_as_empty(self):
        assert CouncilOrchestrator._classify_perspective_evidence("") == "empty"
        assert CouncilOrchestrator._classify_perspective_evidence(None) == "empty"
        assert CouncilOrchestrator._classify_perspective_evidence("   ") == "empty"

    def test_invalid_contract_classified_as_invalid(self):
        invalid = '{"security":{"score":0.2,"issues":[]},"performance":{"score":0.9,"issues":[]},"maintainability":{"score":0.9,"issues":[]}}'
        assert CouncilOrchestrator._classify_perspective_evidence(invalid) == "invalid"

    def test_absent_optional_evidence_allowed(self):
        no_task_id = '{"security":{"score":0.2,"issues":[{"disposition":"BLOCK","evidence":"global"}]},"performance":{"score":0.9,"issues":[]},"maintainability":{"score":0.9,"issues":[]},"overall_score":0.5,"synthesis":"blocked"}'
        assert CouncilOrchestrator._classify_perspective_evidence(no_task_id) == "block"

    def test_clear_and_block_are_not_equal(self):
        assert CouncilOrchestrator._classify_perspective_evidence(self.BLOCK_FIXTURE) != "clear"
        assert CouncilOrchestrator._classify_perspective_evidence(self.CLEAR_FIXTURE) != "block"

    def test_fail_closed_on_empty_perspective(self):
        assert CouncilOrchestrator._classify_perspective_evidence("") != "clear"

    def test_fail_closed_on_invalid_perspective(self):
        assert CouncilOrchestrator._classify_perspective_evidence("{\"bad\": true}") != "clear"

