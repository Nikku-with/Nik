#!/usr/bin/env python3
"""
scripts/optimize_dspy.py — DSPy reasoning ko "train" karo (Jarvis khud ko better banata hai)
==========================================================================================

Jarvis ke DSPy modules (router / planner / extractor / answer) ke prompts
hand-written nahi hain — wo **optimize** hote hain real examples par:

  bootstrap : 1-2 min, sasta  → BootstrapFewShot (best few-shot demos chunta hai)
  gepa      : best quality    → reflective evolution (failures padh ke prompt rewrite)
  mipro     : strong baseline → instructions + demos jointly optimize

Output `optimized/*.json` me jaata hai, aur runtime par `jarvis/dspy_engine/modules.py`
usko automatically load kar leta hai (koi code change nahi).

Examples:
    python scripts/optimize_dspy.py --mode bootstrap
    python scripts/optimize_dspy.py --mode gepa --what both --auto medium
    python scripts/optimize_dspy.py --report

Apna data add karna ho: optimized/trainset.json me apne rows daalo
(= owner ke asli commands + unke sahi workers/steps), phir re-run karo.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(description="NikhilAI Jarvis — DSPy optimizer")
    ap.add_argument("--mode", choices=["bootstrap", "gepa", "mipro"], default="bootstrap")
    ap.add_argument("--what", choices=["router", "planner", "both"], default="both")
    ap.add_argument("--auto", choices=["light", "medium", "heavy"], default="light")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--save-trainset", action="store_true", help="default trainset file me likh do")
    args = ap.parse_args()

    from jarvis.config import PROVIDER, doctor
    from jarvis.dspy_engine import optimize

    if args.save_trainset:
        print("💾 trainset saved:", optimize.save_trainset())
        return 0

    if args.report:
        print(optimize.report())
        print("\n" + doctor().render())
        return 0

    if PROVIDER == "demo":
        print(doctor().render())
        print(
            "\n⚠️  DSPy optimization ke liye real LLM key chahiye (koi API key nahi mili).\n"
            "   .env me GOOGLE_API_KEY daalo, phir:\n"
            "     python scripts/optimize_dspy.py --mode bootstrap --what both\n"
        )
        print("💾 Filhaal default trainset save kar diya:", optimize.save_trainset())
        return 1

    from jarvis.llm import init_dspy

    init_dspy("fast")
    print(optimize.report())
    print(f"\n🧬 Compiling ({args.mode}) … ye 1-10 min le sakta hai.\n")

    if args.what in ("router", "both"):
        prog = optimize.compile_router(args.mode, auto=args.auto)
        print("✅ router  →", optimize.save_compiled(prog, "router"))
    if args.what in ("planner", "both"):
        prog = optimize.compile_planner(args.mode)
        print("✅ planner →", optimize.save_compiled(prog, "planner"))

    print("\n" + optimize.report())
    print("ℹ️  Runtime par apne aap load ho jaayenge (JARVIS_DSPY_OPTIMIZED=1).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
