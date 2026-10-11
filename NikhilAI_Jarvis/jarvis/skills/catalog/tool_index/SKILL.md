---
name: tool_index
description: "Jarvis ke saare tools ka index — kaun sa tool kab use karna hai, aur permission rules"
keywords: [tool, tools, skill, skills, kaise, kya kar sakte, capabilities, help, madad, which tool, konsa tool]
tools: [list_skills, read_skill]
---

# Tool index (sab tools ek jagah)

Har tool group ki detailed skill hai — `read_skill("tool_<group>")` se padho:

- tool_desktop_apps — open_app, close_app, list_windows, type_text, press_hotkey, control_volume, take_screenshot, open_any_app, find_apps_tool, list_apps, refresh_app_index, system_info
- tool_files — find_files, find_file, read_file, search_inside_files, recent_files, list_dir, write_file, folder_file, open_path, play_file
- tool_notes — save_note, append_note, read_note, list_notes, search_notes, jarvis_save_note
- tool_web_research — google_search, fetch_page, deep_research, get_current_datetime, get_weather
- tool_browser — browser_task, browser_research, browser_cli, browser_screenshot, browser_close, browser_doctor
- tool_code — code_engine, run_python, run_script_file, install_package, create_project, python_repl, run_python_isolated, repl_state, repl_reset
- tool_shell_processes — run_shell_command, grant_exec_approval, start_background_process, process_list, process_logs, process_stop, process_input, clear_process_tool
- tool_memory — remember_fact, recall_facts, conversation_history, memory_stats, memory_compact, jarvis_remember, set_preference
- tool_session — session_context, set_workspace, session_note, set_pending_steps, jarvis_progress
- tool_automation — create_automation, list_automations, toggle_automation, run_automation, cancel_automation
- tool_system_ops — refresh_system_tool, clear_cache_tool, reload_config_tool, restart_jarvis_tool, full_system_status
- tool_voice — current_time, agent_task, remember, forget (voice agent ke tools)
- tool_skills — list_skills, read_skill

## General rules
1. Pehle sahi tool chuno, phir call karo. Guess mat karo — jo tool task ko seedha karta ho wahi.
2. Tool call kiye bina "ho gaya" mat bolo. Result check karke hi confirm karo.
3. Permission levels: READ < WRITE < EXECUTE < SYSTEM < DANGEROUS. Har surface (CLI, Telegram, voice, API) ki max level alag hai. Level se upar wala tool blocked hoga — uski jagah shell se bypass mat karo.
4. Delete/kill/uninstall jaise DANGEROUS kaam owner ki approval ke bina nahi.
