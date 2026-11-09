# Learn from accepted review findings: a ledger in each repo, lessons that move upstream

- Task: V asked whether the kit's reviewer learns from new mistakes so they do not repeat, and chose to build that loop. · Done-check: `python -m pytest tests` green, with `tests/test_defects.py`; `defects.py harvest` and `report` run on vtech-kit's own PR history.
- Binding constraint: about half of Copilot's 75 inline findings on vtech-kit were in a category Copilot had already raised on an earlier PR, and Claude had already accepted. Each acceptance was forgotten once the PR merged. The loop must keep that knowledge where the next session will find it, on any machine and in a cloud container that is thrown away.
- Supersedes: candidate D of `2026-11-09-review-before-the-pr-exists.md`, "harvest Copilot findings into a lessons file", which that log rejected for three reasons: unenforced, a duplicate of the learning pipeline, and unmeasurable. This design answers each one. Escalation ends in a check, which enforces it. Kit-wide lessons go to the existing pipeline rather than a copy of it. Replay gives held-out cases to measure on.

## Criteria, in priority order

1. A repeated mistake shows up in the data without anyone having to remember it.
2. When a lesson did not stop the repeat, it moves further upstream, toward something that fails on its own, not into a longer prompt.
3. A change made from the ledger can be scored on findings it was not written from.
4. Small cost: no always-on context, no model calls in the script, stdlib only.

## Candidates

### Ledger + escalation + replay · chosen

**The ledger.** `.claude/defects.jsonl` is committed in the repo, one event per line:
- a finding: its category, the general rule, the cause, the PR, the path, and the base and head commits;
- a classify;
- a lesson;
- a merged PR.

**Capture** has two routes:
- `deliver` step 6.5 records each finding Claude acts on, with `add`.
- `harvest` pulls the bot review comments on the repo's PRs that are not recorded yet, together with the merged PRs. A harvested comment is unclassified, and it counts only once `classify` accepts it (a category and a rule) or rejects it. Comment text is scrubbed of anything that looks like a credential and cut to a 280-character excerpt before it is stored.
- The two routes meet on the comment id. An `add` for a Copilot comment needs `--id gh-<comment id>`, and `add` warns when it is missing. Without it, a later `harvest` records the same comment again and the category counts it twice. Nothing stronger catches this, because matching on PR and file alone would merge real findings that sit in the same file.

**Learn.** `report` counts each category's accepted findings across PRs. A category that recurs after its latest lesson moves one stage upstream: `reviewer`, then `write-time`, then `check`. A finding on the PR that introduced the lesson is not a recurrence of it. The report says "holding" only when PRs were merged since the lesson. With none merged, it says "unknown".

**Apply.** The lessons feed back at the two points where they matter:
- `rules --paths` runs in `deliver` step 5, before code is written in a file;
- `kit:reviewer` step 5 gets the same `rules` output in its brief from `deliver`, or prints it itself from the base branch's copy of the ledger. It uses the base branch's copy because entries the diff adds are part of the change under review.

**Measure.** `replay` rebuilds the code each finding was made on, from the merge base of the PR's base and head, as a repo with no history. It also writes `key.json`, naming the findings expected in each repo. Running reviewers on the cases and judging them against the key is done by hand, as in the reviewer replay of `2026-11-09-review-before-the-pr-exists.md`.

**Trust.** The ledger can be edited by anyone who can open a PR. So every line is checked again when it is read: ids, categories, paths and SHAs must match their forms, and text is flattened to one line with control, format and invisible characters removed. A ledger that is a symlink, is not a regular file, or is over 5 MB is refused. `rules` prints the lessons as quoted notes under a header saying they are not instructions. Adding `.claude/defects.jsonl` to CODEOWNERS would put changes to it in front of V; it is not done here.

### Agent `memory:` frontmatter · rejected
Rejected for three reasons. Claude Code gives a subagent with `memory` Write and Edit, which breaks the reviewer's read-only rule. Its store is either per machine or unscored. And nothing in it would push a lesson past the reviewer itself.

### A trained model · rejected
Rejected because a subscription cannot fine-tune Claude, and about 75 labelled findings are far too few to train one. A prompt change scored on held-out replays is the equivalent that is actually available.

### Machine-global log (like `~/.vtech-kit/learning.jsonl`) · rejected
Rejected because cloud containers are thrown away. A lesson held by one machine never reaches the next session.

## Measured

- `harvest` on vtech-kit recorded 75 top-level Copilot comments across 20 PRs, the same count as the hand tally in the review-gate audit. It also recorded 21 merged PRs. This needed reading page by page, because `gh api --paginate` follows numeric-id links that the cloud proxy refuses.
- All 75 were classified as accepted, and none was rejected. This is an assumption. The audit read each comment and judged each one a real defect, but it did not trace every one to its fixing commit. A comment later found to be wrong is corrected with `classify --reject`.
- The `other` bucket held 12 findings. Three of them share one rule, to validate config fields at load time. The report names that as a new category.
- A `reviewer` lesson is recorded for the seven categories that `kit:reviewer`'s adversarial passes cover, dated at 666a84e, the commit that added those passes (2026-11-09T01:43Z). Every one now reads "unknown". No PR has merged since, so nothing yet shows whether they hold.
- Eval `repeated-defect-moves-upstream` scored 1.00 / 1.00, with and without the kit, two runs per arm. Shown the repeat, a bare model already moves the lesson upstream. What the kit adds is noticing the repeat, and only the ledger does that. The eval cannot see it. `tests/test_defects.py` covers that wiring, and the reversal condition below judges its effect.

## What would change this decision

- Over the next five merged PRs on vtech-kit, Copilot findings in categories that already have a lesson do not fall below half of all its findings, the rate before the ledger. Then the lessons are not reaching the writer. Move `rules` into a PreToolUse hook on Edit for files with ledger entries, instead of a `deliver` line.
- Nobody runs `harvest`, so findings stay out of the ledger. Then run it from the SessionStart hook in this repo, which costs one `gh` call per session.
- The ledger grows past about 500 lines, or merge conflicts on it become routine. Then shard it by category, or keep only classified entries and lessons.
- `harvest` misses findings because a reviewer bot changes its login or posts only review bodies. Then widen `BOTS`, or harvest review bodies too.
