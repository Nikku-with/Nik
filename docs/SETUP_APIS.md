# Setup and optional integrations

Start with the repository's [`README.md`](../README.md) and `.env.example`. Credentials belong in the local `.env` or environment, never in source files, logs, tests, Git, or reports. `python -m jarvis doctor` reports missing packages/configuration without printing secret values.

## Core install

```bash
python -m pip install -e '.[dev,api,telegram,browser]'
cp .env.example .env
python -m jarvis doctor
```

The core install supports deterministic offline/demo workflows. A configured provider is needed for live model responses. Explicit provider selection is respected; check doctor/status output if the configured model cannot bind required tools.

## Model providers

Configure only the key/provider pair you intend to use:

| Provider | Environment | Notes |
|---|---|---|
| Gemini | `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) | Gemini brain and Google Live voice plugin use the provider configuration. |
| OpenRouter | `OPENROUTER_API_KEY`, optional `OPENROUTER_BASE_URL` | OpenAI-compatible provider. |
| Groq | `GROQ_API_KEY`, optional `GROQ_BASE_URL` | OpenAI-compatible provider. |
| OpenAI / Anthropic / xAI / Ollama | Provider-specific variables in `.env.example` | Install the relevant optional package and configure that provider explicitly. |

Model roles may be selected with `BRAIN_MODEL`, `FAST_MODEL`, `PLANNER_MODEL`, `CODE_MODEL`, `RESEARCH_MODEL`, `EXTRACT_MODEL`, `VISION_MODEL`, `BROWSER_MODEL`, and `CRITIC_MODEL`. `BRAIN_PROVIDER` can pin the provider; leave it empty for configured-provider discovery/fallback behavior. The doctor and status commands report the active provider and any binding/setup failure.

## Search and location

- Keyless search is controlled by `DUCKDUCKGO_ENABLED`.
- Google Custom Search requires both `GOOGLE_SEARCH_API_KEY` and `SEARCH_ENGINE_ID`. Jarvis does **not** reuse `GEMINI_API_KEY` as a Google Search credential unless a distinct explicit setting is introduced.
- Optional search configuration includes `BING_SEARCH_API_KEY`, `SEARX_URL`, and `BRAVE_API_KEY`.
- Personal defaults are `CITY=Kota` and `TIMEZONE=Asia/Kolkata`; set either to customize. Invalid timezone configuration falls back to Asia/Kolkata.
- `OPENWEATHER_API_KEY` is optional for weather sources that require it.

## Voice / LiveKit

The current LiveKit 1.8.x stack requires newer OpenAI/Google SDK versions than Browser-Use 0.13.x pins. Keep browser and voice integrations in separate virtual environments rather than allowing one extra to replace the other's pinned SDKs:

```bash
python -m venv .venv-voice
# Linux/macOS: source .venv-voice/bin/activate
# Windows PowerShell: .venv-voice\Scripts\Activate.ps1
python -m pip install -e '.[voice,gemini]'
python -m jarvis voice status
python -m jarvis voice keys
python -m jarvis voice console
```

Configure both environments to use the same project `.env` and workspace location if you want shared persisted owner memory. Set `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, and a supported Gemini key/plugin configuration. The doctor/voice status names exact missing dependencies/configuration without displaying secret values. Voice uses the same `run_agent` implementation. Fake LiveKit sessions are covered by tests; no real microphone or cloud session was run here.

### Troubleshooting: voice Usage and console options

Run `python -m jarvis voice console` (or the `python -m jarvis console` alias) to launch the LiveKit console; bare `python -m jarvis voice` shows Jarvis voice help. Options such as `--list-devices`, `--input-device`, and `--text` are passed through to LiveKit. If the optional LiveKit packages or cloud configuration are missing, the CLI prints the exact setup needed instead of claiming a session started.

## Telegram

Install `.[telegram]`, then set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_OWNER_ID`. Optionally set `TELEGRAM_ALLOWED_USERS` or `TELEGRAM_ALLOWED_CHAT_IDS`; non-owner users are isolated and restricted by the existing surface policy. Start with:

```bash
python -m jarvis telegram
```

Owner dangerous-command requests use server-side callback IDs and inline approve/cancel buttons. For staged sensitive memories, use `/memory pending`, then `/memory confirm <exact-id>` or `/memory cancel <exact-id>`; ordinary yes/no chat replies resolve a pending item only when exactly one exists. A real Telegram account/login is not part of offline test evidence.

## API and web UI

Install `.[api]`; default bind is `127.0.0.1:8000`:

```bash
python -m jarvis serve
```

`NIKHILAI_API_TOKEN` (also accepted as `API_AUTH_TOKEN` or `JARVIS_API_TOKEN`) is optional. When set, private API routes require its bearer token (or the web console's HttpOnly session cookie); comparison uses UTF-8 bytes and invalid tokens return 401. When unset, the API stays open; a non-loopback bind prints a warning but is not blocked. Use HTTPS if you expose it to a network.

The web UI presents visible request progress, results, automations, and pending owner approvals. Approval operations are resolved server-side, once, for the configured owner; do not send bearer tokens in URLs.

Sensitive memory saves/updates stay pending until explicit consent. `GET /memory` includes pending IDs for review; `POST /memory` with `{"action":"confirm","id":"<exact-pending-id>"}` saves that item, while `{"action":"cancel","id":"<exact-pending-id>"}` discards it. A missing/expired/wrong ID does not resolve another pending item.

## Dangerous-Command Gate

`JARVIS_EXEC_APPROVAL` supports:

- `risky` — human approval only for parsed dangerous/sensitive actions; safe commands do not prompt.
- `all` — request owner approval for every command.
- `off` — Gate disabled; one startup warning is emitted.

Gate approvals are bound to a single exact operation, working directory, owner, and surface. They expire and are consumed once. Missing/late/unclear approval, timeout, or wrong identity fails closed. No model-visible token or `confirmed` argument exists. Do not place API keys/passwords in command text; use environment-based configuration.

In the CLI and Telegram, `/gate` reports the effective mode and its source; owners can set `/gate off|risky|all`. An explicit `JARVIS_EXEC_APPROVAL` or `EXEC_APPROVAL` environment value takes precedence over a saved runtime preference, and the UI refuses to claim that a saved value changed the effective mode. `/status` also shows the effective Gate mode. These commands do not contact providers or execute shell commands.

## Browser and desktop

Install `.[browser]` for the Python browser agent/CLI integration. The browser doctor identifies a missing browser-use package or browser runtime. Real Chromium actions require a usable browser installation/profile and were not exercised in offline tests.

Desktop control is registered by default. Optional pyautogui/pynput packages improve input coverage; native platform tools may be used where available. On Linux, a graphical display is required for real mouse/screenshot operations; install Tesseract plus `pytesseract` for OCR. Run `python -m jarvis doctor` to see current readiness. Headless environments receive a friendly unavailability status.

## Other runtime configuration

`MAX_ITERATIONS` bounds graph refinement. Browser budget variables, shell/code timeouts, workspace roots, feature toggles, and file-search limits are documented inline in `.env.example`. `WORKSPACE_DIR` defaults to the project workspace. Personal data and runtime logs should not be included in source-control archives.
