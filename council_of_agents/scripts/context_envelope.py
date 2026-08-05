"""Build structured context envelopes for the user message.

All dynamic context (workspace, session state, task info) lives here,
not in the system prompt. The system prompt is 100% static.
"""

import ast
import hashlib
import os
import re
import subprocess
from pathlib import Path


def build_context_envelope(
    workspace: str = None,
    repository_context: str = None,
    session_id: str = None,
    complexity: str = None,
    route: str = None,
    skill_context: str = None,
    past_context: str = None,
    success_context: str = None,
    task_description: str = None,
    dependency_outputs: str = None,
    chair_classification: str = None,
    plan_text: str = None,
    debate_context: str = None,
) -> str:
    """Build a structured context block to prepend to the user message.

    Returns empty string if no context is provided.

    ``workspace`` may be a relative label; absolute local paths are omitted
    from provider-bound context. ``repository_context`` is read-only evidence
    that agents must ground plans in and never substitutes for local tool scope.
    """
    sections = []

    if workspace and not os.path.isabs(workspace):
        sections.append(f"<workspace>\n{workspace}\n</workspace>")

    if repository_context:
        sections.append(
            f"<context:repository_capsule>\n{repository_context}\n</context:repository_capsule>"
        )

    state_parts = []
    for k, v in [
        ("session_id", session_id),
        ("complexity", complexity),
        ("route", route),
    ]:
        if v:
            state_parts.append(f"- {k}: {v}")
    if state_parts:
        sections.append(
            "<session_state>\n" + "\n".join(state_parts) + "\n</session_state>"
        )

    for label, val in [
        ("Relevant Skills", skill_context),
        ("Past Outcomes", past_context),
        ("Success Patterns", success_context),
        ("Chair Classification", chair_classification),
        ("Plan", plan_text),
        ("Task", task_description),
        ("Dependency Outputs", dependency_outputs),
        ("Debate Context", debate_context),
    ]:
        if val:
            tag = label.lower().replace(" ", "_")
            sections.append(f"<context:{tag}>\n{val}\n</context:{tag}>")

    return "\n\n".join(sections)

_RECON_MAX_FILES = 64
_RECON_MAX_RESULTS = 12
_RECON_MAX_FILE_BYTES = 32 * 1024
_RECON_MAX_BYTES = 256 * 1024
_RECON_EXCLUDED_PARTS = {".git", "data", "graphify-out", "node_modules", "__pycache__", "build", "dist", "benchmarks", "prompts", "fragments"}
_RECON_BINARY_SUFFIXES = {".bmp", ".gif", ".ico", ".jpeg", ".jpg", ".pdf", ".png", ".pyc", ".webp", ".zip"}
_RECON_SOURCE_SUFFIXES = {".js", ".jsx", ".py", ".ts", ".tsx"}
_RECON_SECRET = re.compile(r"(?i)(api[_-]?key|credential|password|secret|token|\.env)")
_RECON_WORDS = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_RECON_SKIP_WORDS = {"add", "after", "and", "before", "bug", "code", "council", "existing", "file", "fix", "for", "from", "hides", "into", "new", "regression", "test", "tests", "that", "the", "their", "this", "when", "with"}


def _recon_relative(path: str) -> str | None:
    value = str(path).replace("\\", "/").lstrip("./")
    parts = value.split("/")
    if not value or any(part in _RECON_EXCLUDED_PARTS for part in parts):
        return None
    if Path(value).suffix.lower() in _RECON_BINARY_SUFFIXES or _RECON_SECRET.search(value):
        return None
    return value


def _recon_terms(task: str) -> list[str]:
    terms = []
    for word in _RECON_WORDS.findall(str(task or "").lower()):
        word = word[:-1] if word.endswith("s") and len(word) > 4 else word
        if word not in _RECON_SKIP_WORDS and not _RECON_SECRET.search(word) and word not in terms:
            terms.append(word)
    return terms[:8]


def _recon_run(args: list[str], workspace: Path) -> list[str]:
    result = subprocess.run(
        ["rg", *args], cwd=workspace, text=True, encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5, check=False,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError("repository reconnaissance command failed")
    return result.stdout.splitlines()


def _recon_symbols(workspace: Path, paths: list[str]) -> list[str]:
    symbols, used = [], 0
    for rel in paths:
        if not rel.endswith(".py"):
            continue
        path = workspace / rel
        size = path.stat().st_size
        if size > _RECON_MAX_FILE_BYTES or used + size > _RECON_MAX_BYTES:
            continue
        used += size
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeError):
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                symbols.append(f"{rel}:{node.name}")
            elif isinstance(node, ast.ClassDef):
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        symbols.append(f"{rel}:{node.name}.{child.name}")
            if len(symbols) >= _RECON_MAX_RESULTS:
                return symbols
    return symbols


def build_repository_capsule(workspace: str | os.PathLike, user_task: str) -> dict:
    """Return bounded, source-free repository facts for planning."""
    root = Path(workspace).resolve()
    if not root.is_dir():
        raise RuntimeError("repository reconnaissance workspace is unavailable")
    files = sorted(filter(None, (_recon_relative(line) for line in _recon_run(
        ["--files", "--hidden", "--sort", "path"], root
    ))))
    files = [rel for rel in files if Path(rel).suffix.lower() in _RECON_SOURCE_SUFFIXES and (root / rel).is_file() and (root / rel).stat().st_size <= _RECON_MAX_FILE_BYTES]
    explicit = []
    for raw in sorted(set(re.findall(r"(?<![A-Za-z0-9_./-])(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+", str(user_task or "")))):
        rel = _recon_relative(raw)
        if rel in files:
            explicit.append(rel)
    terms = _recon_terms(user_task)
    named_sources = sorted((path for path in files if not path.startswith("tests/") and any(term in path.lower() for term in terms)), key=lambda path: (-sum(term in path.lower() for term in terms), path))
    named_tests = sorted((path for path in files if path.startswith("tests/") and any(term in path.lower() for term in terms)), key=lambda path: (-sum(term in path.lower() for term in terms), path))
    generic_sources = [path for path in files if not path.startswith("tests/")]
    generic_tests = [path for path in files if path.startswith("tests/")]
    candidates = sorted(dict.fromkeys(explicit + named_sources[:32] + named_tests[:16] + generic_sources[:16] + generic_tests[:8]))[:_RECON_MAX_FILES]
    scores = {path: 100 for path in explicit}
    for term in terms:
        for rel in (_recon_relative(line) for line in _recon_run(["-i", "-l", "-F", "--max-filesize", "32K", "--", term, *candidates], root)):
            if rel in candidates:
                scores[rel] = scores.get(rel, 0) + 1
    selected = sorted(scores, key=lambda path: (-scores[path], path))[:_RECON_MAX_RESULTS]
    tests = [path for path in selected if path.startswith("tests/") and Path(path).name.startswith("test_")]
    if not tests:
        tests = [path for path in named_tests if Path(path).name.startswith("test_")][:3]
    scopes, code_scopes = [], 0
    for path in selected:
        if len(Path(path).parts) < 2:
            continue
        scope = f"{Path(path).parts[0]}/"
        if scope == "tests/" and scope not in scopes:
            scopes.append(scope)
        elif scope != "tests/" and scope not in scopes and code_scopes < 2:
            scopes.append(scope)
            code_scopes += 1
    scopes.sort()
    discovery_required = not selected
    if discovery_required:
        scopes = sorted({f"{Path(path).parts[0]}/" for path in candidates if len(Path(path).parts) > 1 and not path.startswith("tests/")})[:2]
        if any(path.startswith("tests/") for path in candidates):
            scopes = sorted(set(scopes + ["tests/"]))
    status = "ok" if selected else ("discovery_required" if scopes else "blocked")
    symbols = _recon_symbols(root, [path for path in selected if not path.startswith("tests/")])
    commands = [f"python -m pytest -q {path}" for path in tests[:3]] or (["python -m pytest -q tests/"] if "tests/" in scopes else [])
    facts = {
        "status": status, "search_terms": terms, "selected_paths": selected,
        "matching_symbols": symbols, "regression_tests": tests, "test_commands": commands,
        "frameworks": ["pytest"] if (root / "pyproject.toml").is_file() or (root / "pytest.ini").is_file() else [],
        "allowed_workspace_scope": scopes, "discovery_required": discovery_required,
        "truncated": len(files) > _RECON_MAX_FILES or len(scores) > _RECON_MAX_RESULTS,
        "limits": {"files": _RECON_MAX_FILES, "results": _RECON_MAX_RESULTS, "bytes": _RECON_MAX_BYTES},
    }
    lines = ["Repository reconnaissance. Relative paths only; no source content:"]
    for key in ("selected_paths", "matching_symbols", "regression_tests", "test_commands", "frameworks", "allowed_workspace_scope"):
        lines.append(f"{key}:")
        lines.extend(f"- {value}" for value in facts[key]) if facts[key] else lines.append("- none")
    lines.append(f"discovery_required: {str(discovery_required).lower()}")
    lines.append("Discovery, if required, must be read-only; dependent writes must use its output.")
    facts["capsule"] = "\n".join(lines)
    facts["sha256"] = hashlib.sha256(facts["capsule"].encode("utf-8")).hexdigest()
    return facts