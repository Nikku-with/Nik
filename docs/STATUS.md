# NikhilAI status, capabilities, and audit evidence

> **Independent audit addendum — 2026-10-16.** The figures below were recorded by the
> builder at commit `4387cce` on 2026-10-14 and are **stale**. Re-measured on the audited
> tree:
>
> | Figure | Recorded here | Re-measured |
> |---|---|---|
> | `pytest -o addopts=""` | 355 passed | **455 passed** |
> | `test_v23.py` | 147/147 | **153/153** |
> | Seven harnesses total | 423/423 | **429/429** |
> | `git archive` ZIP entries | 218 | **225** |
>
> `ruff check .`, `python -m compileall -q jarvis tests`, 99/99 registered tools and 18/18
> skill packs were all re-confirmed. Note that `pytest -q` prints no pass/fail count,
> because `pyproject.toml` sets `addopts = "-q"`; use `pytest -o addopts=""` when a count
> is needed.
>
> A line-by-line audit of every claim in this document, the README and the skill files is
> in [CLAIMS_AUDIT.md](CLAIMS_AUDIT.md). The **Not implemented / not wired / not verified**
> list is in [CLAIMS_AUDIT.md §5](CLAIMS_AUDIT.md#5-not-implemented-not-wired-or-not-verified)
> — read it before making a release claim.

**Updated:** 2026-10-14 (Asia/Kolkata)

**Snapshot:** Final code/test commit `4387cce` was checked on 2026-10-14: `pytest -q` passed, with the count-confirming run at `pytest -q -o addopts=''` reporting **355 passed**; Ruff, compileall, and all seven standalone acceptance harnesses passed. The final `git archive` ZIP was fresh-extracted, and its full suite and compileall passed as recorded below. Four FastAPI lifecycle deprecation warnings remain. Live external integrations remain unverified.

This document consolidates the former root-level `FINAL_STATUS.md`, `CAPABILITIES.md`, and `TOOL_AUDIT.md`. Offline/fake tests establish local behavior only; they do not prove that a cloud provider, Telegram account, microphone, desktop session, browser profile, or production scheduler is working.

## Final delivery verification

| Check | Final result |
|---|---|
| Task/chat routing | `is_chat_request()` now defaults to task. Two classifier tests cover **59 task phrases** and **83 clearly conversational phrases**, including the four requested Hinglish desktop/file examples; both tests passed. |
| Offline graph regression | `python tests/test_offline.py` — **97/97**. The notes end-to-end assertion now requires a verified answer, `status=done`, no unresolved ledger entries, and every requirement marked done. The planner's response-only “short summary” step is no longer treated as an unfulfilled tool action. |
| Tool invariant | `python tests/test_tool_invariant.py` — **49/49**. The two run-agent integration checks pass with 2 fake tool records, `execution_required=True`, an honest failed status, and no real host UI action. The five graph source-string checks were replaced by behavioral tests. |
| Standalone harnesses | `test_offline.py` 97/97; `test_v23.py` 147/147; `test_ui_progress.py` 63/63; `test_tool_invariant.py` 49/49; `test_cli_routing.py` 21/21; `test_voice_cli.py` 17/17; `test_llm_bind.py` 29/29 — **423/423 checks**. `tests/test_standalone_harnesses.py` runs all seven under pytest in isolated offline temp workspaces. |
| Lint | `ruff check . --fix` and follow-up `ruff check .` — passed cleanly. |
| Full suite | `pytest -q` exited successfully. Count-confirming `pytest -q -o addopts=''` — **355 passed**, 4 FastAPI `on_event` deprecation warnings. |
| Compile | `python -m compileall -q jarvis tests` — passed. |
| Fresh archive extraction | The final delivery ZIP is generated with `git archive`, extracted into a clean temporary directory, and run through `pytest -q -o addopts=''` (**355 passed**, 4 FastAPI deprecation warnings) plus `python -m compileall -q jarvis tests` (passed). |
| Archive hygiene | The 218-entry release ZIP is built with `git archive`; generated/ignored `nikhilai.egg-info`, `.env`, `.git`, and local `workspace` are omitted. The ZIP listing confirms none of those entries are present. |

See [features, requirements, and environment controls](FEATURES_REQUIREMENTS_ENVIRONMENT.md) for feature scope and accepted blockers. Live cloud, Telegram, LiveKit, desktop, browser, and production-scheduler integrations remain unverified.

## Earlier recorded verification snapshot

The following focused results are historical records from earlier groups. The final full-suite result above is the current aggregate for the verified code/test commit.

| Check | Historical evidence |
|---|---|
| Focused regression subset | `pytest -q tests/test_time_and_chat.py tests/test_advanced_memory.py tests/test_graph_requirements.py tests/test_worker_retry.py tests/test_desktop_tools.py tests/test_desktop_advanced_tools.py tests/test_app_finder.py tests/test_gate.py tests/test_gate_surfaces.py tests/test_capability_matrix.py tests/test_part_a_regressions.py tests/test_part_b_agent_graph.py tests/test_runtime_audit.py tests/test_tool_invariant.py` — 185 tests passed in the recorded focused run. This is not a full-suite run. |
| Skills registry | `pytest -q tests/test_skills_registry.py` — 3 passed after adding the four new packs; `ruff check jarvis/skills tests/test_skills_registry.py --output-format concise` passed. |
| Conversation fast path | `pytest -q tests/test_time_and_chat.py` — 32 passed, including recent-question cooldown, configured language/personality and mood instruction, and “mat poocho”; Ruff passed for the file. |
| Advanced memory/consent | `pytest -q tests/test_advanced_memory.py tests/test_memory_consent_surfaces.py` — 19 passed; scoped Ruff and `git diff --check` passed. FastAPI lifecycle deprecation warnings remain. |
| Focused current regression | `pytest -q tests/test_time_and_chat.py tests/test_advanced_memory.py tests/test_memory_consent_surfaces.py tests/test_performance_trace.py tests/test_runtime_audit.py tests/test_api_gate.py tests/test_telegram_live.py tests/test_telegram_gate.py tests/test_docs_layout.py` — 96 passed; scoped Ruff, `py_compile`, and `git diff --check` passed. No live Telegram/API connection was made. |
| Performance/trace | `pytest -q tests/test_performance_trace.py` — 4 passed; `python tests/test_ui_progress.py` — 63/63. Fake latency benchmark: one-call chat 6.49 ms median vs a simulated four-serial-stage baseline 24.89 ms (73.9% lower; 1 vs 4 fake model calls, 6 ms injected per call). This is not a live-provider or full-graph latency claim. |
| Verified file-save workflow | `pytest -q tests/test_file_write_verification.py tests/test_verified_file_save.py tests/test_file_undo.py tests/test_gate_surfaces.py tests/test_part_b_agent_graph.py tests/test_desktop_tools.py tests/test_desktop_advanced_tools.py tests/test_tool_invariant.py` — 52 passed; scoped Ruff, `py_compile`, and diff check passed. Actual temporary files were read back exactly; mismatch rolled back, incomplete requests created nothing, and the desktop worker exposed only independently verified artifacts. |
| Mode/model/provider/Gate surfaces | `pytest -q tests/test_control_surfaces.py tests/test_runtime_audit.py tests/test_telegram_gate.py tests/test_gate.py tests/test_gate_surfaces.py tests/test_configuration_safety.py` — 62 passed; scoped Ruff, `py_compile`, and `git diff --check` passed. CLI and fake Telegram command handlers report saved preferences separately from active interfaces/routes, surface missing provider keys and environment Gate overrides, and never claim connectivity was tested. FastAPI lifecycle deprecation warnings were emitted by the audit test. |
| Documentation/registry | `pytest -q tests/test_docs_layout.py tests/test_skills_registry.py` — 6 passed; `python tests/test_voice_cli.py` — 17/17; scoped Ruff and `git diff --check` passed. |
| Full test suite | `pytest -q` — 286/286 passed after the implementation and lint-fix commits. The run emitted four FastAPI `on_event` deprecation warnings; there were no test failures. |
| Full lint/syntax/hygiene | `ruff check .` — all checks passed; `python -m compileall -q jarvis tests` and `git diff --check` passed. |
| Repository audit | `python scripts/audit.py` — 18 pass, 1 optional warning, 0 fail; 134 Python files compiled, 88 modules imported, and the 177-file secret scan was clean. Sole warning: optional `browser-use` Python package and CLI are absent (`tools=6`); live browser integration is not established. |
| Registered-tool audit | `python scripts/audit_tools.py` — 99 callable, 0 warnings, 0 broken. Side-effecting tools were schema-checked/skipped; safe calls used offline/demo configuration and fakes. |
| CLI/progress offline harnesses | `python tests/test_voice_cli.py` — 17/17; `python tests/test_ui_progress.py` — 63/63. These verify routing and fake/local behavior, not a live microphone or Telegram account. |
| Live external integrations | Not exercised; see [Offline/live boundaries](#offlinelive-boundaries). |

### Round 2 progress

| Group | Status and evidence |
|---|---|
| Parts 1.1–1.5 | Done (commit `206ee68`): requirement/refinement evidence, split graph/worker behavior, Dangerous-Command Gate, desktop tools, and owner/surface permissions. Relevant graph, retry, regression, Gate, and capability tests passed; the final 286-test suite also passed. |
| Part 1.6 | Done: added `task_verification`, `terminal_ops`, `project_builder`, and `self_review` real-tool skill packs. `tests/test_skills_registry.py` passes and validates tool names against the registry. Commit: `a201e3d`. |
| Part 1.7 | Done: docs reorganized, duplicate status/capability/tool-audit pages consolidated, README links updated, and `CHANGELOG.md` added. Verified by `tests/test_docs_layout.py` (3 of the 6 combined documentation/registry tests), `python tests/test_voice_cli.py` (17/17), scoped Ruff, and `git diff --check`. |
| Parts 1.8–1.9 | Done (commit `731d11b`): advanced desktop controls, browser-aware app matching, provider/config safety, and a hermetic test workspace; targeted and final full-suite checks passed. |
| 16.1 | Done (commit `0b8a770`): deterministic date/time answers use the configured local clock/timezone, with Kota/Asia Kolkata defaults; covered by `tests/test_time_and_chat.py` and `tests/test_offline.py`. |
| 16.2 | Done (commit `0b8a770`): `tests/test_time_and_chat.py` covers the one-call fake-model path, recent-question cooldown (including the three-reply window), configured language/personality and mood instruction, and a “mat poocho” no-follow-up prompt. All 32 cases passed; Ruff passed for the file. |
| 16.3 | Done (commit `4e39454`): `tests/test_advanced_memory.py` covers provenance, contradiction, restart/cross-surface recall, sensitivity, context budget, and forget-by-description with FTS cleanup. `tests/test_memory_consent_surfaces.py` covers exact-ID API confirm/cancel, pending-list visibility, Telegram `/memory pending|confirm|cancel`, owner-only consent, punctuation-tolerant chat confirmation, and refusal to guess when multiple items are pending. Nineteen tests passed; only fake/local API and Telegram handlers were used. |
| 16.4 | Done (commit `0a06536`): CLI `/trace [limit]` shows last-run wall time and model/tool spans from sanitized progress metadata only; prompts, arguments, outputs, and reasoning text are excluded. Four trace tests and the 63-check UI/progress harness passed. Fake latency benchmark is documented above; no live provider timing is claimed. |
| 16.5 | Done (file verification commit `03f3062`; worker integration in `206ee68`): `write_file` re-reads and compares UTF-8 text before success, rolls back on mismatch, and reports an already-identical file as verified without another write. Offline/demo file requests require an explicit path and content; `desktop_worker` independently verifies successful `write_file` calls and returns real paths as artifacts. Missing content or a false success claim cannot create/claim an artifact. The 52-test save/undo/Gate/desktop selection passed using temporary files and fake/offline tools only. |
| 16.6 | Done (commit `6a85794`): CLI and Telegram expose consistent mode, model, provider, and Dangerous-Command Gate controls. `/mode` is labeled as a default preference and does not start another interface; model/provider messages distinguish configured routes and key availability from live connectivity; `/gate` and `/status` show the effective Gate mode/source, reject owner-only changes from guests, and respect explicit environment overrides. Generic `/settings` changes for model/provider/Gate use the same truthful controls. The 62-test fake/local surface and Gate selection passed; no provider, Telegram account, or shell command was contacted/executed by these controls. |

## Capability notes

### Shared request execution and evidence

CLI, API/web, Telegram, LiveKit voice, and scheduled jobs use the shared `jarvis.runtime.agent.run_agent` path. The graph plans numbered requirements, routes worker groups, gathers tool evidence, synthesizes, verifies, and can refine within bounded budgets. Worker failures are represented as failures so independent work can continue. A text-only model is not silently substituted when a task requires tool binding; binding failures are surfaced. Model narration by itself is not proof that a side effect succeeded.

Progress surfaces visible stages and tool start/end/status, not hidden chain-of-thought. Deterministic fast paths and role-specific timeouts are used where appropriate. `/trace` coverage is tested and a synthetic one-call fast-path comparison is recorded above; a live-provider or full-graph before/after latency benchmark has not been run.

### Dangerous-Command Gate

`JARVIS_EXEC_APPROVAL` modes are `risky`, `all`, and `off`:

- `risky`: gate classified dangerous/sensitive operations; ordinary safe commands do not prompt.
- `all`: request owner approval for every command.
- `off`: disable the Gate and issue one startup warning.

Approvals are server-side, single-use, expiring, and bound to the exact operation, working directory, owner, and surface. Missing/late approval, timeout, wrong identity, or an unclear decision fails closed. The model cannot issue its own approval. The CLI, API/web, Telegram, voice, and automation paths resolve approval on owner-controlled surfaces. The supported VM/operator posture does not add a sandbox, path jail, SSRF filter, forced API-auth wall, rate limit, or broad permission rewrite.

### Desktop and file operations

Desktop tools are typed and follow observe → act → verify: fresh screenshot/observation IDs, screenshot-derived coordinates, HiDPI conversion, post-action observation, bounded retry, and human approval for destructive/external actions. A missing display/backend yields an explicit unavailable result. Clipboard inspection is an explicit operation.

File-manager undo uses a bounded snapshot and refuses to overwrite a later edit; removals/replacements remain subject to the Gate. The app-launch memory is for open-app lookup only and should be updated only after a verified launch. The live display/application path is not established by fake desktop tests.

### Browser, process, and automation workflows

Browser supervision uses bounded budgets, progress events, repeated-state handling, registry-backed delegated tools, and per-site/session failure isolation. Parallel research normalizes results before reporting. Process controls distinguish safe inspection from state-changing termination; process stop remains gated. Scheduled tasks preserve origin surface and recheck Gate requirements at execution. These claims describe code paths and offline/fake coverage, not live sites or long-running deployment runs.

### Memory, preferences, and self-improvement

Owner memory is intended to be shared across owner surfaces while guest memory is isolated. Session state uses locked, atomic local writes and reports corruption. Advanced-memory tests cover provenance, contradictions, restart/cross-surface recall, sensitivity, context budgets, forget-by-description, and the current API/chat/Telegram consent flows. Tests use local stores and fakes; they do not establish live account or cloud integration. Conversation tests cover follow-up cooldown, configured language/personality, mood guidance, and “mat poocho” behavior.

`propose_self_improvement` stores a unified-diff proposal for human review and does not edit or execute Jarvis source. Provider latency/cost cannot be measured accurately without live usage telemetry; don't invent a cost estimate.

### Registered skills

Skills are workflow instructions under `jarvis/skills/catalog/<name>/SKILL.md`. The delivery tree contains eighteen packs, including all ten requested workflow names. The four additional requested packs are `deep_research`, `desktop_browser_ops`, `data_extraction_and_reports`, and `automation_and_scheduling`. `tests/test_skills_registry.py` loads all 18 packs and validates declared tool names against the registry; it passed in the final 355-test suite. That registry check establishes metadata/tool-name consistency, not live workflow execution.

## Surface behavior matrix

“Fake succeeds” means a local test double or deterministic offline path, not a live integration. Policy refusals are valid outcomes and must not be described as successful execution.

| Behavior | CLI owner | HTTP/API | Telegram owner | LiveKit voice owner | Scheduled run |
|---|---|---|---|---|---|
| Safe chat/date | Shared runner answer with CLI progress | Shared answer/progress events | Shared answer/message progress | Shared answer/status via the shared runner | Shared runner with run ID/progress |
| Dangerous operation | Owner prompt or cancel; no action on refusal | Owner approval UI or refusal | Owner inline approval or cancel | Clear spoken yes/no; unclear/timeout cancels | Origin-surface Gate checked at execution |
| Desktop app launch | Fake verified result; runtime depends on display/policy | Friendly policy refusal where SYSTEM capability is unavailable | Fake owner result; runtime/policy applies | Friendly policy refusal where SYSTEM capability is unavailable | Policy/runtime refusal is reported |
| Browser delegated safe tool | Fake registry-backed success | Friendly refusal where EXECUTE capability is unavailable | Fake registry-backed success | Fake registry-backed success | Fake registry-backed success |
| Live hardware/account/browser | Not established by offline tests | Configuration/policy errors should be explicit | No real bot account exercised | No real mic/cloud session exercised | No production-duration scheduler run |

Related behavioral coverage includes `tests/test_capability_matrix.py`, `tests/test_automation_runtime.py`, `tests/test_api_gate.py`, `tests/test_telegram_gate.py`, `tests/test_livekit_voice.py`, and the dedicated desktop/browser suites. Consult test results from the current tree before making a release claim.

## Historical verification baseline

The prior committed baseline at `348838a` had a release report that recorded the following. Those figures belong to that earlier snapshot; use the current verification table above for evidence on this Round 2 tree.

| Prior baseline record | Reported result |
|---|---|
| Full pytest | 185/185 passed |
| Hermetic offline acceptance | 96/96; temporary workspace and mocked search/fetch/weather; zero socket/DNS attempts |
| Repository/tool checks | Ruff, compile/repository audit reported passing; registered-tool audit recorded 91 callable, 0 warnings, 0 broken |
| Release ZIP | 131 tracked file entries; fresh extraction and checks reported passing; Git tree was reported clean at that historical point |

The current `scripts/audit_tools.py` result is a mock-backed callability/schema audit (99 callable, 0 warnings, 0 broken). Side-effecting tools were schema-checked/skipped; search/fetch/weather used deterministic fakes; no live provider, site, or account was contacted. The audit blocks accidental socket/DNS access. Re-run `python scripts/audit_tools.py` to reproduce. To save the detailed table, pass an explicit report path, for example `python scripts/audit_tools.py --report /tmp/nikhilai-tool-audit.md` (choose a suitable temporary path on Windows).

## Offline/live boundaries

The current audit does not establish successful live use of Gemini/OpenRouter/Groq, a Telegram bot account, a LiveKit cloud room or microphone, a visible desktop session, Chromium/browser tasks against a live site, or a production-duration scheduler. Those require credentials, optional dependencies, hardware, display/profile configuration, and target-environment smoke tests. The offline tests should use fake LLM/tools/desktop/Telegram and no real credentials or public network.

## Accepted risks and operating notes

- This is a local/VM-first operator with shell, file, desktop, and browser capabilities—not a sandbox. The user explicitly requested a relaxed VM posture; the human-only Dangerous-Command Gate is the new execution safety layer.
- A non-loopback API bind without `API_AUTH_TOKEN` is allowed but warns at startup. Configure a token and HTTPS when exposing it beyond loopback.
- Session-state locking is local-process protection, not a general cross-process file lock; multiple processes sharing one workspace remain a risk.
- Credentials belong in environment configuration, never source files, tests, logs, prompts, or committed reports. Search credentials are distinct from Gemini credentials unless explicitly configured.
- FastAPI lifecycle-hook deprecation warnings were recorded in the prior test run; check current output before claiming they remain or are resolved.

## History and related documents

- Part A commits `5c83247` and `37bba81` are intended to remain in history.
- System structure: [Architecture](ARCHITECTURE.md).
- Setup and optional integrations: [Setup](SETUP_APIS.md).
- Features, requirements, environment controls, and remaining blockers: [Operator handoff](FEATURES_REQUIREMENTS_ENVIRONMENT.md).
- Project changes: [Changelog](../CHANGELOG.md).
