# vtech-kit

This repo is a Claude Code plugin marketplace (`vtech-kit`) consumed by other repos. See README.md for the layout.

- Hook scripts in `plugins/kit/scripts/` must stay **stdlib-only Python 3.8+**: they run on machines whose project venv may not exist yet. They must never crash or print to stdout unless they mean to (SessionStart stdout goes into Claude's context). Wrap `main()` so unexpected errors exit 0.
- Every change to a hook needs a case in `tests/test_hooks.py`. Run `python -m pytest tests` and `claude plugin validate .` before committing.
- A change to what a skill, agent or the policy *tells Claude to do* is a behaviour change, and the only thing that scores it is `plugins/kit/evals/`. Add or adjust a case when you add or change a claim, and keep `python -m pytest tests/test_evals.py` green — it is free. Running the suite spends plan usage, so run it when a claim is in doubt, not on every commit.
- Plugins carry no `version`, so every commit to `main` ships to every repo on its next `/plugin marketplace update`. Treat `main` as released.
- Keep model-invocable skill descriptions short, because each one costs context in every session of every repo. Use `disable-model-invocation: true` for skills only a user should start.
- Plugin agents can't use `hooks`, `mcpServers` or `permissionMode` frontmatter (ignored for plugins). Use `tools`/`disallowedTools`.
