"""Run every acceptance harness under pytest in an isolated offline workspace."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STANDALONE_HARNESSES = (
    "test_offline.py",
    "test_v23.py",
    "test_ui_progress.py",
    "test_tool_invariant.py",
    "test_cli_routing.py",
    "test_voice_cli.py",
    "test_llm_bind.py",
)
_RESULT_RX = re.compile(r"RESULT:\s*(\d+)/(\d+)\s+checks passed")
_SECRET_ENV_KEYS = (
    "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "GROQ_API_KEY",
    "ANTHROPIC_API_KEY", "XAI_API_KEY", "TELEGRAM_BOT_TOKEN", "LIVEKIT_URL", "LIVEKIT_API_KEY",
    "LIVEKIT_API_SECRET",
)


def _offline_env(root: Path) -> dict[str, str]:
    workspace = root / "workspace"
    home = root / "home"
    values = {
        "BRAIN_PROVIDER": "auto",
        "PROVIDER": "demo",
        "OFFLINE_MODE": "1",
        "JARVIS_OFFLINE_MODE": "1",
        "MODEL_DISCOVERY": "false",
        "NIKHILAI_TEST": "1",
        "NIKHILAI_MAIN_PROCESS": "0",
        "WORKSPACE_DIR": str(workspace),
        "NOTES_DIR": str(workspace / "notes"),
        "MEMORY_DB": str(workspace / "nikhilai_memory.sqlite3"),
        "SESSION_STATE_FILE": str(workspace / "nikhilai_memory.json"),
        "RUNTIME_SETTINGS": str(workspace / "runtime_settings.json"),
        "AUTOMATION_FILE": str(workspace / "automations.json"),
        "APP_MEMORY_FILE": str(workspace / "app_launcher_memory.json"),
        "FILE_SEARCH_ROOTS": str(workspace),
        "BROWSER_PROFILE_DIR": str(workspace / "browser-profile"),
        "JARVIS_EXEC_APPROVAL": "risky",
        "TELEGRAM_BOT_TOKEN": "",
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
    }
    for key in _SECRET_ENV_KEYS:
        values[key] = ""
    existing_pythonpath = os.environ.get("PYTHONPATH", "")
    values["PYTHONPATH"] = str(ROOT) + (os.pathsep + existing_pythonpath if existing_pythonpath else "")
    return {**os.environ, **values}


@pytest.mark.parametrize("script", STANDALONE_HARNESSES, ids=lambda name: name.removeprefix("test_").removesuffix(".py"))
def test_standalone_acceptance_harness_passes_offline(script: str) -> None:
    with tempfile.TemporaryDirectory(prefix="nikhilai-harness-") as temporary_root:
        root = Path(temporary_root)
        for directory in (root / "home", root / "workspace"):
            directory.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [sys.executable, str(ROOT / "tests" / script)],
            cwd=ROOT,
            env=_offline_env(root),
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )

    output = result.stdout + "\n" + result.stderr
    summaries = _RESULT_RX.findall(output)
    diagnostic = output[-10000:]
    assert result.returncode == 0, f"{script} exited {result.returncode}:\n{diagnostic}"
    assert summaries, f"{script} did not print a checks-passed summary:\n{diagnostic}"
    assert all(int(passed) == int(total) for passed, total in summaries), (
        f"{script} reported a failed acceptance check:\n{diagnostic}"
    )
