from __future__ import annotations

import asyncio
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_code_engine_selects_the_requested_javascript_runtime(tmp_path, monkeypatch):
    from jarvis.tools import code_engine

    script = tmp_path / "hello.js"
    script.write_text("console.log('hello')", encoding="utf-8")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    monkeypatch.setattr(code_engine, "ARTIFACTS_DIR", artifacts)
    monkeypatch.setattr(code_engine.shutil, "which", lambda name: "/fake/bin/node" if name == "node" else None)
    monkeypatch.setenv("JARVIS_EXEC_APPROVAL", "risky")
    commands = []

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"hello\n", b""

    async def fake_exec(*command, **_kwargs):
        commands.append(command)
        return FakeProcess()

    monkeypatch.setattr(code_engine.asyncio, "create_subprocess_exec", fake_exec)

    result = await code_engine.run_script(script, cwd=tmp_path, language="javascript")

    assert result.ok is True
    assert commands[0][:2] == ("/fake/bin/node", str(script))


@pytest.mark.asyncio
async def test_code_engine_save_as_treats_input_as_a_script_name(tmp_path, monkeypatch):
    from jarvis.tools import code_engine

    scripts = tmp_path / "scripts"
    scripts.mkdir()
    monkeypatch.setattr(code_engine, "SCRIPTS_DIR", scripts)

    async def fake_generate(*_args, **_kwargs):
        return "print('ok')"

    monkeypatch.setattr(code_engine, "generate_code", fake_generate)

    result = await code_engine.code_engine_run(
        task="make a script", language="python", run=False, save_as="../escaped"
    )

    saved_path = Path(result["script"]).resolve()
    assert result["ok"] is True
    assert saved_path.parent == scripts.resolve()
    assert saved_path.name == "escaped.py"


@pytest.mark.asyncio
async def test_run_python_gives_concurrent_snippets_unique_names(tmp_path, monkeypatch):
    from jarvis.tools import code_engine

    monkeypatch.setattr(code_engine, "SCRIPTS_DIR", tmp_path)
    monkeypatch.setattr(code_engine.time, "strftime", lambda *_args: "120000")
    paths = []

    async def fake_run(path, **_kwargs):
        paths.append(Path(path))
        await asyncio.sleep(0)
        return code_engine.RunResult(ok=True, stdout="done")

    monkeypatch.setattr(code_engine, "run_script", fake_run)

    await asyncio.gather(
        code_engine.run_python.ainvoke({"code": "print('first')"}),
        code_engine.run_python.ainvoke({"code": "print('second')"}),
    )

    assert len(paths) == 2
    assert len({path.name for path in paths}) == 2
    assert all(path.exists() for path in paths)


@pytest.mark.asyncio
async def test_javascript_shell_out_is_gated_before_subprocess(tmp_path, monkeypatch):
    from jarvis.security.gate import use_approval_handler
    from jarvis.tools import code_engine

    script = tmp_path / "danger.js"
    script.write_text(
        "const { execSync } = require('child_process');\n"
        "execSync('rm -fr /tmp/nope');\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("JARVIS_EXEC_APPROVAL", "risky")
    monkeypatch.setattr(code_engine.shutil, "which", lambda name: "/fake/bin/node" if name == "node" else None)
    invoked = False

    async def fake_exec(*_args, **_kwargs):
        nonlocal invoked
        invoked = True
        raise AssertionError("denied JavaScript must not run")

    monkeypatch.setattr(code_engine.asyncio, "create_subprocess_exec", fake_exec)
    with use_approval_handler(lambda _request: False):
        result = await code_engine.run_script(script, cwd=tmp_path, language="javascript")

    assert result.ok is False
    assert "cancelled by Gate" in result.stderr
    assert invoked is False
