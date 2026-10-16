from __future__ import annotations

import pytest


@pytest.fixture
def file_save_workspace(tmp_path, monkeypatch):
    from jarvis.tools import file_tools, file_undo

    monkeypatch.setattr(file_undo, "WORKSPACE_DIR", tmp_path)
    monkeypatch.setattr(file_tools, "WORKSPACE_DIR", tmp_path)
    monkeypatch.setattr(file_tools, "FILE_SEARCH_ROOTS", [str(tmp_path)])
    file_tools.invalidate_index()
    monkeypatch.setenv("JARVIS_EXEC_APPROVAL", "risky")
    return tmp_path


@pytest.mark.asyncio
async def test_offline_desktop_workflow_returns_only_a_verified_file_artifact(
    file_save_workspace, monkeypatch,
):
    from jarvis.graph.workers import common, desktop
    from jarvis.tools import file_tools

    target = file_save_workspace / "agent-artifact.md"
    content = "verified through the desktop worker"
    monkeypatch.setattr(desktop, "WORKSPACE_DIR", file_save_workspace)
    monkeypatch.setattr(desktop, "_desktop_tools_and_blocked", lambda _surface: ([file_tools.write_file], []))
    monkeypatch.setattr(common, "is_offline", lambda: True)

    result = await desktop.desktop_worker({
        "request": f"Create a file named {target.name} with this content: {content}",
        "surface": "cli_owner",
        "done_actions": {},
    })

    assert target.is_file() and target.read_text(encoding="utf-8") == content
    assert result["artifacts"] == [str(target)]
    assert result["findings"][0]["artifacts"] == [str(target)]
    assert result["execution_required"] is True
    assert result["tool_evidence"][0]["name"] == "write_file"
    assert result["tool_evidence"][0]["ok"] is True
    assert "readback se verify ki" in result["tool_evidence"][0]["output"]


@pytest.mark.asyncio
async def test_desktop_worker_rejects_a_write_file_success_claim_without_matching_disk_bytes(
    file_save_workspace, monkeypatch,
):
    from jarvis.graph.workers import desktop
    from jarvis.graph.workers.common import SubAgentRun
    from jarvis.tools import file_tools

    target = file_save_workspace / "claimed-but-missing.txt"
    content = "expected bytes"
    monkeypatch.setattr(desktop, "WORKSPACE_DIR", file_save_workspace)
    monkeypatch.setattr(desktop, "_desktop_tools_and_blocked", lambda _surface: ([file_tools.write_file], []))

    async def fake_subagent(**_kwargs):
        return SubAgentRun(text="I saved it.", calls=[{
            "name": "write_file", "args": {"path": str(target), "content": content},
            "output": f"✅ File likh di: {target}", "ok": True,
        }])

    monkeypatch.setattr(desktop, "_run_react_subagent", fake_subagent)
    result = await desktop.desktop_worker({
        "request": f"Create a file named {target.name} with this content: {content}",
        "surface": "cli_owner",
        "done_actions": {},
    })

    assert not target.exists()
    assert result["artifacts"] == []
    assert result["tool_evidence"][0]["ok"] is False
    assert "post-write verification failed" in result["tool_evidence"][0]["output"]


@pytest.mark.asyncio
async def test_offline_file_save_without_explicit_content_does_not_create_a_guess(
    file_save_workspace, monkeypatch,
):
    from jarvis.graph.workers import common, desktop
    from jarvis.tools import file_tools

    target = file_save_workspace / "missing-content.txt"
    monkeypatch.setattr(desktop, "WORKSPACE_DIR", file_save_workspace)
    monkeypatch.setattr(desktop, "_desktop_tools_and_blocked", lambda _surface: ([file_tools.write_file], []))
    monkeypatch.setattr(common, "is_offline", lambda: True)

    result = await desktop.desktop_worker({
        "request": f"Create a file named {target.name}",
        "surface": "cli_owner",
        "done_actions": {},
    })

    assert not target.exists()
    assert result["tool_evidence"] == []
    assert result["artifacts"] == []
    assert "VERIFICATION FAILED" in result["findings"][0]["summary"]
