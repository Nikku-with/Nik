from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def isolated_runtime(monkeypatch, tmp_path):
    from jarvis.config import runtime

    monkeypatch.setattr(runtime, "RUNTIME_SETTINGS_FILE", tmp_path / "runtime-settings.json")
    monkeypatch.setattr(runtime, "_CACHE", dict(runtime.DEFAULTS))
    monkeypatch.setattr(runtime, "_LOADED", True)
    monkeypatch.setattr(runtime, "_FILE_SIGNATURE", runtime._file_signature())
    monkeypatch.setattr(runtime, "_save", lambda: True)
    return runtime


def test_runtime_log_level_accepts_case_insensitive_values(isolated_runtime):
    applied, errors = isolated_runtime.set_many(log_level="debug")

    assert errors == []
    assert applied["log_level"] == "DEBUG"


def test_runtime_rejects_out_of_range_iteration_and_temperature_values(isolated_runtime):
    applied, errors = isolated_runtime.set_many(max_iterations=0, temperature=1.5)

    assert applied == {}
    assert len(errors) == 2
    assert isolated_runtime.get("max_iterations") is None
    assert isolated_runtime.get("temperature") is None


def test_runtime_rejects_fractional_iteration_counts(isolated_runtime):
    applied, errors = isolated_runtime.set_many(max_iterations=1.5)

    assert applied == {}
    assert len(errors) == 1
    assert "integer" in errors[0]


def test_env_default_is_returned_as_a_value_not_looked_up_as_a_name(monkeypatch):
    from jarvis.config import settings

    key = "NIKHILAI_TEST_ENV_DEFAULT_VALUE"
    monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv(f"JARVIS_{key}", raising=False)
    monkeypatch.delenv("fallback-value", raising=False)

    assert settings.env(key, "fallback-value") == "fallback-value"


def test_dotenv_utf8_bom_does_not_hide_the_first_key(tmp_path, monkeypatch):
    from jarvis.config import settings

    key = "NIKHILAI_TEST_BOM_FIRST_KEY"
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"\ufeff{key}=visible\n", encoding="utf-8")
    monkeypatch.setattr(settings, "ROOT_DIR", tmp_path)
    monkeypatch.delenv(key, raising=False)
    monkeypatch.setitem(sys.modules, "dotenv", None)

    settings._load_env_file()

    assert os.environ.get(key) == "visible"


def test_environment_numeric_ranges_fall_back_in_a_fresh_process(monkeypatch):
    env = os.environ.copy()
    env["MAX_ITERATIONS"] = "0"
    env["TEMPERATURE"] = "1.5"
    code = (
        "import json; from jarvis.config import settings as s; "
        "print(json.dumps([s.MAX_GRAPH_ITERATIONS, s.MODEL_ROUTES['brain'].temperature]))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == [3, 0.3]


def test_reload_settings_refreshes_offline_mode_for_all_runtime_consumers(monkeypatch):
    import jarvis.config as config_package
    from jarvis.config import settings
    from jarvis.llm import core

    monkeypatch.setenv("OFFLINE_MODE", "false")
    monkeypatch.setattr(settings, "OFFLINE_MODE", True)
    monkeypatch.setattr(config_package, "OFFLINE_MODE", config_package.OFFLINE_MODE)

    reloaded = settings.reload_settings()

    assert reloaded["offline_mode"] == "false"
    assert settings.OFFLINE_MODE is False
    assert config_package.OFFLINE_MODE is False
    assert core.is_offline() is False
