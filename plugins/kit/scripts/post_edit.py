"""PostToolUse (Edit|Write|MultiEdit): remember the edited file and lint it right away.

Lint uses the repo's `lint.args` from `.claude/kit.json` (run with the configured Python),
so errors come back to Claude while the edit is still in context instead of at the end.
"""

from __future__ import annotations

import subprocess
import sys

from kit_common import (
    emit,
    load_config,
    load_state,
    read_input,
    record_edit,
    relative_to_root,
    repo_root,
    resolve_python,
    run_lint,
    save_state,
)


def main() -> int:
    data = read_input()
    tool_input = data.get("tool_input") or {}
    file_path = tool_input.get("file_path") or tool_input.get("notebook_path")
    root = repo_root(data.get("cwd"))
    if not file_path or root is None:
        return 0
    rel = relative_to_root(root, file_path)
    if rel is None:
        return 0

    session_id = str(data.get("session_id") or "")
    if session_id:
        state = load_state(session_id)
        record_edit(state, root, rel)
        try:
            save_state(session_id, state)
        except OSError:
            pass  # the lint below still has to run

    config = load_config(root)
    lint = config.get("lint", {})
    if not lint.get("enabled") or not rel.endswith(".py"):
        return 0
    python = resolve_python(config, root)
    if python is None:
        return 0
    try:
        report = run_lint(root, python, lint.get("args", []), [rel], timeout=60)
    except (OSError, subprocess.SubprocessError):
        return 0
    if report:
        emit({"decision": "block", "reason": f"Lint errors in {rel} (fix these before moving on):\n{report}"})
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 — a hook must never break the session
        sys.exit(0)
