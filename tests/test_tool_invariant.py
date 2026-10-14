"""
tests/test_tool_invariant.py — HARD INVARIANT suite (tool-required = proof-required)
===================================================================================

Chalao:  python tests/test_tool_invariant.py     (pytest bhi chalega)

Invariant:

    TOOL REQUIRED → MODEL MUST BE TOOL-BOUND → ACTUAL TOOL CALL → ACTUAL TOOL RESULT → TAB SUCCESS

Mandatory tests (user spec):
  TEST 1  bind_tools() raise kare → wo model unbound fallback ke roop me return NAHI hota
  TEST 2  ek model bind fail, doosra success → doosra bound model use hota hai
  TEST 3  saare models fail → hard failure (koi text-only fallback nahi)
  TEST 4  "notepad open krke hello likho" → asli desktop execution + tool/result evidence;
          LLM-only success invalid
  TEST 5  "hello" → normal jawab, tool call ki zaroorat nahi
  TEST 6  tool execution fail → final answer success claim NAHI karta

Plus:
  ✅ anti-regression (source-level): banned pattern `bound.append(model)` wapas na aaye
  ✅ evidence extraction, claims-vs-negation, AgentResult flags
  ✅ deterministic desktop path: har action ka record, unknown action bhi failure banta hai
"""
from __future__ import annotations

import ast
import asyncio
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["JARVIS_OFFLINE_MODE"] = "1"
os.environ.setdefault("PROVIDER", "demo")

PASS, FAIL = "✅", "❌"
_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, ok, detail))
    print(f"  {PASS if ok else FAIL} {name}" + (f" — {detail}" if detail else ""))


from langchain_core.tools import tool as lc_tool  # noqa: E402

from jarvis.llm.core import FallbackChatModel, ToolBindingError  # noqa: E402


# ---------------------------------------------------------------------------
# test doubles
# ---------------------------------------------------------------------------
class BindModel:
    """Minimal chat-model stub — bind on/off, call ok/boom, aur tool count yaad rakhta hai."""

    def __init__(self, name: str, *, bindable: bool = True, signal: str = "ok", tools=0):
        self.name = name
        self.bindable = bindable
        self.signal = signal
        self.tools = tools

    def bind_tools(self, tools, **kwargs):
        if not self.bindable:
            raise RuntimeError(f"{self.name}: tool calling not supported")
        return BindModel(self.name, bindable=True, signal=self.signal, tools=len(tools or []))

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        from langchain_core.messages import AIMessage
        from langchain_core.outputs import ChatGeneration, ChatResult

        return ChatResult(generations=[ChatGeneration(
            message=AIMessage(content=f"{self.name}|tools={self.tools}|sig={self.signal}"))])


class FakeDesktopTool:
    """Desktop tool stub — name required, output scripted."""

    def __init__(self, name: str, output: str = "✅ ho gaya"):
        self.name = name
        self.output = output
        self.calls = 0

    async def ainvoke(self, args=None, **kwargs):
        self.calls += 1
        return self.output


def _chain(*models, role="brain"):
    return FallbackChatModel(role=role, chain=list(models),
                             targets=[f"fake:{m.name}" for m in models])


# ---------------------------------------------------------------------------
# TEST 1
# ---------------------------------------------------------------------------
def test_1_bind_failure_not_returned_unbound() -> None:
    print("\n[TEST 1] bind_tools() raise → model unbound fallback ke roop me return NAHI hota")
    bad = BindModel("no-tools", bindable=False)
    good = BindModel("good")
    bound = _chain(bad, good).bind_tools(["t1", "t2"])
    check("TEST 1: failing model chain me nahi hai (object identity check)",
          all(m is not bad for m in bound.chain), f"chain={len(bound.chain)}")
    check("TEST 1: failing model ka naam/target bhi chain se hata",
          "fake:no-tools" not in bound.targets, str(bound.targets))
    check("TEST 1: failure reason preserve hua (debugging ke liye)",
          bound.bind_failures and "not supported" in bound.bind_failures[0]["error"],
          str(bound.bind_failures)[:90])
    check("TEST 1: only successfully-bound models count hote hain",
          bound.bound_model_count == 1 and bound.tools_bound is True)


# ---------------------------------------------------------------------------
# TEST 2
# ---------------------------------------------------------------------------
def test_2_second_model_used_when_first_fails() -> None:
    print("\n[TEST 2] pehla bind fail, doosra bind success → doosra bound model use hota hai")
    bad, good = BindModel("A-bad", bindable=False), BindModel("B-good")
    bound = _chain(bad, good).bind_tools(["t1", "t2", "t3"])
    out = bound._generate([]).generations[0].message.content
    check("TEST 2: doosra (bound) model hi answer de raha hai", "B-good" in out, out)
    check("TEST 2: us model par tools actually bound hain (tools=3)", "tools=3" in out, out)
    check("TEST 2: chain me sirf bound model hai", len(bound.chain) == 1)


# ---------------------------------------------------------------------------
# TEST 3
# ---------------------------------------------------------------------------
def test_3_all_fail_is_hard_failure() -> None:
    print("\n[TEST 3] saare models fail → hard failure, koi text-only fallback nahi")
    raised = None
    try:
        _chain(BindModel("a", bindable=False), BindModel("b", bindable=False)).bind_tools(["t1"])
    except Exception as exc:  # noqa: BLE001
        raised = exc
    check("TEST 3: ToolBindingError raise hota hai", isinstance(raised, ToolBindingError),
          type(raised).__name__ if raised else "None")
    msg = str(raised or "")
    check("TEST 3: exact spec message mojood hai",
          "No configured model successfully supports tool calling for this execution." in msg,
          msg[:80])
    check("TEST 3: har model ki wajah + actionable hint bhi milta hai",
          "fake:a" in msg and "doctor" in msg)
    # graph path bhi loud hona chahiye (koi plain-text answer nahi)
    import jarvis.graph.nodes as N

    old_off, old_prov, old_get = N.OFFLINE_MODE, N.PROVIDER, N.get_chat_model
    N.OFFLINE_MODE, N.PROVIDER = False, "gemini"
    N.get_chat_model = lambda role="brain", **kw: _chain(BindModel("x", bindable=False))
    run = asyncio.new_event_loop()
    try:
        res = run.run_until_complete(N._run_react_subagent("answer", "notes banao", tools=[tool_stub]))
    finally:
        run.close()
        N.OFFLINE_MODE, N.PROVIDER, N.get_chat_model = old_off, old_prov, old_get
    check("TEST 3: subagent path me bhi error set hota hai (text-only success nahi)",
          bool(res.error) and "tool_binding_failed" in res.error, str(res.error)[:70])
    check("TEST 3: calls khali hain (kuch chala hi nahi)", res.calls == [])


@lc_tool
def tool_stub(x: str = "") -> str:
    """Test tool."""
    return "ok"


# ---------------------------------------------------------------------------
# TEST 4
# ---------------------------------------------------------------------------
def test_4_desktop_action_requires_real_execution() -> None:
    asyncio.run(_test_4_desktop_action_requires_real_execution_impl())


async def _test_4_desktop_action_requires_real_execution_impl() -> None:
    print("\n[TEST 4] 'notepad open krke hello likho' → asli execution + evidence; LLM-only success invalid")
    import jarvis.graph.nodes as N
    from jarvis.graph.nodes import SubAgentRun, desktop_worker, honesty_guard

    fake_open = FakeDesktopTool("open_any_app", "✅ Notepad khul gaya")
    fake_type = FakeDesktopTool("type_text", "⌨️ likh diya: hello")

    old_sub, old_tools = N._run_react_subagent, N._desktop_tools
    # sub-agent jhooth bolta hai ("Ho gaya ✅") par koi tool call nahi karta
    async def lying_subagent(role, task, tools, raw_request=""):
        return SubAgentRun(text="Ho gaya boss ✅ notepad khol diya aur hello likh diya")

    N._run_react_subagent = lying_subagent
    N._desktop_tools = lambda: [fake_open, fake_type]
    try:
        state = {"request": "notepad open krke hello likho", "session_id": "t4",
                 "findings": [], "errors": [], "events": []}
        out = await desktop_worker(state)
    finally:
        N._run_react_subagent, N._desktop_tools = old_sub, old_tools

    calls = out.get("tool_evidence") or []
    check("TEST 4: LLM ne jhooth bola, par deterministic executor ne asli tools chalaye",
          fake_open.calls == 1 and fake_type.calls == 1, f"open={fake_open.calls} type={fake_type.calls}")
    check("TEST 4: tool results state me store hote hain (tool_evidence)",
          len(calls) == 2 and all(c.get("source") == "deterministic" for c in calls), str(len(calls)))
    check("TEST 4: execution_required=True set hota hai", out.get("execution_required") is True)

    # ab honest verdict nikalo — evidence ke saath
    state.update(out)
    verdict_answer, guard = honesty_guard(state, "Ho gaya boss ✅ notepad khol diya, hello likh diya")
    check("TEST 4: evidence hone par success claim allowed hai (guard nahi lagta)",
          guard == "" and bool(out.get("actions_done")), f"guard={guard[:40]!r}")

    # negative half: sub-agent jhooth + tools fail → success claim nahi ban sakta
    fake_open2 = FakeDesktopTool("open_any_app", "❌ notepad nahi mila")
    fake_type2 = FakeDesktopTool("type_text", "❌ typing fail: xdotool chahiye")
    N._run_react_subagent, N._desktop_tools = lying_subagent, (lambda: [fake_open2, fake_type2])
    try:
        state2 = {"request": "notepad open krke hello likho", "session_id": "t4b",
                  "findings": [], "errors": [], "events": []}
        out2 = await desktop_worker(state2)
    finally:
        N._run_react_subagent, N._desktop_tools = old_sub, old_tools
    state2.update(out2)
    ans2, guard2 = honesty_guard(state2, "Ho gaya boss ✅ notepad khol diya, hello likh diya")
    check("TEST 4: tools fail + LLM 'kar diya' → answer me success claim nahi bachta",
          "kar diya" not in ans2 and "khol diya" not in ans2, ans2[:80])
    check("TEST 4: failure explicitly boli jati hai",
          "fail hua" in ans2 or "nahi hua" in ans2, ans2[:70])
    check("TEST 4: guard reason + status honest hote hain",
          guard2 == "all actions failed" and state2.get("status") == "failed", f"{guard2!r}/{state2.get('status')}")

    # integration: asli agent run me bhi evidence capture hota hai (sandbox me tools fail karte hain)
    from jarvis.runtime.agent import run_agent

    res = await run_agent("notepad open krke hello likho", session_id="t4-int")
    check("TEST 4 (integration): desktop request par tool evidence record hota hai",
          len(res.tool_evidence) >= 1, f"{len(res.tool_evidence)} records")
    check("TEST 4 (integration): exec required + status/answer honest",
          res.execution_required is True
          and (res.verified_execution or res.status == "failed")
          and ("fail" in res.answer.lower() or "nahi hua" in res.answer.lower()),
          f"status={res.status} verified={res.verified_execution}")


# ---------------------------------------------------------------------------
# TEST 5
# ---------------------------------------------------------------------------
def test_5_plain_question_needs_no_tool() -> None:
    asyncio.run(_test_5_plain_question_needs_no_tool_impl())


async def _test_5_plain_question_needs_no_tool_impl() -> None:
    print("\n[TEST 5] 'hello' → normal jawab, tool call ki zaroorat nahi")
    from jarvis.runtime.agent import run_agent

    res = await run_agent("hello", session_id="t5")
    check("TEST 5: jawab milta hai (koi exception nahi)", bool(res.answer.strip()), res.answer[:60])
    check("TEST 5: execution required nahi hai", res.execution_required is False)
    check("TEST 5: status 'done' hi rehta hai (falsely failed nahi)",
          res.status in ("done", "answer"), res.status)
    check("TEST 5: koi tool evidence invent nahi hota", res.tool_evidence == [])
    # aur chhota sawaal bhi bina tool chalna chahiye
    res2 = await run_agent("tum kaun ho", session_id="t5b")
    check("TEST 5: identity sawaal bhi normal jawab deta hai", bool(res2.answer.strip()), res2.answer[:50])


# ---------------------------------------------------------------------------
# TEST 6
# ---------------------------------------------------------------------------
def test_6_tool_failure_no_success_claim() -> None:
    asyncio.run(_test_6_tool_failure_no_success_claim_impl())


async def _test_6_tool_failure_no_success_claim_impl() -> None:
    print("\n[TEST 6] tool execution fail → final answer success claim NAHI karta")
    import jarvis.graph.nodes as N
    from jarvis.graph.nodes import SubAgentRun, desktop_worker, honesty_guard

    failing_open = FakeDesktopTool("open_any_app", "❌ nahi mila")
    failing_type = FakeDesktopTool("type_text", "❌ typing fail")

    old_sub, old_tools = N._run_react_subagent, N._desktop_tools

    async def claiming_subagent(role, task, tools, raw_request=""):
        return SubAgentRun(text="Bilkul ho gaya ✅ notepad khol diya aur text type kar diya.")

    N._run_react_subagent, N._desktop_tools = claiming_subagent, (lambda: [failing_open, failing_type])
    try:
        state = {"request": "notepad kholo aur hello likho", "session_id": "t6",
                 "findings": [], "errors": [], "events": []}
        out = await desktop_worker(state)
    finally:
        N._run_react_subagent, N._desktop_tools = old_sub, old_tools
    state.update(out)

    answer, guard = honesty_guard(state, "Bilkul ho gaya ✅ notepad khol diya aur text type kar diya.")
    check("TEST 6: answer me 'ho gaya/kar diya' claim nahi bacha",
          not any(k in answer for k in ("ho gaya", "kar diya", "khol diya")), answer[:80])
    check("TEST 6: failure clearly likhi hai", "fail hua" in answer or "❌" in answer, answer[:60])
    check("TEST 6: status failed set hua", state.get("status") == "failed", str(state.get("status")))
    check("TEST 6: guard reason record hua", guard == "all actions failed", guard)

    # AgentResult level: verified_execution False + status honest
    from jarvis.runtime.agent import AgentResult

    res = AgentResult("x")
    res.findings = [{"tool_calls": out.get("tool_evidence", [])}]
    res.recompute_execution_flags()
    check("TEST 6: AgentResult.verified_execution False rehta hai (fake success block)",
          res.verified_execution is False)
    res2 = AgentResult("y")
    res2.findings = [{"tool_calls": [{"name": "t", "ok": True, "output": "✅ done"}]}]
    res2.recompute_execution_flags()
    check("TEST 6: asli ok evidence par hi verified_execution True hota hai",
          res2.verified_execution is True)


# ---------------------------------------------------------------------------
# anti-regression (source-level)
# ---------------------------------------------------------------------------
BANNED_PATTERN = re.compile(r"^\s*bound\.append\(model\)\s*$", re.MULTILINE)


def test_anti_regression_source() -> None:
    print("\n[ANTI-REGRESSION] source-level guards (future changes se bypass na ho)")
    core_src = (ROOT / "jarvis" / "llm" / "core.py").read_text(encoding="utf-8")
    check("banned pattern `bound.append(model)` dobara nahi aaya",
          not BANNED_PATTERN.search(core_src), "pattern mila!" if BANNED_PATTERN.search(core_src) else "clean")
    check("spec message source me pinned hai",
          "No configured model successfully supports tool calling for this execution." in core_src)
    check("silent-fallback BAN comment maujood hai", "SILENT-FALLBACK BAN" in core_src)

    # AST check: FallbackChatModel.bind_tools ke try/except me except branch *model ko append nahi* karta
    tree = ast.parse(core_src)
    appends_in_except: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "bind_tools":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Try):
                    for handler in sub.handlers:
                        for hnode in ast.walk(handler):
                            if (isinstance(hnode, ast.Call)
                                    and isinstance(hnode.func, ast.Attribute)
                                    and hnode.func.attr == "append"):
                                for a in hnode.args:
                                    if isinstance(a, ast.Name) and a.id == "model":
                                        appends_in_except.append(ast.dump(hnode)[:60])
    check("AST check: except branch me bare `model` append nahi hota", not appends_in_except,
          str(appends_in_except)[:80])
    check("except branch me `continue` hai (chain se drop)",
          "continue" in core_src.split("def bind_tools")[1].split("def bound_model_count")[0])

    nodes_src = (ROOT / "jarvis" / "graph" / "nodes.py").read_text(encoding="utf-8")
    check("graph: ToolBindingError upstream propagate hota hai", "ToolBindingError" in nodes_src)
    check("graph: hard invariant synthesize me lagta hai",
          "execution_required but zero tool evidence" in nodes_src)
    check("graph: respond_node status ko blindly 'done' nahi karta",
          'status = state.get("status") or "done"' in nodes_src)
    check("graph: unknown desktop action bhi failure record banta hai",
          "unsupported_action" in nodes_src)
    check("desktop: har action ka record enforce hota hai",
          "action_record_invariant" in nodes_src)


def test_evidence_helpers() -> None:
    print("\n[EVIDENCE] helpers sahi kaam karte hain")
    from jarvis.runtime.evidence import (
        claims_execution,
        evidence_records,
        has_verified_evidence,
        is_tool_required,
        verify_execution,
    )

    state = {"findings": [{"tool_calls": [{"name": "open_any_app", "output": "✅ khul gaya", "ok": True}]}]}
    check("evidence: findings[].tool_calls se records nikalte hain",
          len(evidence_records(state)) == 1 and has_verified_evidence(state))
    check("evidence: workers se tool-required detect hota hai",
          is_tool_required({}, workers=["desktop"]) and not is_tool_required({}, workers=["answer"]))
    check("evidence: flat state keys bhi cover hote hain",
          len(evidence_records({"tool_evidence": [{"name": "x", "output": "✅", "ok": True}]})) == 1)
    check("claims: execution claims pakde jate hain",
          bool(claims_execution("notepad khol diya")) and bool(claims_execution("I opened notepad")))
    check("claims: negation wale nahi pakde jate",
          claims_execution("maine khol diya nahi") == [] and claims_execution("did not open") == [])
    v = verify_execution({"tool_evidence": [{"name": "a", "ok": True, "output": "✅"}]}, "ho gaya boss")
    check("verdict: ok evidence → verified True, koi failure reason nahi",
          v.verified and v.failure_reason == "")
    v2 = verify_execution({"tool_evidence": [{"name": "a", "ok": False, "output": "❌"}]}, "ho gaya")
    check("verdict: fail record → verified False + reason", not v2.verified and v2.failure_reason)


def main() -> int:
    print("\n" + "=" * 70)
    print("  NIKHILAI — tool-required HARD INVARIANT suite")
    print("=" * 70)
    test_1_bind_failure_not_returned_unbound()
    test_2_second_model_used_when_first_fails()
    test_3_all_fail_is_hard_failure()

    test_4_desktop_action_requires_real_execution()
    test_5_plain_question_needs_no_tool()
    test_6_tool_failure_no_success_claim()

    test_anti_regression_source()
    test_evidence_helpers()

    passed = sum(1 for _n, ok, _d in _results if ok)
    total = len(_results)
    print("\n" + "=" * 70)
    print(f"  RESULT: {passed}/{total} checks passed  ({'ALL GOOD 🚀' if passed == total else 'kuch fail hue ⚠️'})")
    print("=" * 70 + "\n")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
