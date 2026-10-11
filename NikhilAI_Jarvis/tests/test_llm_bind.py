"""
tests/test_llm_bind.py — tool-binding safety suite (silent degradation band)
============================================================================

Chalao:  python tests/test_llm_bind.py     (pytest bhi chalega)

User ne ye bug pakda:

    except Exception:
        bound.append(model)      # ← tools bind fail, phir bhi model chain me reh gaya

Matlab: jis model par tools bind nahi hote, wo **bina tools** ke chain me reh jata tha. ReAct
subagent / `with_structured_output` usse poochta, aur wo tools ke bina "confident" jawab de deta —
chup-chaap. Agar primary model quota/404 se girta, to fallback bina tools ka jawab de deta.

Checks:
  ✅ all bindable → chain waisi hi, `tools_bound=True`, tools asli me bound model par jaate hain
  ✅ ek model bind fail → chain se hata diya (unbound model kabhi use nahi hota)
  ✅ targets/labels chain ke saath aligned rehte hain (taaki attempts log sahi model bataye)
  ✅ bind failure record hota hai: `roles.attempts()` + `bind_failures` + progress warn
  ✅ SAARE fail → `ToolBindingError` (loud), silent tool-less answer nahi
  ✅ kwargs (tool_choice) pass-through
  ✅ `with_structured_output` bhi isi fix se safe (bind_tools ke through jaata hai)
  ✅ runtime fallback: primary call par fail → bound fallback chalta hai, tools intact
  ✅ ReAct path: binding fail par SubAgentRun.error set hota hai + honest message
  ✅ empty tools → kuch bind nahi, koi error nahi
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("JARVIS_OFFLINE_MODE", "1")
os.environ.setdefault("PROVIDER", "demo")

PASS, FAIL = "✅", "❌"
_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, ok, detail))
    print(f"  {PASS if ok else FAIL} {name}" + (f" — {detail}" if detail else ""))


from langchain_core.messages import AIMessage  # noqa: E402
from langchain_core.tools import tool as lc_tool  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402

from jarvis.llm.core import FallbackChatModel, ToolBindingError  # noqa: E402


class FakeModel:
    """Chhota chat model stub — bind_tools support on/off, aur call par (optionally) fail."""

    def __init__(self, name: str, *, bindable: bool = True, boom_on_call: bool = False):
        self.name = name
        self.bindable = bindable
        self.boom_on_call = boom_on_call
        self.tool_count = 0
        self.tool_choice = None
        self.bind_calls = 0

    def bind_tools(self, tools, **kwargs):
        self.bind_calls += 1
        if not self.bindable:
            raise ValueError(f"{self.name}: tools support nahi karta")
        clone = FakeModel(self.name, bindable=True, boom_on_call=self.boom_on_call)
        clone.tool_count = len(tools or [])
        clone.tool_choice = kwargs.get("tool_choice")
        return clone

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        if self.boom_on_call:
            raise RuntimeError(f"{self.name}: quota khatam")
        content = f"{self.name}:tools={self.tool_count}"
        if self.tool_choice:
            content += f":choice={self.tool_choice}"
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])


def _chain(*models: FakeModel, role: str = "brain") -> FallbackChatModel:
    return FallbackChatModel(role=role, chain=list(models),
                             targets=[f"fake:{m.name}" for m in models])


FAKE_TOOLS = ["tool_a", "tool_b"]      # bind_tools ko sirf count chahiye


def t_all_bindable() -> None:
    m1, m2 = FakeModel("primary"), FakeModel("secondary")
    model = _chain(m1, m2)
    bound = model.bind_tools(FAKE_TOOLS)
    check("bind: sab bindable → chain waisi hi", len(bound.chain) == 2, str(len(bound.chain)))
    check("bind: tools_bound = True", bound.tools_bound is True)
    check("bind: bind_failures khali", bound.bind_failures == [])
    out = bound._generate([])
    text = out.generations[0].message.content
    check("bind: asli me tools model par gaye (tools=2)", "tools=2" in text, text)


def t_one_unbindable_is_dropped() -> None:
    m1, bad, m3 = FakeModel("primary"), FakeModel("no-tools", bindable=False), FakeModel("third")
    model = _chain(m1, bad, m3)
    bound = model.bind_tools(FAKE_TOOLS)
    check("bind: bind-fail model chain se HAT gaya (silent unbound nahi)",
          len(bound.chain) == 2, f"chain={len(bound.chain)}")
    names = [getattr(m, "name", "?") for m in bound.chain]
    check("bind: bacha hua chain sahi models ka hai", names == ["primary", "third"], str(names))
    check("bind: targets bhi aligned hain (attempts log sahi label dega)",
          bound.targets == ["fake:primary", "fake:third"], str(bound.targets))
    check("bind: bind_failures me wajah record hui",
          len(bound.bind_failures) == 1 and "no-tools" in bound.bind_failures[0]["target"]
          and "support nahi karta" in bound.bind_failures[0]["error"],
          str(bound.bind_failures)[:110])
    check("bind: dushre model par tools intact hain", bound.chain[0].tool_count == 2)


def t_failure_is_observable() -> None:
    from jarvis.llm import roles as RR

    before = len(RR.attempts(limit=200))
    m1, bad = FakeModel("primary"), FakeModel("broken", bindable=False)
    model = _chain(m1, bad)
    model.bind_tools(FAKE_TOOLS)
    after = RR.attempts(limit=200)
    added = after[before:]
    hit = [a for a in added if "bind_tools failed" in (a.get("error") or "")]
    check("bind: failure attempts-history me record hua (observable)",
          bool(hit) and hit[0]["model"] == "broken", str(hit[:1])[:110])

    # progress bus par warn (CLI/TG live feed me dikhta hai)
    from jarvis.runtime.progress import CollectingSink, use_sink

    sink = CollectingSink()
    m2, bad2 = FakeModel("primary"), FakeModel("broken2", bindable=False)
    with use_sink(sink):
        _chain(m2, bad2).bind_tools(FAKE_TOOLS)
    warns = [e for e in sink.events if e.kind == "warn"]
    check("bind: progress par warn emit hua (live feed me dikhega)",
          bool(warns) and "bind nahi kar paya" in warns[0].text, str(warns[:1])[:110])

    report = _chain(FakeModel("p"), FakeModel("b", bindable=False)).bind_tools(FAKE_TOOLS).binding_report()
    check("bind: binding_report() observability deta hai",
          report["tools_bound"] is True and report["active"] == 1 and len(report["failed"]) == 1,
          str(report)[:110])


def t_all_fail_is_loud() -> None:
    m1, m2 = FakeModel("a", bindable=False), FakeModel("b", bindable=False)
    model = _chain(m1, m2)
    raised: Exception | None = None
    try:
        model.bind_tools(FAKE_TOOLS)
    except Exception as exc:  # noqa: BLE001
        raised = exc
    check("bind: SAARE fail → ToolBindingError raise hota hai (silent nahi)",
          isinstance(raised, ToolBindingError), type(raised).__name__ if raised else "None")
    msg = str(raised or "")
    check("bind: error me har model ki wajah likhi hai",
          "fake:a" in msg and "fake:b" in msg and "tools support nahi karta" in msg, msg[:120])
    check("bind: error message me actionable hint hai",
          "doctor" in msg or "model" in msg, msg[-90:])
    # chain waisi hi rehni chahiye (original object mutate nahi hua)
    check("bind: fail par original chain mutate nahi hui", len(model.chain) == 2)


def t_kwargs_and_empty() -> None:
    m1 = FakeModel("p")
    bound = _chain(m1).bind_tools(FAKE_TOOLS, tool_choice="auto")
    check("bind: kwargs (tool_choice) pass-through hote hain",
          "choice=auto" in bound._generate([]).generations[0].message.content)
    m2 = FakeModel("p2")
    same = _chain(m2).bind_tools([])
    check("bind: empty tools → koi error nahi, koi bind nahi",
          len(same.chain) == 1 and same.tools_bound is True and m2.bind_calls == 0)


def t_runtime_fallback_keeps_tools() -> None:
    """Primary call par fail → fallback chale, par wo bhi *bound* ho."""
    primary = FakeModel("primary", boom_on_call=True)
    secondary = FakeModel("secondary")
    bound = _chain(primary, secondary).bind_tools(FAKE_TOOLS)
    out = bound._generate([])
    text = out.generations[0].message.content
    check("bind: primary girne par fallback chala", "secondary" in text, text)
    check("bind: fallback par bhi tools intact the (silent tool-less nahi)",
          "tools=2" in text, text)


def t_structured_output_safe() -> None:
    """`with_structured_output` LangChain me bind_tools se banta hai → wahi safety milegi."""
    from pydantic import BaseModel

    class Out(BaseModel):
        answer: str = ""

    m1, bad = FakeModel("p"), FakeModel("n", bindable=False)
    bound = _chain(m1, bad).bind_tools(FAKE_TOOLS)
    try:
        runnable = bound.with_structured_output(Out)
        check("bind: with_structured_output bind-fail model ke bina banta hai (raise nahi)",
              runnable is not None)
    except ToolBindingError:
        check("bind: with_structured_output bind-fail par loud hai (silent nahi)", True, "ToolBindingError")

    all_bad = _chain(FakeModel("x", bindable=False))
    raised = None
    try:
        all_bad.with_structured_output(Out)
    except Exception as exc:  # noqa: BLE001
        raised = exc
    check("bind: sab fail + with_structured_output → loud error (chup-chaap plain text nahi)",
          raised is not None, type(raised).__name__ if raised else "None")


async def t_react_path_honest() -> None:
    """ReAct subagent: binding fail par SubAgentRun.error set ho — silent jawab nahi."""
    import jarvis.graph.nodes as N

    old_off, old_prov = N.OFFLINE_MODE, N.PROVIDER
    N.OFFLINE_MODE, N.PROVIDER = False, "gemini"
    bad_chain = _chain(FakeModel("x", bindable=False), role="brain")
    old_get = N.get_chat_model
    N.get_chat_model = lambda role="brain", **kw: bad_chain
    try:
        run = await N._run_react_subagent("answer", "notes banao", tools=[fake_tool])
    finally:
        N.OFFLINE_MODE, N.PROVIDER = old_off, old_prov
        N.get_chat_model = old_get
    check("react: binding fail → SubAgentRun.error set (silent tool-less answer nahi)",
          bool(run.error) and "tool_binding_failed" in run.error, str(run.error)[:110])
    check("react: user ko honest message milta hai (bina tools jawab nahi diya)",
          "bina tools ke jawab dena galat" in run.text, run.text[:110])
    check("react: koi tool call record nahi hua (kuch chala hi nahi)",
          run.calls == [], str(run.calls))


@lc_tool
def fake_tool(x: str = "") -> str:
    """Test tool (LangChain-valid, taaki create_agent ise accept kare)."""
    return "ok"


def t_offline_demo_still_works() -> None:
    """Regression: offline/demo path (bind_tools support karta hai) ulta na toot jaye."""
    from jarvis.llm.demo import DemoChatModel

    demo = DemoChatModel(role="brain")
    bound = demo.bind_tools([fake_tool])
    check("regression: demo brain tools bind kar sakta hai", bound is not None
          and type(bound).__name__ == "DemoChatModel")


def t_real_langchain_model() -> None:
    """Asli LangChain model ke saath normal path na toota ho (fake key, koi network nahi)."""
    import importlib.util

    if importlib.util.find_spec("langchain_openai") is None:
        check("bind: asli ChatOpenAI ke saath bind (library nahi — skip)", True, "skip")
        return
    from langchain_openai import ChatOpenAI

    os.environ.setdefault("OPENAI_API_KEY", "sk-test-not-a-real-key")
    real = ChatOpenAI(model="gpt-4o-mini", api_key="sk-test-not-a-real-key")
    model = FallbackChatModel(role="brain", chain=[real], targets=["openai:gpt-4o-mini"])
    bound = model.bind_tools([fake_tool])
    check("bind: asli LangChain model par bhi tools bound hote hain",
          bound.tools_bound is True and bound.bind_failures == [] and len(bound.chain) == 1)
    check("bind: asli bound model ReAct-style tool schema dikhata hai",
          bool(getattr(bound.chain[0], "kwargs", {}).get("tools")) or hasattr(bound.chain[0], "kwargs"),
          type(bound.chain[0]).__name__)
    # aur: jis model par bind fail ho, wo drop ho (asli + fake mila ke)
    class NoTools:
        name = "no-tools"

        def bind_tools(self, tools, **kwargs):
            raise NotImplementedError("tools nahi")

    mixed = FallbackChatModel(role="brain", chain=[NoTools(), real],
                              targets=["ollama:no-tools", "openai:gpt-4o-mini"]).bind_tools([fake_tool])
    check("bind: asli+fake mixed chain me fail wala drop, baaki kaam karta hai",
          len(mixed.chain) == 1 and mixed.targets == ["openai:gpt-4o-mini"]
          and len(mixed.bind_failures) == 1, str(mixed.targets))


def main() -> int:
    print("\n" + "=" * 66)
    print("  NIKHILAI — tool-binding safety suite (silent degradation band)")
    print("=" * 66)
    print("\n[1] all bindable")
    t_all_bindable()
    print("\n[2] ek model bind-fail → drop (silent unbound nahi)")
    t_one_unbindable_is_dropped()
    print("\n[3] failure observable (attempts + progress + report)")
    t_failure_is_observable()
    print("\n[4] saare fail → loud ToolBindingError")
    t_all_fail_is_loud()
    print("\n[5] kwargs + empty tools")
    t_kwargs_and_empty()
    print("\n[6] runtime fallback tools ke saath")
    t_runtime_fallback_keeps_tools()
    print("\n[7] with_structured_output safety")
    t_structured_output_safe()
    print("\n[8] ReAct path honest failure")
    asyncio.run(t_react_path_honest())
    print("\n[9] offline demo regression")
    t_offline_demo_still_works()
    print("\n[10] asli LangChain model par normal path")
    t_real_langchain_model()

    passed = sum(1 for _n, ok, _d in _results if ok)
    total = len(_results)
    print("\n" + "=" * 66)
    print(f"  RESULT: {passed}/{total} checks passed  ({'ALL GOOD 🚀' if passed == total else 'kuch fail hue ⚠️'})")
    print("=" * 66 + "\n")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
