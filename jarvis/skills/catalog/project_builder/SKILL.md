---
name: project_builder
description: "Build small maintainable projects from explicit acceptance criteria, then run and inspect their tests"
keywords: [project, scaffold, build app, package, implement feature, create project, repo]
tools: [create_project, find_files, read_file, write_file, code_engine, run_script_file, run_shell_command, search_inside_files]
---

# Project-building workflow

1. Convert the request into acceptance criteria and a short file plan. Inspect the existing project before scaffolding; preserve its conventions and avoid replacing unrelated files.
2. Use `create_project` for a new scaffold only when it matches the requested stack; otherwise edit the existing tree with `write_file`/`code_engine`.
3. Build the smallest coherent implementation. Keep secrets in environment variables, avoid unrelated dependencies, and do not add hidden networking, sandboxing, or permission changes.
4. Run the project’s documented test/build command using `run_script_file` or `run_shell_command`. The command gate applies. Inspect failures and relevant files with `read_file`/`search_inside_files` before making a targeted fix.
5. Verify output artifacts exist and match the acceptance criteria with `find_files` and `read_file`. A successful generator process alone is not proof of a correct deliverable.
6. Retry only failed acceptance criteria; retain successful work. Report changed files, exact tests/build result, and any untested live integration.
