"""
jarvis/dspy_engine/optimize.py — DSPy optimization (Jarvis ko khud ko better banane do)
=====================================================================================

Do tarah ka optimization:

  1. BOOTSTRAP (sasta, fast)  — BootstrapFewShot: accha examples se few-shot demos banao.
  2. GEPA / MIPROv2 (powerful) — prompt instructions + demos jointly optimize karo.
     GEPA reflective evolution karta hai: failures padhta hai, prompt ko rewrite karta hai.

Metric: hum "trajectory-level" score dete hain — routing sahi hui? plan executable hai?
answer me evidence hai? Isse compiled program real Jarvis behaviour par optimized hota hai.

Usage:
    python scripts/optimize_dspy.py --mode bootstrap     # 2 min, sasta
    python scripts/optimize_dspy.py --mode gepa --iters 60   # best quality
    python scripts/optimize_dspy.py --report             # ab tak ke compile dikhao

Compiled programs `optimized/<name>.json` me save hote hain, aur runtime par
`modules.ensure_ready()` khud load kar leta hai — koi code change nahi chahiye.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from ..config import OFFLINE_MODE, OPTIMIZED_DIR, PROVIDER

TRAINSET_PATH = OPTIMIZED_DIR / "trainset.json"


# ---------------------------------------------------------------------------
# training data (Hinglish, real-world Jarvis tasks)
# ---------------------------------------------------------------------------
DEFAULT_TRAINSET: list[dict[str, Any]] = [
    {
        "request": "jarvis jaao browser pr alag alag website se laptop ke top 5 price laao aur note pad me laptop_prices naam rakh ke save kardo",
        "workers": ["browser", "desktop"],
        "steps": [
            "Identify 3-5 trusted sites (Amazon, Flipkart, Croma)",
            "Run parallel browser agents to extract laptop model + price from each",
            "Normalize prices and pick top 5 cheapest",
            "Save note as laptop_prices in the notes folder",
        ],
        "deliverable": "note file 'laptop_prices' with top-5 table + sources",
    },
    {
        "request": "mujhe is mahine ka sales data analyse karke ek chart bana do",
        "workers": ["code"],
        "steps": [
            "Sales CSV ka path dhoondho",
            "pandas se load + clean karo",
            "monthly aggregation + matplotlib chart save karo",
            "summary numbers owner ko batao",
        ],
        "deliverable": "chart PNG + summary numbers",
    },
    {
        "request": "aaj ka mausam batao Kota ka",
        "workers": ["research"],
        "steps": ["weather tool se Kota ka current weather lo", "1 line me batao"],
        "deliverable": "short spoken answer",
    },
    {
        "request": "yaad rakhna mera client Ramesh ka number 98xxxxxx21 hai",
        "workers": ["memory"],
        "steps": ["Fact ko memory DB me key 'client_ramesh' se save karo", "Confirmation do"],
        "deliverable": "stored fact confirmation",
    },
    {
        "request": "notepad kholo aur isme meeting notes likh do",
        "workers": ["desktop"],
        "steps": ["save_note tool se meeting_notes naam se file likho", "Notepad me khol do", "Path batao"],
        "deliverable": "opened notepad with saved note",
    },
    {
        "request": "ye script likho jo har roz 9 baje mujhe weather ka email bheje",
        "workers": ["code"],
        "steps": [
            "Python script likho: weather API + smtp send",
            "Script ko run karke verify karo (dry-run)",
            "Cron/Task-Scheduler line owner ko batao",
        ],
        "deliverable": "working script + scheduler command",
    },
    {
        "request": "tum kaun ho",
        "workers": ["answer"],
        "steps": ["No tool needed — identity batao"],
        "deliverable": "identity answer",
    },
]


def save_trainset(examples: list[dict[str, Any]] | None = None) -> Path:
    OPTIMIZED_DIR.mkdir(parents=True, exist_ok=True)
    TRAINSET_PATH.write_text(
        json.dumps(examples or DEFAULT_TRAINSET, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return TRAINSET_PATH


def build_examples() -> list:
    import dspy

    data = DEFAULT_TRAINSET
    if TRAINSET_PATH.exists():
        try:
            data = json.loads(TRAINSET_PATH.read_text(encoding="utf-8")) or data
        except Exception:
            pass

    examples = []
    for row in data:
        ex = dspy.Example(
            request=row["request"],
            history="",
            workers=row.get("workers", ["answer"]),
            reason=row.get("reason", "training example"),
            needs_clarification=False,
            steps=row.get("steps", []),
            deliverable=row.get("deliverable", ""),
            context="",
        )
        # router training
        examples.append(ex.with_inputs("request", "history"))
    return examples


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------
def router_metric(example: Any, pred: Any, trace=None) -> float:
    """Routing theek hui? Partial credit deta hai."""
    gold = set(example.workers or [])
    got_raw = getattr(pred, "workers", []) or []
    if isinstance(got_raw, str):
        got_raw = [w.strip() for w in got_raw.replace(",", " ").split()]
    got = set(str(w).lower() for w in got_raw)
    if not gold or not got:
        return 0.0
    if got == gold:
        return 1.0
    overlap = len(got & gold)
    if overlap == 0:
        return 0.0
    precision = overlap / len(got)
    recall = overlap / len(gold)
    return 0.6 * recall + 0.4 * precision


def planner_metric(example: Any, pred: Any, trace=None) -> float:
    """Plan me concrete steps hain? Deliverable defined hai?"""
    steps = getattr(pred, "steps", []) or []
    if isinstance(steps, str):
        steps = [s for s in steps.split("\n") if s.strip()]
    score = 0.0
    if 3 <= len(steps) <= 8:
        score += 0.5
    elif steps:
        score += 0.25
    verbish = sum(1 for s in steps if any(v in str(s).lower() for v in
                  ("run", "open", "extract", "save", "write", "check", "fetch", "normalize", "call", "karo", "laao")))
    score += 0.3 * (verbish / max(1, len(steps)))
    if getattr(pred, "deliverable", ""):
        score += 0.2
    return min(1.0, score)


def gepa_feedback_metric(gold: Any, pred: Any, trace=None, pred_name=None, pred_trace=None):
    """GEPA ko feedback text chahiye hota hai (kyun fail hua) — yahi learning signal hai."""
    ok = router_metric(gold, pred, trace)
    reason = (
        "Routing correct." if ok >= 0.99 else
        f"Expected workers {gold.workers} but got {getattr(pred, 'workers', None)}. "
        "Browser tasks need 'browser'; note/file saving needs 'desktop'; data crunching needs 'code'."
    )
    return {"score": ok, "feedback": reason}


# ---------------------------------------------------------------------------
# compilers
# ---------------------------------------------------------------------------
def compile_router(mode: str = "bootstrap", auto: str = "light") -> Any:
    import dspy

    program = dspy.Predict("request, history -> workers, reason, needs_clarification")
    trainset = build_examples()
    if not trainset:
        raise RuntimeError("trainset khali hai")

    if mode == "gepa":
        optimizer = dspy.GEPA(metric=gepa_feedback_metric, auto=auto)
        return optimizer.compile(program, trainset=trainset, valset=trainset[: max(1, len(trainset) // 3)])
    if mode == "mipro":
        optimizer = dspy.MIPROv2(metric=router_metric, auto="light")
        return optimizer.compile(program, trainset=trainset, requires_permission_to_run=False)
    optimizer = dspy.BootstrapFewShot(metric=router_metric, max_labeled_demos=6, max_rounds=2)
    return optimizer.compile(program, trainset=trainset)


def compile_planner(mode: str = "bootstrap") -> Any:
    import dspy

    program = dspy.ChainOfThought("request, workers, context -> steps, deliverable")
    trainset = build_examples()
    if mode == "gepa":
        def _g(gold, pred, trace=None, pred_name=None, pred_trace=None):
            score = planner_metric(gold, pred)
            return {"score": score, "feedback": "Make steps concrete and executable; 3-7 steps; include verification step."}

        optimizer = dspy.GEPA(metric=_g, auto="light")
        return optimizer.compile(program, trainset=trainset, valset=trainset[:2])
    optimizer = dspy.BootstrapFewShot(metric=planner_metric, max_labeled_demos=5, max_rounds=2)
    return optimizer.compile(program, trainset=trainset)


# ---------------------------------------------------------------------------
# save / load
# ---------------------------------------------------------------------------
def save_compiled(program: Any, name: str) -> Path:
    OPTIMIZED_DIR.mkdir(parents=True, exist_ok=True)
    path = OPTIMIZED_DIR / f"{name}.json"
    program.save(str(path))
    meta = {
        "name": name,
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "provider": PROVIDER,
    }
    (OPTIMIZED_DIR / f"{name}.meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return path


def load_compiled(name: str) -> Any | None:
    """optimized/<name>.json mila to load karo, warna None."""
    path = OPTIMIZED_DIR / f"{name}.json"
    if not path.exists():
        return None
    try:
        import dspy

        signature_map = {
            "router": "request, history -> workers, reason, needs_clarification",
            "planner": "request, workers, context -> steps, deliverable",
            "code_smith": "task, language, feedback, previous -> code",
            "extract": "source_text, source_url, field_names -> data",
            "final_answer": "request, results, critique -> answer",
        }
        sig = signature_map.get(name)
        if not sig:
            return None
        program = dspy.Predict(sig)
        program.load(str(path))
        return program
    except Exception:
        return None


def report() -> str:
    files = sorted(OPTIMIZED_DIR.glob("*.json"))
    if not files:
        return (
            "📉 Abhi tak koi compiled program nahi hai.\n"
            "Chalao: `python scripts/optimize_dspy.py --mode bootstrap`"
        )
    lines = ["🧬 DSPy compiled programs:"]
    for f in files:
        meta_path = f.with_suffix(".meta.json")
        meta = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        size = f.stat().st_size / 1024
        lines.append(f"• {f.name:<28} {size:6.1f} KB  {meta.get('saved_at', '')}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="DSPy optimization for NikhilAI Jarvis")
    ap.add_argument("--mode", choices=["bootstrap", "gepa", "mipro"], default="bootstrap")
    ap.add_argument("--what", choices=["router", "planner", "both"], default="both")
    ap.add_argument("--auto", choices=["light", "medium", "heavy"], default="light")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    if args.report:
        print(report())
        return 0

    if PROVIDER == "demo" or OFFLINE_MODE:
        print("⚠️  Offline/demo mode: DSPy optimizer ko real LM chahiye (.env me key daalo).")
        print("   Trainset save kar diya:", save_trainset())
        return 1

    from ..llm import init_dspy

    init_dspy("fast")
    save_trainset()

    if args.what in ("router", "both"):
        print(f"🧬 compiling router ({args.mode}) …")
        prog = compile_router(args.mode, auto=args.auto)
        print("✅ saved:", save_compiled(prog, "router"))
    if args.what in ("planner", "both"):
        print(f"🧬 compiling planner ({args.mode}) …")
        prog = compile_planner(args.mode)
        print("✅ saved:", save_compiled(prog, "planner"))
    print("\n" + report())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
