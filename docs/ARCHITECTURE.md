# Architecture

NikhilAI uses one asynchronous request runner, one LangGraph workflow, and a registered tool catalog across the CLI, HTTP/web UI, Telegram, LiveKit voice, and scheduled tasks. Surface adapters provide request identity, progress rendering, and delivery; they do not keep separate agent brains.

## Request flow

```text
CLI / API+web / Telegram / voice / scheduler
                    │
                    ▼
      jarvis.runtime.agent.run_agent
         ├── deterministic fast paths
         ├── session + owner/guest context
         ├── visible progress event stream
         └── LangGraph supervisor
                load → recall → route → plan
                → parallel workers → synthesize
                → verify → critic → bounded refine
                → persist → respond
```

Workers are selected from registered groups (desktop, runtime, research/web, browser, code, memory, skills, automation, and system operations). `jarvis.graph.build` tracks worker results, tool messages, events, failures, and artifacts. Per-worker exceptions become failure results so an unrelated parallel worker can still complete.

The plan is a numbered request checklist. The supervisor acts before closing steps, verifies tool results, retries empty/refused actions only within bounded budgets, continues remaining independent work, and reports unresolved items rather than inventing completion. `jarvis.runtime.evidence` recomputes execution flags from actual tool evidence; model text alone cannot prove an action.

## Runtime and progress

`AgentResult` is the shared result contract: answer/spoken answer, plan, workers, events, findings, artifacts, errors, status, memory persistence, task/run IDs, and tool evidence. `run_agent` establishes memory/state/surface contexts, runs fast paths or the graph, persists the turn, and returns that contract. `stream_agent` adapts progress plus final output for streaming consumers.

Progress events describe visible stages (planning, model/tool start/end, verification, warnings, failures). The UI never exposes hidden chain-of-thought. CLI, Telegram, API/web, and voice render the same progress/error truth with surface-appropriate formatting. CLI `/trace [limit]` shows the last request's measured wall time plus model/tool operation spans; it omits prompts, tool arguments, outputs, and reasoning text. Latency controls are role-specific timeouts, fast paths, cached routing/planning, and no filler sleeps; provider billing is not estimated without provider telemetry. Source self-improvement is proposal-only: unified diffs are stored for owner review, and no tool auto-applies or executes them.

## Model binding and fallback

`jarvis.llm.core.FallbackChatModel` keeps a model in a tool-required chain only when `bind_tools` succeeds. Binding failures remain visible in diagnostics. If every configured candidate fails to bind tools, the task returns an explicit binding error rather than silently using a text-only model. Offline mode uses deterministic local behavior for tests and demonstrations.

## Tool registry and contracts

`jarvis.tools` is the source of truth for registered LangChain tools and group selection. Browser agents may invoke Jarvis actions only through registry-backed delegation; unknown tool names are rejected. `scripts/audit_tools.py` validates names, descriptions, schemas, and safe callability without invoking state-changing operations.

Desktop tools are typed and observable: a click consumes a fresh screenshot observation ID, coordinates are converted from screenshot to device pixels, and the visible result is rechecked. Browser tasks and parallel research have budgets, progress events, repeated-state intervention, and per-session/per-site failure isolation. File-manager changes keep a bounded workspace undo journal; an undo refuses to overwrite a later edit and routes removals/replacements through the Gate. App-launch memory stores only verified launch mappings. Skills are instruction packs in `jarvis/skills/catalog/<snake_case>/SKILL.md`; their declared tools are checked against the registry in tests.

## Memory and persistence

- `jarvis/memory/store.py`: SQLite + FTS5 facts, turns, tasks, and note index.
- `jarvis/memory/session_state.py`: short-term state with atomic, locked persistence and corruption reporting.
- `jarvis/automation/scheduler.py`: timezone-aware schedule parser and locked/atomic JSON store with backup/recovery, run IDs, coalescing, origin-surface delivery, and execution-time Gate checks.
- `workspace/`: runtime state, notes, app mappings, browser/desktop artifacts, logs, and automations. It is local data, not source code for the release archive.

Owner memory is shared across owner surfaces; guest memory namespaces are isolated. Credentials remain in environment configuration, not prompts or reports.

## Human-only Dangerous-Command Gate

`jarvis.security.gate` parses shell segments, quotes, wrappers, normalized flags, and nested interpreters where possible. Unknown/unparseable shell syntax is treated conservatively. The default `risky` mode prompts only for classified dangerous or explicitly sensitive operations; `all` prompts for every command, and `off` disables the Gate with one startup warning.

An approval is server-side and bound to the exact operation, cwd, owner, and surface; it expires and is consumed once. The CLI, API/web, Telegram buttons, voice yes/no flow, and automation runner resolve approvals through owner-side handlers. No model-visible approval token or boolean override is accepted. Refusal, timeout, wrong user/surface, or missing handler means no action.

## Surface adapters

- `jarvis/cli/`: CLI routing, commands, doctor, progress and trace presentation.
- `jarvis/api/server.py`: FastAPI chat/task/stream/status, app and automation endpoints, owner approvals, and inline web console. Non-loopback startup warns if no API token is configured; a configured `API_AUTH_TOKEN`/`NIKHILAI_API_TOKEN` is compared as UTF-8 bytes.
- `jarvis/telegram/`: owner/guest identity, commands, inline approvals, progress editing, and origin-surface automation delivery.
- `jarvis/voice/`: LiveKit wrapper that starts outside an already-running event loop and calls the shared `run_agent`; voice Gate accepts clear yes/no or cancels on uncertainty/timeout.
- `jarvis/automation/`: scheduler calls the shared runner with stored origin surface and unique run ID.

The behavioral surface matrix and current evidence are in [`STATUS.md`](STATUS.md). Setup requirements are in [`SETUP_APIS.md`](SETUP_APIS.md).
