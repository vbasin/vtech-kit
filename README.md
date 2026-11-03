# vtech-kit

One shared source for the Claude Code setup used across my repos (ML_analytics, Grok_Bot, and new builds). The repo is a **plugin marketplace** named `vtech-kit`. Each project turns its plugins on with a few lines in its own `.claude/settings.json`, so a change made here reaches every repo on its next update, without copying files around.

| Plugin | What it gives every repo that enables it |
|---|---|
| `kit` | One prompt is enough: `/kit:deliver` settles the approach, sizes the task and routes it, and a SessionStart hook tells Claude to use it. `/kit:deliberate` runs the design stage against `design-critic`. Plus hooks that lint and test what Claude changed before it can finish, guards on destructive commands, `reviewer` / `test-runner` / `security-reviewer` subagents, and the skills `/kit:verify`, `/kit:scale-out`, `/kit:spec`, `/kit:fix-issue`, `/kit:adopt` |
| `quant` | `causality-reviewer` (look-ahead leakage) and `query-reviewer` (SQL/TimescaleDB) subagents |

## What lives where

| Kind of thing | Where it goes | Why |
|---|---|---|
| Reusable hooks, agents, Claude-only skills | this repo | Edit once, every repo gets it |
| Project rules every agent should follow (Claude, Copilot, Codex, Grok) | the project's `AGENTS.md` | Vendor-neutral |
| Claude-specific project notes | the project's `CLAUDE.md`, which imports `@AGENTS.md` | When both files exist, Claude Code reads only `CLAUDE.md`. Without the import it never sees `AGENTS.md` |
| Skills shared with Copilot | the project's `.github/skills/`, symlinked into `.claude/skills/` | One copy serves both tools. Claude Code follows skill symlinks |
| Permissions, plugin switches | the project's `.claude/settings.json` | Repo-specific |
| Verify and guard settings (interpreter, lint args, test dirs, guards) | the project's `.claude/kit.json` | Repo-specific input to the shared hooks |

## Use it in a repo

1. Add this to the repo's `.claude/settings.json` (merge with what's there):

   ```json
   {
     "extraKnownMarketplaces": {
       "vtech-kit": { "source": { "source": "github", "repo": "vbasin/vtech-kit" } }
     },
     "enabledPlugins": { "kit@vtech-kit": true, "quant@vtech-kit": true }
   }
   ```

2. Add `.claude/kit.json`. Start from [`plugins/kit/templates/.claude/kit.json`](plugins/kit/templates/.claude/kit.json). Without it the guards still work, but nothing is linted or tested.
3. Start `claude` in the repo and accept the folder-trust prompt. Claude Code registers the marketplace and loads both plugins. Check with `/plugin`.

Or open Claude Code in the new repo after step 1 and run `/kit:adopt`, which walks through all of it.

**Private repo access:** if the marketplace is private, the machine running Claude Code needs git credentials for it. On the OCI VM, `gh auth login` followed by `gh auth setup-git` is enough. If the VM has no GitHub SSH key, also set `CLAUDE_CODE_PLUGIN_PREFER_HTTPS=1`.

## Updating

Plugins here have **no `version` field on purpose**, so installs track the latest commit on `main`. To pull changes into a machine:

```
/plugin marketplace update vtech-kit
```

To make updates automatic, open `/plugin` → **Marketplaces** → `vtech-kit` → **Enable auto-update**. If you ever need to hold a repo on a known-good state, add `"version": "x.y.z"` to the plugin's `plugin.json`. Users then only move when you bump it.

## One prompt, not a routing decision

The point of the setup is that the user describes the outcome and Claude works out the scale. Two pieces do that:

- **`/kit:deliver`** holds the protocol: frame the goal and its done-check, settle the approach, size the work, choose between this session, parallel subagents, `/batch` and an `ultracode:` workflow, brief the workers, run the repo's gates, run the matching reviewers, then report. It also names the harness gaps it hits (a missing scope, a rule worth writing) instead of silently widening the job.
- **`/kit:deliberate`** is the design stage `deliver` hands off to whenever the shape of the work is not obvious. It looks up what already exists, writes the criteria *before* any candidate so the first idea can't be rationalised, generates two to four designs that differ in structure rather than detail, has `kit:design-critic` attack them from a fresh context, revises, and attacks again until no surviving objection changes the pick or three rounds are up. Then it records the decision, the rejected options and what would reverse it.
- **`kit:design-critic`** is that adversary. It checks the *field* and the *criteria* before the candidates, because a missing candidate matters more than a flaw in a present one, and it has to pick one and name the fact that would change its mind.
- **`session_policy.py`** prints that instruction into every session, so Claude reaches for `deliver` on its own. The design is never the user's to supply: requirements are theirs, the approach is Claude's.

A repo points `deliver` at its own gates and reviewers in `.claude/kit.json`:

```json
"routing": {
  "note": "Work happens on dated branches; AGENTS.md governs.",
  "decisions": "docs/decisions",
  "gates": [ { "paths": ["src/acquisition/**"], "command": "make test-acquisition" } ],
  "reviewers": [ { "paths": ["src/features/**", "src/labels/**"], "agent": "quant:causality-reviewer" } ]
}
```

`decisions` is where `deliberate` writes its decision logs. Unset, it falls back to the repo's own convention, then to `.claude/decisions/`.

`"routing": { "enabled": false }` turns the session policy off for a repo that doesn't want it.

## How verification works

`kit` runs six small stdlib-only Python hooks (`plugins/kit/scripts/`):

| Hook | Script | Does |
|---|---|---|
| SessionStart | `session_start.py` | Records which files were already dirty, so only this session's changes are checked |
| SessionStart | `session_policy.py` | Prints the routing policy into the session, so one prompt reaches both the right approach and the right scale of work |
| PreToolUse (Bash) | `bash_guard.py` | Asks before `DROP`/`TRUNCATE`/`DELETE` without `WHERE`/chunk operations, before full-suite pytest runs (opt-in), and before repo-specific patterns |
| PostToolUse (Edit/Write) | `post_edit.py` | Remembers the edited file and lints it at once |
| Stop | `stop_verify.py` | Lints the session's changed `.py` files and runs the tests related to them, as explicit paths: their own test files and every test whose imports reach them, nearest first, up to `tests.max_files`. On failure it sends the output back so Claude keeps working. Gaps it cannot close (files no gate covers, tests left out by the cap) are shown to you once and do not block. A diff that already passed is not re-run on the next turn. If Claude stops again with the same failure, it lets the stop through with a notice, so a pre-existing failure can't trap a session |
| SessionEnd | `learning_report.py` | Records the skills, agents, MCP servers and development tools the session added, and sends the repo's queued learning to the kit repo when both the machine and the repo opt in (see below) |

Related tests are found through the repo's own import graph, so a test of `b.py` still runs when `a.py`, which `b` imports, changed. Deleted files are included. Each run starts with an empty bytecode cache, and an interpreter without pytest counts as a failure rather than a pass. `tests.scope: "all"` runs every test file instead, for a repo whose whole suite is cheap. In a large repo many edits reach most of the suite through shared modules (in ML_analytics, editing `src/common/config.py` reaches 162 of 180 test files), so there the cap is what keeps the hook fast and `verification.gates` is the better tool: the repo says what covers what instead of the hook inferring it. A repo can add guard patterns in `.claude/kit.json`:

```json
"guards": { "patterns": [ { "regex": "databento.*(download|get_range)", "reason": "Check the data cost first", "decision": "ask" } ] }
```

## Does the kit help?

`plugins/kit/evals/` is a `claude plugin eval` suite. Every case runs with the
plugin and again with no plugin at all, so the number it reports is **Δ** — what the kit
contributed, not what the model would have done anyway.

```bash
claude plugin eval ./plugins/kit --ablation with-without
```

| Case | Claim it can falsify |
|---|---|
| `task-first` | The kit's improvement loop never runs ahead of the user's task (a guard: Δ near zero is the pass) |
| `capability-required` | A missing prerequisite is obtained during the task, not deferred (a guard: Δ near zero is the pass) |
| `reusable-capability` | A repeated gap leads to a tested reusable candidate |
| `learning-becomes-a-pr` | Learning reported by other repos becomes a scored PR, most-reported first, merged only by you |
| `design-not-handed-back` | The design is Claude's to settle, by comparing rivals against stated criteria |
| `obvious-fix-stays-cheap` | The kit doesn't charge design ceremony for a one-line fix |
| `duplication-is-a-defect` | Duplication and dead config are reportable defects, named at their location |
| `sweep-gets-fanned-out` | Independent work that can't be scripted gets fanned out, and one route is chosen |
| `prior-decision-is-reused` | A decision already on record is reused, not re-derived, while its reversal conditions hold |
| `reversed-decision-reopens` | The same log with a reversal condition now true: the decision reopens from the recorded candidates, not from scratch |
| `promotion-is-offered-not-taken` | A generic addition is offered to the kit with its cost; a repo-specific one stays local |
| `delivery-applies-the-floor` | A task-shaped request gets the delivery protocol, and the fix lands at the cause rather than at the line pointed at |

`obvious-fix-stays-cheap` is the case the kit can lose: its pass condition is Δ near zero,
because every always-on token the kit adds is paid on trivial prompts too. A suite made
only of cases the kit is designed to win would never report that.

The suite spends plan usage — 3 runs × 2 arms is six agent runs per case — so it is run by
hand, not in CI, which would need an `ANTHROPIC_API_KEY` a subscription doesn't provide.
`python -m pytest tests/test_evals.py` checks the suite's schema for free.
[`plugins/kit/evals/README.md`](plugins/kit/evals/README.md) covers the dials and how to
read a number you can trust; the reasoning is in
[docs/decisions/](docs/decisions/2026-10-29-measure-the-kit-with-plugin-evals.md).

## Develop

```bash
pip install pytest ruff
python -m pytest tests          # hook tests + eval-suite schema (no network, no model calls)
claude plugin validate .        # marketplace + both plugins
claude --plugin-dir ./plugins/kit   # try local edits without pushing
```

CI runs both on every PR (`.github/workflows/validate.yml`). It does not run the eval
suite: that needs model credentials in the environment, and a Claude subscription
isn't one.

## Background

The research and plan behind this setup are in [docs/PLAN.md](docs/PLAN.md).

## Language-neutral verification gates

For projects with a trusted, scoped test target, enable `verification` in `.claude/kit.json`:

```json
"verification": {
  "enabled": true,
  "gates": [
    {"name": "web-tests", "paths": ["web/**"], "argv": ["npm", "test", "--", "--run"], "timeout_s": 300},
    {"name": "api-tests", "paths": ["api/**"], "argv": ["make", "test-api"], "timeout_s": 300}
  ]
}
```

Every changed path must match a gate. Commands run as argument arrays without a shell;
`cwd` is optional and must remain within the repository. Gates replace automatic pytest
selection when enabled; Python lint settings still apply. Gate coverage is a repo contract:
include affected integration tests and shared configuration paths. Test commands execute
with the user's privileges. The manual verifier and Stop hook use the same gate runner.

## Learning from the repos that use the kit

Repos that use the kit send what they learned back here, so it can become a PR.

1. **Capture, in each repo, automatically, on Claude Code, Codex and Grok.** Claude Code's
   `learning_report.py` SessionEnd hook, and the Stop hooks `install_native.py` writes for
   Codex and Grok, compare the repo's skills, agents, MCP servers and development dependencies (groups named dev, test, lint, docs and the like,
   `devDependencies`, `requirements-dev*.txt`) with what was there at SessionStart, and
   records what is new. Runtime dependencies are not recorded. Claude records improvement
   ideas with `backlog.py add` as it works. Entries hold the repo name, the kind of thing,
   its name and, for ideas, a few lines of text with anything that looks like a credential
   or a home-directory path redacted before sending. They queue in
   `~/.vtech-kit/learning.jsonl` on that machine (`KIT_LEARNING_FILE` moves it).
2. **Send, when the machine and the repo both opt in.** The machine names the kit repo once;
   the repo's `.claude/kit.json` consents with `"learning": {"share": true}` (the template
   sets it). A repo's file can consent but cannot choose the destination, so a cloned repo
   cannot point your `gh` login somewhere else, and a session only ever sends its own repo's
   entries. They go as issues labelled `kit-learning`; the same gap from another repo is a
   comment on the existing issue (reopening it if it was closed), so the number of repos on
   an issue is how often it recurs. Offline or signed out, entries wait for the next session.
3. **Improve, here.** In this repo, `/kit:improve` reads the open `kit-learning` issues,
   starts with the one the most repos reported, builds and scores a candidate, and opens a
   PR that closes the issue. Nothing merges without you.

Turning sending on, once per machine (the VM, your laptop), in a terminal:

1. Check `gh` is signed in: type `gh auth status` and press Enter. If it says you are not
   logged in, type `gh auth login`, press Enter, and pick GitHub.com, HTTPS, and "Login with
   a web browser"; it prints a code and a link, open the link, paste the code.
2. Name the kit repo as this machine's target. Copy this whole line, paste it, press Enter:
   `python3 "$(find ~/.claude/plugins -path '*kit-deliver/scripts/backlog.py' | head -1)" target vbasin/vtech-kit`
   It prints `{"report_to": "vbasin/vtech-kit"}`. If it prints an error about a missing
   file instead, open Claude Code, type `/plugin marketplace update vtech-kit`, press Enter,
   quit Claude Code, and run the line again.
3. Any time later, the same line with `status` in place of `target vbasin/vtech-kit` shows
   what is queued per repo and how the last send went; with `target ""` it stops sending
   from this machine.

A repo adopted before this change needs `"learning": {"share": true}` added to its
`.claude/kit.json`. SessionEnd reports only what a session adds, so what a repo built
before this hook existed is sent once by hand. From inside that repo's folder, paste and
press Enter:
`python3 "$(find ~/.claude/plugins -path '*kit/scripts/learning_report.py' | head -1)" backfill --claim-ideas`
It prints what it recorded and sent. Running it by hand is the repo's consent, so it does
not need `learning.share`; it does need the machine's target (step 2 above). It reads the
working tree, so anything built but not yet committed on that machine is included.
Running it again sends nothing new. `--claim-ideas`
also tags this machine's older ideas, recorded before entries carried a repo name, with
this repo; leave it off in all but the repo where those ideas came from. If this repo is public, the issues are public too: they show repo
names, component names and idea text.

## Learning reusable skills

`/kit:improve` turns a recurring gap into a candidate skill and evaluates it before
promotion. Candidates contain `candidate.json` (name, purpose, applies_when, not_for,
evidence) and `skill/SKILL.md`. Prepare a trusted, separate JSON suite such as:

```json
{"cases": [
  {"name": "improvement", "fixture": "fixtures/improvement", "argv": ["python3", "check.py"]},
  {"name": "regression", "fixture": "fixtures/regression", "argv": ["python3", "check.py"]}
]}
```

Each check runs twice in fresh copies of its fixture: `KIT_CANDIDATE_DIR` is empty in the
baseline arm and points to the candidate skill in the other arm. Exit 0 means the check
passed. The suite must observe an improvement and preserve at least one baseline success without candidate failures. `learning.py
promote` reruns the suite and refuses to overwrite an existing skill. Use only trusted
suite commands; they execute with your privileges. Model-dependent behavioral evals and
independent review remain necessary before releasing a shared skill.

## Task-first workflow for Claude, Codex, and Grok

Claude's `/kit:deliver` reads the shared portable protocol. To install that same protocol
and its supporting scripts and review briefs into a target repository for Codex, Grok Build, or Claude Code:

```bash
python plugins/kit/scripts/install_portable.py --repo /path/to/project --host codex
python plugins/kit/scripts/install_portable.py --repo /path/to/project --host grok
python plugins/kit/scripts/install_portable.py --repo /path/to/project --host claude
```

Codex discovers the copy in `.agents/skills/kit-deliver`, Grok Build discovers its copy in
`.grok/skills/kit-deliver`, and Claude Code can discover its copy in
`.claude/skills/kit-deliver`. The installer leaves pre-existing unmanaged or modified skills
alone; an unchanged managed installation is updated safely. Host trust, skill activation,
hooks, permissions, available tools, and subagent behavior differ. Explicitly invoke the
`kit-deliver` skill if it is not selected, and use a sequential fallback when a host lacks a
capability. A bare model API needs a tool-capable host. No model parity is implied.

For host-neutral repository checks, place the existing gate configuration in
`.kit/kit.json`; `.claude/kit.json` takes precedence if both exist. Run
`python <installed-skill>/scripts/stop_verify.py --manual <changed-file>` from the target
repo. These are explicit commands on Codex and Grok; Claude's native hooks remain separate.

Learning capture needs a hook, so on Codex and Grok it comes from `install_native.py`
(which also installs the portable skill), not from `install_portable.py` alone. A copy-only
install captures nothing automatically; run `learning_report.py backfill` from the
installed skill's `scripts/` instead. Which distribution each host should use next (Grok
reading the Claude plugin, a Codex plugin) is in
`docs/decisions/2026-11-03-learning-capture-and-distribution-across-hosts.md`.

For a necessary missing capability, search existing tools, research current sources, then
adapt or build what is needed to finish the task. Record optional improvements without
secrets using `<installed-skill>/scripts/backlog.py` and evaluate them **after** delivery.
The default backlog lives in `~/.vtech-kit/learning.jsonl`, outside the target project.
Use `backlog.py --help` to add, list, and resolve entries. Candidate evaluation and
promotion use the bundled `scripts/learning.py`; a trusted independent suite is required.
A validated host-neutral skill marked with `.kit-portable` in `plugins/kit/skills` is
installed for Codex and Grok on the next installer run and is discoverable by the Claude
plugin. Skills removed from the source are not automatically deleted from existing repos;
review and remove outdated copies manually.

For numeric comparisons, run `python <installed-skill>/scripts/score.py
examples/seo-comparison.json`. Each criterion declares direction, bounds, weight and an
optional hard threshold; every option needs a value and evidence for each criterion. An
unknown value is incomplete, not zero. Weights and bounds are assumptions: scores describe
those assumptions and cannot prove global optimality. The SEO file contains synthetic
example data, not actual site or ranking results.

### Native Codex and Grok integration

For a repository where you want the portable workflow plus host-native integration:

```bash
python plugins/kit/scripts/install_native.py --repo /path/to/project --host codex
python plugins/kit/scripts/install_native.py --repo /path/to/project --host grok
```

Run the appropriate command from a checkout of this kit. The installer appends a marked
workflow block to the target repository's `AGENTS.md` and retains its existing content.
It upgrades files it wrote itself and you have not edited (hashes in `.codex/.kit-native.json`
and `.grok/.kit-native.json`). It refuses to replace other existing hook or agent files or a
modified kit block; reconcile those manually. Review and trust
Codex hooks through `/hooks`; Grok project hooks
require `/hooks-trust`. Run `grok inspect` to inspect Grok discovery.

Codex gets a SessionStart policy, a Bash guard, a verification Stop hook and four reviewer
profiles. Grok gets a SessionStart baseline, a Bash deny hook and, from Grok 1.0.70, a Stop
hook that verifies and can block the turn, like Codex's. Both Stop hooks also capture
learning. Grok loads a user-level Claude kit plugin with its hooks; those stand aside, event
by event, where `.grok/hooks/kit.json` handles the same event, so nothing runs twice. Grok
can use native subagents with the portable review briefs. A guard configured as `ask` is not an enforceable approval prompt in
these adapters; use the host's own permission controls. See the installed skill's
`references/hosts.md` for availability, permissions, cloud-surface and validation limits.

### Recorded host validation

See [host validation](docs/HOST_VALIDATION.md) for the tested CLI versions, actual
Codex hook/reviewer evidence, the live Grok learning-capture check (passed on 1.0.70), and
a small reproducible smoke procedure. Native configuration must be loaded and trusted; installing files
alone is not evidence that hooks or named agents ran.
