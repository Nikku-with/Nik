from __future__ import annotations

import os
import shutil
import socket
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Set every persisted-data path before test modules import jarvis.config or any tool.
_TEST_ROOT = Path(tempfile.mkdtemp(prefix="nikhilai-pytest-")).resolve()
_TEST_WORKSPACE = _TEST_ROOT / "workspace"
os.environ.update({
    "NIKHILAI_TEST_ROOT": str(_TEST_ROOT),
    "WORKSPACE_DIR": str(_TEST_WORKSPACE),
    "NOTES_DIR": str(_TEST_WORKSPACE / "notes"),
    "MEMORY_DB": str(_TEST_WORKSPACE / "nikhilai_memory.sqlite3"),
    "SESSION_STATE_FILE": str(_TEST_WORKSPACE / "nikhilai_memory.json"),
    "RUNTIME_SETTINGS": str(_TEST_WORKSPACE / "runtime_settings.json"),
    "AUTOMATION_FILE": str(_TEST_WORKSPACE / "automations.json"),
    "APP_MEMORY_FILE": str(_TEST_WORKSPACE / "app_launcher_memory.json"),
    "FILE_SEARCH_ROOTS": str(_TEST_WORKSPACE),
    "BROWSER_PROFILE_DIR": str(_TEST_WORKSPACE / "browser-profile"),
    "BRAIN_PROVIDER": "demo",
    "OFFLINE_MODE": "1",
    "MODEL_DISCOVERY": "0",
})

import pytest


@pytest.fixture(autouse=True)
def forbid_live_network_in_tests(monkeypatch):
    """Fail any test that attempts DNS or a socket-based network request."""
    attempts: list[str] = []

    def denied(*_args, **_kwargs):
        attempts.append("network operation")
        raise OSError("live network is disabled in the test suite; mock search/fetch backends")

    targets = [
        (socket, "create_connection"),
        (socket, "getaddrinfo"),
        (socket.socket, "connect"),
        (socket.socket, "connect_ex"),
        (socket.socket, "sendto"),
    ]
    for owner, name in targets:
        if hasattr(owner, name):
            monkeypatch.setattr(owner, name, denied)

    yield

    assert not attempts, (
        f"{len(attempts)} live network operation(s) attempted; tests must mock web search/fetch"
    )


def pytest_sessionfinish(session, exitstatus):
    # The complete pytest-owned workspace (DB, notes, undo journal, app memory) is disposable.
    shutil.rmtree(_TEST_ROOT, ignore_errors=True)
