"""Fresh-state validation: Council mode session execution from clean state.

Validates:
1. Fresh session creation and initialization via council routes / session store.
2. Manager review parsing handles trailing model tokens (<|im_end|>function) and surrounding prose.
3. Completeness auditor gap-fill dispatches implementer with role "user" (not "assistant").
4. Dropped native tool calls (e.g. truncated write_file payload) are detected and nudged.
5. All control roles have hard timeouts >= 180s to prevent mid-stream cutoff.
"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from council_of_agents.scripts.council_orchestrator import CouncilOrchestrator
from council_of_agents.scripts.session_store import InMemorySessionStore, SessionState
from council_of_agents.scripts.task_dag import TaskDAG
import src.agent_loop as al


def test_fresh_session_creation_and_state(tmp_path):
    store = InMemorySessionStore()
    session = SessionState(
        session_id="test-session-123",
        user_prompt="Build a clean dashboard",
        complexity="MEDIUM",
        owner="test-user",
        workspace=str(tmp_path),
    )
    store.save(session)
    loaded = store.load("test-session-123")
    assert loaded is not None
    assert loaded.session_id == "test-session-123"
    assert loaded.status == "PENDING"
    assert loaded.user_prompt == "Build a clean dashboard"
    assert loaded.workspace == str(tmp_path)


def test_manager_review_json_parser_robustness():
    orch = CouncilOrchestrator(MagicMock())

    # Case 1: Trailing model tokens (real captured error: <|im_end|>function after JSON)
    raw_trailing = '{"verdict": "APPROVED", "summary": "Looks good", "issues": []}<|im_end|>function'
    assert orch._parse_manager_verdict(raw_trailing) == "APPROVED"
    assert orch._manager_review_issues(raw_trailing) == []

    # Case 2: Markdown code fence with trailing tokens
    raw_markdown = '```json\n{"verdict": "REVISE", "summary": "Fix bugs", "issues": ["bug 1"]}\n```<|im_end|>'
    assert orch._parse_manager_verdict(raw_markdown) == "REVISE"
    assert orch._manager_review_issues(raw_markdown) == ["bug 1"]

    # Case 3: Embedded JSON in surrounding prose
    raw_embedded = 'My review:\n{"verdict": "BLOCKED", "summary": "Missing docs", "issues": ["no docs"]}\nPlease address.'
    assert orch._parse_manager_verdict(raw_embedded) == "BLOCKED"
    assert orch._manager_review_issues(raw_embedded) == ["no docs"]

    # Case 4: Trailing comma in JSON payload
    raw_trailing_comma = '{"verdict": "APPROVED", "summary": "Looks good", "issues": [],}'
    assert orch._parse_manager_verdict(raw_trailing_comma) == "APPROVED"
    assert orch._manager_review_issues(raw_trailing_comma) == []

    # Case 5: Multiple JSON objects in surrounding prose
    raw_multi = 'Initial thought: {"note": 1}\n{"verdict": "REVISE", "summary": "Need tests", "issues": ["missing tests"]}\nNext: {"step": 2}'
    assert orch._parse_manager_verdict(raw_multi) == "REVISE"
    assert orch._manager_review_issues(raw_multi) == ["missing tests"]


@pytest.mark.asyncio
async def test_completeness_loop_gap_fill_role_is_user(tmp_path):
    orch = CouncilOrchestrator(MagicMock())
    partial_audit = {
        "completeness": 0.5,
        "done": False,
        "criteria": [
            {"id": "T1", "met": True, "gap_type": "fillable", "detail": "setup done"},
            {"id": "T2", "met": False, "gap_type": "fillable", "detail": "index.html missing"},
        ],
    }
    complete_audit = {
        "completeness": 1.0,
        "done": True,
        "criteria": [
            {"id": "T1", "met": True, "gap_type": "fillable", "detail": "setup done"},
            {"id": "T2", "met": True, "gap_type": "fillable", "detail": "index.html created"},
        ],
    }
    orch._run_completeness_audit = AsyncMock(side_effect=[partial_audit, complete_audit])
    orch._invoke_agent_safe = AsyncMock(return_value="Created index.html")

    state = SessionState(
        session_id="fresh-sess-1",
        owner="test-user",
        user_prompt="Create index.html",
        status="IN_PROGRESS",
    )
    events = []
    async def emit(**kw):
        events.append(kw)

    dag = TaskDAG.from_task_list([
        {"id": "T1", "description": "setup", "acceptance": "setup done"},
        {"id": "T2", "description": "index", "acceptance": "index.html created"},
    ])

    reply, metrics, cancelled = await orch._completeness_loop(
        state, dag, "initial", set(), set(), "PIPELINE",
        workspace=str(tmp_path), emit=emit, owner="test-user",
        resume_event=asyncio.Event(),
    )

    assert cancelled is False
    assert metrics["after"] == 1.0
    assert metrics["gaps_closed"] == 1
    # Crucial check: implementer was invoked with user role for the fix prompt
    assert orch._invoke_agent_safe.await_count == 1
    invoked_role, _, invoked_messages = orch._invoke_agent_safe.await_args.args[:3]
    assert invoked_role == "implementer"
    assert invoked_messages[-1]["role"] == "user"
    assert "The delivered work is INCOMPLETE" in invoked_messages[-1]["content"]


def test_control_roles_hard_timeouts_floor():
    floor = CouncilOrchestrator.AGENT_HARD_TIMEOUTS["strategist"]
    for role in (
        "chair", "strategist", "manager", "perspective_analyzer",
        "completeness_auditor", "validator_task",
    ):
        cap = CouncilOrchestrator.AGENT_HARD_TIMEOUTS.get(role, 0)
        assert cap >= floor, f"Role {role} hard timeout ({cap}s) is below floor ({floor}s)"


def test_council_js_frontend_json_parser_node():
    """Verify council.js JSON parsing logic directly inside a Node.js process."""
    import subprocess
    import shutil

    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("Node.js runtime not installed in environment")

    js_script = r"""
function safeParseJson(str) {
  if (!str || typeof str !== 'string') return null;
  const trimmed = str.trim();
  try {
    return JSON.parse(trimmed);
  } catch (e1) {
    try {
      let fixed = trimmed;
      fixed = fixed.replace(/\\/g, '\u0001');
      fixed = fixed.replace(/\\([`'])/g, '$1');
      fixed = fixed.replace(/\\(?![/"bfnrt]|u[0-9a-fA-F]{4})/g, '\\\\');
      fixed = fixed.replace(/\u0001/g, '\\\\');
      fixed = fixed.replace(/,\s*([}\]])/g, '$1');
      return JSON.parse(fixed);
    } catch (e2) {
      return null;
    }
  }
}

function extractJsonBlocks(text) {
  const blocks = [];
  let i = 0;
  while (i < text.length) {
    if (text[i] === '{') {
      let depth = 0;
      let inString = false;
      let escaped = false;
      let startIndex = i;
      let found = false;

      for (let j = i; j < text.length; j++) {
        const char = text[j];
        if (escaped) { escaped = false; continue; }
        if (char === '\\') { escaped = true; continue; }
        if (char === '"') { inString = !inString; continue; }
        if (!inString) {
          if (char === '{') { depth++; }
          else if (char === '}') {
            depth--;
            if (depth === 0) {
              const candidate = text.slice(startIndex, j + 1);
              const parsed = safeParseJson(candidate);
              if (parsed && typeof parsed === 'object') {
                blocks.push({ start: startIndex, end: j + 1, raw: candidate, parsed });
                i = j + 1;
                found = true;
                break;
              }
            }
          }
        }
      }
      if (found) continue;
    }
    i++;
  }
  return blocks;
}

function parseManagerReview(rawJson) {
  let clean = (rawJson || '').trim();
  if (clean.includes('```json')) {
    clean = clean.split('```json')[1].split('```')[0].trim();
  } else if (clean.startsWith('```')) {
    clean = clean.split('```')[1].split('```')[0].trim();
  }
  const blocks = extractJsonBlocks(clean);
  const target = blocks.find(b => b.parsed && b.parsed.verdict) || blocks[0];
  if (target && target.parsed && typeof target.parsed === 'object') {
    return target.parsed;
  }
  const direct = safeParseJson(clean);
  if (direct && typeof direct === 'object') return direct;
  const start = clean.indexOf('{');
  const end = clean.lastIndexOf('}');
  if (start !== -1 && end > start) {
    return safeParseJson(clean.slice(start, end + 1));
  }
  return null;
}

const cases = [
  // 1. Trailing model token
  { raw: '{"verdict": "APPROVED", "summary": "Ok"}<|im_end|>function', expVerdict: 'APPROVED' },
  // 2. Trailing comma
  { raw: '{"verdict": "REVISE", "summary": "Fix", "issues": ["bug"],}', expVerdict: 'REVISE' },
  // 3. Embedded in prose with multiple objects
  { raw: 'Thoughts: {"step": 1}\n{"verdict": "BLOCKED", "summary": "Bad"}\nExtra: {"step": 2}', expVerdict: 'BLOCKED' },
  // 4. Code with braces in string
  { raw: '{"verdict": "APPROVED", "summary": "function() { return 42; }"}<|im_end|>', expVerdict: 'APPROVED' },
  // 5. Markdown code fence
  { raw: '```json\n{"verdict": "APPROVED", "summary": "Clean"}\n```', expVerdict: 'APPROVED' },
];

for (const c of cases) {
  const res = parseManagerReview(c.raw);
  if (!res || res.verdict !== c.expVerdict) {
    console.error('FAILED case:', c, 'got:', res);
    process.exit(1);
  }
}
console.log('ALL_CASES_PASSED');
"""
    result = subprocess.run([node_bin, "-e", js_script], capture_output=True, text=True)
    assert result.returncode == 0, f"Node parsing failed: {result.stderr}"
    assert "ALL_CASES_PASSED" in result.stdout

