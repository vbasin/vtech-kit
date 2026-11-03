"""SessionStart: record which files were already dirty, so verification only covers this session's changes,
and which skills, agents, MCP servers and dependencies the repo already has, so SessionEnd
reports only what this session added (`learning_report.py`).

Prints nothing: SessionStart stdout would be added to Claude's context.
"""

from __future__ import annotations

import sys

from kit_common import load_state, prune_old_state, read_input, repo_root, save_state, snapshot


def main() -> int:
    data = read_input()
    session_id = str(data.get("session_id") or "")
    root = repo_root(data.get("cwd"))
    if not session_id or root is None:
        return 0
    state = load_state(session_id)
    if state.get("baseline"):  # resume / compact: keep the original baseline
        return 0
    prune_old_state()
    state["baseline"] = snapshot(root)
    state.setdefault("edited", [])
    try:
        from learning_report import inventory
        state["components"] = inventory(root)
    except Exception:  # noqa: BLE001 — without an inventory SessionEnd records nothing
        pass
    save_state(session_id, state)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 — a hook must never break the session
        sys.exit(0)
