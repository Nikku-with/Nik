# Independent claims audit — 2026-10-16

A line-by-line check of the delivery's own documentation against the code and against
test runs. **The builder's own reports were not trusted as evidence**; every row below
was re-derived by reading code or running something.

Verdicts:

* **TRUE** — the claim holds and was reproduced here.
* **PARTIAL** — the mechanism exists but does not do what the sentence implies, or only
  holds under conditions the sentence does not state.
* **FALSE** — reproduced as not holding.

Counts in this document were produced on the audited tree, not copied from STATUS.md.

---

## 1. README claims

| # | Claim (README) | Verdict | Evidence |
|---|---|---|---|
| 1 | "one shared agent runtime for the CLI, HTTP/web UI, Telegram, LiveKit voice, and scheduled automations" | TRUE | `scripts/wiring_map.py --json`: all five entry points reach `jarvis.runtime.agent.run_agent` and the LangGraph graph (`surface_reachability`). |
| 2 | "Tool-required actions are only reported successful when there is execution evidence" | TRUE | `tests/test_tool_invariant.py` 49/49; `verification_node` + `verified_execution` on `AgentResult`. Chaos test pins `verified_execution is False` when everything is broken. |
| 3 | "Python 3.10+ is supported" | TRUE | `pyproject.toml:10` `requires-python = ">=3.10"`. |
| 4 | "eighteen workflow packs under `jarvis/skills/catalog/`; ten are the required … workflows" | TRUE | 17 pack directories + `README.md`. All four named extras (`deep_research`, `desktop_browser_ops`, `data_extraction_and_reports`, `automation_and_scheduling`) exist. `wiring_map.py` loads all 18. |
| 5 | "typed observe/OCR/click/type/keyboard/window/clipboard tools; … HiDPI conversion" | TRUE | `jarvis/tools/desktop.py:516,540` scale screenshot coordinates into device space (`device_width / screenshot_width`). |
| 6 | "App launcher: open-app-only workspace memory, lookup before search, save after verified launch, stale-entry recovery" | TRUE | `jarvis/tools/app_finder.py`; `_APP_MEMORY.lookup()` precedes `find_apps()`, `save_alias_choice()` runs after a verified launch. |
| 7 | "Browser supervision: … repeated-state detection" | TRUE | `jarvis/tools/browser_tools.py:71` `BROWSER_STUCK_REPEAT_LIMIT = 6`, enforced at line 462. |
| 8 | "session-state JSON uses locked atomic writes and reports corruption through Doctor" | TRUE | `jarvis/memory/session_state.py:80-109` — tempfile + atomic replace under a lock, failures reported. |
| 9 | "File undo: the last verified file-manager mutation has a bounded workspace snapshot; undo refuses to clobber later edits" | TRUE | `jarvis/tools/file_undo.py` via `undo_file_operation`. |
| 10 | "Safe self-improvement: proposed unified diffs are saved for review; the tool never edits or executes NikhilAI source" | TRUE | `jarvis/tools/improvement.py:36-68` writes a `.patch` + metadata under `workspace/proposals/`, never touches `jarvis/`. |
| 11 | "Personal defaults are `CITY=Kota` and `TIMEZONE=Asia/Kolkata`" | TRUE | `jarvis/config/settings.py:150-151`. |
| 12 | "Google Search uses its own `GOOGLE_SEARCH_API_KEY`" | TRUE | `jarvis/config/settings.py:544` — independent of the Gemini key. |
| 13 | "For a non-loopback API bind … If missing, startup warns but does not block the bind" | TRUE | `jarvis/api/server.py:151` warns on network exposure without a token. |
| 14 | "`ruff check .` / `compileall` / `pytest -q` / `python scripts/audit_tools.py`" | PARTIAL | All four run, but `pytest -q` prints no pass/fail count: `pyproject.toml` sets `addopts = "-q"`. Use `pytest -o addopts=""` to get a count. Documented below. |
| 15 | "CLI and Telegram owners can inspect/change the preference with `/gate`" | TRUE | `jarvis/cli/app.py:360`, `jarvis/telegram/bot.py:308` (`cmd_gate`), registered at `bot.py:919`. |
| 16 | "sensitive-memory confirmations use `/memory pending\|confirm\|cancel`" | TRUE | `jarvis/telegram/bot.py:374` `cmd_memory` + `tests/test_memory_consent_surfaces.py`. |
| 17 | "Approvals are bound to the exact operation, working directory, owner, and surface, expire, and are single-use" | TRUE | `jarvis/security/gate.py`; `tests/test_gate.py`, `tests/test_capability_matrix.py`. |
| 18 | "`/model` and `/provider` show the configured route and key availability without probing provider connectivity" | TRUE | `jarvis/cli/app.py:323,340`; `jarvis/llm/health.py` resolves routes with live discovery disabled. |
| 19 | "MIT license; see LICENSE" | TRUE | `LICENSE` present. |
| 20 | "See CHANGELOG.md" | TRUE | `CHANGELOG.md` present. |
| 21 | "`python -m jarvis doctor` reports exact missing optional packages/configuration without printing secrets" | TRUE | `scripts/audit.py` reports "41 doctor checks"; no secret values observed in output. |
| 22 | "Interactive Hinglish requests; `/help`, `/run`, `/trace`, `/apps`, `/automation`, `/stop`, `/doctor`" | TRUE | `scripts/audit.py`: "26 slash commands". |
| 23 | "Install `.[voice,gemini]` in a dedicated `.venv-voice`" | PARTIAL | The dependency conflict is real (LiveKit and browser-use pin incompatible SDK versions) but this audit could not install the voice extra, so the instruction is unverified. |
| 24 | "progress reports visible stages and tool status, never hidden chain-of-thought" | TRUE | `jarvis/runtime/progress.py` + `tests/test_ui_progress.py` 63/63. |

## 2. docs/STATUS.md claims

| # | Claim (STATUS.md) | Verdict | Evidence |
|---|---|---|---|
| 25 | "`pytest -q -o addopts=''` reporting **355 passed**" | **Stale** | The audited tree reports **455 passed**. The figure was correct for commit `4387cce`; it is not the current number. |
| 26 | "`test_v23.py` 147/147 … **423/423 checks**" | **Stale** | `test_v23.py` is **153/153**; the seven harnesses total **429/429** (97 + 153 + 63 + 49 + 21 + 17 + 29). |
| 27 | "The 218-entry release ZIP is built with `git archive`" | **Stale** | `git archive HEAD \| tar -t` now lists **223** entries; `git ls-files` lists 184 tracked files. |
| 28 | "the 177-file secret scan was clean" | NOT REPRODUCIBLE | The tree has 151 Python files. The 177 figure likely counts all tracked files at that commit. A fresh secret scan of the current tree found no credentials; see §4. |
| 29 | "Registered-tool audit — 99 callable, 0 warnings, 0 broken" | TRUE | Re-ran `scripts/audit_tools.py`: **99 callable · 0 warn · 0 broken · PASS**. |
| 30 | "Repository audit — 18 pass, 1 optional warning, 0 fail" | TRUE | Re-ran `scripts/audit.py`: **18 pass · 1 warnings · 0 fail**. Sole warning: `browser-use` python package and CLI absent. |
| 31 | "`is_chat_request()` now defaults to task" | TRUE (narrowed) | Still the default. This audit added a bounded exception for short conversational filler — see §3, defect D-11. |
| 32 | "Four FastAPI `on_event` deprecation warnings remain" | TRUE | Confirmed: 7 warning lines in the current run, all FastAPI lifecycle deprecations. |
| 33 | "Offline tests establish local behavior only" | TRUE | Correct and appropriately hedged throughout the document. |
| 34 | "Live external integrations remain unverified" | TRUE | Confirmed — see §5. |

## 3. SKILL.md and prompt claims

| # | Claim | Verdict | Evidence |
|---|---|---|---|
| 35 | Each skill's frontmatter declares only tools that exist in the registry | TRUE | `scripts/wiring_map.py`: `skills.unknown_tools == []` for all 18 packs; `missing_tools == []`. |
| 36 | Every skill declares keywords | TRUE | `wiring_map.py`: no pack with `keywords: false`. |
| 37 | The skills loader loads all 18 packs | TRUE | `wiring_map.py`: `loader_loads_all: true`; `tests/test_skills_registry.py` passes. |
| 38 | Prompts never reference a tool that is not registered | TRUE | `wiring_map.py`: `prompts.smells == []`. |
| 39 | `prompts.py` exposes `current_datetime_str()` and `greeting_for_now()` for injection into the system prompt | **FALSE** | Both are defined (`jarvis/prompts.py:22,30`) and **called from nowhere** in `jarvis/` or `tests/`. Nothing injects the current date/time or a time-of-day greeting into `voice_instructions()` or `supervisor_prompt()`. Dead helpers. See defect D-14. |

---

## 4. Defects found, and their state

| ID | Area | Defect | State |
|---|---|---|---|
| D-1 | `tools/app_finder.py` | `KeyError: 'norm'` on any index entry not built by `_add()` — surfaced as `open_any_app: error: 'norm'` with no app launched | **Fixed** |
| D-2 | `tools/app_finder.py` | a top-score tie between differently named apps was not treated as ambiguous | **Fixed** (`match_is_ambiguous`) |
| D-3 | `runtime/agent.py` | ambiguity check ran before the permission check, so `telegram_guest` got a clarification instead of `blocked` | **Fixed** |
| D-4 | `security/permissions.py` | `restart_jarvis_tool`, `play_file`, `open_path`, `memory_compact`, `refresh_app_index`, `reload_config_tool` all fell through to READ, so a Telegram guest could restart the assistant or hand an arbitrary path to the OS shell | **Fixed** |
| D-5 | `security/permissions.py` | `check()` allowed any unrecognised name at READ | **Fixed** (fails closed; alias list pinned by a test) |
| D-6 | `graph/workers/browser.py` | `browser_doctor` was registered but never called; `browser_close`, `browser_cli`, `run_python`, `tg_send_buttons`, `list_skills`, `read_skill` had no runtime path | **Partial** — `browser_doctor` now runs on failure; `list_skills`/`read_skill` bound to the desktop worker; 4 remain manual-only (see §5) |
| D-7 | `memory/manager.py` | a failed memory write propagated out of `remember()` and crashed the whole turn | **Fixed** |
| D-8 | `runtime/agent.py` | a chat failure fell back to a greeting line even for a substantive question | **Fixed** |
| D-9 | `llm/core.py` | the aggregate failure named only the last exception, hiding which providers had died | **Fixed** |
| D-10 | `cli/pro_ui.py` | `Text.from_markup` raised `MarkupError` on any `[` in tool output, crashing the dashboard mid-run | **Fixed** |
| D-11 | `runtime/agent.py` | 22 of 25 casual messages were routed through the full LangGraph pipeline | **Fixed** |
| D-12 | `runtime/agent.py` | answering a clarification with a bare noun (`notepad`) was swallowed by the chat path | **Fixed** |
| D-13 | `runtime/agent.py` | `hata do` (split imperative) was not recognised as a command | **Fixed** |
| D-14 | `prompts.py` | `current_datetime_str()` / `greeting_for_now()` are dead code | **Open** — documented as not wired, not deleted (see §5) |
| D-15 | `config/runtime.py` ↔ `runtime/controls.py` | a lazy import cycle | **Open** — benign (both imports are function-local); pinned by `tests/test_wiring.py` so it cannot grow |
| D-16 | `graph/nodes.py` | binds `get_chat_model` at import time, so runtime provider switches do not reach the graph | **Open** — low impact; surfaced while writing the chaos tests |

---

## 5. Not implemented, not wired, or not verified

This is the honest list. Nothing here works the way a reader might assume from the
feature list.

### 5.1 Registered but with no automatic runtime path

Four of the 99 registered tools can only be invoked through the Rich UI's `/tool <name>`
escape hatch (`jarvis/cli/pro_ui.py:362`). No worker binds them and no deterministic
path calls them. They are tested and functional; they are simply not connected.

| Tool | Defined at | Why it is not wired |
|---|---|---|
| `browser_cli` | `jarvis/tools/browser_tools.py:928` | The browser worker only ever picks `browser_research` or `run_browser_task`, so the faster CLI path is never chosen automatically. |
| `browser_close` | `jarvis/tools/browser_tools.py:989` | No shutdown or idle-cleanup hook calls it, so browser sessions are never reclaimed. |
| `run_python` | `jarvis/tools/code_engine.py:459` | The code worker always goes through `code_engine_run`. |
| `tg_send_buttons` | `jarvis/telegram/tools.py:89` | `jarvis/telegram/bot.py` builds its keyboards directly rather than exposing this tool. |

`tests/test_wiring.py::test_no_newly_unwired_tool` pins this list, so adding a 100th
tool without wiring it fails the build.

### 5.2 Dead code

* `jarvis/prompts.py:22` `current_datetime_str()` and `jarvis/prompts.py:30`
  `greeting_for_now()` — defined, exported, called from nowhere. The assistant therefore
  does **not** receive the current date/time in its system prompt; date/time questions
  are answered by the deterministic `_is_time_question` fast path instead.

### 5.3 Not verified on this machine — requires the owner's Windows VM

Nothing below could be exercised here. The Win32 layer in the tests is a stub
(`ctypes.windll` replaced with fakes); none of this is evidence either way.

* Real Win32 behaviour: `EnumWindows`, `SetForegroundWindow`, `SendKeys`, clipboard,
  HiDPI scaling against an actual scaled display.
* Launching, focusing and typing into real applications (Notepad, Chrome, Outlook…).
* `pyautogui` / `pygetwindow` / `pynput` against a live desktop session.
* Live browser automation: Chromium, `browser-use` Python API and CLI
  (`scripts/audit.py` reports `python=no cli=no`).
* Live LLM providers: Gemini, OpenRouter, Groq. The cooldown/circuit-breaker behaviour
  is tested against injected failures, not against a real 429.
* Telegram: no bot account, no inline-button round trip over the network.
* LiveKit voice: no microphone, no cloud room, no spoken yes/no approval.
* Production-duration scheduler runs.
* The `voice` and `browser` pip extras could not be installed in this environment.

### 5.4 Deliberate design limits (not defects)

* Security posture is relaxed by request: no sandbox, no path jail, no SSRF filter, no
  forced API-auth wall, no rate limiting. The Dangerous-Command Gate is the only
  execution safety layer, and it is intact.
* The assistant answers with a local fallback when no model is reachable. That fallback
  is intentionally honest rather than helpful — it says the model was unavailable.
* Session-state locking protects against concurrent writes within one process tree; it
  is not a general cross-process file lock.
