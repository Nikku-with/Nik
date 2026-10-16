from __future__ import annotations

import asyncio

import pytest

from jarvis.security.gate import use_approval_handler


@pytest.mark.asyncio
async def test_write_file_requires_human_before_overwriting_nonempty_file(tmp_path, monkeypatch):
    from jarvis.tools.file_tools import write_file

    target = tmp_path / "notes.txt"
    target.write_text("original", encoding="utf-8")
    monkeypatch.setenv("JARVIS_EXEC_APPROVAL", "risky")
    with use_approval_handler(lambda _req: False):
        response = await write_file.ainvoke({"path": str(target), "content": "replacement"})
    assert "overwrite nahi ki" in response
    assert target.read_text(encoding="utf-8") == "original"


@pytest.mark.asyncio
async def test_write_file_overwrites_only_after_owner_yes(tmp_path, monkeypatch):
    from jarvis.tools.file_tools import write_file

    target = tmp_path / "notes.txt"
    target.write_text("original", encoding="utf-8")
    monkeypatch.setenv("JARVIS_EXEC_APPROVAL", "risky")
    with use_approval_handler(lambda req: req.command.endswith(str(target))):
        response = await write_file.ainvoke({"path": str(target), "content": "replacement"})
    assert "File likh di" in response
    assert target.read_text(encoding="utf-8") == "replacement"


@pytest.mark.asyncio
async def test_folder_file_delete_cancel_does_not_remove_target(tmp_path, monkeypatch):
    from jarvis.tools import file_tools

    target = tmp_path / "keep.txt"
    target.write_text("keep", encoding="utf-8")
    stat = target.stat()

    async def fake_index(*_args, **_kwargs):
        return [{"name": target.name, "path": str(target), "type": "file",
                 "mtime": stat.st_mtime, "size": stat.st_size, "ext": ".txt"}]

    monkeypatch.setattr(file_tools, "_index_items", fake_index)
    monkeypatch.setenv("JARVIS_EXEC_APPROVAL", "risky")
    with use_approval_handler(lambda _req: False):
        response = await file_tools.folder_file.ainvoke({"command": "delete keep.txt"})
    assert "Delete nahi kiya" in response
    assert target.read_text(encoding="utf-8") == "keep"


@pytest.mark.asyncio
async def test_code_engine_shellout_is_gated_before_subprocess(tmp_path, monkeypatch):
    from jarvis.tools.code_engine import run_script

    script = tmp_path / "danger.py"
    script.write_text("import subprocess\nsubprocess.run(['rm', '-rf', '/tmp/nope'])\n", encoding="utf-8")
    monkeypatch.setenv("JARVIS_EXEC_APPROVAL", "risky")
    invoked = False

    async def fake_exec(*_args, **_kwargs):
        nonlocal invoked
        invoked = True
        raise AssertionError("denied code was run")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    with use_approval_handler(lambda _req: False):
        result = await run_script(script, cwd=tmp_path)
    assert not result.ok
    assert "cancelled by Gate" in result.stderr
    assert not invoked


@pytest.mark.asyncio
async def test_process_termination_requires_gate_before_stop(monkeypatch):
    from jarvis.runtime import process_manager as pm

    class FakeInfo:
        pid = "job-1"
        command = "python server.py"
        cwd = "/workspace"
        alive = True

    stopped = []
    monkeypatch.setattr(pm, "get_process", lambda _pid: FakeInfo())
    monkeypatch.setattr(pm, "stop_process", lambda pid, force=False: stopped.append((pid, force)) or "stopped")
    monkeypatch.setenv("JARVIS_EXEC_APPROVAL", "risky")
    tool = next(tool for tool in pm.PROCESS_TOOLS if tool.name == "process_stop")
    with use_approval_handler(lambda _req: False):
        response = await tool.ainvoke({"pid": "job-1"})
    assert "nahi roka" in response
    assert stopped == []


@pytest.mark.asyncio
async def test_restart_requires_gate_even_in_library_owner_process(monkeypatch):
    from jarvis.tools import system_ops

    monkeypatch.setenv("NIKHILAI_ALLOW_RESTART", "1")
    monkeypatch.setenv("NIKHILAI_MAIN_PROCESS", "1")
    monkeypatch.setenv("JARVIS_EXEC_APPROVAL", "risky")
    calls = []
    monkeypatch.setattr(system_ops.os, "execv", lambda *args: calls.append(args))
    with use_approval_handler(lambda _req: False):
        denied = await system_ops.restart_self()
    assert not denied["ok"]
    assert calls == []
    with use_approval_handler(lambda _req: True):
        allowed = await system_ops.restart_self()
    assert allowed["ok"]
    assert len(calls) == 1
