---
name: tool_code
description: "Code likhna, chalana aur fix karna; Python snippets, scripts, packages install, project scaffold, persistent REPL"
keywords: [code, coding, python, script, program, app banao, project, pip, package, install, library, calculate, calculation, hisaab, data, pandas, repl, debug, fix code, error, bug, generate code]
tools: [code_engine, run_python, run_script_file, install_package, create_project, python_repl, run_python_isolated, repl_state, repl_reset]
---

# Code tools (EXECUTE level)

- code_engine(task, language, run, save_as) — poora loop: code likho → chalao → error ho to traceback padh kar fix. Naye script ke liye best.
- run_python(code, timeout) — chhota snippet abhi chalao (calculation, data check).
- run_script_file(path, script_args) — workspace ki existing script chalao.
- install_package(package) — pip install (pandas, requests, playwright).
- create_project(name, description, language) — starter project folder scaffold.
- python_repl(code, timeout) — persistent REPL; variables session me yaad rehte hain.
- run_python_isolated(code, timeout) — fresh subprocess; side effects ya crash-prone code ke liye.
- repl_state() / repl_reset() — REPL variables dekho / clear karo.

## Rules
- Code chalane se pehle samajh lo wo kya karega; file delete/overwrite karne wala code owner se puchh kar.
- Error aaye to traceback padho, phir fix karo — same code bina change ke dobara mat chalao.
- Output verify karke hi result batao.
