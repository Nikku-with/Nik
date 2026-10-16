"""
System operations — "refresh karo", "process clear karo", "cache saaf karo"
=========================================================================

Ye wo controlled maintenance tools hain jo owner natural language me maangta hai:

    "refresh system"        → config + tools + models dobara load (data safe rehta hai)
    "process clear karo"    → background processes band karo
    "cache clear karo"      → temp/cache saaf (workspace data nahi chhedta)
    "jarvis restart karo"   → controlled self-restart (optional)
    "system status"         → CPU/RAM/disk/uptime

SAFETY: ye kabhi bhi user data (notes/memory/workspace) delete nahi karte. "refresh" aur
"clear" ko kabhi bhi "sab mita do" me interpret nahi karte.
"""
from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

from langchain_core.tools import tool

from ..config import LOGS_DIR, WORKSPACE_DIR, python_executable
from ..config import settings as S


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _dir_size(path: Path, cap: int = 4000) -> tuple[int, int]:
    total = count = 0
    try:
        for i, p in enumerate(path.rglob("*")):
            if i > cap:
                break
            try:
                if p.is_file():
                    total += p.stat().st_size
                    count += 1
            except Exception:
                continue
    except Exception:
        pass
    return total, count


SystemResult = dict


# ---------------------------------------------------------------------------
# refresh / reload
# ---------------------------------------------------------------------------
def refresh_system(reload_env: bool = True, reload_tools: bool = True,
                   reload_models: bool = True, clear_caches: bool = False) -> SystemResult:
    """Config, tools, models dobara load karo — user data untouched."""
    report: list[str] = []
    ok = True

    if reload_env:
        try:
            S.reload_settings()
            report.append("config/.env dobara load hua")
        except Exception as exc:
            ok = False
            report.append(f"env reload fail: {exc}")

    if clear_caches:
        try:
            from . import file_tools

            file_tools._INDEX_CACHE.update({"ts": 0, "items": None})  # noqa: SLF001
            report.append("file index cache clear")
        except Exception as exc:
            report.append(f"file cache skip: {exc}")
        try:
            from ..skills import loader as sk

            sk.clear_cache()
            report.append("skills cache clear")
        except Exception:
            pass

    if reload_tools:
        try:
            import importlib
            import sys as _sys

            pkg = _sys.modules.get("jarvis.tools")
            if pkg is None:
                import jarvis.tools as pkg  # noqa: PLC0415

            importlib.reload(pkg)
            names = len(pkg.tool_names())
            report.append(f"tool registry reload ({names} tools)")
        except Exception as exc:
            ok = False
            report.append(f"tool reload fail: {exc}")

    if reload_models:
        try:
            from ..llm import reset_cache

            reset_cache()
            report.append("LLM/model cache reset")
        except Exception as exc:
            report.append(f"model reset skip: {exc}")
        try:
            from ..graph import build as graph_build

            graph_build.reset_graph()
            report.append("LangGraph dobara compile hua")
        except Exception:
            pass

    report.append("aapka data safe hai — notes/memory/workspace ko kuch nahi hua")
    return {"ok": ok, "steps": report}


# ---------------------------------------------------------------------------
# cache clearing (sirf apne temp/cache, kabhi user files nahi)
# ---------------------------------------------------------------------------
_SAFE_CACHE_DIRS = ("__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache")


def clear_cache(scope: str = "all", dry_run: bool = False) -> SystemResult:
    """Cache/temp saaf karo — workspace data (notes, memory, artifacts) chhoota bhi nahi."""
    removed_files = 0
    freed = 0
    details: list[str] = []
    scope = (scope or "all").lower()

    targets: list[Path] = []
    if scope in ("all", "python"):
        root = Path(__file__).resolve().parent.parent.parent
        for dirpath, dirnames, _ in os.walk(root):
            for d in list(dirnames):
                if d in _SAFE_CACHE_DIRS:
                    targets.append(Path(dirpath) / d)
                    dirnames.remove(d)
    if scope in ("all", "logs"):
        cut = time.time() - 7 * 86400
        for log in LOGS_DIR.glob("*.log"):
            try:
                if log.stat().st_mtime < cut:
                    targets.append(log)
            except Exception:
                continue
    if scope in ("all", "index"):
        for name in ("model_cache.json",):
            p = WORKSPACE_DIR / name
            if p.exists():
                targets.append(p)

    for target in targets:
        try:
            if target.is_dir():
                size, _ = _dir_size(target)
                if not dry_run:
                    shutil.rmtree(target, ignore_errors=True)
                removed_files += 1
                freed += size
            elif target.is_file():
                size = target.stat().st_size
                if not dry_run:
                    target.unlink()
                removed_files += 1
                freed += size
        except Exception as exc:
            details.append(f"skip {target.name}: {exc}")

    verb = "hata sakta hoon" if dry_run else "hata diye"
    summary = (f"🧹 cache cleanup ({scope}): {removed_files} item(s) {verb}, "
               f"~{_human(freed)} free hua")
    return {"ok": True, "items": removed_files, "freed_bytes": freed,
            "message": summary, "details": details[:5]}


# ---------------------------------------------------------------------------
# process clearing
# ---------------------------------------------------------------------------
def clear_processes(pid: int = 0, name: str = "", force: bool = False,
                    keep_self: bool = True) -> SystemResult:
    """Background processes band karo. pid/name na do to SAB apne processes band honge."""
    try:
        from ..runtime import process_manager as pm
    except Exception as exc:
        return {"ok": False, "message": f"process manager load nahi hua: {exc}"}

    procs = pm.list_processes()
    if pid:
        out = pm.stop_process(pid, force=force)
        return {"ok": True, "message": out, "stopped": 1}
    if name:
        matched = [p for p in procs if name.lower() in str(p.get("name", "")).lower()
                   or name.lower() in str(p.get("command", "")).lower()]
        if not matched:
            return {"ok": False, "message": f"'{name}' naam ka koi process nahi mila"}
        msgs = [pm.stop_process(p["pid"], force=force) for p in matched]
        return {"ok": True, "message": f"{len(matched)} process band kiye:\n" + "\n".join(msgs), "stopped": len(matched)}

    if not procs:
        return {"ok": True, "message": "koi background process chal hi nahi raha tha ✅", "stopped": 0}
    pid_self = os.getpid()
    msgs = []
    for p in procs:
        if keep_self and p.get("pid") == pid_self:
            continue
        msgs.append(pm.stop_process(p["pid"], force=force))
    return {"ok": True, "message": f"{len(msgs)} process band kiye:\n" + "\n".join(msgs), "stopped": len(msgs)}


async def clear_processes_authorized(pid: int = 0, name: str = "", force: bool = False,
                                     keep_self: bool = True) -> SystemResult:
    """Approve process termination once, then delegate to the low-level manager."""
    from ..runtime import process_manager as pm
    from ..security.gate import authorize_command

    processes = pm.list_processes()
    if pid:
        targets = [entry for entry in processes if str(entry.get("pid")) == str(pid)]
    elif name:
        targets = [entry for entry in processes if name.lower() in str(entry.get("command", "")).lower()]
    else:
        targets = processes
    targets = [entry for entry in targets if entry.get("status") == "running"]
    if not targets:
        return clear_processes(pid=pid, name=name, force=force, keep_self=keep_self)
    summary = ", ".join(f"{entry.get('pid')}:{str(entry.get('command', ''))[:80]}" for entry in targets[:12])
    approval = await authorize_command(
        f"terminate process(es) {summary}", str(WORKSPACE_DIR), force=True,
        reason="process termination requested",
    )
    if not approval.allowed:
        return {"ok": False, "message": f"process nahi roke: {approval.reason}", "stopped": 0}
    return clear_processes(pid=pid, name=name, force=force, keep_self=keep_self)


# ---------------------------------------------------------------------------
# status / info
# ---------------------------------------------------------------------------
def system_status() -> SystemResult:
    info: dict[str, object] = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "python": sys.version.split()[0],
        "executable": python_executable(),
        "pid": os.getpid(),
        "cwd": str(Path.cwd()),
        "workspace": str(WORKSPACE_DIR),
        "platform": sys.platform,
    }
    try:
        import platform

        info["os"] = f"{platform.system()} {platform.release()}"
        info["machine"] = platform.machine()
    except Exception:
        pass
    try:
        import shutil as sh

        total, used, free = sh.disk_usage(str(WORKSPACE_DIR))
        info["disk"] = {"total": _human(total), "used": _human(used), "free": _human(free)}
    except Exception:
        pass
    try:  # RAM (Linux)
        with open("/proc/meminfo") as fh:
            mem = {}
            for line in fh:
                k, _, v = line.partition(":")
                mem[k.strip()] = v.strip()
        info["ram_total"] = mem.get("MemTotal", "?")
        info["ram_available"] = mem.get("MemAvailable", "?")
    except Exception:
        pass
    try:  # load average
        info["load_avg"] = [round(x, 2) for x in os.getloadavg()]
    except Exception:
        pass
    try:
        boot = time.time() - float(open("/proc/uptime").read().split()[0])
        info["uptime_hours"] = round((time.time() - boot) / 3600, 1)
    except Exception:
        pass
    ws_size, ws_files = _dir_size(WORKSPACE_DIR)
    info["workspace_size"] = f"{_human(ws_size)} ({ws_files} files)"
    return {"ok": True, "info": info}


def reload_config() -> SystemResult:
    """Sirf config/settings reload (llm/tools ko chhede bina)."""
    try:
        S.reload_settings()
        from ..config import runtime as R

        R.load(force=True)
        return {"ok": True, "message": "config + runtime settings dobara load ho gayi"}
    except Exception as exc:
        return {"ok": False, "message": f"reload fail: {exc}"}


async def restart_self(delay_s: int = 2) -> SystemResult:
    """Controlled restart, held behind the human-only Dangerous-Command Gate."""
    if os.environ.get("NIKHILAI_ALLOW_RESTART", "1") != "1":
        return {"ok": False, "message": "restart disabled (NIKHILAI_ALLOW_RESTART=0)"}
    if os.environ.get("NIKHILAI_MAIN_PROCESS") != "1":
        return {"ok": False, "message": "restart sirf `python -m jarvis` ke andar chalta hai "
                                        "(library/script se process replace nahi karte). "
                                        "Yehi command dobara chalao."}
    from ..security.gate import authorize_command

    approval = await authorize_command(
        "python -m jarvis restart", str(WORKSPACE_DIR), force=True,
        reason="Jarvis process restart",
    )
    if not approval.allowed:
        return {"ok": False, "message": f"restart nahi kiya: {approval.reason}"}
    try:
        os.environ["NIKHILAI_RESTARTED_AT"] = str(time.time())
        argv = [python_executable(), "-m", "jarvis", *sys.argv[1:]]
        os.execv(python_executable(), argv)  # noqa: S606  (human-approved self restart)
        return {"ok": True, "message": "Restart process handoff ho gaya."}
    except Exception as exc:
        return {"ok": False, "message": f"restart fail: {exc} — manually `python -m jarvis` chalao"}


# ---------------------------------------------------------------------------
# LangChain tools
# ---------------------------------------------------------------------------
@tool
def refresh_system_tool(clear_caches: bool = True) -> str:
    """Config/tools/models dobara load karo ('refresh system', 'reload karo').

    User data (notes, memory, files) bilkul safe rehta hai — ye sirf caches aur registry reload karta hai.
    """
    res = refresh_system(clear_caches=clear_caches)
    icon = "♻️" if res["ok"] else "⚠️"
    return f"{icon} System refresh:\n" + "\n".join(f"  • {s}" for s in res["steps"])


@tool
def clear_cache_tool(scope: str = "all", dry_run: bool = False) -> str:
    """Cache/temp saaf karo (scope: all | python | logs | index). dry_run=True se sirf dikhata hai."""
    res = clear_cache(scope=scope, dry_run=dry_run)
    extra = ("\n" + "\n".join(f"  • {d}" for d in res["details"])) if res["details"] else ""
    return res["message"] + extra


@tool
async def clear_process_tool(pid: int = 0, name: str = "", force: bool = False) -> str:
    """Background processes band karo; process terminate karne se pehle owner approval aata hai."""
    res = await clear_processes_authorized(pid=pid, name=name, force=force)
    return ("🧯 " if res.get("ok") else "❌ ") + str(res.get("message"))


@tool
async def restart_jarvis_tool() -> str:
    """Jarvis ko controlled, human-approved restart karo."""
    res = await restart_self()
    return ("🔄 " if res.get("ok") else "⚠️ ") + str(res.get("message"))


@tool
def reload_config_tool() -> str:
    """Sirf settings/.env dobara load karo (poora system refresh nahi)."""
    res = reload_config()
    return ("♻️ " if res.get("ok") else "❌ ") + str(res.get("message"))


@tool
def full_system_status() -> str:
    """Machine ka detailed status: time, CPU load, RAM, disk, uptime, workspace size, python."""
    res = system_status()
    info = res.get("info", {})
    lines = ["🖥️ System status:"]
    for key, val in info.items():
        if isinstance(val, dict):
            val = ", ".join(f"{k}={v}" for k, v in val.items())
        lines.append(f"  • {key}: {val}")
    return "\n".join(lines)


SYSTEM_OPS_TOOLS = [refresh_system_tool, clear_cache_tool, clear_process_tool,
                    restart_jarvis_tool, reload_config_tool, full_system_status]
