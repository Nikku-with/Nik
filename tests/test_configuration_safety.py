from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_importing_settings_does_not_create_checkout_workspace(tmp_path):
    checkout = tmp_path / "checkout"
    settings_dir = checkout / "jarvis" / "config"
    settings_dir.mkdir(parents=True)
    shutil.copy2(ROOT / "jarvis" / "config" / "settings.py", settings_dir / "settings.py")
    home = tmp_path / "home"
    home.mkdir()
    env = os.environ.copy()
    for key in ("WORKSPACE_DIR", "JARVIS_WORKSPACE", "NOTES_DIR", "JARVIS_NOTES_DIR", "MEMORY_DB",
                "JARVIS_MEMORY_DB", "SESSION_STATE_FILE", "JARVIS_SESSION_STATE"):
        env.pop(key, None)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    code = (
        "import json, runpy; "
        f"settings = runpy.run_path({str(settings_dir / 'settings.py')!r}); "
        "print(json.dumps({'workspace': str(settings['WORKSPACE_DIR'])}))"
    )
    proc = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env,
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    configured = Path(json.loads(proc.stdout)["workspace"]).resolve()
    assert configured != (checkout / "workspace").resolve()
    assert not (checkout / "workspace").exists()


def test_pytest_config_paths_are_under_conftest_temp_workspace():
    from jarvis.config import settings

    test_root = Path(os.environ["NIKHILAI_TEST_ROOT"]).resolve()
    workspace = Path(os.environ["WORKSPACE_DIR"]).resolve()
    assert workspace.is_relative_to(test_root)
    assert workspace != (ROOT / "workspace").resolve()
    assert Path(settings.NOTES_DIR).resolve().is_relative_to(workspace)
    assert Path(settings.MEMORY_DB).resolve().is_relative_to(workspace)
    assert Path(settings.SESSION_STATE_FILE).resolve().is_relative_to(workspace)
    assert Path(settings.APP_MEMORY_FILE).resolve().is_relative_to(workspace)
    assert Path(os.environ["MEMORY_DB"]).resolve().is_relative_to(workspace)


def test_google_search_never_uses_a_gemini_only_key(monkeypatch):
    import requests

    from jarvis.tools import web_tools

    configured = {"GEMINI_API_KEY": "gemini-only-key", "SEARCH_ENGINE_ID": "test-cx"}
    monkeypatch.setattr(web_tools, "env", lambda name: configured.get(name))
    calls = []
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: calls.append((args, kwargs)))

    assert web_tools._google_cse_sync("test query") == []
    assert calls == []


@pytest.mark.asyncio
async def test_search_fetch_and_research_tools_use_mocked_backends(monkeypatch):
    from jarvis.tools import web_tools

    calls = {"search": [], "fetch": []}

    async def fake_search(query: str, num: int = 5):
        calls["search"].append((query, num))
        return [{"title": "Mock source", "url": "https://example.test/mock-page",
                 "snippet": "offline search result"}]

    async def fake_fetch(url: str, max_chars: int = 12000, timeout: float = 25.0):
        calls["fetch"].append((url, max_chars))
        return "MOCK_FETCH_CONTENT — no network used"

    monkeypatch.setattr(web_tools, "web_search_results", fake_search)
    monkeypatch.setattr(web_tools, "fetch_url_text", fake_fetch)

    search_result = await web_tools.google_search.ainvoke({"query": "offline test", "num": 1})
    fetch_result = await web_tools.fetch_page.ainvoke({"url": "https://example.test/direct"})
    research_result = await web_tools.deep_research.ainvoke({"query": "offline research", "max_pages": 1})

    assert "Mock source" in search_result
    assert "MOCK_FETCH_CONTENT" in fetch_result
    assert "MOCK_FETCH_CONTENT" in research_result
    assert len(calls["search"]) == 2
    assert len(calls["fetch"]) == 2
