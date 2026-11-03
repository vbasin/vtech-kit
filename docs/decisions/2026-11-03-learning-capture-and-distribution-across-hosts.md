# Learning capture and distribution across Claude, Codex and Grok

- Task: give Codex and Grok the automatic learning capture Claude Code has, and decide
  whether they should use the same plugin, separate plugins, or one host-neutral route.
  Asked by V on 2026-11-03.
- Done-check: tests per host event (Codex SessionStart and Stop; Grok SessionStart, Stop
  and the first-Bash-call fallback) record a component once across two hosts and repeated
  Stops; a copied install contains the capture code; the installer's own skills and agents
  are not reported; learning never changes the Codex Stop answer. All in `tests/test_native.py`.
- Binding constraint: capture must not depend on a model remembering an instruction, so an
  instruction-only route fails. Codex has no SessionEnd event, and whether Grok runs the
  command of a passive hook is unverified.
- Supersedes: nothing; extends `2026-11-03-learning-flows-back-as-issues.md`, whose capture
  was Claude-only.
- Method: `/kit:deliberate` run headless with a design-critic round ($1.06). The primary host
  docs could not be fetched from that environment (proxy refusal), so host facts marked
  unverified below came from search summaries.

## Criteria, in priority order

1. Capture does not depend on the model.
2. Correct attribution, no double counting, opt-ins unchanged.
3. One implementation, little adapter duplication (stdlib-only, never crash).
4. Always-on context cost.
5. Update propagation.
6. Cheap to reverse.
7. Testable offline.

## Q1: how capture runs on Codex and Grok

| | Candidate | Verdict |
|---|---|---|
| A' | Per-session start inventory, a per-host end trigger, the shared log as the cross-host dedupe | **Chosen** |
| B | Repo-keyed baseline diffed at the next SessionStart on every host | Rejected: a teammate's pull or a branch switch between sessions counts as learned; the baseline races across hosts |
| C | The agent is told to run a command | Rejected on criterion 1; kept as the `backfill` fallback |
| D | A cron or registry sweeper | Rejected: no session baseline, and the registry rots |

Built: `learning_report.capture()` is the one implementation. Claude's SessionEnd,
Codex's Stop (after verification, in a `finally`, within the 480 s hook timeout) and
Grok's passive Stop call it. Codex's and Grok's SessionStart store the inventory, and on
Grok the first Bash call stores it if SessionStart did not run. Stop runs every turn on
both hosts; a repeat records nothing because components already in the log for the repo
are skipped. `learning_report.py` is now in `RUNTIME_SCRIPTS`, and `inventory()` skips
skill folders holding `.kit-installed.json` and `kit_*` Codex agents.

## Q2: same plugin, separate plugins, or one neutral route

| | Candidate | Verdict |
|---|---|---|
| B2 | The Claude plugin stays the source. Grok reads it directly once a probe passes; Codex gets a plugin generated from `install_native.desired()` later; `install_native.py` is the fallback until then | **Chosen, staged** |
| A2 | One plugin for every host | Not distinct: Codex needs its own manifest (`.codex-plugin/plugin.json`), so it collapses into B2 |
| C2 | A neutral skill plus installer only | Rejected: no hooks, so no automatic capture or verification |
| D2 | Status quo repo copies | Kept as the transitional route; fails propagation (copies go stale until the installer is rerun) |

Grok reading the Claude plugin is supported by Grok's own docs and by the 2026-10-29 host
validation, where Grok discovered a user-level Claude-compatible kit plugin. What is not
known is whether that plugin's hooks run under Grok, whether `${CLAUDE_PLUGIN_ROOT}` resolves,
what it costs in context, and whether it double-fires with `.grok/hooks/kit.json`. The
learning check in `docs/HOST_VALIDATION.md` answers the first and fourth.

## What the critique changed

It moved the pick from B to A' on attribution. It found the learning code missing from
`RUNTIME_SCRIPTS`, the installer's own agents being reportable as learned, a 40 s sync able
to outlive a 30 s hook, that per-host plugins are not thin (Codex and Grok use different
stdin and stdout schemas), and that plugin plus native install would double-fire hooks.
All but the last are fixed here; the last is a check in the probe.

## What would change this decision

- Grok passive hooks do not run their command: capture moves fully to the first-Bash-call
  baseline plus a later Bash call as the trigger (attribution weakens). If Codex Stop also
  misses interrupted turns, reopen B.
- Pulls and branch switches between sessions are shown to be rare, or an author filter on
  git history is cheap and correct: B wins on one function and no per-host trigger.
- Codex plugin hooks need re-trust on each update, or the cache path changes per version:
  drop the Codex plugin and keep D2 with a better installer.
- The Claude plugin adds more than about 1k tokens per Grok session, or its hooks double-fire
  with `.grok/hooks/kit.json`: give Grok a thin generated plugin instead.

## Addendum, same day: what the VM showed

Running the install on V's VM (Grok 1.0.70) answered two of the open questions. The
answers come from Grok's built-in docs and `grok inspect`; no live model session was run.

- **Grok loads the Claude plugin, hooks included.** `grok inspect` listed the kit
  plugin's hooks file alongside `.grok/hooks/kit.json`, so SessionStart, the Bash guard
  and Stop each ran twice. Grok's `compat.claude.hooks` switch covers only hooks in
  Claude's settings files, not a plugin's, and there is no per-plugin hook switch.
- **Grok's Stop can block** in Claude's format, and every hook gets Claude's snake_case
  keys as well as Grok's own camelCase ones. Grok sets `GROK_HOOK_EVENT` on every hook.

What changed:
- The plugin's hooks exit when `GROK_HOOK_EVENT` is set and `.grok/hooks/kit.json` exists
  (`kit_common.stand_aside`).
- The native Grok Stop now verifies, like Codex's, with a 480-second timeout.

Native beats plugin under Grok because it is the one written for Grok. It is also the one
present when a repo is installed for Grok on a machine without the Claude plugin. The
reversal condition "double-fires with `.grok/hooks/kit.json`" is met. The thin
`.grok-plugin` it pointed to is not needed while this dedupe holds. Revisit if Grok stops
setting `GROK_HOOK_EVENT`, or starts translating payloads differently for plugin hooks.
