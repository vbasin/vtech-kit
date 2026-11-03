"""SessionEnd: record what this session added to the repo's toolkit, and send it to the kit.

A session that builds a skill, writes an agent, wires an MCP server or adds a development
tool has learned something the kit may need everywhere. Without this hook that knowledge stays
in one repo unless Claude remembers to say so. Here it is recorded deterministically:
SessionStart stores an inventory of the repo's skills, agents, MCP servers and
development dependencies (`inventory`), and SessionEnd records what is in the inventory now and was
not then, names only, in the learning backlog (`portable/kit-deliver/scripts/backlog.py`).
Comparing inventories, not git history, means a file that was already dirty or untracked
when the session started is not mistaken for something this session added.

Runtime dependencies are not recorded: which data library a project uses is that
project's business, and would bury the signal. Development tools (test runners, linters,
browsers for tests) are what a workflow kit can act on.

Two keys must both be set to send: the machine owner's target (`backlog.py target
OWNER/NAME`, kept next to the log) and the repo's consent (`"learning": {"share": true}`
in `.claude/kit.json`). Then this repo's queued entries, these and the ideas Claude
recorded here, go to the target as `kit-learning` issues with `gh`. A repo's committed
config can consent but never choose the destination, so a cloned repo cannot redirect
this machine's `gh` login; and entries from other repos on the same machine are never
sent from here. Offline or signed out, entries stay queued for the next session.

Prints nothing and never fails the session.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from kit_common import git, load_config, load_state, read_input, repo_root, save_state

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / "portable" / "kit-deliver" / "scripts"))
try:
    import backlog  # noqa: E402
except Exception:  # noqa: BLE001 — a partial install must not break the session
    backlog = None  # type: ignore[assignment]

# Installed by the kit itself (install_portable.py), so not something the repo learned.
OWN = {"kit-deliver"} | {p.parent.name for p in (PLUGIN / "skills").glob("*/.kit-portable")}
DEV_REQUIREMENTS = re.compile(
    r"(?:requirements[\w.-]*(?:dev|test|lint|doc)[\w.-]*|(?:dev|test)[\w-]*requirements)\.txt")
MAX_MANIFESTS = 200
PACKAGE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9_.-]*)")
SECTION = re.compile(r'^\s*\[\[?\s*([A-Za-z0-9_][A-Za-z0-9_.\-" ]*?)\s*\]\]?\s*(?:#.*)?$')
KEY = re.compile(r'^\s*"?([A-Za-z0-9_.-]+)"?\s*=\s*')
# An extra or a group counts as development tooling only by its name: an extra such as
# `postgres = ["psycopg"]` is a runtime choice.
DEV_GROUP = re.compile(r"^(?:dev|develop|development|test|tests|testing|lint|linting|docs?|typing|"
                       r"types|typecheck|mypy|qa|ci|check|style|format|bench|benchmarks?)(?:[-_].*)?$",
                       re.IGNORECASE)
POETRY_DEV = re.compile(r"^tool\.poetry\.(?:group\.([^.]+)\.dependencies|dev-dependencies)$")


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
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


def _array_strings(text: str, start: int) -> List[str]:
    """The quoted strings of the TOML array opening at text[start] == "[", skipping inline
    tables (a dependency group's `{include-group = ...}`)."""
    found: List[str] = []
    depth = braces = 0
    i = start
    while i < len(text):
        ch = text[i]
        if ch in "\"'":
            end = text.find(ch, i + 1)
            if end < 0:
                break
            if braces == 0:
                found.append(text[i + 1:end])
            i = end
        elif ch == "#":
            newline = text.find("\n", i)
            i = len(text) if newline < 0 else newline
        elif ch == "{":
            braces += 1
        elif ch == "}":
            braces -= 1
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                break
        i += 1
    return found


def pyproject_dev_dependencies(text: str) -> Set[str]:
    """Package names in pyproject.toml's development groups: the extras, dependency groups
    and Poetry groups named for development (`DEV_GROUP`), and Poetry's dev table. Runtime
    `project.dependencies` and runtime extras are not read.
    A small reader rather than a TOML parser, because Python 3.8-3.10 have none; any other
    array (pytest's `testpaths`, say) is not a dependency and is not read."""
    names: Set[str] = set()
    section = ""
    offset = 0
    for line in text.splitlines(keepends=True):
        header = SECTION.match(line)
        key = None if header else KEY.match(line)
        if header:
            section = header.group(1).replace('"', "").replace(" ", "")
        elif key:
            name, rest = key.group(1), line[key.end():]
            poetry = POETRY_DEV.match(section)
            if poetry:
                if name != "python" and (poetry.group(1) is None or DEV_GROUP.match(poetry.group(1))):
                    names.add(name)
            elif section in ("project.optional-dependencies", "dependency-groups") \
                    and DEV_GROUP.match(name) and rest.lstrip().startswith("["):
                for spec in _array_strings(text, offset + key.end() + rest.index("[")):
                    match = PACKAGE.match(spec)
                    if match:
                        names.add(match.group(1))
        offset += len(line)
    return {n.lower() for n in names}


def requirement_names(text: str) -> Set[str]:
    names = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "-")) or "://" in line:
            continue
        match = PACKAGE.match(line)
        if match:
            names.add(match.group(1).lower())
    return names


def _manifests(root: Path) -> List[str]:
    listed = git(["ls-files", "-co", "--exclude-standard"], root) or ""
    picked = [rel for rel in listed.splitlines()
              if Path(rel).name in ("package.json", "pyproject.toml")
              or DEV_REQUIREMENTS.fullmatch(Path(rel).name)]
    return sorted(picked)[:MAX_MANIFESTS]


def inventory(root: Path) -> List[str]:
    """Every skill, agent, MCP server and development dependency the repo has, as `type:name`."""
    found: Set[str] = set()
    for host in (".claude", ".agents", ".grok"):
        for skill in (root / host / "skills").glob("*/SKILL.md"):
            if skill.parent.name not in OWN:
                found.add(f"skill:{skill.parent.name}")
    found.update(f"agent:{p.stem}" for p in (root / ".claude" / "agents").glob("*.md"))
    found.update(f"agent:{p.stem}" for p in (root / ".codex" / "agents").glob("*.toml"))
    found.update(f"mcp-server:{n}" for n in _json_keys(_read(root / ".mcp.json"), "mcpServers"))
    for rel in _manifests(root):
        text, name = _read(root / rel), Path(rel).name
        if name == "package.json":
            deps = _json_keys(text, "devDependencies")
        elif name == "pyproject.toml":
            deps = pyproject_dev_dependencies(text)
        else:
            deps = requirement_names(text)
        found.update(f"dependency:{d}" for d in deps)
    return sorted(e for e in found if backlog.NAME.fullmatch(e.split(":", 1)[1]))


def main(backlog_file: Optional[Path] = None) -> int:
    data: Dict[str, Any] = read_input()
    session_id = str(data.get("session_id") or "")
    root = repo_root(data.get("cwd"))
    if root is None or not session_id or backlog is None:
        return 0
    path = backlog_file or backlog.DEFAULT
    state = load_state(session_id)
    repo = backlog.repo_slug(root)
    if isinstance(state.get("components"), list):  # absent: started before this hook existed
        before = set(state["components"]) | set(state.get("reported", []))
        added = [e for e in inventory(root) if e not in before]
        for entry in added:
            kind, name = entry.split(":", 1)
            backlog.add_component(path, kind, name, repo)
        state["reported"] = sorted(set(state.get("reported", [])) | set(added))
        save_state(session_id, state)
    learning = load_config(root).get("learning")
    target = backlog.report_target(path)
    if isinstance(learning, dict) and learning.get("share") is True and repo and target \
            and repo.lower() != target.lower():  # the kit repo does not report to itself
        backlog.sync(path, target, repo)
    return 0


def backfill(root: Path, path: Path, claim_ideas: bool = False) -> Dict[str, Any]:
    """Once per repo: record what the repo already has, which SessionEnd never reports
    because it was there when the session started. Built by sessions before this hook
    existed, it is still what the repo learned. Ideas recorded before entries carried a
    repo are claimed for this repo only when asked, since nothing says where they came from.
    Then send to the machine's target. Running this by hand, inside the repo, is that
    repo's consent, so `learning.share` is not required here as it is for the hook."""
    repo = backlog.repo_slug(root)
    records = list(backlog.read_records(path).values())
    known = {f"{r.get('type')}:{r.get('name')}" for r in records
             if r.get("kind") == "component" and r.get("repo") == repo}
    added = [e for e in inventory(root) if e not in known]
    for entry in added:
        kind, name = entry.split(":", 1)
        backlog.add_component(path, kind, name, repo)
    claimed = []
    if claim_ideas and repo:
        taken = {r.get("claimed_from") for r in records}
        for record in records:
            if record.get("kind") == "idea" and not record.get("repo") and record["id"] not in taken:
                backlog.append(path, {**record, "id": backlog.uuid.uuid4().hex, "repo": repo,
                                      "claimed_from": record["id"]})
                claimed.append(record["id"])
    target = backlog.report_target(path)
    sent: Dict[str, Any] = {}
    if repo and target and repo.lower() != target.lower():
        sent = backlog.sync(path, target, repo)
    return {"repo": repo, "recorded": added, "claimed_ideas": len(claimed), "sync": sent or
            "not sent: this machine has no target (`backlog.py target OWNER/NAME`)"}


if __name__ == "__main__":
    if sys.argv[1:2] == ["backfill"]:  # run by hand, not as a hook: report what it did
        found = repo_root(str(Path.cwd()))
        if found is None or backlog is None:
            sys.exit("backfill: run it inside a git checkout of the repo")
        print(json.dumps(backfill(found, backlog.DEFAULT, "--claim-ideas" in sys.argv), indent=2))
        sys.exit(0)
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 — a hook must never break the session
        sys.exit(0)
