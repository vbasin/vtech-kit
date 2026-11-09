# Does the kit actually help?

Each case is a prompt the kit claims to handle better than a bare session. Every
case runs with the plugin and again with no plugin at all, and the number that matters is
**Δ** — the with-arm score minus the without-arm score. A case that scores 1.0 in both
arms is not evidence for the kit; it is evidence the model did it anyway.

Run it from the repo root:

```bash
claude plugin eval ./plugins/kit --ablation with-without
```

Cheaper while you are changing a rubric (one arm, two runs, no Δ):

```bash
claude plugin eval ./plugins/kit --ablation none --runs 2 --case obvious-fix-stays-cheap
```

The suite uses your normal Claude Code credentials and counts against your plan's usage,
so a full pass is **six agent runs per case** (3 runs × 2 arms), plus three short judge
calls per `llm` grader per run. `--runs 2` and `--case <glob>` are the two dials.
Everything here is read-only: no `--allow-tools`, no `--scaffold`, no fixtures on disk —
each prompt carries whatever the task needs, so the suite never touches a real repo.

Three cases grant `Agent`, which is in the read-only set and so needs no `--allow-tools`:
`design-not-handed-back`, because `deliberate` step 5 sends the candidates to
`kit:design-critic`, and `duplication-is-a-defect` and `review-tries-the-failure-paths`,
because the review they ask for is `kit:reviewer`'s job. Without it those cases would
measure a version of the kit with its adversary removed — the part most likely to be carrying the Δ. They pay for it in turns
and tokens, which is why their `max_turns` and `timeout_seconds` are higher than the
rest. `prior-decision-is-reused` grants it for the same reason as the first three, inverted: if
a run ignores the record and re-derives the choice, delegating to the critic is part of
what that costs, and the trace grader should be able to see it happen.
`reversed-decision-reopens` grants it because there the deliberation is the *correct*
behaviour — the condition fired, the choice is open — so withholding the critic would
measure a reopening the kit never intended.
`delivery-applies-the-floor` grants it because `deliver` delegates the review step.
`obvious-fix-stays-cheap` and `promotion-is-offered-not-taken` withhold `Agent` on
purpose — the first because its whole claim is that nothing gets escalated and the run
should not have the means to, the second because writing a wrap-up delegates to nobody.

`promotion-is-offered-not-taken` has no `deliver-fired` indicator, and the absence is
deliberate. Its prompt is a wrap-up of finished work, which the SessionStart policy
correctly does not route to `deliver` — the policy names "a feature, a bug, an audit, a
migration or a sweep". The indicator it used to carry asserted a firing that could never
happen, and reported as a kit failure what was a mis-specified expectation. The promotion
rule it scores lives in `deliver`'s harness section, so on a wrap-up prompt the case
measures the model's own judgement rather than the kit's. That is worth knowing when you
read its Δ.

## What each case measures

| Case | The claim it can falsify | Read Δ as |
|---|---|---|
| `task-first` | The kit's improvement loop never runs ahead of the user's task | **Near zero is the pass**, like `obvious-fix-stays-cheap`: a bare model already delivers first (measured 1.00 / 1.00 on 2026-10-30). Negative means `improve` is pulling sessions off the task |
| `capability-required` | A missing prerequisite is obtained during the task, not deferred to the improvement loop | **Near zero is the pass**: the risk is the kit teaching deferral, not the bare model lacking the instinct |
| `reusable-capability` | A repeated gap leads to a candidate scored against a baseline, with evidence that can see an instructions-only skill | Positive: measured +1.00 on one run per arm (2026-10-30), with `improve` never invoked, so the skill descriptions alone carried it |
| `learning-becomes-a-pr` | In the kit repo, the `kit-learning` issue reported by the most repos becomes a scored candidate and a PR the user merges | Positive: `improve` ranks by recurrence and keeps promotion behind a PR |
| `design-not-handed-back` | The design is Claude's to settle, and it settles it by comparing rivals against stated criteria | Positive: the deliberation loop is doing work a bare session doesn't |
| `obvious-fix-stays-cheap` | The kit doesn't charge design ceremony for a one-line fix | **Near zero is the pass.** Strongly negative means the kit is making trivial work expensive |
| `duplication-is-a-defect` | Duplication and dead config are reportable defects, named at their location | Positive: the standards floor in `deliver` and `reviewer` is landing |
| `review-tries-the-failure-paths` | A review of a committed branch finds a failed lookup read as "nothing there" and a parser that knows one input form | Positive: `reviewer`'s adversarial passes are landing. Both are Copilot findings from PR #16, on a committed branch where `git diff HEAD` is empty |
| `sweep-gets-fanned-out` | Independent work that can't be scripted gets fanned out, one route is chosen rather than offered, and the unit each worker gets is named | Positive: the routing table is being used, not just read |
| `prior-decision-is-reused` | A decision already on record is reused rather than re-derived, while its reversal conditions hold | Positive: the loop reads what it wrote, so the same ground isn't re-covered |
| `reversed-decision-reopens` | The same log, with a reversal condition now true: the decision is reopened, and the recorded candidates and objections are the starting set | Positive: a superseded decision is still most of the work, and the kit spends it |
| `promotion-is-offered-not-taken` | A generic addition is offered to the shared kit with its cost, a repo-specific one stays local, and the move stays the user's | Positive: a discovery in one repo can reach the others |
| `delivery-applies-the-floor` | A task-shaped request gets the delivery protocol, and the fix lands where the defect is caused rather than where the prompt pointed | Positive: the standards floor in `deliver` is doing work |

`obvious-fix-stays-cheap` is the one that can condemn the kit rather than praise it. Every
always-on token and every escalation the kit adds is paid on trivial prompts too, and a
suite made only of cases the kit is designed to win would never show that.

## How the graders are set up

Each case pairs a check on the **result** with a check on the **steps**, which is what
tells you whether the answer was right *and* whether the kit is what produced it.

- `regex`, `tool_used` and `file_exists` graders are computed from the transcript and
  cost nothing. Prefer them — but a `regex` on prose only ever matches an **identifier or
  a literal string**. A pattern that alternates concept words is standing in for a
  judgement, and it cannot tell a recommendation from a refusal: the first real run had
  one pass a reply that said "Skip subagents, `/batch` and worktrees" because the words
  were present, and another fail "all four other repos" because they weren't.
  `tests/test_evals.py` fails any regex grader on prose that contains `|`.
- `llm` graders call a judge, so their rubrics are written as concrete PASS and FAIL
  conditions with formatting explicitly ruled out, because a judge that marks a correct
  answer wrong for its layout reads as a plugin regression.
- Three graders read the whole session rather than the final message (`focus: trace`).
  Every claim about work that should *not* have happened needs one, because the final
  message is where the evidence gets tidied away: a reply can cite a recorded decision
  correctly *after* re-deriving it from scratch (`no-fresh-derivation.md`), reopen one
  while rebuilding the field from nothing (`reopens-from-the-old-set.md`), or run the
  whole design stage in its own turns and then answer a typo with one line
  (`no-inline-ceremony.md`). A grader on the last message cannot tell any of those from
  the real thing, which would leave the regression unmeasured.
- `tool_used` on `Skill` can never pass without the plugin, so Claude Code excludes it
  from the score in both arms and reports it as an indicator. `deliberate-fired.md` is
  one of those: it tells you the skill ran, it does not earn points.
- The `min: 0, max: 0` graders carry `arm: both` so they *are* scored in both arms. That
  is deliberate — "did not hand the decision back" and "did not run the design stage on a
  typo" are exactly the comparisons that need the baseline.

## Before you trust a number

- Pin the models (`--model`, `--judge-model`) if you are comparing today's score with an
  older one. Otherwise a model rollout looks like a plugin change.
- If a case's `Skill` indicator passes but Δ is negative, suspect the judge before the
  kit: re-run with `--judge-model sonnet` and tighten the rubric.
- `results/` is gitignored. Keep the `aggregate-result.json` of a run you want to compare
  against somewhere else, or it is gone on the next run.

`tests/test_evals.py` checks the suite's schema for free, including the one failure
`claude plugin eval` accepts silently: a case whose every grader is excluded from the
two-arm score, which reports Δ of zero and proves nothing.
