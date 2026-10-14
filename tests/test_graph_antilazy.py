"""
tests/test_graph_antilazy.py — anti-lazy / anti-blind graph (spec section 2)
=============================================================================
  pytest tests/test_graph_antilazy.py
  python tests/test_graph_antilazy.py

No LLM / no network. Covers: blind-handoff detection (tool proof ke bina
summary = blind), UNVERIFIED digest marks, critic blind→refine, targeted
re-verify (subset + stall guard), lazy-plan fallback, evidence balance-sheet.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("JARVIS_OFFLINE_MODE", "1")

from jarvis.config import MAX_GRAPH_ITERATIONS  # noqa: E402
from jarvis.graph import nodes as N  # noqa: E402
from jarvis.graph.state import evidence_digest, initial_state  # noqa: E402
from jarvis.runtime import evidence as E  # noqa: E402

try:
    import pytest  # type: ignore

    @pytest.fixture(autouse=True)
    def _auto_isolation():
        yield
except Exception:  # pragma: no cover
    pass


def _finding(worker, summary, tool_calls=None, **kw):
    f = {"worker": worker, "summary": summary, "ts": 0.0}
    if tool_calls is not None:
        f["tool_calls"] = tool_calls
    f.update(kw)
    return f


def _ok_call(name="open_app", output="✅ khol diya"):
    return {"name": name, "args": {}, "output": output, "ok": True, "source": "tool"}


def _fail_call(name="open_app", output="❌ nahi mila"):
    return {"name": name, "args": {}, "output": output, "ok": False, "source": "tool"}


# ---------------------------------------------------------------------------
# 1) blind-handoff detection
# ---------------------------------------------------------------------------
def test_finding_is_blind_matrix():
    # tool-required worker, proof nahi → blind
    blind, reason = E.finding_is_blind(_finding("desktop", "notepad khol diya, hello likha"))
    assert blind and "tool_calls khaali" in reason
    # proof hai → not blind
    blind, _ = E.finding_is_blind(_finding("desktop", "notepad khol diya", tool_calls=[_ok_call()]))
    assert not blind
    # non-tool worker (research/memory) → kabhi blind nahi (text output hota hai)
    assert E.finding_is_blind(_finding("research", "5 sites padhe, ye nikla")) == (False, "")
    assert E.finding_is_blind(_finding("memory", "yaad rakha")) == (False, "")
    # lazy/khaali → blind
    blind, reason = E.finding_is_blind(_finding("code", "", lazy=True))
    assert blind and "lazy" in reason
    # saare calls fail par success-jaisi summary → blind
    blind, reason = E.finding_is_blind(_finding("browser", "site se data nikaal diya", tool_calls=[_fail_call()]))
    assert blind and "fail" in reason
    # garbage input → not blind (crash nahi)
    assert E.finding_is_blind(None) == (False, "")
    assert E.finding_is_blind({"worker": "desktop"})[0] is True  # khaali summary


def test_detect_blind_handoffs_state():
    st = initial_state("notepad kholo", session_id="t-blind")
    st["workers"] = ["desktop", "research"]
    st["findings"] = [
        _finding("desktop", "notepad khol diya boss"),
        _finding("research", "kuch sites dekhi"),
        _finding("desktop", "dobara try kiya", tool_calls=[_ok_call()]),
    ]
    blind = E.detect_blind_handoffs(st)
    assert len(blind) == 1, blind
    assert blind[0]["worker"] == "desktop" and "notepad" in blind[0]["summary"]
    assert E.detect_blind_handoffs({}) == []
    assert E.detect_blind_handoffs(None) == []


def test_session_evidence_summary():
    st = initial_state("x", session_id="t-bal")
    st["workers"] = ["desktop"]
    st["tool_evidence"] = [_ok_call(), _fail_call("close_app")]
    st["findings"] = [_finding("desktop", "hawa me claim")]
    bal = E.session_evidence_summary(st)
    assert bal["tool_calls"] == 2 and bal["verified"] == 1 and bal["failed"] == 1
    assert bal["blind_findings"] == ["desktop"], bal
    st2 = initial_state("namaste", session_id="t-chat")
    assert E.session_evidence_summary(st2)["status"] == "no-evidence-needed"


# ---------------------------------------------------------------------------
# 2) digest marks blind findings UNVERIFIED
# ---------------------------------------------------------------------------
def test_digest_marks_blind():
    st = initial_state("x", session_id="t-digest")
    st["findings"] = [_finding("desktop", "notepad khol diya"), _finding("research", "sites padhe")]
    plain = evidence_digest(st)
    assert "UNVERIFIED" not in plain
    marked = evidence_digest(st, blind=["desktop"])
    assert "UNVERIFIED" in marked and "desktop" in marked
    # research par mark nahi lagna chahiye
    research_chunk = [c for c in marked.split("[") if c.startswith("2]")][0]
    assert "UNVERIFIED" not in research_chunk, research_chunk


# ---------------------------------------------------------------------------
# 3) synthesize: blind pakdo + errors + events
# ---------------------------------------------------------------------------
def test_synthesize_detects_blind():
    st = initial_state("notepad kholo aur hello likho", session_id="t-synth")
    st["workers"] = ["desktop"]
    st["findings"] = [_finding("desktop", "notepad khol diya, hello likh diya")]
    st["errors"] = []
    out = asyncio.run(N.synthesize_node(st))
    assert any("blind_handoff" in e for e in st["errors"]), st["errors"]
    assert any(ev.get("node") == "blind_handoff" for ev in out["events"]), out["events"]
    assert "UNVERIFIED" in out["evidence"], out["evidence"][:300]


def test_synthesize_verified_no_blind_noise():
    st = initial_state("notepad kholo", session_id="t-synth-ok")
    st["workers"] = ["desktop"]
    st["findings"] = [_finding("desktop", "notepad khol diya", tool_calls=[_ok_call()])]
    st["errors"] = []
    out = asyncio.run(N.synthesize_node(st))
    assert not [e for e in st["errors"] if "blind_handoff" in e], st["errors"]
    assert "UNVERIFIED" not in out["evidence"]


# ---------------------------------------------------------------------------
# 4) critic: blind → refine trigger (offline heuristic)
# ---------------------------------------------------------------------------
def test_critic_blind_forces_refine():
    st = initial_state("notepad kholo", session_id="t-critic")
    st["workers"] = ["desktop"]
    st["answer"] = "Ho gaya boss, notepad khol diya aur kaam complete kar diya sab kuch badhiya."
    st["evidence"] = "kuch evidence text yahan hai taaki lambai poori ho jaye bilkul sahi."
    st["findings"] = [_finding("desktop", "notepad khol diya")]
    st["errors"] = []
    out = asyncio.run(N.critic_node(st))
    assert out["critique"]["ok"] is False, out["critique"]
    assert "desktop" in out["critique"]["fix_hint"], out["critique"]
    assert any("blind" in m for m in out["critique"]["missing"])


def test_critic_verified_passes():
    st = initial_state("notepad kholo", session_id="t-critic-ok")
    st["workers"] = ["desktop"]
    st["answer"] = (
        "Ho gaya boss, notepad khol diya hai aur poora kaam complete ho gaya hai sab kuch sahi chal raha hai."
    )
    st["evidence"] = "evidence text yahan hai kaafi lamba taaki score na gire bilkul."
    st["findings"] = [_finding("desktop", "notepad khol diya", tool_calls=[_ok_call()])]
    st["errors"] = []
    out = asyncio.run(N.critic_node(st))
    assert out["critique"]["ok"] is True, out["critique"]


# ---------------------------------------------------------------------------
# 5) refine: targeted re-verify + stall guard
# ---------------------------------------------------------------------------
def test_refine_targets_only_failed_workers():
    st = initial_state("notepad kholo aur mausam batao", session_id="t-refine-1")
    st["workers"] = ["desktop", "research"]
    st["iterations"] = 0
    st["fix_hint"] = "desktop dobara"
    st["refine_targets"] = []
    st["findings"] = [
        _finding("desktop", "notepad khol diya"),  # blind → target
        _finding("research", "mausam: 32°C, baarish", tool_calls=[_ok_call("web_search")]),
    ]
    out = asyncio.run(N.refine_node(st))
    assert out["dispatch_targets"] == ["desktop"], out
    assert out["refine_targets"] == ["desktop"]
    assert out["iterations"] == 1
    assert "re-verify: desktop" in out["request"]


def test_refine_stall_when_same_targets_fail_twice():
    st = initial_state("notepad kholo", session_id="t-refine-stall")
    st["workers"] = ["desktop"]
    st["iterations"] = 1
    st["fix_hint"] = "phir fail"
    st["refine_targets"] = ["desktop"]  # pichhli baar bhi desktop hi tha
    st["findings"] = [
        _finding("desktop", "notepad khol diya"),
        _finding("desktop", "phir koshish, phir claim"),  # naya evidence → sig alag
    ]
    out = asyncio.run(N.refine_node(st))
    assert out["iterations"] == MAX_GRAPH_ITERATIONS, out
    assert any("STALLED" in ev.get("detail", "") for ev in out["events"]), out["events"]
    # stall ke baad critic persist par jayega (loop khatam)
    assert N.after_critic({"critique": {"ok": False}, "iterations": out["iterations"]}) == "persist"


def test_refine_falls_back_to_all_when_nothing_identified():
    st = initial_state("kuch karo", session_id="t-refine-all")
    st["workers"] = ["desktop", "research"]
    st["iterations"] = 0
    st["fix_hint"] = "vague"
    st["refine_targets"] = []
    st["findings"] = [_finding("research", "thoda data", tool_calls=[_ok_call("web_search")])]
    out = asyncio.run(N.refine_node(st))
    assert sorted(out["dispatch_targets"]) == ["desktop", "research"], out


# ---------------------------------------------------------------------------
# 6) dispatch: subset routing
# ---------------------------------------------------------------------------
def test_dispatch_subset_and_fallbacks():
    from langgraph.types import Send

    st = initial_state("x", session_id="t-d")
    st["workers"] = ["desktop", "research"]
    st["dispatch_targets"] = ["desktop"]
    sends = N.dispatch(st)
    assert isinstance(sends, list) and len(sends) == 1 and isinstance(sends[0], Send)
    st["dispatch_targets"] = []
    sends = N.dispatch(st)
    assert len(sends) == 2, "targets khaali → poori workers list"
    st["workers"] = ["answer"]
    assert N.dispatch(st) == "synthesize"


# ---------------------------------------------------------------------------
# 7) plan: lazy planner fallback
# ---------------------------------------------------------------------------
def test_plan_empty_steps_fallback():
    from jarvis.dspy_engine import modules as dspy_mods
    from jarvis.memory.session_state import get_state

    real_plan = dspy_mods.plan
    dspy_mods.plan = lambda request, workers, context="": {"steps": [], "deliverable": ""}
    try:
        get_state().set_pending_steps([])
        st = initial_state("notepad kholo", session_id="t-plan")
        st["workers"] = ["desktop"]
        out = asyncio.run(N.plan_node(st))
        assert len(out["plan"]) == 1 and "fallback" in out["plan"][0], out["plan"]
        assert any("0 steps" in e for e in out["errors"]), out["errors"]
    finally:
        dspy_mods.plan = real_plan
        get_state().set_pending_steps([])


def test_plan_normal_steps_passthrough():
    from jarvis.dspy_engine import modules as dspy_mods

    real_plan = dspy_mods.plan
    dspy_mods.plan = lambda request, workers, context="": {"steps": ["a", "b"], "deliverable": "d"}
    try:
        st = initial_state("x", session_id="t-plan-ok")
        st["workers"] = ["desktop"]
        out = asyncio.run(N.plan_node(st))
        assert out["plan"] == ["a", "b"] and out["errors"] == []
    finally:
        dspy_mods.plan = real_plan


# ---------------------------------------------------------------------------
# 8) reasoning EVIDENCE balance-sheet line
# ---------------------------------------------------------------------------
def test_reasoning_evidence_line():
    st = initial_state("x", session_id="t-r")
    st["plan"] = ["step1"]
    st["workers"] = ["desktop"]
    st["evidence"] = "e" * 100
    st["tool_evidence"] = [_ok_call(), _fail_call("close_app")]
    st["findings"] = [_finding("desktop", "hawa claim")]
    lines = N.build_reasoning_summary(st)
    ev = [line for line in lines if line.startswith("EVIDENCE:")]
    assert len(ev) == 1, lines
    assert "1/2 tools verified" in ev[0] and "1 failed" in ev[0], ev[0]
    assert "blind: desktop" in ev[0], ev[0]


def test_lazy_finding_normalization():
    f = N._finding("desktop", "   ")
    assert f["lazy"] is True and "khali" in f["summary"]
    f2 = N._finding("desktop", "kaam ho gaya")
    assert f2.get("lazy") is None and f2["summary"] == "kaam ho gaya"


# ---------------------------------------------------------------------------
# script runner (repo style)
# ---------------------------------------------------------------------------
def _run_all() -> int:
    import traceback

    fns = [(n, fn) for n, fn in sorted(globals().items()) if n.startswith("test_") and callable(fn)]
    passed, failed = 0, []
    for name, fn in fns:
        try:
            fn()
            print(f"  ✅ {name}")
            passed += 1
        except Exception as exc:
            print(f"  ❌ {name} — {exc}")
            traceback.print_exc(limit=4)
            failed.append(name)
    print("=" * 66)
    print(
        f"  RESULT: {passed}/{passed + len(failed)} checks passed  "
        f"({'ALL GOOD 🚀' if not failed else 'kuch fail hue ⚠️'})"
    )
    print("=" * 66)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(_run_all())
