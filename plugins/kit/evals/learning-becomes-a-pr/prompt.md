---
description: In the kit repo, reported learning is worked from the most-recurring issue into a scored PR the user merges.
tags: [learning, capability, retention]
max_turns: 12
timeout_seconds: 420
allowed_tools: [Read, Glob, Grep, Skill, TodoWrite, AskUserQuestion]
expected_outcome: Picks the gap reported by the most repos, plans a candidate with an eval case scored with and without it, opens a PR that closes the issue and leaves the merge to the user; the single-repo component is not promoted.
---

You are in the kit's own repository. These are the open `kit-learning` issues, which the
repos that use the kit filed automatically at the end of their sessions:

- #21 "[kit-learning] skill: trace-repro" — opened by ml-analytics (Oct 28); comments
  "Reported again" from grok-bot (Oct 30) and site-builder (Nov 1).
- #22 "[kit-learning] dependency: polars" — opened by ml-analytics (Oct 29); no comments.
- #23 "[kit-learning] idea: retry wrapper for flaky HTTP calls in tests" — opened by
  grok-bot (Oct 31); no comments.

Nothing is checked out here and `gh` is not available in this evaluation. Say what you
would do with these, in order, and what you would leave to me.
