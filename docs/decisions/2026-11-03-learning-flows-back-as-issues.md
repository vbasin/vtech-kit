# Learning flows back to the kit as GitHub issues

- Task: make what sessions learn in the repos that use the kit reach the kit repo, where
  it can become a PR. Asked by V on 2026-11-03, after an audit showed the learning rules
  existed but nothing captured anything automatically and the backlog was never read back.
- Done-check: hook tests for capture, send, repeat and retry; an eval case scoring that
  reported learning becomes a scored PR, most-reported first.
- Binding constraint: the work happens in other repos and on other machines, and the kit
  repo is the only place the kit can change.
- Supersedes: nothing; extends `2026-10-29-task-first-portable-capabilities.md`, whose
  backlog stayed on one machine.

## Criteria

1. Capture does not depend on Claude remembering a rule.
2. Learning from many repos lands in one place, and a repeat is counted, not duplicated.
3. Nothing reaches the shared kit without evidence and V's merge.
4. No file contents or secrets leave a repo; posting outside a repo is opt-in.
5. No model usage and no new service.

## Candidates

| | Candidate | Verdict |
|---|---|---|
| A | Issues in the kit repo via `gh`, one per fingerprint, repeats as comments | **Chosen.** GitHub already holds V's credentials on the VM; issues are an inbox `improve` can rank, and close from the PR |
| B | Each repo commits a learning file; a kit-side job pulls from every repo | Rejected: the kit would need a list of repos and read access to all of them, and the file would churn every repo's history |
| C | Each machine opens a PR to the kit appending to an inbox file | Rejected: a PR per session is noise, and two machines conflict on one file |
| D | Keep the local backlog and run `improve` by hand | Rejected: the audit's finding was that this never happens |

## What it does

- `learning_report.py` (SessionEnd) records skills, agents, MCP servers and dependencies
  added since SessionStart into `backlog.py`'s log, names only, tagged with the repo.
- With `learning.report_to` set in the repo's `.claude/kit.json`, it sends queued entries
  as `kit-learning` issues. Fingerprint: component type + name, or the idea's first words.
  Failures leave entries queued.
- `improve` in the kit repo ranks open `kit-learning` issues by distinct repos, builds and
  scores the top one, and opens a PR that closes it. Its description now names this case,
  because without it the skill did not fire on the eval prompt.

## Measured

`learning-becomes-a-pr`, two runs per arm: with 1.00 / without 0.33, Δ +0.67, `improve`
fired 2 of 2. The first version of its grader failed a correct answer for not *opening*
with #21; it now judges the plan, not paragraph order.

## What would change this decision

- Issues pile up unread → add the weekly routine V deferred (uses Pro usage).
- Ideas from different repos never share a fingerprint → fingerprint on a normalized
  gap key Claude writes, not the idea text.
- The kit repo becomes public and repo names are sensitive → send a hash of the repo
  name instead.
