import pytest

from council_of_agents.scripts.workspace_revision import (
    WorkspaceConflictError,
    WorkspaceScopeError,
    WorkspaceWriteGuard,
    snapshot_workspace,
)


def test_revision_is_stable_and_changes_with_scoped_content(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    target = src / "a.py"
    target.write_text("x = 1\n", encoding="utf-8")

    first = snapshot_workspace(tmp_path, ["src"])
    same = snapshot_workspace(tmp_path, ["src", "src"])
    assert first == same
    assert "src/a.py" in first.file_hashes

    target.write_text("x = 2\n", encoding="utf-8")
    changed = snapshot_workspace(tmp_path, ["src"])
    assert changed.revision != first.revision


def test_revision_records_missing_scope(tmp_path):
    revision = snapshot_workspace(tmp_path, ["missing.txt"])
    assert revision.file_hashes == {"missing.txt": "<missing>"}


def test_revision_rejects_workspace_escape(tmp_path):
    with pytest.raises(WorkspaceScopeError, match="escapes root"):
        snapshot_workspace(tmp_path, ["../outside"])


def test_write_guard_blocks_stale_file_before_mutation(tmp_path):
    target = tmp_path / "src" / "a.py"
    target.parent.mkdir()
    target.write_text("v1", encoding="utf-8")
    base = snapshot_workspace(tmp_path, ["src/a.py"])
    guard = WorkspaceWriteGuard(tmp_path, ["src/a.py"], base.file_hashes)

    target.write_text("external change", encoding="utf-8")
    with pytest.raises(WorkspaceConflictError, match="stale workspace write blocked"):
        guard.check_before_write("edit_file", '{"path":"src/a.py"}')


def test_write_guard_allows_own_followup_write_after_rebase(tmp_path):
    target = tmp_path / "src" / "a.py"
    target.parent.mkdir()
    target.write_text("v1", encoding="utf-8")
    base = snapshot_workspace(tmp_path, ["src/a.py"])
    guard = WorkspaceWriteGuard(tmp_path, ["src"], base.file_hashes)

    assert guard.check_before_write("edit_file", '{"path":"src/a.py"}') == "src/a.py"
    target.write_text("v2", encoding="utf-8")
    guard.record_after_write("edit_file", '{"path":"src/a.py"}')
    assert guard.check_before_write("edit_file", '{"path":"src/a.py"}') == "src/a.py"


def test_write_guard_rejects_undeclared_write_scope(tmp_path):
    guard = WorkspaceWriteGuard(tmp_path, [], {})
    with pytest.raises(WorkspaceScopeError, match="outside declared scope"):
        guard.check_before_write("write_file", "new.txt\ncontent")


def test_write_guard_rejects_unverifiable_mutation_channels(tmp_path):
    guard = WorkspaceWriteGuard(tmp_path, ["src"], {})
    with pytest.raises(WorkspaceScopeError, match="not compatible"):
        guard.check_tool_channel("bash")
    with pytest.raises(WorkspaceScopeError, match="not compatible"):
        guard.check_tool_channel("mcp__filesystem__write")
    guard.check_tool_channel("read_file")


def test_read_only_guard_rejects_writes_and_shell_channels(tmp_path):
    guard = WorkspaceWriteGuard(tmp_path, [], {}, enforce_channels=True)
    with pytest.raises(WorkspaceScopeError, match="not compatible"):
        guard.check_tool_channel("bash")
    with pytest.raises(WorkspaceScopeError, match="outside declared scope"):
        guard.check_before_write("write_file", "output.txt\ntrash")


def test_write_guard_extracts_path_from_malformed_json_edit_file(tmp_path):
    """An edit_file whose JSON has unescaped quotes (docstring-style triple
    quotes inside string values) must still be scope-checked via the declared
    path instead of hard-stopping on 'no parseable path' (regression: the
    vertical slice failed when one malformed-but-declared path killed the
    task with zero retries)."""
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir()
    target.write_text('def app():\n    return {"status": "running"}\n', encoding="utf-8")
    base = snapshot_workspace(tmp_path, ["src"])
    guard = WorkspaceWriteGuard(tmp_path, ["src"], base.file_hashes)
    malformed = ('{"path": "src/app.py", "old_string": """Small service entrypoint."""\n'
                 'def app():\n    return {"status": "running"}", "new_string": "new body"}')
    assert guard.check_before_write("edit_file", malformed) == "src/app.py"

    # Scope enforcement still applies to the leniently extracted path.
    narrow = WorkspaceWriteGuard(tmp_path, ["src/app.py"], base.file_hashes)
    with pytest.raises(WorkspaceScopeError, match="outside declared scope"):
        narrow.check_before_write("edit_file", malformed.replace("src/app.py", "tests/test_app.py"))


def test_write_guard_allows_declared_new_file_once(tmp_path):
    guard = WorkspaceWriteGuard(tmp_path, ["src"], {})
    assert guard.check_before_write("write_file", "src/new.py\nprint('x')") == "src/new.py"
    target = tmp_path / "src" / "new.py"
    target.parent.mkdir()
    target.write_text("print('x')", encoding="utf-8")
    guard.record_after_write("write_file", "src/new.py\nprint('x')")
    assert guard.check_before_write("edit_file", '{"path":"src/new.py"}') == "src/new.py"
