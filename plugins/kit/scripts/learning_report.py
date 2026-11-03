"""SessionEnd: record what this session added to the repo's toolkit, and send it to the kit.

A session that builds a skill, writes an agent, wires an MCP server or adds a dependency
has learned something the kit may need everywhere. Without this hook that knowledge stays
in one repo unless Claude remembers to say so. Here it is recorded deterministically:
the skills, agents, MCP servers and dependencies added since SessionStart go into the
learning backlog (`portable/kit-deliver/scripts/backlog.py`), names only.

When the repo opts in with `learning.report_to` in `.claude/kit.json` (the kit's
`OWNER/NAME`), queued entries — these and the ideas Claude recorded — are then sent there
as `kit-learning` issues with `gh`. Offline or signed out, they stay queued for the next
session. This posts outside the repo, which is why it is opt-in per repo.

Prints nothing and never fails the session.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from kit_common import changed_since, git, load_config, load_state, read_input, repo_root, save_state

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "portable" / "kit-deliver" / "scripts"))
import backlog  # noqa: E402

SKILL = re.compile(r"^(?:\.claude|\.agents|\.grok)/skills/([^/]+)/SKILL\.md$")
AGENT = re.compile(r"^(?:\.claude/agents/([^/]+)\.md|\.codex/agents/([^/]+)\.toml)$")
REQUIREMENT = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9_.-]*)", re.M)
PYPROJECT_DEP = re.compile(r'^\s*"([A-Za-z0-9][A-Za-z0-9_.-]*)\s*(?:\[[^\]]*\])?\s*(?:[<>=!~;][^"]*)?"\s*,?\s*$', re.M)
OWN = {"kit-deliver"}  # installed by the kit itself; not something the repo learned


def _before(root: Path, head: str, rel: str) -> str:
    return (git(["show", f"{head}:{rel}"], root) or "") if head else ""


def _now(root: Path, rel: str) -> str:
    try:
        return (root / rel).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _json_keys(text: str, *sections: str) -> Set[str]:
    try:
        data = json.loads(text) if text.strip() else {}
    except ValueError:
        return set()
    keys: Set[str] = set()
    for section in sections:
        value = data.get(section) if isinstance(data, dict) else None
        if isinstance(value, dict):
            keys.update(k for k in value if isinstance(k, str))
    return keys


def _requirement_names(text: str) -> Set[str]:
    return {m.group(1).lower() for m in REQUIREMENT.finditer(text)
            if not m.group(0).lstrip().startswith(("#", "-"))}


def detect(root: Path, head: str, changed: List[str]) -> List[Tuple[str, str]]:
    """(type, name) for each skill, agent, MCP server or dependency this diff added."""
    found: List[Tuple[str, str]] = []
    for rel in sorted(changed):
        if not (root / rel).is_file():
            continue
        name = Path(rel).name
        skill, agent = SKILL.match(rel), AGENT.match(rel)
        if skill and skill.group(1) not in OWN and not _before(root, head, rel):
            found.append(("skill", skill.group(1)))
        elif agent and not _before(root, head, rel):
            found.append(("agent", agent.group(1) or agent.group(2)))
        elif rel == ".mcp.json":
            added = _json_keys(_now(root, rel), "mcpServers") - _json_keys(_before(root, head, rel), "mcpServers")
            found.extend(("mcp-server", n) for n in sorted(added))
        elif name == "package.json":
            sections = ("dependencies", "devDependencies")
            added = _json_keys(_now(root, rel), *sections) - _json_keys(_before(root, head, rel), *sections)
            found.extend(("dependency", n) for n in sorted(added))
        elif name == "pyproject.toml":
            added = ({m.lower() for m in PYPROJECT_DEP.findall(_now(root, rel))}
                     - {m.lower() for m in PYPROJECT_DEP.findall(_before(root, head, rel))})
            found.extend(("dependency", n) for n in sorted(added))
        elif re.fullmatch(r"requirements[\w.-]*\.txt", name):
            added = _requirement_names(_now(root, rel)) - _requirement_names(_before(root, head, rel))
            found.extend(("dependency", n) for n in sorted(added))
    return [(t, n) for t, n in dict.fromkeys(found) if backlog.NAME.fullmatch(n)]


def main(backlog_file: Optional[Path] = None) -> int:
    data: Dict[str, Any] = read_input()
    session_id = str(data.get("session_id") or "")
    root = repo_root(data.get("cwd"))
    if root is None or not session_id:
        return 0
    path = backlog_file or backlog.DEFAULT
    state = load_state(session_id)
    baseline = state.get("baseline") or {}
    if baseline:
        changed = changed_since(root, baseline, state.get("edited", []))
        reported = set(state.get("reported", []))
        repo = backlog.repo_slug(root)
        for kind, name in detect(root, str(baseline.get("head") or ""), changed):
            if f"{kind}:{name}" in reported:
                continue
            backlog.add_component(path, kind, name, repo)
            reported.add(f"{kind}:{name}")
        state["reported"] = sorted(reported)
        save_state(session_id, state)
    learning = load_config(root).get("learning")
    target = learning.get("report_to") if isinstance(learning, dict) else None
    if isinstance(target, str) and backlog.SLUG.fullmatch(target):
        backlog.sync(path, target)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 — a hook must never break the session
        sys.exit(0)
