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

- `learning_report.py` (SessionEnd) records the skills, agents, MCP servers and
  development dependencies that are in the repo now and were not in SessionStart's
  inventory, names only, tagged with the repo.
- When the machine names a target (`backlog.py target OWNER/NAME`) and the repo consents
  (`learning.share` in `.claude/kit.json`), it sends that repo's queued entries as
  `kit-learning` issues. Fingerprint: component type + name, or the idea's first words.
  Failures leave entries queued; the last outcome is in `backlog.py status`.
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

## Review round (PR #16): Copilot, then the kit on itself

Copilot left eight comments; `/kit:deliver` run headless on the branch (read-only, $0.46)
found twelve, overlapping on four. All of Copilot's held up when checked. What changed:

| Finding | Source | Change |
|---|---|---|
| A sync sent every repo's entries from the machine-wide log | both | Only the syncing repo's entries are sent |
| The repo's committed config chose the destination, so a cloned repo could redirect the user's `gh` login | kit | The machine owner sets the target; a repo can only consent (`learning.share`) |
| A failed `issue list` was read as "no issues", so a retry could duplicate one | both | It stops and leaves everything queued |
| Listing a missing label succeeds empty, so the label was never created | Copilot | `gh label create --force` before the first create |
| An idea's text reached GitHub only as an 80-character title | Copilot | The whole idea goes in the body, scrubbed of credential- and home-path-shaped text |
| Comparing with HEAD reported files already dirty or untracked at SessionStart | Copilot | SessionStart stores an inventory; SessionEnd reports the difference |
| `pyproject.toml` parsing read any quoted line (`testpaths`, `keywords`) | both | A reader for the dependency-group arrays and tables only |
| Runtime dependencies would bury the skill and agent signal; one repo's data library is not kit work | kit | Only development dependencies are recorded |
| `improve` listed 30 issues and closed single-repo ones, which a later report could not revive | Copilot | `--limit 1000`; single-repo issues stay open; a report on a closed issue reopens it |
| A call could run past the hook timeout mid-create; two sessions could sync at once | kit | Each call gets the remaining budget, none starts with under 5s left; one sync per machine (lock) |
| Send failures were silent | kit | `sync-status.json`, shown by `backlog.py status` |
| Ranking counted comments, not repos | kit | `improve` counts distinct repos named |
| Portable skills the kit itself installs counted as learned; the kit repo reported to itself | kit | Excluded |
| Second Copilot pass: the idea title skipped the scrub; a sync read the log before taking the lock; a failed reopen still commented; every extra and Poetry group counted as dev tooling | Copilot | Title scrubbed; log re-read under the lock; a failed reopen leaves the entry queued; only groups named for development (`dev`, `test`, `lint`, `docs`, `typing`, ...) are read |

Not taken: moving the send to a detached background process (the bounded budget removes the
mid-create kill; a detached process is invisible when it fails), and a required `--gap` key
for ideas (`improve` already merges near-duplicates; the reversal condition below stands).
The kit review's claim that `vbasin/vtech-kit` does not exist was wrong: the repository was
renamed, and `vbasin/claude-kit` redirects to it.

## What the repos learned before the hook

SessionEnd compares with SessionStart's inventory, so anything already in a repo when the
hook first runs is never reported, and ideas recorded before entries carried a repo are
never sent. `learning_report.py backfill` closes that once per repo: it records the current
inventory (minus what is already recorded for the repo), optionally claims the older ideas,
and sends under the same two opt-ins. Idempotent. Rejected: walking git history for every
component ever added (it would report what was since removed, and the current inventory is
what the repo actually relies on). On ML_analytics it records four skills and five dev tools.

