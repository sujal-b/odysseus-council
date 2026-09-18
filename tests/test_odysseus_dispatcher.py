import os

import pytest

from tests.helpers.cli_loader import load_script


@pytest.mark.skipif(os.name == "nt", reason="POSIX exec-bit only; nt uses is_file")
def test_is_runnable_subcommand_requires_executable_file(tmp_path):
    cli = load_script("odysseus")
    sub = tmp_path / "odysseus-demo"
    sub.write_text("#!/bin/sh\n")
    sub.chmod(0o644)

    assert cli._is_runnable_subcommand(sub) is False

    sub.chmod(0o755)
    assert cli._is_runnable_subcommand(sub) is True


def test_venv_python_resolves_os_aware(tmp_path, monkeypatch):
    cli = load_script("odysseus")
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    venv_bin = tmp_path / "venv" / "bin" / "python"
    venv_bin.parent.mkdir(parents=True)
    venv_bin.write_text("")
    venv_win = tmp_path / "venv" / "Scripts" / "python.exe"
    venv_win.parent.mkdir(parents=True)
    venv_win.write_text("")
    sub = scripts_dir / "odysseus-foo"
    sub.write_text("#!/bin/sh\necho hi\n")
    sub.chmod(0o755)
    monkeypatch.setattr(cli, "SCRIPTS_DIR", scripts_dir)

    captured = {}

    def fake_execv(path, args):
        captured["py"] = path
        captured["args"] = args
        raise SystemExit(0)

    def fake_call(args, **kw):
        captured["py"] = args[0]
        captured["args"] = args
        return 0

    monkeypatch.setattr(cli.os, "execv", fake_execv)
    monkeypatch.setattr(cli.subprocess, "call", fake_call)

    monkeypatch.setattr(cli.os, "name", "posix")
    captured.clear()
    try:
        cli.main(["foo", "x"])
    except SystemExit:
        pass
    assert captured["py"].replace("\\", "/").endswith("venv/bin/python")

    monkeypatch.setattr(cli.os, "name", "nt")
    captured.clear()
    try:
        cli.main(["foo", "x"])
    except SystemExit:
        pass
    assert captured["py"].replace("\\", "/").endswith("venv/Scripts/python.exe")


def test_is_runnable_extensionless_without_exec_bit_counts_on_nt(tmp_path, monkeypatch):
    cli = load_script("odysseus")
    sub = tmp_path / "odysseus-demo"
    sub.write_text("#!/bin/sh\n")
    sub.chmod(0o644)
    monkeypatch.setattr(cli.os, "access", lambda p, mode: False)
    monkeypatch.setattr(cli.os, "name", "nt")
    assert cli._is_runnable_subcommand(sub) is True
    monkeypatch.setattr(cli.os, "name", "posix")
    assert cli._is_runnable_subcommand(sub) is False
    monkeypatch.setattr(cli.os, "access", lambda p, mode: True)
    monkeypatch.setattr(cli.os, "name", "posix")
    assert cli._is_runnable_subcommand(sub) is True
    monkeypatch.setattr(cli.os, "name", "nt")
    assert cli._is_runnable_subcommand(sub) is True


def test_exec_branch_uses_subprocess_on_nt_and_execv_on_posix(tmp_path, monkeypatch):
    cli = load_script("odysseus")
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    venv_bin = tmp_path / "venv" / "bin" / "python"
    venv_bin.parent.mkdir(parents=True)
    venv_bin.write_text("")
    venv_win = tmp_path / "venv" / "Scripts" / "python.exe"
    venv_win.parent.mkdir(parents=True)
    venv_win.write_text("")
    sub = scripts_dir / "odysseus-foo"
    sub.write_text("#!/bin/sh\necho hi\n")
    sub.chmod(0o755)
    monkeypatch.setattr(cli, "SCRIPTS_DIR", scripts_dir)

    exec_calls = []
    call_calls = []

    def fake_execv(path, args):
        exec_calls.append((path, list(args)))
        raise SystemExit(0)

    def fake_call(args, **kw):
        call_calls.append(list(args))
        return 0

    monkeypatch.setattr(cli.os, "execv", fake_execv)
    monkeypatch.setattr(cli.subprocess, "call", fake_call)

    monkeypatch.setattr(cli.os, "name", "posix")
    try:
        cli.main(["foo", "a", "b"])
    except SystemExit:
        pass
    assert len(exec_calls) == 1
    assert len(call_calls) == 0
    assert exec_calls[0][0].replace("\\", "/").endswith("venv/bin/python")
    assert exec_calls[0][1][1].endswith("odysseus-foo")
    assert exec_calls[0][1][2:] == ["a", "b"]
    exec_calls.clear()
    call_calls.clear()

    monkeypatch.setattr(cli.os, "name", "nt")
    try:
        cli.main(["foo", "a", "b"])
    except SystemExit:
        pass
    assert len(call_calls) == 1
    assert len(exec_calls) == 0
    assert call_calls[0][0].replace("\\", "/").endswith("venv/Scripts/python.exe")
    assert call_calls[0][1].endswith("odysseus-foo")
    assert call_calls[0][2:] == ["a", "b"]
    call_calls.clear()
    exec_calls.clear()

    monkeypatch.setattr(cli.os, "name", "nt")
    cli.main(["help", "foo"])
    assert len(call_calls) == 1
    assert call_calls[0][0].replace("\\", "/").endswith("venv/Scripts/python.exe")
    assert call_calls[0][1].endswith("odysseus-foo")
    assert call_calls[0][2] == "--help"
