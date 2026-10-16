# WIRING MAP — NikhilAI / Jarvis

> **Generated file — do not edit by hand.**
> Source: `python scripts/wiring_map.py` (AST analysis, stdlib only).
> Har table neeche **source code se** nikal kar banayi gayi hai; README/STATUS.md
> ke kisi bhi claim par bharosa nahi kiya gaya.

## 0. Ek nazar me

| metric | value |
|---|---|
| modules analysed | 89 |
| registered tools | 99 |
| tools with a test reference | 70 |
| tools an executor can reach | 83 |
| dead modules | 0 |
| import cycles (any edge) | 1 |
| import cycles (module-level only, break `import`) | 0 |
| duplicate capability implementations | 0 |

## 1. Tool table

Har registered tool: kahan bana, kaun reference karta hai, kaun se surface policy se
allowed hain, aur koi test ise chhuta hai ya nahi.

| tool | module | groups | level | surfaces | referenced by | test |
|---|---|---|---|---|---|---|
| `append_note` | `jarvis.tools.notes_tools:124` | desktop,notes | WRITE | cli_owner, api, telegram_owner, voice_owner, automation | — | **NO** |
| `browser_cli` | `jarvis.tools.browser_tools:928` | research,browser | EXECUTE | cli_owner, telegram_owner, voice_owner, automation | jarvis.api.server, jarvis.runtime.status | yes |
| `browser_close` | `jarvis.tools.browser_tools:989` | research,browser | EXECUTE | cli_owner, telegram_owner, voice_owner, automation | — | yes |
| `browser_doctor` | `jarvis.tools.browser_tools:995` | research,browser | EXECUTE | cli_owner, telegram_owner, voice_owner, automation | — | yes |
| `browser_research` | `jarvis.tools.browser_tools:815` | research,browser | EXECUTE | cli_owner, telegram_owner, voice_owner, automation | jarvis.graph.workers.browser, jarvis.llm.demo | yes |
| `browser_screenshot` | `jarvis.tools.browser_tools:971` | research,browser | EXECUTE | cli_owner, telegram_owner, voice_owner, automation | — | **NO** |
| `browser_task` | `jarvis.tools.browser_tools:769` | research,browser | EXECUTE | cli_owner, telegram_owner, voice_owner, automation | jarvis.dspy_engine.modules, jarvis.graph.workers.browser | yes |
| `cancel_automation` | `jarvis.automation.tools:71` | ops,automation | WRITE | cli_owner, api, telegram_owner, voice_owner, automation | — | **NO** |
| `clear_cache_tool` | `jarvis.tools.system_ops:352` | ops,system_ops | DANGEROUS | cli_owner, telegram_owner | jarvis.security.permissions, jarvis.tools.browser_tools | yes |
| `clear_process_tool` | `jarvis.tools.system_ops:360` | ops,system_ops | DANGEROUS | cli_owner, telegram_owner | jarvis.graph.workers.offline, jarvis.security.permissions, jarvis.tools.browser_tools | **NO** |
| `click_text` | `jarvis.tools.desktop:1001` | desktop | SYSTEM | cli_owner, api, telegram_owner, voice_owner, automation | jarvis.security.permissions | yes |
| `close_app` | `jarvis.tools.system_tools:240` | desktop,system | SYSTEM | cli_owner, api, telegram_owner, voice_owner, automation | jarvis.graph.workers.desktop, jarvis.graph.workers.desktop_actions, jarvis.security.permissions, jarvis.tools.browser_tools | yes |
| `code_engine` | `jarvis.tools.code_engine:443` | code | EXECUTE | cli_owner, telegram_owner, voice_owner, automation | jarvis.graph.workers.code, jarvis.llm.demo, jarvis.security.permissions, jarvis.tools.browser_tools | yes |
| `control_volume` | `jarvis.tools.system_tools:380` | desktop,system | SYSTEM | cli_owner, telegram_owner | jarvis.graph.workers.desktop_actions | **NO** |
| `conversation_history` | `jarvis.memory.memory_tools:45` | memory | READ | cli_owner, api, telegram_owner, telegram_guest, voice_owner, automation | — | **NO** |
| `create_automation` | `jarvis.automation.tools:33` | ops,automation | WRITE | cli_owner, api, telegram_owner, voice_owner, automation | jarvis.graph.workers.offline | yes |
| `create_project` | `jarvis.tools.code_engine:535` | code | WRITE | cli_owner, api, telegram_owner, voice_owner, automation | jarvis.security.permissions | yes |
| `deep_research` | `jarvis.tools.web_tools:371` | research,web | READ | cli_owner, api, telegram_owner, telegram_guest, voice_owner, automation | — | yes |
| `describe_screen` | `jarvis.tools.desktop:1141` | desktop | SYSTEM | cli_owner, api, telegram_owner, voice_owner, automation | jarvis.security.permissions | yes |
| `desktop_click` | `jarvis.tools.desktop:759` | desktop | SYSTEM | cli_owner, api, telegram_owner, voice_owner, automation | jarvis.security.permissions | yes |
| `desktop_clipboard_read` | `jarvis.tools.desktop:1326` | desktop | SYSTEM | cli_owner, api, telegram_owner, voice_owner, automation | jarvis.security.permissions | yes |
| `desktop_clipboard_write` | `jarvis.tools.desktop:1336` | desktop | SYSTEM | cli_owner, api, telegram_owner, voice_owner, automation | jarvis.security.permissions | yes |
| `desktop_drag` | `jarvis.tools.desktop:1182` | desktop | SYSTEM | cli_owner, api, telegram_owner, voice_owner, automation | jarvis.security.permissions | yes |
| `desktop_focus` | `jarvis.tools.desktop:1315` | desktop | SYSTEM | cli_owner, api, telegram_owner, voice_owner, automation | jarvis.security.permissions | yes |
| `desktop_move` | `jarvis.tools.desktop:814` | desktop | SYSTEM | cli_owner, api, telegram_owner, voice_owner, automation | jarvis.security.permissions | yes |
| `desktop_observe` | `jarvis.tools.desktop:737` | desktop | SYSTEM | cli_owner, api, telegram_owner, voice_owner, automation | jarvis.security.permissions | yes |
| `desktop_ocr` | `jarvis.tools.desktop:748` | desktop | SYSTEM | cli_owner, api, telegram_owner, voice_owner, automation | jarvis.security.permissions | yes |
| `desktop_press` | `jarvis.tools.desktop:909` | desktop | SYSTEM | cli_owner, api, telegram_owner, voice_owner, automation | jarvis.security.permissions | yes |
| `desktop_scroll` | `jarvis.tools.desktop:835` | desktop | SYSTEM | cli_owner, api, telegram_owner, voice_owner, automation | jarvis.security.permissions | yes |
| `desktop_type` | `jarvis.tools.desktop:860` | desktop | SYSTEM | cli_owner, api, telegram_owner, voice_owner, automation | jarvis.security.permissions | yes |
| `desktop_windows` | `jarvis.tools.desktop:1303` | desktop | SYSTEM | cli_owner, api, telegram_owner, voice_owner, automation | jarvis.security.permissions | yes |
| `fetch_page` | `jarvis.tools.web_tools:337` | research,web | READ | cli_owner, api, telegram_owner, telegram_guest, voice_owner, automation | — | yes |
| `find_apps_tool` | `jarvis.tools.app_finder:735` | desktop,app_finder | READ | cli_owner, api, telegram_owner, telegram_guest, voice_owner, automation | — | **NO** |
| `find_file` | `jarvis.tools.file_tools:455` | desktop,files | READ | cli_owner, api, telegram_owner, telegram_guest, voice_owner, automation | — | **NO** |
| `find_files` | `jarvis.tools.file_tools:321` | desktop,files | READ | cli_owner, api, telegram_owner, telegram_guest, voice_owner, automation | jarvis.graph.verification, jarvis.graph.workers.offline | yes |
| `find_image` | `jarvis.tools.desktop:1116` | desktop | SYSTEM | cli_owner, api, telegram_owner, voice_owner, automation | jarvis.security.permissions | yes |
| `folder_file` | `jarvis.tools.file_tools:500` | desktop,files | READ | cli_owner, api, telegram_owner, telegram_guest, voice_owner, automation | jarvis.llm.demo, jarvis.tools.browser_tools | yes |
| `full_system_status` | `jarvis.tools.system_ops:381` | ops,system_ops | READ | cli_owner, api, telegram_owner, telegram_guest, voice_owner, automation | jarvis.graph.workers.offline | **NO** |
| `get_current_datetime` | `jarvis.tools.web_tools:350` | research,web | READ | cli_owner, api, telegram_owner, telegram_guest, voice_owner, automation | jarvis.llm.demo | yes |
| `get_weather` | `jarvis.tools.web_tools:358` | research,web | READ | cli_owner, api, telegram_owner, telegram_guest, voice_owner, automation | jarvis.llm.demo | **NO** |
| `google_search` | `jarvis.tools.web_tools:321` | research,web | READ | cli_owner, api, telegram_owner, telegram_guest, voice_owner, automation | jarvis.llm.demo | yes |
| `hold` | `jarvis.tools.desktop:1267` | desktop | SYSTEM | cli_owner, api, telegram_owner, voice_owner, automation | jarvis.security.permissions | yes |
| `install_package` | `jarvis.tools.code_engine:505` | code | DANGEROUS | cli_owner, telegram_owner | jarvis.runtime.agent, jarvis.security.permissions, jarvis.tools.browser_tools | yes |
| `key_down` | `jarvis.tools.desktop:1255` | desktop | SYSTEM | cli_owner, api, telegram_owner, voice_owner, automation | jarvis.security.permissions | yes |
| `key_up` | `jarvis.tools.desktop:1261` | desktop | SYSTEM | cli_owner, api, telegram_owner, voice_owner, automation | jarvis.security.permissions | yes |
| `list_apps` | `jarvis.tools.app_finder:882` | desktop,app_finder | READ | cli_owner, api, telegram_owner, telegram_guest, voice_owner, automation | — | **NO** |
| `list_automations` | `jarvis.automation.tools:51` | ops,automation | WRITE | cli_owner, api, telegram_owner, voice_owner, automation | — | yes |
| `list_dir` | `jarvis.tools.file_tools:707` | desktop,files | READ | cli_owner, api, telegram_owner, telegram_guest, voice_owner, automation | — | **NO** |
| `list_notes` | `jarvis.tools.notes_tools:158` | desktop,notes | WRITE | cli_owner, api, telegram_owner, voice_owner, automation | — | yes |
| `list_skills` | `jarvis.skills.loader:168` | skills | READ | cli_owner, api, telegram_owner, telegram_guest, voice_owner, automation | — | yes |
| `list_windows` | `jarvis.tools.system_tools:309` | desktop,system | SYSTEM | cli_owner, api, telegram_owner, voice_owner, automation | jarvis.security.permissions, jarvis.tools.desktop | **NO** |
| `manage_apps` | `jarvis.tools.app_finder:902` | desktop,app_finder | READ | cli_owner, api, telegram_owner, telegram_guest, voice_owner, automation | — | yes |
| `memory_compact` | `jarvis.memory.memory_tools:61` | memory | WRITE | cli_owner, api, telegram_owner, voice_owner, automation | jarvis.security.permissions | yes |
| `memory_stats` | `jarvis.memory.memory_tools:51` | memory | READ | cli_owner, api, telegram_owner, telegram_guest, voice_owner, automation | jarvis.api.server, jarvis.runtime.status | **NO** |
| `open_any_app` | `jarvis.tools.app_finder:753` | desktop,app_finder | SYSTEM | cli_owner, api, telegram_owner, voice_owner, automation | jarvis.graph.workers.desktop, jarvis.graph.workers.desktop_actions, jarvis.graph.workers.offline, jarvis.runtime.agent (+1) | yes |
| `open_app` | `jarvis.tools.system_tools:229` | desktop,system | SYSTEM | cli_owner, api, telegram_owner, voice_owner, automation | jarvis.graph.workers.desktop, jarvis.graph.workers.desktop_actions, jarvis.llm.demo, jarvis.runtime.agent (+1) | yes |
| `open_path` | `jarvis.tools.file_tools:467` | desktop,files | EXECUTE | cli_owner, telegram_owner, voice_owner, automation | jarvis.security.permissions | yes |
| `play_file` | `jarvis.tools.file_tools:485` | desktop,files | EXECUTE | cli_owner, telegram_owner, voice_owner, automation | jarvis.security.permissions | yes |
| `press_hotkey` | `jarvis.tools.system_tools:358` | desktop,system | SYSTEM | cli_owner, api, telegram_owner, voice_owner, automation | jarvis.graph.workers.desktop_actions, jarvis.security.permissions | yes |
| `process_input` | `jarvis.runtime.process_manager:299` | desktop,system,process | EXECUTE | cli_owner, telegram_owner, voice_owner, automation | jarvis.tools.browser_tools | **NO** |
| `process_list` | `jarvis.runtime.process_manager:266` | desktop,system,process | EXECUTE | cli_owner, telegram_owner, voice_owner, automation | — | **NO** |
| `process_logs` | `jarvis.runtime.process_manager:277` | desktop,system,process | EXECUTE | cli_owner, telegram_owner, voice_owner, automation | — | **NO** |
| `process_stop` | `jarvis.runtime.process_manager:282` | desktop,system,process | DANGEROUS | cli_owner, telegram_owner | jarvis.security.permissions, jarvis.tools.browser_tools | yes |
| `propose_self_improvement` | `jarvis.tools.improvement:36` | code | WRITE | cli_owner, api, telegram_owner, voice_owner, automation | jarvis.security.permissions | yes |
| `python_repl` | `jarvis.runtime.repl:386` | runtime,repl | EXECUTE | cli_owner, telegram_owner, voice_owner, automation | jarvis.security.permissions, jarvis.tools.browser_tools | yes |
| `read_file` | `jarvis.tools.file_tools:361` | desktop,files | READ | cli_owner, api, telegram_owner, telegram_guest, voice_owner, automation | jarvis.graph.verification, jarvis.graph.workers.offline | yes |
| `read_note` | `jarvis.tools.notes_tools:142` | desktop,notes | WRITE | cli_owner, api, telegram_owner, voice_owner, automation | — | yes |
| `read_skill` | `jarvis.skills.loader:173` | skills | READ | cli_owner, api, telegram_owner, telegram_guest, voice_owner, automation | — | yes |
| `recall_facts` | `jarvis.memory.memory_tools:28` | memory | READ | cli_owner, api, telegram_owner, telegram_guest, voice_owner, automation | jarvis.llm.demo | **NO** |
| `recent_files` | `jarvis.tools.file_tools:441` | desktop,files | READ | cli_owner, api, telegram_owner, telegram_guest, voice_owner, automation | jarvis.memory.session_state | **NO** |
| `refresh_app_index` | `jarvis.tools.app_finder:895` | desktop,app_finder | WRITE | cli_owner, api, telegram_owner, voice_owner, automation | jarvis.security.permissions | yes |
| `refresh_system_tool` | `jarvis.tools.system_ops:341` | ops,system_ops | SYSTEM | cli_owner, telegram_owner | jarvis.graph.workers.offline | **NO** |
| `reload_config_tool` | `jarvis.tools.system_ops:374` | ops,system_ops | WRITE | cli_owner, api, telegram_owner, voice_owner, automation | jarvis.security.permissions | yes |
| `remember_fact` | `jarvis.memory.memory_tools:17` | memory | WRITE | cli_owner, api, telegram_owner, voice_owner, automation | jarvis.graph.workers.memory, jarvis.llm.demo, jarvis.memory.store, jarvis.runtime.agent (+1) | yes |
| `repl_reset` | `jarvis.runtime.repl:426` | runtime,repl | READ | cli_owner, api, telegram_owner, telegram_guest, voice_owner, automation | — | **NO** |
| `repl_state` | `jarvis.runtime.repl:418` | runtime,repl | READ | cli_owner, api, telegram_owner, telegram_guest, voice_owner, automation | — | **NO** |
| `restart_jarvis_tool` | `jarvis.tools.system_ops:367` | ops,system_ops | DANGEROUS | cli_owner, telegram_owner | jarvis.security.permissions, jarvis.tools.browser_tools | yes |
| `run_automation` | `jarvis.automation.tools:64` | ops,automation | WRITE | cli_owner, api, telegram_owner, voice_owner, automation | — | **NO** |
| `run_python` | `jarvis.tools.code_engine:459` | code | EXECUTE | cli_owner, telegram_owner, voice_owner, automation | jarvis.security.permissions, jarvis.tools.browser_tools | yes |
| `run_python_isolated` | `jarvis.runtime.repl:410` | runtime,repl | EXECUTE | cli_owner, telegram_owner, voice_owner, automation | jarvis.tools.browser_tools | **NO** |
| `run_script_file` | `jarvis.tools.code_engine:480` | code | EXECUTE | cli_owner, telegram_owner, voice_owner, automation | jarvis.tools.browser_tools | yes |
| `run_shell_command` | `jarvis.tools.system_tools:466` | desktop,system | EXECUTE | cli_owner, telegram_owner, voice_owner, automation | jarvis.tools.browser_tools | yes |
| `save_note` | `jarvis.tools.notes_tools:68` | desktop,notes | WRITE | cli_owner, api, telegram_owner, voice_owner, automation | jarvis.graph.workers.desktop, jarvis.graph.workers.offline, jarvis.llm.demo, jarvis.tools.browser_tools | yes |
| `search_inside_files` | `jarvis.tools.file_tools:390` | desktop,files | READ | cli_owner, api, telegram_owner, telegram_guest, voice_owner, automation | — | yes |
| `search_notes` | `jarvis.tools.notes_tools:168` | desktop,notes | WRITE | cli_owner, api, telegram_owner, voice_owner, automation | — | **NO** |
| `session_context` | `jarvis.memory.session_state:285` | runtime,session | READ | cli_owner, api, telegram_owner, telegram_guest, voice_owner, automation | — | **NO** |
| `session_note` | `jarvis.memory.session_state:299` | runtime,session | WRITE | cli_owner, api, telegram_owner, voice_owner, automation | — | yes |
| `set_pending_steps` | `jarvis.memory.session_state:305` | runtime,session | WRITE | cli_owner, api, telegram_owner, voice_owner, automation | — | yes |
| `set_preference` | `jarvis.memory.session_state:311` | runtime,session | WRITE | cli_owner, api, telegram_owner, voice_owner, automation | — | yes |
| `set_workspace` | `jarvis.memory.session_state:290` | runtime,session | WRITE | cli_owner, api, telegram_owner, voice_owner, automation | — | yes |
| `start_background_process` | `jarvis.runtime.process_manager:240` | desktop,system,process | EXECUTE | cli_owner, telegram_owner, voice_owner, automation | jarvis.tools.browser_tools | **NO** |
| `system_info` | `jarvis.tools.system_tools:544` | desktop,system | SYSTEM | cli_owner, telegram_owner | jarvis.security.permissions | yes |
| `take_screenshot` | `jarvis.tools.system_tools:418` | desktop,system | SYSTEM | cli_owner, api, telegram_owner, voice_owner, automation | jarvis.graph.workers.desktop_actions, jarvis.security.permissions | **NO** |
| `tg_send_buttons` | `jarvis.telegram.tools:89` | telegram | WRITE | cli_owner, api, telegram_owner, voice_owner, automation | jarvis.security.permissions | yes |
| `toggle_automation` | `jarvis.automation.tools:57` | ops,automation | WRITE | cli_owner, api, telegram_owner, voice_owner, automation | — | **NO** |
| `type_text` | `jarvis.tools.system_tools:328` | desktop,system | SYSTEM | cli_owner, api, telegram_owner, voice_owner, automation | jarvis.graph.workers.desktop, jarvis.graph.workers.desktop_actions, jarvis.graph.workers.offline, jarvis.runtime.agent (+2) | yes |
| `undo_file_operation` | `jarvis.tools.file_undo:211` | desktop,files | WRITE | cli_owner, api, telegram_owner, voice_owner, automation | jarvis.graph.workers.offline, jarvis.security.permissions | yes |
| `wait_for_text` | `jarvis.tools.desktop:1032` | desktop | SYSTEM | cli_owner, api, telegram_owner, voice_owner, automation | jarvis.security.permissions | yes |
| `write_file` | `jarvis.tools.file_tools:670` | desktop,files | WRITE | cli_owner, api, telegram_owner, voice_owner, automation | jarvis.graph.workers.desktop, jarvis.graph.workers.offline, jarvis.tools.browser_tools | yes |

### 1a. Tools koi worker/executor directly nahi chala sakta (4)

Ye tools registry me hain, par kisi bhi worker/executor ke code path me nazar
nahi aate (na `tools_for(...)` group se, na kisi `_call("name")` se, na kisi
SKILL.md se). LLM unhe tabhi chala sakta hai jab ReAct sub-agent ko poora
tool set bind ho — deterministic path se ye **unreachable** hain.

- `browser_cli` (`jarvis.tools.browser_tools`)
- `browser_close` (`jarvis.tools.browser_tools`)
- `run_python` (`jarvis.tools.code_engine`)
- `tg_send_buttons` (`jarvis.telegram.tools`)

### 1b. Tools jinhe koi test nahi chhuta (29)

`append_note`, `browser_screenshot`, `cancel_automation`, `clear_process_tool`, `control_volume`, `conversation_history`, `find_apps_tool`, `find_file`, `full_system_status`, `get_weather`, `list_apps`, `list_dir`, `list_windows`, `memory_stats`, `process_input`, `process_list`, `process_logs`, `recall_facts`, `recent_files`, `refresh_system_tool`, `repl_reset`, `repl_state`, `run_automation`, `run_python_isolated`, `search_notes`, `session_context`, `start_background_process`, `take_screenshot`, `toggle_automation`

## 2. Module graph

### 2a. Dead modules — jinhe koi import nahi karta (0)

Koi dead module nahi — har module kahin na kahin import hota hai.

### 2b. Import cycles — module-level, import-breaking (0)

Koi module-level cycle nahi — `import jarvis...` sab jagah safe hai.

### 2b-2. Lazy cycles — function-level imports only (1)

Ye sirf function/method ke andar ke `import` se bane hain. Python inhe tolerate
karta hai (import time par cycle nahi banta), par ye coupling smell hai — agar
inme se koi bhi import top-level par chala gaya to `import jarvis` toot jayega.

Sabse chhote (fix karne layak) cycles:

- `jarvis.config.runtime` → `jarvis.runtime.controls` → `jarvis.config.runtime`  (2 modules)

Poore strongly-connected components: 1; sabse bade me 56 modules.

### 2c. Files > 600 lines (13)

| module | lines |
|---|---|
| `jarvis.tools.desktop` | 1353 |
| `jarvis.runtime.agent` | 1237 |
| `jarvis.tools.browser_tools` | 1024 |
| `jarvis.telegram.bot` | 974 |
| `jarvis.security.gate` | 931 |
| `jarvis.tools.app_finder` | 924 |
| `jarvis.cli.app` | 838 |
| `jarvis.api.server` | 832 |
| `jarvis.memory.store` | 811 |
| `jarvis.tools.file_tools` | 728 |
| `jarvis.automation.scheduler` | 719 |
| `jarvis.config.settings` | 700 |
| `jarvis.voice.livekit_agent` | 615 |

### 2d. Duplicate capability implementations (0)

Koi duplicate capability nahi: har capability ki ek hi asli implementation hai (baaki sirf usi ko delegate karte hain).

## 3. Surface × capability matrix

Yeh matrix **code path** se nikali hai: pehle dekha ki kaun sa surface module
`jarvis.runtime.agent` (unified runner) tak pahunchta hai, phir har capability ke
representative tools par asli permission engine (`jarvis/security/permissions.py`)
chalaya. Har blocked cell ke saath blocking policy likhi hai.

| surface | entry module | reaches `runtime.agent` | reaches `graph.build` |
|---|---|---|---|
| cli | `jarvis.cli.app`, `jarvis.cli.pro_ui`, `jarvis.runtime.repl` | yes | yes |
| web | `jarvis.api.server` | yes | yes |
| telegram | `jarvis.telegram.bot`, `jarvis.telegram.live` | yes | yes |
| voice | `jarvis.voice.livekit_agent`, `jarvis.voice.speech` | yes | yes |
| scheduled | `jarvis.automation.scheduler` | yes | yes |

| capability | cli_owner | api | telegram_owner | telegram_guest | voice_owner | automation |
|---|---|---|---|---|---|---|
| app-open | ✅ allowed | ✅ allowed | ✅ allowed | 🚫 blocked | ✅ allowed | ✅ allowed |
| keyboard | ✅ allowed | ✅ allowed | ✅ allowed | 🚫 blocked | ✅ allowed | ✅ allowed |
| mouse | ✅ allowed | ✅ allowed | ✅ allowed | 🚫 blocked | ✅ allowed | ✅ allowed |
| browser | ✅ allowed | 🚫 blocked | ✅ allowed | 🚫 blocked | ✅ allowed | ✅ allowed |
| files | ✅ allowed | 🟡 partial | ✅ allowed | 🟡 partial | ✅ allowed | ✅ allowed |
| shell | ✅ allowed | 🚫 blocked | ✅ allowed | 🚫 blocked | 🟡 partial | 🟡 partial |
| schedule | ✅ allowed | ✅ allowed | ✅ allowed | 🚫 blocked | ✅ allowed | ✅ allowed |
| memory | ✅ allowed | ✅ allowed | ✅ allowed | 🟡 partial | ✅ allowed | ✅ allowed |

### 3a. Blocked cells — exact policy

- **app-open** on **telegram_guest**: `'close_app' = SYSTEM level, par 'telegram_guest' ki limit READ hai; 'list_windows' = SYSTEM level, par 'telegram_guest' ki limit READ hai; 'open_any_app' = SYSTEM level, par 'telegram_guest' ki limit READ hai; 'open_app' = SYSTEM level, par 'telegram_guest' ki limit READ hai`
- **keyboard** on **telegram_guest**: `'desktop_press' = SYSTEM level, par 'telegram_guest' ki limit READ hai; 'desktop_type' = SYSTEM level, par 'telegram_guest' ki limit READ hai; 'hold' = SYSTEM level, par 'telegram_guest' ki limit READ hai; 'key_down' = SYSTEM level, par 'telegram_guest' ki limit READ hai; 'key_up' = SYSTEM level, par 'telegram_guest' ki limit READ hai; 'press_hotkey' = SYSTEM level, par 'telegram_guest' ki limit READ hai; 'type_text' = SYSTEM level, par 'telegram_guest' ki limit READ hai`
- **mouse** on **telegram_guest**: `'click_text' = SYSTEM level, par 'telegram_guest' ki limit READ hai; 'desktop_click' = SYSTEM level, par 'telegram_guest' ki limit READ hai; 'desktop_drag' = SYSTEM level, par 'telegram_guest' ki limit READ hai; 'desktop_move' = SYSTEM level, par 'telegram_guest' ki limit READ hai; 'desktop_scroll' = SYSTEM level, par 'telegram_guest' ki limit READ hai; 'find_image' = SYSTEM level, par 'telegram_guest' ki limit READ hai`
- **browser** on **api**: `'browser_cli' = EXECUTE level, par 'api' ki limit WRITE hai; 'browser_close' = EXECUTE level, par 'api' ki limit WRITE hai; 'browser_doctor' = EXECUTE level, par 'api' ki limit WRITE hai; 'browser_research' = EXECUTE level, par 'api' ki limit WRITE hai; 'browser_screenshot' = EXECUTE level, par 'api' ki limit WRITE hai; 'browser_task' = EXECUTE level, par 'api' ki limit WRITE hai`
- **browser** on **telegram_guest**: `'browser_cli' = EXECUTE level, par 'telegram_guest' ki limit READ hai; 'browser_close' = EXECUTE level, par 'telegram_guest' ki limit READ hai; 'browser_doctor' = EXECUTE level, par 'telegram_guest' ki limit READ hai; 'browser_research' = EXECUTE level, par 'telegram_guest' ki limit READ hai; 'browser_screenshot' = EXECUTE level, par 'telegram_guest' ki limit READ hai; 'browser_task' = EXECUTE level, par 'telegram_guest' ki limit READ hai`
- **files** on **api**: `allowed: read_file=READ, write_file=WRITE, find_file=READ, find_files=READ, list_dir=READ, undo_file_operation=WRITE, search_inside_files=READ | blocked: open_path=EXECUTE`
- **files** on **telegram_guest**: `allowed: read_file=READ, find_file=READ, find_files=READ, list_dir=READ, search_inside_files=READ | blocked: write_file=WRITE, open_path=EXECUTE, undo_file_operation=WRITE`
- **shell** on **api**: `'install_package' = DANGEROUS level, par 'api' ki limit WRITE hai; 'python_repl' = EXECUTE level, par 'api' ki limit WRITE hai; 'run_python' = EXECUTE level, par 'api' ki limit WRITE hai; 'run_python_isolated' = EXECUTE level, par 'api' ki limit WRITE hai; 'run_script_file' = EXECUTE level, par 'api' ki limit WRITE hai; 'run_shell_command' = EXECUTE level, par 'api' ki limit WRITE hai`
- **shell** on **telegram_guest**: `'install_package' = DANGEROUS level, par 'telegram_guest' ki limit READ hai; 'python_repl' = EXECUTE level, par 'telegram_guest' ki limit READ hai; 'run_python' = EXECUTE level, par 'telegram_guest' ki limit READ hai; 'run_python_isolated' = EXECUTE level, par 'telegram_guest' ki limit READ hai; 'run_script_file' = EXECUTE level, par 'telegram_guest' ki limit READ hai; 'run_shell_command' = EXECUTE level, par 'telegram_guest' ki limit READ hai`
- **shell** on **voice_owner**: `allowed: run_shell_command=EXECUTE, python_repl=EXECUTE, run_python=EXECUTE, run_python_isolated=EXECUTE, run_script_file=EXECUTE | blocked: install_package=DANGEROUS`
- **shell** on **automation**: `allowed: run_shell_command=EXECUTE, python_repl=EXECUTE, run_python=EXECUTE, run_python_isolated=EXECUTE, run_script_file=EXECUTE | blocked: install_package=DANGEROUS`
- **schedule** on **telegram_guest**: `'cancel_automation' = WRITE level, par 'telegram_guest' ki limit READ hai; 'create_automation' = WRITE level, par 'telegram_guest' ki limit READ hai; 'list_automations' = WRITE level, par 'telegram_guest' ki limit READ hai; 'run_automation' = WRITE level, par 'telegram_guest' ki limit READ hai; 'toggle_automation' = WRITE level, par 'telegram_guest' ki limit READ hai`
- **memory** on **telegram_guest**: `allowed: recall_facts=READ, memory_stats=READ, conversation_history=READ | blocked: remember_fact=WRITE, memory_compact=WRITE`

Policy source: `jarvis/security/permissions.py` — `LEVELS = READ < WRITE < EXECUTE < SYSTEM < DANGEROUS`; har surface ka `max_level` `SURFACES` dict me hai; `OWNER_DESKTOP_SYSTEM_TOOLS` sirf api/voice/automation par desktop-control ko SYSTEM level allow karta hai.

## 4. Prompt / skill / tool consistency

| check | result |
|---|---|
| SKILL.md files discovered | 18 |
| loader scans whole catalog (`rglob('SKILL.md')`) | yes |
| SKILL.md naming a missing tool | 0 |
| prompt modules present | 1 |
| prompt functions found | 4 |
| date/time/weekday/city/weather smells in prompts | 0 |

### 4a. Skill inventory

| skill | declared tools | unknown | keywords |
|---|---|---|---|
| `app_launcher` | `list_apps`, `find_apps_tool`, `open_app`, `open_any_app`, `manage_apps` | — | yes |
| `automation_and_scheduling` | `get_current_datetime`, `create_automation`, `list_automations`, `toggle_automation`, `run_automation`, `cancel_automation` | — | yes |
| `automation_setup` | `create_automation`, `list_automations`, `toggle_automation`, `cancel_automation`, `run_automation` | — | yes |
| `browser_supervision` | `browser_task`, `browser_research`, `browser_screenshot`, `browser_cli`, `browser_doctor`, `google_search`, `deep_research`, `fetch_page`, `save_note` | — | yes |
| `code_engine` | `code_engine`, `run_python`, `run_python_isolated`, `python_repl`, `write_file`, `run_shell_command`, `propose_self_improvement` | — | yes |
| `data_extraction_and_reports` | `google_search`, `fetch_page`, `browser_research`, `read_file`, `write_file`, `code_engine`, `run_script_file`, `save_note` | — | yes |
| `deep_research` | `google_search`, `deep_research`, `fetch_page`, `browser_research`, `write_file`, `save_note` | — | yes |
| `desktop_browser_ops` | `open_any_app`, `desktop_observe`, `desktop_focus`, `desktop_click`, `desktop_type`, `desktop_press`, `wait_for_text`, `browser_task`, `browser_screenshot` | — | yes |
| `desktop_control` | `desktop_observe`, `desktop_ocr`, `desktop_click`, `desktop_move`, `desktop_scroll`, `desktop_type`, `desktop_press`, `desktop_windows`, `desktop_focus`, `desktop_clipboard_read`, `desktop_clipboard_write` | — | yes |
| `file_management` | `find_files`, `find_file`, `list_dir`, `search_inside_files`, `read_file`, `write_file`, `folder_file`, `undo_file_operation`, `recent_files`, `save_note`, `read_note`, `list_notes`, `search_notes` | — | yes |
| `gst_invoice` | `find_files`, `read_file`, `code_engine`, `write_file`, `save_note` | — | yes |
| `memory_management` | `recall_facts`, `remember_fact`, `memory_stats`, `memory_compact`, `conversation_history` | — | yes |
| `project_builder` | `create_project`, `find_files`, `read_file`, `write_file`, `code_engine`, `run_script_file`, `run_shell_command`, `search_inside_files` | — | yes |
| `self_review` | `find_files`, `read_file`, `search_inside_files`, `run_shell_command`, `propose_self_improvement`, `get_current_datetime` | — | yes |
| `task_verification` | `find_files`, `read_file`, `search_inside_files`, `run_shell_command`, `desktop_observe`, `wait_for_text`, `get_current_datetime` | — | yes |
| `terminal_ops` | `run_shell_command`, `start_background_process`, `process_list`, `process_logs`, `process_input`, `process_stop`, `get_current_datetime` | — | yes |
| `web_research` | `google_search`, `deep_research`, `fetch_page`, `get_weather`, `get_current_datetime` | — | yes |
| `website_data_collection` | `browser_research`, `browser_task`, `deep_research`, `google_search`, `fetch_page`, `save_note`, `find_files` | — | yes |

## 5. Shipped-code smells (TODO / NotImplementedError)

Koi TODO/FIXME/NotImplementedError shipped code me nahi.

