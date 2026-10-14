# 🏗️ ARCHITECTURE — NikhilAI v2.3

> Ye document batata hai ki system andar se kaise chalta hai: graph, workers, tools, memory,
> providers, voice, telegram, aur security model.

---

## 1. High-level flow

```
CLI / Voice / Telegram / HTTP API
            │
            ▼
   jarvis.runtime.agent.run_agent(text, session_id, surface)      ← ek hi entry point
            │
            ├── fast paths (deterministic, LLM ke bina)
            │     • time/date    → "aaj ka date", "abhi kitne baje" (local clock, guess nahi)
            │     • memory ops   → "yaad rakh…", "bhool jao…", "memories dikhao"
            │     • settings ops → "mode voice karo", "provider openrouter"
            │
            └── LangGraph supervisor graph
                  load → recall → route → plan → dispatch
                                                    ├─ research_worker
                                                    ├─ browser_worker
                                                    ├─ code_worker
                                                    ├─ desktop_worker
                                                    └─ memory_worker
                  synthesize → critic → (refine ↺) → persist → respond
```

**Kyun ek runner:** voice/text/telegram ke alag brain banane se behaviour diverge hota hai aur
bug teen jagah fix karne padte hain. Yahan sab ek hi graph, ek hi memory, ek hi tools use karte
hain — bas `session_id` aur `surface` (permission level) badalta hai.

### Kya parallel hai

`dispatch` node LangGraph ke `Send` fan-out se saare selected workers **ek saath** chalata hai.
State me `Annotated[list, operator.add]` reducers lage hain (`findings`, `artifacts`, `errors`,
`events`, `notes_saved`), isliye parallel outputs safely merge hote hain.

### Failure isolation

Ek worker crash → baaki workers chalte rehte hain, error `errors[]` me record hota hai, aur answer
adhoora nahi — balki "kya fail hua" ke saath aata hai. Provider fail → model chain agla try.
Tool fail → fallback tool (search: ddgs → searx → brave → bing → ddg-html → wikipedia).

---

## 2. State schema (`jarvis/graph/state.py`)

| Field | Type | Kaam |
|---|---|---|
| `request` | str | owner ki request |
| `session_id` | str | cli / voice / telegram:<chat_id> / automation:<id> |
| `messages` | `Annotated[list, add_messages]` | LangChain message history (checkpointer) |
| `memory_context`, `owner_facts`, `history_summary` | str | recall node ka output |
| `workers`, `route_reason`, `needs_clarification` | — | router ka decision |
| `plan`, `deliverable`, `selected_tools` | list/str | planner ka output |
| `findings` | `Annotated[list, add]` | workers ke structured results |
| `evidence` | str | synthesize ke liye digested evidence |
| `artifacts`, `notes_saved`, `errors` | `Annotated[list, add]` | outputs |
| `iterations`, `critique`, `fix_hint` | int/dict/str | critic-refine loop |
| `answer`, `spoken` | str | final answer + TTS-ready text |
| `reasoning_summary` | list[str] | PLAN / DECISION / ACTION / RESULT / VERIFICATION |
| `events` | `Annotated[list, add]` | observability trace |
| `status`, `task_id` | str | lifecycle |

---

## 3. Reasoning layer (`jarvis/dspy_engine/`)

DSPy modules (har ek ka **deterministic fallback** hai — LLM na mile to bhi chalta hai):

| Module | Kaam | Fallback |
|---|---|---|
| `router` | request → workers (fast model) | keyword + intent heuristics |
| `planner` | executable plan steps | template plan |
| `code_smith` | code generate (code engine stage-1) | sketch template |
| `extract_structured` | page text → JSON fields | regex/heuristic |
| `normalize_rows` | multi-site rows comparable banao | passthrough |
| `critic` | answer quality check | score heuristic |
| `final_answer` | polished Hinglish synthesis | offline template |
| `voice_polish` | short spoken version | `voice.speech` cleanup |

**Action verification:** `desktop_worker` sirf tool ke *actual* output ko maanta hai. Sub-agent ne
tool call na kiya → deterministic executor khud chalata hai; jo hua wo `actions_done`/`action_failures`
me jaata hai, aur `honesty_guard` final answer banata hai (fail step sabse upar, fake success block).

**Time policy:** koi bhi date/time ka jawab local system clock se aata hai (`_time_answer` fast path),
aur `evidence_digest()` har LLM call me ek authoritative `LOCAL CLOCK` line inject karta hai — model ko
kabhi date/time guess karne ki zaroorat nahi padti.

**Chain-of-thought policy:** hidden reasoning kabhi user ko nahi dikhta. `reasoning_summary`
structured banaya jata hai (PLAN/DECISION/ACTION/RESULT/VERIFICATION/ITERATIONS/ERRORS) aur wahi
CLI/API me dikhaya ja sakta hai (`SHOW_REASONING=true`).

---

## 4. Provider layer (`jarvis/llm/`)

```
jarvis/llm/
├── providers.py    # ProviderSpec registry: gemini · openrouter · groq · openai · anthropic · xai · ollama
├── roles.py        # role → model chain resolution + cross-provider fallback + attempt history
├── discovery.py    # live /models se free/available models (12h cache, offline-safe)
├── core.py         # FallbackChatModel (LangChain BaseChatModel) · LLMFactory · init_dspy
└── demo.py         # DemoChatModel — offline deterministic brain
```

**Resolution order** (role ke liye):

1. Runtime setting / env override (`NIKHILAI_<ROLE>_MODEL`, `BRAIN_MODEL`)
2. Provider defaults (`config.MODEL_DEFAULTS[provider][role]` — free models pehle)
3. Live discovery → sabse fit **free** model (`discovery.best_free_model`)
4. Cross-provider chain (`openrouter → gemini → groq → …`)

`FallbackChatModel` ek LangChain `BaseChatModel` hai jiske andar ye poori chain hoti hai. Har call
par pehla model try, fail hone par agla — aur attempt (provider/model/error/latency) record hota hai
(`model_attempts()`), jo `status`/doctor/API me dikhta hai.

---

## 5. Tool layer (`jarvis/tools/`)

Groups (`tools_for(...)` se agent ko sirf uska subset milta hai):

| Group | Count | Contents |
|---|---|---|
| `desktop` | 34 | system control, files, notes, app finder, shell, processes |
| `runtime` | 9 | REPL (persistent + isolated), session state |
| `research` | 11 | search, fetch, deep research, weather, datetime, browser-* |
| `code` | 5 | code engine + script management |
| `memory` | 5 | search/list/remember/forget/update |
| `ops` | 11 | refresh/cache/process/restart/status + automations |
| `skills` | 2 | SKILL.md loader |
| **total unique** | **77** | `tool_policy_summary()` |

Rule of thumb: **sasta pehle** — HTTP research (seconds) → browser-use (minutes) sirf jab
interaction/JS/login chahiye. Shell commands approval-gated, secrets placeholder-based.

---

## 6. Memory (`jarvis/memory/`)

```
store.py     — SQLite + FTS5: facts · episodes · tasks · notes (+ search_index)
manager.py   — high-level ops: remember · recall · update · forget · list · summarize
memory_tools.py — LangChain tools (agent se callable)
session_state.py — short-term JSON: workspace, task history, last error, pending steps
```

- **Kinds:** fact · preference · profile · episode · task · note · setting
- **Dedupe:** token-set similarity ≥ 0.86 par naya insert nahi — update ya duplicate-skip
- **Real deletion:** `forget()` row + FTS index dono se hatata hai (verify tests me)
- **Store policy:** sirf useful/explicit cheezein — har message save nahi hota
- **Isolation:** Telegram per-chat session; long-term memory owner-scoped (`user_id`)

---

## 7. Automations (`jarvis/automation/`)

```
scheduler.py — AutomationStore (workspace/automations.json) + daemon thread (20s tick)
tools.py     — create/list/toggle/run/cancel LangChain tools
```

`parse_schedule()` natural language samajhta hai: `har subah 8 baje` · `daily 18:30` · `every 2h` ·
`har 30 minute` · `kal 6 PM` · `6 baje`. Runner wahi `run_agent()` call karta hai — yaani scheduled
task bhi poora graph chalata hai (research/code/browser sab available).

---

## 8. Security (`jarvis/security/permissions.py`)

```
READ < WRITE < EXECUTE < SYSTEM < DANGEROUS
```

| Surface | Max level |
|---|---|
| `cli_owner` | DANGEROUS |
| `telegram_owner` | DANGEROUS |
| `telegram_guest` | READ |
| `voice_owner` | EXECUTE |
| `api` | WRITE |
| `automation` | EXECUTE |

Layered defence: permission level → approval gate (`EXEC_APPROVAL` + token) → catastrophic pattern
blocklist → protected paths → secret redaction. Blacklist-only nahi — **permission-based** model.

---

## 9. Voice (`jarvis/voice/`)

```
MIC → LiveKit (WebRTC) → Gemini Live (realtime STT+LLM+TTS)
                              │ tool calls: current_time · agent_task · remember · forget
                              ▼
                    runtime.agent.run_agent(surface="voice_owner")
                              ▼
                    LangGraph (same graph + tools + memory)
                              ▼
                    voice.speech.for_speech()  ← emoji/markdown/code/URL cleanup
                              ▼
                         spoken answer
```

`voice/speech.py` TTS se pehle text normalize karta hai: emojis (mapped → words, warna strike),
markdown, code blocks, links, paths, tables, bullets. Isliye model kabhi "white heavy check mark"
ya "asterisk asterisk" nahi bolta.

---

## 10. Telegram (`jarvis/telegram/`)

python-telegram-bot (async) long polling. Auth triptych: `TELEGRAM_OWNER_ID` (owner) ·
`TELEGRAM_ALLOWED_USERS` · `TELEGRAM_ALLOWED_CHAT_IDS` (guest, read-only). Commands: start/help/
status/mode/model/provider/tools/settings/memory/remember/forget/automation/refresh/clear/stop/
restart/voice/menu/progress — plus free-text (bina slash ke kuch bhi likho → agent chalega).

| File | Kaam |
|---|---|
| `bot.py` | handlers, auth, `run_agent` bridge (wahi LangGraph), inline buttons (callback → wahi command fn) |
| `live.py` | **live progress message**: ek message live edit (spinner + last 8 events), sync-safe `on_event`, background flusher (rate-limit safe), error/traceback blocks |
| `md.py` | markdown → Telegram HTML (bold/code/heading/link, injection-safe) + 4096-safe chunking |

---

## 9b. Tool-binding rule (`jarvis/llm/core.py::FallbackChatModel.bind_tools`)

```
chain models ──bind_tools(tools)──┬── ✅ bind hua          → chain me rakho
                                  ├── ❌ bind fail         → chain se HATA do (+ record karo)
                                  └── (sab fail)           → ToolBindingError raise (loud)
```

* Kabhi bhi unbound model se jawab nahi banaya jata — warna agent tools ke bina "confident"
  answer de deta hai (silent degradation).
* `with_structured_output()` LangChain me isi `bind_tools` par jaata hai → wahi safety wahan bhi.
* Observability: `bind_failures`, `binding_report()`, `roles.attempts()` aur live progress warn.
* ReAct subagent (`graph/nodes.py::_run_react_subagent`) `ToolBindingError` ko pakad kar
  `SubAgentRun(error=...)` deta hai → graph honest "kaam fail hua + reason" banata hai.
* `require_tool_bound(model)` (runtime/evidence.py) belt-and-braces check hai — koi naya code path
  bina bind kiye model pass kare to wahi loud fail hota hai.
* Logging (`jarvis.llm` logger): har model ka bind success/fail + exception, aur summary
  `tool-binding summary role=… bound=… failed=…`. `binding_report()` me `bound_models` + `targets`.

---

## 9c. Execution evidence — "success bina proof ke nahi" (`jarvis/runtime/evidence.py`)

```
worker/action ──tool call──► record {name, args, output, ok, source} ──► state.tool_evidence
                                                                          │
synthesize ──► honesty_guard() ──► verify_execution() ──────────────────┘
                                   │ ok records + failed records + claims
                                   ▼
                    verified=True  |  failed  |  partial_failure
```

* `TOOL_REQUIRED_WORKERS = {desktop, code, browser, ops, automation, system}` — inme text-only
  jawab success nahi ban sakta.
* Evidence sources: `state["tool_evidence"]` (workers), `actions_done`/`action_failures`
  (deterministic desktop executor), `findings[].tool_calls` (ReAct subagent ke asli ToolMessages).
* `_clean_record()` outcome keys (`found`, `path`, …) carry karta hai — khali search
  (`found=False`) ko success evidence nahi maana jata (`supporting_records()`).
* `honesty_guard()` claim + evidence ko match karta hai; mismatch → answer rewrite + honest status.
* `respond_node` status ko kabhi blindly `"done"` nahi karta (guard ka status hi upstream jaata hai).
* `AgentResult.recompute_execution_flags()` flags ko evidence se dobara nikalta hai — koi code path
  `verified_execution` ko manually true nahi kar sakta.

Graph-level test: `tests/test_tool_invariant.py` (TEST 4/6 real desktop execution + honest failure).

---

## 10a. Command routing (`jarvis/cli/routing.py`)

`python -m jarvis <kuch bhi>` ka ek hi faisla-karta module:

1. pehla shabd **alias/command** hai? → wo command (`console`/`mic`/`keys`/`tg`/`diag`/`st`/`menu` …)
2. filler shabd (`chalao`, `kholo`, `start`, `mode`, `karo`) hata do → `python -m jarvis console chalao` bhi chalta hai
3. single-word close typo (≥80% similar, typo auto-route) → alias + user ko note (`consol` → console)
4. warna → **poori line AI request** (`hy`, `aaj ka mausam batao`) — koi rigid prefix nahi

Interactive CLI me `route_natural_command()` wahi kaam karta hai natural lines ke liye
(`console chalao`, `mic se baat karni hai`, `telegram chalao`) — aur sirf wahi lines pakadta hai
jo clearly local session start karti hain (agent ke kaam hijack nahi hote).

---

## 10b. Live progress bus (`jarvis/runtime/progress.py`)

```
graph nodes ─┐
agent ───────┼─► emit(kind, text, tool=…, status=…) ─► Sink (ContextVar)
tool loop ───┘                                          ├── CLI: cli/pro_ui.Dashboard (Rich Live)
                                                        └── TG:  telegram/live.LiveProgress
```

* Kinds: `start · thinking · plan · tool_start · tool_end · verify · warn · error · done`
* Sink `ContextVar` me hota hai — isliye koi function signature nahi badla, aur library/test use
  me default `NullSink` (chup-chaap, kuch print nahi).
* `CallbackSink` crash-safe hai — progress kabhi main kaam nahi todta.
* CLI: `cli/pro_ui.py` = header bar + live stream + plan/tools side panel + `/menu` keyboard menu +
  `/run` tool runner. Telegram: ek hi message edit hota hai (chat spam nahi).

---

## 11. Interfaces

| Surface | Entry | Notes |
|---|---|---|
| CLI | `python -m jarvis` / `cli` / koi bhi free text | **live dashboard** (header + activity stream + plan/tools panel), `/menu` button menu, `/run <tool>` runner, reasoning panel |
| Voice | `python -m jarvis voice [console|dev|start|test|keys|status]` | LiveKit + Gemini Live; `test` = keys ke bina pipeline check |
| Telegram | `python -m jarvis telegram` | same agent, per-chat sessions |
| HTTP API | `python -m jarvis serve` | `/chat` `/stream` `/tools` `/memory` `/automations` `/voice` `/graph` `/health` |
| Doctor | `python -m jarvis doctor [--json] [--fix]` | 37 checks |
| Tests | `python -m jarvis test` | 422 checks (428 with livekit) — offline-safe |
| Audit | `python scripts/audit.py` | repo audit: compile/imports/secrets/legacy/integrations |
| Tool audit | `python scripts/audit_tools.py --report TOOL_AUDIT.md` | har tool LLM ki tarah invoke — 77/77 callable proof |

---

## 12. Scaling path

1. **More workers** — naya worker `nodes.py` me ek async function + `WORKER_NODES` me entry; router
   keywords me add karo. Graph edges sab generic hain.
2. **More tools** — `jarvis/tools/*` me `@tool` function + group registry entry.
3. **More providers** — `providers.py` me `ProviderSpec` (OpenAI-compatible hone par sirf base_url).
4. **Better routing** — `config.MODEL_DEFAULTS` me role models update; discovery live models khud
   le aayegi.
5. **Deeper memory** — SQLite ko Postgres/pgvector par swap karo (`store.py` ka interface same).
6. **Multi-user** — abhi owner-scoped; per-user memory namespace + per-user permissions extend karna easy hai.
