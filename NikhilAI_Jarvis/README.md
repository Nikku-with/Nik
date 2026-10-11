# 🤖 NikhilAI

**A local/VM-first autonomous AI operator** — CLI, real-time voice, aur Telegram teen se chalta hai.
Ek hi LangGraph brain, 100+ tools, multi-provider fallback, memory, automations aur self-healing code.

> Chatbot nahi. Ye ek **operator** hai: kaam karta hai, files manage karta hai, browser chalata hai,
> code likh ke chalata hai, shell commands deta hai, research karta hai, yaad rakhta hai, aur
> schedule par apne aap kaam karta hai.

```
you › meri resume pdf dhoondh ke 3 line summary do
🤖 desktop  → find_files (typo-proof) → read_file (PDF) → summary      1.9s

you › har subah 8 baje mausam batao
🤖 ✅ Automation set: #5db6f54c — daily 08:00 (next: 05 Oct 2026, 08:00)

you › refresh system karo
🤖 ♻️ config reload • tool registry reload (77 tools) • model cache reset — aapka data safe hai

you › yaad rakh mera favourite laptop Dell XPS hai
🤖 🧠 yaad rakh liya (fact)          ← bolne par update/delete bhi karta hai
```

---

### Verification: "bola kar diya" ka matlab tool chala ✅

Model kabhi hawa me "ho gaya" bol deta hai — NikhilAI ab ye **verify** karta hai:

1. Sub-agent ke **actual tool calls** track hote hain (LangChain `AIMessage.tool_calls` + `ToolMessage` dono se).
2. Action wali request par agar sub-agent ne tool call **nahi** kiya → engine khud deterministic
   executor se wahi tool chalata hai (`notepad open krke hello likho` → open + type).
3. Jo hua wahi state me jaata hai: `action_required` / `actions_done` / `action_failures`.
4. **Honesty guard** final answer banata hai — fail hue step hamesha **sabse upar** dikhte hain:

```
⚠️ Aadha kaam hua, Nikhil Mehra.
✅ Jo hua: open_any_app → notepad launch ho gaya
❌ Jo fail hua: type_text → typing fail: Windows par SendKeys block hai
```

Kuch bhi nahi chala to seedha: *"Kaam nahi hua — koi tool call nahi chala. Aise hi 'ho gaya'
bolna galat hota hai."* Fake success kabhi nahi.

### Typing / hotkey / screenshot — zero install

Windows/macOS par kuch install karne ki zaroorat **nahi**: typing PowerShell `SendKeys` (unicode
ke liye clipboard + `^v`), hotkey `SendKeys`/`osascript`, screenshot PowerShell `System.Drawing` /
`screencapture`. Linux par `xdotool`/`wtype` (ya `gnome-screenshot`/`scrot`) chahiye.
Chahte ho smooth automation to optional extra: `pip install "nikhilai[desktop]"` (pyautogui).


## 📑 Table of contents

1. [Ye kya hai](#-ye-kya-hai)
2. [Install](#-install-5-minute-setup)
3. [Configuration](#-configuration-kaun-si-key-chahiye)
4. [Teen modes](#-teen-modes)
5. [Architecture](#-architecture)
6. [Tools](#-tools-100)
7. [Memory](#-memory)
8. [Automations](#-automations)
9. [Multi-provider brain + fallbacks](#-multi-provider-brain--fallbacks)
10. [Browser-Use](#-browser-use)
11. [Code execution + self-healing](#-code-execution--self-healing)
12. [Security](#-security)
13. [Troubleshooting](#-troubleshooting)
14. [Deployment (VM/container)](#-deployment-vm--container-recommended-in-this-order)

---

## 🧠 Ye kya hai

Ek multi-agent system jo aapke machine par (ya VM me) chalta hai:

| Layer | Kya use hota hai |
|---|---|
| Orchestration | **LangGraph** — explicit graph: load → recall → route → plan → parallel workers → synthesize → critic → refine → persist → respond |
| Tools | **LangChain** tool calling (+ OpenAI-compatible tool schema) |
| Brain | **Gemini / OpenRouter / Groq** — role-based routing + multi-level fallback |
| Reasoning | **DSPy** modules (router, planner, extractor, critic, final answer) + heuristic fallback |
| Browser | **Browser-Use** (Python agents + CLI harness) |
| Voice | **LiveKit Agents + Gemini Live** (realtime, low latency) |
| Interfaces | CLI (Rich UI), Voice, **Telegram bot**, HTTP API + web console |
| Memory | SQLite + FTS5 (facts/preferences/profile/episodes/tasks/notes/settings) |
| Extras | Automations (cron-like), skills (SKILL.md), background processes, approvals |

**Offline/demo mode:** koi API key na ho to bhi poora system chalta hai — deterministic demo brain
ke saath asli graph, asli tools, asli memory. Pehli baar try karne ke liye key ki zaroorat nahi.

---

## ⚡ Install (5-minute setup)

```bash
git clone <your-repo-url> NikhilAI && cd NikhilAI

# 1) virtual environment
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 2) dependencies
pip install -U pip
pip install -r requirements.txt

# 3) config
cp .env.example .env               # Windows: copy .env.example .env
# .env kholo → OWNER_NAME likho + ek brain key daalo (neeche dekho)

# 4) check sab theek hai?
python -m jarvis doctor

# 5) chalao
python -m jarvis
```

**Minimum:** Python 3.10+ aur ek brain key. Voice ke liye LiveKit keys (free), Telegram ke liye bot
token (free). Baaki sab optional hai — search bina key ke bhi chalti hai (`ddgs`).

---

## 🔑 Configuration — kaun si key chahiye

Poora detail: **[SETUP_APIS.md](SETUP_APIS.md)** (step-by-step, screenshots-jaise instructions).

| Kya | Kahan se | Zaroori? |
|---|---|---|
| `GEMINI_API_KEY` | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) — free tier | ✅ haan (ya OpenRouter/Groq) |
| `OPENROUTER_API_KEY` | [openrouter.ai/keys](https://openrouter.ai/keys) — free `:free` models | optional (fallback) |
| `GROQ_API_KEY` | [console.groq.com/keys](https://console.groq.com/keys) — free tier, bahut fast | optional (fallback) |
| `LIVEKIT_URL` + `LIVEKIT_API_KEY` + `LIVEKIT_API_SECRET` | [cloud.livekit.io](https://cloud.livekit.io) → Settings → Keys | voice ke liye |
| `TELEGRAM_BOT_TOKEN` | Telegram par **@BotFather** → `/newbot` | Telegram ke liye |
| `TELEGRAM_OWNER_ID` | Telegram par **@userinfobot** | Telegram secure rakhne ke liye (recommended) |
| `BING_SEARCH_API_KEY` / `BRAVE_API_KEY` | optional search upgrade | optional |

**Identity sab env se control hoti hai** (code me hardcode nahi):

```env
NIKHILAI_NAME=NikhilAI          # assistant ka naam
OWNER_NAME=Nikhil Mehra         # aapka naam
OWNER_ID=                       # aapki id (telegram/allowlist)
ASSISTANT_PERSONALITY=smart,friendly,concise,agentic
LANGUAGE=hinglish               # hinglish | hindi | english | auto
RESPONSE_STYLE=concise          # concise | detailed | bullet
DEFAULT_MODE=text               # text | voice | telegram
```

Runtime me badalna ho (restart ke bina, persist bhi hota hai):

```bash
python -m jarvis settings owner_name "Nikhil Mehra"
python -m jarvis settings mode voice
python -m jarvis settings personality smart,savage,concise
# CLI ke andar:  /settings owner_name Nikhil        Telegram: /settings owner_name Nikhil
```

---

## 🎛 Teen modes

### 1️⃣ Text / CLI mode

```bash
python -m jarvis                # interactive menu
python -m jarvis cli            # direct CLI
python -m jarvis "chrome kholo" # ek request chala kar exit
```

**Koi rigid prefix nahi** — `python -m jarvis` ke baad jo bhi likho:

| Aap likho | Kya hota hai |
|---|---|
| `python -m jarvis hy` | **"hy" seedha AI ko jaata hai** (poore agent pipeline se jawab) |
| `python -m jarvis aaj ka mausam batao` | wahi — poori line ek AI request |
| `python -m jarvis console` | 🎙️ **LiveKit console** (shortcut of `voice console`) — mic+speaker, browser nahi |
| `python -m jarvis mic` / `talk` / `speak` / `awaaz` | wahi console mode |
| `python -m jarvis keys` | voice ke liye exact keys + steps |
| `python -m jarvis voice test` | keys ke bina voice pipeline check |
| `python -m jarvis tg` / `bot` | Telegram bot |
| `python -m jarvis diag` / `st` / `menu` | doctor / status / control menu |
| `python -m jarvis consol` (typo) | `console` samajh leta hai + bata deta hai ki typo samjha |
| `python -m jarvis "text mode start"` | text CLI mode |

Rule bilkul simple hai: **pehla shabd agar command/alias hai → wo command**, warna **poori line AI ke liye request**.
`chalao / kholo / start / mode / karo` jaise filler shabd automatically hata diye jate hain
(`python -m jarvis console chalao` bhi chalta hai), aur close typo (≥80% similar) auto-correct ho jata hai.

Interactive CLI ke andar bhi natural lines chalti hain: `console chalao`, `voice mode chalu karo`,
`mic se baat karni hai`, `telegram chalao`, `doctor chalao`, `menu` — baaki sab kuch agent ko jaata hai
(jaise `telegram par message bhej do` — wo agent ka kaam hai, router usse hijack nahi karta).

CLI ka look pro hai — ek **live dashboard** jo request ke dauraan khud update hota hai:

```
╭────────────────────────── ◆ NIKHILAI v2.3.0 ──────────────────────────╮
│ brain gemini-3.8-flash • mode text • tools 77 • mem 42   GEMINI LIVE • 2.4s │
╰───────────────────────────────────────────────────────────────────────╯
╭─ ⚡ Live activity ─────────────────╮╭─ 🧭 Context ─────────────────╮
│ ⠇ open_any_app chal raha hai…     ││ 📋 Plan (3)                  │
│ 🚀 Request mila: chrome kholo     ││ 1. app dhoondho              │
│ 🧠 Samajh raha hoon + memory…     ││ 2. kholo                     │
│ 📋 3-step plan ready              ││ 3. verify karo               │
│ 🛠️ open_any_app chala raha hoon   ││ 🛠️ Tools (1)                 │
│ ✅ open_any_app: Chrome khul gaya ││ ✅ open_any_app 812ms        │
╰───────────────────────────────────╯╰──────────────────────────────╯
```

• **Thinking / planning / har tool call live dikhta hai** (spinner ke saath) — chup-chaap kaam nahi.
• `/menu` = keyboard **button menu** (↑/↓ + Enter, ya hotkey letter) — status, tools browser,
  memory, automations, settings, voice, doctor, refresh.
• `/tools [group]` — group-wise browser (desktop, runtime, research, web, browser, code, memory,
  skills, ops, automation, system_ops).
• `/run <tool> [k=v ...]` — **koi bhi tool seedha chalao**, schema auto-dikhta hai:
  `/run system_info`, `/run write_file path=notes.txt content=hello`,
  `/run search_inside_files query=jarvis`.
• Errors panel me poora error + last steps ka trail aata hai (kuch chhupta nahi).
• Progress band chahiye? `/settings cli_progress off` (ya `telegram_progress` TG ke liye).

Slash commands: `/help /status /mode /voice /text /model /provider /tools /menu /run /dash
/memory /settings /automation /skills /refresh /clear /stop /restart /doctor /reasoning /quit`
— par zaroorat nahi, seedha Hinglish me likho.

Sab tool callable hain — proof: [`TOOL_AUDIT.md`](TOOL_AUDIT.md) (77/77 ✅, regenerate:
`python scripts/audit_tools.py --report TOOL_AUDIT.md`).

### 2️⃣ Voice mode (real-time)

```bash
pip install "livekit-agents[google]" livekit-plugins-google livekit-plugins-silero

python -m jarvis voice --help      # saare modes + keys ki list
python -m jarvis voice keys        # kaunsi keys chahiye, kahan se milegi (exact 5-min steps)
python -m jarvis voice test        # keys ke BINA pipeline check (spoken text + latency)
python -m jarvis voice console     # terminal me mic+speaker se baat karo
python -m jarvis voice dev         # dev mode (LiveKit Cloud playground)
python -m jarvis voice start       # production worker
python -m jarvis voice status      # kya ready hai, kya missing

# LiveKit ke apne options seedha pass hote hain:
python -m jarvis voice console --list-devices     # mic/speaker list karo
python -m jarvis voice console --text             # mic ke bina keyboard se test
python -m jarvis voice console --input-device 2   # specific mic chuno
```

Menu se bhi wahi chalta hai: `python -m jarvis` → `2` (Voice mode) — aur ab woh
`console` mode me hi khulta hai (pehle LiveKit apna *Usage* print karke ruk jata tha —
ab fix hai, `tests/test_voice_cli.py` isi ka regression test hai).

Console me kaise: `voice console` aapke hi laptop ka mic/speaker use karta hai (koi browser nahi
chahiye) — pehle `voice keys` se teen keys (`LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`)
+ `GEMINI_API_KEY` `.env` me daalo, phir `voice status` par sab ✅ dikhna chahiye.
Keys nahi hain abhi? `voice test` poora speaking pipeline bina keys chala kar dikhata hai.

Pipeline: `MIC → LIVEKIT (WebRTC) → GEMINI LIVE (realtime) → tools → LangGraph → spoken answer`.
Voice aur text **same brain** use karte hain — same memory, same tools, same permissions.

Voice par jawab chhota aur bolne layak hota hai: emojis markdown URLs code-blocks automatically
saaf ho jate hain (TTS "white heavy check mark" nahi bolta).

### 3️⃣ Telegram mode

```bash
# .env me TELEGRAM_BOT_TOKEN + TELEGRAM_OWNER_ID daalo, phir:
python -m jarvis telegram
```

Telegram pe bot ko message bhejo — wahi agent chalega:

```
chrome kholo                          → desktop tool
aaj ka mausam batao                   → live research
meri resume pdf dhoondh ke summary do → file intelligence
yaad rakh mera office Sector 62 me hai → memory
har subah 8 baje news bhejo           → automation
/mode voice   /model groq:llama-3.3-70b-versatile   /settings owner_name Nikhil
```

Authorization: `TELEGRAM_OWNER_ID` = full access. `TELEGRAM_ALLOWED_USERS` / `TELEGRAM_ALLOWED_CHAT_IDS`
me daale gaye log **guest** hote hain → sirf read-only tools (shell/desktop/file-write blocked).
Har chat ka apna session aur apni memory isolation hai.

**Live progress + errors Telegram par bhi dikhte hain** (ek hi message edit hota rehta hai,
chat spam nahi):

```
⠹ open_any_app chal raha hai…
2.1s • 6 events
🚀 Request mila: chrome kholo
🧠 Samajh raha hoon + memory check…
📋 3-step plan ready
🛠️ open_any_app chala raha hoon
✅ open_any_app: Chrome khul gaya (812ms)

[final jawab]

⚠️ Errors (1):
• honesty_guard: all actions failed
Failed steps:
❌ type_text: pyautogui missing
```

Crash ho to bot chup nahi hota — error type + traceback tail + progress trail sab bhejta hai.
Band karna ho: `/progress off`.

**Buttons:** `/start` ya `/menu` par inline keyboard aata hai — `Status · Tools · Settings ·
Memory · Refresh · Mode · Automations · Help`. Button dabane par wahi command chalti hai
(duplicate logic nahi).

---

## 🏗 Architecture

```
                        ┌──────────────────────────────┐
   CLI / Voice / TG /   │  jarvis.runtime.agent        │  ← ek hi entry point
   HTTP API  ──────────▶│  run_agent(text, session,    │
                        │            surface)          │
                        └──────────────┬───────────────┘
                                       ▼
        ┌──────────────────────────────────────────────────────────┐
        │              LangGraph supervisor graph                  │
        │                                                          │
        │  load → recall → route ─┬→ plan → dispatch (parallel)    │
        │                         │      ├── research_worker       │
        │                         │      ├── browser_worker        │
        │                         │      ├── code_worker           │
        │                         │      ├── desktop_worker        │
        │                         │      └── memory_worker         │
        │                         └→ synthesize → critic → refine │
        │                                    → persist → respond   │
        └──────────────────────────────────────────────────────────┘
                     │                │                 │
              ┌──────▼─────┐   ┌──────▼──────┐   ┌──────▼───────┐
              │ tools (77+) │   │ memory      │   │ llm router   │
              │ LangChain   │   │ SQLite+FTS5 │   │ roles + chain│
              └─────────────┘   └─────────────┘   └──────────────┘
```

**Reasoning**: hidden chain-of-thought kabhi expose nahi hota. Iske bajaye **structured summary**
milta hai jo user ko dikhaya ja sakta hai:

```
PLAN: owner ki request samjho → web se data → verify → note save
DECISION: research, desktop worker chune gaye
ACTION: research 4.2s | desktop 0.3s
RESULT: 8,420 chars evidence, 2 artifacts
VERIFICATION: 0.9 (critic pass)
```

**Files:**

```
jarvis/
├── __main__.py          # entry: cli · voice · telegram · doctor · status · test · settings · serve
├── config/              # settings.py (env) + runtime.py (live, editable, persisted)
├── llm/                 # providers · roles (role router) · discovery (free models) · core (fallback) · demo
├── graph/               # state · nodes (workers) · build (compile + stream)
├── tools/               # system · file_tools · file_readers · app_finder · web · browser · code_engine · notes · system_ops
├── runtime/             # agent (unified runner) · repl · process_manager · status · session state
├── memory/              # store (SQLite/FTS) · manager (remember/update/forget) · tools
├── automation/          # scheduler + tools (scheduled/recurring tasks)
├── security/            # permissions (READ → DANGEROUS levels)
├── voice/               # livekit_agent + speech (emoji-safe TTS text)
├── telegram/            # bot (auth + commands + free text)
├── cli/                 # ui (Rich) · app (REPL) · doctor
├── skills/              # SKILL.md loader
├── dspy_engine/         # signatures · modules · optimize
├── agents/              # langchain bridge (create_agent sub-agents)
├── api/                 # FastAPI server + web console
└── prompts.py
```

---

## 🧰 Tools (77+)

| Group | Count | Examples |
|---|---|---|
| Desktop / files | 34 | `find_files`, `read_file` (PDF/DOCX/XLSX/CSV/JSON/ZIP…), `search_inside_files`, `open_any_app`, `run_shell_command`, `take_screenshot`, `open_path`, `write_file` |
| Runtime | 9 | `python_repl`, `run_python_isolated`, `repl_state`, `repl_reset`, `session_context`, `set_workspace` |
| Research / web | 11 | `google_search`, `fetch_page`, `deep_research`, `get_weather`, `get_current_datetime`, `browser_task`, `browser_research` |
| Code | 5 | `code_engine`, `write_script`, `run_script`, `heal_code`, `list_scripts` |
| Memory | 5 | `memory_search`, `memory_list`, `remember_fact`, `forget_memory`, `update_memory` |
| Ops / automation | 11 | `refresh_system_tool`, `clear_cache_tool`, `clear_process_tool`, `restart_jarvis_tool`, `create_automation`, `list_automations` |
| Skills | 2 | `list_skills`, `read_skill` |

Groups se agents ko sirf relevant subset milta hai (`tools_for("desktop","runtime","ops")`) —
isse model ka decision fatigue kam hota hai aur latency gir jaati hai.

---

## 🧠 Memory

```bash
you › yaad rakh mera favourite color black hai
you › mera model OpenRouter hai, yaad rakho
you › show my memories
you › mera model wali memory update karo — ab Gemini use karta hoon
you › wo purani color wali baat bhool jao         → asli DELETE (index se bhi)
you › meri saari preferences delete karo           → /memory all
```

Kinds: `fact · preference · profile · episode · task · note · setting`.
Duplicate avoid hote hain, aur sirf useful/explicit cheezein store hoti hain — har message nahi.

---

## ⏰ Automations

```bash
you › har subah 8 baje aaj ka mausam batao
you › every 2h laptop price check karo
you › kal 6 PM par report bhej do
you › mere automations dikhao                    # ya: /automation list
you › wo automation band kar do                  # /automation off <id>
```

`workspace/automations.json` me persist — restart ke baad bhi zinda. Scheduler background thread
hai (koi external dep nahi) aur wahi LangGraph agent chalata hai.

---

## 🧠 Multi-provider brain + fallbacks

```env
BRAIN_PROVIDER=             # khali = auto-detect (gemini → openrouter → groq)
BRAIN_MODEL=                # khali = role default
GEMINI_API_KEY=...          # ek bhi key kaafi hai
OPENROUTER_API_KEY=...
GROQ_API_KEY=...
```

**Role-based routing** — har role ka apna model + fallback chain:

| Role | Kaam | Default (gemini) | Fallback |
|---|---|---|---|
| `brain` | planning + final answer | `gemini-3.8-flash` | 3.7 → 2.5 → doosre providers |
| `fast` | router, chhote steps | `gemini-3.5-flash-lite` | 3.1-lite → groq llama-8b |
| `code` | code likhna/heal | `gemini-3.8-flash` | 3.7 → pro |
| `research` | page extract | `gemini-3.7-flash` | 3.6 |
| `vision` | screenshots | `gemini-3.7-flash` | — |
| `critic` | verify | `gemini-3.7-flash` | — |

**Fallback 4 levels par:**

1. **Model** — ek model 404/quota de to chain ka agla (`FallbackChatModel`)
2. **Provider** — Gemini fail → OpenRouter → Groq (chain me mixed)
3. **Discovery** — koi configured model na chale to live `/models` se best **free** model uthata hai
4. **Tool** — search API → ddgs → SearXNG → Bing → DDG HTML → Wikipedia · HTTP research → Browser-Use

Har attempt record hota hai (provider, model, error, latency) — `/status` aur doctor me dikhta hai.

**Tool-binding safety (silent degradation band):** jab model par tools bind hote hain
(`create_agent` / ReAct / `with_structured_output`), to har chain model par bind try hota hai —

| Halat | Kya hota hai |
|---|---|
| Bind fail (model tool-calling support nahi karta) | wo model **chain se hata diya jata hai** — kabhi bhi unbound model se jawab nahi banaya jata; wajah `bind_failures` + attempts-log + live progress warn me dikhti hai |
| Kuch bind huye, kuch nahi | bache hue models se kaam chalta hai, par "kaun chhoot gaya" saaf record hota hai (`binding_report()`) |
| **Saare** fail | `ToolBindingError` — loud fail. Bina tools ke "confident" jawab dene se behtar hai saaf "kaam nahi hua + reason" |

(Ye ek real bug ka fix hai: pehle `except Exception: bound.append(model)` tha, matlab bind fail
hone par bhi model chain me reh jata tha aur agent tools ke bina jawab de deta tha.)

**Execution proof (hard invariant: `jarvis/runtime/evidence.py`):** tool-required kaam me success
ka sirf ek proof hai — **asli tool call + uska asli result**.

```
TOOL REQUIRED → MODEL MUST BE TOOL-BOUND → ACTUAL TOOL CALL → ACTUAL TOOL RESULT → TAB SUCCESS
```

| Rule | Kaise enforce hota hai |
|---|---|
| LLM ka text ("ho gaya", "opened", "maine kar diya") evidence nahi | `honesty_guard()` evidence/state se verify karta hai, text se nahi; bina evidence ke claim → "⚠️ Ye claim verify nahi hua" |
| Tool chala hi nahi (action maanga tha) | zero evidence + `execution_required` → status `failed`, honest message + `invariant` event |
| Tool fail hua | status `failed`, final answer me "Kaam fail hua" + which tool/why (fake success impossible) |
| Kuch fail, kuch pass | `partial_failure` — warning sabse upar, dono list (✅ jo hua / ❌ jo fail) |
| Search chali par kuch nahi mila | claim hataya jata hai; asli natija ("0 matches") likha jata hai |
| Success | `AgentResult.verified_execution=True` sirf tab jab ok tool record mojood ho aur koi failed record na ho (`recompute_execution_flags()` — manual set se, sirf evidence se) |

Sab checks: `tests/test_tool_invariant.py` (49 checks — TEST 1–6 + source-level anti-regression:
banned `bound.append(model)` pattern, AST check, invariant + guard pinning).

---

## 🌐 Browser-Use

Dono tarike support hain:

- **Python agents** — `browser_task` / `browser_research` (parallel multi-site, structured fields)
- **CLI harness** — `browser-use` binary (JARVIS usse prompt kar sakta hai)

Strategy: **sasta pehle** — simple extraction HTTP research se (seconds me). Interaction/JS/login
chahiye to Browser-Use par escalate. Screenshots, retries, timeouts, session reuse sab handled.

```bash
you › jaao 4 sites se laptop ke top prices laao aur note me laptop_prices naam se save karo
you › amazon par login karke order status check karo     # profile + secrets se
```

---

## 💻 Code execution + self-healing

```
request → plan → generate code → static check → execute → traceback → repair → retest → artifact
```

- `code_engine` — script likhwa kar chalata hai, error par **khud fix kar ke** dobara chalata hai
  (configurable attempts: `CODE_HEAL_ATTEMPTS`)
- `python_repl` — persistent Python session (variables yaad rehte hain) + AST safety + timeout
  (daemon thread, hang nahi hota) + dangerous pattern block
- `run_python_isolated` — fresh `sys.executable` sandbox me temp file chala kar result laata hai
- Artifacts: `workspace/scripts/`, `workspace/artifacts/`

---

## 🔐 Security

- **Permission levels** — `READ < WRITE < EXECUTE < SYSTEM < DANGEROUS`; har surface (CLI owner,
  Telegram owner, Telegram guest, voice, API) ka apna max level. Guest ko shell/desktop **kabhi** nahi.
- **Approval gate** — `EXEC_APPROVAL=risky` par delete/kill/install jaisi commands pe token chahiye
  (`EXEC_TOKEN=haan-karo`). Catastrophic patterns (`rm -rf /`, `mkfs`, `dd if=`) hamesha blocked.
- **Secrets placeholders** — command me `${SECRET_email}` likho; asli value `JARVIS_SECRET_email` se
  aati hai aur output me auto-redact hoti hai. LLM ko plain password kabhi nahi dikhta.
- **Telegram isolation** — unauthorized users blocked, guests read-only, per-chat memory namespace.
- **`.env` kabhi commit nahi** — `.gitignore` me hai; doctor bhi secrets print nahi karta.
- **Data-safe maintenance** — "refresh" / "clear cache" / "process clear" aapke notes, memory aur
  workspace ko **kabhi** nahi chhoote. Vague "sab saaf karo" ko destructive nahi maana jata.

---

## 🧯 Troubleshooting

| Problem | Fix |
|---|---|
| `provider=demo`, jawab "OFFLINE / DEMO MODE" | `.env` me `GEMINI_API_KEY` (ya OpenRouter/Groq) daalo |
| Model 404 / quota error | Automatic fallback chalega; `python -m jarvis status` se dekho. Manual: `/model <naam>` |
| Voice ready nahi | `pip install "livekit-agents[google]" livekit-plugins-google` + 3 LiveKit keys + `GEMINI_API_KEY` |
| Telegram bot reply nahi kar raha | `TELEGRAM_OWNER_ID` set hai? Bot ko `/start` bheja? Token sahi? |
| Telegram chup-chaap ignore karta hai | Aap allowed list me nahi ho — apna numeric id `.env` me daalo |
| Command "risky" bol kar ruk gaya | Bolo/confirm karo token ke saath, ya `EXEC_APPROVAL=off` |
| `browser-use` nahi chal raha | `pip install browser-use`; Chromium na ho to `python -m browser_use.cli install` |
| Sab kuch ek saath check karna | `python -m jarvis doctor` (37 checks) |
| Tests | `python -m jarvis test` (422 checks; 428 with `livekit-agents` installed) |

---

## 🐳 Deployment (VM / container recommended, in this order)

Isko **VM ya container me chalao** — isme shell + file + desktop tools hain, isliye apni personal
machine par dene se pehle ek isolated user ke saath chalana behtar hai.

**Option A — plain VM (simplest)**

```bash
# Ubuntu VM
sudo apt update && sudo apt install -y python3.12-venv git
git clone <repo> NikhilAI && cd NikhilAI
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && cp .env.example .env
nano .env                     # keys + OWNER_NAME
python -m jarvis doctor && python -m jarvis telegram &   # ya cli/voice
```

**Option B — systemd (auto-restart)**

```ini
# /etc/systemd/system/nikhilai.service
[Unit]
Description=NikhilAI agent
After=network-online.target

[Service]
WorkingDirectory=/opt/NikhilaiAI
EnvironmentFile=/opt/NikhilaiAI/.env
ExecStart=/opt/NikhilaiAI/.venv/bin/python -m jarvis telegram
Restart=always
User=nikhilai

[Install]
WantedBy=multi-user.target
```

**Option C — Docker**

```dockerfile
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git curl && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -r requirements.txt
ENV WORKSPACE_DIR=/data
VOLUME ["/data"]
CMD ["python", "-m", "jarvis", "cli"]
```

```bash
docker build -t nikhilai .
docker run -it --env-file .env -v nikhilai-data:/data nikhilai
```

**Option D — voice deploy (LiveKit Cloud)**

```bash
curl -sSL https://get.livekit.io/cli | bash
lk cloud auth
lk agent init my-nikhilai --template agent-starter-python   # YA apna repo use karo
lk agent deploy
```

> ⚠️ Desktop/app-control tools headless VM me limited kaam karenge (koi GUI nahi). Server par
> `DESKTOP_ENABLED=false` kar do, aur shell/file/research/code tools ka poora maza lo.

---

## 🧩 Extend karna

**Naya tool:**

```python
# jarvis/tools/my_tools.py
from langchain_core.tools import tool

@tool
def my_tool(query: str) -> str:
    """Ek line me: ye tool kya karta hai (model isi description se decide karta hai)."""
    return "result"
```

```python
# jarvis/tools/__init__.py me group me add kar do
```

**Naya skill (SKILL.md):**

```markdown
---
name: invoice_banao
description: "Client invoice banane ka process"
keywords: [invoice, bill, gst, client]
tools: [code_engine, save_note]
---
1. client data read_file se lo
2. code_engine se invoice generate karwao
3. verify karo, phir save_note me record rakho
```

**Naya provider:** `jarvis/llm/providers.py` me ek `ProviderSpec` add karo (OpenAI-compatible hone par
sirf `base_url` chahiye) aur `jarvis/config/settings.py` ke `MODEL_DEFAULTS` me role models daalo.

---

## 📜 License

MIT — [LICENSE](LICENSE) dekho. Credits: LangChain, LangGraph, Browser-Use, DSPy, LiveKit,
python-telegram-bot, Rich, aur OpenClaw (ideation reference only — koi code copy nahi).

---

<p align="center"><i>Banaya gaya <b>Nikhil Mehra</b> ke liye — apne machine ka asli maalik banne ke liye.</i></p>
