# Host integration

The host is the coding-agent application, not the model API alone. A skill supplies a
workflow; a hook runs at a lifecycle event; an MCP server supplies connected tools. A
GitHub checkout supplies files but does not itself run hooks or authenticate an agent.
Choose available tools and respect host and user permissions. Never claim an absent hook,
reviewer, check or external connection ran.

| Host | Installation and discovery | Native behavior |
|---|---|---|
| Claude Code | Marketplace plugin `kit`, or portable `.claude/skills/kit-deliver` | Plugin session, tool and Stop hooks and its reviewer agents, when enabled |
| Codex | `install_native.py --repo PATH --host codex`; inspect `/hooks` and available agents | Project SessionStart, Bash PreToolUse and Stop hooks (Stop also captures learning); four project-scoped custom reviewers |
| Grok Build | `install_native.py --repo PATH --host grok`; inspect `grok inspect` | Project SessionStart, Bash PreToolUse and Stop hooks (Stop verifies and captures learning); built-in subagents, and a user's Claude plugins |

Native installation also installs the portable skill. It refuses conflicting existing
configuration files; reconcile them manually rather than overwriting user settings. It
adds a marked workflow block to repository `AGENTS.md`, preserving unrelated content;
nested instructions can still affect the current directory. Codex
project configuration and each hook must be trusted using the host's `/hooks` interface.
Grok project hooks require `/hooks-trust` or the host's documented trust setting. Hooks can
be disabled by policy, may fail open, and are not a substitute for permissions. In Codex,
`ask` guard decisions provide context but cannot force an approval prompt; Grok's documented
hook contract only supports explicit denial, so `ask` is left to the host permission system.
These guards only cover supported Bash tool calls; they do not monitor every possible tool.

Codex's Stop hook asks the agent to continue once on verification failure, then reports
remaining failures. The hook requires a session baseline and configured checks; a missing
baseline is unavailable, not a pass. The skill still runs and reports appropriate checks.
Codex project hooks require a supported local execution surface; cloud orchestration may
exclude local hooks even when tools run locally. Plugin installation does not confer trust.

Grok 1.0.70's built-in hook docs say Stop can block in Claude's format (`decision: block`,
at most 8 continuations), so the Grok Stop hook verifies and asks for one fix the way Codex's
does; earlier Grok releases documented Stop as passive, so on those the skill must still run
the manual verifier before the final answer. SessionStart reads only a top-level
`systemMessage`, so it cannot inject the routing policy. Grok also loads a user's Claude
plugins, hooks included, with Claude's snake_case input keys alongside its own; where
`.grok/hooks/kit.json` exists, the Claude plugin's kit hooks for the events it handles
(SessionStart, Bash PreToolUse, Stop) see `GROK_HOOK_EVENT` and exit, so each runs once;
PostToolUse lint and SessionEnd still come from the plugin. Start and Stop step aside only
when the installed native hook verifies at Stop, so an older native copy never removes it. Grok can use available native
subagents with the reviewer briefs in `references/reviewers/`; custom Grok agent schemas
have not been verified here. If delegation is unavailable,
review sequentially and disclose the limitation. Inspect the host's hook list after
installing; `grok inspect` lists the Claude plugin's hooks file too, which is expected.

Learning capture runs on every host. Each session's start records the repo's skills,
agents, MCP servers and development dependencies; Claude's SessionEnd, and Codex's and
Grok's Stop (which run every turn), record what is new in `~/.vtech-kit/learning.jsonl`.
A component already recorded for the repo, by any session on any host, is not recorded
again. On Grok the first Bash call sets the start inventory if SessionStart did not run.
A live Grok 1.0.70 session ran them and recorded a new skill (2026-11-03, see
`docs/HOST_VALIDATION.md`); Codex's live run is still pending. Sending to the kit repo needs the same two opt-ins on every host. The
kit's own installed skills and `kit_*` agents are never reported as learned.

For Codex, use the installed `kit_design_critic`, `kit_reviewer`,
`kit_security_reviewer`, and `kit_test_runner` agents when a task warrants an independent
review and host and user instructions authorize delegation. They inherit the parent's
permissions; instructions to avoid edits are not a separate sandbox. Grok can delegate a
specific review task and brief to an available built-in agent. Neither host can guarantee
that an LLM chooses a skill, delegates, or follows instructions on every task.

Useful checks include a small edit, a deliberate failing gate, a corrected gate, a
subagent review, and a task requiring a missing capability. Repeat on each actual host and
record the observed hooks, tools, and results. An MCP integration is optional for external
data or tools; it is not needed just to execute the included local Python scripts.

Primary references, accessed 2026-10-29:
- https://learn.chatgpt.com/docs/hooks
- https://learn.chatgpt.com/docs/agent-configuration/subagents
- https://developers.openai.com/plugins/build/plugins
- https://docs.x.ai/build/features/hooks
- https://docs.x.ai/build/features/subagents
- https://docs.x.ai/build/cli/reference
