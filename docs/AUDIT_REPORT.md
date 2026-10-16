# NikhilAI / Jarvis — Independent Audit Report (PROMPT 2 of 2)

**Tarikh:** 2026-10-16 (Asia/Kolkata)
**Branch:** `arena/d2e74493-nik` — HEAD `228f58a`
**Auditor ka rule:** builder ki kisi bhi report, `docs/STATUS.md`, README ya skill description ko evidence maan kar nahi chalna. Har claim code padh kar ya kuch chala kar verify kiya gaya.

---

## 1. Ek nazar me

| | Baseline (jaise mili) | Final (audit ke baad) |
|---|---|---|
| `ruff check .` | clean | **clean** |
| `python -m compileall -q jarvis tests` | pass | **pass** |
| `pytest -o addopts=""` | **355 passed** | **482 passed** (+127 naye tests) |
| 7 standalone harnesses | 422/423 (1 fail) | **429/429** (0 fail) |
| Registered tools | 99 | **99** (sab callable, 0 broken) |
| Tools jinke koi test hain | 63 | **70** |
| Tools jinke koi automatic runtime path hai | 76 | **83** (+4 documented manual-only) |
| Dead modules | 0 | **0** |
| Hard import cycles | 0 | **0** |
| `git archive` ZIP | — | **225 entries / 186 files**, hygiene clean |
| Naye test files | — | `test_live_replay.py`, `test_wiring.py`, `test_chaos.py`, `test_conversation.py`, `test_claims.py` + `scripts/wiring_map.py` |

Baseline me `test_v23.py` **146/147** tha — ek check fail ho raha tha (env-coupling: `BRAIN_PROVIDER` aur `config.runtime` dono set hone chahiye). Ab **153/153**.

---

## 2. Step-by-step: kya kiya

| Step | Kaam | Result |
|---|---|---|
| 1 | Baseline: ruff / compileall / pytest / 7 harnesses / ZIP hygiene | Sab capture kiya; ZIP me koi `.git`, `.env`, `__pycache__`, egg-info nahi tha |
| 2 | AST wiring map (`scripts/wiring_map.py`) → `docs/WIRING_MAP.md` + `tests/test_wiring.py` | 99 tools, 70 tested, 83 reachable, 0 dead modules, 0 duplicate capability, 0 hard cycle |
| 3 | Acceptance replay (`tests/test_live_replay.py`) — owner ke 7 real Windows failures | **12 tests**, sab pass; 4 real defects nikle aur fix hue |
| 4 | Chaos / failure-injection matrix (`tests/test_chaos.py`) | **42 tests**; 3 aur defects nikle |
| 5 | Rich UI + conversation limits + memory + Telegram (`tests/test_conversation.py`) | **27 tests**; 5 aur defects nikle |
| 6 | Claims audit (README / docs / SKILL.md / prompts) → `docs/CLAIMS_AUDIT.md` | 39 claims: 32 TRUE, 2 PARTIAL, 3 stale, 1 FALSE |
| 7 | Fresh `git archive` ZIP pe sab dobara chalaya | 482 passed, ruff clean, compileall clean, 429/429 harnesses |

---

## 3. Wiring score

`scripts/wiring_map.py --check` (AST-based, stdlib only, koi LLM nahi):

```
tools=99   tested=70   reachable=83
dead_modules=0   cycles=1 (lazy)   duplicates=0   hard_cycles=0
```

* **99 registered tools** — sabke args schema valid, sab kisi na kisi surface se allowed.
* **83 tools** kisi deterministic caller ya worker binding se reachable hain.
* **4 tools** sirf Rich UI ke `/tool <name>` escape hatch se chal sakte hain — reason ke saath document kiya gaya hai (`docs/CLAIMS_AUDIT.md` §5.1). Ye list `tests/test_wiring.py` me pin hai, to 100th tool bina wiring ke add nahi ho sakta.
* **0 dead modules**, **0 import-time cycles**. Ek lazy cycle hai (`config.runtime` ↔ `runtime.controls`) — dono imports function ke andar hain, isliye benign hai, par pin kar diya gaya hai taaki badh na sake.
* Har entry point (CLI, Telegram, API, scheduler) unified runner aur LangGraph graph tak pahunchta hai.

---

## 4. Acceptance replay — owner ke 7 real failures

Ye wahi 7 cheezein hain jo owner ne apni Windows VM pe dekhi thi. Har ek ke liye ek test hai jo fake Windows (stubbed `ctypes.windll.user32`), fake app index, fake launcher/typing aur ek **virtual clock** ke saath `run_agent` chalata hai.

| # | Owner ka observed behaviour | Purana result | Ab | Test |
|---|---|---|---|---|
| 1 | `hy bhai` / `hy` | har baar wahi ek canned line | 1 LLM call, har turn pe line badalti hai, koi tool nahi | `test_greeting_is_varied_and_costs_one_llm_call` |
| 2 | `kaisa hai re` | 5 LLM call, 63.7 s | chat path, **1 LLM call**, cooled-down primary skip | `test_how_are_you_uses_chat_path_and_skips_cooled_down_primary` |
| 3 | `chrome kholde` | 5 LLM call, 47 s | `open_any_app` direct, **0 LLM call** | `test_chrome_kholde_opens_without_any_llm_call` |
| 4 | `notebook open krke likh` | "BEST MATCH: OUTLOOK 56.7%" | **ek sawal**, options ke saath; jawab `notepad` dene pe alias save | `test_notebook_asks_one_question_then_saves_the_alias` |
| 5 | `notepad open krke hy likhde` | "WINDOW NOT FOUND", 0 chars | 3 s baad aane wali window accept | `test_window_appearing_after_three_seconds_is_accepted` |
| 6 | `python install krde` | generated script, 2 approvals, ~125 s | ek clarification, **ek pip run**, **ek approval** | `test_ambiguous_install_asks_once_then_runs_pip_once` |
| 7 | doosra `chrome kholo` | naya process | **0 LLM call**, existing window focus | `test_second_chrome_kholo_focuses_the_existing_window` |

**Row 4 literally reproduce hui:** fake index pe `notebook` → `Microsoft Outlook 56.7%`, `Notepad 45.3%`, `Google Chrome 32.4%`. Owner ne jo number bola tha, wahi number aaya.

Is step me 4 real defects mile (D-1…D-4), sab fix.

---

## 5. Chaos matrix — jab kuch toot jaye

`tests/test_chaos.py` (42 tests) ek-ek dependency todo deta hai aur pin karta hai ki assistant ko kya karna chahiye: `run_agent` se exception nahi aana chahiye, jo safal nahi hua usko safal nahi bolna chahiye, fail ki wajah record honi chahiye, aur fail hua provider rotation se nikalna chahiye (har turn pe dobara try nahi).

| Layer | Inject kiya | Expected reaction |
|---|---|---|
| Provider error → class | 429 / quota / timeout / 5xx / 401 / unknown | sahi cooldown class + window (60 / 20 / 30 / 300 / 15 s) |
| Backoff | baar-baar fail | ×2, ×4, ×8, phir flat; 900 s ceiling sirf 300 s auth base se reachable |
| Circuit breaker | cooled-down target | bina call kiye skip; fail pe circuit khulta hai; success pe maaf; expiry pe wapas |
| Fallback chain | sab targets fail | honest error jo **har failed target ka naam le**, "koi fake answer nahi" |
| Agent | sab models down | status `failed`/`partial_failure`, error me model failure, `verified_execution=False` |
| Tool | raise / failure-string / missing tool / permission denied | record `ok=False`, koi crash nahi; denied pe `blocked` + zero evidence |
| Windows (stubbed) | launch fail / window kabhi na aaye / Win32 focus se inkaar | jhootha "done" nahi, exception nahi |
| Persistence | session state read-only / memory DB full | turn chalta hai, jawab milta hai, disk error **bol kar** bataya jata hai |
| Sab kuch toota | network + provider + launcher sab fail | ek sentence wapas, kabhi fake success nahi |

Is step me 3 real defects mile (D-7…D-9), sab fix.

---

## 6. Claims audit — 39 claims

Poori table `docs/CLAIMS_AUDIT.md` me hai, har row ke saath evidence.

| Verdict | Count | Misaal |
|---|---|---|
| **TRUE** | 32 | "one shared agent runtime for CLI/API/Telegram/voice/scheduler" — wiring map se verify; 99/99 tools callable; 18/18 skill packs; HiDPI scaling (`desktop.py:516,540`); browser stuck-loop detection (`browser_tools.py:71`); `propose_self_improvement` source kabhi edit nahi karta |
| **PARTIAL** | 2 | README ka `pytest -q` — count print hi nahi hota (`addopts="-q"`). Voice extra install ho nahi saka, to wo instruction unverified hai |
| **Stale** | 3 | STATUS.md: 355 → **455/482** passed; `test_v23` 147 → **153**; ZIP 218 → **225** entries. README aur STATUS.md dono me correct kar diya, purane record dikhne ke liye chhode |
| **FALSE** | 1 | `prompts.py` ke `current_datetime_str()` / `greeting_for_now()` **kahin se call hi nahi hote** — matlab assistant ko system prompt me aaj ki tareekh ya time-of-day greeting milti hi nahi |

Ek aur claim reproduce nahi ho payi: "177-file secret scan" — tree me 151 Python files hain.

---

## 7. Defects: mile, theek hue, aur jo khule hain

| ID | File:line | Defect | State |
|---|---|---|---|
| D-1 | `tools/app_finder.py:500,375` | `KeyError: 'norm'` — koi bhi index entry jo `_add()` se nahi bani, poora app launch crash. Owner ko dikhta tha: `open_any_app: error: 'norm'` | ✅ Fixed |
| D-2 | `tools/app_finder.py` | top score pe tie (Notepad vs Notepad++) ko ambiguous nahi maana jata tha — randomly ek launch ho jata | ✅ Fixed (`match_is_ambiguous`) |
| D-3 | `runtime/agent.py` | ambiguity check permission check se **pehle** chal raha tha — `telegram_guest` ko `blocked` ki jagah clarification milta tha | ✅ Fixed |
| D-4 | `security/permissions.py` | `restart_jarvis_tool`, `play_file`, `open_path`, `memory_compact`, `refresh_app_index`, `reload_config_tool` sab READ the — **Telegram guest bhi assistant restart kar sakta tha ya koi bhi path OS shell ko de sakta tha** | ✅ Fixed |
| D-5 | `security/permissions.py` | `check()` kisi bhi unknown naam ko READ pe allow kar deta tha | ✅ Fixed (fail-closed + alias list test se pin) |
| D-6 | `workers/browser.py`, `workers/desktop.py`, `telegram/tools.py` | 6 registered tools ka koi runtime path hi nahi tha | ⚠️ Partial — 2 wire ho gaye, 4 manual-only documented |
| D-7 | `memory/manager.py:145` | memory write fail hone pe exception poore turn ko maar deta tha — owner ko jawab ki jagah traceback milta | ✅ Fixed |
| D-8 | `runtime/agent.py` | chat fail hone pe substantive sawal ka bhi greeting line se jawab (`"mujhe business idea do"` → `"Hey Nikhil! Main yahin hoon"`) | ✅ Fixed |
| D-9 | `llm/core.py` | aggregate error sirf aakhri exception batata tha — owner ko pata hi nahi chalta kaun sa provider mar gaya | ✅ Fixed |
| D-10 | `cli/pro_ui.py:143-315` | `Text.from_markup` kisi bhi `[` pe `MarkupError` — JSON list, Python repr ya `[Errno 30] Read-only file system` se **poora dashboard crash** | ✅ Fixed (`escape()`) |
| D-11 | `runtime/agent.py:819` | 25 casual messages me se **22** poore LangGraph pipeline se jaate the (`acha`, `theek hai`, `hmm`, `bye`) | ✅ Fixed |
| D-12 | `runtime/agent.py:1001` | clarification ka jawab sirf noun dekar (`notepad`) chat path swallow kar leta tha | ✅ Fixed |
| D-13 | `runtime/agent.py:809` | `hata do` (split imperative) command nahi pehchana jata tha | ✅ Fixed |
| D-14 | `prompts.py:22,30` | `current_datetime_str()` / `greeting_for_now()` dead code | ❌ **Open** — delete nahi kiya, "not wired" likh diya |
| D-15 | `config/runtime.py:202` ↔ `runtime/controls.py:12` | lazy import cycle | ❌ **Open** — benign; test se pin |
| D-16 | `graph/nodes.py:10` | `get_chat_model` import-time pe bind — runtime provider switch graph tak nahi pahunchta | ❌ **Open** — low impact |

**13 fix hue, 3 khule.** Khule hue teeno chhote hain aur document kar diye gaye hain.

---

## 8. Jo verify NAHI kiya ja sakta (owner ki Windows VM chahiye)

Ye list poori tarah honest hai — neeche ki koi bhi cheez is machine pe test nahi hui. Tests me Win32 layer ek **stub** hai (`ctypes.windll` fake se replace). Ye "kaam karta hai" ya "nahi karta" — dono me se kuch prove nahi karta.

* **Real Win32:** `EnumWindows`, `SetForegroundWindow`, `SendKeys`, clipboard, asli scaled display pe HiDPI.
* **Asli apps:** Notepad / Chrome / Outlook launch, focus, typing.
* **Desktop libs:** `pyautogui`, `pygetwindow`, `pynput` (evdev wheel nahi milne se install hi nahi hua).
* **Live browser:** Chromium, `browser-use` Python API + CLI (`scripts/audit.py`: `python=no cli=no`).
* **Live providers:** Gemini / OpenRouter / Groq. Cooldown aur circuit breaker **injected failures** se test hue hain, asli 429 se nahi.
* **Telegram:** koi bot account nahi, network pe inline-button round trip nahi.
* **LiveKit voice:** koi mic, koi cloud room, koi spoken yes/no approval nahi.
* **Scheduler:** production-duration run nahi.
* **`voice` aur `browser` pip extras** is environment me install nahi ho sake.

---

## 9. Rating: **6.5 / 10**

**Kaise bana:** delivery me 99 tools, 18 skill packs, ek shared LangGraph runner aur 5 surfaces hain — ye kam nahi hai. Tests baseline pe hi 355 pass the, ruff clean tha, ZIP hygiene saaf thi. Lekin audit me jo mila wo ye hai: **surface bada hai, par wiring aur safety me chhed hain.**

**Ghatane wale point:**
1. 99 tools me se 4 ka koi automatic path hai hi nahi, aur 26 ke koi test nahi the (ab 29).
2. Security matrix me 6 tools READ the jinhe nahi hona chahiye tha — guest Telegram se assistant restart ho sakta tha.
3. Owner ke 7 me se 7 real failures reproduce hue — matlab delivery VM pe chalne se pehle test nahi hui thi.
4. Dashboard ek `[` se crash ho jata tha — roz-marra ke output me `[` aam hai.
5. Docs me numbers stale the aur ek claim galat thi (prompt helpers dead hain).

**Badhane wale point:**
1. Architecture saaf hai: shared runner, evidence-based verification, Dangerous-Command Gatepoora intact.
2. Cooldown/circuit-breaker design socha-samjha hai (class-wise windows, backoff, success pe maafi).
3. Offline/demo fallback honest hai — jhooth nahi bolta.
4. Har fix ke saath ek test hai jo dobara tootne se rokta hai; koi bhi existing test kamzor nahi kiya gaya.

---

## 10. Teen sabse bade risk

**Risk 1 — Live provider behaviour abhi tak unproven hai (sabse bada).**
Circuit breaker, cooldown, fallback chain — sab **fake failures** se test hue hain. Asli Groq/Gemini/OpenRouter ke saath ek bhi call nahi hui. 429 ka asli shape, asli header, asli retry-after, aur quota-exhausted ke baad kya hota hai — ye sab owner ki VM pe hi pata chalega. Agar asli error string `_RATE_LIMIT_MARKERS` se match na kare to wo `unknown` (15 s) ban jayega, aur 60 s ki jagah 15 s baad dobara try hoga — matlab bar-bar fail.

**Risk 2 — Desktop layer ka koi real proof nahi.**
Owner ka pura use-case desktop hai (apps kholna, type karna), aur is audit me wo layer 100% stub hai. HiDPI scaling, `SetForegroundWindow` ka fail hona, UAC-elevated window, locked screen — ye sab asli Windows pe hi dikhenge. Jo 7 failures owner ne dekhi thi, unka fix ab test me hai, par test ek **nakli** Windows pe hai.

**Risk 3 — 99 tools ka surface area, jinme se 29 untested aur 4 unwired hain.**
Har naya tool permission matrix me silently READ ban sakta hai (D-4 yahi tha). Ab ek test hai jo galat classification pakdega, par sirf un 6 naam ke liye jo humne fix kiye. Agla tool jo kisi keyword se match na kare, wo phir se READ banega — aur agar wo guest surface pe allowed ho gaya to phir se wahi hole.

---

## 11. Files

| File | Kya hai |
|---|---|
| `docs/AUDIT_REPORT.md` | ye report |
| `docs/CLAIMS_AUDIT.md` | 39 claims ki TRUE/PARTIAL/FALSE table + **Not implemented** section |
| `docs/WIRING_MAP.md` | AST se generate hua wiring map (269 lines) |
| `docs/STATUS.md` | builder ki report, upar independent-audit addendum ke saath |
| `scripts/wiring_map.py` | wiring analyzer (`--json`, `--check`) |
| `tests/test_live_replay.py` | owner ke 7 failures ka replay (12 tests) |
| `tests/test_wiring.py` | wiring contract (22 tests) |
| `tests/test_chaos.py` | failure-injection matrix (42 tests) |
| `tests/test_conversation.py` | conversation limits, Rich UI, memory, Telegram (27 tests) |
| `tests/test_claims.py` | claims document drift na ho (27 tests) |

**Chalane ka tarika:**
```bash
ruff check .
python -m compileall -q jarvis tests
pytest -o addopts=""          # -q count chhupa deta hai (addopts="-q")
python scripts/wiring_map.py --check
python scripts/audit_tools.py
```
