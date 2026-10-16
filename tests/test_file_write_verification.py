from __future__ import annotations

import json
from pathlib import Path

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
async def test_write_file_reads_back_exact_saved_text_and_indexes_the_artifact(file_save_workspace):
    from jarvis.tools import file_tools, file_undo

    target = file_save_workspace / "reports" / "verified-save.md"
    content = "# Verified save\n\nKota · workflow checked.\n"

    response = await file_tools.write_file.ainvoke({"path": str(target), "content": content})

    assert "readback se verify ki" in response
    assert str(target) in response
    assert target.is_file() and target.read_text(encoding="utf-8") == content
    indexed = await file_tools._index_items([str(file_save_workspace)], force=True)
    assert any(item["path"] == str(target) and item["type"] == "file" for item in indexed)
    history = json.loads(file_undo._history_path().read_text(encoding="utf-8"))
    assert len(history) == 1 and history[0]["path"] == str(target)

    same_content = await file_tools.write_file.ainvoke({"path": str(target), "content": content})
    assert "same content" in same_content and "readback verified" in same_content
    assert len(json.loads(file_undo._history_path().read_text(encoding="utf-8"))) == 1


@pytest.mark.asyncio
async def test_write_file_readback_mismatch_rolls_back_instead_of_claiming_success(
    file_save_workspace, monkeypatch,
):
    from jarvis.tools import file_tools

    target = file_save_workspace / "bad-readback.txt"
    original_read_text = Path.read_text

    def mismatched_read_text(path, *args, **kwargs):
        actual = original_read_text(path, *args, **kwargs)
        return actual + "\ncorruption" if path == target else actual

    monkeypatch.setattr(Path, "read_text", mismatched_read_text)

    response = await file_tools.write_file.ainvoke({"path": str(target), "content": "expected payload"})

    assert response.startswith("❌ write_file error:")
    assert "verification" in response
    assert not target.exists()
