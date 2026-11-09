# Review every branch before it reaches a PR: a reviewer stamp, a PR gate, and a reviewer that tries the failure paths

- Task: V asked why Copilot keeps finding defects on vtech-kit's own PRs that Claude then agrees with, and for the root causes and fixes. · Done-check: `python -m pytest tests` green, with `tests/test_hooks.py` cases for the gate; the new eval case loads in `tests/test_evals.py`; a replay of past PR diffs scores the reviewer.
- Binding constraint: the kit's review step lived only in `deliver` prose, and `deliver` was not even loaded in the sessions that built the kit. Anything that depends on Claude remembering to review fails the same way. The fix has to hold when no skill fires.
- Supersedes: nothing. It amends `deliver` step 6.6 (re-run a reviewer "only when the fix touched what it flagged"), which the replay shows is too narrow: Copilot's second rounds on #1, #16 and #30 found defects that the fix commits introduced.

## Evidence

- 21 PRs, 75 Copilot inline findings. On the 14 Claude-authored PRs there were 57 findings, and Claude accepted every one.
- Only #15 and #16 record a kit review before the PR, a headless `/kit:deliver` run. This repo has no `.claude/settings.json`, so it never loaded its own plugin. The cloud sessions that built it had no user-scope kit until the setup script added after #30.
- Blind replay: each historical diff was put in a history-free repo and reviewed once.

| PR diff | Copilot findings | current `reviewer` | rewritten `reviewer` |
|---|---|---|---|
| #30 dba646d | 8 | 2 | 4 |
| #16 67516d9 | 8 | 3 | 4 |
| #15 d75a8a5 | 4 | 1 | 1 |
| held out: #9 fb9f2f9 | 3 | 0 | 2 |
| held out: #28 37a0157 | 3 | 3 | 1 |

  The rewrite was written from the categories of the first three, so their gain (6 to 9 of 20) is partly leakage. On the held-out pair it ties (3 of 6 each), and each run is a single sample. The union of the two reviewers caught 9 of 20 and 5 of 6. Independent passes add recall that wording alone does not.
- Copilot categories over the 57: verification claiming success when nothing ran (~13), parsers handling only the happy input shape (~11), partial failure or two sessions leaving wrong state (~15), eval graders that pass a wrong answer (~11), data leaving the machine (3), instructions contradicting another component (2), docs claiming more than the evidence (2).

## Criteria, in priority order

1. The review happens before a PR exists, or before a push updates one, even when no skill fires (co-binding with 2).
2. The review finds what Copilot found, measured on diffs it was not written from.
3. It must not deadlock: a reviewer that cannot run (usage limit, no Agent tool) never wedges work.
4. Small cost to every repo: always-on context, usage per push, friction on trivial work, with a per-repo off switch.
5. Stdlib, tested, fails open. Easy to delete.

## Candidates

### B′ — reviewer stamp + PR gate + rewritten reviewer + dogfooding · chosen
- SubagentStart records the working-tree hash when `kit:reviewer` starts; SubagentStop stamps that tree as reviewed if it has not changed meanwhile (both probed live on Claude Code 2.1.319, background agents included; `agent_type` arrives as `kit:reviewer`).
- A PreToolUse hook on PR creation (`*create_pull_request` MCP tools, `gh pr create`) denies, with the reason, when HEAD's tree carries no stamp. It also gates `git push` to a branch it has already seen a PR opened for, which is how fix commits stop reaching Copilot first.
- `review.gate` is `once` by default: deny once per tree, then let a retry through, so it cannot deadlock. `deny` is strict, and `off` disables it. This repo runs `deny`.
- The reviewer defaults to the whole branch (merge-base with the default branch, plus the working tree). `git diff HEAD` is empty once the work is committed, which is exactly when a PR is about to open. It also gains adversarial passes from the categories.
- This repo gets `.claude/settings.json` and `.claude/kit.json`, so its own sessions load the released kit.

### A — repo-local discipline · rejected
Rejected: a CLAUDE.md line is the same remembering that already failed, and it duplicates `deliver` step 6.

### C — Stop-hook review stage · rejected
Rejected: it fires after the turn, so the PR or push has already happened. It also runs on every turn of every branch.

### D — harvest Copilot findings into a lessons file · rejected
Rejected: it enforces nothing, it would duplicate the learning pipeline, and it cannot be measured without leakage. The categories were harvested by hand into the reviewer passes instead.

## What the critique changed

- It split enforcement from recall and ranked them co-binding, because a gate on a 30% reviewer is weak assurance.
- It caught that the gate would stamp an empty `git diff HEAD` review. The reviewer now defaults to the branch range.
- It moved the tree record from "whatever the tree is at stop" to "the tree at start, still unchanged at stop".
- It narrowed push gating from every `git push` to branches with a PR, and added `once` so the gate cannot deadlock.
- It required held-out replay diffs and stated that Codex and Grok get no gate.

## Review round on this change (before its PR)

- `kit:security-reviewer`: one finding, that a reviewer stopped by an error or a usage limit still stamped the tree. Fixed: the stamp needs `Review complete:` as the reviewer's last line.
- `kit:reviewer`: seven findings, all acted on.
  - A stamp of the working tree never matched the commit when an untracked file existed. HEAD's tree is now stamped too.
  - Shell forms went unseen: `bash -c`, subshells, `if`/`then`, `sudo`, wrapper flag values, a leading `cd`, push refspecs, `-Hfeat`. A command that will not lex falls back to the raw text.
  - The gate judged the wrong branch or repo.
  - The verdict was matched mid-message.
  - Concurrent writes to the state file had no lock.
  - The remaining three are documented as limits in the script.
- `kit:reviewer`, second round on the fixed script: eight findings, all acted on. Comments ended the line that followed them; `sudo -n`, `bash -lc`, `eval` and `x=$(git push)` went unseen; a fork's PR to its upstream and `gh -R` were judged by the wrong repo; `--delete`, `--tags` and `HEAD:feat` pushes were judged by the wrong tree or name; and a stale lock could be broken by two waiters at once.
- `kit:reviewer`, third round on those fixes: three regressions the fixes introduced (`sudo -u` and `xargs -I` lost the program, `--tags` beside a branch skipped the push, a `$(...)` refspec was read as branch names), all fixed with tests. Two low-severity races are documented as limits. This is the deliver 6.6 amendment in action: each fix round introduced defects of its own.
- Live, in Claude Code 2.1.319 with `--plugin-dir`: an unreviewed `gh pr create` was denied. A real `kit:reviewer` run stamped the tree, including with an untracked file present, and the same command then went through.

## Unresolved

- One run per arm. Whether the rewritten passes beat the current reviewer on unseen diffs is not established (held-out tie). They are kept because they cost one Sonnet run's reading and found real defects Copilot raised only in later rounds. The decision on them is reopened by the condition below.
- Codex and Grok: no SubagentStart/Stop, so no stamp and no gate. Their PRs rely on the portable review briefs.

## What would change this decision

- Copilot findings per Claude-authored PR on this repo do not fall below the 4.1 average (57 / 14) over the next five PRs made with the gate on. Then the stamp certifies too little: run two independent reviewers in parallel before the PR (the union result above), or replace the reviewer.
- A Claude Code release drops `agent_type` from SubagentStart/SubagentStop, or stops firing them for background agents. The stamp then never lands, and `once` degrades to a nudge. Move the stamp to a script the reviewer runs.
- The gate's denials become noise: more than one in five is followed by a retry with no reviewer run in between. Then default `review.gate` to `off` outside this repo.
