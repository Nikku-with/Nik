from __future__ import annotations

import json

import pytest

from jarvis.security import gate
from jarvis.security.permissions import use_surface
from jarvis.tools import file_tools, file_undo


@pytest.fixture
def undo_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(file_undo, "WORKSPACE_DIR", tmp_path)
    monkeypatch.setattr(file_tools, "FILE_SEARCH_ROOTS", [str(tmp_path)])
    file_tools.invalidate_index()
    monkeypatch.setenv("JARVIS_EXEC_APPROVAL", "risky")
    monkeypatch.setattr(gate, "BROKER", gate.ApprovalBroker(owner_id="file-owner", ttl_s=0.3))
    return tmp_path


@pytest.mark.asyncio
async def test_write_overwrite_is_undoable_and_restores_previous_bytes(undo_workspace):
    target = undo_workspace / "report.txt"
    target.write_text("original data", encoding="utf-8")

    with use_surface("cli_owner"), gate.use_approval_handler(lambda _request: True):
        written = await file_tools.write_file.ainvoke({"path": str(target), "content": "replacement data"})
        undone = await file_undo.undo_file_operation.ainvoke({})

    assert "File likh di" in written
    assert "Undo ho gaya" in undone
    assert target.read_text(encoding="utf-8") == "original data"
    assert json.loads(file_undo._history_path().read_text(encoding="utf-8")) == []


@pytest.mark.asyncio
async def test_undo_new_file_requires_human_and_refusal_keeps_file_and_history(undo_workspace):
    target = undo_workspace / "new.txt"
    with use_surface("cli_owner"):
        written = await file_tools.write_file.ainvoke({"path": str(target), "content": "new data"})
    assert "File likh di" in written and target.read_text(encoding="utf-8") == "new data"

    with use_surface("cli_owner"), gate.use_approval_handler(lambda _request: False):
        denied = await file_undo.undo_file_operation.ainvoke({})
    assert "Undo nahi hua" in denied
    assert target.read_text(encoding="utf-8") == "new data"
    assert len(json.loads(file_undo._history_path().read_text(encoding="utf-8"))) == 1

    with use_surface("cli_owner"), gate.use_approval_handler(lambda _request: True):
        undone = await file_undo.undo_file_operation.ainvoke({})
    assert "Undo ho gaya" in undone
    assert not target.exists()


@pytest.mark.asyncio
async def test_undo_refuses_to_clobber_a_file_changed_after_the_recorded_operation(undo_workspace):
    target = undo_workspace / "mutable.txt"
    with use_surface("cli_owner"):
        await file_tools.write_file.ainvoke({"path": str(target), "content": "first"})
    target.write_text("later edit", encoding="utf-8")

    with use_surface("cli_owner"):
        result = await file_undo.undo_file_operation.ainvoke({})
    assert "badal chuki hai" in result
    assert target.read_text(encoding="utf-8") == "later edit"
    assert len(json.loads(file_undo._history_path().read_text(encoding="utf-8"))) == 1


@pytest.mark.asyncio
async def test_folder_rename_is_reversible_and_new_name_is_parsed_cleanly(undo_workspace):
    old = undo_workspace / "old-name.txt"
    new = undo_workspace / "new-name.txt"
    old.write_text("rename me", encoding="utf-8")
    file_tools.invalidate_index()

    with use_surface("cli_owner"):
        renamed = await file_tools.folder_file.ainvoke({"command": "old-name.txt ko new-name.txt rename karo"})
    assert "Rename: old-name.txt → new-name.txt" in renamed
    assert new.read_text(encoding="utf-8") == "rename me" and not old.exists()

    with use_surface("cli_owner"), gate.use_approval_handler(lambda _request: True):
        undone = await file_undo.undo_file_operation.ainvoke({})
    assert "Undo ho gaya" in undone
    assert old.read_text(encoding="utf-8") == "rename me" and not new.exists()


@pytest.mark.asyncio
async def test_folder_delete_uses_gate_then_undo_restores_deleted_file(undo_workspace):
    target = undo_workspace / "victim.txt"
    target.write_text("recoverable bytes", encoding="utf-8")
    file_tools.invalidate_index()

    with use_surface("cli_owner"), gate.use_approval_handler(lambda _request: True):
        deleted = await file_tools.folder_file.ainvoke({"command": "delete victim.txt"})
    assert "Deleted" in deleted and "undo_file_operation" in deleted
    assert not target.exists()

    with use_surface("cli_owner"):
        restored = await file_tools.folder_file.ainvoke({"command": "undo last file operation"})
    assert "Undo ho gaya" in restored
    assert target.read_text(encoding="utf-8") == "recoverable bytes"
    assert json.loads(file_undo._history_path().read_text(encoding="utf-8")) == []


def test_file_undo_tool_is_registered_with_write_permission():
    from jarvis.security.permissions import check
    from jarvis.tools import tool_names, tools_for

    assert "undo_file_operation" in tool_names()
    assert "undo_file_operation" in tool_names(tools_for("desktop"))
    assert check("undo_file_operation", "cli_owner").allowed
    assert not check("undo_file_operation", "telegram_guest").allowed


def test_offline_agent_routes_undo_to_registered_tool_and_records_refusal_as_failure():
    import asyncio

    from jarvis.dspy_engine.modules import fallback_route
    from jarvis.graph.nodes import _offline_tool_run, _tool_call_record
    from jarvis.runtime.evidence import is_tool_required

    class FakeUndoTool:
        name = "undo_file_operation"

        def __init__(self):
            self.calls = []

        async def ainvoke(self, args):
            self.calls.append(args)
            return "✅ Undo ho gaya: write — /tmp/example.txt"

    tool = FakeUndoTool()
    output, records = asyncio.run(_offline_tool_run("undo last file operation", [tool]))
    assert fallback_route("undo last file operation") == ["desktop"]
    assert is_tool_required({"request": "undo last file operation", "workers": ["desktop"]})
    assert tool.calls == [{}]
    assert records and records[0]["name"] == "undo_file_operation" and records[0]["ok"] is True
    assert "Undo ho gaya" in output
    assert _tool_call_record("undo_file_operation", {}, "🚫 Undo nahi hua: owner cancelled")["ok"] is False
