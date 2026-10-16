"""
jarvis/dspy_engine/signatures.py — DSPy Signatures (typed reasoning contracts)
=============================================================================

DSPy ka core idea: prompt haath se likhne ke bajaye **signature** declare karo,
aur optimizer (GEPA/MIPROv2) khud best instructions + few-shot examples dhoondh le.
Isse Jarvis ki reasoning:
  * zyada accurate   (compiled prompts + bootstrapped demos)
  * zyada fast       (chhota model + chhota prompt hi kaam kar jaata hai)
  * zyada sasta      (fast model router decisions leta hai, brain sirf synthesis karta hai)
"""
from __future__ import annotations

# dspy optional nahi hai (core dependency hai) — par agar kisi machine par install fail ho jaye
# to poora agent import se na toote. Har DSPy module ka deterministic fallback maujood hai.
try:  # pragma: no cover - environment dependent
    import dspy

    HAS_DSPY = True
except Exception as _exc:  # noqa: BLE001
    HAS_DSPY = False
    _DSPY_ERROR = f"{type(_exc).__name__}: {_exc}"

    class _Stub:
        """dspy na hone par bhi class definitions chalein (runtime par fallback use hota hai)."""

        def __init__(self, *a: object, **kw: object) -> None:
            self.__doc__ = kw.get("desc", "") or ""

        def __call__(self, *a: object, **kw: object) -> None:  # pragma: no cover
            return None

    class _StubField:
        def __init__(self, *a: object, **kw: object) -> None: ...

    class _DspyStub:
        Signature = _Stub
        InputField = _StubField
        OutputField = _StubField

    dspy = _DspyStub()  # type: ignore[assignment]


class RouteIntent(dspy.Signature):
    """Classify the owner's request and pick the workers that must run.

    Available workers:
      research : web search + page fetching + parallel multi-source reading
      browser  : real browser automation (click/type/login/JS-heavy/bot-protected sites)
      code     : write + run + self-heal code, data crunching, automation scripts
      desktop  : open/close apps, find files, save notes/notepad, system control
      memory   : store or recall facts about the owner
      answer   : no tool needed — direct conversational answer
    """

    request: str = dspy.InputField(desc="owner ki raw Hinglish request")
    history: str = dspy.InputField(desc="recent conversation context (short)")
    workers: list[str] = dspy.OutputField(desc="1-3 workers from: research, browser, code, desktop, memory, answer")
    reason: str = dspy.OutputField(desc="1 line: kyun ye workers")
    needs_clarification: bool = dspy.OutputField(desc="true agar request itni ambiguous hai ki guess karna galat hoga")


class MakePlan(dspy.Signature):
    """Turn a request into a short, executable plan of concrete steps for the chosen workers."""

    request: str = dspy.InputField()
    workers: list[str] = dspy.InputField()
    context: str = dspy.InputField(desc="owner facts + memory + available tools summary")
    steps: list[str] = dspy.OutputField(desc="3-7 concrete steps, har step ek action ho (guess nahi)")
    deliverable: str = dspy.OutputField(desc="exact deliverable: file? note? JSON? answer?")


class WriteCode(dspy.Signature):
    """Write a complete, runnable program that solves the task. Output ONLY code."""

    task: str = dspy.InputField()
    language: str = dspy.InputField()
    feedback: str = dspy.InputField(desc="'none' ya pichhle attempt ka traceback/error")
    previous: str = dspy.InputField(desc="'none' ya pichhla code jo fail hua")
    code: str = dspy.OutputField(desc="complete runnable program, koi explanation nahi")


class ExtractFields(dspy.Signature):
    """Extract structured fields from raw page/scrape text. Missing field => null (never guess)."""

    source_text: str = dspy.InputField()
    source_url: str = dspy.InputField()
    field_names: list[str] = dspy.InputField(desc="chahiye data ke field names, e.g. ['product','price']")
    data: dict = dspy.OutputField(desc="field → value (string/number/null). Sirf jo page me tha.")


class NormalizeRows(dspy.Signature):
    """Normalize several sites' extracted rows into one consistent schema so they can be compared."""

    rows_json: str = dspy.InputField()
    field_names: list[str] = dspy.InputField()
    normalized_json: str = dspy.OutputField(desc="JSON list, same fields, comparable units/format")


class CritiqueAnswer(dspy.Signature):
    """Harshly critique the draft answer against the owner's request and evidence."""

    request: str = dspy.InputField()
    draft: str = dspy.InputField()
    evidence: str = dspy.InputField(desc="tool outputs / sources ka digest")
    ok: bool = dspy.OutputField(desc="true agar answer sach + complete + request ke mutabik hai")
    score: float = dspy.OutputField(desc="0.0 - 1.0 quality score")
    missing: list[str] = dspy.OutputField(desc="jo cheezein missing/galat hain")
    fix_hint: str = dspy.OutputField(desc="agla attempt kaise better kare")


class FinalAnswer(dspy.Signature):
    """Write the final answer for the owner in Hinglish. Short, factual, no hallucination."""

    request: str = dspy.InputField()
    results: str = dspy.InputField(desc="saare workers ke outputs (evidence)")
    critique: str = dspy.InputField(desc="critic ka feedback (khali ho sakta hai)")
    answer: str = dspy.OutputField(desc="Hinglish final answer, owner ko address karke, max 200 words")


class VoicePolish(dspy.Signature):
    """Convert a written result into short spoken Hinglish (voice ke liye — natural, warm)."""

    written: str = dspy.InputField()
    spoken: str = dspy.OutputField(desc="2-4 sentences, bolne me natural, numbers/naam clearly")


class WebsiteTaskSpec(dspy.Signature):
    """Turn an owner request into a precise browser-agent task prompt + extraction fields."""

    request: str = dspy.InputField()
    known_sites: list[str] = dspy.InputField()
    browser_task: str = dspy.OutputField(desc="English, step-by-step, unambiguous browser-agent task")
    fields_to_extract: list[str] = dspy.OutputField(desc="fields to extract from each site")
    save_note_as: str = dspy.OutputField(desc="note file ka naam (short, snake_case)")
