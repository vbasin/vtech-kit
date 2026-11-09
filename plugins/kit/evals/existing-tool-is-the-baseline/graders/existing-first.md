---
type: llm
focus: last_message
weight: 2
---

PASS if the plan names at least one review tool that already exists, either one Claude Code
ships (`/code-review`, `/security-review`) or a plugin or agent it labels as needing to be
checked first. It must also state a decision rule before building: the new agent is built
only if it beats that tool on the same examples (for example past PRs and their Copilot
findings), and is dropped if it does not. Running both is acceptable only as an outcome
of that measurement.

FAIL if the plan goes straight to designing a new agent. Also FAIL if it keeps the
existing tool only as a control beside a new agent that is built either way, mentions a
tool with no measurement against it, or installs a third-party tool without evaluating it.
