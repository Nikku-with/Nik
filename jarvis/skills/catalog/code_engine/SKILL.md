---
name: code_engine
description: "Code ko chhote steps me likhna, chalana, error inspect karna aur verified output dena"
keywords: [code, script, python, program, coding, bug, fix, test, calculate]
tools: [code_engine, run_python, run_python_isolated, python_repl, write_file, run_shell_command, propose_self_improvement]
---

# Code task workflow

1. Acceptance criteria aur expected output pehle likho; existing files edit karne se pehle relevant source/tests read karo.
2. Smallest useful change `code_engine` ya `write_file` se banao. Persistent variables ki zaroorat ho tab `python_repl`; one-shot analysis ke liye `run_python_isolated`.
3. Generated code ko run karne se pehle inspect/validate karo. Shell-out (direct ya nested) dangerous ho to human Gate approval ke bina execute nahi hoga.
4. Relevant test/command run karke actual output verify karo. Timeout/error/approval denial ko success mat mark karo; logs me secrets mat print karo.
5. Source-code self-improvement request par `propose_self_improvement` se unified diff ko workspace proposal ke roop me save karo. Ye tool source edit ya code execute nahi karta; owner review, tests, aur manual application se pehle proposal ko done mat bolo.
6. User ko changed files, test command/result, aur koi unverified edge case batayo.
