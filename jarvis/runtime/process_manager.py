"""
jarvis/runtime/process_manager.py — Background process manager
=============================================================

Jarvis ka background process manager hai: koi bhi long-running cheez
background me chala sake (dev server, watcher, build, ffmpeg export) aur baad me:

    start_process(cmd)  →  process_id
    process_list()      →  chal rahe processes (id, cmd, pid, uptime, alive)
    process_logs(id)    →  stdout/stderr tail (live)
    process_stop(id)    →  gracefully band
    process_input(id)   →  stdin me likho (interactive CLI ke liye)

Saara state in-memory + logs disk par (workspace/logs/proc_<id>.log), taaki gateway
restart hone par bhi logs padhe ja sakein.
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ..config import LOGS_DIR, PROCESS_MAX, WORKSPACE_DIR


@dataclass
class ProcInfo:
    pid: str
    command: str
    popen: subprocess.Popen | None = None
    log_path: Path | None = None
    cwd: str = ""
    started: float = field(default_factory=time.time)
    status: str = "running"
    returncode: int | None = None

    @property
    def alive(self) -> bool:
        if self.popen is None:
            return False
        return self.popen.poll() is None

    def snapshot(self) -> dict:
        return {
            "pid": self.pid,
            "command": self.command,
            "cwd": self.cwd,
            "status": "running" if self.alive else f"exited({self.popen.poll() if self.popen else '?'})",
            "uptime_s": round(time.time() - self.started, 1),
            "log": str(self.log_path) if self.log_path else "",
            "os_pid": self.popen.pid if self.popen else None,
        }


IS_WINDOWS = os.name == "nt"
_PROCS: dict[str, ProcInfo] = {}
_LOCK = threading.RLock()


def _prune() -> None:
    """Purane exited processes ko list se hatao (limit ke andar raho)."""
    if len(_PROCS) <= PROCESS_MAX:
        return
    exited = sorted([p for p in _PROCS.values() if not p.alive], key=lambda p: p.started)
    for p in exited:
        if len(_PROCS) <= PROCESS_MAX:
            break
        _PROCS.pop(p.pid, None)


def start_process(command: str, cwd: str = "", name: str = "", env: dict | None = None) -> ProcInfo:
    """Background me command chalao. stdout/stderr log file me jaata hai."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    pid = (name or uuid.uuid4().hex[:8]).replace(" ", "_")
    log_path = LOGS_DIR / f"proc_{pid}.log"
    workdir = Path(cwd).expanduser() if cwd else WORKSPACE_DIR
    if not workdir.exists():
        workdir = WORKSPACE_DIR

    with _LOCK:
        existing = _PROCS.get(pid)
        if existing is not None and existing.alive:
            raise ValueError(f"'{pid}' naam ka process pehle se chal raha hai (already running)")

        popen_options: dict[str, object] = {
            "shell": True,
            "cwd": str(workdir),
            "stderr": subprocess.STDOUT,
            "stdin": subprocess.PIPE,
            "env": {**os.environ, "PYTHONIOENCODING": "utf-8", **(env or {})},
        }
        if IS_WINDOWS:
            popen_options["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            popen_options["start_new_session"] = True

        with log_path.open("w", encoding="utf-8", errors="ignore") as log_file:
            log_file.write(
                f"$ {command}\n[cwd: {workdir}]\n[started: {time.strftime('%Y-%m-%d %H:%M:%S')}]\n"
                + "-" * 60 + "\n"
            )
            log_file.flush()
            popen_options["stdout"] = log_file
            popen = subprocess.Popen(command, **popen_options)

        info = ProcInfo(pid=pid, command=command, popen=popen, log_path=log_path, cwd=str(workdir))
        _PROCS[pid] = info
        _prune()
        return info


def list_processes() -> list[dict]:
    with _LOCK:
        processes = sorted(_PROCS.values(), key=lambda p: -p.started)
    return [process.snapshot() for process in processes]


def get_process(pid: str) -> ProcInfo | None:
    query = pid.strip()
    if not query:
        return None
    with _LOCK:
        return _PROCS.get(query)


def read_logs(pid: str, tail_lines: int = 80) -> str:
    info = get_process(pid)
    if not info or not info.log_path or not info.log_path.exists():
        return f"❌ Process '{pid}' ka koi log nahi mila."
    lines = info.log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    return "\n".join(lines[-tail_lines:])


def _signal_process_tree(popen: subprocess.Popen, force: bool) -> None:
    """Signal the whole process group/tree created by start_process."""
    if IS_WINDOWS:
        if force:
            try:
                result = subprocess.run(
                    ["taskkill", "/PID", str(popen.pid), "/T", "/F"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError:
                if popen.poll() is None:
                    popen.kill()
                return
            if result.returncode != 0 and popen.poll() is None:
                popen.kill()
            return
        try:
            popen.send_signal(getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM))
        except Exception:
            popen.terminate()
        return

    try:
        os.killpg(os.getpgid(popen.pid), signal.SIGKILL if force else signal.SIGTERM)
    except Exception:
        if force:
            popen.kill()
        else:
            popen.terminate()


def stop_process(pid: str, force: bool = False) -> str:
    info = get_process(pid)
    if not info or info.popen is None:
        return f"❌ Process '{pid}' nahi mila."
    if not info.alive:
        info.status = "exited"
        return f"ℹ️ '{info.pid}' pehle hi exit ho chuka hai (code {info.popen.poll()})."
    try:
        _signal_process_tree(info.popen, force=force)
        try:
            info.popen.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _signal_process_tree(info.popen, force=True)
            try:
                info.popen.wait(timeout=5)
            except subprocess.TimeoutExpired:
                return f"❌ '{info.pid}' process group abhi tak band nahi hua."
        info.returncode = info.popen.poll()
        info.status = "stopped"
        if info.log_path:
            try:
                with info.log_path.open("a", encoding="utf-8") as log_file:
                    log_file.write("\n[stopped by Jarvis]\n")
            except OSError:
                pass
        return f"🛑 '{info.pid}' band kar diya."
    except Exception as exc:
        return f"❌ stop fail: {exc}"


def send_input(pid: str, text: str) -> str:
    info = get_process(pid)
    if not info or info.popen is None or info.popen.stdin is None:
        return f"❌ Process '{pid}' ka stdin available nahi."
    if not info.alive:
        return f"❌ Process '{info.pid}' chal nahi raha."
    try:
        if not text.endswith("\n"):
            text += "\n"
        info.popen.stdin.write(text.encode())
        info.popen.stdin.flush()
        return f"⌨️ '{info.pid}' ko input bhej diya."
    except Exception as exc:
        return f"❌ input fail: {exc}"


def stop_all() -> int:
    n = 0
    with _LOCK:
        processes = list(_PROCS.values())
    for p in processes:
        if p.alive:
            stop_process(p.pid, force=True)
            n += 1
    return n


# ---------------------------------------------------------------------------
# LangChain tools
# ---------------------------------------------------------------------------
try:
    from langchain_core.tools import tool
except Exception:  # pragma: no cover
    tool = None  # type: ignore

if tool is not None:

    @tool
    async def start_background_process(command: str, cwd: str = "", name: str = "") -> str:
        """Kisi bhi command ko BACKGROUND me chalao; dangerous command pe human Gate aayega."""
        from ..security.gate import authorize_command
        from ..security.permissions import current_surface

        workdir = Path(cwd).expanduser() if cwd else WORKSPACE_DIR
        if not workdir.exists():
            workdir = WORKSPACE_DIR
        approval = await authorize_command(command, str(workdir), surface=current_surface())
        if not approval.allowed:
            return f"🚫 Background command nahi chalaya: {approval.reason}"
        try:
            info = start_process(command, cwd=str(workdir), name=name)
            tail = read_logs(info.pid, tail_lines=12).splitlines()
            output = "\n".join(tail[4:]) if len(tail) > 4 else ""
            if not output:
                output = "(process started; check process_logs for output)"
            return (
                f"🚀 Background process start requested: **{info.pid}** "
                f"(os pid {info.popen.pid if info.popen else '?'})\n"
                f"📜 log: {info.log_path}\n---\n{output[-1500:]}"
            )
        except Exception as exc:
            return f"❌ background start fail: {exc}"

    @tool
    def process_list() -> str:
        """Background processes ki list (running + exited) with uptime aur log path."""
        procs = list_processes()
        if not procs:
            return "ℹ️ Koi background process nahi chal raha."
        lines = [f"⚙️ {len(procs)} process(es):"]
        for p in procs:
            lines.append(f"- **{p['pid']}** [{p['status']}] {p['uptime_s']}s | `{p['command'][:70]}`\n  log: {p['log']}")
        return "\n".join(lines)

    @tool
    def process_logs(pid: str, tail_lines: int = 80) -> str:
        """Background process ke logs padho (live tail) — "server start hua ya nahi?" check karne ke liye."""
        return read_logs(pid, tail_lines)

    @tool
    async def process_stop(pid: str, force: bool = False) -> str:
        """Background process band karo; actual process termination owner approval maangta hai."""
        from ..security.gate import authorize_command
        from ..security.permissions import current_surface

        info = get_process(pid)
        if info is None or not info.alive:
            return stop_process(pid, force)
        approval = await authorize_command(
            f"terminate process {info.pid}: {info.command}", info.cwd or str(WORKSPACE_DIR),
            force=True, surface=current_surface(), reason="process termination requested",
        )
        if not approval.allowed:
            return f"🚫 Process nahi roka: {approval.reason}"
        return stop_process(pid, force)

    @tool
    def process_input(pid: str, text: str) -> str:
        """Chalte process ke stdin me input bhejo (interactive CLI ke liye)."""
        return send_input(pid, text)

    PROCESS_TOOLS = [start_background_process, process_list, process_logs, process_stop, process_input]
else:  # pragma: no cover
    PROCESS_TOOLS = []
