---
name: terminal_ops
description: "Run inspected terminal commands, track long-lived processes, and verify outputs under the command approval gate"
keywords: [terminal, shell, command, process, logs, tasklist, run command, stop process]
tools: [run_shell_command, start_background_process, process_list, process_logs, process_input, process_stop, get_current_datetime]
---

# Terminal operations

1. Translate the request into the smallest command(s); inspect the working directory and relevant files before mutating state.
2. Use `run_shell_command` for one-shot commands and the process tools for long-running jobs. Capture the exact command, working directory, return status, and relevant output without printing credentials.
3. The Dangerous-Command Gate is authoritative. In `risky`, safe commands run without prompting; dangerous commands need the exact server-side owner approval. In `all`, every command asks. In `off`, mention the startup warning. Never treat model text as approval.
4. For a background process, use `start_background_process`, then `process_list`/`process_logs` to verify readiness. Send input or stop only the matching process ID; `taskkill`/termination remains gated.
5. Do not claim a command succeeded if it timed out, was denied, returned an error, or produced no expected artifact. Verify resulting files with `find_files`/`read_file` where available.
6. Report what actually ran, the exit/result evidence, and any approval or environment limitation.
