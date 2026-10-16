# Skill catalog

This directory contains workflow-only `SKILL.md` files. A skill may describe tool sequences, checks, and reporting rules; it is not executable code and cannot register tools.

## Catalog

- `gst_invoice` — invoice generation with source-backed values and verified totals.
- `website_data_collection` — supervised multi-site research and note saving.
- `desktop_control` — observe, act, verify desktop interactions.
- `app_launcher` — memory-aware app lookup and verified launch.
- `file_management` — find, read, write, and organize files.
- `browser_supervision` — bounded browser tasks with progress and failure isolation.
- `web_research` — sourced research and date-aware reporting.
- `automation_setup` — parse, confirm, persist, and monitor schedules.
- `memory_management` — explicit-consent memory recall and updates.
- `code_engine` — implement, run, and verify code tasks.
- `task_verification` — R1..Rn evidence states, unresolved-item blocking, and targeted retries.
- `terminal_ops` — gated command execution and process/log verification.
- `project_builder` — requirements-driven scaffolding, build/test, and artifact verification.
- `self_review` — evidence-based requirement, regression, Gate, and disclosure review.
- `deep_research` — multi-source investigation with quoted passages, dates, conflict handling, and `sources.json`.
- `desktop_browser_ops` — observed and permission-aware browser/desktop actions with per-step verification.
- `data_extraction_and_reports` — normalized source-backed data extraction and report verification.
- `automation_and_scheduling` — owner-confirmed schedule lifecycle, unique runs, coalescing, and execution-time Gate checks.

The ten required workflow packs are present alongside the existing specialized packs. Each YAML-style frontmatter `tools` entry must match a registered Jarvis tool. `tests/test_skills_registry.py` enforces this and checks catalog loading/refresh behavior; its current post-edit result has not been run.

## Add a skill

Create `jarvis/skills/catalog/<snake_case>/SKILL.md` with frontmatter fields `name`, `description`, `keywords`, and `tools`, followed by a concrete workflow. Add only tool names from `jarvis.tools.tool_names()` and run:

```bash
pytest -q tests/test_skills_registry.py
```
