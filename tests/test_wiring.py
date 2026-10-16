"""Structural guarantees for the wiring map (scripts/wiring_map.py).

These checks are the "does it hold together" layer: every module imports, no module is
orphaned, no import-time cycle, no duplicate capability, no prompt that describes a tool
that does not exist, and — the important one — **no registered tool is silently unwired**.

A registered tool is considered wired when some code path can reach it at runtime:

*   `wired`  — a deterministic caller (`_call("x", …)`, `x.ainvoke(…)`, `by_name.get("x")`)
    or a runtime binding (`tools_for("desktop", …)` handed to a model), or
*   `manual` — reachable only through the Rich UI's `/tool <name>` escape hatch
    (jarvis/cli/pro_ui.py), which is a real path but not an automatic one.

The manual set is pinned below. Any NEW tool that lands in `jarvis.tools.ALL_TOOLS`
without a wiring path fails `test_no_newly_unwired_tool`; wiring an existing manual tool
without updating this list also fails. That keeps the list honest instead of aspirational.

Run `python scripts/wiring_map.py` to regenerate docs/WIRING_MAP.md.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Tools that are registered, tested, and invocable via `/tool <name>` in the Rich UI, but
# that no worker binds and no deterministic path calls. Each entry says why.
EXPECTED_MANUAL_TOOLS: dict[str, str] = {
    "browser_cli": (
        "browser-use CLI (Path-2) driver — jarvis/tools/browser_tools.py:928. Registered and "
        "tested, but the browser worker only ever picks browser_research/run_browser_task, so "
        "the CLI path is never chosen automatically."
    ),
    "browser_close": (
        "closes the browser session (jarvis/tools/browser_tools.py:989). No shutdown or "
        "idle-cleanup hook calls it, so sessions are never reclaimed automatically."
    ),
    "run_python": (
        "one-off snippet runner (jarvis/tools/code_engine.py:459). The code worker always goes "
        "through code_engine_run, so this registered tool has no automatic caller."
    ),
    "tg_send_buttons": (
        "Telegram inline keyboards (jarvis/telegram/tools.py:89). jarvis/telegram/bot.py builds "
        "its keyboards directly instead of exposing this tool, so it is unreachable at runtime."
    ),
}

# The one lazy (function-local) import cycle in the tree. It is benign because both imports
# are inside function bodies, but it is a real design smell and is pinned so it cannot grow.
EXPECTED_LAZY_CYCLES: set[tuple[str, ...]] = {
    ("jarvis.config.runtime", "jarvis.runtime.controls"),
}


@pytest.fixture(scope="module")
def wiring(tmp_path_factory) -> dict:
    """Run the AST wiring analyser once and hand the module its findings."""
    json_path = tmp_path_factory.mktemp("wiring") / "wiring.json"
    script = REPO_ROOT / "scripts" / "wiring_map.py"
    proc = subprocess.run(
        [sys.executable, str(script), "--root", str(REPO_ROOT), "--json", str(json_path)],
        capture_output=True, text=True, cwd=str(REPO_ROOT), check=False,
    )
    assert proc.returncode == 0, f"wiring_map.py failed:\n{proc.stdout}\n{proc.stderr}"
    return json.loads(json_path.read_text(encoding="utf-8"))


def _load_analyser():
    path = REPO_ROOT / "scripts" / "wiring_map.py"
    spec = importlib.util.spec_from_file_location("_wiring_map", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # dataclasses resolves annotations via sys.modules[cls.__module__], so register first
    sys.modules["_wiring_map"] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop("_wiring_map", None)
    return module


# ---------------------------------------------------------------------------
# modules
# ---------------------------------------------------------------------------
def test_every_jarvis_module_imports():
    """Every module under jarvis/ must import on a clean interpreter."""
    jarvis_root = REPO_ROOT / "jarvis"
    mods = sorted(
        ".".join(p.relative_to(REPO_ROOT).with_suffix("").parts)
        for p in jarvis_root.rglob("*.py")
        if p.name != "__init__.py" and "__pycache__" not in p.parts
    )
    failed: list[str] = []
    for name in mods:
        code = (
            "import importlib, sys;"
            f"sys.path.insert(0, {str(REPO_ROOT)!r});"
            f"importlib.import_module({name!r})"
        )
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                              text=True, check=False, cwd=str(REPO_ROOT))
        if proc.returncode != 0:
            failed.append(f"{name}: {proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else '?'}")
    assert not failed, "modules that do not import:\n" + "\n".join(failed)


def test_no_dead_module(wiring):
    """A module nothing imports is dead weight; either wire it or delete it."""
    assert wiring["dead_modules"] == [], f"dead (unimported) modules: {wiring['dead_modules']}"


def test_no_import_time_cycle(wiring):
    """A hard cycle breaks `import jarvis.x` on a cold interpreter."""
    assert wiring["minimal_hard_cycles"] == [], f"import-time cycles: {wiring['minimal_hard_cycles']}"
    assert wiring["hard_cycles"] == [], "hard cycles: " + str(wiring["hard_cycles"])


def test_lazy_cycles_are_only_the_known_one(wiring):
    """Lazy cycles are survivable but they are a smell; pin the set so it cannot grow."""
    found = {tuple(cycle) for cycle in wiring["minimal_cycles"]}
    assert found == EXPECTED_LAZY_CYCLES, (
        f"lazy import cycles changed.\nknown : {sorted(EXPECTED_LAZY_CYCLES)}\n"
        f"found : {sorted(found)}"
    )


# ---------------------------------------------------------------------------
# tool registry
# ---------------------------------------------------------------------------
def test_registry_has_no_duplicate_tool_names():
    from jarvis.tools import ALL_TOOLS

    names = [getattr(t, "name", "") for t in ALL_TOOLS]
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, f"tool names registered more than once: {dupes}"
    assert len(ALL_TOOLS) == 99, (
        f"registry size changed ({len(ALL_TOOLS)}); update docs/WIRING_MAP.md and this test"
    )


def test_no_duplicate_capability(wiring):
    assert wiring["duplicate_capabilities"] == [], (
        f"tools exposing the same capability: {wiring['duplicate_capabilities']}"
    )


def test_no_newly_unwired_tool(wiring):
    """Every registered tool must be reachable at runtime, or explicitly listed as manual."""
    unreachable = set(wiring["unreachable_tools"])
    assert unreachable == set(EXPECTED_MANUAL_TOOLS), (
        "unwired tools changed.\n"
        f"  new/unexpected unwired : {sorted(unreachable - set(EXPECTED_MANUAL_TOOLS))}\n"
        f"  now wired (drop it from EXPECTED_MANUAL_TOOLS): "
        f"{sorted(set(EXPECTED_MANUAL_TOOLS) - unreachable)}"
    )


@pytest.mark.parametrize("name", sorted(EXPECTED_MANUAL_TOOLS))
def test_manual_tool_is_still_registered_and_invocable(name: str):
    """A 'manual' tool must at least exist and be callable through the /tool escape hatch."""
    from jarvis.tools import ALL_TOOLS

    tool_obj = next((t for t in ALL_TOOLS if getattr(t, "name", "") == name), None)
    assert tool_obj is not None, f"{name} vanished from the registry"
    assert callable(getattr(tool_obj, "ainvoke", None)), f"{name} has no ainvoke()"


def test_every_tool_is_allowed_on_at_least_one_surface():
    """A tool no surface may ever run is dead weight in the registry."""
    from jarvis.security.permissions import SURFACES, check
    from jarvis.tools import ALL_TOOLS

    dead = []
    for tool_obj in ALL_TOOLS:
        name = getattr(tool_obj, "name", "")
        if not any(check(name, surface).allowed for surface in SURFACES):
            dead.append(name)
    assert not dead, f"tools no surface can run: {dead}"


def test_permission_check_is_total_over_the_registry():
    """`check()` must fail closed, not raise, for any name/level combination."""
    from jarvis.security.permissions import SURFACES, check
    from jarvis.tools import ALL_TOOLS

    for tool_obj in ALL_TOOLS:
        name = getattr(tool_obj, "name", "")
        for surface in SURFACES:
            decision = check(name, surface)
            assert hasattr(decision, "allowed"), f"check({name!r}, {surface!r}) returned {decision!r}"
    # unknown names must fail closed rather than raise
    assert check("definitely_not_a_tool", "cli_owner").allowed is False


def test_permission_capability_aliases_are_exact():
    """`check()` gates capabilities as well as tools; that alias list must not drift.

    Too narrow and a worker gets silently blocked (the gate fails closed on unknown names);
    too wide and a hallucinated tool name inherits the weakest level.
    """
    import re

    from jarvis.security.permissions import CAPABILITY_ALIASES
    from jarvis.tools import ALL_TOOLS

    registered = {getattr(t, "name", "") for t in ALL_TOOLS}
    used: set[str] = set()
    for path in (REPO_ROOT / "jarvis").rglob("*.py"):
        src = path.read_text(encoding="utf-8")
        for pattern in (r'permission_check\(\s*["\']([a-z_0-9]+)["\']',
                        r'[^_.\w]check\(\s*["\']([a-z_0-9]+)["\']',
                        r'_permission_block\(\s*state\s*,\s*["\']([a-z_0-9]+)["\']',
                        r'_permission_block\(\s*state\s*,\s*["\'][a-z_0-9]+["\']\s*,\s*["\']([a-z_0-9]+)["\']'):
            used |= set(re.findall(pattern, src))

    unknown = {n for n in used if n not in registered}
    stale = {n for n in CAPABILITY_ALIASES if n not in unknown}
    assert unknown <= CAPABILITY_ALIASES, (
        "capability name passed to the permission gate is neither a registered tool nor a known "
        f"alias: {sorted(unknown - CAPABILITY_ALIASES)} — add it to CAPABILITY_ALIASES"
    )
    assert not stale, (
        f"CAPABILITY_ALIASES lists names nothing uses any more: {sorted(stale)}"
    )


def test_dangerous_actions_cannot_run_from_the_guest_surface():
    """Regression: these were classified READ, so a guest (max READ) could run them."""
    from jarvis.security.permissions import tool_level

    must_be_escalated = {
        "restart_jarvis_tool": "DANGEROUS",
        "play_file": "EXECUTE",
        "open_path": "EXECUTE",
        "memory_compact": "WRITE",
        "refresh_app_index": "WRITE",
        "reload_config_tool": "WRITE",
    }
    from jarvis.security.permissions import check

    wrong = {n: (tool_level(n), want) for n, want in must_be_escalated.items()
             if tool_level(n) != want}
    assert not wrong, f"tool level regression: {wrong}"
    for name in must_be_escalated:
        assert check(name, "telegram_guest").allowed is False, f"{name} is allowed for a guest"


def test_every_tool_exposes_a_usable_schema():
    """A tool whose args schema will not serialise can never be bound to a model."""
    from jarvis.tools import ALL_TOOLS

    broken = []
    for tool_obj in ALL_TOOLS:
        name = getattr(tool_obj, "name", "")
        try:
            schema = getattr(tool_obj, "args_schema", None) or getattr(tool_obj, "args", None)
            if schema is None:
                broken.append(f"{name}: no args schema")
        except Exception as exc:  # pragma: no cover - defensive
            broken.append(f"{name}: {type(exc).__name__}: {exc}")
    assert not broken, "tools with an unusable schema:\n" + "\n".join(broken)


# ---------------------------------------------------------------------------
# prompts and skills
# ---------------------------------------------------------------------------
def test_prompts_do_not_describe_missing_tools(wiring):
    smells = wiring["prompts"]["smells"]
    assert smells == [], f"prompt text references unknown tools: {smells}"


def test_no_stub_or_hardcoded_placeholder(wiring):
    assert wiring["stub_smells"] == [], f"stub/placeholder smells: {wiring['stub_smells']}"


def test_skill_files_load_with_valid_frontmatter(wiring):
    skills = wiring["skills"]
    assert skills["loader_loads_all"] is True, "skills loader could not load every SKILL.md"
    assert skills["skill_count"] >= 1, "no skills found"
    for skill in skills["skills"]:
        assert skill["keywords"], f"skill without keywords: {skill['path']}"
        assert skill["unknown_tools"] == [], (
            f"skill {skill['path']} declares tools that are not registered: {skill['unknown_tools']}"
        )
    assert skills["missing_tools"] == [], f"skills missing tool frontmatter: {skills['missing_tools']}"


def test_skill_loader_returns_declared_tools():
    from jarvis.skills.loader import load_skills

    loaded = load_skills()
    assert loaded, "skills loader returned nothing"
    for skill in loaded:
        assert skill.name, "a skill loaded without a name"


# ---------------------------------------------------------------------------
# surfaces
# ---------------------------------------------------------------------------
def test_every_surface_reaches_the_runner(wiring):
    """Each entry point (CLI, Telegram, API, scheduler) must reach the unified runner."""
    for surface, info in wiring["surface_reachability"].items():
        assert info["reaches_unified_runner"] is True, f"{surface} never reaches the runner"
        assert info["reaches_graph"] is True, f"{surface} never reaches the LangGraph graph"
        assert info["closure_size"] > 0, f"{surface} has an empty import closure"


def test_wiring_map_document_is_current():
    """docs/WIRING_MAP.md must match what the analyser reports (no stale doc)."""
    analyser = _load_analyser()
    fresh = analyser.render_markdown(analyser.build_wiring_map(REPO_ROOT))
    committed = (REPO_ROOT / "docs" / "WIRING_MAP.md").read_text(encoding="utf-8")
    assert fresh == committed, (
        "docs/WIRING_MAP.md is stale — run `python scripts/wiring_map.py` and commit the result"
    )
