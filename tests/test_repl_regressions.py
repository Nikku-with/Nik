from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor


def test_repl_keeps_variables_across_subprocess_runs(tmp_path):
    from jarvis.runtime.repl import PythonREPL

    repl = PythonREPL(tmp_path / "session.json")

    first = repl.run("values = [10, 20]")
    second = repl.run("values.append(30)\nsum(values)")

    assert first["ok"] is True
    assert second["ok"] is True
    assert "60" in second["stdout"]


def test_repl_returns_child_traceback_on_runtime_error(tmp_path):
    from jarvis.runtime.repl import PythonREPL

    result = PythonREPL(tmp_path / "session.json").run("1 / 0")

    assert result["ok"] is False
    assert "ZeroDivisionError" in result["stderr"]
    assert result["error"]


def test_repl_replays_imports_and_definitions_between_child_runs(tmp_path):
    from jarvis.runtime.repl import PythonREPL

    repl = PythonREPL(tmp_path / "session.json")

    imported = repl.run("import math")
    square_root = repl.run("math.sqrt(81)")
    defined = repl.run("def triple(value):\n    return value * 3")
    tripled = repl.run("triple(7)")

    assert imported["ok"] and square_root["ok"]
    assert "9.0" in square_root["stdout"]
    assert defined["ok"] and tripled["ok"]
    assert "21" in tripled["stdout"]


def test_repl_registry_isolated_by_session_state_user(monkeypatch):
    from jarvis.memory.session_state import use_state_user
    from jarvis.runtime import repl

    monkeypatch.setattr(repl, "_REGISTRY", {})
    with use_state_user("repl-regression-user-one"):
        first = repl.get_repl()
    with use_state_user("repl-regression-user-two"):
        second = repl.get_repl()

    assert first is not second
    assert first.state_file != second.state_file


def test_repl_rejects_syntax_errors_before_starting_execution():
    from jarvis.runtime.repl import PythonREPL, safety_check

    assert safety_check("if True print('missing colon')")[0] is False
    result = PythonREPL().run("if True print('missing colon')")
    assert result["ok"] is False
    assert "SyntaxError" in result["stderr"]


def test_concurrent_repl_runs_serialize_namespace_updates(tmp_path):
    from jarvis.runtime.repl import PythonREPL

    repl = PythonREPL(tmp_path / "session.json")
    repl.run("values = []")
    barrier = threading.Barrier(2)

    def append_value(value):
        barrier.wait()
        return repl.run(f"values.append({value})")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(append_value, (1, 2)))

    final = repl.run("sorted(values)")
    assert all(result["ok"] for result in results)
    assert "[1, 2]" in final["stdout"]


def test_repl_timeout_does_not_leave_a_running_execution_thread(tmp_path):
    from jarvis.runtime.repl import PythonREPL

    repl = PythonREPL(tmp_path / "session.json")
    result = repl.run("import time; time.sleep(0.25)", timeout=0.01)

    assert result["ok"] is False
    assert not any(thread.name == "jarvis-repl" and thread.is_alive() for thread in threading.enumerate())


def test_repl_state_uses_atomic_session_state_writer(tmp_path, monkeypatch):
    from jarvis.memory import session_state
    from jarvis.runtime.repl import PythonREPL

    state_file = tmp_path / "session.json"
    state_file.write_text(json.dumps({"notes": "keep this", "repl": {"variables": {}, "history": []}}),
                          encoding="utf-8")
    replaced = []
    real_replace = session_state.os.replace

    def inspect_and_replace(source, destination):
        payload = json.loads(session_state.Path(source).read_text(encoding="utf-8"))
        assert payload["notes"] == "keep this"
        assert payload["repl"]["variables"]["answer"] == 42
        replaced.append(destination)
        return real_replace(source, destination)

    monkeypatch.setattr(session_state.os, "replace", inspect_and_replace)
    result = PythonREPL(state_file).run("answer = 42")

    assert result["ok"] is True
    assert replaced == [state_file]
    assert json.loads(state_file.read_text(encoding="utf-8"))["notes"] == "keep this"


def test_repl_surfaces_corrupt_session_state_without_overwriting_it(tmp_path):
    from jarvis.runtime.repl import PythonREPL

    state_file = tmp_path / "session.json"
    state_file.write_text("{broken", encoding="utf-8")
    repl = PythonREPL(state_file)

    result = repl.run("answer = 42")

    assert result["ok"] is True
    assert "session state unreadable" in result["state_error"]
    assert state_file.read_text(encoding="utf-8") == "{broken"
