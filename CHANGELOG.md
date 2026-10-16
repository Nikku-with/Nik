# Changelog

Notable user-facing changes are tracked here. Round 2 is still in progress; this is not a release announcement.

## [Unreleased] — Round 2 audit

### Added

- Four registry-backed workflow skills: `task_verification`, `terminal_ops`, `project_builder`, and `self_review`. The delivery tree adds the remaining requested workflows: `deep_research`, `desktop_browser_ops`, `data_extraction_and_reports`, and `automation_and_scheduling`; the 18-skill registry test passes in the final full suite.
- Requirement-ledger verification/refinement coverage, time/date and chat fast-path coverage, advanced-memory cases, expanded desktop interactions, and Gate/surface regression coverage. See [`docs/STATUS.md`](docs/STATUS.md) for current evidence and remaining work.
- Sensitive-memory saves/updates remain staged until explicit consent. API confirmation is tied to an exact pending ID; Telegram exposes pending IDs plus confirm/cancel commands. Ambiguous multi-item replies no longer resolve the newest item by default.
- Natural-language memory forgetting strips common command/category phrasing before matching, retains other facts, and cleans the FTS entry for the deleted fact.
- Added CLI `/trace [limit]` for the last run's wall-time and model/tool spans. The report uses sanitized progress metadata and excludes request text, prompts, arguments, outputs, and reasoning content.
- File saves now require exact readback verification before success; the desktop workflow independently checks file bytes and returns a saved path as an artifact. Offline/demo writes require explicit file name and content, and a mismatch rolls back.
- Added consistent CLI/Telegram mode, model, provider, and `/gate` controls. Status separates saved preferences from the active interface/route, reports the effective Gate source, surfaces missing provider keys, and never implies a live connectivity check.

### Changed

- Reorganized technical documentation into `docs/`; combined status, capability notes, and the historical tool-audit record in [`docs/STATUS.md`](docs/STATUS.md).
- Updated README links to the canonical architecture, setup, status, and changelog files.
- Made the pytest fixture configure a temporary workspace before Jarvis imports; the final Round 2 full-suite, standalone-harness, and fresh-archive verification is recorded below.

### Verification state

- Final code/test commit `4387cce`: task-first English/Hinglish classification is covered by 59 task and 83 conversational phrases; the offline notes graph harness passes 97/97; the tool-invariant harness passes 49/49 with two run-agent integration checks; all seven acceptance harnesses pass (423/423 checks); `ruff check .` and compileall pass.
- `pytest -q` exited successfully; the count-confirming `pytest -q -o addopts=''` run passed 355 tests with four FastAPI lifecycle-hook deprecation warnings. A fresh extraction of the `git archive` release ZIP also passed the full suite and compileall. The ZIP omits `.git`, `.env`, generated `nikhilai.egg-info`, and runtime workspace data. No live cloud, Telegram account, LiveKit, desktop, browser, or production-scheduler integration is claimed.
