from __future__ import annotations


class FakePopen:
    pid = 4242

    def poll(self):
        return None



def test_start_process_uses_windows_process_group_flag(tmp_path, monkeypatch):
    from jarvis.runtime import process_manager as pm

    captured = {}
    monkeypatch.setattr(pm, "IS_WINDOWS", True)
    monkeypatch.setattr(pm, "WORKSPACE_DIR", tmp_path)
    monkeypatch.setattr(pm, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(pm, "_PROCS", {})
    monkeypatch.setattr(pm.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, raising=False)

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return FakePopen()

    monkeypatch.setattr(pm.subprocess, "Popen", fake_popen)
    result = pm.start_process("echo hello", name="windows-test")

    assert result.pid == "windows-test"
    assert captured["command"] == "echo hello"
    assert captured["creationflags"] == 512
    assert "start_new_session" not in captured
    assert result.log_path and result.log_path.exists()
