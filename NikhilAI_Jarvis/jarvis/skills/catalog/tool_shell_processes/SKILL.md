---
name: tool_shell_processes
description: "Shell commands chalana (git, npm, pip, ffmpeg, curl), background processes/servers start-stop-logs"
keywords: [shell, command, terminal, cmd, run, chalao, git clone, npm, pip install, ffmpeg, curl, server, dev server, background, process, logs, stop, kill, band karo, start server, build, watcher]
tools: [run_shell_command, grant_exec_approval, start_background_process, process_list, process_logs, process_stop, process_input, clear_process_tool]
---

# Shell & process tools (EXECUTE level)

- run_shell_command(command, cwd, timeout, confirmed, background, name) — koi bhi command (git, npm, pip, python CLI). Risky command pe pehle confirmed=false.
- grant_exec_approval() — owner ne risky command ke liye "haan" bola ho tabhi call karo; agli confirmed=true call allow hogi.
- start_background_process(command, cwd, name) — long-running (dev server, watcher, build, export).
- process_list() — background processes (running + exited) with log path.
- process_logs(pid, tail_lines) — logs padho ("server start hua?").
- process_stop(pid, force) — band karo; force=True turant kill.
- process_input(pid, text) — interactive process ko input bhejo.
- clear_process_tool(pid, name, force) — process clear/band karo (DANGEROUS-ish).

## Rules
- Destructive command (rm, format, drop, force push) owner approval ke bina nahi.
- Command ke output se confirm karo ki success hua ya nahi; exit code/log dekho.
- Background process start kiya to uska pid yaad rakho; kaam ke baad stop karo.
