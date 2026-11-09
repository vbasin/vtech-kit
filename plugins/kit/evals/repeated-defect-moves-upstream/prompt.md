---
description: A defect category that keeps recurring after a reviewer lesson is moved upstream to a write-time step or a check, and the change is scored on PRs it was not written from.
tags: [learning, review]
max_turns: 10
timeout_seconds: 360
allowed_tools: [Read, Glob, Grep, Skill, TodoWrite]
expected_outcome: The lesson moves one stage upstream (a write-time rule or, better, a test or shared helper), not another reviewer line, with a held-out replay to show it works.
---

Copilot keeps catching the same kind of bug in our PRs. Two months ago, after six PRs of
it, we added an "every form of the input" pass to our pre-PR reviewer. Three PRs since
then still had it: a TOML parser that missed `key = "..."`
written with single quotes, a YAML front-matter reader that missed folded `>` values, and
a shell-command matcher that missed `bash -lc`. All three parsers are hand-written in the
repo's hook scripts, which must stay standard-library Python 3.8. The reviewer already has
an "every form of the input" pass. Our CI runs pytest.

What should change, and how will we know it worked? No repository is checked out in this
evaluation; give the plan.
