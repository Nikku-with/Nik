"""
jarvis/tools/code_engine.py — SELF-WRITING + SELF-HEALING CODE ENGINE
====================================================================

Owner ki demand: "ai khudse codes likh ke run kare tasks ke liye".

Ye module wahi karta hai — 5 stage pipeline:

    ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌────────────┐   ┌──────────┐
    │ 1. DRAFT │ → │ 2. CHECK │ → │ 3. RUN   │ → │ 4. HEAL    │ → │ 5. REPORT│
    │ LLM code │   │ AST +    │   │ subproc  │   │ traceback  │   │ save +   │
    │ likhta   │   │ safety   │   │ +output  │   │ wapas LLM  │   │ summary  │
    └──────────┘   └──────────┘   └──────────┘   └────────────┘   └──────────┘

* Offline mode me rule-based codegen (templates) chalta hai — taaki bina API key bhi demo ho.
* Har script workspace/scripts/ me save hoti hai (reusable automation ban jaati hai).
* Dangerous code (rm -rf /, shutil.rmtree("/"), credential exfiltration) static check me block.
"""
from __future__ import annotations

import ast
import asyncio
import json
import re
import shlex
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool

from ..config import (
    ARTIFACTS_DIR,
    CODE_MAX_HEAL_ATTEMPTS,
    CODE_TIMEOUT_S,
    PROVIDER,
    SCRIPTS_DIR,
    is_offline,
    python_executable,
)
from ..prompts import coder_system_prompt
from .improvement import propose_self_improvement

# ---------------------------------------------------------------------------
# safety
# ---------------------------------------------------------------------------
_BANNED_PATTERNS = (
    "rm -rf /", "shutil.rmtree('/')", 'shutil.rmtree(\"/\")', "os.system('rm -rf /",
    "mkfs", "format c:", ":(){:|:&};:", "os.remove('/etc", "open('/dev/sda",
    "subprocess.call(['rm', '-rf', '/'])", "base64.b64decode", "socket.socket(socket.AF_INET",
    "requests.post('http://evil", "os.environ['AWS_SECRET",
)
_LANGUAGE_ALIASES = {
    "py": "python", "python": "python", "python3": "python",
    "js": "javascript", "javascript": "javascript", "node": "javascript", "nodejs": "javascript",
}
_LANGUAGE_EXTENSIONS = {"python": ".py", "javascript": ".js"}


def _normalize_language(language: str | None) -> str:
    return _LANGUAGE_ALIASES.get(str(language or "python").strip().lower(), "")


def _script_language(path: Path, language: str | None = None) -> str:
    if language:
        return _normalize_language(language)
    return {".py": "python", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript"}.get(
        path.suffix.lower(), ""
    )


def _script_name(save_as: str, task: str, extension: str) -> str:
    raw_name = str(save_as or slugify(task)).strip().replace("\\", "/").rsplit("/", 1)[-1]
    if raw_name.lower().endswith(extension.lower()):
        raw_name = raw_name[:-len(extension)]
    return raw_name.strip(" .") or "script"


def _javascript_runner() -> str | None:
    return shutil.which("node")


def static_check(code: str, language: str = "python") -> tuple[bool, str]:
    """Returns (ok, message). Python ho to AST parse + banned pattern scan."""
    if not code.strip():
        return False, "Code khali hai."
    low = code.lower()
    for bad in _BANNED_PATTERNS:
        if bad.lower() in low:
            return False, f"Dangerous pattern mile: `{bad}` — blocked by policy."
    if _normalize_language(language) == "python":
        try:
            ast.parse(code)
        except SyntaxError as exc:
            return False, f"SyntaxError: {exc.msg} (line {exc.lineno})"
        # network exfil heuristics
        if re.search(r"requests\.(post|put)\(", code) and re.search(r"token|password|api_key|secret", low):
            return False, "Code credentials POST kar raha hai — blocked (exfiltration risk)."
    return True, "ok"


def extract_code(text: str, language: str = "python") -> str:
    """LLM output me se code block nikaalo (```python ... ``` ya poora body)."""
    if not text:
        return ""
    fences = re.findall(r"```(?:[a-zA-Z0-9_+-]*)\n(.*?)```", text, re.S)
    if fences:
        return max(fences, key=len).strip()
    if text.strip().startswith(("import ", "from ", "#!", "def ", "print(", "class ")):
        return text.strip()
    return text.strip()


def slugify(task: str, maxlen: int = 40) -> str:
    s = re.sub(r"[^\w\s-]", "", (task or "script").lower())
    s = re.sub(r"[\s_-]+", "_", s).strip("_")
    return (s[:maxlen] or "script") + "_" + time.strftime("%H%M%S")


# ---------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------
@dataclass
class RunResult:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    returncode: int | None = None
    duration_s: float = 0.0
    timed_out: bool = False
    script_path: str = ""
    artifacts: list[str] = field(default_factory=list)

    def render(self, max_len: int = 3500) -> str:
        head = f"[exit {self.returncode} | {self.duration_s:.1f}s]" + (" ⏱️ TIMEOUT" if self.timed_out else "")
        out = (self.stdout or "").strip()
        err = (self.stderr or "").strip()
        body = f"{head}\n{out[-max_len:]}"
        if err:
            body += f"\n--- stderr ---\n{err[-1500:]}"
        if self.artifacts:
            body += "\n--- files created ---\n" + "\n".join(self.artifacts[:10])
        return body


async def run_script(
    path: Path,
    args: list[str] | None = None,
    timeout: int = CODE_TIMEOUT_S,
    cwd: Path | None = None,
    language: str | None = None,
) -> RunResult:
    """Requested language ke runtime me script chalao, permission aur Gate ke saath."""
    from ..security.permissions import check as permission_check

    decision = permission_check("run_script")
    if not decision.allowed:
        return RunResult(ok=False, stderr=f"Permission blocked: {decision.reason}", script_path=str(path))
    normalized_language = _script_language(path, language)
    if not normalized_language:
        return RunResult(ok=False, stderr=f"Unsupported script language: {language or path.suffix}", script_path=str(path))
    if normalized_language == "python":
        interpreter = python_executable()
    else:
        interpreter = _javascript_runner()
        if not interpreter:
            return RunResult(ok=False, stderr="Node.js nahi mila; JavaScript script run nahi hui.", script_path=str(path))
    cmd = [interpreter, str(path)] + list(args or [])
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return RunResult(ok=False, stderr=f"Script read failed: {exc}", script_path=str(path))
    from ..security.gate import authorize_command
    from ..security.permissions import current_surface

    approval = await authorize_command(
        shlex.join(cmd), str(cwd or ARTIFACTS_DIR),
        inspection_text=source,
        inspection_language=normalized_language,
        surface=current_surface(),
    )
    if not approval.allowed:
        return RunResult(ok=False, stderr=f"Execution cancelled by Gate: {approval.reason}", script_path=str(path))
    started = time.time()
    before = {p for p in ARTIFACTS_DIR.rglob("*") if p.is_file()}
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd or ARTIFACTS_DIR),
            env={**_clean_env(), "JARVIS_SCRIPT": "1"},
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            timed_out = False
        except asyncio.TimeoutError:
            proc.kill()
            out, err = b"", f"Timeout: {timeout}s ke andar complete nahi hua".encode()
            timed_out = True
        after = {p for p in ARTIFACTS_DIR.rglob("*") if p.is_file()}
        new_files = sorted(str(p) for p in (after - before))[:12]
        return RunResult(
            ok=(not timed_out and proc.returncode == 0),
            stdout=(out or b"").decode(errors="ignore"),
            stderr=(err or b"").decode(errors="ignore"),
            returncode=proc.returncode,
            duration_s=time.time() - started,
            timed_out=timed_out,
            script_path=str(path),
            artifacts=new_files,
        )
    except Exception as exc:
        return RunResult(ok=False, stderr=str(exc), duration_s=time.time() - started, script_path=str(path))


def _clean_env() -> dict:
    import os
    return {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}


# ---------------------------------------------------------------------------
# code generation
# ---------------------------------------------------------------------------
async def generate_code(task: str, language: str = "python", feedback: str = "", previous: str = "") -> str:
    """LLM se code likhwao; language-specific offline template sirf Python ke liye hai."""
    normalized_language = _normalize_language(language)
    if not normalized_language:
        raise ValueError(f"Unsupported language: {language}")
    language = normalized_language
    system = coder_system_prompt() + (
        f"\nThe requested language is {language}; use that language exactly and never return Python when another language was requested."
    )

    user = (
        f"TASK: {task}\n\n"
        f"Language: {language}\n"
        f"Artifacts folder (output yahin likho): {ARTIFACTS_DIR}\n"
        f"Scripts folder: {SCRIPTS_DIR}\n"
    )
    if previous and feedback:
        user += (
            f"\nPEHLA ATTEMPT (fail hua):\n```{language}\n{previous[:4000]}\n```\n"
            f"\nERROR:\n{feedback[:2000]}\n"
            "\nAb root-cause fix karke POORA corrected code do. Sirf ek code block, koi explanation nahi.\n"
        )
    else:
        user += f"\nSirf ek complete, runnable `{language}` code block do. Koi explanation nahi.\n"

    # 1) DSPy route (fast + optimizable)
    if not is_offline() and PROVIDER != "demo":
        try:
            from ..dspy_engine.modules import get_module
            smith = get_module("code_smith")
            if smith is not None:
                pred = await asyncio.to_thread(smith, task=task, language=language, feedback=feedback or "none", previous=previous or "none")
                code = extract_code(getattr(pred, "code", "") or "", language)
                if code:
                    return code
        except Exception:
            pass

    # 2) plain LLM
    if not is_offline() and PROVIDER != "demo":
        try:
            from ..llm import get_chat_model
            model = get_chat_model("code")
            resp = await model.ainvoke([SystemMessage(content=system), HumanMessage(content=user)])
            code = extract_code(getattr(resp, "content", "") or "", language)
            if code:
                return code
        except Exception as exc:
            feedback = f"{feedback}\n(llm error: {exc})".strip()

    # Offline templates are Python; never mislabel generated Python as another language.
    if language != "python":
        raise RuntimeError(f"Offline mode me {language} code generation available nahi hai; Python supported hai.")
    return _offline_codegen(task, language)


def _offline_codegen(task: str, language: str = "python") -> str:
    """OFFLINE MODE: common Python task families ke liye ready templates."""
    if _normalize_language(language) != "python":
        raise ValueError(f"Offline templates only support Python, not {language}.")
    from ..config import ARTIFACTS_DIR as ART
    low = (task or "").lower()
    ts = time.strftime("%Y%m%d_%H%M%S")

    if any(k in low for k in ("note", "save", "likh", "file banao")):
        return f'''"""Offline-template: note/file writer"""\nfrom pathlib import Path\nimport time\n\nOUT = Path(r"{ART}") / "offline_note_{ts}.txt"\ncontent = {json.dumps(task)}\nOUT.write_text(f"Task: {{content}}\\nGenerated by Jarvis offline code engine at {{time.ctime()}}\\n", encoding="utf-8")\nprint(f"[OK] file banaya: {{OUT}}")\nprint(f"[SIZE] {{OUT.stat().st_size}} bytes")\n'''

    if any(k in low for k in ("scrape", "website", "http", "web se", "site")):
        urls = re.findall(r"https?://[^\s,]+", task) or ["https://example.com"]
        return f'''"""Offline-template: requests + BeautifulSoup scraper"""\nimport json, re\nfrom pathlib import Path\nimport requests\nfrom bs4 import BeautifulSoup\n\nURLS = {json.dumps(urls)}\nOUT = Path(r"{ART}") / "scrape_{ts}.json"\nHEADERS = {{"User-Agent": "Mozilla/5.0 (compatible; NikhilAI-Jarvis)"}}\n\ndef clean(t):\n    return re.sub(r"\\s+", " ", t or "").strip()\n\nreport = {{}}\nfor u in URLS:\n    try:\n        r = requests.get(u, headers=HEADERS, timeout=20)\n        soup = BeautifulSoup(r.text, "html.parser")\n        title = clean(soup.title.string if soup.title else "")\n        text = clean(" ".join(p.get_text() for p in soup.find_all(["h1","h2","p"])[:40]))\n        report[u] = {{"status": r.status_code, "title": title, "text_preview": text[:1200]}}\n        print(f"[OK] {{u}} -> {{r.status_code}} | {{title[:60]}}")\n    except Exception as e:\n        report[u] = {{"error": str(e)}}\n        print(f"[FAIL] {{u}} -> {{e}}")\n\nOUT.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")\nprint(f"[SAVED] {{OUT}}")\n'''

    if any(k in low for k in ("csv", "excel", "report", "data", "table")):
        return f'''"""Offline-template: CSV report builder"""\nimport csv, time\nfrom pathlib import Path\n\nOUT = Path(r"{ART}") / "report_{ts}.csv"\nrows = [\n    ["task", "status", "generated_at"],\n    [{json.dumps(task[:120])}, "done", time.ctime()],\n]\nwith OUT.open("w", newline="", encoding="utf-8") as f:\n    csv.writer(f).writerows(rows)\nprint(f"[OK] report banaya: {{OUT}}")\nprint(OUT.read_text(encoding="utf-8"))\n'''

    if any(k in low for k in ("calculate", "calc", "analyse", "analyze", "count", "sum", "number")):
        return f'''"""Offline-template: quick numeric analysis"""\nimport statistics\n\ntask = {json.dumps(task)}\nnumbers = [12, 45, 7, 23, 89, 34, 56]\nprint("[INPUT] " + task)\nprint(f"[DATA] {{numbers}}")\nprint(f"[SUM] {{sum(numbers)}}")\nprint(f"[MEAN] {{statistics.mean(numbers):.2f}}")\nprint(f"[MEDIAN] {{statistics.median(numbers)}}")\nprint(f"[MAX] {{max(numbers)}} | [MIN] {{min(numbers)}} | [STDEV] {{statistics.pstdev(numbers):.2f}}")\n'''

    return f'''"""Offline-template: task plan executor\n\nAsli LLM (GOOGLE_API_KEY) aane par yahi file dynamic code ban jaayegi.\n"""\nimport json, time\nfrom pathlib import Path\n\nTASK = {json.dumps(task)}\nOUT = Path(r"{ART}") / "task_report_{ts}.json"\n\nsteps = [\n    "1. Task samjho aur inputs identify karo",\n    "2. Data collect karo (web/browser/file)",\n    "3. Process + validate karo",\n    "4. Output save karo aur owner ko report do",\n]\nreport = {{"task": TASK, "steps": steps, "engine": "offline-template", "generated_at": time.ctime()}}\nOUT.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")\nprint("[PLAN] " + TASK)\nfor s in steps:\n    print("  " + s)\nprint(f"[SAVED] {{OUT}}")\n'''


# ---------------------------------------------------------------------------
# THE ENGINE
# ---------------------------------------------------------------------------
async def code_engine_run(
    task: str,
    language: str = "python",
    run: bool = True,
    save_as: str = "",
    max_attempts: int | None = None,
    args: list[str] | None = None,
) -> dict:
    """
    Full pipeline: draft → check → run → heal → report.
    Returns dict: {ok, script, attempts, report, artifacts, stdout, code}
    """
    from ..security.permissions import check as permission_check

    decision = permission_check("code_engine")
    if not decision.allowed:
        message = f"Permission blocked: {decision.reason}"
        return {"ok": False, "script": "", "attempts": 0, "report": message,
                "artifacts": [], "stdout": "", "code": "", "history": []}
    normalized_language = _normalize_language(language)
    if not normalized_language:
        message = f"❌ Unsupported language '{language}'. Abhi Python aur JavaScript supported hain."
        return {"ok": False, "script": "", "attempts": 0, "report": message,
                "artifacts": [], "stdout": "", "code": "", "history": []}
    extension = _LANGUAGE_EXTENSIONS[normalized_language]
    attempts_limit = max_attempts or CODE_MAX_HEAL_ATTEMPTS
    try:
        code = await generate_code(task, normalized_language)
    except Exception as exc:
        message = f"❌ Code generate nahi hua: {exc}"
        return {"ok": False, "script": "", "attempts": 0, "report": message,
                "artifacts": [], "stdout": "", "code": "", "history": []}
    feedback = ""
    history: list[dict] = []
    script_path = SCRIPTS_DIR / f"{_script_name(save_as, task, extension)}{extension}"
    result = RunResult(ok=False)

    for attempt in range(1, attempts_limit + 1):
        ok_static, msg = static_check(code, language)
        if not ok_static:
            feedback = f"static check failed: {msg}"
            history.append({"attempt": attempt, "stage": "static", "error": msg})
            try:
                code = await generate_code(task, normalized_language, feedback=feedback, previous=code)
            except Exception as exc:
                result = RunResult(
                    ok=False, stderr=f"Code generation failed after static-check error: {exc}",
                    script_path=str(script_path),
                )
                break
            continue

        if script_path.is_file() and script_path.stat().st_size > 0:
            try:
                prior_code = script_path.read_text(encoding="utf-8")
            except OSError:
                prior_code = ""
            if prior_code != code:
                import hashlib

                from ..security.gate import authorize_command
                from ..security.permissions import current_surface

                digest = hashlib.sha256(code.encode("utf-8")).hexdigest()[:12]
                approval = await authorize_command(
                    f"overwrite generated script {script_path} with sha256:{digest}",
                    str(script_path.parent), force=True, surface=current_surface(),
                    reason="generated code would overwrite an existing non-empty script",
                )
                if not approval.allowed:
                    return {
                        "ok": False, "script": str(script_path), "attempts": attempt - 1,
                        "report": f"🚫 Script overwrite nahi ki: {approval.reason}", "artifacts": [],
                        "stdout": "", "code": code, "history": history,
                    }
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text(code, encoding="utf-8")
        try:
            from .file_tools import invalidate_index

            invalidate_index()
        except Exception:
            pass

        if not run:
            return {
                "ok": True, "script": str(script_path), "attempts": attempt,
                "report": f"✅ Code likh diya (run=False): {script_path}", "artifacts": [],
                "stdout": "", "code": code,
            }

        result = await run_script(script_path, args=args, language=normalized_language)
        history.append({"attempt": attempt, "stage": "run", "returncode": result.returncode,
                        "duration_s": round(result.duration_s, 2), "stderr": result.stderr[-500:]})
        if result.ok:
            break

        # ---- HEAL: traceback + code wapas LLM ko
        feedback = (result.stderr or result.stdout or "unknown failure")[-2500:]
        try:
            code = await generate_code(task, normalized_language, feedback=feedback, previous=code)
        except Exception as exc:
            result = RunResult(
                ok=False,
                stderr=f"Code generation failed after execution error: {exc}",
                script_path=str(script_path),
            )
            break

    report = (
        f"✅ Task complete — {len(history)} attempt(s)\n"
        f"📜 Script: {script_path}\n"
        f"{result.render()}"
        if result.ok
        else f"⚠️ {attempts_limit} attempts ke baad bhi script fail hui.\n📜 Script: {script_path}\n{result.render()}"
    )
    return {
        "ok": result.ok,
        "script": str(script_path),
        "attempts": len(history),
        "report": report,
        "artifacts": result.artifacts,
        "stdout": result.stdout[-8000:],
        "code": code,
        "history": history,
    }


# ---------------------------------------------------------------------------
# TOOLS
# ---------------------------------------------------------------------------
@tool
async def code_engine(task: str, language: str = "python", run: bool = True, save_as: str = "") -> str:
    """Likhna + chalana + khud fix karna: AI code likhta hai, run karta hai, error aaya to traceback padh kar
    khud theek karta hai aur dobara chalata hai.

    Use jab owner bole:
      - "ek script likho jo ... kare"
      - "ye data process karke CSV bana do"
      - "automate karo: har website se price nikaalo"
      - "code likh ke chala do"
    task = kya karna hai (natural language), save_as = script ka naam.
    """
    outcome = await code_engine_run(task=task, language=language, run=run, save_as=save_as)
    return outcome["report"] + (f"\n\n📦 Artifacts: {len(outcome.get('artifacts', []))}" if outcome.get("artifacts") else "")


@tool
async def run_python(code: str, timeout: int = 60) -> str:
    """Run a small Python snippet right now (quick calculation / data check).

    Chhote one-off kaam ke liye — bade tasks ke liye `code_engine` use karo.
    """
    from ..security.permissions import check as permission_check

    decision = permission_check("run_python")
    if not decision.allowed:
        return f"🚫 Permission blocked: {decision.reason}"
    ok, msg = static_check(code, "python")
    if not ok:
        return f"🚫 Blocked: {msg}"
    tmp = SCRIPTS_DIR / f"snippet_{time.strftime('%H%M%S')}_{uuid.uuid4().hex}.py"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(code, encoding="utf-8")
    res = await run_script(tmp, timeout=timeout)
    return res.render()


@tool
async def run_script_file(path: str, script_args: list[str] | None = None) -> str:
    """Run an existing script file from the workspace (jo pehle banayi thi).

    NOTE: parameter ka naam `script_args` hai, `args` nahi — LangChain me `args` naam ka
    parameter `*args` (varargs) samajh liya jata tha, jisse tool ka schema `v__args` ban jata
    aur LLM ise kabhi successfully call nahi kar pata (TypeError). Ye bug isi wajah se fix hua.
    """
    from ..security.permissions import check as permission_check

    decision = permission_check("run_script_file")
    if not decision.allowed:
        return f"🚫 Permission blocked: {decision.reason}"
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = SCRIPTS_DIR / p
    if not p.exists():
        matches = list(SCRIPTS_DIR.glob(f"*{Path(path).stem}*"))
        if not matches:
            return f"❌ Script nahi mili: {path}"
        p = matches[0]
    res = await run_script(p, args=script_args)
    return f"▶️ {p.name}\n{res.render()}"


@tool
async def install_package(package: str) -> str:
    """Install a Python package with pip (owner-only destructive environment change)."""
    from ..security.permissions import check as permission_check

    decision = permission_check("install_package")
    if not decision.allowed:
        return f"🚫 Permission blocked: {decision.reason}"
    pkg = re.sub(r"[^A-Za-z0-9_.\-=<>\[\]/]", "", (package or "").strip())
    if not pkg:
        return "❌ Package naam khali hai."
    from ..security.gate import authorize_command
    from ..security.permissions import current_surface

    install_command = shlex.join([python_executable(), "-m", "pip", "install", "-q", pkg])
    approval = await authorize_command(install_command, str(SCRIPTS_DIR), surface=current_surface())
    if not approval.allowed:
        return f"🚫 Package install cancel hua: {approval.reason}"
    proc = await asyncio.create_subprocess_exec(
        python_executable(), "-m", "pip", "install", "-q", "--disable-pip-version-check", pkg,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=600)
    except asyncio.TimeoutError:
        proc.kill()
        return f"⏱️ pip install {pkg} timeout."
    return f"{'✅' if proc.returncode == 0 else '❌'} pip install {pkg}\n{(out or b'').decode(errors='ignore')[-1200:]}"


@tool
async def create_project(name: str, description: str = "", language: str = "python") -> str:
    """Scaffold a small project folder in the workspace with starter files.

    Use: "naya python project banao jo scraper ho", "flask app scaffold karo".
    """
    from ..security.permissions import check as permission_check

    decision = permission_check("create_project")
    if not decision.allowed:
        return f"🚫 Permission blocked: {decision.reason}"
    normalized_language = _normalize_language(language)
    if not normalized_language:
        return f"❌ Unsupported project language: {language}"
    slug = re.sub(r"[^\w-]", "_", (name or "project").strip().lower())[:40] or "project"
    root = ARTIFACTS_DIR / slug
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(parents=True, exist_ok=True)

    try:
        main_code = await generate_code(
            f"Create the main entry file for project '{name}'. Description: {description or 'n/a'}.",
            language=normalized_language,
        )
    except Exception as exc:
        return f"❌ Project code generate nahi hua: {exc}"
    extension = _LANGUAGE_EXTENSIONS[normalized_language]
    files = {
        root / "src" / f"main{extension}": main_code,
        root / "README.md": f"# {name}\n\n{description or 'Scaffolded by Jarvis (NikhilAI).'}\n",
        root / "requirements.txt": "# add dependencies here\n",
        root / "tests" / "test_basic.py": (
            "def test_smoke():\n    assert True\n" if normalized_language == "python" else "// add tests\n"
        ),
    }
    for path, content in files.items():
        path.write_text(content, encoding="utf-8")
    listing = "\n".join(f"  - {p.relative_to(root)}" for p in files)
    runner = "python" if normalized_language == "python" else "node"
    entry_file = root / "src" / f"main{extension}"
    return f"🏗️ Project ban gaya: {root}\n{listing}\n\n▶️ Run: {runner} {entry_file}"


CODE_TOOLS = [code_engine, run_python, run_script_file, install_package, create_project, propose_self_improvement]
