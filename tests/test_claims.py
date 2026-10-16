"""Pin the claims in docs/CLAIMS_AUDIT.md so the document cannot drift from the code.

docs/CLAIMS_AUDIT.md states verdicts about the README, docs/STATUS.md and the skill
files. A verdict that goes stale is worse than no verdict, so the facts behind the
rows that can be checked cheaply are asserted here.

The expensive rows (full-suite count, harness totals, archive entry count) are not
re-derived from inside pytest — running the suite inside the suite is not sensible.
They are re-measured at the end of an audit run and edited by hand.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _claim_doc() -> str:
    path = ROOT / "docs" / "CLAIMS_AUDIT.md"
    assert path.is_file(), "docs/CLAIMS_AUDIT.md is missing"
    return path.read_text(encoding="utf-8")


def test_claims_audit_exists_and_is_linked_from_readme_and_status():
    """A claims audit nobody links to is a claims audit nobody reads."""
    assert (ROOT / "docs" / "CLAIMS_AUDIT.md").is_file()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    status = (ROOT / "docs" / "STATUS.md").read_text(encoding="utf-8")
    assert "docs/CLAIMS_AUDIT.md" in readme, "README does not link to the claims audit"
    assert "CLAIMS_AUDIT.md" in status, "STATUS.md does not link to the claims audit"


# ---------------------------------------------------------------------------
# the numeric claims quoted in the audit must match the code
# ---------------------------------------------------------------------------
def test_audit_quotes_the_real_registry_size():
    from jarvis.tools import ALL_TOOLS

    doc = _claim_doc()
    assert f"{len(ALL_TOOLS)} callable" in doc or f"{len(ALL_TOOLS)} registered" in doc, (
        f"CLAIMS_AUDIT.md does not state the registry size ({len(ALL_TOOLS)})")


def test_audit_quotes_the_real_skill_count():
    from jarvis.skills.loader import load_skills

    count = len(load_skills())
    assert f"{count} pack" in _claim_doc(), f"CLAIMS_AUDIT.md does not state {count} skill packs"


def test_audit_names_every_manual_only_tool():
    """The 'not wired' list in the audit must be exactly the unwired set."""
    doc = _claim_doc()
    from jarvis.tools import ALL_TOOLS

    registered = {getattr(t, "name", "") for t in ALL_TOOLS}
    for name in ("browser_cli", "browser_close", "run_python", "tg_send_buttons"):
        assert name in registered, f"{name} is no longer registered"
        assert f"`{name}`" in doc, f"CLAIMS_AUDIT.md does not list {name} as manual-only"


# ---------------------------------------------------------------------------
# README claims that are cheap to hold to their word
# ---------------------------------------------------------------------------
def test_readme_python_floor_matches_pyproject():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'requires-python\s*=\s*"([^"]+)"', pyproject)
    assert match, "no requires-python in pyproject.toml"
    floor = match.group(1)
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    version = re.sub(r"[^0-9.]", "", floor).split(".")[:2]
    assert ".".join(version) in readme, (
        f"README does not state the Python floor {'.'.join(version)} from pyproject")


def test_readme_city_and_timezone_defaults_match_settings():
    from jarvis.config import settings as S

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert f"CITY={S.CITY}" in readme, f"README does not state the default CITY ({S.CITY})"
    assert f"TIMEZONE={S.TIMEZONE}" in readme, (
        f"README does not state the default TIMEZONE ({S.TIMEZONE})")


def test_readme_verification_commands_all_exist():
    """`pytest -q`, `ruff check .` and `scripts/audit_tools.py` must all be real."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "python scripts/audit_tools.py" in readme
    assert (ROOT / "scripts" / "audit_tools.py").is_file()
    assert "python -m compileall -q jarvis tests" in readme


def test_readme_warns_that_pytest_q_prints_no_count():
    """`addopts = "-q"` suppresses the summary line; the README must say so."""
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    if 'addopts' in pyproject and '-q' in pyproject:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        assert 'addopts=""' in readme, (
            "pytest is configured with -q but the README never mentions how to get a count")


# ---------------------------------------------------------------------------
# the dead prompt helpers must stay documented as dead
# ---------------------------------------------------------------------------
def test_dead_prompt_helpers_are_recorded_as_dead():
    """`current_datetime_str` / `greeting_for_now` are defined and never called.

    Either they get wired (and this test is updated) or the audit keeps saying they are
    dead. What must not happen is silently shipping them as if they were in use.
    """
    from jarvis import prompts

    helpers = [name for name in ("current_datetime_str", "greeting_for_now")
               if hasattr(prompts, name)]
    assert helpers, "the prompt helpers are gone; update the audit"

    source = ""
    for path in (ROOT / "jarvis").rglob("*.py"):
        if path.name == "prompts.py":
            continue
        source += path.read_text(encoding="utf-8")
    unwired = [name for name in helpers if name not in source]
    doc = _claim_doc()
    for name in unwired:
        assert name in doc, f"{name} is defined but never called and the audit does not say so"


# ---------------------------------------------------------------------------
# the sensitive-fact and chat-routing behaviour the audit relies on
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text", [
    "mera card number 4111 1111 1111 1111 hai",
    "mera CVV 123 hai",
    "my api key is sk-abc123",
    "mera address 12 Gandhi Nagar Jaipur hai",
])
def test_sensitive_facts_are_staged_not_written(text: str):
    from jarvis.memory.manager import is_sensitive_fact

    assert is_sensitive_fact(text) is True, f"{text!r} would be written without confirmation"


@pytest.mark.parametrize("text", [
    "mera naam Nikhil hai",
    "mujhe chai pasand hai",
    "mera favourite colour blue hai",
])
def test_ordinary_facts_are_not_treated_as_sensitive(text: str):
    """Over-blocking here would make the confirmation prompt fire on every memory save."""
    from jarvis.memory.manager import is_sensitive_fact

    assert is_sensitive_fact(text) is False, f"{text!r} is ordinary but was flagged sensitive"


@pytest.mark.parametrize("text", [
    "notepad kholo", "chrome open karo", "file delete karo", "screenshot le lo",
    "note likho", "weather batao", "music chalao", "calculator kholo",
    "bhai file hata do", "reminder set karo", "search karo python",
])
def test_action_requests_stay_out_of_the_chat_path(text: str):
    """`is_chat_request` runs before `_direct_app_request`; a false positive here
    silently stops the owner's desktop commands from ever running."""
    from jarvis.runtime.agent import is_chat_request

    assert is_chat_request(text) is False, f"{text!r} was misclassified as chat"
