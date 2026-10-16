---
name: self_review
description: "Review a proposed change against requirements, tests, regressions, security, and truthful reporting"
keywords: [self review, review code, audit, inspect diff, regressions, quality, release check]
tools: [find_files, read_file, search_inside_files, run_shell_command, propose_self_improvement, get_current_datetime]
---

# Self-review checklist

1. Restate the acceptance criteria as `R1..Rn`; inspect the changed files and relevant tests before forming a verdict.
2. Check behavior, failure paths, regressions, permission/Gate boundaries, and evidence for success claims. Look for dead code, secret exposure, accidental workspace files, and unsupported live-integration claims.
3. Run only relevant tests first, then the broader suite if requested. Record exact command, pass/fail count, and warnings. Do not call an unrun test “passing.”
4. If a code change is needed, propose the smallest patch through `propose_self_improvement`; that tool creates a reviewable proposal only and does not edit source. Do not apply or claim it applied.
5. Recheck every R-item after changes. Each gets `done(evidence)`, `failed(reason)`, or `needs_user(question)`; any unresolved item blocks an overall “done.”
6. Return prioritized findings first, then tests, residual risks, and any live integration that was not exercised.
