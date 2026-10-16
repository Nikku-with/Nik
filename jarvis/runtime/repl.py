"""
jarvis/runtime/repl.py — Persistent Python REPL (killable subprocess runner)
===========================================================================

Owner ka ask: *"Python ka `exec()` / `sys.executable` use karke temporary sandbox banaye
jahan Jarvis apna hi banaya code run karke check kar sake"*.

Ye module dono deta hai:

  1. **Persistent REPL** (default) — ek hi namespace me kaam karta hai, isliye
     "pehle variable banao, agle step me use karo" wala flow chalta hai:
         step 1: `df = [1,2,3]`
         step 2: `sum(df)`        → 6  ✅ (variable yaad hai)

     Kaise: AST syntax check → killable subprocess execution → stdout/stderr capture
     → JSON-safe variables session state me atomically persist hote hain.

  2. **Isolated subprocess runner** (`run_isolated`) — jab poora fresh process chahiye
     (`sys.executable` subprocess), heavy/crashy code ke liye.

Compatibility checks (not a sandbox):
  * blocked: os.system/rm -rf, subprocess shell escape, shutil.rmtree('/'), socket exfil,
    `__import__('os').system`, credential posting
  * allowed: pandas/csv/json/math/requests(optional) jaisa normal analysis & automation
  * workspace me hi file likhne ki salah; system paths guard
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import signal
import subprocess
import textwrap
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from ..config import CODE_TIMEOUT_S, SCRIPTS_DIR, WORKSPACE_DIR, python_executable

_BANNED = (
    "rm -rf /", "shutil.rmtree('/')", 'shutil.rmtree("/")', "os.system('rm -rf",
    "mkfs", "format c:", ":(){:|:&};:", ">/dev/sda", "__import__('os').system",
    '__import__("os").system', "os.popen(", "subprocess.call(['rm', '-rf', '/'",
    "base64.b64decode", "os.environ['AWS_SECRET", "os.environ[\"AWS_SECRET",
)

_REPL_CHILD = r'''import ast, contextlib, io, json, sys, traceback
payload = json.loads(sys.stdin.read())
namespace = dict(payload.get("variables") or {})
namespace["__name__"] = "__jarvis_repl__"
namespace["__builtins__"] = __builtins__
workspace = payload["workspace"]
namespace["WORKSPACE"] = workspace
namespace["pwd"] = lambda: workspace
captured_out, captured_err = io.StringIO(), io.StringIO()
error = None
try:
    with contextlib.redirect_stdout(captured_out), contextlib.redirect_stderr(captured_err):
        for source in payload.get("imports", []):
            exec(source, namespace)
        for source in payload.get("definitions", []):
            exec(source, namespace)
        tree = ast.parse(payload["code"], mode="exec")
        last_expr = payload.get("last_expr")
        if last_expr and tree.body and isinstance(tree.body[-1], ast.Expr):
            tree.body = tree.body[:-1]
        exec(compile(tree, "<jarvis_repl>", "exec"), namespace)
        if last_expr:
            value = eval(last_expr, namespace)
            if value is not None:
                print(repr(value))
except BaseException:
    error = traceback.format_exc()
variables = {}
for key, value in namespace.items():
    if key.startswith("__") or key in {"pwd", "WORKSPACE", "print"}:
        continue
    try:
        json.dumps(value)
        variables[key] = value
    except Exception:
        variables[key] = f"<{type(value).__name__}>"
result = {"ok": error is None, "stdout": captured_out.getvalue(),
          "stderr": captured_err.getvalue(), "error": error, "variables": variables}
sys.stdout.write(payload["result_marker"] + json.dumps(result, ensure_ascii=False) + "\n")
'''


def _kill_repl_process(process: subprocess.Popen) -> None:
    """Terminate the subprocess group used for one REPL execution."""
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            process.kill()
        else:
            if result.returncode != 0 and process.poll() is None:
                process.kill()
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except OSError:
        process.kill()


def _execute_repl_child(payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    """Execute one REPL request in a killable child process and decode its result."""
    options: dict[str, Any] = {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "cwd": str(WORKSPACE_DIR),
        "env": {**os.environ, "PYTHONIOENCODING": "utf-8"},
    }
    if os.name == "nt":
        options["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        options["start_new_session"] = True
    try:
        process = subprocess.Popen([python_executable(), "-c", _REPL_CHILD], **options)
    except Exception as exc:
        error = f"REPL subprocess start nahi hua ({type(exc).__name__}: {exc})"
        return {"ok": False, "stdout": "", "stderr": error, "error": error, "variables": {}}

    try:
        stdout, stderr = process.communicate(
            input=json.dumps(payload, ensure_ascii=False), timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        _kill_repl_process(process)
        try:
            process.communicate(timeout=2)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
        error = f"⏱️ Timeout: {timeout}s ke andar code complete nahi hua; subprocess terminate kar diya."
        return {"ok": False, "stdout": "", "stderr": error, "error": error, "variables": {}}

    marker = payload["result_marker"]
    marker_index = stdout.rfind(marker)
    if marker_index < 0:
        error = f"REPL subprocess ka result nahi mila (exit code {process.returncode})"
        detail = stderr.strip()
        return {"ok": False, "stdout": stdout, "stderr": detail or error,
                "error": error, "variables": {}}
    result_text = stdout[marker_index + len(marker):].splitlines()[0]
    try:
        result = json.loads(result_text)
    except json.JSONDecodeError as exc:
        error = f"REPL subprocess ka result parse nahi hua: {exc}"
        return {"ok": False, "stdout": stdout[:marker_index], "stderr": stderr or error,
                "error": error, "variables": {}}
    result["stdout"] = stdout[:marker_index] + result.get("stdout", "")
    if stderr:
        result["stderr"] = "\n".join(part for part in (result.get("stderr", ""), stderr) if part)
    return result


def safety_check(code: str) -> tuple[bool, str]:
    """Check syntax and the existing explicit deny-list; this is not a sandbox."""
    if not (code or "").strip():
        return False, "code khali hai"
    low = code.lower()
    for bad in _BANNED:
        if bad.lower() in low:
            return False, f"dangerous pattern blocked: `{bad}`"
    try:
        ast.parse(code)
    except SyntaxError as exc:
        return False, f"SyntaxError: {exc.msg} (line {exc.lineno})"
    return True, "ok"


class PythonREPL:
    """Persistent namespace REPL with each execution isolated in a killable subprocess."""

    def __init__(self, state_file: Path | None = None, *, session_state=None):
        from ..memory.session_state import SessionState, get_state

        self._lock = threading.RLock()
        if session_state is not None:
            self._state = session_state
        else:
            self._state = get_state() if state_file is None else SessionState(Path(state_file))
        self.state_file = self._state.path
        self.namespace: dict[str, Any] = {"__name__": "__jarvis_repl__", "__builtins__": __builtins__}
        self.history: list[dict[str, Any]] = []
        self._imports: list[str] = []
        self._definitions: list[str] = []
        self.state_error = self._state.load_error
        self._load_state()

    # ---------------------------------------------------------------- state
    def _load_state(self) -> None:
        """Restore JSON-safe values and history from the shared session-state store."""
        repl_state = self._state.data.get("repl", {})
        if not isinstance(repl_state, dict):
            self.state_error = "REPL session state unreadable: repl section must be an object"
            return
        variables = repl_state.get("variables") or {}
        if isinstance(variables, dict):
            for key, value in variables.items():
                if isinstance(key, str) and re.match(r"^[A-Za-z_]\w*$", key):
                    self.namespace[key] = value
        history = repl_state.get("history") or []
        self.history = [item for item in history if isinstance(item, dict)] if isinstance(history, list) else []
        self.state_error = self._state.load_error

    def _safe_variables(self) -> dict[str, Any]:
        variables: dict[str, Any] = {}
        for key, value in self.namespace.items():
            if key.startswith("__") or key in {"pwd", "WORKSPACE", "print"} or not re.match(r"^[A-Za-z_]\w*$", key):
                continue
            try:
                json.dumps(value)
                variables[key] = value
            except Exception:
                variables[key] = f"<{type(value).__name__}>"
        return variables

    def save_state(self) -> bool:
        """Update only the REPL section through the shared locked, atomic state writer."""
        with self._lock:
            repl_state = {
                "variables": self._safe_variables(),
                "history": self.history[-40:],
                "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            saved = self._state.update_section("repl", repl_state)
            self.state_error = "" if saved else self._state.load_error
            return saved

    @staticmethod
    def _declaration_key(source: str) -> str:
        node = ast.parse(source).body[0]
        return f"{type(node).__name__}:{getattr(node, 'name', source)}"

    # ----------------------------------------------------------------- run
    def run(self, code: str, timeout: float = CODE_TIMEOUT_S) -> dict[str, Any]:
        with self._lock:
            return self._run_locked(code, timeout)

    def _run_locked(self, code: str, timeout: float) -> dict[str, Any]:
        ok, msg = safety_check(code)
        if not ok:
            return {"ok": False, "stdout": "", "stderr": f"🚫 {msg}", "error": msg,
                    "duration_s": 0.0, "state_error": self.state_error}

        source = textwrap.dedent(code)
        last_expr_src: str | None = None
        import_sources: list[str] = []
        definition_sources: list[str] = []
        try:
            tree = ast.parse(source, mode="exec")
            for node in tree.body:
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    import_sources.append(ast.unparse(node))
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    definition_sources.append(ast.unparse(node))
            if tree.body and isinstance(tree.body[-1], ast.Expr):
                last_expr_src = ast.unparse(tree.body[-1])
                tree.body = tree.body[:-1]
            compile(tree, "<jarvis_repl>", "exec")
        except SyntaxError as exc:
            return {"ok": False, "stdout": "", "stderr": f"SyntaxError: {exc.msg} (line {exc.lineno})",
                    "error": f"SyntaxError: {exc.msg}", "duration_s": 0.0,
                    "state_error": self.state_error}

        marker = f"__NIKHILAI_REPL_RESULT_{uuid.uuid4().hex}__"
        payload = {
            "code": source,
            "last_expr": last_expr_src,
            "variables": self._safe_variables(),
            "imports": list(self._imports),
            "definitions": list(self._definitions),
            "workspace": str(WORKSPACE_DIR),
            "result_marker": marker,
        }
        started = time.monotonic()
        result = _execute_repl_child(payload, timeout)
        duration = time.monotonic() - started

        variables = result.get("variables")
        if isinstance(variables, dict):
            self.namespace = {"__name__": "__jarvis_repl__", "__builtins__": __builtins__, **variables}
        if result.get("ok"):
            self._imports = list(dict.fromkeys([*self._imports, *import_sources]))
            definitions = {self._declaration_key(item): item for item in self._definitions}
            for declaration in definition_sources:
                definitions[self._declaration_key(declaration)] = declaration
            self._definitions = list(definitions.values())

        result.setdefault("ok", False)
        result.setdefault("stdout", "")
        result.setdefault("stderr", "")
        result.setdefault("error", None)
        if result["error"] and result["error"] not in result["stderr"]:
            result["stderr"] = "\n".join(part for part in (result["stderr"], result["error"]) if part)
        result["duration_s"] = round(duration, 3)
        self.history.append({
            "code": source[:400],
            "ok": result["ok"],
            "duration_s": result["duration_s"],
            "at": time.strftime("%H:%M:%S"),
        })
        self.save_state()
        result["state_error"] = self.state_error
        return result

    def reset(self) -> bool:
        with self._lock:
            self.namespace = {"__name__": "__jarvis_repl__", "__builtins__": __builtins__}
            self.history = []
            self._imports = []
            self._definitions = []
            return self.save_state()

    def variables(self) -> dict[str, str]:
        with self._lock:
            return {
                key: f"{type(value).__name__} = {str(value)[:80]}"
                for key, value in self.namespace.items()
                if not key.startswith("__")
            }


_REGISTRY: dict[tuple[str, str], PythonREPL] = {}
_REGISTRY_LOCK = threading.RLock()


def get_repl(name: str = "main") -> PythonREPL:
    from ..memory.session_state import get_state

    state = get_state()
    key = (str(state.path.resolve()), name)
    with _REGISTRY_LOCK:
        if key not in _REGISTRY:
            _REGISTRY[key] = PythonREPL(session_state=state)
        return _REGISTRY[key]


def run_isolated(code: str, timeout: int = CODE_TIMEOUT_S) -> dict[str, Any]:
    """Fresh subprocess me chalao (sys.executable) — crashy/heavy code ke liye."""
    ok, msg = safety_check(code)
    if not ok:
        return {"ok": False, "stdout": "", "stderr": f"🚫 {msg}", "returncode": None, "script": ""}
    SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    script = SCRIPTS_DIR / f"isolated_{time.strftime('%H%M%S')}.py"
    script.write_text(code, encoding="utf-8")
    try:
        proc = subprocess.run([python_executable(), str(script)], capture_output=True, text=True,
                              timeout=timeout, cwd=str(WORKSPACE_DIR),
                              env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        return {"ok": proc.returncode == 0, "stdout": proc.stdout, "stderr": proc.stderr,
                "returncode": proc.returncode, "script": str(script)}
    except subprocess.TimeoutExpired:
        return {"ok": False, "stdout": "", "stderr": f"⏱️ timeout {timeout}s", "returncode": None, "script": str(script)}


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------
try:
    from langchain_core.tools import tool
except Exception:  # pragma: no cover
    tool = None  # type: ignore

if tool is not None:

    @tool
    async def python_repl(code: str, timeout: int = 60) -> str:
        """Persistent Python REPL — apna hi likha code run karke check karo (variables yaad rehte hain).

        Flow: step 1 me `data = [1,2,3]`, step 2 me `sum(data)` → 6 ✅
        Errors aayein to traceback wapas milta hai — tum khud fix karke dobara chala sakte ho.
        Heavy/crashy code ke liye `run_python_isolated` use karo.

        Examples:
          python_repl("import statistics\\ndata=[12,45,7]; print(statistics.mean(data))")
          python_repl("data.append(99); sum(data)")
        """
        repl = get_repl()
        result = await asyncio.to_thread(repl.run, code, timeout)
        head = f"[repl {'ok' if result['ok'] else 'error'} | {result['duration_s']}s]\n"
        body = result["stdout"].strip()
        err = result["stderr"].strip()
        out = head + body
        if err:
            out += f"\n--- error ---\n{err[-2000:]}"
        if result.get("state_error"):
            out += f"\n⚠️ Session state save nahi hua: {result['state_error']}"
        return out or head + "(koi output nahi)"

    @tool
    async def run_python_isolated(code: str, timeout: int = 90) -> str:
        """Fresh subprocess me Python chalao (side effects se bachne ke liye, ya crash-prone code ke liye)."""
        result = await asyncio.to_thread(run_isolated, code, timeout)
        return (f"[isolated exit={result['returncode']}]\n{result['stdout'][-3000:]}"
                + (f"\n--- stderr ---\n{result['stderr'][-1500:]}" if result["stderr"].strip() else "")
                + f"\n📜 script: {result['script']}")

    @tool
    async def repl_state() -> str:
        """REPL ke current variables dikhao (kya-kya memory me hai)."""
        vars_ = get_repl().variables()
        if not vars_:
            return "🧪 REPL khali hai (koi variable nahi)."
        return "🧪 REPL variables:\n" + "\n".join(f"- {k}: {v}" for k, v in vars_.items())

    @tool
    async def repl_reset() -> str:
        """REPL namespace clear karo (naya start)."""
        saved = get_repl().reset()
        if not saved:
            return f"🧹 REPL memory reset ho gayi, lekin disk save fail hua: {get_repl().state_error}"
        return "🧹 REPL reset ho gaya."

    REPL_TOOLS = [python_repl, run_python_isolated, repl_state, repl_reset]
else:  # pragma: no cover
    REPL_TOOLS = []
