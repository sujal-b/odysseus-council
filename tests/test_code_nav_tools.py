"""Tests for the code-navigation tools (grep, glob, ls) + read_file line range."""
import os
import shutil
import asyncio
import tempfile
import json
from types import SimpleNamespace
import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/test_code_nav.db")

from src.tool_execution import _direct_fallback
import src.tool_execution as tool_execution
from src.agent_tools.filesystem_tools import ReadFileTool


def _run(tool, content):
    return asyncio.run(_direct_fallback(tool, content))


def _run_read(content):
    """Exercise the deterministic reader without the optional MCP transport."""
    return asyncio.run(ReadFileTool().execute(content, {}))


@pytest.fixture
def repo():
    # The platform temp directory is on the default tool-path allowlist.
    # Avoid POSIX-rooted /tmp on Windows: resolving that synthetic path can
    # block in some Python/drive configurations.
    root = tempfile.mkdtemp(prefix="codenav_").replace("\\", "/")
    try:
        with open(os.path.join(root, "a.py"), "w") as f:
            f.write("import os\n# needle here\nprint('x')\n")
        os.mkdir(os.path.join(root, "sub"))
        with open(os.path.join(root, "sub", "b.txt"), "w") as f:
            f.write("nothing\nNEEDLE upper\n")
        os.mkdir(os.path.join(root, "node_modules"))
        with open(os.path.join(root, "node_modules", "dep.py"), "w") as f:
            f.write("needle in dep\n")
        g = os.path.join(root, ".git")
        os.mkdir(g)
        with open(os.path.join(g, "config"), "w") as f:
            f.write("needle in git\n")
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ── grep ──────────────────────────────────────────────────────────────────

def test_grep_finds_match(repo):
    r = _run("grep", f'{{"pattern": "needle", "path": "{repo}"}}')
    assert r["exit_code"] == 0
    assert "a.py:2:" in r["output"]


def test_grep_skips_junk_dirs(repo):
    r = _run("grep", f'{{"pattern": "needle", "path": "{repo}"}}')
    assert "node_modules" not in r["output"]
    assert ".git/config" not in r["output"]


def test_grep_ignore_case(repo):
    r = _run("grep", f'{{"pattern": "needle", "ignore_case": true, "path": "{repo}"}}')
    assert "b.txt:2:" in r["output"]


def test_grep_glob_filter(repo):
    r = _run("grep", f'{{"pattern": "needle", "ignore_case": true, "glob": "*.py", "path": "{repo}"}}')
    assert "a.py" in r["output"]
    assert "b.txt" not in r["output"]


def test_grep_no_match(repo):
    r = _run("grep", f'{{"pattern": "zzzznotfound", "path": "{repo}"}}')
    assert r["exit_code"] == 0
    assert "No matches" in r["output"]


def test_grep_requires_pattern(repo):
    r = _run("grep", "{}")
    assert r["exit_code"] == 1
    assert "pattern is required" in r["error"]


def test_grep_path_outside_roots_rejected(repo):
    r = _run("grep", '{"pattern": "x", "path": "/etc"}')
    assert r["exit_code"] == 1
    assert "outside the allowed roots" in r["error"]


def test_grep_python_fallback_when_no_rg(repo, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    r = _run("grep", f'{{"pattern": "needle", "path": "{repo}"}}')
    assert r["exit_code"] == 0
    assert "a.py:2:" in r["output"]
    assert "node_modules" not in r["output"]
    assert ".git/config" not in r["output"]


# ── glob ──────────────────────────────────────────────────────────────────

def test_glob_py(repo):
    r = _run("glob", f'{{"pattern": "*.py", "path": "{repo}"}}')
    assert r["exit_code"] == 0
    assert "a.py" in r["output"]


def test_glob_recursive_skips_junk(repo):
    r = _run("glob", f'{{"pattern": "**/*.py", "path": "{repo}"}}')
    assert "a.py" in r["output"]
    assert "node_modules" not in r["output"]


def test_glob_requires_pattern(repo):
    r = _run("glob", "{}")
    assert r["exit_code"] == 1


# ── ls ────────────────────────────────────────────────────────────────────

def test_ls_lists_entries(repo):
    r = _run("ls", f'{{"path": "{repo}"}}')
    assert r["exit_code"] == 0
    assert "a.py" in r["output"]
    assert "sub/" in r["output"]
    assert ".git" not in r["output"]  # hidden skipped


def test_ls_path_outside_rejected(repo):
    r = _run("ls", '{"path": "/etc"}')
    assert r["exit_code"] == 1
    assert "outside the allowed roots" in r["error"]


# ── read_file line range ───────────────────────────────────────────────────

def test_read_file_offset_limit(repo):
    p = os.path.join(repo, "lines.txt")
    with open(p, "w") as f:
        f.write("\n".join(f"line{i}" for i in range(1, 11)) + "\n")
    r = _run_read(json.dumps({"path": p, "offset": 3, "limit": 2}))
    assert r["exit_code"] == 0
    assert r["output"] == "line3\nline4\n"


def test_read_file_plain_path_backcompat(repo):
    r = _run_read(os.path.join(repo, "a.py"))
    assert r["exit_code"] == 0
    assert "needle" in r["output"]
    assert r["read_mode"] == "full"


def test_read_file_large_path_returns_index_not_whole_file(repo):
    p = os.path.join(repo, "large.py")
    with open(p, "w") as f:
        f.write("\n".join(["def first():", "    return 1"] + [f"value_{i} = {i}" for i in range(300)]) + "\n")
    r = _run_read(p)
    assert r["exit_code"] == 0
    assert r["read_mode"] == "index"
    assert "Full content withheld by context policy" in r["output"]
    assert "def first():" in r["output"]
    assert "value_299" not in r["output"]
    assert r["total_lines"] == 302


def test_read_file_query_returns_relevant_neighborhood_only(repo):
    p = os.path.join(repo, "query.py")
    with open(p, "w") as f:
        f.write("\n".join([f"line_{i}" for i in range(200)] + ["def target_symbol():", "    return 42"] + [f"tail_{i}" for i in range(100)]) + "\n")
    r = _run_read(json.dumps({"path": p, "query": "target_symbol"}))
    assert r["read_mode"] == "retrieval"
    assert "201: def target_symbol():" in r["output"]
    assert "1: line_0" not in r["output"]
    assert "tail_99" not in r["output"]


def test_read_file_explicit_window_is_hard_capped(repo):
    p = os.path.join(repo, "many.txt")
    with open(p, "w") as f:
        f.write("\n".join(f"line{i}" for i in range(1, 501)) + "\n")
    r = _run_read(json.dumps({"path": p, "offset": 1, "limit": 9999}))
    assert r["read_mode"] == "window"
    assert "line240\n" in r["output"]
    assert "line241\n" not in r["output"]
    assert r["next_offset"] == 241


def test_legacy_read_route_cannot_bypass_policy_gate(monkeypatch):
    async def fake_direct(tool, content, **kwargs):
        return {"output": "bounded", "exit_code": 0, "read_mode": "index"}

    monkeypatch.setattr(tool_execution, "_direct_fallback", fake_direct)
    monkeypatch.setattr(
        tool_execution,
        "get_mcp_manager",
        lambda: (_ for _ in ()).throw(AssertionError("MCP must not receive file reads")),
    )
    result = asyncio.run(tool_execution._call_mcp_tool("read_file", '{"path":"large.py"}'))
    assert result["read_mode"] == "index"


def test_native_mcp_read_route_cannot_bypass_policy_gate(monkeypatch):
    async def fake_direct(tool, content, **kwargs):
        assert tool == "read_file"
        return {"output": "bounded", "exit_code": 0, "read_mode": "window"}

    monkeypatch.setattr(tool_execution, "_direct_fallback", fake_direct)
    monkeypatch.setattr(tool_execution, "is_public_blocked_tool", lambda tool: False)
    block = SimpleNamespace(
        tool_type="mcp__filesystem__read_file",
        content='{"path":"large.py","offset":20,"limit":40}',
    )
    _, result = asyncio.run(tool_execution.execute_tool_block(block, skip_workspace_check=True))
    assert result["read_mode"] == "window"


def test_tool_feedback_has_a_final_context_boundary():
    payload = "A" * 15000 + "TAIL_SENTINEL"
    formatted = tool_execution.format_tool_result("untrusted tool", {"output": payload, "exit_code": 0})
    assert "chars omitted by context policy" in formatted
    assert "TAIL_SENTINEL" in formatted
    assert len(formatted) < 11000
