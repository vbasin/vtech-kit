---
name: deliver
description: "Take a task from request to reviewed change: settle the approach, size the work, route it (this session, parallel subagents, /batch or a dynamic workflow), run it, verify it, review it. Use at the start of any request beyond one obvious edit — a feature, a bug, an audit, a migration or a sweep."
argument-hint: "<what you want built, fixed or audited>"
---

Deliver: $ARGUMENTS

You own this task end to end. Decide the routing yourself and say what you chose in one line; do not ask the user to pick the tool.

The repo's written rules are the floor, not the brief. Apply what a strong engineer
applies without being asked, and say which of those standards bound this change. A prompt
for recall, not a checklist: duplication, dead code and dead config, abstraction nothing
needs, interfaces that leak their implementation, error handling that swallows failures,
naming that misleads, work done per row that belongs in a set operation, state that
should be derived rather than stored. The user should never have to name one of these for
you to apply it.

## 1. Frame it

- Restate the goal in one sentence, and name the **done-check**: the command that proves it works (a scoped test target, a test file, a script). No done-check available → say so and pick the closest one.
- Requirements unclear — you do not know what the user wants built → stop and ask them to run `/kit:spec <feature>` themselves (it is user-only, so you cannot start it). A written spec implemented in a fresh session beats a long interview transcript.
- The *design* not being obvious is not that case. That is yours to settle in step 2, not the user's to supply.
- Read `.claude/kit.json`. Its `routing` block (optional) names this repo's gates and reviewers: `gates` map path globs to the command that gates them, `reviewers` map path globs to the agent that must see the diff. Use them in steps 5 and 6. A `verification.gates` list, when enabled, is what the Stop hook itself runs; treat its entries as gates too. No block → work the gates out from the repo (`make help`, the Makefile, CI) and say which you used.

## 2. Decide the approach

Shape not obvious — more than one sensible design, a new component, a schema or an
interface, anything expensive to undo → run `deliberate` first and build what it chose. It
generates rival designs, has `kit:design-critic` attack them, revises and converges, then
leaves a decision log. Do not skip it because a workable approach came to mind quickly:
beating the first workable approach is what it is for.

Obvious work — a named bug, a mechanical change, a question → straight to step 3. Say which
of the two you judged it to be, in one line.

## 3. Size it

Count the units of work and ask whether they are **independent**. Dependent steps belong in one session: parallelism costs more and reviews worse.

| What you are looking at | Route |
|---|---|
| One file, an obvious fix, a question | Do it here. No fan-out, no plan mode. |
| A feature or bug over a few files, steps that depend on each other | This session. Plan mode first (Shift+Tab), `Explore` for lookups. |
| 2 to 5 independent pieces that each need judgement | Parallel subagents, one brief each. |
| The same mechanical change across many files | `/batch <the change>` — workers get their own worktrees. |
| A codebase-wide audit, migration or sweep; research to cross-check | A dynamic workflow: start the prompt with `ultracode:`. Pilot first. |
| Work that must keep running when the machine is off | Say so, and offer a cloud session or a scheduled routine. |

Escalate one step only when the pilot earns it. Never fan out a single dependent chain.

## 4. Pick the model and effort

| Who | Model | Effort |
|---|---|---|
| This session | leave as the user set it | leave it — changing it mid-session throws away the prompt cache |
| Read-heavy workers: searches, inventories, log reading | Haiku | low |
| Reviewers, test writers, parallel implementers | Sonnet | medium to high |
| A piece harder than the main task | the session's own model | high |

Never change the session's model or effort yourself. If the task needs a different one, finish, then tell the user to start a fresh session at that setting.

## 5. Run it

- Before writing code in a file, run `python3 "${CLAUDE_PLUGIN_ROOT}/portable/kit-deliver/scripts/defects.py"` `rules --paths <the files>`. It lists the lessons
  this repo has already paid for in review: the mistakes found in these files and this
  area before. Apply them while you write, and pass them to any worker that writes code.
- Each worker gets a brief that stands alone: the goal, the exact files or slice, the repo rules that bind it, the done-check, and the output format. Workers see none of this conversation.
- A worker that edits code runs with `isolation: worktree`. Two agents never edit the same files in one checkout.
- Keep intermediate output in files, not in this conversation. For a workflow, watch per-agent token use in `/workflows` and stop a run that loops.
- Hitting a usage limit mid-run is normal on a subscription: the run pauses and resumes after the reset. Say so rather than retrying smaller.

## 6. Verify, then review

1. Run the gates for every area touched — the `routing.gates` entries whose globs match, or the repo's scoped targets. Never the full suite unless the repo says to.
2. `/kit:verify` (or let the Stop hook do it) lints and runs the covering tests for what changed.
3. Run `/simplify` when the change touched code that already existed, or added more than a
   few files. Do it before the reviewers, so they judge the code that will actually ship
   rather than a draft of it.
4. Run `kit:reviewer` on any non-trivial diff, plus every `routing.reviewers` agent whose
   globs match the changed files. Also run Claude Code's built-in `/code-review high` when
   the session has it: it catches different defects from `kit:reviewer`. Start them all in
   parallel, in one message. Put the `defects.py rules --paths <changed files>` output from step 5 in
   each reviewer's brief.
5. Act on findings that affect correctness, safety, testability or duplication. Say which ones you skipped and why — acting on every nitpick is its own failure.
   Record each finding you act on, from any reviewer and Copilot's on the PR included:
   `python3 "${CLAUDE_PLUGIN_ROOT}/portable/kit-deliver/scripts/defects.py"` `add --category <c> --summary <what was wrong> --rule <the general lesson>
   --source <reviewer> --path <file> [--pr <n>]`, adding `--id gh-<comment id>` for a PR
   review comment so a later `harvest` knows it. The rule is the lesson for next time,
   not this fix. The ledger is `.claude/defects.jsonl`; commit it with the fix.
6. **Any edit you made in steps 3 to 5 puts the diff back where it started.** Before you
   report, re-run the gates whose globs match what changed and `/kit:verify`, and run
   `kit:reviewer` again on what the fixes changed: a fix commit is new code, and that is
   where the second-round defects come from. Also re-run any `routing.reviewers` agent whose
   glob a newly touched path brings into scope. What you report as verified has to be the
   diff you are delivering, not the one you checked two edits ago.
7. Before opening a PR, or pushing to a branch that has one, the last `kit:reviewer` run
   must have seen the tree you are about to push. The review gate hook enforces this
   (`review.gate` in `.claude/kit.json`) and says what to run if it was not.

A test that fails for a reason your change did not introduce stays failing: say so, do not fix it in passing, and never skip or delete a test to get green.

## 7. Report

Close with four lines: what changed, how it was verified (the commands and their result), what you skipped and why, and what remains. Then do the repo's documentation step if it has one.

## Working out the harness as you go

When the task needs a capability nothing available provides, or produces one worth
keeping, read `${CLAUDE_PLUGIN_ROOT}/portable/kit-deliver/references/capabilities.md`: it
decides whether to build it now or record it for after delivery, whether it belongs in
this repo or the kit, and what evidence a promotion needs. Its backlog script is in
`${CLAUDE_PLUGIN_ROOT}/portable/kit-deliver/scripts/`. The `improve` skill runs the
evaluation once the task is delivered.

When the task shows the harness is missing something, name it in the report rather than
silently widening the job:

- A gate that does not exist for the area you changed → propose adding a scope.
- The same correction given twice → propose a rule or a line in `CLAUDE.md`.
- Knowledge needed only sometimes → propose a skill, not more `CLAUDE.md`, with what it does, what it costs in always-on context, and the argument against it.
- A review dimension this repo needs on every diff of a kind → propose an agent in the plugin.
- A run worth repeating → save the workflow (`s` in `/workflows`) and commit it.
