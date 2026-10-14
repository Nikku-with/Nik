# Tool Callability Audit — NIKHILAI

**Date:** 2026-10-05  ·  **Tools:** 77  ·  **Verdict:** PASS ✅

- ✅ callable: **77**
- 🟡 warn: **0**
- 🔴 broken: **0**

Har tool ko LLM ki tarah invoke kiya gaya (`ainvoke` + schema-validated args). Side-effect wale tools schema-check hote hain (asli call `--live-dangerous` se).

| # | group | tool | status | evidence |
|---|-------|------|--------|----------|
| 1 | code | `code_engine` | ✅ | 1ms • ✅ Code likh diya (run=False): /home/user/NikhilAI_Jarvis/workspace/scripts/aud |
| 2 | code | `create_project` | ✅ | 1ms • 🏗️ Project ban gaya: /home/user/NikhilAI_Jarvis/workspace/artifacts/audit   -  |
| 3 | code | `install_package` | ✅ | 431ms • ❌ pip install audit ERROR: Could not find a version that satisfies the require |
| 4 | code | `run_python` | ✅ | 20ms • [exit 0 \| 0.0s] 2 |
| 5 | code | `run_script_file` | ✅ | 31ms • ▶️ audit.audit [exit 0 \| 0.0s] [PLAN] audit   1. Task samjho aur inputs identi |
| 6 | desktop | `append_note` | ✅ | schema ok • call skipped (side-effect) • args=['content', 'name'] |
| 7 | desktop | `close_app` | ✅ | schema ok • call skipped (side-effect) • args=['window_title'] |
| 8 | desktop | `control_volume` | ✅ | schema ok • call skipped (side-effect) • args=['action'] |
| 9 | desktop | `find_apps_tool` | ✅ | 2ms • ❌ 'notepad' naam ka koi app installed nahi mila. Tip: `python -c "from jarvis. |
| 10 | desktop | `find_file` | ✅ | 11ms • ❌ 'audit' nahi mila (roots: /home/user, /home/user/NikhilAI_Jarvis/workspace). |
| 11 | desktop | `find_files` | ✅ | 128ms • 🔍 10 matches: 1. 📁 NikhilAI_Jarvis  [DIR, 05 Oct 2026, 13:40, match 71.2%]     |
| 12 | desktop | `folder_file` | ✅ | schema ok • call skipped (side-effect) • args=['command'] |
| 13 | desktop | `grant_exec_approval` | ✅ | 1ms • ✅ Approval granted — 5 minute ke andar ek risky command `confirmed=true` ke sa |
| 14 | desktop | `list_apps` | ✅ | 1ms • koi match nahi |
| 15 | desktop | `list_dir` | ✅ | 1ms • 📂 /tmp (9 entries) 📁 .ICE-unix                                           DIR   |
| 16 | desktop | `list_notes` | ✅ | 1ms • 📚 5 notes (/home/user/NikhilAI_Jarvis/workspace/notes): - `meeting_notes.md` — |
| 17 | desktop | `list_windows` | ✅ | 1ms • ⚠️ list_windows unavailable: [Errno 2] No such file or directory: 'osascript' |
| 18 | desktop | `open_any_app` | ✅ | schema ok • call skipped (side-effect) • args=['query'] |
| 19 | desktop | `open_app` | ✅ | schema ok • call skipped (side-effect) • args=['app_title'] |
| 20 | desktop | `open_path` | ✅ | schema ok • call skipped (side-effect) • args=['app', 'path'] |
| 21 | desktop | `play_file` | ✅ | schema ok • call skipped (side-effect) • args=['name'] |
| 22 | desktop | `press_hotkey` | ✅ | schema ok • call skipped (side-effect) • args=['keys'] |
| 23 | desktop | `process_input` | ✅ | schema ok • call skipped (side-effect) • args=['pid', 'text'] |
| 24 | desktop | `process_list` | ✅ | 1ms • ℹ️ Koi background process nahi chal raha. |
| 25 | desktop | `process_logs` | ✅ | 1ms • ❌ Process '999999' ka koi log nahi mila. |
| 26 | desktop | `process_stop` | ✅ | schema ok • call skipped (side-effect) • args=['force', 'pid'] |
| 27 | desktop | `read_file` | ✅ | 141ms • 🔎 '/tmp/__nikhilai_nonexistent__.txt' nahi mila tha, closest match use kiya: n |
| 28 | desktop | `read_note` | ✅ | 3ms • ❌ Note nahi mila: __nikhilai_nonexistent__ |
| 29 | desktop | `recent_files` | ✅ | 1ms • 🕒 Last 2 din ki 15 files: - README.md  [15B, 05 Oct, 13:52]   /home/user/Nikhi |
| 30 | desktop | `refresh_app_index` | ✅ | 1ms • 🔄 App index refresh ho gaya: 3 entries. |
| 31 | desktop | `run_shell_command` | ✅ | schema ok • call skipped (side-effect) • args=['background', 'command', 'confirmed', 'cwd', 'name', 'timeout'] |
| 32 | desktop | `save_note` | ✅ | schema ok • call skipped (side-effect) • args=['as_txt', 'content', 'name', 'open_in_notepad', 'tags'] |
| 33 | desktop | `search_inside_files` | ✅ | 59ms • 🔎 'nikhilai-audit-needle' 2 file(s) me mila:  📄 TOOL_AUDIT.md    /home/user/Ni |
| 34 | desktop | `search_notes` | ✅ | 2ms • ❌ 'nikhilai' kisi note me nahi mila. |
| 35 | desktop | `start_background_process` | ✅ | schema ok • call skipped (side-effect) • args=['command', 'cwd', 'name'] |
| 36 | desktop | `system_info` | ✅ | 205ms • 🖥️ Linux 6.1.158+ (x86_64) 🐍 Python 3.13.14 @ /usr/local/bin/python3 CPU: 2 co |
| 37 | desktop | `take_screenshot` | ✅ | schema ok • call skipped (side-effect) • args=['name'] |
| 38 | desktop | `type_text` | ✅ | schema ok • call skipped (side-effect) • args=['text'] |
| 39 | desktop | `write_file` | ✅ | schema ok • call skipped (side-effect) • args=['content', 'path'] |
| 40 | memory | `conversation_history` | ✅ | 2ms • 🗂️ Recent conversation: Owner: hello Owner: hello Jarvis: नमस्ते Nikhil sir 🙌  |
| 41 | memory | `memory_compact` | ✅ | schema ok • call skipped (side-effect) • args=- |
| 42 | memory | `memory_stats` | ✅ | 2ms • 🧠 Memory stats — facts: 1, episodes: 1057, notes: 47, tasks: 419, size: 1220 K |
| 43 | memory | `recall_facts` | ✅ | 2ms • 🧠 Mil gaya: - [episode • 0.33] मैं Jarvis हूं — NikhilAI का personal AI operat |
| 44 | memory | `remember_fact` | ✅ | schema ok • call skipped (side-effect) • args=['fact', 'key', 'tags'] |
| 45 | ops | `cancel_automation` | ✅ | schema ok • call skipped (side-effect) • args=['ident'] |
| 46 | ops | `clear_cache_tool` | ✅ | 1ms • 🧹 cache cleanup (audit): 0 item(s) hata diye, ~0.0 B free hua |
| 47 | ops | `clear_process_tool` | ✅ | schema ok • call skipped (side-effect) • args=['force', 'name', 'pid'] |
| 48 | ops | `create_automation` | ✅ | schema ok • call skipped (side-effect) • args=['enable', 'schedule', 'task'] |
| 49 | ops | `full_system_status` | ✅ | 3ms • 🖥️ System status:   • time: 2026-10-05 13:52:11   • python: 3.13.14   • execut |
| 50 | ops | `list_automations` | ✅ | 1ms • 📋 Koi automation set nahi hai. Bolo: "har subah 8 baje mausam batao" |
| 51 | ops | `refresh_system_tool` | ✅ | schema ok • call skipped (side-effect) • args=['clear_caches'] |
| 52 | ops | `reload_config_tool` | ✅ | 9ms • ♻️ config + runtime settings dobara load ho gayi |
| 53 | ops | `restart_jarvis_tool` | ✅ | schema ok • call skipped (side-effect) • args=- |
| 54 | ops | `run_automation` | ✅ | 1ms • ❌ automation 'audit' nahi mili |
| 55 | ops | `toggle_automation` | ✅ | schema ok • call skipped (side-effect) • args=['enabled', 'ident'] |
| 56 | research | `browser_cli` | ✅ | 20ms • ⚠️ `browser-use` CLI machine par nahi mila. Install: ```bash pip install brows |
| 57 | research | `browser_close` | ✅ | 1ms • ℹ️ Koi browser session khula nahi tha. |
| 58 | research | `browser_doctor` | ✅ | 1ms • 🩺 browser-use status • python API      : ❌ • CLI (Path-2)    : ❌  • mode       |
| 59 | research | `browser_research` | ✅ | schema ok • call skipped (side-effect) • args=['fields', 'max_parallel', 'save_note_as', 'sites', 'task'] |
| 60 | research | `browser_screenshot` | ✅ | 1ms • ❌ browser-use installed nahi hai. |
| 61 | research | `browser_task` | ✅ | schema ok • call skipped (side-effect) • args=['max_steps', 'output_fields', 'save_note_as', 'start_url', 'tas |
| 62 | research | `deep_research` | ✅ | schema ok • call skipped (side-effect) • args=['max_pages', 'query', 'sites'] |
| 63 | research | `fetch_page` | ✅ | 136ms • 📄 https://example.com --- Example Domain This domain is for use in documentati |
| 64 | research | `get_current_datetime` | ✅ | 1ms • Monday, 05 October 2026 — 07:22 PM (IST) |
| 65 | research | `get_weather` | ✅ | 786ms • Clear , 29°C (feels 28°C), humidity 38%, wind 14 km/h — Kota |
| 66 | research | `google_search` | ✅ | 1708ms • 🔎 Top results: 1. Welcome to Python.org    https://www.python.org/    Python i |
| 67 | runtime | `python_repl` | ✅ | 3ms • [repl ok \| 0.0s] 2 |
| 68 | runtime | `repl_reset` | ✅ | schema ok • call skipped (side-effect) • args=- |
| 69 | runtime | `repl_state` | ✅ | 0ms • 🧪 REPL variables: - print: function = <function PythonREPL.run.<locals>._captu |
| 70 | runtime | `run_python_isolated` | ✅ | 17ms • [isolated exit=0] ok  📜 script: /home/user/NikhilAI_Jarvis/workspace/scripts/i |
| 71 | runtime | `session_context` | ✅ | 1ms • 🧭 Session state (jarvis_memory.json): Workspace: /home/user/NikhilAI_Jarvis/wo |
| 72 | runtime | `session_note` | ✅ | 1ms • 📝 Session note saved: audit |
| 73 | runtime | `set_pending_steps` | ✅ | 1ms • 📌 0 pending steps saved. |
| 74 | runtime | `set_preference` | ✅ | 1ms • ⚙️ Preference set: audit = audit |
| 75 | runtime | `set_workspace` | ✅ | 1ms • ❌ Path nahi mila: audit |
| 76 | skills | `list_skills` | ✅ | 1ms • 🧩 Skills: - gst_invoice: Client ke liye GST invoice banane ka process (HTML →  |
| 77 | skills | `read_skill` | ✅ | 1ms • ### SKILL: gst_invoice Client ke liye GST invoice banane ka process (HTML → PD |

Regenerate: `python scripts/audit_tools.py --report TOOL_AUDIT.md`
