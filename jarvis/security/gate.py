"""Human-only approval gate for destructive or explicitly gated commands.

The model never receives an approval token. A request is held server-side, bound to
its exact command and working directory, and resolved once by the owning surface.
"""
from __future__ import annotations

import ast
import asyncio
import base64
import contextvars
import inspect
import os
import re
import shlex
import sys
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from ..config import settings as S


@dataclass(frozen=True)
class CommandAssessment:
    dangerous: bool
    reason: str = ""
    parseable: bool = True


@dataclass(frozen=True)
class ApprovalRequest:
    id: str
    command: str
    why_flagged: str
    cwd: str
    surface: str
    expires_at: float


@dataclass(frozen=True)
class ApprovalResult:
    allowed: bool
    reason: str
    request_id: str = ""


ApprovalHandler = Callable[[ApprovalRequest], Awaitable[bool | None] | bool | None]


class _NestedDangerous(ValueError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


_handler: contextvars.ContextVar[ApprovalHandler | None] = contextvars.ContextVar(
    "jarvis_approval_handler", default=None
)
_surface_handlers: dict[str, ApprovalHandler] = {}
CompletionHandler = Callable[[str, str, str], Awaitable[None] | None]
_completion_handlers: dict[str, CompletionHandler] = {}


def register_approval_completion_handler(surface: str, handler: CompletionHandler | None) -> None:
    key = str(surface or "").strip()
    if not key:
        raise ValueError("approval surface is required")
    if handler is None:
        _completion_handlers.pop(key, None)
    else:
        _completion_handlers[key] = handler


async def report_approval_completion(request_id: str, surface: str, status: str, result: str) -> None:
    handler = _completion_handlers.get(str(surface or ""))
    if handler is None or not request_id:
        return
    try:
        outcome = handler(str(request_id), str(status), str(result))
        if inspect.isawaitable(outcome):
            await outcome
    except Exception:
        return


def register_approval_handler(surface: str, handler: ApprovalHandler | None) -> None:
    """Register a server-side prompt/notification callback for one owner surface."""
    key = str(surface or "").strip()
    if not key:
        raise ValueError("approval surface is required")
    if handler is None:
        _surface_handlers.pop(key, None)
    else:
        _surface_handlers[key] = handler


@contextmanager
def use_approval_handler(handler: ApprovalHandler | None) -> Iterator[None]:
    """Temporarily bind an approval handler to the current async request context."""
    token = _handler.set(handler)
    try:
        yield
    finally:
        _handler.reset(token)


def _split_shell_segments(command: str) -> list[tuple[str, str]]:
    """Split shell control operators outside quotes; operator belongs to next segment."""
    segments: list[tuple[str, str]] = []
    buf: list[str] = []
    quote = ""
    escaped = False
    comment = False
    preceding = ""
    i = 0
    while i < len(command):
        ch = command[i]
        if comment:
            if ch in "\r\n":
                comment = False
                text = "".join(buf).strip()
                if text:
                    segments.append((preceding, text))
                    buf.clear()
                    preceding = ";"
            i += 1
            continue
        if escaped:
            buf.append(ch)
            escaped = False
            i += 1
            continue
        if ch == "\\" and quote != "'":
            buf.append(ch)
            escaped = True
            i += 1
            continue
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in {"'", '"'}:
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "#" and (i == 0 or command[i - 1].isspace()):
            comment = True
            i += 1
            continue
        if ch in "\r\n":
            text = "".join(buf).strip()
            if text:
                segments.append((preceding, text))
                buf.clear()
                preceding = ";"
            i += 1
            continue
        # Parentheses/braces can group executable commands. Split around them so
        # `(rm -rf x)` and `{ rm -rf x; }` are parsed like ordinary segments.
        if ch in "(){}":
            text = "".join(buf).strip()
            if text:
                segments.append((preceding, text))
                buf.clear()
                preceding = ";"
            i += 1
            continue
        op = ""
        for candidate in ("&&", "||", "|&", ";", "|", "&"):
            if command.startswith(candidate, i):
                op = candidate
                break
        if op:
            text = "".join(buf).strip()
            if text:
                segments.append((preceding, text))
                buf.clear()
            preceding = op
            i += len(op)
            continue
        buf.append(ch)
        i += 1
    if quote or escaped:
        raise ValueError("unterminated quote or escape")
    text = "".join(buf).strip()
    if text:
        segments.append((preceding, text))
    return segments


def _tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment, posix=True)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc


def _pipeline_executable(tokens: list[str]) -> str:
    """Return the actual command after common leading wrappers, without unwrapping -c."""
    items = list(tokens)
    while items:
        exe = items[0].rsplit("/", 1)[-1].lower().removesuffix(".exe")
        rest = items[1:]
        if exe in {"sudo", "doas", "nohup", "setsid"}:
            idx = 0
            takes_value = {"-u", "--user", "-g", "--group", "-h", "--host", "-p", "--prompt"}
            while idx < len(rest):
                if rest[idx] == "--":
                    idx += 1
                    break
                if rest[idx] in takes_value:
                    idx += 2
                elif rest[idx].startswith("-"):
                    idx += 1
                else:
                    break
            items = rest[idx:]
            continue
        if exe == "env":
            idx = 0
            while idx < len(rest) and (rest[idx].startswith("-") or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", rest[idx])):
                if rest[idx] in {"-u", "--unset", "-C", "--chdir"} and idx + 1 < len(rest):
                    idx += 2
                else:
                    idx += 1
            items = rest[idx:]
            continue
        if exe == "nice":
            idx = 0
            while idx < len(rest) and rest[idx].startswith("-"):
                idx += 2 if rest[idx] in {"-n", "--adjustment"} and idx + 1 < len(rest) else 1
            items = rest[idx:]
            continue
        if exe == "xargs":
            idx = 1
            takes_value = {"-I", "-J", "-L", "-n", "-P", "--replace", "--max-args", "--max-procs"}
            while idx < len(items) and items[idx].startswith("-"):
                flag = items[idx]
                idx += 1
                if flag in takes_value and idx < len(items):
                    idx += 1
            items = items[idx:]
            continue
        return exe
    return ""


def _unwrap(tokens: list[str], depth: int = 0) -> list[str]:
    """Remove common command wrappers and recursively unwrap inline shell commands."""
    if depth > 10:
        raise ValueError("nested command wrappers exceeded limit")
    if not tokens:
        return tokens
    executable = tokens[0].rsplit("/", 1)[-1].lower()
    rest = tokens[1:]
    if executable in {"sudo", "doas", "nohup", "setsid"}:
        idx = 0
        takes_value = {"-u", "--user", "-g", "--group", "-h", "--host", "-p", "--prompt", "-C", "--close-from"}
        while idx < len(rest):
            item = rest[idx]
            if item == "--":
                idx += 1
                break
            if item in takes_value:
                idx += 2
            elif item.startswith("-"):
                idx += 1
            else:
                break
        return _unwrap(rest[idx:], depth + 1)
    if executable == "env":
        idx = 0
        while idx < len(rest) and (rest[idx].startswith("-") or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", rest[idx])):
            if rest[idx] in {"-u", "--unset", "-C", "--chdir"} and idx + 1 < len(rest):
                idx += 2
            else:
                idx += 1
        return _unwrap(rest[idx:], depth + 1)
    if executable == "nice":
        idx = 0
        while idx < len(rest) and rest[idx].startswith("-"):
            if rest[idx] in {"-n", "--adjustment"} and idx + 1 < len(rest):
                idx += 2
            else:
                idx += 1
        return _unwrap(rest[idx:], depth + 1)
    if executable in {"bash", "sh", "dash", "zsh", "ksh", "fish"}:
        for idx, flag in enumerate(rest):
            if flag == "-c" or (flag.startswith("-") and "c" in flag[1:]):
                if idx + 1 >= len(rest):
                    raise ValueError("shell -c has no command")
                nested = rest[idx + 1]
                nested_assessment = assess_command(nested)
                if nested_assessment.dangerous:
                    raise _NestedDangerous(nested_assessment.reason)
                nested_segments = _split_shell_segments(nested)
                return _unwrap(_tokens(nested_segments[0][1]), depth + 1) if nested_segments else []
        return _unwrap([item for item in rest if not item.startswith("-")], depth + 1)
    if executable in {"cmd", "cmd.exe"}:
        for idx, flag in enumerate(rest):
            if flag.lower() in {"/c", "/k"}:
                if idx + 1 >= len(rest):
                    raise ValueError("cmd /c has no command")
                nested = " ".join(rest[idx + 1:])
                nested_assessment = assess_command(nested)
                if nested_assessment.dangerous:
                    raise _NestedDangerous(nested_assessment.reason)
                nested_segments = _split_shell_segments(nested)
                return _unwrap(_tokens(nested_segments[0][1]), depth + 1) if nested_segments else []
    if executable in {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}:
        lowered = [item.lower() for item in rest]
        for flag in ("-command", "-c", "-encodedcommand", "-e"):
            if flag in lowered:
                idx = lowered.index(flag)
                if idx + 1 >= len(rest):
                    raise ValueError("PowerShell command is empty")
                nested = rest[idx + 1]
                if flag in {"-encodedcommand", "-e"}:
                    try:
                        nested = base64.b64decode(nested).decode("utf-16le", errors="strict")
                    except Exception as exc:
                        raise ValueError("PowerShell encoded command could not be decoded") from exc
                nested_assessment = assess_command(nested)
                if nested_assessment.dangerous:
                    raise _NestedDangerous(nested_assessment.reason)
                nested_segments = _split_shell_segments(nested)
                return _unwrap(_tokens(nested_segments[0][1]), depth + 1) if nested_segments else []
    if executable == "xargs":
        idx = 1
        options_with_value = {"-I", "-J", "-L", "-n", "-P", "--replace", "--max-args", "--max-procs"}
        while idx < len(tokens) and tokens[idx].startswith("-"):
            flag = tokens[idx]
            idx += 1
            if flag in options_with_value and idx < len(tokens):
                idx += 1
        return _unwrap(tokens[idx:], depth + 1)
    return tokens


def _sql_risk(sql_text: str) -> str:
    for statement in str(sql_text or "").split(";"):
        if re.search(r"\bdelete\s+from\b", statement, re.I) and not re.search(r"\bwhere\b", statement, re.I):
            return "SQL DELETE without WHERE"
        if re.search(r"\b(?:drop|truncate)\s+(?:table|database|schema)\b", statement, re.I):
            return "SQL schema/data destruction"
    return ""


def _has_recursive_force(args: list[str]) -> bool:
    for raw in args:
        value = raw.lower().split("=", 1)[0]
        if value in {"--recursive", "--recurse", "--recurse-submodules", "-recursive", "-recurse"}:
            return True
        if value in {"/s", "/q", "-s", "-q"}:
            continue
        if value.startswith("-") and not value.startswith("--"):
            cluster = value[1:]
            if "r" in cluster or "R" in cluster:
                return True
        if value.startswith("--") and value[2:] in {"force", "f"}:
            continue
    return False


def _has_force(args: list[str]) -> bool:
    for raw in args:
        value = raw.lower().split("=", 1)[0]
        if value in {"--force", "-force", "/f", "/q"}:
            return True
        if value.startswith("-") and not value.startswith("--") and "f" in value[1:]:
            return True
    return False


def _dangerous_tokens(tokens: list[str]) -> str:
    if not tokens:
        return ""
    tokens = list(tokens)
    # Grouping tokens left behind by a shell frontend are syntax, not executable names.
    tokens[0] = tokens[0].lstrip("({")
    exe = tokens[0].rsplit("/", 1)[-1].lower().removesuffix(".exe")
    args = [item.lower() for item in tokens[1:]]

    if exe in {"rm", "del", "erase", "rmdir", "rd", "remove-item"}:
        if _has_recursive_force(args) or (exe in {"del", "erase", "rmdir", "rd"} and any(a in {"/s", "-recurse", "-recursive"} for a in args)):
            return "recursive/forced delete"
    if exe in {"mkfs", "mkfs.ext2", "mkfs.ext3", "mkfs.ext4", "mkfs.xfs", "mkfs.btrfs", "format", "diskpart"}:
        return "disk format or filesystem operation"
    if exe == "dd" and any(re.match(r"^of\s*=", arg) for arg in args):
        return "write to block device or raw output"
    if exe in {"shutdown", "reboot", "halt", "poweroff", "init", "stop-computer", "restart-computer"}:
        return "machine shutdown or reboot"
    if exe in {"killall", "pkill", "taskkill", "stop-process", "stop-service"} or (
        exe == "kill" and not (args and all(arg in {"-l", "-L", "--list"} for arg in args))
    ):
        return "process termination"
    if exe in {"chmod", "chown"} and _has_recursive_force(args):
        return "recursive permission/ownership change"
    if exe == "find" and any(a == "-delete" for a in args):
        return "find -delete"
    if exe == "git" and args:
        if args[0] == "reset" and any(a == "--hard" for a in args[1:]):
            return "destructive git reset"
        if args[0] == "clean" and not any("n" in a[1:] for a in args[1:] if a.startswith("-")) and any(
            a.startswith("-") and "f" in a[1:] and "d" in a[1:] for a in args[1:]
        ):
            return "destructive git clean"
        if args[0] == "push" and any(a in {"--force", "-f", "--force-with-lease"} or a.startswith("--force-with-lease=") for a in args[1:]):
            return "force push"
    if exe == "docker" and args[:2] == ["system", "prune"]:
        return "docker system prune"
    if exe in {"pip", "pip3", "npm", "pnpm", "yarn", "apt", "apt-get", "brew", "dnf", "yum", "pacman", "winget", "choco", "conda"}:
        action = next((arg for arg in args if not arg.startswith("-")), "")
        if action in {"uninstall", "remove", "purge", "autoremove"} and not any(
            arg in {"--dry-run", "-n"} for arg in args
        ):
            return "package removal"
    return ""


def _substitution_commands(text: str) -> list[str]:
    """Extract commands inside $(...), <(...), and backticks, preserving nesting."""
    found: list[str] = []
    i = 0
    while i < len(text):
        if text.startswith("$(", i) or text.startswith("<(", i):
            start = i + 2
            depth = 1
            quote = ""
            escaped = False
            j = start
            while j < len(text) and depth:
                ch = text[j]
                if escaped:
                    escaped = False
                elif ch == "\\" and quote != "'":
                    escaped = True
                elif quote:
                    if ch == quote:
                        quote = ""
                elif ch in {"'", '"'}:
                    quote = ch
                elif ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                j += 1
            if depth:
                raise ValueError("unterminated shell substitution")
            found.append(text[start:j - 1])
            i = j
            continue
        if text[i] == "`":
            j = i + 1
            escaped = False
            while j < len(text):
                if escaped:
                    escaped = False
                elif text[j] == "\\":
                    escaped = True
                elif text[j] == "`":
                    break
                j += 1
            if j >= len(text):
                raise ValueError("unterminated backtick command substitution")
            found.append(text[i + 1:j])
            i = j + 1
            continue
        i += 1
    return found


def assess_command(command: str) -> CommandAssessment:
    """Parse common POSIX/Windows shell forms; unknown syntax is gated, never assumed safe."""
    text = str(command or "").strip()
    if not text:
        return CommandAssessment(False, "", True)
    try:
        # These execution forms can consume code that is not visible as a normal argv token.
        # Fail closed before token normalization; nested payloads are still recursively inspected.
        if re.search(r"\b(?:sh|bash|dash|zsh|ksh|fish|python(?:3|2)?|py|perl|ruby|node)\b[^\n]*<\s*\(", text, re.I):
            return CommandAssessment(True, "interpreter executes process-substitution input")
        if re.search(r"<<\s*\w|\$\(\(|\$\{[^}]*\?", text):
            return CommandAssessment(True, "shell syntax could not be normalized safely", False)
        segments = _split_shell_segments(text)
        if not segments:
            return CommandAssessment(False, "", True)
        parsed: list[tuple[str, list[str], str, list[str]]] = []
        for operator, part in segments:
            raw_tokens = _tokens(part)
            parsed.append((operator, _unwrap(raw_tokens), part, raw_tokens))
        for substitution in _substitution_commands(text):
            nested = assess_command(substitution)
            if nested.dangerous:
                return nested
        for idx, (_operator, tokens, part, raw_tokens) in enumerate(parsed):
            reason = _dangerous_tokens(tokens)
            if reason:
                return CommandAssessment(True, reason)
            exe = raw_tokens[0].rsplit("/", 1)[-1].lower().removesuffix(".exe") if raw_tokens else ""
            if idx + 1 < len(parsed) and parsed[idx + 1][0] in {"|", "|&"}:
                next_raw = parsed[idx + 1][3]
                next_exe = _pipeline_executable(next_raw)
                interpreters = {"sh", "bash", "dash", "zsh", "ksh", "fish", "python", "python3", "python2",
                                "py", "perl", "ruby", "node", "php"}
                if next_exe in interpreters:
                    reason = ("downloaded content piped directly into an interpreter" if exe in {"curl", "wget"}
                              else "piped content executed by an interpreter")
                    return CommandAssessment(True, reason)
            if exe in {"sh", "bash", "dash", "zsh", "ksh", "fish", "python", "python3", "python2", "py",
                       "perl", "ruby", "node", "php"} and "<" in raw_tokens:
                return CommandAssessment(True, "script supplied to interpreter through stdin redirection")
            if exe == "eval":
                return CommandAssessment(True, "dynamic shell eval can execute generated commands")
            lowered_raw = [token.lower() for token in raw_tokens]
            if exe in {"sh", "bash", "dash", "zsh", "ksh", "fish"} and "-c" in lowered_raw:
                inline = " ".join(raw_tokens)
                if re.search(r"\$\(|`", inline) and re.search(r"\b(?:base64|xxd|openssl|python|perl)\b", inline, re.I):
                    return CommandAssessment(True, "decoded or substituted text is executed by a shell")
            sql_client = exe in {"mysql", "mariadb", "sqlite3", "psql", "sqlcmd", "duckdb", "invoke-sqlcmd"}
            sql_statement = exe in {"delete", "drop", "truncate"}
            if sql_client or sql_statement:
                sql_text = " ".join(raw_tokens).lower()
                sql_risk = _sql_risk(sql_text)
                if sql_risk:
                    return CommandAssessment(True, sql_risk)
            if exe in {"python", "python3", "python2", "py"} and "-c" in [x.lower() for x in tokens[1:]]:
                idx_c = [x.lower() for x in tokens[1:]].index("-c") + 2
                code = tokens[idx_c] if idx_c < len(tokens) else ""
                code_result = assess_python_code(code)
                if code_result.dangerous:
                    return code_result
            # Commands with process substitutions are checked recursively above.
            if re.search(r"(?:^|\s)(?:>|>>)\s*/dev/(?:sd|nvme|vd|hd|disk)", part, re.I):
                return CommandAssessment(True, "write to block device")
        # Redirections are syntax rather than argv; inspect tokenized targets only.
        for _, _, _part, raw_tokens in parsed:
            for idx, token in enumerate(raw_tokens[:-1]):
                if token in {">", ">>", "1>", "2>", "&>"}:
                    target = raw_tokens[idx + 1]
                    if re.match(r"^/dev/(?:sd|nvme|vd|hd|disk)", target, re.I):
                        return CommandAssessment(True, "write to block device")
            if any(re.match(r"^\\d?(?:>>?)?/dev/(?:sd|nvme|vd|hd|disk)", token, re.I)
                   for token in raw_tokens):
                return CommandAssessment(True, "write to block device")
    except _NestedDangerous as exc:
        return CommandAssessment(True, exc.reason, True)
    except (ValueError, IndexError) as exc:
        return CommandAssessment(True, f"command could not be parsed safely: {exc}", False)
    return CommandAssessment(False, "")


def _js_string_literal(source: str, start: int) -> tuple[str | None, int]:
    while start < len(source) and source[start].isspace():
        start += 1
    if start >= len(source) or source[start] not in {"'", '"'}:
        return None, start
    quote = source[start]
    end = start + 1
    escaped = False
    while end < len(source):
        char = source[end]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == quote:
            literal = source[start:end + 1]
            try:
                value = ast.literal_eval(literal)
            except (SyntaxError, ValueError):
                return None, end + 1
            return (value if isinstance(value, str) else None), end + 1
        end += 1
    return None, end


def _js_string_array(source: str, start: int) -> tuple[list[str] | None, int]:
    while start < len(source) and source[start].isspace():
        start += 1
    if start >= len(source) or source[start] != "[":
        return None, start
    depth = 0
    quote = ""
    escaped = False
    for end in range(start, len(source)):
        char = source[end]
        if escaped:
            escaped = False
        elif char == "\\" and quote:
            escaped = True
        elif quote:
            if char == quote:
                quote = ""
        elif char in {"'", '"'}:
            quote = char
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                try:
                    values = ast.literal_eval(source[start:end + 1])
                except (SyntaxError, ValueError):
                    return None, end + 1
                if isinstance(values, list) and all(isinstance(item, str) for item in values):
                    return values, end + 1
                return None, end + 1
    return None, len(source)


def assess_javascript_code(code: str) -> CommandAssessment:
    """Inspect literal Node child_process calls using the shared command Gate."""
    source = code or ""
    imports_child_process = re.search(
        r"(?:require\s*\(\s*['\"](?:node:)?child_process['\"]\s*\)|"
        r"from\s*['\"](?:node:)?child_process['\"])",
        source,
    )
    if not imports_child_process:
        return CommandAssessment(False, "")
    calls = re.finditer(r"\b(execSync|exec|spawnSync|spawn|execFileSync|execFile|fork)\s*\(", source)
    for match in calls:
        operation = match.group(1)
        first, position = _js_string_literal(source, match.end())
        if first is None:
            return CommandAssessment(True, "JavaScript launches a dynamic child-process command")
        if operation in {"exec", "execSync"}:
            command = first
        elif operation in {"fork"}:
            return CommandAssessment(True, "JavaScript launches another script with child_process.fork")
        else:
            while position < len(source) and source[position].isspace():
                position += 1
            if position < len(source) and source[position] == ",":
                args, _ = _js_string_array(source, position + 1)
                if args is None:
                    return CommandAssessment(True, "JavaScript launches a child process with dynamic arguments")
            else:
                args = []
            command = shlex.join([first, *args])
        assessment = assess_command(command)
        if assessment.dangerous:
            return assessment
    return CommandAssessment(False, "")


def assess_code(code: str, language: str = "python") -> CommandAssessment:
    """Dispatch source inspection to the supported language-aware Gate parser."""
    normalized = str(language or "python").strip().lower()
    if normalized in {"javascript", "js", "node", "nodejs"}:
        return assess_javascript_code(code)
    return assess_python_code(code)


def assess_python_code(code: str) -> CommandAssessment:
    """Inspect literal Python shell-outs and destructive APIs before code is executed."""
    try:
        tree = ast.parse(code or "")
    except SyntaxError as exc:
        return CommandAssessment(True, f"Python code could not be parsed safely: {exc.msg}", False)

    def dotted(node: ast.AST) -> str:
        parts: list[str] = []
        current = node
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
        return ".".join(reversed(parts))

    def literal_string(node: ast.AST) -> str | None:
        try:
            value = ast.literal_eval(node)
        except Exception:
            return None
        return value if isinstance(value, str) else None

    process_replace_calls = {
        "os.execv", "os.execve", "os.execvp", "os.execvpe", "os.execl", "os.execle", "os.execlp", "os.execlpe",
    }
    process_spawn_calls = {
        "os.posix_spawn", "os.posix_spawnp", "os.spawnl", "os.spawnle", "os.spawnlp", "os.spawnlpe",
        "os.spawnv", "os.spawnve", "os.spawnvp", "os.spawnvpe",
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = dotted(node.func)
        if name.endswith((".execute", ".executemany", ".executescript")):
            for arg in node.args:
                query = literal_string(arg)
                if query:
                    sql_risk = _sql_risk(query)
                    if sql_risk:
                        return CommandAssessment(True, sql_risk)
        if name in {"shutil.rmtree", "os.remove", "os.unlink", "os.rmdir", "Path.unlink", "Path.rmdir"} or name.endswith((".unlink", ".rmdir")):
            return CommandAssessment(True, f"destructive Python filesystem call: {name}")
        if (name in {"os.kill", "os.killpg", "signal.raise_signal"}
                or (isinstance(node.func, ast.Attribute) and node.func.attr in {"kill", "terminate"})):
            return CommandAssessment(True, f"Python process termination call: {name}")
        if name in process_replace_calls:
            return CommandAssessment(True, f"Python process replacement call: {name}")
        if name in process_spawn_calls:
            return CommandAssessment(True, f"Python process launch call needs owner review: {name}")
        if name in {"exec", "eval", "builtins.exec", "builtins.eval"}:
            nested_code = literal_string(node.args[0]) if node.args else None
            if nested_code is None:
                return CommandAssessment(True, "dynamic Python exec/eval cannot be normalized safely")
            nested_assessment = assess_python_code(nested_code)
            if nested_assessment.dangerous:
                return nested_assessment
        process_launchers = {
            "os.system", "os.popen", "subprocess.run", "subprocess.Popen", "subprocess.call",
            "subprocess.check_call", "subprocess.check_output", "asyncio.create_subprocess_shell",
            "asyncio.create_subprocess_exec", "asyncio.subprocess.create_subprocess_shell",
            "asyncio.subprocess.create_subprocess_exec", "os.execv", "os.execvp", "os.execl", "os.execlp",
        }
        if name in process_launchers or name.endswith((".create_subprocess_shell", ".create_subprocess_exec")):
            command: str | None = None
            if node.args:
                if name.endswith("create_subprocess_exec") and all(literal_string(arg) is not None for arg in node.args):
                    command = shlex.join([str(literal_string(arg)) for arg in node.args])
                else:
                    command = literal_string(node.args[0])
                if command is None and isinstance(node.args[0], (ast.List, ast.Tuple)):
                    values = [literal_string(item) for item in node.args[0].elts]
                    if all(item is not None for item in values):
                        command = shlex.join([str(item) for item in values])
            if command:
                result = assess_command(command)
                if result.dangerous:
                    return result
            elif node.args:
                return CommandAssessment(True, "Python launches a dynamic process command")
            shell_true = any(keyword.arg == "shell" and isinstance(keyword.value, ast.Constant)
                             and keyword.value.value is True for keyword in node.keywords)
            if name in {"os.system", "os.popen"} or shell_true:
                return CommandAssessment(True, "Python launches an unparsed shell command")
        if name in {"open", "builtins.open"} and len(node.args) >= 2:
            path = literal_string(node.args[0])
            mode = literal_string(node.args[1]) or ""
            if path and re.match(r"^/dev/(?:sd|nvme|vd|hd|disk)", path) and any(ch in mode for ch in "wa+"):
                return CommandAssessment(True, "Python writes to a block device")
    return CommandAssessment(False, "")


def approval_mode() -> str:
    env_value = os.getenv("JARVIS_EXEC_APPROVAL") or os.getenv("EXEC_APPROVAL")
    if env_value:
        value = env_value.strip().lower()
    else:
        try:
            from ..config import runtime as R

            value = str(R.get("exec_approval", S.EXEC_APPROVAL or "risky")).strip().lower()
        except Exception:
            value = str(S.EXEC_APPROVAL or "risky").strip().lower()
    return value if value in {"off", "risky", "all"} else "risky"


class ApprovalBroker:
    """Single-use approval requests; only the owning surface/user can resolve them."""

    def __init__(self, *, owner_id: str | None = None, ttl_s: float = 60.0):
        self.owner_id = str(owner_id if owner_id is not None else (S.TELEGRAM_OWNER_ID or S.OWNER_ID or ""))
        self.ttl_s = max(0.1, float(ttl_s))
        self._pending: dict[str, tuple[ApprovalRequest, asyncio.Future[bool], str]] = {}
        self._lock = asyncio.Lock()

    async def ask(self, command: str, cwd: str, reason: str, surface: str) -> ApprovalResult:
        loop = asyncio.get_running_loop()
        request_id = uuid.uuid4().hex[:12]
        req = ApprovalRequest(request_id, command, reason, cwd, surface, time.time() + self.ttl_s)
        future: asyncio.Future[bool] = loop.create_future()
        owner = self.owner_id
        async with self._lock:
            self._pending[request_id] = (req, future, owner)
        handler = _handler.get() or _surface_handlers.get(surface)
        try:
            if handler is not None:
                try:
                    response = handler(req)
                    if inspect.isawaitable(response):
                        response = await asyncio.wait_for(response, timeout=self.ttl_s)
                    if isinstance(response, bool):
                        await self.resolve(request_id, response, owner or _implicit_owner(surface), surface=surface)
                except asyncio.TimeoutError:
                    return ApprovalResult(False, "approval prompt timed out; command cancelled", request_id)
                except Exception as exc:
                    return ApprovalResult(False, f"approval prompt failed ({type(exc).__name__}); command cancelled", request_id)
            elif surface == "cli_owner":
                try:
                    answer = await asyncio.wait_for(
                        asyncio.to_thread(input, f"Approve dangerous command in {cwd}: {command!r}? [y/N] "),
                        timeout=self.ttl_s,
                    )
                except (asyncio.TimeoutError, EOFError, OSError):
                    return ApprovalResult(False, "approval prompt timed out or input unavailable; command cancelled", request_id)
                accepted = answer.strip().lower() in {"y", "yes", "haan", "ha"}
                await self.resolve(request_id, accepted, owner or "cli_owner", surface=surface)
            else:
                try:
                    from ..runtime.progress import emit

                    emit("approval_required", "Dangerous command needs human approval", detail={
                        "id": req.id, "command": req.command, "why_flagged": req.why_flagged,
                        "cwd": req.cwd, "surface": req.surface, "expires_at": req.expires_at,
                    }, status="pending")
                except Exception:
                    pass
            try:
                allowed = await asyncio.wait_for(future, timeout=self.ttl_s)
            except asyncio.TimeoutError:
                return ApprovalResult(False, "approval timed out; command cancelled", request_id)
            return ApprovalResult(allowed, "approved" if allowed else "cancelled by owner", request_id)
        finally:
            async with self._lock:
                self._pending.pop(request_id, None)

    async def resolve(self, request_id: str, allow: bool, user_id: str, *, surface: str) -> bool:
        """Resolve once. Surface and configured owner must match; late/wrong clicks fail."""
        async with self._lock:
            entry = self._pending.get(str(request_id))
            if entry is None:
                return False
            req, future, owner = entry
            if time.time() > req.expires_at or str(surface) != req.surface:
                return False
            # With no configured account ID, local CLI/voice and the authenticated or
            # loopback API UI can answer through their human-only input surfaces.
            if owner:
                if str(user_id) != owner:
                    return False
            elif req.surface in {"cli_owner", "voice_owner"}:
                if str(user_id) != req.surface:
                    return False
            elif req.surface == "api":
                if str(user_id) != "api_owner":
                    return False
            else:
                return False
            if future.done():
                return False
            future.set_result(bool(allow))
            return True

    def pending(self, request_id: str) -> ApprovalRequest | None:
        entry = self._pending.get(str(request_id))
        if entry is None:
            return None
        req = entry[0]
        return req if time.time() <= req.expires_at else None

    def pending_requests(self, surface: str | None = None) -> list[ApprovalRequest]:
        now = time.time()
        return [req for req, future, _ in self._pending.values()
                if not future.done() and req.expires_at >= now and (surface is None or req.surface == surface)]


def _implicit_owner(surface: str) -> str:
    if surface in {"cli_owner", "voice_owner"}:
        return surface
    if surface == "api":
        return "api_owner"
    return ""


BROKER = ApprovalBroker()


async def request_human_approval(command: str, cwd: str, *, surface: str, reason: str) -> ApprovalResult:
    """Require an owner answer even when the shell Gate mode is OFF (e.g. voice forget)."""
    return await BROKER.ask(str(command), str(cwd or os.getcwd()), str(reason), str(surface))


async def authorize_command(
    command: str,
    cwd: str,
    *,
    force: bool = False,
    surface: str | None = None,
    inspection_text: str = "",
    inspection_language: str = "python",
    assessment_command: str | None = None,
    reason: str = "",
) -> ApprovalResult:
    """Assess and, when required, ask the real owner before executing one operation."""
    mode = approval_mode()
    if mode == "off":
        return ApprovalResult(True, "gate disabled")
    from .permissions import current_surface

    active_surface = str(surface or current_surface())
    assessment = assess_command(assessment_command if assessment_command is not None else command)
    if inspection_text:
        code_assessment = assess_code(inspection_text, inspection_language)
        if code_assessment.dangerous:
            assessment = code_assessment
    needs_gate = force or mode == "all" or assessment.dangerous
    if not needs_gate:
        return ApprovalResult(True, "command is safe")
    if mode == "risky" and not force and not assessment.dangerous:
        return ApprovalResult(True, "not flagged as dangerous")
    why = reason or assessment.reason or (
        "explicitly sensitive operation requires approval" if force else "mode=all requires approval for every command"
    )
    return await BROKER.ask(str(command), str(cwd or os.getcwd()), why, active_surface)


def startup_gate_warning() -> str | None:
    return "Dangerous-command gate is OFF." if approval_mode() == "off" else None


def warn_startup(api_host: str | None = None) -> None:
    """Print required startup warnings once per process without exposing secrets."""
    if startup_gate_warning() and os.environ.get("NIKHILAI_GATE_OFF_WARNING_SHOWN") != "1":
        os.environ["NIKHILAI_GATE_OFF_WARNING_SHOWN"] = "1"
        print("Dangerous-command gate is OFF.", file=sys.stderr)
    host = str(api_host or S.API_HOST or "127.0.0.1").strip().strip("[]").lower()
    network_bind = host not in {"localhost", "127.0.0.1", "::1", ""}
    if network_bind and not S.API_AUTH_TOKEN and os.environ.get("NIKHILAI_API_BIND_WARNING_SHOWN") != "1":
        os.environ["NIKHILAI_API_BIND_WARNING_SHOWN"] = "1"
        print("Warning: non-loopback API bind without API_AUTH_TOKEN.", file=sys.stderr)
