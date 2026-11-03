# When the task meets a missing capability

The one statement of this rule. `kit-deliver`, Claude's `deliver`, `deliberate` and
`improve` all point here instead of restating it.

1. **Needed to finish this task, and nothing available fits** → research, adapt or build
   the smallest capability that does, now, and use it to finish. Do not hand the task back
   because a skill is missing, and do not install or run untrusted downloaded code.
2. **Something available is adequate** → use it, and finish and verify the task first.
   Record the improvement idea without secrets (`scripts/backlog.py add` in this skill
   folder, or the report's "what remains" line) and evaluate it only after delivery, or
   when the user asks. In a repo that shares learning (`learning.share` in its kit config),
   recorded ideas, and the components a session added, reach the kit repo as
   `kit-learning` issues at session end; the kit is improved from there, never from inside
   this repo.
3. **Where an addition lives.** One that names no path, no schema, no service and no rule
   of the repo it was built in — you can describe it without saying which repo you were
   in — belongs in the kit; say so in the report, with the case for it. Anything else
   stays in the repo: a skill that encodes this repo's gates, data contracts or layout is
   worth more here and nothing anywhere else.
4. **Promotion needs evidence first.** Moving an addition into the kit makes every session
   in every repo pay its always-on context (`claude plugin details <plugin>` prices it).
   Promote only after evaluation and only when the task authorizes changing the shared
   kit. Which evaluation counts depends on what the candidate is:
   - instructions only (a `SKILL.md` and nothing executable): an exit-code suite cannot
     observe them, so the evidence is a `plugins/kit/evals/` case scored with and without
     the candidate, with a positive Δ;
   - executable helpers: `scripts/learning.py evaluate` with a trusted suite that has an
     improvement case and a preserved baseline case (see `learning.md`).

   Without that evidence or that authority, leave the candidate where it is and report
   it: what it does, its cost, and the case against it.
