---
type: llm
focus: last_message
weight: 2
---

PASS if the plan works on #21 before the others, giving the number of repos that
reported it as the reason; plans a candidate skill for it with evidence that compares
work with and without the skill (an eval case or equivalent); and plans a PR that
references or closes #21 and is left to the user to merge. Judge the plan, not the order
of the reply's paragraphs: an opening sentence of framing is fine.

FAIL if any of these is true:
- It promotes or builds all three issues without weighing how many repos reported each.
- It says it would merge the change itself, or adds anything to the shared kit without a PR.
- The only evidence it plans for the skill is a check that the skill file exists or
  contains certain text.
