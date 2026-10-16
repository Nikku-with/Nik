# NikhilAI features, requirements, and environment controls

**Project:** NikhilAI / Jarvis — Nikhil Mehra  
**Snapshot date:** 2026-10-14 (Asia/Calcutta)  
**Source tree:** `NikhilAI_Jarvis/`

This is the operator handoff for the packaged source. It describes what is present, what the runtime needs, the main environment controls, and what remains unverified or incomplete. It does not claim live integrations or tests that were not run.

## Delivery and verification boundary

The final code/test commit `4387cce` passed `pytest -q -o addopts=''` (355 tests), `ruff check .`, `python -m compileall -q jarvis tests`, and all seven standalone acceptance harnesses (423/423 checks). The final `git archive` delivery ZIP was also fresh-extracted and verified with the full suite and compileall. Four FastAPI lifecycle deprecation warnings remain. Live cloud models, Telegram account, LiveKit room/microphone, visible desktop, Chromium profile, and production-duration scheduler have not been established by offline/fake tests.

## Feature inventory

| Area | Delivered in source | Evidence / limit |
|---|---|---|
| Shared agent and graph | Shared runner for CLI, API/web, Telegram, LiveKit voice, and scheduler; routed specialist workers; requirement/evidence state; bounded critic/refine flow; per-worker exception isolation. | `tests/test_part_a_regressions.py`, `tests/test_worker_retry.py`, `tests/test_graph_requirements.py`, and `tests/test_part_b_agent_graph.py` are included in the final 355-passed suite. |
| Idempotency and honest outcomes | Action ledger/replay protection, evidence-based success checks, partial-failure reporting, and no silent text-only fallback for tool-required work. | `tests/test_tool_invariant.py`, `tests/test_worker_retry.py`, and `tests/test_graph_requirements.py` pass within the final full-suite run. |
| Dangerous-Command Gate | Human-only approvals for classified risky operations, bound to command/context and resolved via owner-controlled surfaces; `risky`, `all`, and `off` modes. Mouse/keyboard controls do not bypass surface permissions or the Gate. | `tests/test_gate.py`, `tests/test_gate_surfaces.py`, `tests/test_api_gate.py`, `tests/test_telegram_gate.py`, and `tests/test_code_engine_regressions.py` pass within the final full-suite run. |
| Desktop and browser | Typed observe/act/verify desktop controls, bounded browser tasks, progress, per-session/per-site isolation, and owner-default-on mouse/keyboard environment switches. | `tests/test_desktop_tools.py`, `tests/test_desktop_advanced_tools.py`, `tests/test_browser_supervision.py`, and `tests/test_capability_matrix.py` pass in the final suite. Live display/browser execution is not claimed. |
| Code and process tools | Code generation/execution, process-group management, background-process inspection/control, and explicit timeouts. | `tests/test_process_manager.py`, `tests/test_process_manager_regressions.py`, and the new `tests/test_code_engine_regressions.py` pass in the final suite. |
| Files and reports | Indexed search, typed readers (where optional libraries are installed), verified writes, notes, and reversible file-manager operations. | Earlier coverage includes `tests/test_file_write_verification.py`, `tests/test_verified_file_save.py`, and `tests/test_file_undo.py`. `save_as` path-containment remains **BLOCKED** (see below). |
| Memory and schedules | Owner/guest memory separation, consent-aware memory actions, forget/index cleanup, atomic local session state, timezone-aware scheduling, unique run IDs, missed-run coalescing, and Gate checks at execution. | Prior coverage includes `tests/test_advanced_memory.py`, `tests/test_memory_consent_surfaces.py`, `tests/test_session_state.py`, and `tests/test_automation_runtime.py`. No live account or production scheduler run is claimed. |
| Progress, latency, and CLI | Shared progress events, one-call conversational fast path, bounded model/tool timeouts, and sanitized CLI `/trace` spans. | Earlier tests include `tests/test_performance_trace.py`, `tests/test_time_and_chat.py`, and `tests/test_ui_progress.py`. Synthetic timing is not live-provider latency or cost data. |
| Skills | The catalog contains the ten requested workflows (`task_verification`, `deep_research`, `file_management`, `terminal_ops`, `project_builder`, `desktop_browser_ops`, `data_extraction_and_reports`, `automation_and_scheduling`, `memory_management`, `self_review`) plus existing specialized packs. | `tests/test_skills_registry.py` loads the full 18-pack catalog and verifies all declared tools against the registry; it passed as part of the final 355-test suite. |

### Part D completion boundary

Part D is not represented as fully accepted merely because related tools exist. The following items remain partial or need end-to-end evidence:

| Part D item | Current state and remaining acceptance work |
|---|---|
| 1. Typed Plan → Act → Verify with crash resume | Requirements/evidence tracking and bounded refinement exist. A typed dependency-aware `Step{id, goal, tool_hint, depends_on, verify}` plan persisted/resumed at the failed step in SQLite is **not demonstrated as complete**. |
| 2. Multi-agent research | Parallel web/page collection and browser isolation exist. The full 3–6 sub-question orchestration, structured claim/quote/date/confidence outputs, independent reviewer, conflict reconciliation, and generated Markdown plus `sources.json` are **not demonstrated end to end**. |
| 3. Persistent terminal | One-shot command execution and background process controls exist. A persistent shell per conversation with durable cwd/env, streamed output, job lifecycle, and full-log artifacts is **not demonstrated as complete**. |
| 4. Scalable file manager | Index/search, readers, writes, and undo exist. Bulk dry-run previews, organize-by-rule, hash-based duplicate detection, and trash/undo lifecycle are **not all demonstrated**. |
| 5. Observable/idempotent tools | Idempotency/evidence and sanitized `/trace` exist. A complete per-call `call_id`/args-hash/duration/result stream persisted to `workspace/trace.jsonl`, with session timeline UI, is **not demonstrated as complete**. |
| 6. Typed contracts | Typed DSPy components exist in the codebase, but full Pydantic coverage for router/planner/extractor/critic and runtime parsing has not been re-audited for this delivery. |
| 7. Memory lifecycle | Consent, provenance, recall, and forget behavior exist. Full time-decay/procedure lifecycle plus post-task skill offer is **not demonstrated end to end**. |
| 8. Desktop/browser reliability | Observe–act–verify, budgets, and failure isolation are implemented in offline-oriented paths. Live target-window/browser reliability remains unverified. |
| 9. Latency/cost | Fast paths, caching/timeouts, and synthetic timing exist. Live first-token and provider-cost measurements are unavailable; no bill estimate is claimed. |
| 10. Self-improvement | Proposal-only diff generation exists. The weekly trace-analysis and DSPy-trainset proposal job is **not demonstrated**. |

These are remaining acceptance items, not claims that the overall “10/10” definition has been met. Revisit them only in the prompt's order and add behavioral tests before claiming completion.

## Explicitly blocked item and accepted scope

- **BLOCKED — `save_as` containment assertion:** implementing a path jail/containment guard conflicts with the owner's explicit relaxed-VM scope and prohibition on path jails. It remains unchanged; do not report it as fixed.
- Security posture stays as requested: the Dangerous-Command Gate is the only new safety layer. No sandbox, path jail, SSRF filter, rate limit, forced API-auth wall, CORS policy, or broad permission rewrite is added. Existing permissions, guest restrictions, and Gate behavior remain authoritative.
- The API remains optionally authenticated; a non-loopback bind without an API token warns rather than being blocked. Use the private VM as intended and configure a token/HTTPS when exposing the API.

## Runtime requirements

### Core

- Python **3.10 or newer**.
- Install the core project and only the extras needed for the surfaces you will run. Typical development/operator setup:

```bash
python -m pip install -e '.[dev,api,telegram,browser,gemini,desktop]'
```

- A local/offline demo can run without cloud credentials; live model answers need a configured provider and its SDK/key.
- Windows, macOS, and Linux are supported by different native adapters. Real desktop control requires an interactive display/session; a headless VM returns an unavailable result rather than evidence of success.

### Optional integrations

| Capability | Extra / requirement |
|---|---|
| HTTP API and web console | `api` extra; FastAPI/Uvicorn. |
| Telegram | `telegram` extra plus bot token and owner ID. |
| Browser agent | `browser` extra plus supported browser runtime/profile; real-site use is not part of offline verification. |
| Gemini / LiveKit voice | `gemini` and `voice` extras, provider/LiveKit credentials, audio hardware or a configured cloud room. Keep voice and Browser-Use in separate virtual environments if their SDK constraints conflict. |
| Desktop OCR/input | Optional desktop packages improve coverage; a real display is required. Tesseract plus `pytesseract` enables OCR where supported. |
| Document readers | PDF/DOCX/XLSX/PPTX and image readers use optional libraries listed in project requirements; unsupported/missing readers must be reported honestly. |

Credentials are environment-only. Keep real values in a local untracked `.env` or the deployment environment; never put secrets into this document, source, tests, logs, or ZIP.

## Environment controls

`.env.example` is the source for the complete variable list. Defaults below reflect that template; environment-specific runtime settings may override them.

| Control | Default / values | Purpose |
|---|---|---|
| `OWNER_NAME`, `OWNER_ID`, `OWNER_NAME_HINDI` | Template uses a placeholder name; ID empty. | Owner identity and owner-only control surfaces. Set private values locally. |
| `CITY`, `TIMEZONE` | `Kota`, `Asia/Kolkata`. Invalid IANA zones fall back to Kolkata/IST, including fixed UTC+05:30 fallback if system tzdata is absent. | Weather/location defaults and explicit clock/schedule calculations. |
| `LANGUAGE`, `RESPONSE_STYLE`, `ASSISTANT_PERSONALITY` | `hinglish`, `concise`, `smart,friendly,concise,agentic`. | User-facing response language/style/persona. |
| `DEFAULT_MODE` | `text`. | Default interface preference only; does not start a bot, microphone, or LiveKit session. |
| `BRAIN_PROVIDER`, `BRAIN_MODEL` | Empty/auto selection in template. | Select or pin the primary provider/model. Explicit provider choice is not silently overridden. |
| `GEMINI_API_KEY`, `OPENROUTER_API_KEY`, `GROQ_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `XAI_API_KEY` | Empty. | Provider credentials. Configure only the provider(s) used; never copy real values into the archive. |
| `FAST_MODEL`, `PLANNER_MODEL`, `CODE_MODEL`, `RESEARCH_MODEL`, `EXTRACT_MODEL`, `VISION_MODEL`, `BROWSER_MODEL`, `CRITIC_MODEL` | Optional/empty. | Per-role model overrides. |
| `GOOGLE_SEARCH_API_KEY`, `SEARCH_ENGINE_ID`, `BING_SEARCH_API_KEY`, `BRAVE_API_KEY`, `SEARX_URL`, `DUCKDUCKGO_ENABLED` | Keys/URL empty; keyless search enabled. | Search providers. Google Search does not reuse a Gemini-only key unless explicitly configured. |
| `BROWSER_ENABLED`, `SHELL_ENABLED`, `DESKTOP_ENABLED`, `FILE_TOOLS_ENABLED`, `RESEARCH_ENABLED`, `MEMORY_ENABLED`, `AUTOMATION_ENABLED` | `true`. | Feature availability switches. A feature switch does not grant surface permissions. |
| `DESKTOP_MOUSE_ENABLED` | **`true`**. | Owner-default-on pointer/mouse tool availability; set false/0 to disable mouse actions. |
| `DESKTOP_KEYBOARD_ENABLED` | **`true`**. | Owner-default-on keyboard/typing/hotkey tool availability; set false/0 to disable keyboard actions. |
| Surface permissions and Gate | Existing per-surface permission policy; `JARVIS_EXEC_APPROVAL=risky` in the template (`risky`, `all`, `off`). | Mouse/keyboard defaults do **not** alter owner/guest permissions, surface restrictions, or the Dangerous-Command Gate. `off` disables the Gate as explicitly configured. |
| `ALLOW_SHELL`, `ALLOW_FILE_DELETE` | `true` in template. | Existing action-level enablement; does not bypass the Gate or surface permissions. |
| `MAX_ITERATIONS`, `MAX_PROCESSES`, `SHELL_TIMEOUT_S`, `CODE_TIMEOUT_S` | `3`, `12`, `120`, `90`. | Refinement, process, shell, and script limits. |
| `BROWSER_MODE`, `BROWSER_HEADLESS`, `BROWSER_MAX_STEPS`, `BROWSER_TIMEOUT_S`, `BROWSER_MAX_PARALLEL` | `auto`, `true`, `60`, `420`, `3`. | Browser runtime mode, headless setting, step/time/parallel budgets. |
| `WORKSPACE_DIR`, `APP_MEMORY_FILE`, `FILE_SEARCH_ROOTS`, `BROWSER_PROFILE_DIR` | Workspace-derived or empty/home defaults. | Runtime data and searchable roots. Runtime personal data/profile folders are excluded from the delivery ZIP. |
| `MAX_FILE_READ_CHARS`, `FILE_SEARCH_MAX_RESULTS`, `CONTENT_SEARCH_MAX_FILES` | `20000`, `15`, `2000`. | File-reader/search limits. |
| `API_HOST`, `API_PORT` | `127.0.0.1`, `8000`. | API bind. Non-loopback without a token warns but is not blocked. |
| `NIKHILAI_API_TOKEN` / `API_AUTH_TOKEN` | Optional. | Optional API bearer/session-token protection; not a forced auth wall. |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_OWNER_ID`, `TELEGRAM_ALLOWED_USERS`, `TELEGRAM_ALLOWED_CHAT_IDS` | Empty. | Telegram runtime identity/allowlists; store real IDs and token privately. |
| `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, `VOICE_ENABLED` | Credentials empty; `VOICE_ENABLED=true`. | Optional LiveKit voice surface. Credential presence does not prove a live connection. |
| `LOG_LEVEL`, `SHOW_REASONING`, `USE_DSPY`, `DSPY_OPTIMIZED` | `INFO`, `true`, `true`, `true`. | Logging level, visible structured summary, and DSPy options. Hidden chain-of-thought is not exposed. |

## Archive hygiene

The delivery archive should contain source, tests, skills, and documentation, but must exclude `.env`, `.git`, workspace/runtime databases, browser profiles, local logs, virtual environments, bytecode, caches, and generated build artifacts. The ZIP must be created **beside** the source tree, not inside it, so it cannot archive itself. No installation, test suite, lint, compile, audit, or live service is required to create the source archive.
