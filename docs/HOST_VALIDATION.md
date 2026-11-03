# Native host validation — 2026-10-29

Date uses America/Chicago. Tests ran on a Linux ARM64 machine against the merged
PR #13 source, `c5a90c2cb30c8f6296c39e5bee94bd6af685e6c5`, followed by the four
review fixes in this change. The merge is already on `main`; its
[GitHub validation run passed](https://github.com/vbasin/vtech-kit/actions/runs/41002247871).

## Observed behavior

| Check | Codex CLI 0.164.0-alpha.11.1 | Grok Build 1.0.60 |
| --- | --- | --- |
| Project skill discovery | `kit-deliver` discovered and read in a live model session | `grok inspect` discovers `kit-deliver` after project trust |
| Native hook discovery | SessionStart policy observed with normal config loading | `grok inspect` lists the project Bash hook after trust |
| Harmless Bash denial probe | Actual shell call blocked by the native PreToolUse hook | Direct adapter returns deny; model-run enforcement remains unverified |
| Deliberate failing gate | Manual verifier exits 1; automatic Stop hook reports both failed tests and continues the turn | Manual verifier exits 1; live model session blocked by expired authentication |
| Corrected gate | Manual verifier exits 0 after changing subtraction to addition | Pending live model test |
| Independent reviewer | Actual `spawn_agent` call with `agent_type: kit_reviewer`; review completed with no findings and no edits | Pending live model test |

The native Stop failure was observed on the merged source. The corrected-code
phase used a refreshed installation containing the four review fixes. Refreshing
both host installations correctly reported `installed` for the portable updates.

This is a bounded smoke test, not a claim of host parity or improved task quality.
Claude was not tested. Windows process-tree cleanup was not tested on Windows.
The Grok installation also discovers an existing user-level Claude-compatible kit
plugin: inspect overlapping hooks before treating it as an isolated native run.

## Configuration and trust matter

An initial Codex experiment used `--ignore-user-config`. That invocation discovered
the portable skill but did not expose the named reviewer or run the project hooks,
even with a project trust override. With normal config loading, the same files
exposed `kit_reviewer`, supplied the SessionStart policy, and denied the probe.
Do not use that initial invocation as evidence that native hooks work or as a
recommended native smoke command.

For this automation, the generated hook commands and scripts were inspected first.
Codex used a per-invocation project trust override and the documented
`--dangerously-bypass-hook-trust` option for those vetted hooks; no persistent Codex
hook trust was written. This does not test the interactive `/hooks` trust flow.
Normal installation should use `/hooks` to review and trust hook definitions.
See [OpenAI hook documentation](https://learn.chatgpt.com/docs/hooks).

Grok initially excluded the untrusted project's skill, instructions, and hook.
`grok --trust inspect` then discovered all three and persisted trust for this newly
created smoke repository. The live request failed before inference because saved
authentication was rejected (`invalid_grant` / not signed in). Run
`grok login --device-code` in an operator terminal, then repeat the live test.
The operator chose to leave live Grok testing pending for this change.
Do not put credentials in evidence files. Grok's passive Stop event cannot enforce
verification; explicitly invoke the manual verifier before reporting.
See [Grok hook documentation](https://docs.x.ai/build/features/hooks).

## Reproduce in a small repository

Use separate new Git repositories for Codex and Grok. Create `calculator.py`:

```python
def add(a: int, b: int) -> int:
    return a - b  # Deliberately wrong for the initial gate.
```

Create `test_calculator.py`:

```python
import unittest
from calculator import add

class CalculatorTest(unittest.TestCase):
    def test_positive(self):
        self.assertEqual(add(2, 3), 5)

    def test_negative(self):
        self.assertEqual(add(-2, -3), -5)
```

Put this trusted configuration in `.kit/kit.json`:

```json
{
  "verification": {
    "enabled": true,
    "gates": [{
      "name": "calculator-unit",
      "paths": ["calculator.py", "test_calculator.py", ".kit/*"],
      "argv": ["python3", "-m", "unittest", "-v", "test_calculator"],
      "timeout_s": 10
    }]
  },
  "guards": {
    "patterns": [{
      "regex": "KIT_GUARD_PROBE",
      "decision": "deny",
      "reason": "Harmless kit smoke denial probe"
    }]
  }
}
```

Install with `install_native.py --repo PATH --host codex` or `--host grok`, inspect
and trust the generated configuration, then commit the initial fixture. Ignore
`__pycache__/`. Scope the session to `calculator.py` edits and prohibit changes to
the verifier, tests, and hook settings.

1. Ask the host to use `kit-deliver`, report discovery, and execute exactly
   `printf KIT_GUARD_PROBE` once. Expect denial; do not retry around a denial.
2. Run `python3 <skill>/scripts/stop_verify.py --manual calculator.py`. Expect exit
   1 and two failed tests. `<skill>` is `.agents/skills/kit-deliver` for Codex or
   `.grok/skills/kit-deliver` for Grok.
3. In a separate Codex phase, add only a comment to `calculator.py` and attempt to
   finish without manually verifying. Expect the Stop hook to report failure.
   Leave arithmetic unchanged for this phase and report the failure honestly.
4. Ask the host to correct subtraction to addition, run the same manual verifier,
   and request one independent reviewer. Codex should use `kit_reviewer`; Grok
   should use an available native subagent with the installed reviewer brief.
5. Inspect tool events and results, not only the model's summary. Expect the
   corrected gate to exit 0 and the reviewer to finish without changing files.

## Four PR #13 review fixes

- Non-boolean `verification.enabled` values now fail strict configuration loading
  before selecting gate mode; malformed values cannot silently suppress pytest.
- Candidate evaluation/promotion and portable installation share the same
  frontmatter-name check. Missing, duplicate, or mismatched names are rejected.
- Gates start in a separate process group on POSIX and kill that group on timeout.
  Regression cases include a running wrapper and an exited wrapper whose child
  retains output pipes. Windows uses `taskkill /T /F`; Windows behavior remains
  unverified. Processes deliberately escaping their group are outside this bound.
- Native installer status now includes portable skill additions and updates.

Scoped validation: **144 tests passed in 37.20 seconds** across
`tests/test_hooks.py`, `tests/test_learning.py`, `tests/test_native.py`, and
`tests/test_portable.py`. Ruff on changed scripts/tests and `git diff --check`
passed. An independent read-only code review found no blocking regression.
The full suite was not run.

## PR #14 cancellation follow-up

Copilot identified that Ctrl-C bypassed timeout-only cleanup after gates were
detached into their own process group. The manual verifier could exit while a
gate worker continued writing to the workspace. The cleanup handler now also
catches `KeyboardInterrupt`, kills the group/tree, reaps the direct child, and
re-raises the interrupt.

Two real SIGINT regression cases reproduced the unwanted write before the fix:
one with a live wrapper and one with an exited wrapper whose child retains its
output pipes. Both pass after the fix. Tests release the worker only after the
verifier exits and clean up their process groups even on assertion failure.
Scoped `tests/test_hooks.py` and `tests/test_native.py` checks, Ruff on changed
Python files, and `git diff --check` passed. An independent read-only review found
no blocking issue. Windows cancellation behavior remains unverified.

## Local evidence

The operator's smoke repositories and raw transcripts are under
`/home/ubuntu/kit-host-smoke/`. They are outside this repository and are not
published; authentication diagnostics can contain sensitive metadata.

Relevant evidence files under its `evidence/` directory:

- `codex-config-check.jsonl`: named role discovery and actual guard denial.
- `codex-stop.jsonl`: automatic failing gate and resumed model report.
- `codex-fix-review.jsonl`: failing gate, correction, passing gate, reviewer phase.
- `codex-reviewer-evidence.json`: selected native spawn fields from the local
  session record, confirming the custom agent type without copying prompt content.
- `grok-inspect-trusted.txt`: project discovery after trust.
- `grok-manual-fail.txt`: failing manual gate.

The earlier `codex-discovery` and `codex-hooks` logs used
`--ignore-user-config`; retain them as diagnostic evidence of that limitation.

## Learning capture check (added 2026-11-03; Grok passed, Codex pending)

Tests cover the Codex and Grok capture hooks by calling them directly. What they cannot
show is whether a live host calls them: Codex's Stop is documented to run every turn, and
Grok documents SessionStart and Stop as passive (output ignored) without saying whether the
command still runs. This check answers that, and whether a user-level Claude kit plugin
doubles Grok's hooks. Nothing is sent anywhere: the log goes to a scratch file and no
target is set for it.

In a terminal on the machine with Codex or Grok signed in, paste each line and press Enter:

1. `mkdir -p ~/kit-learning-check && cd ~/kit-learning-check && git init -q && git remote add origin https://github.com/acme/check.git`
2. `python3 /path/to/vtech-kit/plugins/kit/scripts/install_native.py --repo . --host grok`
   (or `--host codex`; replace `/path/to/vtech-kit` with where the kit is checked out).
3. `export KIT_LEARNING_FILE=~/kit-learning-check.jsonl`
4. Start the host in this folder (`grok` or `codex`), trust the project hooks when asked
   (`/hooks-trust` on Grok, `/hooks` on Codex), and type:
   `Create .agents/skills/check-skill/SKILL.md with a name and description, then stop.`
5. Quit the host, then run `cat ~/kit-learning-check.jsonl`.

Pass: one line containing `"name": "check-skill"`. No file, or no such line, means the
host did not run the hooks; record which. For the doubling question on Grok, also run
`grok inspect` and note whether `SessionStart` and `Stop` are listed twice (once from
`.grok/hooks/kit.json`, once from a Claude kit plugin).

### Result on Grok 1.0.70 (2026-11-03, V's VM)

Passed. Run headless with `grok --trust --permission-mode acceptEdits --allow 'Write' -p
"<the prompt above>"` (without `--allow 'Write'` a headless run refuses the write, since it
cannot ask for approval). The log held one line:
`{"kind":"component","type":"skill","name":"check-skill","repo":"acme/check","status":"proposed",...}`.
So live capture works on Grok: the native Stop ran and recorded the new skill. This does
not show which hook set the start inventory, SessionStart or the first-Bash-call fallback. Doubling was settled
earlier the same day: `grok inspect` did list the Claude plugin's hooks too, and the plugin's
hooks now stand aside per event (`kit_common.stand_aside`). Codex's hooks were trusted in the VS Code extension the
same day; only its live run is still pending.

## Cloud session learning check (2026-11-03, claude.ai cloud container)

Passed. Claude Code 2.1.317, the kit loaded with `--plugin-dir` from this branch, a headless
session started in a folder *above* a fresh checkout (the cloud layout), asked to create
`.claude/skills/site-audit/SKILL.md`. The learning log recorded the skill with
`context.what` from its description; the Stop hook asked for the reason and to commit; the
session ran `backlog.py why` ("the project had no skill covering site auditing...") and
committed the skill by explicit path. Separately, the REST listing `gh api
repos/vbasin/vtech-kit/issues?labels=kit-learning...` and the label lookup returned the
real issues from the same container, where `gh issue list` is refused (GraphQL). Not run
live: creating an issue, which would have posted to the real kit repo.
