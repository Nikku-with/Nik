---
name: task_verification
description: "Turn a request into R-items, gather direct evidence for each, and never mark unresolved work complete"
keywords: [verify, verification, done, evidence, checklist, multi-step, requirement, acceptance criteria, proof]
tools: [find_files, read_file, search_inside_files, run_shell_command, desktop_observe, wait_for_text, get_current_datetime]
---

# Evidence-first task verification

1. Rewrite the request into `R1..Rn`. Keep each item atomic and state what observable evidence would satisfy it.
2. Before a state-changing action, inspect current state with the closest registered read/observe tool (`find_files`, `read_file`, `search_inside_files`, `desktop_observe`, or a relevant status tool). Record the result; do not infer it.
3. Perform one action at a time with a registered tool. Keep tool arguments scoped to the exact requested item. For shell commands, obey the Dangerous-Command Gate; never supply model-generated approval.
4. Verify the postcondition with a fresh read, screenshot/OCR, process log, or test result. A command returning zero is not enough when the user requested a resulting file/UI state.
5. Assign each item exactly one status: `done(evidence)`, `failed(reason)`, or `needs_user(question)`. Never report the overall task as done while any item is unresolved.
6. Retry only failed/unverified items, once the cause is understood. Reuse successful action evidence and do not repeat successful side effects.
7. Final response: list each R-item and its evidence/failure/question; separate completed work from assumptions and live integrations not exercised.
