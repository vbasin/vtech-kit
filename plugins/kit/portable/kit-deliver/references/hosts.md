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
| Grok Build | `install_native.py --repo PATH --host grok`; inspect `grok inspect` | Project Bash PreToolUse deny hook, passive SessionStart and Stop hooks for learning capture, and built-in subagents; verify explicitly before reporting |

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

Grok documents Stop as passive and says passive hook output is ignored: this integration
cannot enforce Stop verification or inject SessionStart context through those hooks. The
skill must run the manual verifier before the final answer. Grok can use available native
subagents with the reviewer briefs in `references/reviewers/`; custom Grok agent schemas
and Claude-plugin compatibility have not been verified here. If delegation is unavailable,
review sequentially and disclose the limitation. Do not install overlapping Claude and
native hooks without inspecting the resulting duplicate hooks in the host.

Learning capture runs on every host. Each session's start records the repo's skills,
agents, MCP servers and development dependencies; Claude's SessionEnd, and Codex's and
Grok's Stop (which run every turn), record what is new in `~/.vtech-kit/learning.jsonl`.
A component already recorded for the repo, by any session on any host, is not recorded
again. On Grok the first Bash call sets the start inventory if SessionStart did not run.
Grok's passive hooks are expected to run their command with the output ignored; that
is unverified until the learning check in `docs/HOST_VALIDATION.md` is run on a live
Grok session. Sending to the kit repo needs the same two opt-ins on every host. The
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
