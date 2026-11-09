"""Review before the PR: stamp what `kit:reviewer` reviewed, and gate PR creation on it.

Three events, one script:

  SubagentStart  (agent_type kit:reviewer)  remember the working tree it starts on
  SubagentStop   (same agent)               stamp that tree as reviewed, if it is unchanged
  PreToolUse     (a `*create_pull_request` MCP tool, or Bash running `gh pr create`,
                  or `git push` to a branch this repo has opened a PR from)
                 deny when HEAD's tree carries no stamp

Why a hook and not a line in a skill: the review step used to live only in `deliver`, and
the sessions that most needed it never loaded that skill. A tree hash names exactly what
was reviewed, so a fix commit made after the review is unreviewed until it is reviewed.

`review.gate` in `.claude/kit.json`:
  "once" (default)  deny once per tree, then let a retry through, so a reviewer that
                    cannot run (usage limit, no Agent tool) never wedges the work
  "deny"            deny until the tree is reviewed
  "off"             no gate, no stamps

State lives in the repo's git common dir (`kit-review.json`), so every session and
worktree of the repo shares it, and nothing is ever written into the working tree.
Claude Code only: Codex and Grok have no subagent events, so they get no stamp and no gate.

Known limits, each a nudge-sized gap rather than a forged review:
- A reviewer asked about one named file still stamps the whole tree it started on.
- A PR opened through `gh api` or the web UI is not seen, so its branch's pushes are not gated.
- A branch stays gated for pushes after its PR closes (the last 50 branch names are kept).
- Hashing the tree with `git add -A` writes blobs for untracked files into `.git/objects`,
  which git prunes as unreachable objects.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from kit_common import emit, git, load_config, read_input, repo_root, session_roots

REVIEWERS = {"kit:reviewer", "reviewer"}
MODES = {"once", "deny", "off"}
KEEP = 50  # stamps and denials remembered per repo
QUICK = re.compile(r"\bpush\b|\bpr\b")
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
VERDICT = re.compile(r"(?:\A|\n)Review complete:[^\n]*\s*\Z")  # reviewer.md's last line
DURATION = re.compile(r"^\d+(\.\d+)?[smhd]?$")
LOCK_WAIT_S, LOCK_STALE_S = 3.0, 30.0


# ── where and what ─────────────────────────────────────────────────────────────
def mode(root: Path) -> str:
    review = load_config(root).get("review")
    value = review.get("gate") if isinstance(review, dict) else None
    return value if value in MODES else "once"


def worktree_tree(root: Path) -> Optional[str]:
    """The tree object the working tree would commit as, untracked files included and ignored
    ones not, computed in a throwaway index so the real one is never touched. Seeded from the
    real index so unchanged files are not re-hashed."""
    index = git(["rev-parse", "--git-path", "index"], root)
    gitdir = git(["rev-parse", "--git-dir"], root)
    if gitdir is None:
        return None
    gitdir_path = (root / gitdir.strip()).resolve()
    fd, tmp = tempfile.mkstemp(prefix="kit-review-index-", dir=str(gitdir_path))
    os.close(fd)
    try:
        src = (root / index.strip()) if index else None
        if src is not None and src.is_file():
            Path(tmp).write_bytes(src.read_bytes())
        else:
            os.unlink(tmp)  # git refuses an empty index file; a missing one is a fresh index
        env = dict(os.environ, GIT_INDEX_FILE=tmp)
        if git(["add", "-A"], root, timeout=60, env=env) is None:
            return None
        out = git(["write-tree"], root, env=env)
        return (out.strip() or None) if out is not None else None
    finally:
        with suppress(OSError):
            os.unlink(tmp)


def ref_tree(root: Path, ref: str) -> Optional[str]:
    out = git(["rev-parse", "--verify", "--quiet", f"{ref}^{{tree}}"], root)
    return (out.strip() or None) if out else None


def current_branch(root: Path) -> str:
    out = git(["symbolic-ref", "--quiet", "--short", "HEAD"], root)
    return out.strip() if out else ""


def origin_slug(root: Path) -> str:
    url = (git(["remote", "get-url", "origin"], root) or "").strip()
    m = re.search(r"github\.com[:/]+([^/]+/[^/]+?)(?:\.git)?/?$", url)
    return m.group(1).lower() if m else ""


# ── state ──────────────────────────────────────────────────────────────────────
def _state_file(root: Path) -> Optional[Path]:
    common = git(["rev-parse", "--git-common-dir"], root)
    return (root / common.strip()).resolve() / "kit-review.json" if common else None


def load(root: Path) -> Dict[str, Any]:
    path = _state_file(root)
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path else {}
    except (OSError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def save(root: Path, state: Dict[str, Any]) -> None:
    path = _state_file(root)
    if path is None:
        return
    for key in ("reviewed", "denied", "pr_branches"):
        if isinstance(state.get(key), list):
            state[key] = state[key][-KEEP:]
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        with suppress(OSError):
            tmp.unlink()


@contextmanager
def locked(root: Path) -> Iterator[None]:
    """Serialise load-modify-save across sessions, worktrees and parallel reviewers, which all
    share one state file. A lock that cannot be had in time is skipped: a lost update only
    loses a stamp or a reminder, it never forges a review."""
    path = _state_file(root)
    lock = path.with_name(path.name + ".lock") if path else None
    held = False
    deadline = time.monotonic() + LOCK_WAIT_S
    while lock is not None and not held and time.monotonic() < deadline:
        try:
            os.close(os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            held = True
        except FileExistsError:
            with suppress(OSError):
                if time.time() - lock.stat().st_mtime > LOCK_STALE_S:
                    lock.unlink()  # left by a hook that was killed
                    continue
            time.sleep(0.05)
        except OSError:
            break
    try:
        yield
    finally:
        if held and lock is not None:
            with suppress(OSError):
                lock.unlink()


def _add(state: Dict[str, Any], key: str, value: str) -> None:
    items = [v for v in state.get(key, []) if isinstance(v, str) and v != value]
    state[key] = [*items, value]


# ── subagent events ────────────────────────────────────────────────────────────
def on_start(data: Dict[str, Any]) -> None:
    agent = str(data.get("agent_id") or "")
    for root in session_roots(data.get("cwd")):
        if mode(root) == "off":
            continue
        tree = worktree_tree(root)
        if not tree:
            continue
        with locked(root):
            state = load(root)
            pending = state.get("pending") if isinstance(state.get("pending"), dict) else {}
            # HEAD's tree too: the gate compares commits, and an untracked scratch file in the
            # working tree must not make a reviewed commit look unreviewed.
            pending[agent] = {"tree": tree, "head": ref_tree(root, "HEAD")}
            state["pending"] = dict(list(pending.items())[-KEEP:])
            save(root, state)


def on_stop(data: Dict[str, Any]) -> None:
    agent = str(data.get("agent_id") or "")
    finished = bool(VERDICT.search(str(data.get("last_assistant_message") or "")))
    for root in session_roots(data.get("cwd")):
        now = worktree_tree(root) if finished else None
        with locked(root):
            state = load(root)
            pending = state.get("pending") if isinstance(state.get("pending"), dict) else {}
            start = pending.pop(agent, None)
            if not isinstance(start, dict):
                continue
            # Stamp only what the reviewer saw: an edit made while it read is not reviewed.
            if finished and now and now == start.get("tree"):
                for tree in (start.get("tree"), start.get("head")):
                    if isinstance(tree, str) and tree:
                        _add(state, "reviewed", tree)
            state["pending"] = pending
            save(root, state)


# ── the gate ───────────────────────────────────────────────────────────────────
GH_GLOBAL_WITH_VALUE = {"-R", "--repo", "--hostname"}
GIT_GLOBAL_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}
PUSH_WITH_VALUE = {"--repo", "-o", "--push-option", "--receive-pack", "--exec"}
# What can stand before the program in a simple command: shell keywords, `sudo`, `xargs`.
LEADERS = {"!", "then", "do", "else", "elif", "if", "while", "until", "sudo", "xargs"}
WRAPPER_WITH_VALUE = {"-u", "--unset", "-s", "--signal", "-k", "--kill-after", "-n", "-g", "-U"}
SHELLS = {"bash", "sh", "zsh", "dash"}
RAW_PR = re.compile(r"\bgh\b[^\n;&|]*\bpr\s+create\b")
RAW_PUSH = re.compile(r"\bgit\b[^\n;&|]*\bpush\b")

Action = Tuple[str, Optional[str], Optional[str]]


def bash_actions(command: str, directory: Optional[str] = None, depth: int = 0) -> List[Action]:
    """(action, directory, branch) for each `gh pr create` or `git push` the command runs.
    `directory` follows a leading `cd` and `git -C`; `branch` is the PR's head or the pushed
    branch, None for the current one."""
    from bash_guard import WRAPPERS, _segments

    segments = _segments(command)
    if not segments:  # unbalanced quotes and the like: fall back to the raw text
        return ([("pr", directory, None)] if RAW_PR.search(command) else []) + (
            [("push", directory, None)] if RAW_PUSH.search(command) else [])
    found: List[Action] = []
    for seg in segments:
        seg = [t.lstrip("$({`").rstrip(")}`") for t in seg]
        seg = [t for t in seg if t]
        i = 0
        while i < len(seg):  # past wrappers and what they take: `timeout -s KILL 60`, `FOO=bar`
            tok = seg[i]
            if tok in WRAPPERS or tok in LEADERS or ASSIGNMENT.match(tok) or DURATION.match(tok):
                i += 1
            elif tok.startswith("-"):
                i += 2 if tok in WRAPPER_WITH_VALUE else 1
            else:
                break
        if i >= len(seg):
            continue
        prog, rest = seg[i].rsplit("/", 1)[-1], seg[i + 1:]
        if prog == "cd" and rest:
            directory = _join(directory, rest[0])
        elif prog in SHELLS and "-c" in rest and depth < 3:
            script = rest[rest.index("-c") + 1:rest.index("-c") + 2]
            found.extend(bash_actions(script[0], directory, depth + 1) if script else [])
        elif prog == "gh":
            if _positional(rest, GH_GLOBAL_WITH_VALUE)[:2] == ["pr", "create"]:
                head = _option(rest, ("-H", "--head"))
                found.append(("pr", directory, head.split(":")[-1] if head else None))
        elif prog == "git":
            where = _option(rest, ("-C",))
            target = _join(directory, where) if where else directory
            words = _positional(rest, GIT_GLOBAL_WITH_VALUE)
            if words[:1] == ["push"]:
                after = rest[rest.index("push") + 1:]
                refspecs = _positional(after, PUSH_WITH_VALUE)[1:]  # after the remote
                branches = [_pushed_branch(r) for r in refspecs] or [None]
                found.extend(("push", target, b) for b in branches)
    return found


def _join(base: Optional[str], path: str) -> str:
    path = os.path.expanduser(path)
    return path if os.path.isabs(path) or base is None else os.path.join(base, path)


def _pushed_branch(refspec: str) -> Optional[str]:
    src = refspec.lstrip("+").split(":", 1)[0]
    if src.startswith("refs/heads/"):
        src = src[len("refs/heads/"):]
    return None if src in ("", "HEAD") else src


def _positional(args: List[str], with_value: set) -> List[str]:
    out, skip = [], False
    for arg in args:
        if skip:
            skip = False
        elif arg in with_value:
            skip = True
        elif not arg.startswith("-"):
            out.append(arg)
    return out


def _option(args: List[str], names: Tuple[str, ...]) -> Optional[str]:
    for i, arg in enumerate(args):
        for name in names:
            if arg == name and i + 1 < len(args):
                return args[i + 1]
            if name.startswith("--") and arg.startswith(name + "="):
                return arg.split("=", 1)[1]
            if not name.startswith("--") and arg.startswith(name) and len(arg) > len(name):
                return arg[len(name):]  # attached short form: -Hfeature
    return None


def _pick(candidates: List[Path], slug: str) -> Optional[Path]:
    """The checkout the PR is opened from. A checkout whose origin names another repo is not
    it; one with no GitHub origin is accepted only when it is the only checkout."""
    slugs = {r: origin_slug(r) for r in candidates}
    matching = [r for r, s in slugs.items() if s and s == slug]
    if len(matching) == 1:
        return matching[0]
    if len(candidates) == 1 and not slugs[candidates[0]]:
        return candidates[0]
    return None


def _gated_ref(root: Path, branch: Optional[str]) -> Optional[str]:
    """The ref whose tree the PR or push carries, or None when it cannot be known here."""
    if not branch or branch == current_branch(root):
        return "HEAD"
    for ref in (f"refs/heads/{branch}", f"refs/remotes/origin/{branch}"):
        if ref_tree(root, ref):
            return ref
    return None


def decide(root: Path, action: str, branch: Optional[str]) -> Optional[str]:
    """None to let the action through, else the reason it is denied. Records state."""
    gate = mode(root)
    if gate == "off":
        return None
    name = branch or current_branch(root)
    ref = _gated_ref(root, branch)
    tree = ref_tree(root, ref) if ref else None
    if tree is None:
        return None
    with locked(root):
        state = load(root)
        if action == "push" and name not in state.get("pr_branches", []):
            return None  # pushes before a PR exists are work in progress, not a review event
        if tree in state.get("reviewed", []) or (gate == "once" and tree in state.get("denied", [])):
            if action == "pr" and name:
                _add(state, "pr_branches", name)
                save(root, state)
            return None
        _add(state, "denied", tree)
        save(root, state)
    what = "open this PR" if action == "pr" else f"push {name}, which has a PR"
    retry = (" This is the one reminder for this tree: retrying unchanged goes through."
             if gate == "once" else "")
    return (
        f"kit review gate: you are about to {what}, but its tree ({tree[:10]}) has not been "
        "reviewed. Run the `kit:reviewer` agent on the whole branch (merge-base with the "
        "default branch to HEAD, plus the working tree), fix what it finds, commit, and "
        "retry. Copilot reviews every push, so anything the review would catch costs a "
        f"round there instead.{retry} To change this, set `review.gate` (once|deny|off) in "
        ".claude/kit.json."
    )


def on_pre_tool(data: Dict[str, Any]) -> None:
    tool = str(data.get("tool_name") or "")
    tool_input = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    cwd = data.get("cwd") if isinstance(data.get("cwd"), str) else None
    targets: List[Tuple[Path, str, Optional[str]]] = []
    if tool.startswith("mcp__") and tool.endswith("create_pull_request"):
        slug = f"{tool_input.get('owner', '')}/{tool_input.get('repo', '')}".lower()
        root = _pick(session_roots(cwd), slug)
        head = tool_input.get("head") if isinstance(tool_input.get("head"), str) else None
        if root is not None:
            targets.append((root, "pr", head.split(":")[-1] if head else None))
    elif tool == "Bash":
        command = tool_input.get("command") if isinstance(tool_input.get("command"), str) else ""
        if not command or not QUICK.search(command):
            return
        for action, directory, head in bash_actions(command):
            base = _join(cwd or os.getcwd(), directory) if directory else cwd
            root = repo_root(base)
            if root is not None:
                targets.append((root, action, head))
    for root, action, head in targets:
        reason = decide(root, action, head)
        if reason:
            emit({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                         "permissionDecision": "deny",
                                         "permissionDecisionReason": reason}})
            return


def main() -> int:
    data = read_input()
    event = data.get("hook_event_name")
    if event == "SubagentStart" and data.get("agent_type") in REVIEWERS:
        on_start(data)
    elif event == "SubagentStop" and data.get("agent_type") in REVIEWERS:
        on_stop(data)
    elif event == "PreToolUse":
        on_pre_tool(data)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 — a hook must never break the session
        sys.exit(0)
