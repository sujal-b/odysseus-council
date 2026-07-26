import asyncio
import logging
import re
import sys
import time
import collections
import os
import signal
import subprocess
from typing import Optional, Callable, Awaitable, Tuple, Dict, List
from src.constants import MAX_OUTPUT_CHARS

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Bash command safety
# ---------------------------------------------------------------------------

# Positive set: command prefixes that are unambiguously read-only / non-destructive.
# git commit and git push are intentionally EXCLUDED — they mutate remote state.
BASH_READONLY_PREFIXES: frozenset[str] = frozenset({
    "ls", "cat", "pwd", "echo", "which", "whoami", "date", "head", "tail",
    "grep", "rg", "findstr", "select-string", "get-childitem",
    "wc", "diff", "find", "file", "stat", "du", "df", "env", "printenv",
    "type", "uname", "hostname", "id", "groups", "dir",
    # git read-only ops
    "git status", "git log", "git diff", "git branch", "git show",
    "git stash list", "git remote", "git tag",
    # package manager inspection
    "pip list", "pip show", "pip freeze", "pip check",
    "npm list", "npm ls", "npm outdated",
    # version checks
    "python --version", "python3 --version", "node --version",
    "npm --version", "pip --version", "pip3 --version",
    # build/test inspection
    "pytest --collect-only", "pytest --version",
})

# Regex patterns that catch obfuscated destructive commands regardless of
# extra whitespace or flag ordering. Applied AFTER whitespace normalisation.
_DESTRUCTIVE_PATTERNS: List[re.Pattern[str]] = [
    # rm with -r flag anywhere in the flag cluster, followed by / (flags may intervene)
    re.compile(r"\brm\s+(?:-[\w]+\s+)*-[\w]*r[\w]*(?:\s+-[^\s]+)*\s+/"),
    re.compile(r"\brm\s+(?:-[\w]+\s+)*-[\w]*f[\w]*(?:\s+-[^\s]+)*\s+/"),
    re.compile(r"\bmkfs\b"),                        # format filesystem
    re.compile(r"\bdd\s+if="),                     # block device overwrite
    re.compile(r"\bshred\b"),                      # secure delete
    re.compile(r"\bwipefs\b"),                     # wipe filesystem signatures
    re.compile(r">\s*/dev/sd[a-z]"),               # redirect to block device
    re.compile(r">\s*/dev/nvme"),                  # redirect to nvme device
    re.compile(r"\|\s*(bash|sh|zsh|fish|dash)\b"),# piping to shell interpreter
    re.compile(r"\b(curl|wget)\s+"),               # network exfil / install
    re.compile(r"\b(ssh|sftp|ftp|scp)\s+"),        # remote access
    re.compile(r"\bchmod\s+[0-7]*7[0-7]*\s+/"),   # world-writable at root
    re.compile(r"\bchown\s+.+\s+/\s*$"),           # change owner of root
    re.compile(r"\b(nc|netcat|ncat)\s+"),          # netcat exfil
    re.compile(r"\bpython[23]?\s+-c\s+"),          # inline python exec via bash
    re.compile(r"\beval\s+"),                      # eval
    re.compile(r"\bexec\s+"),                      # exec
    re.compile(r"\$\([^)]+\)"),                    # command substitution $()
    re.compile(r"`[^`]+`"),                        # backtick substitution
]

# Pattern that matches sub-commands that ARE a bare shell interpreter
# (the left side of `echo x | bash` once split on |)
_BARE_SHELL_RE = re.compile(r"^(bash|sh|zsh|fish|dash)(\s|$)")

# Shell chain operators — split compound commands on these
_CHAIN_SPLIT_RE = re.compile(r"&&|\|\||;|\|(?!\|)")

# Patterns for detecting --index-url / --registry in install commands (for SSRF check)
_INDEX_URL_RE = re.compile(
    r"(?:--index-url|--extra-index-url|-i)\s+(\S+)",
    re.IGNORECASE,
)
_NPM_REGISTRY_RE = re.compile(
    r"(?:--registry)\s+(\S+)",
    re.IGNORECASE,
)


def _normalise(cmd: str) -> str:
    """Collapse runs of whitespace to a single space and strip."""
    return re.sub(r"\s+", " ", cmd).strip()


def _split_compound(cmd: str) -> List[str]:
    """Split a shell command on &&, ||, ;, and | into sub-commands."""
    parts = _CHAIN_SPLIT_RE.split(cmd)
    return [p.strip() for p in parts if p.strip()]


def _extract_install_index_urls(cmd: str) -> List[str]:
    """Extract custom registry/index URLs from pip/npm install commands."""
    urls: List[str] = []
    urls.extend(_INDEX_URL_RE.findall(cmd))
    urls.extend(_NPM_REGISTRY_RE.findall(cmd))
    return urls


def _validate_bash_command(cmd: str) -> Optional[str]:
    """Return an error message string if *cmd* should be blocked, else None.

    Validation steps:
    1. Normalise whitespace to defeat double-space bypasses.
    2. Check the full normalised command against _DESTRUCTIVE_PATTERNS first
       (catches pipe-to-shell patterns before splitting removes the pipe).
    3. Split on compound operators and check each sub-command individually
       against the same patterns plus the bare-shell-interpreter check.
    4. For pip/npm install commands with a custom --index-url/--registry,
       validate that the URL is a public HTTP endpoint (reuses url_security).
    """
    normalised = _normalise(cmd)

    # Step 2: whole-command patterns (must run before splitting removes |)
    for pattern in _DESTRUCTIVE_PATTERNS:
        if pattern.search(normalised):
            return (
                f"Security block: command contains a forbidden pattern "
                f"(matched /{pattern.pattern}/) in: {normalised!r}"
            )

    # Step 3: per-sub-command checks
    sub_cmds = _split_compound(normalised)
    for sub in sub_cmds:
        sub_norm = _normalise(sub)

        # Bare shell interpreter as a sub-command (right side of pipe after split)
        if _BARE_SHELL_RE.match(sub_norm):
            return (
                f"Security block: sub-command is a bare shell interpreter: {sub_norm!r}"
            )

        for pattern in _DESTRUCTIVE_PATTERNS:
            if pattern.search(sub_norm):
                return (
                    f"Security block: command contains a forbidden pattern "
                    f"(matched /{pattern.pattern}/) in: {sub_norm!r}"
                )

        # SSRF guard: validate custom package registry URLs
        if re.match(r"pip[23]?\s+install|pip[23]?\s+download", sub_norm, re.IGNORECASE) or \
                re.match(r"npm\s+install|npm\s+i\b", sub_norm, re.IGNORECASE):
            for url in _extract_install_index_urls(sub_norm):
                try:
                    from src.url_security import is_public_http_url
                    if not is_public_http_url(url):
                        return (
                            f"Security block: package registry URL is not a public "
                            f"HTTP endpoint: {url!r}"
                        )
                except Exception:
                    logger.warning("url_security unavailable — SSRF guard for package registry URLs is inactive")
                    pass

    return None

DEFAULT_BASH_TIMEOUT = 300     # 300 seconds (5 minutes)
DEFAULT_PYTHON_TIMEOUT = 60 * 60

PROGRESS_INTERVAL_S = 2.0
PROGRESS_TAIL_LINES = 12

# Process group flags for subprocesses
extra_kwargs = {}
if os.name == 'nt':
    extra_kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
else:
    extra_kwargs['start_new_session'] = True


def kill_process_tree(proc):
    try:
        pid = proc.pid
        if os.name == 'nt':
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            os.killpg(pid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


async def _run_subprocess_streaming(
    proc: asyncio.subprocess.Process,
    *,
    timeout: float,
    progress_cb: Optional[Callable[[Dict], Awaitable[None]]] = None,
) -> Tuple[str, str, Optional[int], bool]:
    started = time.time()
    stdout_full: list[str] = []
    stderr_full: list[str] = []
    tail = collections.deque(maxlen=PROGRESS_TAIL_LINES)

    async def _reader(stream, full_buf, label: str):
        if stream is None:
            return
        while True:
            line = await stream.readline()
            if not line:
                break
            decoded = line.decode("utf-8", errors="replace").rstrip("\n")
            full_buf.append(decoded)
            if label == "err":
                tail.append(f"! {decoded}")
            else:
                tail.append(decoded)

    async def _progress_emitter():
        await asyncio.sleep(PROGRESS_INTERVAL_S)
        while True:
            if progress_cb:
                try:
                    await progress_cb({
                        "elapsed_s": round(time.time() - started, 1),
                        "tail": "\n".join(list(tail)),
                    })
                except Exception:
                    pass
            await asyncio.sleep(PROGRESS_INTERVAL_S)

    rd_out = asyncio.create_task(_reader(proc.stdout, stdout_full, "out"))
    rd_err = asyncio.create_task(_reader(proc.stderr, stderr_full, "err"))
    prog_task = asyncio.create_task(_progress_emitter()) if progress_cb else None

    timed_out = False
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        timed_out = True
        try:
            kill_process_tree(proc)
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
        except Exception:
            pass
    except asyncio.CancelledError:
        try:
            kill_process_tree(proc)
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
        except Exception:
            pass
        for t in (rd_out, rd_err):
            t.cancel()
        if prog_task is not None:
            prog_task.cancel()
        raise
    finally:
        if prog_task is not None and not prog_task.done():
            prog_task.cancel()
            try:
                await prog_task
            except (asyncio.CancelledError, Exception):
                pass
        for t in (rd_out, rd_err):
            try:
                await asyncio.wait_for(t, timeout=1)
            except Exception:
                pass

    return (
        "\n".join(stdout_full),
        "\n".join(stderr_full),
        proc.returncode,
        timed_out,
    )

class BashTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import _AGENT_WORKDIR, _truncate
        progress_cb = ctx.get("progress_cb")
        workspace = ctx.get("workspace")
        _subproc_env = ctx.get("subproc_env")

        # Validate the command before execution
        block_reason = _validate_bash_command(content)
        if block_reason:
            return {"error": block_reason, "exit_code": 1}

        proc = await asyncio.create_subprocess_shell(
            content,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_subproc_env,
            cwd=workspace or _AGENT_WORKDIR,
            **extra_kwargs
        )
        stdout, stderr, rc, timed_out = await _run_subprocess_streaming(
            proc,
            timeout=DEFAULT_BASH_TIMEOUT,
            progress_cb=progress_cb,
        )
        if timed_out:
            return {"error": f"bash: timed out after {DEFAULT_BASH_TIMEOUT}s — process killed", "exit_code": 124, "stdout": _truncate(stdout, MAX_OUTPUT_CHARS), "stderr": _truncate(stderr, MAX_OUTPUT_CHARS)}
        output = stdout.rstrip()
        err = stderr.rstrip()
        if err:
            output = (output + "\nSTDERR: " + err).strip() if output else "STDERR: " + err
        output = _truncate(output, MAX_OUTPUT_CHARS)
        return {"output": output or "(no output)", "exit_code": rc or 0}

class PythonTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import _AGENT_WORKDIR, _truncate
        progress_cb = ctx.get("progress_cb")
        workspace = ctx.get("workspace")
        _subproc_env = ctx.get("subproc_env")
        proc = await asyncio.create_subprocess_exec(
            (sys.executable or "python"), "-I", "-c", content,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_subproc_env,
            cwd=workspace or _AGENT_WORKDIR,
            **extra_kwargs
        )
        stdout, stderr, rc, timed_out = await _run_subprocess_streaming(
            proc,
            timeout=DEFAULT_PYTHON_TIMEOUT,
            progress_cb=progress_cb,
        )
        if timed_out:
            return {"error": f"python: timed out after {DEFAULT_PYTHON_TIMEOUT}s — process killed", "exit_code": 124, "stdout": _truncate(stdout, MAX_OUTPUT_CHARS), "stderr": _truncate(stderr, MAX_OUTPUT_CHARS)}
        output = stdout.rstrip()
        err = stderr.rstrip()
        if err:
            output = (output + "\nSTDERR: " + err).strip() if output else "STDERR: " + err
        output = _truncate(output, MAX_OUTPUT_CHARS)
        return {"output": output or "(no output)", "exit_code": rc or 0}
