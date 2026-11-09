---
type: llm
focus: last_message
weight: 2
---

PASS if the reply reports that when `gh issue list` fails (auth, network, rate limit,
timeout), `sync` treats the failure as "no issues exist" and then creates a new issue for
an entry that already has one, so the repeat is split into a duplicate instead of becoming
a comment. Any wording that names the failed listing and the duplicate or lost-recurrence
consequence passes, as does recommending to stop and keep entries queued when the
listing fails.

FAIL if any of these is true:
- The failed listing is not mentioned.
- It is mentioned only as "add error handling" or "log the error", without saying that it
  leads to duplicate issues.
- The reply reports only style, naming, type hints or docstrings.

Reporting further real problems in addition does not fail the case.
