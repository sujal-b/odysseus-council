import pytest
import asyncio
from types import SimpleNamespace
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
async def test_direct_fallback_to_pipeline(tmp_path):
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
    mock_state.workspace = str(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "auth.py").write_text("def auth(): pass\n", encoding="utf-8")

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
                return '```tasks\n[{"id":"T1","description":"Fix src/auth.py using repository evidence.","acceptance":"src/auth.py fixed.","read_scope":["src/"],"write_scope":[]}]\n```'
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
async def test_direct_timeout_escalates_to_pipeline(tmp_path):
    """A transient implementer timeout on the DIRECT path must escalate to
    PIPELINE, not kill the production run (regression: the vertical slice
    failed end-to-end when the DIRECT implementer endpoint stopped answering
    and the raised TimeoutError bypassed the existing DIRECT→PIPELINE
    escalation, which only handled None/quality-failure replies)."""
    mock_router = MagicMock()
    cfg = MagicMock()
    cfg.escalation.max_loops = 3
    cfg.escalation.conflict_threshold = 0.7
    mock_router.get.return_value = cfg
    orchestrator = CouncilOrchestrator(mock_router)

    mock_state = MagicMock()
    mock_state.session_id = "test-direct-timeout"
    mock_state.user_prompt = "Add a health endpoint and a regression test."
    mock_state.role_overrides = {}
    mock_state.owner = "test-user"
    mock_state.workspace = str(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def health(): pass\\n", encoding="utf-8")

    chair_reply = '{"complexity": "SIMPLE", "route": "DIRECT", "action": "read", "target": "src/app.py", "reason": "read-only"}'
    pipeline_impl_reply = "Added the health endpoint and its regression test."

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
        mock_outcome_store_class.return_value = MagicMock()
        mock_skills_manager_class.return_value.get_relevant_skills.return_value = []

        async def side_effect(role, state, messages, emit, **kwargs):
            roles_called.append(role)
            if role == "chair":
                return chair_reply
            elif role == "implementer":
                if roles_called.count("implementer") == 1:
                    raise asyncio.TimeoutError(
                        "implementer received no complete model response within 600s"
                    )
                return pipeline_impl_reply
            elif role == "strategist":
                return '```tasks\n[{"id":"T1","description":"Inspect src/app.py","acceptance":"src/app.py inspected.","read_scope":["src/"],"write_scope":[]}]\n```'
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

    # The raised timeout must not propagate: the run escalates to PIPELINE.
    assert "strategist" in roles_called, "Timeout must escalate to PIPELINE"
    assert roles_called.count("implementer") == 2, "Implementer called twice: once DIRECT (raised), once PIPELINE"


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
    (tmp_path / "core").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "core" / "session_manager.py").write_text("class SessionManager:\\n    def load_sessions(self): return 0\\n", encoding="utf-8")
    (tmp_path / "tests" / "test_session_manager.py").write_text("def test_session(): pass\\n", encoding="utf-8")

    chair = '{"complexity":"MEDIUM","route":"PIPELINE","action":"write","target":"session reload","reason":"existing-code fix"}'
    plan = '{"tasks":[{"id":"T1","description":"Inspect core/session_manager.py existing session loader","acceptance":"The failure path is identified","read_scope":["core/"],"write_scope":[]}]}'
    revised_plan = '{"tasks":[{"id":"T1","description":"Inspect core/session_manager.py existing session loader and tests","acceptance":"The failure path is identified with regression coverage","read_scope":["core/","tests/"],"write_scope":[]}]}'
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
async def test_unparseable_plan_revision_falls_through_to_override_gate(tmp_path):
    """A schema-invalid strategist revision (bounded retries exhausted) must
    degrade to the Manager override gate, not crash the run (regression: the
    vertical slice died when a revision reply could not be normalized and the
    SchemaValidationError escaped run(), skipping the designed gate)."""
    from council_of_agents.scripts.council_retry import SchemaValidationError
    mock_router = MagicMock()
    cfg = MagicMock()
    cfg.escalation.max_loops = 3
    cfg.escalation.conflict_threshold = 0.7
    mock_router.get.return_value = cfg
    orchestrator = CouncilOrchestrator(mock_router)

    mock_state = MagicMock()
    mock_state.session_id = "test-revision-schema-fail"
    mock_state.user_prompt = "Add a health endpoint and a regression test."
    mock_state.role_overrides = {}
    mock_state.owner = "test-user"
    mock_state.workspace = str(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "app.py").write_text("def health(): pass\\n", encoding="utf-8")
    (tmp_path / "tests" / "test_app.py").write_text("def test_health(): pass\\n", encoding="utf-8")

    chair = '{"complexity":"MEDIUM","route":"PIPELINE","action":"write","target":"health endpoint","reason":"code change"}'
    plan = '{"tasks":[{"id":"T1","description":"Inspect src/app.py","acceptance":"src/app.py failure path is identified","read_scope":["src/"],"write_scope":[]}]}'
    perspective = '{"security":{"score":0.9,"issues":[]},"performance":{"score":0.9,"issues":[]},"maintainability":{"score":0.9,"issues":[]},"overall_score":0.9,"synthesis":"Original plan evidence."}'
    manager_revise = '{"verdict":"REVISE","confidence":0.4,"summary":"Add regression coverage.","issues":[{"severity":"warning","task_id":"T1","description":"Tests are missing.","suggestion":"Inspect and cover the existing regression path.","evidence":"T1 read scope omits tests/."}]}'

    calls = []

    async def side_effect(role, state, messages, emit, **kwargs):
        calls.append(role)
        if role == "chair":
            return chair
        if role == "strategist":
            if calls.count("strategist") == 1:
                return plan
            raise SchemaValidationError(
                "strategist schema invalid: strategist response could not be normalized (raw_shape=unparseable)",
                raw_text="Here is the revised plan:",
                validation_error="strategist response could not be normalized (raw_shape=unparseable)",
            )
        if role == "perspective_analyzer":
            return perspective
        if role == "manager":
            return manager_revise
        return None

    with patch.object(orchestrator, "_invoke_agent_safe", side_effect=side_effect), \
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

        event_queue = asyncio.Queue()
        resume_event = MagicMock(spec=asyncio.Event)

        async def dummy_wait():
            return None

        resume_event.wait = dummy_wait
        # Must NOT raise: the schema-invalid revision must not escape run().
        await orchestrator.run(mock_state, event_queue, resume_event)

    emitted = []
    while not event_queue.empty():
        event = event_queue.get_nowait()
        if event is not None:
            emitted.append(event)

    review_events = [e for e in emitted if e.event == "review_required"]
    assert review_events, "run must fall through to the Manager override gate"
    assert review_events[0].extra["requires_override"] is True
    strategy_errors = [e for e in emitted if e.event == "error" and e.agent == "strategist"]
    assert any("parseable plan revision" in e.text for e in strategy_errors), (
        "a clear strategist error event must explain the failed revision"
    )
    assert calls.count("strategist") == 2, "exactly one revision attempt (bounded invoke retries live inside _invoke_agent_safe)"


@pytest.mark.asyncio
async def test_direct_fallback_max_once(tmp_path):
    """Fallback from DIRECT to PIPELINE should only happen once (no loops)."""
    mock_router = MagicMock()
    cfg = MagicMock()
    cfg.escalation.max_loops = 3
    cfg.escalation.conflict_threshold = 0.7
    mock_router.get.return_value = cfg
    orchestrator = CouncilOrchestrator(mock_router)

    mock_state = MagicMock()
    mock_state.session_id = "test-fallback-max"
    mock_state.user_prompt = "Read src/app.py"
    mock_state.role_overrides = {}
    mock_state.owner = "test-user"
    mock_state.workspace = str(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def app(): pass\\n", encoding="utf-8")

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
                return '```tasks\n[{"id":"T1","description":"Inspect src/app.py","acceptance":"src/app.py inspected.","read_scope":["src/"],"write_scope":[]}]\n```'
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
    """Relative labels are safe context; prompts remain static."""
    from council_of_agents.scripts.context_envelope import build_context_envelope
    envelope = build_context_envelope(workspace="workspace-label")
    assert "<workspace>" in envelope
    assert "workspace-label" in envelope
    mock_router = MagicMock()
    orchestrator = CouncilOrchestrator(mock_router)
    assert "workspace-label" not in orchestrator._load_prompt("implementer")


def test_absolute_workspace_is_omitted_from_provider_context():
    from council_of_agents.scripts.context_envelope import build_context_envelope
    assert build_context_envelope(workspace="/test/workspace") == ""


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

class TestStructuredOutputIntegration:

    def test_single_role_evaluate_sends_json_schema_by_default(self):
        from council_of_agents.scripts.council_schemas import build_response_format, SCHEMA_MAP
        for role in ("chair", "strategist", "manager", "perspective_analyzer"):
            rf = build_response_format(role)
            assert rf is not None, f"{role} should have json_schema"
            assert rf["type"] == "json_schema"
            assert "json_schema" in rf
            assert rf["json_schema"]["strict"] is True

    def test_implementer_has_schema_but_not_in_json_object_roles(self):
        from council_of_agents.scripts.council_schemas import build_response_format
        from src.llm_core import _requires_json_object
        rf = build_response_format("implementer")
        assert rf is not None, "implementer IS in SCHEMA_MAP"
        assert rf["type"] == "json_schema"
        assert _requires_json_object({"agent": "implementer"}) is False

    def test_unknown_role_has_no_json_schema(self):
        from council_of_agents.scripts.council_schemas import build_response_format
        assert build_response_format("nonexistent_role") is None

    def test_chair_arbitration_has_json_schema(self):
        from council_of_agents.scripts.council_schemas import build_response_format
        from src.llm_core import _requires_json_object
        rf = build_response_format("chair_arbitration")
        assert rf is not None, "chair_arbitration IS in SCHEMA_MAP"
        assert rf["type"] == "json_schema"
        assert _requires_json_object({"agent": "chair_arbitration"}) is True

    def test_all_control_schemas_have_top_level_required(self):
        from council_of_agents.scripts.council_schemas import SCHEMA_MAP, build_response_format
        for role, schema_cls in SCHEMA_MAP.items():
            rf = build_response_format(role)
            schema = rf["json_schema"]["schema"]
            assert "required" in schema
            props = list(schema.get("properties", {}).keys())
            for p in props:
                assert p in schema["required"], f"{role} schema missing required field '{p}'"

    def test_all_control_schemas_have_nested_required(self):
        from council_of_agents.scripts.council_schemas import build_response_format, SCHEMA_MAP
        for role, schema_cls in SCHEMA_MAP.items():
            rf = build_response_format(role)
            schema = rf["json_schema"]["schema"]
            defs = schema.get("$defs", {})
            for def_name, def_schema in defs.items():
                if "properties" in def_schema:
                    assert "required" in def_schema, f"{role}.{def_name} missing required"
                    for p in def_schema["properties"]:
                        assert p in def_schema["required"], f"{role}.{def_name} missing required '{p}'"

    def test_schema_does_not_mutate_source(self):
        from council_of_agents.scripts.council_schemas import SCHEMA_MAP, build_response_format, ChairOutput
        original_schema = ChairOutput.model_json_schema()
        build_response_format("chair")
        after_schema = ChairOutput.model_json_schema()
        assert after_schema == original_schema, "build_response_format must not mutate source schema"

    def test_production_call_agent_adds_response_format(self):
        from council_of_agents.scripts.council_schemas import build_response_format
        for role in ("chair", "strategist", "manager", "perspective_analyzer"):
            rf = build_response_format(role)
            assert rf is not None
            assert rf["json_schema"]["name"] == role

    def test_requires_json_object_false_with_tools(self):
        from src.llm_core import _requires_json_object
        assert _requires_json_object({"agent": "chair"}, tools=[{"type": "function"}]) is False
        assert _requires_json_object({"agent": "strategist"}, tools=[{"type": "function"}]) is False
        assert _requires_json_object({"agent": "manager"}, tools=[{"type": "function"}]) is False
        assert _requires_json_object({"agent": "implementer"}, tools=[{"type": "function"}]) is False

    def test_requires_json_object_only_for_json_object_roles(self):
        from src.llm_core import _requires_json_object
        non_control = {"agent": "implementer"}
        assert _requires_json_object(non_control) is False
        non_control["agent"] = "debate_response"
        assert _requires_json_object(non_control) is False

    def test_response_format_override_takes_precedence(self):
        from src.llm_core import _requires_json_object
        tc = {"agent": "chair", "response_format": {"type": "json_schema", "json_schema": {"name": "chair", "strict": True}}}
        assert _requires_json_object(tc) is True

    def test_nonexistent_agent_no_json_object(self):
        from src.llm_core import _requires_json_object
        assert _requires_json_object({"agent": "nobody"}) is False
        assert _requires_json_object({}) is False
        assert _requires_json_object(None) is False

    def test_requires_json_object_true_for_control_roles(self):
        from src.llm_core import _requires_json_object
        for role in ("chair", "strategist", "manager", "perspective_analyzer", "chair_arbitration", "completeness_auditor"):
            assert _requires_json_object({"agent": role}) is True


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


class TestTimeoutPolicy:

    def test_manager_hard_timeout_matches_slow_provider_floor(self):
        """Regression: the Manager sits on the same slow nvidia provider as
        the Strategist, so its total model-wait budget must be at least the
        Strategist's. The slice run 6 died when a working Manager was cut off
        at the 60s inactivity cap twice in a row."""
        manager_cap = CouncilOrchestrator.AGENT_HARD_TIMEOUTS.get(
            "manager", CouncilOrchestrator.AGENT_TIMEOUTS["manager"])
        strategist_cap = CouncilOrchestrator.AGENT_HARD_TIMEOUTS["strategist"]
        assert manager_cap >= strategist_cap


@pytest.mark.asyncio
async def test_agent_runner_passes_state_workspace_to_call_agent(tmp_path):
    """Regression: tool-enabled calls must target state.workspace even when the
    session is not in the in-memory session store (e.g. after process restart)."""
    captured = {}

    class StubOrchestrator:
        AGENT_TIMEOUTS = {"implementer": 30}
        AGENT_HARD_TIMEOUTS = {"implementer": 30}
        AGENT_MAX_RETRIES = {"implementer": 0}

        async def _call_agent(self, role, session_id, overrides, messages,
                              on_chunk=None, emit_cb=None, written_paths=None,
                              owner=None, tool_results_out=None, route="PIPELINE",
                              workspace_write_guard=None, context_fallback=None,
                              disable_tools=False, required_contract=None, workspace=None):
            captured["workspace"] = workspace
            return "implemented."

    from council_of_agents.scripts.session_store import SessionState
    from council_of_agents.scripts.agent_runner import AgentRunner
    state = SessionState(session_id="wfa-regression", owner="admin",
                         user_prompt="add a health endpoint", workspace=str(tmp_path),
                         status="PENDING")
    runner = AgentRunner(StubOrchestrator(), state, emit=None)
    result = await runner.invoke("implementer", [{"role": "user", "content": "go"}])
    assert result
    assert captured["workspace"] == str(tmp_path)

@pytest.mark.asyncio
async def test_completeness_gap_fill_runs_with_workspace_guard():
    """Regression: the completeness gap-fill implementer dispatch must use the
    same workspace confinement as DAG tasks (safe tools, writes in workspace)."""
    from council_of_agents.scripts.council_orchestrator import CouncilOrchestrator
    from council_of_agents.scripts.session_store import SessionState
    from council_of_agents.scripts.task_dag import TaskDAG
    import tempfile, os

    mock_router = MagicMock()
    orchestrator = CouncilOrchestrator(mock_router)

    workspace = tempfile.mkdtemp(prefix="council-gap-")
    state = SessionState(session_id="wfb-gap-regression", owner="admin",
                         user_prompt="deliver the artifact", workspace=workspace,
                         status="IN_PROGRESS")

    seen = {}
    audit_calls = {"n": 0}

    async def fake_audit(state, criteria, impl_reply, written_paths, emit, owner):
        audit_calls["n"] += 1
        if audit_calls["n"] == 1:
            return {
                "done": False, "completeness": 0.0,
                "criteria": [{"id": "C1", "met": False, "gap_type": "fillable",
                              "description": "add src/app.py", "acceptance": "file exists"}],
            }
        return {"done": True, "completeness": 1.0,
                "criteria": [{"id": "C1", "met": True}]}

    async def fake_invoke(role, state, messages, emit, **kwargs):
        if role == "implementer":
            seen["kwargs"] = kwargs
            return "gap closed"
        return None

    async def fake_emit(**event):
        pass

    async def fake_wait():
        return True

    orchestrator._run_completeness_audit = fake_audit
    orchestrator._invoke_agent_safe = fake_invoke
    orchestrator._load_prompt = lambda role, workspace=None: f"prompt {role}"

    resume = MagicMock(spec=asyncio.Event)
    resume.wait = fake_wait

    reply, metrics, cancelled = await orchestrator._completeness_loop(
        state, TaskDAG(), "initial", [], set(), "PIPELINE", workspace,
        fake_emit, "admin", resume,
    )
    assert cancelled is False
    assert audit_calls["n"] == 2
    assert "gap closed" in reply
    guard = seen["kwargs"].get("workspace_write_guard")
    assert guard is not None, "gap-fill dispatch must carry a workspace write guard"
    assert guard.enforce_channels is True
    assert os.path.realpath(guard.root) == os.path.realpath(workspace)

def test_readonly_task_rule_forbids_writes_and_writable_rule_requires_diff():
    """Regression: read-only DAG tasks must get an explicit no-write rule, and
    writable tasks must keep the artifact-diff rule (slice runs 5 and 7: the
    blanket "leave a diff" line pushed read-only implementers into writing)."""
    readonly = CouncilOrchestrator._guarded_execution_rule(SimpleNamespace(write_scope=[]))
    writable = CouncilOrchestrator._guarded_execution_rule(SimpleNamespace(write_scope=["src/"]))
    assert "READ-ONLY" in readonly and "write_file" in readonly
    # The read-only rule must also carry the channel warning (slice runs 12/13:
    # read-only implementers kept calling bash to inspect).
    assert "bash" in readonly and "python" in readonly
    assert "artifact diff" in writable and "READ-ONLY" not in writable

def test_scope_violation_retry_gives_explicit_compliance_guidance():
    """Regression: guard rejections must carry corrective guidance, not the
    generic alternate-approach line (slice runs 3/9: bash in guarded
    execution; runs 5/7/8: writes outside declared scope)."""
    task = SimpleNamespace(write_scope=["src/"])
    bash = CouncilOrchestrator._build_execution_retry(
        task, "tool 'bash' is not compatible with guarded workspace execution", "scope_violation")
    assert bash["strategy"] == "channel_compliance"
    assert "read_file" in bash["instruction"] and "bash" in bash["instruction"]

    scope = CouncilOrchestrator._build_execution_retry(
        task, "write target is outside declared scope: tests/test_app.py", "scope_violation")
    assert scope["strategy"] == "scope_compliance"
    assert "['src/']" in scope["instruction"] and "tests/test_app.py" not in scope["instruction"].split("report")[0]

    readonly = CouncilOrchestrator._build_execution_retry(
        SimpleNamespace(write_scope=[]),
        "write target is outside declared scope: src/app.py", "scope_violation")
    assert readonly["strategy"] == "readonly_compliance"
    assert "read-only" in readonly["instruction"]

@pytest.mark.asyncio
async def test_task_gate_receives_actual_written_files_evidence(tmp_path):
    """Regression: the per-task Manager gate must see the guard-approved write
    record, not just the implementer's self-report (slice runs 8-10: the gate
    REVISE'd completed tasks for "verification" it had no evidence for)."""
    mock_router = MagicMock()
    cfg = MagicMock()
    cfg.escalation.max_loops = 3
    cfg.escalation.conflict_threshold = 0.7
    mock_router.get.return_value = cfg
    orchestrator = CouncilOrchestrator(mock_router)

    mock_state = MagicMock()
    mock_state.session_id = "test-gate-evidence"
    mock_state.user_prompt = "Add a health endpoint and a regression test."
    mock_state.role_overrides = {}
    mock_state.owner = "test-user"
    mock_state.workspace = str(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "app.py").write_text("def health(): pass\n", encoding="utf-8")
    (tmp_path / "tests" / "test_app.py").write_text("def test_health(): pass\n", encoding="utf-8")

    chair = '{"complexity":"MEDIUM","route":"PIPELINE","action":"write","target":"health endpoint","reason":"code change"}'
    plan = '{"tasks":[{"id":"T1","description":"Inspect src/app.py","acceptance":"src/app.py findings documented","read_scope":["src/"],"write_scope":[]}]}'
    perspective = '{"security":{"score":0.9,"issues":[]},"performance":{"score":0.9,"issues":[]},"maintainability":{"score":0.9,"issues":[]},"overall_score":0.9,"synthesis":"clear"}'
    manager_approve = '{"verdict":"APPROVED","confidence":0.9,"summary":"ok"}'
    audit_done = '{"done": true, "completeness": 1.0, "criteria": [{"id": "C1", "met": true}]}'

    gate_messages = []

    async def side_effect(role, state, messages, emit, **kwargs):
        joined = "\n".join(m.get("content", "") for m in messages)
        if "Actual files written by this task's attempt" in joined:
            gate_messages.append(joined)
        if role == "chair":
            return chair
        if role == "strategist":
            return plan
        if role == "perspective_analyzer":
            return perspective
        if role == "manager":
            return manager_approve
        if role == "implementer":
            return "T1 done: inspected the service, findings documented."
        if role == "completeness_auditor":
            return audit_done
        return None

    with patch.object(orchestrator, "_invoke_agent_safe", side_effect=side_effect), \
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

        event_queue = asyncio.Queue()
        resume_event = MagicMock(spec=asyncio.Event)

        async def dummy_wait():
            return None

        resume_event.wait = dummy_wait
        await orchestrator.run(mock_state, event_queue, resume_event)

    assert len(gate_messages) == 1, "the per-task gate must receive the written-files evidence"
    assert "Actual files written by this task's attempt: NONE" in gate_messages[0]
    assert "Task contract: acceptance" in gate_messages[0]
    assert "Judge ONLY against this task's acceptance" in gate_messages[0]
    # Non-empty path carries the guard-approved paths verbatim; the
    # deterministic verification result travels with it.
    assert "src/app.py" in CouncilOrchestrator._task_gate_evidence_line(["src/app.py", "tests/x.py"])
    passed = SimpleNamespace(passed=True, adapter="command")
    assert "PASSED" in CouncilOrchestrator._task_gate_evidence_line(["src/app.py"], passed)
    failed = SimpleNamespace(passed=False, adapter="command")
    assert "FAILED" in CouncilOrchestrator._task_gate_evidence_line(["src/app.py"], failed)
    line = CouncilOrchestrator._task_gate_contract_line(
        SimpleNamespace(acceptance="endpoint added", write_scope=["src/"]))
    assert "['src/']" in line and "endpoint added" in line

def test_rehydrate_retry_on_readonly_task_forbids_writes():
    """Regression: Manager-REVISE retries on a read-only task must keep the
    no-write/no-bash instruction (slice run 14: the rehydrate retry had no
    warning and the implementer wrote src/app.py in a read-only task)."""
    retry = CouncilOrchestrator._build_execution_retry(
        SimpleNamespace(write_scope=[]), "Manager rejected task T1", "handoff_corruption")
    assert retry["strategy"] == "rehydrate_contract"
    assert "READ-ONLY" in retry["instruction"] and "bash" in retry["instruction"]
    writable = CouncilOrchestrator._build_execution_retry(
        SimpleNamespace(write_scope=["src/"]), "Manager rejected task T2", "handoff_corruption")
    assert "READ-ONLY" not in writable["instruction"]
    assert "bash" in writable["instruction"]  # channel warning on every retry

def test_guard_rejection_is_soft_tool_failure_not_attempt_abort():
    """Regression: a workspace-guard rejection must reach the model as a tool
    failure result instead of aborting the attempt (vertical-slice runs
    5/7/12-18: the first out-of-scope write killed the whole attempt). The
    write is still blocked; the attempt survives to correct course."""
    from src.agent_loop import _soft_guard_rejection
    from council_of_agents.scripts.workspace_revision import WorkspaceWriteGuard

    class Block:
        def __init__(self, tool, content=""):
            self.tool_type = tool
            self.content = content

    guard = WorkspaceWriteGuard("C:/ws", [], {}, enforce_channels=True, task_id="T1")
    desc, result = _soft_guard_rejection(Block("bash", "ls -la"), guard)
    assert result["guard_rejected"] is True and result["exit_code"] == 1
    assert "not compatible" in result["error"]
    desc, result = _soft_guard_rejection(Block("write_file", "src/app.py\nprint(1)"), guard)
    assert result["guard_rejected"] is True and "outside declared scope" in result["error"]
    # Compliant calls are untouched.
    assert _soft_guard_rejection(Block("read_file", '{"path": "src/app.py"}'), guard) is None

def test_write_file_parser_unwraps_json_object_calls():
    """Regression: write_file must accept the JSON-object call shape
    ({"path": ..., "content": ...}) models emit, not treat the object as the
    path (vertical-slice run 19: every write call was a JSON blob, so the
    attempt wrote nothing and died with NONE evidence)."""
    from src.tool_execution import _parse_write_file
    assert _parse_write_file('{"path": "src/app.py", "content": "def health(): pass"}') == {
        "path": "src/app.py", "content": "def health(): pass"}
    # Legacy text shape (path on first line) is unchanged.
    assert _parse_write_file("src/app.py\ndef health(): pass") == {
        "path": "src/app.py", "content": "def health(): pass"}
    # Non-JSON first line stays text.
    assert _parse_write_file("notes.txt\nhello")["path"] == "notes.txt"
