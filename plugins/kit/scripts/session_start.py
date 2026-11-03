"""SessionStart: record which files were already dirty, so verification only covers this session's changes,
and which skills, agents, MCP servers, dependencies and decision logs each repo already has,
so the session's Stop and SessionEnd report only what it added (`learning_report.begin`).

Prints nothing: SessionStart stdout would be added to Claude's context.
"""

from __future__ import annotations

import sys

from kit_common import load_state, prune_old_state, read_input, repo_root, save_state, snapshot


def main() -> int:
    data = read_input()
    session_id = str(data.get("session_id") or "")
    if not session_id:
        return 0
    try:  # also above the repos, as in a cloud session that starts in the home folder
        from learning_report import begin
        begin(session_id, data.get("cwd"))
    except Exception:  # noqa: BLE001 — without an inventory the session's end records nothing
        pass
    root = repo_root(data.get("cwd"))
    if root is None:
        return 0
    state = load_state(session_id)
    if state.get("baseline"):  # resume / compact: keep the original baseline
        return 0
    prune_old_state()
    state["baseline"] = snapshot(root)
    state.setdefault("edited", [])
    save_state(session_id, state)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 — a hook must never break the session
        sys.exit(0)
