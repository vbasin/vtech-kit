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

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from kit_common import emit, git, load_config, load_state, read_input, repo_root, save_state

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / "portable" / "kit-deliver" / "scripts"))
try:
    import backlog  # noqa: E402
except Exception:  # noqa: BLE001 — a partial install must not break the session
    backlog = None  # type: ignore[assignment]

# Installed by the kit itself (install_portable.py), so not something the repo learned.
OWN = {"kit-deliver"} | {p.parent.name for p in (PLUGIN / "skills").glob("*/.kit-portable")}
INSTALLED = ".kit-installed.json"  # install_portable.MANIFEST
NATIVE_AGENT_PREFIX = "kit_"  # install_native.ROLES: the Codex reviewers it writes
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
            # A directory the kit's installer wrote carries its manifest: not learned here.
            if skill.parent.name not in OWN and not (skill.parent / INSTALLED).exists():
                found.add(f"skill:{skill.parent.name}")
    found.update(f"agent:{p.stem}" for p in (root / ".claude" / "agents").glob("*.md"))
    found.update(f"agent:{p.stem}" for p in (root / ".codex" / "agents").glob("*.toml")
                 if not p.stem.startswith(NATIVE_AGENT_PREFIX))
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


def recorded(path: Path, repo: str) -> Set[str]:
    """Components already in the log for this repo, from any session on any host."""
    return {f"{r.get('type')}:{r.get('name')}" for r in backlog.read_records(path).values()
            if r.get("kind") == "component" and r.get("repo") == repo}


# YAML frontmatter (`description: ...`) or a Codex agent's TOML (`description = "..."`).
FRONTMATTER_DESCRIPTION = re.compile(r"^description\s*[:=]\s*(.*)$", re.MULTILINE)
DECISIONS = "docs/decisions"
# A field and its indented continuation lines: the logs wrap long fields.
DECISION_FIELDS = re.compile(r"^-\s*(Task|Binding constraint):\s*(.+(?:\n[ \t]+\S.*)*)", re.MULTILINE)
# Raw text kept for the backlog, which scrubs it whole and only then cuts it to its own
# limit: cutting first could split a credential into a piece the scrubber no longer sees.
RAW_LIMIT = 4000


def component_paths(root: Path, entry: str) -> List[Path]:
    """Where a skill or agent of the inventory lives in the repo (none for the others)."""
    kind, name = entry.split(":", 1)
    if kind == "skill":
        return [root / host / "skills" / name for host in (".claude", ".agents", ".grok")
                if (root / host / "skills" / name / "SKILL.md").exists()]
    if kind == "agent":
        return [p for p in (root / ".claude" / "agents" / f"{name}.md",
                            root / ".codex" / "agents" / f"{name}.toml") if p.exists()]
    return []


def describe(root: Path, entry: str) -> str:
    """The component's own one-line description from its frontmatter: what it is for."""
    for path in component_paths(root, entry):
        found = FRONTMATTER_DESCRIPTION.search(_read(path / "SKILL.md" if path.is_dir() else path))
        if found:
            text = found.group(1).strip().strip("'\"|>").strip()
            if text:
                return text[:RAW_LIMIT]
    return ""


def decision_digests(root: Path) -> Dict[str, str]:
    """Digest of every decision log, so the end of a session sees which ones it wrote."""
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()[:16]
            for p in sorted((root / DECISIONS).glob("*.md"))[:500] if p.is_file()}


def decision_context(root: Path, before: Optional[Dict[str, str]]) -> str:
    """Task and binding constraint of the decision logs this session wrote or changed:
    why the component was sought and what settled the design. None known at start: none."""
    if before is None:
        return ""
    parts = []
    for rel, digest in decision_digests(root).items():
        if before.get(rel) == digest:
            continue
        text = _read(root / rel)
        fields = "; ".join(f"{k}: {' '.join(v.split())}" for k, v in DECISION_FIELDS.findall(text))
        if fields:
            parts.append(f"{Path(rel).stem}: {fields}")
    return " | ".join(parts)[:RAW_LIMIT]


def start_inventory(root: Path) -> Dict[str, Any]:
    """What a session's end is compared with."""
    return {"components": inventory(root), "decisions": decision_digests(root)}


def capture(root: Path, state: Dict[str, Any], path: Path, budget_s: float = 40.0,
            send: bool = True) -> List[Dict[str, Any]]:
    """Record what this session added since its start inventory, with what context there is,
    then send the repo's queue if both opt-ins hold and `send`. Shared by every host's
    end-of-session (or end-of-turn) hook: Claude's SessionEnd and Stop here, Codex's and
    Grok's Stop in `native_hook.py`. Safe to repeat: what this session or any other already
    recorded for the repo is skipped. Updates `state`; the caller saves it. Returns the
    entries recorded now."""
    repo = backlog.repo_slug(root)
    made: List[Dict[str, Any]] = []
    if isinstance(state.get("components"), list):  # absent: started before this hook existed
        before = set(state["components"]) | set(state.get("reported", []))
        added = [e for e in inventory(root) if e not in before]
        if added:
            known = recorded(path, repo)
            decision = decision_context(root, state.get("decisions"))
            for entry in added:
                if entry not in known:
                    kind, name = entry.split(":", 1)
                    made.append(backlog.add_component(
                        path, kind, name, repo, {"what": describe(root, entry), "decision": decision}))
        state["reported"] = sorted(set(state.get("reported", [])) | set(added))
    if send:
        send_repo(root, path, budget_s)
    return made


def send_repo(root: Path, path: Path, budget_s: float = 40.0) -> Optional[Dict[str, Any]]:
    """Send this repo's queue when the machine names a target and the repo consents."""
    repo = backlog.repo_slug(root)
    learning = load_config(root).get("learning")
    target = backlog.report_target(path)
    if isinstance(learning, dict) and learning.get("share") is True and repo and target \
            and repo.lower() != target.lower():  # the kit repo does not report to itself
        return backlog.sync(path, target, repo, budget_s=budget_s)
    return None


def learning_roots(cwd: Optional[str]) -> List[Path]:
    """The repo the session runs in; or, when it runs above its checkouts (a claude.ai cloud
    session starts in the home folder with each repo cloned beneath it), those checkouts."""
    root = repo_root(cwd)
    if root is not None:
        return [root]
    base = Path(cwd or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    try:
        children = sorted(p for p in base.iterdir() if p.is_dir() and (p / ".git").exists())
    except OSError:
        return []
    return [p for p in (repo_root(str(c)) for c in children[:50]) if p is not None][:10]


def user_inventory() -> List[str]:
    """Skills and agents saved for the user rather than in a repo: they reach no repo, no
    other machine and no learning report, and a cloud container discards them."""
    home = Path.home() / ".claude"
    found = {f"skill:{p.parent.name}" for p in (home / "skills").glob("*/SKILL.md")}
    found.update(f"agent:{p.stem}" for p in (home / "agents").glob("*.md"))
    return sorted(found)


def learning_id(session_id: str) -> str:
    # Its own state file: the Stop verifier saves the session's file in parallel with this hook.
    return f"{session_id}-learning"


def begin(session_id: str, cwd: Optional[str]) -> None:
    """SessionStart: the start inventory of every root, and of the user's own folders."""
    sid = learning_id(session_id)
    state = load_state(sid)
    if state.get("roots") is not None:  # resume / compact: keep the original inventory
        return
    state["roots"] = {str(r): start_inventory(r) for r in learning_roots(cwd)}
    state["user"] = user_inventory()
    save_state(sid, state)


def _roots_state(session_id: str, cwd: Optional[str]) -> Dict[str, Any]:
    state = load_state(learning_id(session_id))
    roots = state.setdefault("roots", {})
    for root in learning_roots(cwd):
        if str(root) not in roots:
            # Started before this hook existed, or cloned mid-session: its inventory from now.
            old = load_state(session_id)
            roots[str(root)] = ({"components": old["components"], "reported": old.get("reported", [])}
                                if isinstance(old.get("components"), list) and repo_root(cwd) == root
                                else start_inventory(root))
    return state


def main(backlog_file: Optional[Path] = None) -> int:
    """SessionEnd: record and send. Nothing to ask: the session is over."""
    data: Dict[str, Any] = read_input()
    session_id = str(data.get("session_id") or "")
    if not session_id or backlog is None:
        return 0
    path = backlog_file or backlog.DEFAULT
    state = _roots_state(session_id, data.get("cwd"))
    for root in learning_roots(data.get("cwd")):
        capture(root, state["roots"][str(root)], path)
    save_state(learning_id(session_id), state)
    return 0


NO_ACCESS = re.compile(r"not enabled for this session|Resource not accessible|HTTP 40[34]", re.IGNORECASE)


def stop_main(backlog_file: Optional[Path] = None) -> int:
    """Stop: record what this turn added, then, once per thing, ask Claude for what only it
    knows or can do: why the component was needed, and saving it where it lasts. Sends on
    the next Stop, so the reason goes out with the report. A cloud container may be
    discarded without a SessionEnd, so this is where a cloud session reports."""
    data: Dict[str, Any] = read_input()
    session_id = str(data.get("session_id") or "")
    if not session_id or backlog is None:
        return 0
    path = backlog_file or backlog.DEFAULT
    scripts = PLUGIN / "portable" / "kit-deliver" / "scripts" / "backlog.py"
    state = _roots_state(session_id, data.get("cwd"))
    roots = learning_roots(data.get("cwd"))
    asks: List[str] = []
    # Asked last Stop and still without a reason: send them without one from now on.
    for ident in state.pop("awaiting", []):
        backlog.release(path, ident)
    awaiting: List[str] = []
    for root in roots:
        sub = state["roots"][str(root)]
        for event in capture(root, sub, path, send=False):
            entry = f"{event['type']}:{event['name']}"
            if event["type"] in ("skill", "agent"):
                backlog.hold(path, event["id"])  # no sync, from any session, sends it yet
                awaiting.append(event["id"])
                asks.append(f"- {event['type']} `{event['name']}` in {event['repo'] or root.name}: "
                            f"say why it was needed, in one or two sentences (what you were trying "
                            f"to do or optimize, and what was missing): `python3 {scripts} --file "
                            f"{path} why --id {event['id']} --text \"...\"`")
            dirty = [p for p in component_paths(root, entry)
                     if git(["status", "--porcelain", "--", str(p)], root)]
            if dirty:
                rels = " ".join(str(p.relative_to(root)) for p in dirty)
                asks.append(f"- `{rels}` is not committed in {root.name}: commit it (explicit paths), "
                            "so it outlasts this session and reaches other machines.")
    seen = set(state.setdefault("user_flagged", []))
    target = backlog.report_target(path).lower()
    own = [r for r in roots if backlog.repo_slug(r).lower() != target] if target else roots
    for entry in user_inventory():
        if entry not in set(state.get("user", [])) | seen:
            seen.add(entry)
            kind, name = entry.split(":", 1)
            where = (f"Save it in the repo ({', '.join(r.name for r in own)}) under `.claude/{kind}s/` "
                     "and commit it." if own else
                     "This session has no repo of its own: if it is reusable beyond this project, add "
                     "it to the kit repo as a pull request; otherwise say where you kept it.")
            asks.append(f"- {kind} `{name}` was saved under ~/.claude, where no repo, other machine or "
                        f"learning report sees it. {where}")
    state["user_flagged"] = sorted(seen)
    # Each thing is asked about once (`reported`, `user_flagged`), so a continuation may ask
    # about what it has just added, such as a skill moved from ~/.claude into the repo.
    if asks:
        state["awaiting"] = awaiting
        save_state(learning_id(session_id), state)
        emit({"decision": "block", "reason": "kit learning (one-time, for what this session added):\n"
              + "\n".join(asks) + "\nThen finish."})
        return 0
    failed = ""
    for root in roots:
        result = send_repo(root, path, budget_s=40.0)
        if result and NO_ACCESS.search(result.get("error") or ""):
            failed = backlog.report_target(path)
    save_state(learning_id(session_id), state)
    if failed and os.environ.get("CLAUDE_CODE_REMOTE") == "true" and not state.get("access_asked"):
        state["access_asked"] = True
        save_state(learning_id(session_id), state)
        emit({"decision": "block", "reason": (
            f"kit learning could not reach {failed}: this cloud session has no GitHub access to it. "
            f"Request it (the add_repo tool, repository {failed}, push access), then run "
            f"`python3 {scripts} --file {path} sync --repo <OWNER/NAME of the repo with queued "
            "entries>`. If access is refused, say so in one line and finish.")})
    return 0


def backfill(root: Path, path: Path, claim_ideas: bool = False) -> Dict[str, Any]:
    """Once per repo: record what the repo already has, which SessionEnd never reports
    because it was there when the session started. Built by sessions before this hook
    existed, it is still what the repo learned. Ideas recorded before entries carried a
    repo are claimed for this repo only when asked, since nothing says where they came from.
    Then send to the machine's target. Running this by hand, inside the repo, is that
    repo's consent, so `learning.share` is not required here as it is for the hook."""
    repo = backlog.repo_slug(root)
    if not backlog.SLUG.fullmatch(repo or ""):
        # Entries without a repo can never be sent; write nothing rather than strand them.
        return {"repo": "", "error": "no GitHub origin: `git remote get-url origin` must be github.com/OWNER/NAME"}
    records = list(backlog.read_records(path).values())
    known = recorded(path, repo)
    added = [e for e in inventory(root) if e not in known]
    for entry in added:
        kind, name = entry.split(":", 1)
        backlog.add_component(path, kind, name, repo)
    claimed = []
    if claim_ideas:
        for record in records:
            if record.get("kind") == "idea" and not record.get("repo"):
                backlog.append(path, {"kind": "claim", "id": record["id"], "repo": repo,
                                      "created_at": backlog.now()})
                claimed.append(record["id"])
    target = backlog.report_target(path)
    sent: Dict[str, Any] = {}
    if target and repo.lower() != target.lower():
        sent = backlog.sync(path, target, repo)
    return {"repo": repo, "recorded": added, "claimed_ideas": len(claimed), "sync": sent or
            "not sent: this machine has no target (`backlog.py target OWNER/NAME`)"}


if __name__ == "__main__":
    if sys.argv[1:2] == ["backfill"]:  # run by hand, not as a hook: report what it did
        found = repo_root(str(Path.cwd()))
        if found is None or backlog is None:
            sys.exit("backfill: run it inside a git checkout of the repo")
        outcome = backfill(found, backlog.DEFAULT, "--claim-ideas" in sys.argv)
        print(json.dumps(outcome, indent=2))
        sys.exit(1 if outcome.get("error") else 0)
    try:
        sys.exit(stop_main() if sys.argv[1:2] == ["stop"] else main())
    except Exception:  # noqa: BLE001 — a hook must never break the session
        sys.exit(0)
