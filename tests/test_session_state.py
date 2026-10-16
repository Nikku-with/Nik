from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def test_session_state_uses_atomic_replace_and_reports_corrupt_json(tmp_path, monkeypatch, caplog):
    from jarvis.memory import session_state as state_module

    path = tmp_path / "session.json"
    path.write_text("{broken", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger=state_module.__name__):
        state = state_module.SessionState(path)
    assert "session state unreadable" in state.load_error
    assert "JSONDecodeError" in state.load_error
    assert any("could not be loaded" in record.message for record in caplog.records)

    real_replace = state_module.os.replace
    replaced = []

    def check_complete_temp_file(source, destination):
        source_path = Path(source)
        destination_path = Path(destination)
        assert source_path.parent == destination_path.parent
        assert source_path.suffix == ".tmp"
        assert json.loads(source_path.read_text(encoding="utf-8"))["notes"] == "recovered"
        replaced.append(destination_path)
        return real_replace(source, destination)

    monkeypatch.setattr(state_module.os, "replace", check_complete_temp_file)
    state.set("notes", "recovered")
    assert replaced == [path]
    assert state.load_error == ""
    assert json.loads(path.read_text(encoding="utf-8"))["notes"] == "recovered"
    assert not list(tmp_path.glob(".*.tmp"))


def test_session_state_concurrent_mutations_remain_valid_and_bounded(tmp_path):
    from jarvis.memory.session_state import MAX_HISTORY, SessionState

    state = SessionState(tmp_path / "concurrent.json")

    def add_tasks(worker: int):
        for index in range(20):
            state.add_task(f"worker-{worker}-{index}")

    threads = [threading.Thread(target=add_tasks, args=(worker,)) for worker in range(5)]
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(thread.start) for thread in threads]
        for future in futures:
            future.result()
        for thread in threads:
            thread.join()

    persisted = json.loads(state.path.read_text(encoding="utf-8"))
    assert len(persisted["task_history"]) == MAX_HISTORY
    assert len({item["goal"] for item in persisted["task_history"]}) == MAX_HISTORY
    assert state.load_error == ""


def test_doctor_surfaces_session_state_corruption(monkeypatch):
    from types import SimpleNamespace

    from jarvis.cli import doctor
    from jarvis.memory import session_state

    monkeypatch.setattr(session_state, "get_state", lambda: SimpleNamespace(load_error="session state unreadable: bad JSON"))
    rows = {label: (status, detail) for status, label, detail in doctor.collect_checks()}
    assert rows["Session state"][0] == "warn"
    assert "unreadable" in rows["Session state"][1]
