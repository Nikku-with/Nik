# NikhilAI

**NikhilAI** is Nikhil Mehra's local/VM-first AI operator: one shared agent runtime for the CLI, HTTP/web UI, Telegram, LiveKit voice, and scheduled automations. It can answer, research, manage files and notes, launch apps, use a supervised browser, run code, remember owner preferences, and report progress. Tool-required actions are only reported successful when there is execution evidence.

User-facing responses are Hinglish by default. Internal code and technical docs are English.

## Quick start

**Linux / macOS (PowerShell users can use the Windows block below):**

```bash
git clone <repo-url> NikhilAI && cd NikhilAI
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev,api,telegram,browser,gemini,desktop]'
cp .env.example .env
python -m jarvis doctor
python -m jarvis
```

**Windows PowerShell:**

```powershell
git clone <repo-url> NikhilAI
Set-Location NikhilAI
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,api,telegram,browser,gemini,desktop]"
Copy-Item .env.example .env
python -m jarvis doctor
python -m jarvis
```

The `desktop` extra is optional; omit it for headless use. Add a provider key to `.env` only if you want live LLM responses; never commit `.env`.

Python 3.10+ is supported. With no provider key, deterministic offline/demo workflows remain available; live model calls require configured credentials. Optional integrations need their own dependencies, credentials, hardware, or display session.

## Run a surface

| Surface | Start | Notes |
|---|---|---|
| CLI | `python -m jarvis` or `python -m jarvis cli` | Interactive Hinglish requests; `/help`, `/run`, `/trace`, `/apps`, `/automation`, `/stop`, `/doctor`. |
| API + web console | `python -m jarvis serve` | FastAPI and responsive web UI; optional `API_AUTH_TOKEN` / `NIKHILAI_API_TOKEN` guards private routes when configured. Sensitive-memory consent is exact-ID via `/memory`. |
| Telegram | `python -m jarvis telegram` | Configure `TELEGRAM_BOT_TOKEN` and `TELEGRAM_OWNER_ID`; owner approvals use inline buttons and sensitive-memory confirmations use `/memory pending|confirm|cancel`. |
| LiveKit voice | `python -m jarvis voice console` or `python -m jarvis console` | Install `.[voice,gemini]` in a dedicated `.venv-voice` (LiveKit and Browser-Use pin incompatible OpenAI/Google SDK versions); configure cloud keys, then check `python -m jarvis voice status`. Console flags such as `--list-devices` and `--text` pass through to LiveKit. |
| Scheduled jobs | Create from natural language or `/automation` | Persisted jobs run through the shared agent; execution Gate is checked again at run time. |

The CLI also accepts one-shot free text: `python -m jarvis hy` or `python -m jarvis "Kota ka weather batao"`.

## Capabilities

- **Plan → act → verify → resume:** numbered task steps, actual tool evidence, bounded retries/refinement, and honest partial/failure status.
- **Shared progress:** CLI/API/web/Telegram/voice/scheduler use `jarvis.runtime.agent.run_agent`; progress reports visible stages and tool status, never hidden chain-of-thought.
- **Desktop:** typed observe/OCR/click/type/keyboard/window/clipboard tools; fresh screenshot coordinates, HiDPI conversion, visible verification, and human approval for destructive interactions. Headless systems receive a friendly unavailable result.
- **App launcher:** open-app-only workspace memory, lookup before search, save after verified launch, stale-entry recovery, and `/apps` management.
- **Browser supervision:** bounded steps/time, progress, repeated-state detection, isolated research sessions, per-site failure isolation, and registry-backed delegated tools.
- **Memory and schedules:** shared owner memory with guest isolation; session-state JSON uses locked atomic writes and reports corruption through Doctor; timezone-aware automations retain their origin surface and unique run IDs.
- **File undo:** the last verified file-manager mutation has a bounded workspace snapshot; undo refuses to clobber later edits and uses the owner Gate for removals/replacements.
- **Safe self-improvement:** proposed unified diffs are saved for review; the tool never edits or executes NikhilAI source. Latency is bounded by fast-role routing and configured timeouts; provider billing estimates are not available offline.
- **Skills:** eighteen workflow packs under `jarvis/skills/catalog/`; ten are the required verification/research/file/terminal/project/desktop/reporting/automation/memory/self-review workflows.

Desktop examples, surface behavior, recorded verification evidence, and offline/live boundaries are in [docs/STATUS.md](docs/STATUS.md). System structure is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). See [features, requirements, and environment controls](docs/FEATURES_REQUIREMENTS_ENVIRONMENT.md) for the operator handoff.

## Dangerous-Command Gate

`JARVIS_EXEC_APPROVAL` accepts `risky` (default), `all`, or `off`:

- `risky` asks the human owner before classified dangerous commands or explicitly sensitive operations; ordinary safe commands do not prompt.
- `all` asks for every command.
- `off` disables the command Gate and prints one startup warning.

Approvals are bound to the exact operation, working directory, owner, and surface, expire, and are single-use. CLI prompts, API/web approval controls, Telegram inline buttons, and spoken LiveKit yes/no handle approvals. The model cannot approve itself or pass a `confirmed` flag. On cancellation, timeout, missing approver, or wrong user, the operation does not run.

CLI and Telegram owners can inspect/change the preference with `/gate` and `/gate off|risky|all`; `/status` reports the effective mode and whether it comes from environment or saved settings. Explicit `JARVIS_EXEC_APPROVAL`/`EXEC_APPROVAL` values take precedence over the saved preference, and the command reports that override instead of claiming a change. `/mode` changes only the default interface preference; it does not start a microphone or bot. `/model` and `/provider` show the configured route and key availability without probing provider connectivity.

This project is an operator with shell, file, desktop, and browser capabilities—not a sandbox. Run it under a dedicated account or VM and keep credentials in environment configuration.

## Configuration and operations

- Copy `.env.example` to `.env`; configure only integrations you need. See [docs/SETUP_APIS.md](docs/SETUP_APIS.md).
- Personal defaults are `CITY=Kota` and `TIMEZONE=Asia/Kolkata`; both are configurable and invalid timezones fall back to IST.
- Google Search uses its own `GOOGLE_SEARCH_API_KEY`; it does not reuse a Gemini key unless explicitly configured.
- For a non-loopback API bind, consider setting an API auth token. If missing, startup warns but does not block the bind; use HTTPS for network access.
- `python -m jarvis doctor` reports exact missing optional packages/configuration without printing secrets.

## Verification

```bash
ruff check .
python -m compileall -q jarvis tests
pytest -q
python scripts/audit_tools.py
```

Current verification evidence, capability notes, and offline/live boundaries are consolidated in [docs/STATUS.md](docs/STATUS.md). See [CHANGELOG.md](CHANGELOG.md) for changes in the active Round 2 audit.

## Project map

- `jarvis/runtime/` — shared runner, progress, evidence, process manager.
- `jarvis/graph/` — LangGraph planner/workers/verification/refinement.
- `jarvis/tools/` — typed, registered integrations.
- `jarvis/security/` — permission decisions and the human-only Gate.
- `jarvis/memory/`, `jarvis/automation/`, `jarvis/skills/` — persistence, scheduling, workflow packs.
- `jarvis/api/`, `jarvis/cli/`, `jarvis/telegram/`, `jarvis/voice/` — user-facing surfaces.
- `tests/` — fake-provider, fake-tool, fake-Telegram, fake-LiveKit, browser, desktop, Gate, and cross-surface behavior.

MIT license; see [LICENSE](LICENSE).
