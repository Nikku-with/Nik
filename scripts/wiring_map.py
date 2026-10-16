#!/usr/bin/env python3
"""
scripts/wiring_map.py — "kya cheez kis se judi hai?" ka static, code-se-nikala hua jawab
======================================================================================

Ye script **koi bhi claim hand-written nahi karta**. Saara data AST (abstract syntax tree)
se aata hai — yani jo kuch `docs/WIRING_MAP.md` me likha hai, wo source code se generate hua
hai. Builder ke README/STATUS.md ke daawon par bharosa nahi kiya gaya.

Stdlib-only (koi third-party import nahi) taaki bina kisi install ke bhi chale.

    python scripts/wiring_map.py                 # docs/WIRING_MAP.md likho + summary
    python scripts/wiring_map.py --check         # findings ho to exit code 1 (CI gate)
    python scripts/wiring_map.py --json out.json # machine-readable dump

Ye 4 cheezein nikalta hai:
  1. TOOL TABLE      — har registered tool kahan bana, kaun use karta hai, kaun se surface
                       tak pahunch sakta hai, aur koi test use chhuta hai ya nahi.
  2. MODULE GRAPH    — dead modules, import cycles, >600 line wale files, aur ek hi kaam ki
                       duplicate implementations.
  3. SURFACE MATRIX  — capability x surface matrix, *code path* se nikali (policy se nahi
                       sirf), jisme har blocked cell ke saath blocking policy likhi hai.
  4. PROMPT/SKILL    — SKILL.md jo tool naam bole wo maujood ho; loader har skill uthaye;
                       prompt file maujood ho; kisi rendered prompt me date/time/weekday/
                       city/weather pattern na ho.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "jarvis"

# ---------------------------------------------------------------------------
# capability -> representative registered tool names
# (matrix is derived from real permission decisions on these tools, never hand-written)
# ---------------------------------------------------------------------------
CAPABILITIES: dict[str, tuple[str, ...]] = {
    "app-open": ("open_app", "open_any_app", "focus_window", "list_windows", "close_app"),
    "keyboard": ("type_text", "press_hotkey", "desktop_type", "desktop_press",
                 "key_down", "key_up", "hold"),
    "mouse": ("desktop_click", "desktop_move", "desktop_scroll", "desktop_drag",
              "click_text", "find_image"),
    "browser": ("browser_task", "browser_research", "browser_cli", "browser_screenshot",
                "browser_close", "browser_doctor"),
    "files": ("read_file", "write_file", "find_file", "find_files", "list_dir",
              "open_path", "undo_file_operation", "search_inside_files"),
    "shell": ("run_shell_command", "python_repl", "run_python", "run_python_isolated",
              "run_script_file", "install_package"),
    "schedule": ("create_automation", "list_automations", "toggle_automation",
                 "run_automation", "cancel_automation"),
    "memory": ("remember_fact", "recall_facts", "memory_stats", "conversation_history",
               "memory_compact"),
}

# surface label -> the module that *starts* that surface
SURFACES: dict[str, tuple[str, ...]] = {
    "cli": ("jarvis.cli.app", "jarvis.cli.pro_ui", "jarvis.runtime.repl"),
    "web": ("jarvis.api.server",),
    "telegram": ("jarvis.telegram.bot", "jarvis.telegram.live"),
    "voice": ("jarvis.voice.livekit_agent", "jarvis.voice.speech"),
    "scheduled": ("jarvis.automation.scheduler",),
}

# modules that are process entry points — they are never "dead" even if nothing imports them
ENTRY_POINTS = {
    "jarvis",
    "jarvis.__main__",
    "jarvis.__init__",
}

# capabilities that are legitimately implemented once per OS backend inside a strategy class;
# a same-name method on sibling backend classes is not a duplicate implementation
_BACKEND_METHOD_NAMES = {
    "active_window", "list_windows", "focus_window", "type_text", "press_hotkey",
    "screenshot", "click", "move", "scroll", "drag", "clipboard_read", "clipboard_write",
}

# module-level capability functions that must exist in exactly one place
_CAPABILITY_FUNCTIONS = (
    "focus_window", "window_exists", "launch_app", "find_apps", "build_app_index",
    "web_search_results", "fetch_url_text", "read_any_file", "fuzzy_search",
)

# prompt-injection smell: rendered prompts must never contain these
# Only injected *values* count. A prompt that instructs "do not mention time/weather" is
# correct behaviour, so bare topic words are deliberately not part of this pattern.
_PROMPT_SMELL_RX = re.compile(
    r"(\bcurrent_datetime_str\b|\bgreeting_for_now\b|\bcurrent_hour\b|\bget_current_city\b"
    r"|\b\d{1,2}\s*:\s*\d{2}\s*(?:am|pm)\b|\b(?:mon|tues|wednes|thurs|fri|satur|sun)day\b"
    r"|\b\d{1,2}\s*\u00b0\s*[CF]\b|\bforecast\s+for\b|\bweather\s+(?:is|in|today|report)\b"
    r"|\b(?:aaj|today)\s+(?:ka|ki)\s+(?:mausam|weather)\b)", re.I,
)
# an instruction telling the model NOT to mention these topics is never a smell
_PROMPT_PROHIBITION_RX = re.compile(
    r"(?:do\s+not|don'?t|never|mat|nahi)\s+(?:restate|mention|inject|add|include|bolo|batao)"
    r"|\bunless\s+asked\b|\bspecifically\s+poocha\b", re.I,
)

_SKILL_TOOL_RX = re.compile(r"`([a-z][a-z0-9_]{2,40})`")
_TOOL_CALL_STR_RX = re.compile(r"[\"']([a-z][a-z0-9_]{2,40})[\"']")


# ---------------------------------------------------------------------------
# module model
# ---------------------------------------------------------------------------
@dataclass
class ModuleInfo:
    name: str
    path: Path
    tree: ast.Module
    lines: int
    top_level_imports: set[str] = field(default_factory=set)
    nested_imports: set[str] = field(default_factory=set)
    defined: set[str] = field(default_factory=set)
    tool_funcs: dict[str, int] = field(default_factory=dict)   # tool name -> lineno
    tool_lists: dict[str, list[str]] = field(default_factory=dict)  # X_TOOLS -> [func names]
    string_literals: set[str] = field(default_factory=set)
    dynamic_imports: set[str] = field(default_factory=set)

    @property
    def all_imports(self) -> set[str]:
        return self.top_level_imports | self.nested_imports | self.dynamic_imports


def module_name_for(path: Path, root: Path) -> str:
    rel = path.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _resolve_relative(module: str, level: int, target: str | None, *, is_package: bool = False) -> str | None:
    """Resolve `from ..x import y` in `module` to an absolute dotted name."""
    parts = module.split(".")
    # `jarvis/graph/nodes.py` -> level=1 base is `jarvis.graph`
    # `jarvis/graph/__init__.py` -> level=1 base is `jarvis.graph` too (the package itself)
    base_parts = parts if is_package else (parts[:-1] if len(parts) > 1 else parts)
    if level > 1:
        drop = level - 1
        if len(base_parts) < drop:
            return None
        base_parts = base_parts[:-drop] if drop else base_parts
    prefix = ".".join(base_parts)
    if not target:
        return prefix or None
    return f"{prefix}.{target}" if prefix else target


def _is_tool_decorator(node: ast.expr) -> bool:
    """Detect `@tool` / `@tool(...)` from langchain."""
    if isinstance(node, ast.Name):
        return node.id == "tool"
    if isinstance(node, ast.Attribute):
        return node.attr == "tool"
    if isinstance(node, ast.Call):
        return _is_tool_decorator(node.func)
    return False


def _is_delegating_stub(node: ast.AST) -> bool:
    """True when a function body is nothing but `return <call>(...)` / `return await <call>(...)`."""
    body = list(getattr(node, "body", None) or [])
    # docstring, `from x import y` and `import x` do not count as real logic
    body = [stmt for stmt in body if not (
        isinstance(stmt, (ast.Import, ast.ImportFrom))
        or (isinstance(stmt, ast.Expr) and isinstance(getattr(stmt, "value", None), ast.Constant))
    )]
    if len(body) != 1 or not isinstance(body[0], ast.Return):
        return False
    value = body[0].value
    if value is None:
        return False
    if isinstance(value, ast.Await):
        value = value.value
    return isinstance(value, ast.Call)


def parse_modules(root: Path) -> dict[str, ModuleInfo]:
    modules: dict[str, ModuleInfo] = {}
    for path in sorted((root / PACKAGE).rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        source = path.read_text(encoding="utf-8", errors="ignore")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:  # pragma: no cover - compileall catches this earlier
            raise SystemExit(f"syntax error in {path}: {exc}") from exc
        name = module_name_for(path, root)
        is_package = path.name == "__init__.py"
        info = ModuleInfo(name=name, path=path, tree=tree, lines=len(source.splitlines()))
        modules[name] = info

        for node in ast.walk(tree):
            # ---- imports -------------------------------------------------
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(PACKAGE):
                        info.top_level_imports.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    resolved = _resolve_relative(name, node.level, node.module, is_package=is_package)
                elif node.module and node.module.startswith(PACKAGE):
                    resolved = node.module
                else:
                    resolved = None
                bucket = (info.top_level_imports if node.col_offset == 0 else info.nested_imports)
                if resolved:
                    if node.module is None:
                        # `from . import a, b` — each alias is a submodule of `resolved`
                        for alias in node.names:
                            if alias.name.startswith(PACKAGE):
                                bucket.add(alias.name)
                            elif alias.name != "*":
                                bucket.add(f"{resolved}.{alias.name}")
                    else:
                        bucket.add(resolved)
                        for alias in node.names:
                            if alias.name != "*":
                                bucket.add(f"{resolved}.{alias.name}")
            # ---- dynamic imports -----------------------------------------
            elif isinstance(node, ast.Call):
                func = node.func
                dotted = ""
                if isinstance(func, ast.Attribute):
                    dotted = f"{ast.unparse(func.value)}.{func.attr}"
                elif isinstance(func, ast.Name):
                    dotted = func.id
                if dotted.endswith("import_module") and node.args:
                    try:
                        value = ast.literal_eval(node.args[0])
                    except Exception:
                        value = None
                    if isinstance(value, str) and value.startswith(PACKAGE):
                        info.dynamic_imports.add(value)
                # __import__("jarvis.x")
                if dotted == "__import__" and node.args:
                    try:
                        value = ast.literal_eval(node.args[0])
                    except Exception:
                        value = None
                    if isinstance(value, str) and value.startswith(PACKAGE):
                        info.dynamic_imports.add(value)
            # ---- definitions ---------------------------------------------
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                info.defined.add(node.name)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if any(_is_tool_decorator(d) for d in node.decorator_list):
                        info.tool_funcs[node.name] = node.lineno
            # ---- string literals -----------------------------------------
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if 2 <= len(node.value) <= 120:
                    info.string_literals.add(node.value)

        # ---- *_TOOLS lists (module level *and* inside try/except branches) ----
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                value = node.value
                if value is None or not isinstance(value, (ast.List, ast.Tuple)):
                    continue
                for target in targets:
                    if not isinstance(target, ast.Name) or not target.id.endswith("TOOLS"):
                        continue
                    names: list[str] = []
                    for element in value.elts:
                        if isinstance(element, ast.Name):
                            names.append(element.id)
                        elif isinstance(element, ast.Starred) and isinstance(element.value, ast.Name):
                            names.append(f"*{element.value.id}")
                    if names:
                        # a `try:` branch defines the real list; a later `except:` branch may
                        # reset it to `[]`. Keep whichever assignment added real entries.
                        existing = info.tool_lists.get(target.id, [])
                        info.tool_lists[target.id] = existing + [n for n in names
                                                                 if n not in existing]
    return modules


# ---------------------------------------------------------------------------
# tool registry (resolved statically, mirroring jarvis/tools/__init__.py)
# ---------------------------------------------------------------------------
@dataclass
class ToolInfo:
    name: str
    module: str
    lineno: int
    groups: list[str] = field(default_factory=list)
    level: str = ""
    referenced_by: set[str] = field(default_factory=set)
    surfaces: list[str] = field(default_factory=list)
    tested: bool = False
    tests: set[str] = field(default_factory=set)
    skills: set[str] = field(default_factory=set)


def resolve_tool_lists(modules: dict[str, ModuleInfo]) -> dict[str, list[str]]:
    """Resolve every `*_TOOLS` list (including `*CONST` splats) to concrete function names."""
    raw: dict[str, list[str]] = {}
    for info in modules.values():
        for list_name, names in info.tool_lists.items():
            raw.setdefault(list_name, []).extend(names)
    resolved: dict[str, list[str]] = {}
    for _ in range(4):  # bounded expansion for splats
        changed = False
        for list_name, names in raw.items():
            out: list[str] = []
            for item in names:
                if item.startswith("*") and item[1:] in raw:
                    out.extend(raw[item[1:]])
                elif item.startswith("*"):
                    out.append(item[1:])
                else:
                    out.append(item)
            if out != resolved.get(list_name):
                changed = True
            resolved[list_name] = out
        raw = resolved
        if not changed:
            break
    return resolved


def build_tools(modules: dict[str, ModuleInfo], groups: dict[str, list[str]]) -> dict[str, ToolInfo]:
    func_to_module: dict[str, tuple[str, int]] = {}
    for info in modules.values():
        for tool_name, lineno in info.tool_funcs.items():
            func_to_module.setdefault(tool_name, (info.name, lineno))

    # a *_TOOLS list may name a function that is not @tool-decorated in this pass
    # (e.g. defined behind a try/except import) — still register it.
    for names in groups.values():
        for func_name in names:
            if func_name.startswith("*"):
                continue
            if func_name not in func_to_module:
                for info in modules.values():
                    if func_name in info.defined:
                        func_to_module[func_name] = (info.name, 0)
                        break

    tools: dict[str, ToolInfo] = {}
    for group_name, names in groups.items():
        for func_name in names:
            if func_name.startswith("*"):
                continue
            module, lineno = func_to_module.get(func_name, ("<unresolved>", 0))
            entry = tools.get(func_name)
            if entry is None:
                entry = ToolInfo(name=func_name, module=module, lineno=lineno)
                tools[func_name] = entry
            if group_name not in entry.groups:
                entry.groups.append(group_name)
    return tools


def group_membership(modules: dict[str, ModuleInfo]) -> dict[str, list[str]]:
    """Recreate the ALL_TOOLS / group composition of jarvis/tools/__init__.py from source.

    Group constants there are assembled from other constants with `[*A, *B]` splats, so the
    names are expanded through `resolve_tool_lists` before they are handed out.
    """
    resolved = resolve_tool_lists(modules)
    init = modules.get("jarvis.tools")
    composition: dict[str, list[str]] = {}
    if init is not None:
        for node in ast.walk(init.tree):
            if not isinstance(node, ast.Assign) or not isinstance(node.value, (ast.List, ast.Tuple)):
                continue
            for target in node.targets:
                if not isinstance(target, ast.Name) or not target.id.isupper():
                    continue
                names: list[str] = []
                for element in node.value.elts:
                    if isinstance(element, ast.Name):
                        names.append(element.id)
                    elif isinstance(element, ast.Starred) and isinstance(element.value, ast.Name):
                        names.append(f"*{element.value.id}")
                if names:
                    existing = composition.get(target.id, [])
                    composition[target.id] = existing + [n for n in names if n not in existing]
    # groups surfaced by tools_for()
    group_keys = {
        "desktop": "DESKTOP_TOOLS", "runtime": "RUNTIME_TOOLS", "research": "RESEARCH_TOOLS",
        "web": "WEB_TOOLS", "browser": "BROWSER_TOOLS", "code": "CODE_TOOLS",
        "memory": "MEMORY_TOOLS", "skills": "SKILL_TOOLS", "ops": "OPS_TOOLS",
        "automation": "AUTOMATION_TOOLS", "system_ops": "SYSTEM_OPS_TOOLS",
        "telegram": "TELEGRAM_TOOLS", "app_finder": "APP_FINDER_TOOLS",
        "system": "SYSTEM_TOOLS", "files": "FILE_TOOLS", "notes": "NOTE_TOOLS",
        "process": "PROCESS_TOOLS", "repl": "REPL_TOOLS", "session": "SESSION_TOOLS",
    }
    out: dict[str, list[str]] = {}
    for group, const in group_keys.items():
        raw_names = composition.get(const) or resolved.get(const) or []
        expanded: list[str] = []
        for item in raw_names:
            if item.startswith("*"):
                expanded.extend(resolved.get(item[1:], [item[1:]]))
            else:
                expanded.append(item)
        seen: set[str] = set()
        out[group] = [n for n in expanded if not (n in seen or seen.add(n))]  # type: ignore[func-returns-value]
    return out


# ---------------------------------------------------------------------------
# references: which modules mention a tool (as a string or as a bare name)
# ---------------------------------------------------------------------------
def build_reference_index(modules: dict[str, ModuleInfo]) -> dict[str, set[str]]:
    index: dict[str, set[str]] = defaultdict(set)
    for info in modules.values():
        for literal in info.string_literals:
            if re.fullmatch(r"[a-z][a-z0-9_]{2,40}", literal):
                index[literal].add(info.name)
        for defined_name in info.defined:
            index[defined_name].add(info.name)
    return index


def collect_worker_tool_names(modules: dict[str, ModuleInfo]) -> dict[str, set[str]]:
    """Tool names each worker/executor can actually invoke, read from its source."""
    out: dict[str, set[str]] = {}
    for mod_name, info in modules.items():
        names: set[str] = set()
        for node in ast.walk(info.tree):
            if isinstance(node, ast.Call):
                func = node.func
                # by_name.get("x") / _call("x", {...})
                if isinstance(func, ast.Attribute) and func.attr in {"get", "get_tool"} and node.args:
                    try:
                        value = ast.literal_eval(node.args[0])
                    except Exception:
                        value = None
                    if isinstance(value, str):
                        names.add(value)
                # _call("take_screenshot", {}) / await call("open_any_app", {...})
                if isinstance(func, ast.Name) and func.id in {"_call", "call"} and node.args:
                    try:
                        value = ast.literal_eval(node.args[0])
                    except Exception:
                        value = None
                    if isinstance(value, str):
                        names.add(value)
                # install_package.ainvoke({...}) — a direct call of the registered tool
                if (isinstance(func, ast.Attribute)
                        and func.attr in {"ainvoke", "invoke", "arun", "run"}
                        and isinstance(func.value, ast.Name)):
                    names.add(func.value.id)
                # tools_for("desktop", "runtime")
                if isinstance(func, ast.Name) and func.id == "tools_for":
                    for arg in node.args:
                        try:
                            value = ast.literal_eval(arg)
                        except Exception:
                            value = None
                        if isinstance(value, str):
                            names.add(f"group:{value}")
        if names:
            out[mod_name] = names
    return out


# ---------------------------------------------------------------------------
# module-graph findings
# ---------------------------------------------------------------------------
def find_dead_modules(modules: dict[str, ModuleInfo], external_refs: set[str]) -> list[str]:
    imported: set[str] = set()
    for info in modules.values():
        imported |= {m for m in info.all_imports if m in modules}
    dead = []
    for name in modules:
        if name in ENTRY_POINTS or name in imported or name in external_refs:
            continue
        dead.append(name)
    return sorted(dead)


def _sccs(edges: dict[str, set[str]], nodes: Iterable[str]) -> list[list[str]]:
    """Tarjan SCC over `edges`; returns every strongly connected component with >1 node."""
    result: list[list[str]] = []
    index_counter = [0]
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}

    for start in sorted(nodes):
        if start in indices:
            continue
        work: list[tuple[str, list[str]]] = [(start, iter(sorted(edges.get(start, ()))))]
        indices[start] = lowlink[start] = index_counter[0]
        index_counter[0] += 1
        stack.append(start)
        on_stack.add(start)
        while work:
            current, children = work[-1]
            advanced = False
            for child in children:
                if child not in edges and child not in indices:
                    continue
                if child not in indices:
                    indices[child] = lowlink[child] = index_counter[0]
                    index_counter[0] += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, iter(sorted(edges.get(child, ())))))
                    advanced = True
                    break
                if child in on_stack:
                    lowlink[current] = min(lowlink[current], indices[child])
            if advanced:
                continue
            work.pop()
            if lowlink[current] == indices[current]:
                component: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == current:
                        break
                if len(component) > 1:
                    result.append(sorted(component))
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[current])
    return sorted(result)


def find_cycles(modules: dict[str, ModuleInfo]) -> list[list[str]]:
    """Every import cycle including lazy (function-level) edges — a design smell."""
    succ: dict[str, set[str]] = defaultdict(set)
    for info in modules.values():
        for target in info.all_imports:
            if target in modules and target != info.name:
                succ[info.name].add(target)
    return _sccs(succ, modules)


def shortest_cycles(edges: dict[str, set[str]], components: list[list[str]]) -> list[list[str]]:
    """For each strongly connected component, the shortest concrete cycle inside it.

    A 50-node SCC is technically one cycle but tells nobody what to fix; the shortest cycle
    through it names the actual pair/triangle of modules that must be decoupled.
    """
    from collections import deque

    found: list[list[str]] = []
    for component in components:
        members = set(component)
        best: list[str] | None = None
        for start in sorted(members):
            if best is not None and len(best) <= 2:
                break
            previous: dict[str, str | None] = {start: None}
            queue: deque[str] = deque([start])
            while queue:
                current = queue.popleft()
                if best is not None and len(best) <= 2:
                    break
                for neighbour in sorted(edges.get(current, ()) & members):
                    if neighbour == start:
                        path = [current]
                        while previous.get(path[-1]) is not None:
                            path.append(previous[path[-1]])  # type: ignore[arg-type]
                        candidate = list(reversed(path))
                        if best is None or len(candidate) < len(best):
                            best = candidate
                        break
                    if neighbour not in previous:
                        previous[neighbour] = current
                        queue.append(neighbour)
        if best:
            found.append(best)
    return sorted(found, key=lambda item: (len(item), item))


def find_hard_cycles(modules: dict[str, ModuleInfo]) -> list[list[str]]:
    """Cycles built only from module-level imports — these can break `import` outright."""
    succ: dict[str, set[str]] = defaultdict(set)
    for info in modules.values():
        for target in info.top_level_imports:
            if target in modules and target != info.name:
                succ[info.name].add(target)
    return _sccs(succ, modules)


def _class_method_positions(info: ModuleInfo) -> set[tuple[str, int]]:
    """(name, lineno) of every method defined inside a class.

    `focus_window` on the Windows backend, the Linux backend and the fallback backend is one
    strategy with three per-OS implementations, not three duplicate capabilities — so class
    methods are excluded from the duplicate check by design.
    """
    found: set[tuple[str, int]] = set()
    for node in ast.walk(info.tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for child in ast.walk(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found.add((child.name, child.lineno))
    return found


def find_duplicate_capabilities(modules: dict[str, ModuleInfo]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    owners: dict[str, list[tuple[str, int, bool]]] = defaultdict(list)
    for info in modules.values():
        methods = _class_method_positions(info)
        for node in ast.walk(info.tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name not in _CAPABILITY_FUNCTIONS:
                continue
            if (node.name, node.lineno) in methods:
                continue  # method of a per-OS backend class — not a module-level duplicate
            owners[node.name].append((info.name, node.lineno, _is_delegating_stub(node)))
    for capability, places in sorted(owners.items()):
        if len(places) < 2:
            continue
        real = [(m, line) for m, line, stub in places if not stub]
        if len(real) <= 1:
            continue  # only one genuine implementation; the others just delegate
        findings.append({
            "capability": capability,
            "implementations": [f"{m}:{line}" for m, line in real],
            "delegating": [f"{m}:{line}" for m, line, stub in places if stub],
        })
    return findings


def find_long_files(modules: dict[str, ModuleInfo], limit: int = 600) -> list[tuple[str, int]]:
    return sorted(((m.name, m.lines) for m in modules.values() if m.lines > limit),
                  key=lambda item: -item[1])


# ---------------------------------------------------------------------------
# surface reachability
# ---------------------------------------------------------------------------
def transitive_imports(modules: dict[str, ModuleInfo], start: str) -> set[str]:
    seen: set[str] = set()
    queue = [start]
    while queue:
        current = queue.pop()
        info = modules.get(current)
        if info is None or current in seen:
            continue
        seen.add(current)
        queue.extend(t for t in info.all_imports if t in modules)
    return seen


def surface_reachability(modules: dict[str, ModuleInfo]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for surface, entries in SURFACES.items():
        closure: set[str] = set()
        for entry in entries:
            closure |= transitive_imports(modules, entry)
        out[surface] = {
            "entry_modules": [e for e in entries if e in modules],
            "reaches_unified_runner": "jarvis.runtime.agent" in closure,
            "reaches_graph": "jarvis.graph.build" in closure,
            "closure_size": len(closure),
        }
    return out


def permission_matrix(tools: dict[str, ToolInfo]) -> dict[str, dict[str, dict[str, str]]]:
    """Ask the real policy engine (jarvis.security.permissions) for every decision."""
    sys.path.insert(0, str(ROOT))
    from jarvis.security.permissions import LEVELS, check, tool_level  # noqa: PLC0415

    matrix: dict[str, dict[str, dict[str, str]]] = {}
    for capability, names in CAPABILITIES.items():
        row: dict[str, dict[str, str]] = {}
        for surface in ("cli_owner", "api", "telegram_owner", "telegram_guest",
                        "voice_owner", "automation"):
            known = [n for n in names if n in tools]
            if not known:
                row[surface] = {"state": "no-tool", "detail": "capability has no registered tool"}
                continue
            allowed = []
            blocked = []
            for name in known:
                decision = check(name, surface)
                (allowed if decision.allowed else blocked).append(
                    f"{name}={tool_level(name)}")
            if allowed and not blocked:
                row[surface] = {"state": "allowed", "detail": ", ".join(allowed)}
            elif allowed and blocked:
                row[surface] = {"state": "partial",
                                "detail": f"allowed: {', '.join(allowed)} | blocked: {', '.join(blocked)}"}
            else:
                reasons = {check(n, surface).reason for n in known}
                row[surface] = {"state": "blocked",
                                "detail": "; ".join(sorted(r for r in reasons if r))}
        matrix[capability] = row
    matrix["_levels"] = {level: level for level in LEVELS}
    return matrix


# ---------------------------------------------------------------------------
# prompts + skills
# ---------------------------------------------------------------------------
def check_prompts(modules: dict[str, ModuleInfo], root: Path) -> dict[str, Any]:
    prompts_mod = modules.get("jarvis.prompts")
    smells: list[str] = []
    rendered: list[str] = []
    if prompts_mod is not None:
        source_lines = prompts_mod.path.read_text(encoding="utf-8", errors="ignore").splitlines()
        for node in ast.walk(prompts_mod.tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                match = _PROMPT_SMELL_RX.search(node.value)
                if match:
                    # a prompt that says "do not mention time/weather" is correct, not a smell;
                    # only flag the hit when the surrounding lines are not a prohibition.
                    context = "\n".join(source_lines[max(0, node.lineno - 4):node.lineno + 3])
                    if _PROMPT_PROHIBITION_RX.search(context):
                        continue
                    smells.append(f"jarvis/prompts.py:{node.lineno} literal contains {match.group(0)!r}")
    # every prompt-producing function must be free of clock/city/weather injection
    for name, info in modules.items():
        if not name.startswith("jarvis.prompts"):
            continue
        for node in ast.walk(info.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and "prompt" in node.name:
                rendered.append(f"{name}.{node.name}")
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call):
                        target = ast.unparse(sub.func)
                        if target.split(".")[-1] in {"current_datetime_str", "current_hour",
                                                     "greeting_for_now", "get_current_city"}:
                            smells.append(f"{name}.{node.name}() injects {target}()")
    return {"smells": sorted(set(smells)), "prompt_functions": sorted(set(rendered))}


def check_skills(root: Path, tools: dict[str, ToolInfo]) -> dict[str, Any]:
    loader = root / PACKAGE / "skills" / "loader.py"
    catalog = root / PACKAGE / "skills" / "catalog"
    skill_files = sorted(catalog.rglob("SKILL.md"))
    loader_source = loader.read_text(encoding="utf-8", errors="ignore") if loader.exists() else ""
    loads_all = "rglob(\"SKILL.md\")" in loader_source

    skills: list[dict[str, Any]] = []
    missing_tools: list[dict[str, str]] = []
    for path in skill_files:
        text = path.read_text(encoding="utf-8", errors="ignore")
        frontmatter = ""
        if text.startswith("---"):
            end = text.find("\n---", 3)
            if end > 0:
                frontmatter = text[3:end]
        declared: list[str] = []
        for line in frontmatter.splitlines():
            if line.lower().startswith("tools:"):
                declared = [t.strip().strip("\"'") for t in
                            line.partition(":")[2].strip().strip("[]").split(",") if t.strip()]
        unknown = [t for t in declared if t not in tools]
        for tool_name in unknown:
            missing_tools.append({"skill": path.parent.name, "tool": tool_name,
                                  "file": str(path.relative_to(root))})
        body_tools = {m for m in _SKILL_TOOL_RX.findall(text) if m in tools}
        skills.append({
            "name": path.parent.name,
            "path": str(path.relative_to(root)),
            "declared_tools": declared,
            "unknown_tools": unknown,
            "body_tool_mentions": sorted(body_tools),
            "keywords": bool(re.search(r"^keywords:", frontmatter, re.M)),
        })
    readme_files = sorted(catalog.rglob("README.md"))
    prompt_files = [p for p in (root / PACKAGE).rglob("*.py") if "prompt" in p.name]
    return {
        "loader_loads_all": loads_all,
        "skill_count": len(skill_files),
        "skills": skills,
        "missing_tools": missing_tools,
        "catalog_readme": [str(p.relative_to(root)) for p in readme_files],
        "prompt_modules": sorted(str(p.relative_to(root)) for p in prompt_files),
    }


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------
def scan_tests(root: Path) -> dict[str, set[str]]:
    hits: dict[str, set[str]] = defaultdict(set)
    test_dir = root / "tests"
    if not test_dir.exists():
        return hits
    for path in sorted(test_dir.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        rel = str(path.relative_to(root))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for token in re.findall(r"[a-z][a-z0-9_]{2,40}", node.value):
                    hits[token].add(rel)
            if isinstance(node, ast.Attribute):
                hits[node.attr].add(rel)
            if isinstance(node, ast.Name):
                hits[node.id].add(rel)
    return hits


def external_module_refs(root: Path) -> set[str]:
    """Modules referenced from scripts/, tests/ or pyproject that nothing inside jarvis imports."""
    refs: set[str] = set()
    pattern = re.compile(rf"{PACKAGE}(?:\.[a-zA-Z_][\w]*){{1,}}")
    for directory in ("scripts", "tests"):
        for path in sorted((root / directory).rglob("*.py")):
            text = path.read_text(encoding="utf-8", errors="ignore")
            for match in pattern.findall(text):
                refs.add(match)
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        refs.update(pattern.findall(pyproject.read_text(encoding="utf-8", errors="ignore")))
    return refs


# ---------------------------------------------------------------------------
# shipped-code smells
# ---------------------------------------------------------------------------
def find_stub_smells(modules: dict[str, ModuleInfo]) -> list[dict[str, str]]:
    smells: list[dict[str, str]] = []
    for info in modules.values():
        source_lines = info.path.read_text(encoding="utf-8", errors="ignore").splitlines()
        for node in ast.walk(info.tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.Raise) and isinstance(sub.exc, ast.Call):
                    name = ast.unparse(sub.exc.func).split(".")[-1]
                    if name == "NotImplementedError":
                        smells.append({"module": info.name, "line": str(sub.lineno),
                                       "kind": "NotImplementedError",
                                       "text": source_lines[sub.lineno - 1].strip()[:140]})
            # TODO / FIXME comments inside shipped code
        for lineno, line in enumerate(source_lines, 1):
            if re.search(r"#\s*(TODO|FIXME|HACK)\b", line):
                smells.append({"module": info.name, "line": str(lineno), "kind": "TODO/FIXME",
                               "text": line.strip()[:140]})
    return smells


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------
def build_wiring_map(root: Path = ROOT) -> dict[str, Any]:
    modules = parse_modules(root)
    groups = group_membership(modules)
    tools = build_tools(modules, groups)
    references = build_reference_index(modules)
    worker_tools = collect_worker_tool_names(modules)
    test_hits = scan_tests(root)
    external = external_module_refs(root)

    # --- tool -> who references it -----------------------------------------
    for tool_name, tool in tools.items():
        tool.referenced_by = {m for m in references.get(tool_name, set()) if m != tool.module}
        tool.tests = {t for t in test_hits.get(tool_name, set())}
        tool.tested = bool(tool.tests)

    # --- permission level + policy-allowed surfaces ------------------------
    sys.path.insert(0, str(root))
    from jarvis.security.permissions import check, tool_level  # noqa: PLC0415

    policy_surface_names = ("cli_owner", "api", "telegram_owner", "telegram_guest",
                            "voice_owner", "automation")
    for tool in tools.values():
        tool.level = tool_level(tool.name)
        tool.surfaces = [s for s in policy_surface_names if check(tool.name, s).allowed]

    skills_report = check_skills(root, tools)
    for skill in skills_report["skills"]:
        for tool_name in skill["body_tool_mentions"]:
            if tool_name in tools:
                tools[tool_name].skills.add(skill["name"])

    reachability = surface_reachability(modules)
    matrix = permission_matrix(tools)

    dead = find_dead_modules(modules, external)
    cycles = find_cycles(modules)
    hard_cycles = find_hard_cycles(modules)
    succ_lazy: dict[str, set[str]] = defaultdict(set)
    succ_hard: dict[str, set[str]] = defaultdict(set)
    for mod in modules.values():
        for target in mod.all_imports:
            if target in modules and target != mod.name:
                succ_lazy[mod.name].add(target)
        for target in mod.top_level_imports:
            if target in modules and target != mod.name:
                succ_hard[mod.name].add(target)
    minimal_cycles = shortest_cycles(succ_lazy, cycles)
    minimal_hard_cycles = shortest_cycles(succ_hard, hard_cycles)
    long_files = find_long_files(modules)
    duplicates = find_duplicate_capabilities(modules)
    prompt_report = check_prompts(modules, root)
    stub_smells = find_stub_smells(modules)

    # tools no worker/executor can actually reach
    reachable: set[str] = set()
    for _mod_name, names in worker_tools.items():
        for name in names:
            if name.startswith("group:"):
                group = name.split(":", 1)[1]
                reachable.update(t for t, tool in tools.items() if group in tool.groups)
            else:
                reachable.add(name)
    unreachable = sorted(t for t in tools if t not in reachable and not tools[t].skills)

    return {
        "root": str(root),
        "modules": {name: {
            "path": str(info.path.relative_to(root)),
            "lines": info.lines,
            "imports": sorted(info.all_imports),
            "tools": sorted(info.tool_funcs),
            "tool_lists": sorted(info.tool_lists),
        } for name, info in modules.items()},
        "tools": {name: {
            "module": tool.module,
            "line": tool.lineno,
            "groups": tool.groups,
            "level": tool.level,
            "surfaces": tool.surfaces,
            "referenced_by": sorted(tool.referenced_by),
            "tested": tool.tested,
            "tests": sorted(tool.tests)[:6],
            "skills": sorted(tool.skills),
        } for name, tool in sorted(tools.items())},
        "groups": groups,
        "worker_tools": {k: sorted(v) for k, v in sorted(worker_tools.items())},
        "unreachable_tools": unreachable,
        "dead_modules": dead,
        "cycles": cycles,
        "hard_cycles": hard_cycles,
        "minimal_cycles": minimal_cycles,
        "minimal_hard_cycles": minimal_hard_cycles,
        "long_files": [{"module": m, "lines": n} for m, n in long_files],
        "duplicate_capabilities": duplicates,
        "surface_reachability": reachability,
        "capability_matrix": matrix,
        "prompts": prompt_report,
        "skills": skills_report,
        "stub_smells": stub_smells,
        "summary": {
            "modules": len(modules),
            "tools": len(tools),
            "tools_tested": sum(1 for t in tools.values() if t.tested),
            "tools_reachable": len([t for t in tools if t in reachable]),
            "dead_modules": len(dead),
            "cycles": len(cycles),
            "hard_cycles": len(hard_cycles),
            "duplicates": len(duplicates),
        },
    }


# ---------------------------------------------------------------------------
# markdown rendering
# ---------------------------------------------------------------------------
def _md_escape(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render_markdown(report: dict[str, Any]) -> str:
    out: list[str] = []
    summary = report["summary"]
    out.append("# WIRING MAP — NikhilAI / Jarvis")
    out.append("")
    out.append("> **Generated file — do not edit by hand.**")
    out.append("> Source: `python scripts/wiring_map.py` (AST analysis, stdlib only).")
    out.append("> Har table neeche **source code se** nikal kar banayi gayi hai; README/STATUS.md")
    out.append("> ke kisi bhi claim par bharosa nahi kiya gaya.")
    out.append("")
    out.append("## 0. Ek nazar me")
    out.append("")
    out.append("| metric | value |")
    out.append("|---|---|")
    out.append(f"| modules analysed | {summary['modules']} |")
    out.append(f"| registered tools | {summary['tools']} |")
    out.append(f"| tools with a test reference | {summary['tools_tested']} |")
    out.append(f"| tools an executor can reach | {summary['tools_reachable']} |")
    out.append(f"| dead modules | {summary['dead_modules']} |")
    out.append(f"| import cycles (any edge) | {summary['cycles']} |")
    out.append(f"| import cycles (module-level only, break `import`) | {summary['hard_cycles']} |")
    out.append(f"| duplicate capability implementations | {summary['duplicates']} |")
    out.append("")

    # ---------------- 1. tool table ----------------
    out.append("## 1. Tool table")
    out.append("")
    out.append("Har registered tool: kahan bana, kaun reference karta hai, kaun se surface policy se")
    out.append("allowed hain, aur koi test ise chhuta hai ya nahi.")
    out.append("")
    out.append("| tool | module | groups | level | surfaces | referenced by | test |")
    out.append("|---|---|---|---|---|---|---|")
    for name, tool in report["tools"].items():
        refs = ", ".join(tool["referenced_by"][:4]) or "—"
        if len(tool["referenced_by"]) > 4:
            refs += f" (+{len(tool['referenced_by']) - 4})"
        surfaces = ", ".join(tool["surfaces"]) or "—"
        out.append(
            f"| `{name}` | `{tool['module']}:{tool['line']}` | {','.join(tool['groups']) or '—'} | "
            f"{tool['level']} | {surfaces} | {_md_escape(refs)} | "
            f"{'yes' if tool['tested'] else '**NO**'} |"
        )
    out.append("")

    unreachable = report["unreachable_tools"]
    out.append(f"### 1a. Tools koi worker/executor directly nahi chala sakta ({len(unreachable)})")
    out.append("")
    if unreachable:
        out.append("Ye tools registry me hain, par kisi bhi worker/executor ke code path me nazar")
        out.append("nahi aate (na `tools_for(...)` group se, na kisi `_call(\"name\")` se, na kisi")
        out.append("SKILL.md se). LLM unhe tabhi chala sakta hai jab ReAct sub-agent ko poora")
        out.append("tool set bind ho — deterministic path se ye **unreachable** hain.")
        out.append("")
        for name in unreachable:
            tool = report["tools"][name]
            out.append(f"- `{name}` (`{tool['module']}`)")
    else:
        out.append("Koi unreachable tool nahi — har registered tool kisi code path se juda hai.")
    out.append("")

    untested = [n for n, t in report["tools"].items() if not t["tested"]]
    out.append(f"### 1b. Tools jinhe koi test nahi chhuta ({len(untested)})")
    out.append("")
    out.append(", ".join(f"`{n}`" for n in untested) if untested else "Koi nahi — sab tools tested hain.")
    out.append("")

    # ---------------- 2. module graph ----------------
    out.append("## 2. Module graph")
    out.append("")
    dead = report["dead_modules"]
    out.append(f"### 2a. Dead modules — jinhe koi import nahi karta ({len(dead)})")
    out.append("")
    if dead:
        for name in dead:
            info = report["modules"][name]
            out.append(f"- `{name}` — `{info['path']}` ({info['lines']} lines)")
    else:
        out.append("Koi dead module nahi — har module kahin na kahin import hota hai.")
    out.append("")

    hard = report["hard_cycles"]
    cycles = report["cycles"]
    out.append(f"### 2b. Import cycles — module-level, import-breaking ({len(hard)})")
    out.append("")
    if hard:
        out.append("Ye cycles **top-level** imports se bane hain; Python `import` ke waqt in par")
        out.append("atrekta hai (partially-initialised module / AttributeError).")
        out.append("")
        out.append("Sabse chhote (fix karne layak) cycles:")
        out.append("")
        for component in report.get("minimal_hard_cycles", [])[:20]:
            out.append("- " + " → ".join(f"`{m}`" for m in component)
                       + f" → `{component[0]}`  ({len(component)} modules)")
        if not report.get("minimal_hard_cycles"):
            for component in hard:
                out.append("- " + " → ".join(f"`{m}`" for m in component) + " → (cycle)")
    else:
        out.append("Koi module-level cycle nahi — `import jarvis...` sab jagah safe hai.")
    out.append("")
    lazy = [c for c in cycles if c not in hard]
    out.append(f"### 2b-2. Lazy cycles — function-level imports only ({len(lazy)})")
    out.append("")
    if lazy:
        out.append("Ye sirf function/method ke andar ke `import` se bane hain. Python inhe tolerate")
        out.append("karta hai (import time par cycle nahi banta), par ye coupling smell hai — agar")
        out.append("inme se koi bhi import top-level par chala gaya to `import jarvis` toot jayega.")
        out.append("")
        out.append("Sabse chhote (fix karne layak) cycles:")
        out.append("")
        for component in report.get("minimal_cycles", [])[:20]:
            out.append("- " + " → ".join(f"`{m}`" for m in component)
                       + f" → `{component[0]}`  ({len(component)} modules)")
        out.append("")
        out.append(f"Poore strongly-connected components: {len(lazy)}; sabse bade me "
                   f"{max(len(c) for c in lazy)} modules.")
    else:
        out.append("Koi lazy cycle nahi.")
    out.append("")

    long_files = report["long_files"]
    out.append(f"### 2c. Files > 600 lines ({len(long_files)})")
    out.append("")
    if long_files:
        out.append("| module | lines |")
        out.append("|---|---|")
        for item in long_files:
            out.append(f"| `{item['module']}` | {item['lines']} |")
    else:
        out.append("Koi file 600 lines se upar nahi.")
    out.append("")

    duplicates = report["duplicate_capabilities"]
    out.append(f"### 2d. Duplicate capability implementations ({len(duplicates)})")
    out.append("")
    if duplicates:
        out.append("| capability | real implementations | delegating stubs |")
        out.append("|---|---|---|")
        for item in duplicates:
            out.append(f"| `{item['capability']}` | {', '.join(item['implementations'])} | "
                       f"{', '.join(item['delegating']) or '—'} |")
    else:
        out.append("Koi duplicate capability nahi: har capability ki ek hi asli implementation hai "
                   "(baaki sirf usi ko delegate karte hain).")
    out.append("")

    # ---------------- 3. surface matrix ----------------
    out.append("## 3. Surface × capability matrix")
    out.append("")
    out.append("Yeh matrix **code path** se nikali hai: pehle dekha ki kaun sa surface module")
    out.append("`jarvis.runtime.agent` (unified runner) tak pahunchta hai, phir har capability ke")
    out.append("representative tools par asli permission engine (`jarvis/security/permissions.py`)")
    out.append("chalaya. Har blocked cell ke saath blocking policy likhi hai.")
    out.append("")
    reach = report["surface_reachability"]
    out.append("| surface | entry module | reaches `runtime.agent` | reaches `graph.build` |")
    out.append("|---|---|---|---|")
    for surface, data in reach.items():
        out.append(f"| {surface} | {', '.join(f'`{m}`' for m in data['entry_modules']) or '—'} | "
                   f"{'yes' if data['reaches_unified_runner'] else '**NO**'} | "
                   f"{'yes' if data['reaches_graph'] else 'no'} |")
    out.append("")
    surfaces = ("cli_owner", "api", "telegram_owner", "telegram_guest", "voice_owner", "automation")
    matrix = report["capability_matrix"]
    out.append("| capability | " + " | ".join(surfaces) + " |")
    out.append("|---|" + "---|" * len(surfaces))
    for capability in CAPABILITIES:
        row = matrix.get(capability, {})
        cells = []
        for surface in surfaces:
            cell = row.get(surface, {})
            state = cell.get("state", "?")
            mark = {"allowed": "✅", "partial": "🟡", "blocked": "🚫", "no-tool": "➖"}.get(state, "?")
            cells.append(f"{mark} {state}")
        out.append(f"| {capability} | " + " | ".join(cells) + " |")
    out.append("")
    out.append("### 3a. Blocked cells — exact policy")
    out.append("")
    any_blocked = False
    for capability in CAPABILITIES:
        row = matrix.get(capability, {})
        for surface in surfaces:
            cell = row.get(surface, {})
            if cell.get("state") in {"blocked", "partial", "no-tool"}:
                any_blocked = True
                out.append(f"- **{capability}** on **{surface}**: `{cell.get('detail', '')}`")
    if not any_blocked:
        out.append("Koi blocked cell nahi.")
    out.append("")
    out.append("Policy source: `jarvis/security/permissions.py` — `LEVELS = "
               "READ < WRITE < EXECUTE < SYSTEM < DANGEROUS`; har surface ka `max_level` "
               "`SURFACES` dict me hai; `OWNER_DESKTOP_SYSTEM_TOOLS` sirf api/voice/automation "
               "par desktop-control ko SYSTEM level allow karta hai.")
    out.append("")

    # ---------------- 4. prompt / skill consistency ----------------
    out.append("## 4. Prompt / skill / tool consistency")
    out.append("")
    skills = report["skills"]
    prompts = report["prompts"]
    out.append("| check | result |")
    out.append("|---|---|")
    out.append(f"| SKILL.md files discovered | {skills['skill_count']} |")
    out.append(f"| loader scans whole catalog (`rglob('SKILL.md')`) | "
               f"{'yes' if skills['loader_loads_all'] else '**NO**'} |")
    out.append(f"| SKILL.md naming a missing tool | "
               f"{len(skills['missing_tools'])} |")
    out.append(f"| prompt modules present | {len(skills['prompt_modules'])} |")
    out.append(f"| prompt functions found | {len(prompts['prompt_functions'])} |")
    out.append(f"| date/time/weekday/city/weather smells in prompts | "
               f"{len(prompts['smells'])} |")
    out.append("")
    if skills["missing_tools"]:
        out.append("**SKILL.md → missing tool:**")
        out.append("")
        for item in skills["missing_tools"]:
            out.append(f"- `{item['file']}` names `{item['tool']}` — no such registered tool")
        out.append("")
    if prompts["smells"]:
        out.append("**Prompt smells:**")
        out.append("")
        for smell in prompts["smells"]:
            out.append(f"- {smell}")
        out.append("")
    out.append("### 4a. Skill inventory")
    out.append("")
    out.append("| skill | declared tools | unknown | keywords |")
    out.append("|---|---|---|---|")
    for skill in skills["skills"]:
        out.append(f"| `{skill['name']}` | "
                   f"{', '.join(f'`{t}`' for t in skill['declared_tools']) or '—'} | "
                   f"{', '.join(f'`{t}`' for t in skill['unknown_tools']) or '—'} | "
                   f"{'yes' if skill['keywords'] else '**NO**'} |")
    out.append("")

    # ---------------- 5. shipped-code smells ----------------
    smells = report["stub_smells"]
    out.append("## 5. Shipped-code smells (TODO / NotImplementedError)")
    out.append("")
    if smells:
        out.append("| module:line | kind | text |")
        out.append("|---|---|---|")
        for item in smells:
            out.append(f"| `{item['module']}:{item['line']}` | {item['kind']} | "
                       f"`{_md_escape(item['text'])}` |")
    else:
        out.append("Koi TODO/FIXME/NotImplementedError shipped code me nahi.")
    out.append("")
    return "\n".join(out) + "\n"


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate docs/WIRING_MAP.md from source code.")
    parser.add_argument("--root", default=str(ROOT), help="project root (default: repo root)")
    parser.add_argument("--out", default="", help="markdown output path")
    parser.add_argument("--json", dest="json_out", default="", help="also dump JSON here")
    parser.add_argument("--check", action="store_true",
                        help="exit 1 when dead modules, cycles, duplicates or missing skill tools exist")
    args = parser.parse_args(list(argv) if argv is not None else None)

    root = Path(args.root).resolve()
    report = build_wiring_map(root)
    markdown = render_markdown(report)

    out_path = Path(args.out) if args.out else root / "docs" / "WIRING_MAP.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(markdown, encoding="utf-8")
    print(f"wrote {out_path} ({len(markdown.splitlines())} lines)")
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        print(f"wrote {args.json_out}")

    summary = report["summary"]
    print(f"  tools={summary['tools']} tested={summary['tools_tested']} "
          f"reachable={summary['tools_reachable']}")
    print(f"  dead_modules={summary['dead_modules']} cycles={summary['cycles']} "
          f"duplicates={summary['duplicates']}")
    if report["dead_modules"]:
        print("  dead: " + ", ".join(report["dead_modules"]))
    for component in report.get("minimal_hard_cycles", []):
        print("  HARD cycle: " + " -> ".join(component))
    for component in report.get("minimal_cycles", []):
        print("  lazy cycle: " + " -> ".join(component))
    for item in report["duplicate_capabilities"]:
        print(f"  duplicate: {item['capability']} -> {', '.join(item['implementations'])}")
    for item in report["skills"]["missing_tools"]:
        print(f"  missing skill tool: {item['file']} -> {item['tool']}")
    for smell in report["prompts"]["smells"]:
        print(f"  prompt smell: {smell}")

    if args.check:
        problems = (len(report["dead_modules"]) + len(report["hard_cycles"])
                    + len(report["duplicate_capabilities"])
                    + len(report["skills"]["missing_tools"]) + len(report["prompts"]["smells"]))
        if problems:
            print(f"FAIL: {problems} wiring problem(s)")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
