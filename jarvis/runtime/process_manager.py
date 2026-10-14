"""
jarvis/runtime/process_manager.py — Background process manager
=============================================================

OpenClaw ke `exec` + `process` tools se inspired: Jarvis koi bhi long-running cheez
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
            "status": "running" if self.alive else f"exited({self.popen.poll() if self.popen else '?'})",
            "uptime_s": round(time.time() - self.started, 1),
            "log": str(self.log_path) if self.log_path else "",
            "os_pid": self.popen.pid if self.popen else None,
        }


_PROCS: dict[str, ProcInfo] = {}


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

    log_file = log_path.open("w", encoding="utf-8", errors="ignore")
    log_file.write(f"$ {command}\n[cwd: {workdir}]\n[started: {time.strftime('%Y-%m-%d %H:%M:%S')}]\n" + "-" * 60 + "\n")
    log_file.flush()

    popen = subprocess.Popen(
        command,
        shell=True,
        cwd=str(workdir),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        stdin=subprocess.PIPE,
        env={**os.environ, "PYTHONIOENCODING": "utf-8", **(env or {})},
        start_new_session=True,
    )
    info = ProcInfo(pid=pid, command=command, popen=popen, log_path=log_path)
    _PROCS[pid] = info
    _prune()
    return info


def list_processes() -> list[dict]:
    return [p.snapshot() for p in sorted(_PROCS.values(), key=lambda p: -p.started)]


def get_process(pid: str) -> ProcInfo | None:
    if pid in _PROCS:
        return _PROCS[pid]
    for p in _PROCS.values():  # partial match
        if p.pid.startswith(pid) or pid in p.command:
            return p
    return None


def read_logs(pid: str, tail_lines: int = 80) -> str:
    info = get_process(pid)
    if not info or not info.log_path or not info.log_path.exists():
        return f"❌ Process '{pid}' ka koi log nahi mila."
    lines = info.log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    return "\n".join(lines[-tail_lines:])


def stop_process(pid: str, force: bool = False) -> str:
    info = get_process(pid)
    if not info or info.popen is None:
        return f"❌ Process '{pid}' nahi mila."
    if not info.alive:
        info.status = "exited"
        return f"ℹ️ '{info.pid}' pehle hi exit ho chuka hai (code {info.popen.poll()})."
    try:
        if force:
            info.popen.kill()
        else:
            try:
                os.killpg(os.getpgid(info.popen.pid), signal.SIGTERM)
            except Exception:
                info.popen.terminate()
            try:
                info.popen.wait(timeout=5)
            except subprocess.TimeoutExpired:
                info.popen.kill()
        info.status = "stopped"
        try:
            info.log_path.open("a", encoding="utf-8").write("\n[stopped by Jarvis]\n") if info.log_path else None
        except Exception:
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
    for p in list(_PROCS.values()):
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
    def start_background_process(command: str, cwd: str = "", name: str = "") -> str:
        """Kisi bhi command ko BACKGROUND me chalao (dev server, watcher, build, long export).

        Returns process id — usse `process_logs` / `process_stop` use karo.
        Use jab command turant return na kare: "npm run dev chala do", "python -m http.server 8080 background me".
        """
        try:
            info = start_process(command, cwd=cwd, name=name)
            time.sleep(2.0)
            tail = read_logs(info.pid, tail_lines=12).splitlines()
            head = "\n".join(tail[3:]) if len(tail) > 3 else ""
            return (
                f"🚀 Background process chalu: **{info.pid}** (os pid {info.popen.pid if info.popen else '?'})\n"
                f"📜 log: {info.log_path}\n---\n{head[-1500:]}"
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
    def process_stop(pid: str, force: bool = False) -> str:
        """Background process band karo (force=True se turant kill)."""
        return stop_process(pid, force)

    @tool
    def process_input(pid: str, text: str) -> str:
        """Chalte process ke stdin me input bhejo (interactive CLI ke liye)."""
        return send_input(pid, text)

    PROCESS_TOOLS = [start_background_process, process_list, process_logs, process_stop, process_input]
else:  # pragma: no cover
    PROCESS_TOOLS = []
