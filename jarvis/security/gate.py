"""
Dangerous-Command Gate — destructive shell/actions ke liye human-only approval.

Design (spec section 3):
  * `classify_shell(command)` — real parsing (quote-aware split, shlex per segment,
    flag normalization, sudo/env/bash -c unwrap). Unparseable = dangerous.
  * `ApprovalRequest` + `ApprovalBroker` — one request object, delivered to the surface
    the request came from (CLI y/N prompt, Telegram inline buttons, LiveKit spoken
    confirm, web UI modal). Yes = run once. No/timeout = cancel.
  * The model can NEVER approve: no `grant_exec_approval` tool, no `confirmed` arg.
    Approval flows broker -> tool via runtime context, never via model output.
  * Modes via JARVIS_EXEC_APPROVAL (or runtime `exec_approval`): off | risky | all.
    off = no gate (+ one startup warning), risky = dangerous only (default),
    all = ask for every shell command.

Covered: run_shell_command, code_engine shell-outs, folder_file delete,
write_file overwrite of non-empty files, kill/close of foreign processes,
restart_self, CLI /run, automation dangerous steps.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import os
import shlex
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

SAFE = "safe"
RISKY = "risky"
DANGEROUS = "dangerous"

DEFAULT_TIMEOUT_S = 60

__all__ = [
    "SAFE",
    "RISKY",
    "DANGEROUS",
    "DEFAULT_TIMEOUT_S",
    "GateDecision",
    "classify_shell",
    "is_dangerous_shell",
    "ApprovalRequest",
    "ApprovalResult",
    "ApprovalBroker",
    "get_broker",
    "gate_context",
    "current_surface",
    "effective_mode",
    "GateCheck",
    "check_shell",
    "check_action",
    "register_asker",
    "ask_cli",
    "reset_broker_for_tests",
]

# ---------------------------------------------------------------------------
# command classification — real parsing, not substring matching
# ---------------------------------------------------------------------------


@dataclass
class GateDecision:
    dangerous: bool
    reason: str = ""
    severity: str = SAFE  # safe | risky | dangerous (reporting detail)
    command: str = ""

    def __bool__(self) -> bool:  # pragma: no cover - convenience
        return self.dangerous


def _split_segments(command: str) -> list[str]:
    """Shell line ko operators ( ; && || | & newlines ) par todo, quotes respect karke."""
    segs: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    esc = False
    s = command or ""
    i = 0

    def flush() -> None:
        t = "".join(buf).strip()
        if t:
            segs.append(t)
        buf.clear()

    while i < len(s):
        ch = s[i]
        if esc:
            buf.append(ch)
            esc = False
            i += 1
            continue
        if ch == "\\" and quote != "'":
            esc = True
            buf.append(ch)
            i += 1
            continue
        if ch in ('"', "'"):
            if quote is None:
                quote = ch
            elif quote == ch:
                quote = None
            buf.append(ch)
            i += 1
            continue
        if quote is None:
            two = s[i : i + 2]
            if two in ("&&", "||"):
                flush()
                i += 2
                continue
            if ch in ";|&\n()":
                flush()
                i += 1
                continue
        buf.append(ch)
        i += 1
    if quote is not None:
        raise ValueError("unbalanced quote")
    flush()
    return segs


def _parse_segment(seg: str) -> list[str]:
    """One pipeline segment -> argv. Unparseable nahi hona chahiye (caller dangerous maanta hai)."""
    return shlex.split(seg, posix=True)


_SHELL_C_FLAG = {
    "bash": "-c",
    "sh": "-c",
    "dash": "-c",
    "zsh": "-c",
    "fish": "-c",
    "ksh": "-c",
    "pwsh": None,
    "powershell": None,  # -c / -command / -encodedcommand
    "cmd": None,  # /c
    "python": "-c",
    "python3": "-c",
    "pythonw": "-c",
    "perl": "-e",
    "ruby": "-e",
    "php": "-r",
    "node": "-e",
}

_DOWNLOADERS = {
    "curl",
    "wget",
    "aria2c",
    "axel",
    "http",
    "https",
    "fetch",
    "iwr",
    "invoke-webrequest",
    "invoke-restmethod",
}
_SHELLS = {
    "bash",
    "sh",
    "dash",
    "zsh",
    "fish",
    "ksh",
    "tcsh",
    "pwsh",
    "powershell",
    "cmd",
    "python",
    "python3",
    "perl",
    "ruby",
    "php",
    "node",
    "lua",
    "iex",
    "invoke-expression",
}


def _basename(prog: str) -> str:
    base = (prog or "").replace("\\", "/").rsplit("/", 1)[-1]
    low = base.lower()
    for ext in (".exe", ".cmd", ".bat", ".ps1", ".com"):
        if low.endswith(ext):
            base = base[: -len(ext)]
            low = base.lower()
    return low


_WRAPPER_VALUE_FLAGS: dict[str, set[str]] = {
    # wrapper -> value-taking flags (glued form bhi: -uX, -n5)
    "sudo": {
        "-u",
        "-g",
        "-p",
        "-U",
        "-r",
        "-t",
        "-T",
        "-C",
        "--user",
        "--group",
        "--prompt",
        "--other-user",
        "--role",
        "--type",
        "--close-from",
    },
    "doas": {"-u"},
    "nice": {"-n"},
    "ionice": {"-c", "-n", "-p"},
    "stdbuf": {"-o", "-e", "-i"},
    "nsenter": {"-t", "--target", "--mount", "--uts", "--ipc", "--net", "--pid", "--user", "--cgroup", "--time"},
    "unshare": {"--map-user", "--map-group"},
    "time": {"-o", "--output"},
    "proxychains": {"-f"},
    "pkexec": {"--user"},
    "gsudo": {"-u"},
    "fakeroot": {"-l", "--lib", "-f", "--faked"},
}


def _skip_wrapper_flags(toks: list[str], wrapper: str) -> list[str]:
    """`sudo -u nobody -n VAR=x cmd` -> [cmd ...]. Values/glued forms samajhta hai."""
    rest = toks[1:]
    values = _WRAPPER_VALUE_FLAGS.get(wrapper, set())
    value_letters = {v[1] for v in values if len(v) == 2 and v.startswith("-")}
    i = 0
    while i < len(rest):
        t = rest[i]
        if t == "--":
            return rest[i + 1 :]
        if t.startswith("/") and wrapper in ("runas",):
            i += 1  # /user:.. /password:.. /noprofile ...
            continue
        if t.startswith("-") and len(t) > 1 and not t[1].isdigit():
            if "=" in t or t.startswith("--"):
                i += 1  # --flag / --flag=value
                continue
            # short: -n (maybe value next) ya -n5 (glued) ya bundle (-nv)
            if t[1] in value_letters and len(t) == 2:
                i += 2  # value agle token me
            else:
                i += 1
            continue
        break
    # chroot ka pehla non-flag arg = newroot (wo bhi skip)
    if wrapper == "chroot" and i < len(rest):
        i += 1
    return rest[i:]


def _strip_wrappers(tokens: list[str]) -> tuple[list[str], str]:
    """sudo/env/nice/timeout/xargs/VAR=x wrappers hatao. Returns (argv, note)."""
    toks = list(tokens)
    # leading VAR=x assignments
    while toks and _is_env_assign(toks[0]):
        toks = toks[1:]
    if toks and toks[0] == "!":
        toks = toks[1:]
    guard = 0
    while toks and guard < 8:
        guard += 1
        head = _basename(toks[0])
        if head in (
            "sudo",
            "doas",
            "runas",
            "pkexec",
            "gsudo",
            "fakeroot",
            "command",
            "builtin",
            "nice",
            "nohup",
            "setsid",
            "stdbuf",
            "ionice",
            "unshare",
            "nsenter",
            "time",
            "chroot",
            "proxychains",
            "tsocks",
        ):
            toks = _skip_wrapper_flags(toks, head)
            while toks and _is_env_assign(toks[0]):
                toks = toks[1:]
            continue
        if head == "env":
            toks = toks[1:]
            while toks and (_is_env_assign(toks[0]) or toks[0] in ("-i", "-0", "--null", "-u")):
                if toks[0] == "-u":
                    toks = toks[2:]  # -u NAME
                else:
                    toks = toks[1:]
            continue
        if head == "timeout":
            toks = toks[1:]
            # timeout [OPTS] DURATION CMD... — duration + flags skip karo
            while toks and (toks[0].startswith("-") or _is_duration(toks[0])):
                if toks[0] in ("-s", "--signal", "-k", "--kill-after"):
                    toks = toks[2:]
                else:
                    toks = toks[1:]
            continue
        if head == "xargs":
            toks = _strip_xargs(toks)
            continue
        if head == "find":
            extracted, dangerous = _handle_find(toks)
            if dangerous:
                return [], dangerous  # note carries the danger reason
            if extracted is not None:
                toks = extracted
                continue
            return toks, ""  # plain find (no -exec) — safe-ish, analyse as find
        if head == "git":
            sub = _git_subcommand(toks)
            return (["git", *sub] if sub else toks), ""
        break
    while toks and _is_env_assign(toks[0]):
        toks = toks[1:]
    return toks, ""


def _is_env_assign(tok: str) -> bool:
    if "=" not in tok or tok.startswith(("=", "-", "/", "<", ">")):
        return False
    name = tok.split("=", 1)[0]
    return bool(name) and all(c.isalnum() or c == "_" for c in name) and not name[0].isdigit()


def _is_duration(tok: str) -> bool:
    t = tok.strip().lower()
    if t.replace(".", "", 1).isdigit():
        return True
    return len(t) > 1 and t[:-1].replace(".", "", 1).isdigit() and t[-1] in "smhd"


def _strip_xargs(toks: list[str]) -> list[str]:
    """xargs FLAGS CMD ARGS... -> [CMD, ARGS...]."""
    rest = toks[1:]
    i = 0
    while i < len(rest):
        t = rest[i]
        if t in ("-0", "--null", "-t", "--verbose", "-r", "--no-run-if-empty", "-p", "--interactive"):
            i += 1
        elif t in (
            "-I",
            "-n",
            "-P",
            "-s",
            "-E",
            "-e",
            "-a",
            "--arg-file",
            "--max-args",
            "--max-procs",
            "--replace",
            "--delimiter",
            "-d",
        ):
            i += 2
        elif t.startswith("-") and len(t) > 1 and not t[1].isdigit():
            # combined short flags (-n1, -I{}, -0r ...)
            if any(c in t for c in "InPsEad") and len(t) > 2 and not t[2:].lstrip("-").replace(".", "", 1).isdigit():
                i += 1  # value glued (-I{}) — still just skip the flag token
            else:
                i += 1
        else:
            break
    return rest[i:] if i < len(rest) else ["echo"]  # bare `xargs` (no cmd) = echo


def _handle_find(toks: list[str]) -> tuple[list[str] | None, str]:
    """find ... -delete -> dangerous; -exec CMD -> CMD ko analyse karo."""
    low = [t.lower() for t in toks]
    if "-delete" in low:
        return None, "find … -delete (recursive mass delete)"
    for flag in ("-exec", "-execdir", "-ok", "-okdir"):
        if flag in low:
            idx = low.index(flag)
            cmd = toks[idx + 1 :]
            # `;` / `+` terminator tak
            cut = len(cmd)
            for j, t in enumerate(cmd):
                if t in (";", "+"):
                    cut = j
                    break
            inner = [c for c in cmd[:cut] if c != "{}"]
            if not inner:
                return None, "find -exec with empty command (unparseable)"
            return inner, ""
    return None, ""


def _git_subcommand(toks: list[str]) -> list[str]:
    """git [GLOBAL FLAGS] SUBCOMMAND ... -> [SUBCOMMAND, ...]."""
    rest = toks[1:]
    i = 0
    two_word = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}
    while i < len(rest):
        t = rest[i]
        if t in two_word:
            i += 2
        elif t.startswith("-") and t not in ("-", "--"):
            i += 1
        elif t == "--":
            i += 1
            break
        else:
            break
    return rest[i:]


def _has_short_flag(args: list[str], letters: str) -> bool:
    """Combined short flags me letter dhoondho: -rf, -fr, -rfi ... (long flags alag se)."""
    for a in args:
        if a.startswith("--"):
            continue
        if a.startswith("-") and len(a) > 1 and not a[1].isdigit():
            if any(c in a[1:] for c in letters):
                return True
    return False


def _has_long_flag(args: list[str], names: set[str]) -> bool:
    for a in args:
        if a.startswith("--"):
            if a[2:].split("=", 1)[0] in names:
                return True
    return False


def _targets(args: list[str]) -> list[str]:
    """Flag nahi wale args (probable paths/targets)."""
    out: list[str] = []
    skip_next = False
    for a in args:
        if skip_next:
            skip_next = False
            continue
        if a.startswith("-") and len(a) > 1:
            # value-taking short flags (best effort)
            if a in ("-o", "-O", "-C", "-s", "-m", "-t", "-T", "-u", "-U"):
                skip_next = True
            continue
        out.append(a)
    return out


_DANGER_PATHS = {
    "/",
    "/*",
    "/.",
    "/*.*",
    "~",
    "$home",
    "${home}",
    "%userprofile%",
    "%systemdrive%\\",
    "c:\\",
    "c:/",
    ".",
    "./",
    "..",
    "*",
    "**",
    "***",
    ".\\",
    "..\\",
    "\\",
    "%windir%",
    "%systemroot%",
}


def _norm_target(t: str) -> str:
    return (t or "").strip().strip("\"'").lower().rstrip("/\\") or "/"


def _classify_argv(argv: list[str], raw_command: str) -> GateDecision:
    """Unwrapped argv -> verdict. Har dangerous rule ke saath human-readable reason."""
    if not argv:
        return GateDecision(False, severity=SAFE, command=raw_command)
    prog = _basename(argv[0])
    args = argv[1:]
    low_args = [a.lower() for a in args]
    targets = _targets(args)

    def bad(reason: str) -> GateDecision:
        return GateDecision(True, reason=reason, severity=DANGEROUS, command=raw_command)

    def risky(reason: str) -> GateDecision:
        return GateDecision(False, reason=reason, severity=RISKY, command=raw_command)

    # -- shell -c / python -c / powershell -command : inner command ko analyse karo --
    if prog in _SHELL_C_FLAG:
        flag = _SHELL_C_FLAG[prog]
        if prog in ("pwsh", "powershell"):
            joined = " ".join(args).lower()
            if "-encodedcommand" in joined or " -enc " in f" {joined} ":
                return bad("powershell -EncodedCommand (opaque payload, verify nahi ho sakta)")
            inner = _after_flag(args, {"-c", "-command"})
            if inner is not None:
                return _adopt_inner(inner, raw_command, prog)
            return risky(f"{prog} interactive/script use")
        if prog == "cmd":
            inner = _after_flag(args, {"/c", "/k", "-c"})
            if inner is not None:
                return _adopt_inner(inner, raw_command, prog)
            return risky("cmd.exe use")
        if flag and flag in low_args:
            idx = low_args.index(flag)
            inner = args[idx + 1] if idx + 1 < len(args) else ""
            if prog in ("python", "python3", "pythonw"):
                return _classify_python_inline(inner, raw_command)
            if not inner.strip():
                return bad(f"{prog} {flag} with empty script (unparseable)")
            return _adopt_inner(inner, raw_command, prog)
        # bare `bash script.sh` / `python app.py` — normal execution
        if prog in ("perl", "ruby", "php", "node") and args and args[0] in ("-e", "-r"):
            return _adopt_inner(args[1] if len(args) > 1 else "", raw_command, prog)
        return GateDecision(False, severity=SAFE, command=raw_command)

    # -- destructive filesystem --
    if prog == "rm":
        if _has_short_flag(args, "rR") or _has_long_flag(args, {"recursive"}):
            return bad("rm with recursive flag (tree delete)")
        for t in targets:
            if _norm_target(t) in _DANGER_PATHS:
                return bad(f"rm on '{t}' (system/root/home/wildcard target)")
        if any(t.strip() in ("*", "**") for t in targets):
            return bad("rm with bare wildcard (mass delete in cwd)")
        return risky("rm (single-file delete — allowed, not recursive)")
    if prog in ("rmdir",):
        if "/s" in low_args or "/q" in low_args and "/s" in low_args:
            return bad("rmdir /s (recursive delete)")
        return GateDecision(False, severity=SAFE, command=raw_command)
    if prog in ("del", "erase"):
        if "/s" in low_args:
            return bad("del /s (recursive delete)")
        return risky("del (single-file delete)")
    if prog == "rd":
        if "/s" in low_args:
            return bad("rd /s (recursive delete)")
        return GateDecision(False, severity=SAFE, command=raw_command)
    if prog in ("remove-item", "ri", "rm-item"):
        if (
            _has_short_flag(args, "rR")
            or _has_long_flag(args, {"recurse", "recursive"})
            or any(a.lower().startswith("-recurs") for a in args)
        ):
            return bad("Remove-Item -Recurse (tree delete)")
        return risky("Remove-Item (single delete)")
    if prog in ("shred", "srm"):
        return bad(f"{prog} (secure irreversible delete)")
    if prog in ("mkfs", "mke2fs", "mkdosfs", "mkfs.vfat", "mkfs.ext4", "mkfs.ntfs", "mkswap", "wipefs", "diskpart"):
        return bad(f"{prog} (disk/filesystem destructive)")
    if prog in ("format", "format-volume"):
        return bad(f"{prog} (disk format)")
    if prog == "dd":
        for a in args:
            if a.lower().startswith("of="):
                dest = a[3:]
                if dest.lower() not in ("/dev/null", "nul"):
                    return bad(f"dd of={dest} (raw disk/image write)")
        return GateDecision(False, severity=SAFE, command=raw_command)
    if prog in ("cryptsetup",) and any(a.lower() in ("luksformat", "erase") for a in args):
        return bad("cryptsetup luksFormat/erase (disk wipe)")
    if prog in ("lvremove", "vgremove", "pvremove"):
        return bad(f"{prog} (LVM volume destroy)")
    if prog == "zfs" and "destroy" in low_args:
        return bad("zfs destroy (dataset destroy)")

    # -- power / reboot --
    if prog in ("shutdown", "reboot", "halt", "poweroff", "restart-computer", "stop-computer"):
        return bad(f"{prog} (shutdown/reboot)")
    if prog == "init" and targets and targets[0] in ("0", "6"):
        return bad("init 0/6 (halt/reboot)")
    if prog == "systemctl" and targets and targets[0] in ("poweroff", "reboot", "halt"):
        return bad(f"systemctl {targets[0]}")
    if prog == "loginctl" and targets and targets[0] in ("poweroff", "reboot"):
        return bad(f"loginctl {targets[0]}")

    # -- kill (shell me ownership verify nahi ho sakta -> foreign maan ke gate) --
    if prog in (
        "kill",
        "killall",
        "pkill",
        "pgrep",
        "taskkill",
        "tskill",
        "stop-process",
        "skill",
        "killall5",
        "pkill9",
    ):
        if prog == "pgrep":
            return GateDecision(False, severity=SAFE, command=raw_command)
        return bad(f"{prog} (process kill — ownership shell me verify nahi ho sakta)")

    # -- recursive permission changes --
    if prog in ("chmod", "chown", "chgrp", "cacls", "icacls"):
        if _has_short_flag(args, "rR") or _has_long_flag(args, {"recursive"}) or "/t" in low_args:
            return bad(f"{prog} recursive (permission/ownership tree change)")
        if prog in ("cacls", "icacls") and any(a.lower() in ("/deny", "/remove", "/reset") for a in args):
            return bad(f"{prog} {' '.join(args)} (ACL destructive change)")
        return GateDecision(False, severity=SAFE, command=raw_command)

    # -- git destructive --
    if prog == "git":
        sub = args[0].lower() if args else ""
        rest = args[1:]
        if sub == "reset" and "--hard" in [a.lower() for a in rest]:
            return bad("git reset --hard (working tree wipe)")
        if sub == "clean" and _has_short_flag(rest, "f"):
            return bad("git clean -f (untracked files wipe)")
        if sub == "push" and ("-f" in rest or "--force" in [a.lower() for a in rest]):
            return bad("git push --force (history rewrite on remote)")
        if sub == "branch" and "-D" in rest:
            return bad("git branch -D (force branch delete)")
        if sub == "stash" and rest and rest[0].lower() == "clear":
            return bad("git stash clear (stash wipe)")
        return GateDecision(False, severity=SAFE, command=raw_command)

    # -- SQL destructive (client + statement in one command) --
    if prog in ("sqlite3", "mysql", "mariadb", "psql", "sqlcmd", "isql", "sqlite"):
        blob = " ".join(args).upper()
        if "DROP " in f"{blob} " or blob.strip().startswith("DROP") or " DROP" in blob:
            return bad(f"{prog} with DROP (schema/object delete)")
        if "TRUNCATE" in blob:
            return bad(f"{prog} with TRUNCATE (table wipe)")
        if "DELETE" in blob and "WHERE" not in blob:
            return bad(f"{prog} with DELETE-without-WHERE (mass row delete)")
        return GateDecision(False, severity=SAFE, command=raw_command)

    # -- containers --
    if prog in ("docker", "podman", "nerdctl"):
        if len(args) >= 2 and args[0].lower() == "system" and args[1].lower() == "prune":
            return bad(f"{prog} system prune (images/containers/volumes wipe)")
        if args and args[0].lower() == "volume" and len(args) > 1 and args[1].lower() == "prune":
            return bad(f"{prog} volume prune (volume wipe)")
        return GateDecision(False, severity=SAFE, command=raw_command)

    # -- package removal --
    if prog in (
        "apt",
        "apt-get",
        "yum",
        "dnf",
        "microdnf",
        "apk",
        "pacman",
        "zypper",
        "emerge",
        "brew",
        "port",
        "snap",
        "flatpak",
        "choco",
        "chocolatey",
        "winget",
        "scoop",
    ):
        verbs = {"remove", "purge", "uninstall", "erase", "delete", "del", "rm", "autoremove", "-r", "-rs", "-rns"}
        if any(a.lower() in verbs for a in args):
            return bad(f"{prog} package removal")
        return GateDecision(False, severity=SAFE, command=raw_command)
    if prog in ("pip", "pip3", "uv"):
        if args and args[0].lower() == "uninstall":
            return bad("pip uninstall (package removal)")
        return GateDecision(False, severity=SAFE, command=raw_command)
    if prog == "npm" and args and args[0].lower() in ("uninstall", "un", "remove", "rm"):
        return bad("npm uninstall/remove (package removal)")
    if prog == "yarn" and args and args[0].lower() == "remove":
        return bad("yarn remove (package removal)")
    if prog == "pnpm" and args and args[0].lower() in ("remove", "uninstall", "un", "rm"):
        return bad("pnpm remove (package removal)")
    if prog == "gem" and args and args[0].lower() == "uninstall":
        return bad("gem uninstall (package removal)")
    if prog == "cargo" and args and args[0].lower() == "uninstall":
        return bad("cargo uninstall (package removal)")
    if prog == "composer" and args and args[0].lower() == "remove":
        return bad("composer remove (package removal)")
    if prog == "helm" and args and args[0].lower() == "uninstall":
        return bad("helm uninstall (release removal)")
    if (
        prog == "kubectl"
        and args
        and args[0].lower() == "delete"
        and any(a in ("--all", "-A", "--all-namespaces") for a in args)
    ):
        return bad("kubectl delete --all (cluster-wide delete)")

    # -- cloud / infra destructive --
    if prog == "aws" and len(args) >= 2:
        svc, op = args[0].lower(), args[1].lower()
        if svc == "s3" and ((op == "rb" and "--force" in low_args) or (op == "rm" and "--recursive" in low_args)):
            return bad(f"aws s3 {op} (bucket/object mass delete)")
    if prog == "terraform" and args and (args[0].lower() == "destroy" or "-destroy" in low_args):
        return bad("terraform destroy (infra wipe)")

    # -- windows destructive admin --
    if prog == "sc" and args and args[0].lower() == "delete":
        return bad("sc delete (service delete)")
    if prog == "reg" and args and args[0].lower() == "delete":
        return bad("reg delete (registry delete)")
    if prog == "schtasks" and "/delete" in low_args:
        return bad("schtasks /delete (task delete)")
    if prog == "netsh" and any(a.lower() in ("delete", "reset") for a in args):
        return bad("netsh delete/reset (network config destructive)")
    if prog == "bcdedit" and "/delete" in low_args:
        return bad("bcdedit /delete (boot entry delete)")
    if prog == "vssadmin" and "delete" in low_args:
        return bad("vssadmin delete (shadow copy delete)")
    if prog == "wbadmin" and "delete" in low_args:
        return bad("wbadmin delete (backup delete)")
    if prog == "wmic" and "delete" in low_args:
        return bad("wmic delete")
    if prog == "cipher" and "/w" in low_args:
        return bad("cipher /w (free-space wipe)")

    # -- firewall flush --
    if prog == "iptables" and any(a in ("-F", "-X", "-Z", "--flush") for a in args):
        return bad("iptables flush (firewall rules wipe)")
    if prog == "nft" and "flush" in low_args:
        return bad("nft flush (firewall rules wipe)")
    if prog == "ufw" and args and args[0].lower() in ("disable", "reset"):
        return bad(f"ufw {args[0]} (firewall off/reset)")

    # -- eval with downloader --
    if prog == "eval":
        import re as _re

        blob = " ".join(args).lower()
        if any(_re.search(rf"(?<![a-z0-9_-]){d}(?![a-z0-9_-])", blob) for d in _DOWNLOADERS):
            return bad("eval of downloaded content (remote code execution)")

    # -- block-device targets for copy-ish tools --
    if prog in ("cp", "mv", "copy", "move", "tee", "install"):
        for t in targets:
            if _is_block_device(t):
                return bad(f"{prog} -> {t} (write to block device)")
        return GateDecision(False, severity=SAFE, command=raw_command)

    # -- find (plain) --
    if prog == "find":
        return GateDecision(False, severity=SAFE, command=raw_command)

    return GateDecision(False, severity=SAFE, command=raw_command)


def _after_flag(args: list[str], flags: set[str]) -> str | None:
    low = [a.lower() for a in args]
    for i, a in enumerate(low):
        if a in flags and i + 1 < len(args):
            return args[i + 1]
    return None


def _adopt_inner(inner: str, raw_command: str, wrapper: str) -> GateDecision:
    inner_decision = classify_shell(inner)
    if inner_decision.dangerous:
        return GateDecision(
            True, reason=f"{wrapper} -c wraps: {inner_decision.reason}", severity=DANGEROUS, command=raw_command
        )
    if inner_decision.severity == RISKY:
        return GateDecision(
            False, reason=f"{wrapper} -c wraps: {inner_decision.reason}", severity=RISKY, command=raw_command
        )
    return GateDecision(False, severity=SAFE, command=raw_command)


def _classify_python_inline(code: str, raw_command: str) -> GateDecision:
    """`python -c "code"` — shell-outs + rmtree scan, extracted commands re-classified."""
    import re as _re

    if "shutil" in code and "rmtree" in code:
        return GateDecision(
            True, reason="python -c with shutil.rmtree (tree delete)", severity=DANGEROUS, command=raw_command
        )
    inner_cmds: list[str] = []
    for pat in (
        r"os\.system\(\s*[\"'](.*?)[\"']\s*\)",
        r"os\.popen\(\s*[\"'](.*?)[\"']\s*\)",
        r"subprocess\.\w+\(\s*[\"'](.*?)[\"']\s*[,)]",
    ):
        try:
            inner_cmds.extend(_re.findall(pat, code, flags=_re.S))
        except Exception:
            pass
    for cmd in inner_cmds:
        d = classify_shell(cmd)
        if d.dangerous:
            return GateDecision(
                True, reason=f"python -c shell-out is dangerous: {d.reason}", severity=DANGEROUS, command=raw_command
            )
    if inner_cmds:
        return GateDecision(
            False, reason="python -c with shell-out (checked, not dangerous)", severity=RISKY, command=raw_command
        )
    return GateDecision(False, severity=SAFE, command=raw_command)


_BLOCK_DEV_PREFIXES = (
    "/dev/sd",
    "/dev/hd",
    "/dev/nvme",
    "/dev/vd",
    "/dev/xvd",
    "/dev/mmcblk",
    "/dev/loop",
    "/dev/dm-",
    "/dev/md",
    "/dev/disk",
    "\\\\.\\physicaldrive",
)


def _is_block_device(target: str) -> bool:
    t = (target or "").strip().lower()
    return t.startswith(_BLOCK_DEV_PREFIXES)


def classify_shell(command: str) -> GateDecision:
    """Full command line -> GateDecision. Unparseable = dangerous (fail-closed)."""
    raw = command or ""
    text = raw.strip()
    if not text:
        return GateDecision(False, severity=SAFE, command=raw)
    low = text.lower()

    # fork bomb + raw-text tells (before parsing)
    if ":(){ " in text.replace("\t", " ") or ":(){" in text:
        return GateDecision(True, reason="fork bomb pattern :(){...}", severity=DANGEROUS, command=raw)
    if "shutil" in low and "rmtree" in low:
        return GateDecision(True, reason="shutil.rmtree in command (tree delete)", severity=DANGEROUS, command=raw)
    # redirection writes to block devices: `> /dev/sda`, `2>>/dev/nvme0n1` ...
    import re as _re

    for m in _re.finditer(r"(?:^|[\s;|&])(?:\d{0,2}\s*>>?\s*)([^\s;|&]+)", text):
        dest = m.group(1).strip("\"'")
        if _is_block_device(dest):
            return GateDecision(True, reason=f"redirect write to block device {dest}", severity=DANGEROUS, command=raw)
    # process substitution executing downloads: bash <(curl ...), source <(wget ...)
    if "<(" in text and any(d in low for d in ("curl", "wget", "aria2c", "fetch", "iwr")):
        return GateDecision(
            True, reason="process substitution runs downloaded content", severity=DANGEROUS, command=raw
        )

    try:
        segments = _split_segments(text)
    except ValueError as exc:
        return GateDecision(True, reason=f"unparseable command ({exc})", severity=DANGEROUS, command=raw)
    if not segments:
        return GateDecision(False, severity=SAFE, command=raw)

    parsed: list[list[str]] = []
    for seg in segments:
        try:
            parsed.append(_parse_segment(seg))
        except ValueError as exc:
            return GateDecision(
                True, reason=f"unparseable segment ({exc}): {seg[:60]}", severity=DANGEROUS, command=raw
            )

    # pipe-to-shell: downloader | shell  (segments joined by `|` in source — best effort:
    # check adjacent parsed segments when raw text has a pipe between them)
    if "|" in text:
        for i in range(len(parsed) - 1):
            try:
                left, _ = _strip_wrappers(parsed[i])
                right, _ = _strip_wrappers(parsed[i + 1])
            except Exception:
                continue
            if not left or not right:
                continue
            if _basename(left[0]) in _DOWNLOADERS and _basename(right[0]) in _SHELLS:
                return GateDecision(
                    True, reason=f"piped download into shell ({left[0]} | {right[0]})", severity=DANGEROUS, command=raw
                )

    worst_risky = ""
    for argv in parsed:
        try:
            unwrapped, note = _strip_wrappers(argv)
        except Exception:
            return GateDecision(True, reason="unparseable segment after unwrap", severity=DANGEROUS, command=raw)
        if note:  # e.g. find -delete surfaced during unwrap
            return GateDecision(True, reason=note, severity=DANGEROUS, command=raw)
        if not unwrapped:
            continue
        decision = _classify_argv(unwrapped, raw)
        if decision.dangerous:
            return decision
        if decision.severity == RISKY and not worst_risky:
            worst_risky = decision.reason
    if worst_risky:
        return GateDecision(False, reason=worst_risky, severity=RISKY, command=raw)
    return GateDecision(False, severity=SAFE, command=raw)


def is_dangerous_shell(command: str) -> bool:
    return classify_shell(command).dangerous


# ---------------------------------------------------------------------------
# approval plumbing — one request, delivered to the originating surface
# ---------------------------------------------------------------------------

AskerFn = Callable[["ApprovalRequest"], str]  # returns "yes" | "no" | "always"


@dataclass
class ApprovalRequest:
    id: str
    command: str
    why_flagged: str
    cwd: str
    surface: str
    kind: str = "shell"  # shell | overwrite | delete | kill | restart | run_tool
    timeout_s: int = DEFAULT_TIMEOUT_S
    created_at: float = field(default_factory=time.time)
    status: str = "pending"  # pending | approved | denied | expired
    decided_by: str = ""
    decide_note: str = ""

    @property
    def expires_at(self) -> float:
        return self.created_at + self.timeout_s

    @property
    def expired(self) -> bool:
        return self.status == "pending" and time.time() > self.expires_at

    def short_command(self, limit: int = 300) -> str:
        cmd = self.command or ""
        return cmd if len(cmd) <= limit else cmd[:limit] + "…"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "command": self.short_command(),
            "why_flagged": self.why_flagged,
            "cwd": self.cwd,
            "surface": self.surface,
            "kind": self.kind,
            "status": self.status,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "decided_by": self.decided_by,
        }


@dataclass
class ApprovalResult:
    approved: bool
    reason: str = ""
    via: str = ""  # kaunsi surface/asker ne decide kiya
    always: bool = False  # "always allow this exact command" chuna gaya


@dataclass
class GateCheck:
    allowed: bool
    message: str = ""  # deny par user-facing Hinglish reason
    decision: GateDecision | None = None
    mode: str = "risky"
    approval_id: str = ""


class ApprovalBroker:
    """Central broker — approvals mangta hai, surfaces unhe answer karte hain."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._pending: dict[str, ApprovalRequest] = {}
        self._events: dict[str, threading.Event] = {}
        self._askers: dict[str, AskerFn] = {}
        self._decisions: list[dict[str, Any]] = []

    # -- askers ----------------------------------------------------------
    def register_asker(self, surface_key: str, fn: AskerFn | None) -> None:
        with self._lock:
            if fn is None:
                self._askers.pop(surface_key, None)
            else:
                self._askers[surface_key] = fn

    def asker_for(self, surface: str) -> tuple[str, AskerFn | None]:
        s = (surface or "").lower()
        with self._lock:
            if s in self._askers:
                return s, self._askers[s]
            for key in ("cli", "telegram", "voice", "web", "api", "automation"):
                if s.startswith(key) and key in self._askers:
                    return key, self._askers[key]
        return "", None

    # -- pending store ---------------------------------------------------
    def pending(self) -> list[ApprovalRequest]:
        with self._lock:
            self._expire_locked()
            return [r for r in self._pending.values() if r.status == "pending"]

    def pending_for(self, surface: str) -> list[ApprovalRequest]:
        s = (surface or "").lower()
        return [
            r
            for r in self.pending()
            if r.surface.lower() == s or r.surface.lower().startswith(s) or s.startswith(r.surface.lower())
        ]

    def get(self, approval_id: str) -> ApprovalRequest | None:
        with self._lock:
            return self._pending.get(approval_id)

    def _expire_locked(self) -> None:
        now = time.time()
        for req in self._pending.values():
            if req.status == "pending" and now > req.expires_at:
                req.status = "expired"
                req.decide_note = "timeout — koi jawab nahi aaya"
                ev = self._events.get(req.id)
                if ev is not None:
                    ev.set()

    # -- main flow -------------------------------------------------------
    def request(
        self,
        command: str,
        why: str,
        cwd: str = "",
        surface: str = "cli",
        kind: str = "shell",
        timeout_s: int = DEFAULT_TIMEOUT_S,
    ) -> ApprovalResult:
        req = ApprovalRequest(
            id=uuid.uuid4().hex[:8],
            command=command,
            why_flagged=why,
            cwd=cwd or "",
            surface=surface,
            kind=kind,
            timeout_s=timeout_s,
        )
        ev = threading.Event()
        with self._lock:
            self._pending[req.id] = req
            self._events[req.id] = ev
        try:
            _, asker = self.asker_for(surface)
            if asker is None:
                if surface.lower().startswith("automation"):
                    asker = self._automation_wait
                else:
                    self._decide(
                        req.id,
                        approved=False,
                        by="broker",
                        note=f"surface '{surface}' par poochne ka rasta nahi hai (asker missing)",
                    )
                    return ApprovalResult(False, reason=req.decide_note, via="broker")
            box: dict[str, Any] = {}

            def _run() -> None:
                try:
                    box["answer"] = asker(req) or "no"
                except Exception as exc:
                    box["answer"] = "no"
                    box["error"] = f"{type(exc).__name__}: {exc}"

            worker = threading.Thread(target=_run, name=f"gate-ask-{req.id}", daemon=True)
            worker.start()
            # asker ka jawab AAYE ya event set ho (web/modal resolve) ya timeout
            deadline = time.time() + timeout_s
            while time.time() < deadline:
                if ev.wait(timeout=min(0.2, max(0.01, deadline - time.time()))):
                    break
                if "answer" in box:
                    break
            if req.status != "pending":
                # kisi aur raste se decide ho gaya (web modal / telegram button)
                return ApprovalResult(
                    req.status == "approved", reason=req.decide_note or req.status, via=req.decided_by
                )
            answer = str(box.get("answer", "") or "").strip().lower()
            if "answer" not in box:
                self._decide(
                    req.id, approved=False, by=surface, note=f"timeout ({timeout_s}s) — koi jawab nahi, cancel kiya"
                )
                return ApprovalResult(False, reason=req.decide_note, via=surface)
            if answer in ("yes", "y", "haan", "ha", "ok", "run", "approve", "approved"):
                self._decide(req.id, approved=True, by=surface, note="owner ne haan bola")
                return ApprovalResult(True, via=surface)
            if answer in ("always", "always-allow", "hamesha"):
                self._decide(req.id, approved=True, by=surface, note="owner ne hamesha-allow chuna")
                _allowlist_add(command, cwd)
                return ApprovalResult(True, via=surface, always=True)
            note = str(box.get("error") or "owner ne mana kiya / cancel")
            self._decide(req.id, approved=False, by=surface, note=note)
            return ApprovalResult(False, reason=note, via=surface)
        finally:
            with self._lock:
                self._events.pop(req.id, None)

    def _automation_wait(self, req: ApprovalRequest) -> str:
        """Automation surface: pending me rakho, web-modal/Telegram resolve ka wait karo."""
        _, tg = self.asker_for("telegram")
        if tg is not None:
            try:
                return str(tg(req) or "no")
            except Exception:
                pass
        # warna timeout tak pending (web modal se approve ho sakta hai)
        ev = self._events.get(req.id)
        if ev is not None:
            ev.wait(timeout=req.timeout_s)
            if req.status == "approved":
                return "yes"
        return "no"

    def _decide(self, approval_id: str, approved: bool, by: str, note: str = "") -> bool:
        """Single-use decide — pehla decision final, baaki ignore. Returns applied?"""
        with self._lock:
            req = self._pending.get(approval_id)
            if req is None or req.status != "pending":
                return False
            req.status = "approved" if approved else "denied"
            req.decided_by = by
            req.decide_note = note
            self._decisions.append(
                {"id": req.id, "approved": approved, "by": by, "at": time.time(), "command": req.short_command(120)}
            )
            self._decisions = self._decisions[-50:]
            ev = self._events.get(approval_id)
            if ev is not None:
                ev.set()
            return True

    def resolve(self, approval_id: str, approved: bool, by: str = "human", note: str = "") -> bool:
        """Surface callbacks (Telegram button / web modal) yahi se resolve karte hain."""
        with self._lock:
            req = self._pending.get(approval_id)
            if req is None:
                return False
            if req.status != "pending":
                return False  # single-use: dobara decide nahi
            if req.expired:
                req.status = "expired"
                req.decide_note = "expired — timeout ke baad click"
                ev = self._events.get(approval_id)
                if ev is not None:
                    ev.set()
                return False
        return self._decide(approval_id, approved=approved, by=by, note=note or ("approved" if approved else "denied"))

    def recent_decisions(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._decisions[-limit:])


_BROKER: ApprovalBroker | None = None
_BROKER_LOCK = threading.Lock()


def get_broker() -> ApprovalBroker:
    global _BROKER
    with _BROKER_LOCK:
        if _BROKER is None:
            _BROKER = ApprovalBroker()
            _BROKER.register_asker("cli", ask_cli)
        return _BROKER


def reset_broker_for_tests() -> ApprovalBroker:
    """Tests ke liye fresh broker (askers + pending clear, cli asker default)."""
    global _BROKER
    with _BROKER_LOCK:
        _BROKER = ApprovalBroker()
        _BROKER.register_asker("cli", ask_cli)
        return _BROKER


def register_asker(surface_key: str, fn: AskerFn | None) -> None:
    get_broker().register_asker(surface_key, fn)


# ---------------------------------------------------------------------------
# runtime context — kaunsi surface par chal raha hai
# ---------------------------------------------------------------------------
_surface_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("nikhilai_gate_surface", default="")
_asker_ctx: contextvars.ContextVar[Any] = contextvars.ContextVar("nikhilai_gate_asker", default=None)


class gate_context:
    """`with gate_context('telegram_owner', asker=...):` — is block ke gate checks isi surface par."""

    def __init__(self, surface: str, asker: AskerFn | None = None):
        self.surface = surface
        self.asker = asker
        self._t1: contextvars.Token | None = None
        self._t2: contextvars.Token | None = None

    def __enter__(self) -> gate_context:
        self._t1 = _surface_ctx.set(self.surface)
        if self.asker is not None:
            self._t2 = _asker_ctx.set(self.asker)
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._t1 is not None:
            _surface_ctx.reset(self._t1)
        if self._t2 is not None:
            _asker_ctx.reset(self._t2)


def current_surface(default: str = "cli") -> str:
    return _surface_ctx.get() or default


def _context_asker() -> AskerFn | None:
    fn = _asker_ctx.get()
    return fn if callable(fn) else None


# ---------------------------------------------------------------------------
# modes + allowlist
# ---------------------------------------------------------------------------
_OFF_WARNED = False


def effective_mode() -> str:
    """off | risky | all — runtime override > env. Invalid = risky (fail-closed-ish default)."""
    mode = ""
    try:
        from ..config import runtime as R

        mode = str(R.get("exec_approval", "") or "").strip().lower()
    except Exception:
        mode = ""
    if not mode:
        try:
            from ..config import settings as S

            mode = str(S.EXEC_APPROVAL or "").strip().lower()
        except Exception:
            mode = ""
    if mode not in ("off", "risky", "all"):
        if mode:
            mode = "risky"
        else:
            mode = "risky"
    global _OFF_WARNED
    if mode == "off" and not _OFF_WARNED:
        _OFF_WARNED = True
        print(
            "⚠️  Dangerous-command gate is OFF — destructive commands bina pooche chalenge "
            "(JARVIS_EXEC_APPROVAL=risky karo for safety)"
        )
    return mode


def _allowlist_path() -> str:
    try:
        from ..config import WORKSPACE_DIR

        return str(WORKSPACE_DIR / "gate_allowlist.json")
    except Exception:
        return os.path.join(os.path.expanduser("~"), ".nikhilai_gate_allowlist.json")


def _allow_key(command: str, cwd: str) -> str:
    blob = f"{(cwd or '').strip()}\x00{(command or '').strip()}".encode("utf-8", "ignore")
    return hashlib.sha256(blob).hexdigest()[:32]


def _allowlist_has(command: str, cwd: str) -> bool:
    try:
        path = _allowlist_path()
        if not os.path.exists(path):
            return False
        data = json.loads(open(path, encoding="utf-8").read() or "{}")
        return _allow_key(command, cwd) in (data.get("allowed") or {})
    except Exception:
        return False


def _allowlist_add(command: str, cwd: str) -> None:
    try:
        path = _allowlist_path()
        try:
            data = json.loads(open(path, encoding="utf-8").read() or "{}")
        except Exception:
            data = {}
        allowed = data.get("allowed") or {}
        allowed[_allow_key(command, cwd)] = {"command": (command or "")[:400], "cwd": cwd or "", "at": time.time()}
        data["allowed"] = allowed
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# default askers
# ---------------------------------------------------------------------------
def ask_cli(req: ApprovalRequest) -> str:
    """CLI: terminal par y/N/always poocho (non-interactive = auto no)."""
    try:
        if not sys.stdin.isatty():
            return "no"
    except Exception:
        return "no"
    print(f"\n🔐 DANGEROUS — approval chahiye (surface: {req.surface}, 60s timeout):")
    print(f"   $ {req.short_command(400)}")
    if req.cwd:
        print(f"   cwd: {req.cwd}")
    print(f"   wajah: {req.why_flagged}")
    box: dict[str, str] = {}

    def _read() -> None:
        try:
            box["v"] = input("   Chalaoon? [y/N/always] ").strip().lower()
        except Exception:
            box["v"] = "no"

    t = threading.Thread(target=_read, name="gate-cli-input", daemon=True)
    t.start()
    t.join(timeout=req.timeout_s)
    v = box.get("v", "")
    if not v:
        print("   ⏱️ timeout — cancel kiya.")
        return "no"
    if v in ("y", "yes", "haan", "ha", "ok", "run"):
        return "yes"
    if v in ("always", "hamesha", "a"):
        return "always"
    return "no"


# ---------------------------------------------------------------------------
# public checks — tools/surfaces yahi call karte hain
# ---------------------------------------------------------------------------
def check_shell(
    command: str, cwd: str = "", surface: str | None = None, timeout_s: int = DEFAULT_TIMEOUT_S
) -> GateCheck:
    """Shell command chalane se pehle gate. Returns GateCheck(allowed, message, ...)."""
    mode = effective_mode()
    decision = classify_shell(command)
    surf = surface or current_surface()
    if mode == "off":
        return GateCheck(True, mode=mode, decision=decision)
    needs_ask = decision.dangerous or mode == "all"
    if not needs_ask:
        return GateCheck(True, mode=mode, decision=decision)
    if _allowlist_has(command, cwd):
        return GateCheck(True, mode=mode, decision=decision, message="allowlisted exact command — gate skipped")
    why = decision.reason if decision.dangerous else f"mode=all — har command par approval ({command[:80]})"
    asker = _context_asker()
    broker = get_broker()
    if asker is not None:
        # test/surface-injected asker — seedha poocho, broker plumbing skip
        req = ApprovalRequest(
            id=uuid.uuid4().hex[:8],
            command=command,
            why_flagged=why,
            cwd=cwd or "",
            surface=surf,
            kind="shell",
            timeout_s=timeout_s,
        )
        try:
            answer = str(asker(req) or "no").strip().lower()
        except Exception as exc:
            answer = "no"
            why2 = f"asker crash: {type(exc).__name__}"
            return GateCheck(False, message=f"🔐 Gate: {why2} — cancel kiya.", decision=decision, mode=mode)
        if answer in ("yes", "y", "haan", "ha", "ok", "run", "approve", "approved"):
            return GateCheck(True, mode=mode, decision=decision, approval_id=req.id)
        if answer in ("always", "always-allow", "hamesha"):
            _allowlist_add(command, cwd)
            return GateCheck(True, mode=mode, decision=decision, approval_id=req.id)
        return GateCheck(
            False,
            decision=decision,
            mode=mode,
            approval_id=req.id,
            message=f"🔐 Cancel — owner ne approval nahi diya ({why}). Command nahi chali.",
        )
    res = broker.request(command, why, cwd=cwd, surface=surf, kind="shell", timeout_s=timeout_s)
    if res.approved:
        return GateCheck(True, mode=mode, decision=decision)
    return GateCheck(
        False,
        decision=decision,
        mode=mode,
        message=f"🔐 Cancel — {res.reason or 'approval nahi mili'} ({why}). Command nahi chali.",
    )


_ACTION_REASONS = {
    "overwrite": "non-empty file overwrite (data loss)",
    "delete": "file/folder delete (data loss)",
    "kill": "kill/close of a process Jarvis did not start",
    "restart": "Jarvis self-restart (process replace)",
    "run_tool": "tool execution needs confirmation",
}


def check_action(
    kind: str, description: str, surface: str | None = None, cwd: str = "", timeout_s: int = DEFAULT_TIMEOUT_S
) -> GateCheck:
    """Non-shell dangerous actions (overwrite/delete/kill/restart//run) ke liye gate."""
    mode = effective_mode()
    surf = surface or current_surface()
    why = _ACTION_REASONS.get(kind, f"{kind} needs approval")
    decision = GateDecision(True, reason=why, severity=DANGEROUS, command=description)
    if mode == "off":
        return GateCheck(True, mode=mode, decision=decision)
    # in risky mode ye actions hamesha dangerous hain; in all mode bhi poocho (same path)
    if _allowlist_has(f"{kind}:{description}", cwd):
        return GateCheck(True, mode=mode, decision=decision)
    asker = _context_asker()
    broker = get_broker()
    if asker is not None:
        req = ApprovalRequest(
            id=uuid.uuid4().hex[:8],
            command=description,
            why_flagged=why,
            cwd=cwd or "",
            surface=surf,
            kind=kind,
            timeout_s=timeout_s,
        )
        try:
            answer = str(asker(req) or "no").strip().lower()
        except Exception as exc:
            return GateCheck(
                False, message=f"🔐 Gate: asker crash ({type(exc).__name__}) — cancel.", decision=decision, mode=mode
            )
        if answer in ("yes", "y", "haan", "ha", "ok", "run", "approve", "approved"):
            return GateCheck(True, mode=mode, decision=decision, approval_id=req.id)
        if answer in ("always", "always-allow", "hamesha"):
            _allowlist_add(f"{kind}:{description}", cwd)
            return GateCheck(True, mode=mode, decision=decision, approval_id=req.id)
        return GateCheck(
            False,
            decision=decision,
            mode=mode,
            approval_id=req.id,
            message=f"🔐 Cancel — owner ne approval nahi diya ({why}). Kaam nahi hua.",
        )
    res = broker.request(description, why, cwd=cwd, surface=surf, kind=kind, timeout_s=timeout_s)
    if res.approved:
        return GateCheck(True, mode=mode, decision=decision)
    return GateCheck(
        False,
        decision=decision,
        mode=mode,
        message=f"🔐 Cancel — {res.reason or 'approval nahi mili'} ({why}). Kaam nahi hua.",
    )
