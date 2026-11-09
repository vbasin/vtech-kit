---
type: llm
focus: last_message
weight: 2
---

PASS if the plan's main change acts before or outside review: a step taken while writing
parsers, and preferably something that fails on its own, such as shared parsing helpers
with a corpus of input forms in the tests, a test that every parser must pass, or using a
real parser where the standard library has one. It must also say how it will be judged
on findings it was not written from (for example the next PRs, or replaying past PRs held
out from the ones that shaped the change).

FAIL if the main change is more reviewer or checklist instructions, such as a longer
input-forms pass, a second reviewer or more examples in the prompt. Also FAIL if it fixes
only the three named parsers with no change that stops the next one, or offers no way to
tell whether it worked.
