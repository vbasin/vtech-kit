---
description: Asked to build a pre-PR review agent, Claude checks what already ships before building one, and makes the best existing option the baseline to beat.
tags: [capability, reuse]
max_turns: 12
timeout_seconds: 420
allowed_tools: [Read, Glob, Grep, Skill, TodoWrite, Agent]
expected_outcome: A search of what already exists (built-in commands, installed and published plugins) before any design, and a plan that measures the new agent against the best existing one on the same cases.
---

Copilot keeps finding real bugs in our pull requests after we open them: failed API calls
treated as "nothing found", parsers that only handle one input format, and checks that
report success when they never ran. Write the plan for a review agent we can run before
opening a PR to catch these first. No repository is checked out in this evaluation, and
nothing will be built yet; I want the plan.
