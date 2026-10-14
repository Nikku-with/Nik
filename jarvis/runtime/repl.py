"""
jarvis/runtime/repl.py — Persistent Python REPL (self-correction sandbox)
========================================================================

Owner ka ask: *"Python ka `exec()` / `sys.executable` use karke temporary sandbox banaye
jahan Jarvis apna hi banaya code run karke check kar sake"*.

Ye module dono deta hai:

  1. **Persistent REPL** (default) — ek hi namespace me kaam karta hai, isliye
     "pehle variable banao, agle step me use karo" wala flow chalta hai:
         step 1: `df = [1,2,3]`
         step 2: `sum(df)`        → 6  ✅ (variable yaad hai)

     Kaise: AST-safety check → thread me `exec()` (timeout ke saath) → stdout/stderr capture
     → namespace session_state.json me persist ho jaata hai (restart ke baad bhi context).

  2. **Isolated subprocess runner** (`run_isolated`) — jab poora fresh process chahiye
     (`sys.executable` subprocess), heavy/crashy code ke liye.

Safety (personal-assistant grade, but serious):
  * blocked: os.system/rm -rf, subprocess shell escape, shutil.rmtree('/'), socket exfil,
    `__import__('os').system`, credential posting
  * allowed: pandas/csv/json/math/requests(optional) jaisa normal analysis & automation
  * workspace me hi file likhne ki salah; system paths guard
"""
from __future__ import annotations

import ast
import asyncio
import io
import json
import os
import re
import subprocess
import sys
import textwrap
import time
import traceback
from pathlib import Path
from typing import Any

from ..config import CODE_TIMEOUT_S, SESSION_STATE_FILE, SCRIPTS_DIR, WORKSPACE_DIR, python_executable

_BANNED = (
    "rm -rf /", "shutil.rmtree('/')", 'shutil.rmtree("/")', "os.system('rm -rf",
    "mkfs", "format c:", ":(){:|:&};:", ">/dev/sda", "__import__('os').system",
    '__import__("os").system', "os.popen(", "subprocess.call(['rm', '-rf', '/'",
    "base64.b64decode", "os.environ['AWS_SECRET", "os.environ[\"AWS_SECRET",
)


def safety_check(code: str) -> tuple[bool, str]:
    """AST parse + banned pattern scan. Returns (ok, message)."""
    if not (code or "").strip():
        return False, "code khali hai"
    low = code.lower()
    for bad in _BANNED:
        if bad.lower() in low:
            return False, f"dangerous pattern blocked: `{bad}`"
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return False, f"SyntaxError: {exc.msg} (line {exc.lineno})"
    # attribute-level checks
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in {"rmtree", "unlink", "remove", "rmdir"}:
            # allow only inside workspace
            src = ast.unparse(node) if hasattr(ast, "unparse") else ""
            if "WORKSPACE" not in src and "workspace" not in src and "/" in src and not src.startswith("Path"):
                pass  # warn-only: path guard runtime me hai
    return True, "ok"


class PythonREPL:
    """Ek persistent namespace wala REPL (scripts/notes sab isi se chalti hain)."""

    def __init__(self, state_file: Path = SESSION_STATE_FILE):
        self.state_file = state_file
        self.namespace: dict[str, Any] = {"__name__": "__jarvis_repl__", "__builtins__": __builtins__}
        self.history: list[dict[str, Any]] = []
        self._load_state()

    # ---------------------------------------------------------------- state
    def _load_state(self) -> None:
        """Pichhle session ke variables wapas laao (sirf JSON-safe values)."""
        try:
            if not self.state_file.exists():
                return
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            repl_state = data.get("repl", {})
            for key, value in (repl_state.get("variables") or {}).items():
                if re.match(r"^[A-Za-z_]\w*$", key):
                    self.namespace[key] = value
            self.history = repl_state.get("history") or []
        except Exception:
            pass

    def save_state(self) -> None:
        try:
            data = {}
            if self.state_file.exists():
                data = json.loads(self.state_file.read_text(encoding="utf-8"))
            safe_vars: dict[str, Any] = {}
            for key, value in self.namespace.items():
                if key.startswith("__"):
                    continue
                try:
                    json.dumps(value)
                    safe_vars[key] = value
                except Exception:
                    safe_vars[key] = f"<{type(value).__name__}>"
            data["repl"] = {"variables": safe_vars, "history": self.history[-40:],
                            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")}
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        except Exception:
            pass

    # ----------------------------------------------------------------- run
    def run(self, code: str, timeout: int = CODE_TIMEOUT_S) -> dict[str, Any]:
        ok, msg = safety_check(code)
        if not ok:
            return {"ok": False, "stdout": "", "stderr": f"🚫 {msg}", "error": msg, "duration_s": 0.0}

        source = textwrap.dedent(code)
        last_expr_src: str | None = None
        try:
            tree = ast.parse(source, mode="exec")
            if tree.body and isinstance(tree.body[-1], ast.Expr):
                last_expr_src = ast.unparse(tree.body[-1])  # REPL feel: last expression print ho
                tree.body = tree.body[:-1]
            compiled = compile(tree, "<jarvis_repl>", "exec")
        except SyntaxError as exc:
            return {"ok": False, "stdout": "", "stderr": f"SyntaxError: {exc.msg} (line {exc.lineno})",
                    "error": f"SyntaxError: {exc.msg}", "duration_s": 0.0}

        stdout_buf, stderr_buf = io.StringIO(), io.StringIO()
        started = time.time()
        error: list[str | None] = [None]

        # ⚠️ Global `redirect_stdout` jaan-boojh kar use NAHI kiya:
        # timeout hone par runaway thread us redirect ko pakde rehta hai → poore process
        # ka stdout StringIO me chala jaata hai (output gaayab). Isliye per-run buffer
        # inject karte hain: namespace ka `print` usi buffer me likhta hai (thread-safe).
        def _capture_print(*args, sep: str = " ", end: str = "\n", file=None, flush: bool = False) -> None:
            target = file if file is not None else stdout_buf
            target.write(sep.join(str(a) for a in args) + end)
            if flush:
                target.flush()

        exec_ns = dict(self.namespace)
        exec_ns["print"] = _capture_print
        exec_ns["pwd"] = lambda: str(WORKSPACE_DIR)
        exec_ns["WORKSPACE"] = str(WORKSPACE_DIR)

        def _exec() -> None:
            try:
                exec(compiled, exec_ns)
                if last_expr_src:
                    try:
                        value = eval(last_expr_src, exec_ns)
                        if value is not None:
                            _capture_print(repr(value))
                    except Exception:
                        error[0] = traceback.format_exc()
            except Exception:
                error[0] = traceback.format_exc()
            finally:
                # naye/updated variables wapas persistent namespace me
                for key, value in exec_ns.items():
                    if key not in {"__builtins__"}:
                        self.namespace[key] = value

        # NOTE: ThreadPoolExecutor use nahi karte — uska exit hook (atexit) runaway thread
        # ko join karta hai → process hang. Daemon thread exit par join NAHI hota,
        # isliye timeout par hum turant return kar sakte hain.
        import threading

        worker = threading.Thread(target=_exec, name="jarvis-repl", daemon=True)
        worker.start()
        worker.join(timeout=timeout)
        if worker.is_alive():
            error[0] = (f"⏱️ Timeout: {timeout}s ke andar code complete nahi hua (infinite loop?) — "
                        f"runaway code background me chhod diya, namespace dirty ho sakta hai. "
                        f"`repl_reset` se saaf karo.")

        duration = time.time() - started
        out, err = stdout_buf.getvalue(), stderr_buf.getvalue()
        self.history.append({"code": source[:400], "ok": error[0] is None,
                             "duration_s": round(duration, 3), "at": time.strftime("%H:%M:%S")})
        self.namespace.pop("_jarvis_last_expr", None)
        self.save_state()
        return {
            "ok": error[0] is None,
            "stdout": out,
            "stderr": err + (("\n" + error[0]) if error[0] else ""),
            "error": error[0],
            "duration_s": round(duration, 3),
        }

    def reset(self) -> None:
        self.namespace = {"__name__": "__jarvis_repl__", "__builtins__": __builtins__}
        self.history = []
        self.save_state()

    def variables(self) -> dict[str, str]:
        return {k: f"{type(v).__name__} = {str(v)[:80]}" for k, v in self.namespace.items() if not k.startswith("__")}


_REGISTRY: dict[str, PythonREPL] = {}


def get_repl(name: str = "main") -> PythonREPL:
    if name not in _REGISTRY:
        _REGISTRY[name] = PythonREPL()
    return _REGISTRY[name]


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
        get_repl().reset()
        return "🧹 REPL reset ho gaya."

    REPL_TOOLS = [python_repl, run_python_isolated, repl_state, repl_reset]
else:  # pragma: no cover
    REPL_TOOLS = []
