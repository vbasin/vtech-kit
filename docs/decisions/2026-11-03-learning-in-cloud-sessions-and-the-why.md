# Learning in cloud sessions, and reporting why a component exists

- Task: make a skill or tool that any session builds or downloads reach the kit's learning
  pipeline, including claude.ai cloud sessions and projects with no repo of their own, and
  report why it was needed, not just its name. Asked by V on 2026-11-03.
- Done-check: `tests/test_hooks.py` cases for Stop-time capture above the checkouts, the
  why/commit asks, the `~/.claude` move ask, the kit-only project, the no-access prompt and
  REST sending; plus a live headless session in a cloud container that recorded the skill,
  wrote the reason and committed it (`docs/HOST_VALIDATION.md`).
- Binding constraint: capture must not depend on the model remembering an instruction, and
  a cloud session gives hooks no MCP tools, so whatever sends must work through `gh`.
- Supersedes: nothing. Extends `2026-11-03-learning-capture-and-distribution-across-hosts.md`,
  whose capture assumed a session in a repo on a machine with a target and a signed-in `gh`.

## What a cloud session actually allows (checked in one, 2026-11-03)

- A plugin installed at user scope runs its hooks there, whatever the working folder.
- The session starts in the home folder with each repo cloned beneath it, so "the repo of
  the working folder" is none of them.
- `gh` reaches GitHub's REST API for the repos attached to the session; GraphQL, which every
  `gh issue` command uses, is refused. A repo not attached answers 403 and names `add_repo`.
- The container can be discarded without a SessionEnd.

## Choices

| Question | Chosen | Rejected, and why |
|---|---|---|
| Where cloud sessions get the plugin and target | The environment's setup script installs `kit` at user scope and sets the target | Repo `.claude/settings.json`: never read when the session starts above the repo, and absent in a project with no repo. A project instruction alone: depends on the model (kept as the floor) |
| When to capture | Stop, every turn (repeats record nothing), plus SessionEnd | SessionEnd only: may never fire in a cloud container |
| Which repos | The working folder's repo, else each checkout directly beneath it | Every repo on disk: slow, and not the session's |
| How to send | `gh api` REST calls, an issue body passed as a JSON file | `gh issue`: GraphQL. `-f labels[]=`: older `gh` builds cannot send arrays |
| Where the why comes from | Deterministic first: the component's own description and the task and binding constraint of a decision log the session wrote. Then one Stop-hook ask for a one-or-two-sentence reason, sent on the next Stop | Whole decision logs or transcripts: too much leaves the machine. Model-only notes: missing whenever the model forgets |
| A skill saved only under `~/.claude` | Ask once to move it into the repo and commit it | Moving it from the hook: guesses which repo, and writes files the user did not ask for |
| A project whose only repo is the kit | Ask for a kit pull request if the skill is reusable | Reporting it as an issue against the kit itself: the kit does not report to itself, and a PR is the improvement |

## Privacy

Free text is cut to 600 characters per field and passed through the same credential and
home-path scrub as ideas. Nothing is sent without the repo's `learning.share` and the
machine's (or setup script's) target.

## What would change this

- Cloud sessions gain hook access to MCP tools, or GraphQL: send through those instead.
- The setup script stops running before Claude Code reads plugins: move the install to a
  SessionStart hook in the environment, or back to repo settings.
- A skill kept in a project's shared files (not a repo) turns out to be common: inventory
  that folder too, with an attribution the project can name.
