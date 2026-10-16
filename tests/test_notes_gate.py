from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_save_note_new_file_is_safe_but_overwrite_requires_owner_gate(monkeypatch, tmp_path):
    from jarvis.security import gate
    from jarvis.security.permissions import use_surface
    from jarvis.tools import notes_tools

    monkeypatch.setattr(notes_tools, "NOTES_DIR", tmp_path)
    monkeypatch.setattr(notes_tools, "get_memory", lambda: SimpleNamespace(index_note=lambda **_kwargs: None))

    async def forbidden_gate(*_args, **_kwargs):
        raise AssertionError("creating a new note should not prompt")

    monkeypatch.setattr(gate, "authorize_command", forbidden_gate)
    with use_surface("cli_owner"):
        created = await notes_tools.save_note.ainvoke({"name": "fresh", "content": "first"})
    assert "save ho gaya" in created.lower()
    path = tmp_path / "fresh.md"
    assert path.exists() and "first" in path.read_text(encoding="utf-8")

    calls = []

    async def deny_overwrite(command, cwd, **kwargs):
        calls.append((command, cwd, kwargs))
        return SimpleNamespace(allowed=False, reason="owner denied")

    monkeypatch.setattr(gate, "authorize_command", deny_overwrite)
    before = path.read_text(encoding="utf-8")
    with use_surface("cli_owner"):
        denied = await notes_tools.save_note.ainvoke({"name": "fresh", "content": "replacement"})
    assert "owner denied" in denied
    assert path.read_text(encoding="utf-8") == before
    assert len(calls) == 1 and calls[0][2]["force"] is True
