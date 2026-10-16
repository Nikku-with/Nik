from __future__ import annotations

import signal
from types import SimpleNamespace

import pytest


class FakePopen:
    def __init__(self, pid=4242):
        self.pid = pid
        self.returncode = None
        self.kill_calls = 0

    def poll(self):
        return self.returncode

    def kill(self):
        self.kill_calls += 1

    def terminate(self):
        self.returncode = -signal.SIGTERM

    def wait(self, timeout=None):
        self.returncode = 0
        return self.returncode


def _prepare_manager(monkeypatch, tmp_path, popen_factory):
    from jarvis.runtime import process_manager as pm

    monkeypatch.setattr(pm, "WORKSPACE_DIR", tmp_path)
    monkeypatch.setattr(pm, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(pm, "_PROCS", {})
    monkeypatch.setattr(pm.subprocess, "Popen", popen_factory)
    return pm


def test_force_stop_signals_the_process_group_not_only_the_parent(monkeypatch, tmp_path):
    from jarvis.runtime import process_manager as pm

    popen = FakePopen()
    info = pm.ProcInfo(pid="tree", command="python parent.py", popen=popen)
    monkeypatch.setattr(pm, "_PROCS", {"tree": info})
    monkeypatch.setattr(pm, "IS_WINDOWS", False)
    monkeypatch.setattr(pm.os, "getpgid", lambda _pid: 4242)
    received = []
    monkeypatch.setattr(pm.os, "killpg", lambda pgid, sig: received.append((pgid, sig)))

    stopped = pm.stop_all()

    assert stopped == 1
    assert received == [(4242, signal.SIGKILL)]
    assert popen.kill_calls == 0


def test_windows_force_stop_uses_taskkill_tree(monkeypatch):
    from jarvis.runtime import process_manager as pm

    popen = FakePopen()
    info = pm.ProcInfo(pid="windows-tree", command="python parent.py", popen=popen)
    monkeypatch.setattr(pm, "_PROCS", {info.pid: info})
    monkeypatch.setattr(pm, "IS_WINDOWS", True)
    commands = []
    monkeypatch.setattr(
        pm.subprocess,
        "run",
        lambda command, **_kwargs: commands.append(command) or SimpleNamespace(returncode=0),
    )

    assert pm.stop_all() == 1
    assert commands == [["taskkill", "/PID", "4242", "/T", "/F"]]
    assert popen.kill_calls == 0


def test_start_rejects_a_name_already_owned_by_a_live_process(monkeypatch, tmp_path):
    launches = []

    def fake_popen(_command, **_kwargs):
        popen = FakePopen(pid=4242 + len(launches))
        launches.append(popen)
        return popen

    pm = _prepare_manager(monkeypatch, tmp_path, fake_popen)
    pm.start_process("python first.py", name="worker")

    with pytest.raises(ValueError, match="already running"):
        pm.start_process("python second.py", name="worker")

    assert len(launches) == 1


def test_process_lookup_does_not_match_command_substrings(monkeypatch):
    from jarvis.runtime import process_manager as pm

    info = pm.ProcInfo(pid="job-1234", command="python /tmp/worker.py", popen=FakePopen())
    monkeypatch.setattr(pm, "_PROCS", {info.pid: info})

    assert pm.get_process("python") is None
    assert pm.get_process("worker.py") is None
    assert pm.get_process("job-12") is None
    assert pm.get_process("job-1234") is info


def test_parent_log_handle_is_closed_after_start(monkeypatch, tmp_path):
    captured = {}

    def fake_popen(_command, **kwargs):
        captured["stdout"] = kwargs["stdout"]
        return FakePopen()

    pm = _prepare_manager(monkeypatch, tmp_path, fake_popen)
    info = pm.start_process("python worker.py", name="log-close")

    assert info.log_path and info.log_path.exists()
    assert captured["stdout"].closed
