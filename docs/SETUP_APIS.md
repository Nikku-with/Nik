# 🔑 Jarvis Setup — Kaun si API chahiye, kahan se milegi, kaise lagani hai

> Ye file hi tera checklist hai. Neeche **jo chahiye** wahi daal — sab kuch mandatory nahi.
> Sirf **1 key** (Gemini) daalte hi poora dimaag chalu ho jata hai. Voice ke liye 3 cheez extra.
> Baaki sab **keyless** hai — bina kisi API ke bhi file dhoondhna, padhna, app kholna, CMD chalana,
> REPL se code likhna-run karna, search karna **sab kaam karta hai**.

---

## 0. TL;DR — kya laana hai

| Priority | Kya | Kahan se | .env me kahan |
|---|---|---|---|
| 🔴 **Zaroori** | **Gemini API key** (`GEMINI_API_KEY`) | https://aistudio.google.com → *Get API key* | line ~17 |
| 🔴 **Voice ke liye** | **LiveKit URL + API Key + Secret** | https://cloud.livekit.io → project → *Settings → Keys* | line ~97-99 |
| 🟡 Optional (search quality) | Brave Search API key | https://brave.com/search/api/ | `BRAVE_API_KEY` |
| 🟡 Optional (extra dimaag) | xAI / Groq / OpenAI / Anthropic key | console.x.ai / console.groq.com / platform.openai.com / console.anthropic.com | `GROQ_API_KEY` etc. |
| 🟡 Optional (search/TTS) | SearXNG self-host URL, OpenWeatherMap, Google CSE | searxng docs / openweathermap.org / programmablesearchengine.google.com | `JARVIS_SEARX_URL`, `OPENWEATHER_API_KEY`, `GOOGLE_SEARCH_API_KEY`+`SEARCH_ENGINE_ID` |
| ⚪ Baad me (chat control) | Telegram / Discord bot token | @BotFather / discord.com/developers | abhi code me nahi |

**Total minimum: 1 key. Voice ke saath 4 chhoti values. Baaki sab optional.**

---

## 1. Gemini API key (dimaag) — 2 minute ka kaam

1. Kholo 👉 **https://aistudio.google.com/apikey**
2. Google account se sign in (Gmail chalega).
3. **"Create API key"** dabao → project select/skip karo → key copy karo (aisi dikhti hai: `AIzaSy...`).
4. Repo me `.env` file kholo aur ye kar do:

```env
GOOGLE_API_KEY=AIzaSy...tumhari_key...
# (chaho to dono me same key daal do — dono naam support hain)
GEMINI_API_KEY=
```

5. Check karo:

```bash
cd NikhilAI
python -m jarvis doctor      # provider: google dikhna chahiye (abhi 'demo' aa raha hai kyunki key khali hai)
python -m jarvis "hello, tum kaun ho?"
```

- **Free tier** hai (daily/minute limits ke saath) — personal use ke liye kaafi. Limit cross ho to Jarvis khud fallback model pe switch ho jata hai (neeche table).
- Payment/credit card **nahi** chahiye free tier ke liye.
- Key ko git me **kabhi commit na karo** — `.gitignore` me `.env` already ignored hai. ✅

### Jarvis ke andar kaun sa model kahan lagta hai

| Role | Model | Kyun |
|---|---|---|
| `brain` (default/supervisor) | **gemini-3.8-flash** | Sabse intelligent general model — planning + answers |
| `browser` (browser-use agent) | **gemini-3.7-flash** | Coding/agentic tasks me sabse strong, browser steps ke liye |
| `fast` (router, chhote kaam) | **gemini-3.5-flash-lite** | Ultra sasta + fast — routing/bulk me latency 10x kam |
| `code` (script likhna/heal) | **gemini-3.8-flash** | Code + planning dono |
| `extract` (data nikalna) | **gemini-3.5-flash-lite** | Price/table jaise structured extraction |
| `realtime` (voice) | **gemini-3.8-live** | Live API — sub-second turn-taking, function calling support |
| *Hardest reasoning (optional)* | `gemini-3.1-pro-preview` | Tab use karo jab 3.8 Flash atak jaye — `.env` me `JARVIS_BRAIN_FALLBACKS` me daal do |

⚠️ **2026 ka sach:** Gemini **2.0 shut down** ho gaya, **2.5 series sirf purane users** ke liye open hai (naye accounts me nahi milega). Isliye default 3.x pe set hai. Fallback chains me 2.5 ke naam hain — agar tumhari key un models me 404 de, Jarvis next model pe auto-switch ho jata hai (`FallbackChatModel`), kuch nahi todta.

Fallback badalna ho:
```env
JARVIS_BRAIN_FALLBACKS=gemini-3.7-flash,gemini-3.1-pro-preview
JARVIS_FAST_FALLBACKS=gemini-3.6-flash,gemini-2.5-flash-lite
```

---

## 2. Voice stack + LiveKit keys (real-time baat karne ke liye)

**Stack kya use ho raha hai:** `LiveKit Agents` (transport/WebRTC + turn detection) + **Gemini Live API** (`gemini-3.8-live` = ears+dimaag+mouth ek hi realtime model me) — code file: `jarvis/voice/livekit_agent.py`.
LiveKit isliye: ultra-low latency (<500ms), barge-in (beech me bol sakte ho), echo/noise cancellation built-in. Zoom/Meet jaisa jugad nahi — proper WebRTC.

### Step-by-step (Cloud — recommended)

1. **Project banao:** https://cloud.livekit.io → sign up (free) → **Create Project**.
2. **Keys nikalo:** project ke andar → **Settings → Keys** → *Create key*. Wahan 3 cheez milengi:
   - `LIVEKIT_URL` → `wss://<project-name>.livekit.cloud`
   - `LIVEKIT_API_KEY` → `APIxxxxxxxxxxxx`
   - `LIVEKIT_API_SECRET` → lamba secret
3. `.env` me daalo:
```env
LIVEKIT_URL=wss://<tumhara-project>.livekit.cloud
LIVEKIT_API_KEY=APIxxxxxxxx
LIVEKIT_API_SECRET=xxxxxxxxxxxxxxxx
JARVIS_VOICE_NAME=Charon        # voice choose karo
JARVIS_REALTIME_MODEL=gemini-3.8-live
```
4. Voice worker chalao (apni machine pe, ek baar install):
```bash
pip install "livekit-agents[google]" livekit-plugins-google livekit-plugins-silero

python -m jarvis voice status     # pehle check: kya ready hai, kya missing
python -m jarvis voice test       # keys ke BINA bhi pipeline check (spoken text + latency)
python -m jarvis voice console    # terminal me hi mic+speaker — browser nahi chahiye
python -m jarvis console          # wahi shortcut (`mic`/`talk` bhi chalta hai)
python -m jarvis voice dev        # dev mode (LiveKit Cloud debugging/jobs)
python -m jarvis voice start      # production worker

# LiveKit ke apne options bhi seedha do (passthrough):
python -m jarvis voice console --list-devices     # audio devices dikhao
python -m jarvis voice console --text             # mic ke bina, keyboard se test
python -m jarvis voice console --input-device 2   # kaunsa mic use karna hai
```

**Console me kya hota hai:** `voice console` aapke hi laptop ka mic leta hai → LiveKit room (aap hi
ke machine par, local participant) → Gemini Live realtime → tools/LangGraph → jawab speaker par.
Isliye koi browser/website nahi chahiye. Sirf 4 keys chahiye (teen LiveKit + ek Gemini) —
exact steps ek command me: **`python -m jarvis voice keys`**.

Gadbad lage to:
| Symptom | Fix |
|---|---|
| **LiveKit apna `Usage: python -m jarvis [OPTIONS] COMMAND ...` help print kar deta hai** (voice console start nahi hota) | ✅ Fixed — menu option 2 / `voice` (bina mode) me hum `console` subcommand chhod dete the, LiveKit `sys.argv` se padhta hai. Ab har raaste se `console` jaata hai. Test: `python tests/test_voice_cli.py` |
| Mic/speaker kaunse hain dekhna hai | `python -m jarvis voice console --list-devices` |
| Sirf keyboard se test karna hai (mic ke bina) | `python -m jarvis voice console --text` |
| `voice mode abhi ready nahi` | `python -m jarvis voice keys` follow karo; `voice status` par sab ✅ hone chahiye |
| Keys daal di, phir bhi missing | `.env` workspace root me hona chahiye (`python -m jarvis doctor` se check) |
| Mic sunai nahi deta | OS me mic permission + input device default check karo |
| Latency zyada | `JARVIS_REALTIME_MODEL` = `gemini-3.8-live` rakho (realtime model hi sabse fast hai) |

### CLI route (agar LiveKit CLI se deploy karna ho)

```bash
# install
curl -sSL https://get.livekit.io/cli | bash      # Linux/WSL   (macOS: brew install livekit-cli)
lk cloud auth                                     # browser me login → project link
lk agent init my-jarvis --template agent-starter-python
# template me .env.local aata hai; keys daal ke:
lk agent deploy
```
Templates: `livekit-examples/agent-starter-python` (ya `-node`).

### Fully local / self-host (0 cloud)

```bash
livekit-server --dev            # → ws://localhost:7880, key: devkey, secret: secret
```
`.env` me wahi local values daal do. (Baaki machine se connect karna ho to HTTPS/TURN setup lagega — production me Cloud hi aasan hai.)

> LiveKit Cloud ka apna "Inference" (STT/TTS) bhi free tier me included hai, lekin Jarvis Gemini Live use karta hai — yaani tera **Gemini key hi voice ka dimaag** hai.

---

## 2.5 OpenRouter / Groq (free fallback brains — optional par recommended)

Sirf ek provider par depend karna theek nahi. Do free keys daal do — model fail ho ya quota
khatam ho to agent automatically agle provider par switch ho jata hai.

```env
# OpenRouter — free `:free` models (https://openrouter.ai/keys)
OPENROUTER_API_KEY=sk-or-v1-...
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1

# Groq — bahut fast free tier (https://console.groq.com/keys)
GROQ_API_KEY=gsk_...
GROQ_BASE_URL=https://api.groq.com/openai/v1

# chaaho to provider force karo (warna auto-detect: gemini → openrouter → groq)
BRAIN_PROVIDER=
```

Jarvis live `/models` endpoint se **available free models** khud dhoondh leta hai
(`MODEL_DISCOVERY=true`), isliye koi purana model hardcode nahi hota.

---

## 2.6 Telegram bot (phone se control)

1. Telegram me **@BotFather** → `/newbot` → naam do → **token** copy karo.
2. Apna numeric id: Telegram me **@userinfobot** ko message karo.
3. `.env` me:

```env
TELEGRAM_BOT_TOKEN=123456789:AA...
TELEGRAM_OWNER_ID=123456789            # sirf aap poore tools chala paoge
TELEGRAM_ALLOWED_CHAT_IDS=             # family/team (guest = read-only)
```

4. Chalao: `python -m jarvis telegram` → Telegram pe bot ko `/start` bhejo.

Auth: owner = full access, allowed users = guest (read-only: shell/desktop/file-write blocked).

---

## 3. Optional boosters (na daalo to bhi chalega)

| Kya | Kahan se | Kya milega |
|---|---|---|
| `BRAVE_API_KEY` | brave.com/search/api → free plan | Search quality + ranking best; ddgs fail hone par backup |
| `JARVIS_SEARX_URL` | khud ka SearXNG (`docker run searxng/searxng`) | 100% private search, koi rate limit nahi |
| `XAI_API_KEY` | console.x.ai | Grok model fallback (bade context + tool use) |
| `GROQ_API_KEY` | console.groq.com (free) | Bahut fast Llama fallback — jab Gemini quota khatam ho |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | platform.openai.com / console.anthropic.com | A-grade fallback brains |
| `OPENWEATHER_API_KEY` | openweathermap.org | Weather tool (bina key bhi DDG se kaam chal jata hai) |
| `GOOGLE_SEARCH_API_KEY` + `SEARCH_ENGINE_ID` | programmable search engine | Official Google results (100 query/day free) |

**Bina API wale powers (abhi isi second chal rahe hain):**
- 🔎 Web search — `ddgs` (DuckDuckGo + Brave + Bing + mojeek etc. ka meta-search, **keyless**)
- 📄 Kisi bhi file ka read — PDF, Word, Excel, CSV, JSON, notebook, PPT, image, zip/tar, media
- 🗂️ File dhoondhna — naam galat/typo ho ya adhoora, multiple folders me dhoondh deta hai
- 🖥️ App kholna — naam se search karke (Windows/macOS/Linux sab)
- ⚙️ System control — native `subprocess` + `os` + `shutil` (koi extra install nahi)
- 🐍 Python REPL — apna code khud likh ke test karta hai, error aaye to khud fix karke dobara chalata hai
- 🧠 `workspace/jarvis_memory.json` — current workspace + task history + last error + short-term memory

---

## 4. Security (by default on)

- **Exec approval:** `JARVIS_EXEC_APPROVAL=risky` → delete/kill/install jaisi commands pe Jarvis pehle puchega ("haan-karo" bolo to chalega). `off` = bina puche, `all` = sab pe puche.
- **Secrets:** command me `${SECRET_email}` likho, real value `JARVIS_SECRET_email` se aayegi — aur output me auto-redact ho jaati hai (LLM ko kabhi plain password nahi dikhta).
- **Protected paths:** System32 / Program Files / /etc / /boot jaisi jagah delete-lock hai.
- **Background processes** capped hain (`JARVIS_MAX_PROCESSES=12`), har process ka log `workspace/logs/` me.

---

## 5. Verify checklist (keys daalne ke baad)

```bash
python -m jarvis doctor      # ✅ provider google, livekit ok dikhe
python -m jarvis voice keys  # voice ke liye exact keys + steps
python -m jarvis test    # 422/422 aana chahiye (428 with livekit-agents)
python -m jarvis status       # 66 tools
python -m jarvis "delhi ka aaj ka mausam batao aur usse ek short note banao"
python3 -m uvicorn jarvis.api.server:app --host 0.0.0.0 --port 8000   # API + web console
```
