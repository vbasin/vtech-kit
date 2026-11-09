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
- A substitution inside single quotes (`echo '$(git push)'`) is read as a push: a false
  reminder, never a missed one.
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
from typing import Any, Dict, Iterator, List, NamedTuple, Optional, Tuple

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
        # 40s rather than the hook's 60s, so one slow hash usually still reaches `finally`.
        if git(["add", "-A"], root, timeout=40, env=env) is None:
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


def remote_slugs(root: Path) -> set:
    """`owner/repo` of `origin` and `upstream`: a PR from a fork names the upstream repo."""
    slugs = set()
    for remote in ("origin", "upstream"):
        url = (git(["remote", "get-url", remote], root) or "").strip()
        m = re.search(r"github\.com[:/]+([^/]+/[^/]+?)(?:\.git)?/?$", url)
        if m:
            slugs.add(m.group(1).lower())
    return slugs


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
                    _break_stale(lock)  # left by a hook that was killed
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


def _break_stale(lock: Path) -> None:
    """Remove a stale lock. The rename is atomic, so two waiters cannot both remove the same
    file, and a live lock taken by mistake is put back. A third waiter can still slip in while
    it is out, which only loses a stamp, as `locked` accepts."""
    victim = lock.with_name(f"{lock.name}.{os.getpid()}")
    os.rename(str(lock), str(victim))
    try:
        if time.time() - victim.stat().st_mtime <= LOCK_STALE_S:
            with suppress(OSError):
                os.link(str(victim), str(lock))  # fails if a third waiter holds it now
    finally:
        with suppress(OSError):
            victim.unlink()


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
PUSH_DELETES = {"--delete", "-d"}  # publishes no branch's tree
# Shell keywords that can stand before the program in a simple command.
LEADERS = {"!", "then", "do", "else", "elif", "if", "while", "until"}
# Wrappers whose options take a separate value: `sudo -n` takes none, `sudo -u` and `nice -n` do.
WRAPPER_WITH_VALUE = {
    "nice": {"-n", "--adjustment"},
    "timeout": {"-s", "--signal", "-k", "--kill-after"},
    "env": {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"},
    "sudo": {"-u", "--user", "-g", "--group", "-U", "--other-user", "-C", "-h", "-p", "-D"},
    "xargs": {"-I", "-n", "-P", "-L", "-d", "-E", "-s", "-a"},
}
SHELLS = {"bash", "sh", "zsh", "dash"}
SHELL_C = re.compile(r"-[a-zA-Z]*c[a-zA-Z]*")  # -c, and combined forms such as -lc and -ec
SUBSTITUTION = re.compile(r"\$\(|`")
RAW_PR = re.compile(r"\bgh\b[^\n;&|]*\bpr\s+create\b")
RAW_PUSH = re.compile(r"\bgit\b[^\n;&|]*\bpush\b")


class Action(NamedTuple):
    action: str  # "pr" or "push"
    directory: Optional[str]  # from a leading `cd` or `git -C`; None for the session's cwd
    ref: Optional[str]  # the local branch whose tree is published; None for HEAD
    name: Optional[str]  # the branch name on GitHub; None for the current branch
    repo: Optional[str] = None  # `gh -R owner/repo`


def strip_comments(command: str) -> str:
    """Drop `# ...` comments but keep the newline that ends them. shlex's own comment handling
    eats that newline, which joins the next command onto the commented one."""
    out, quote, i = [], "", 0
    while i < len(command):
        ch = command[i]
        if quote:
            if ch == "\\" and quote == '"' and i + 1 < len(command):
                out.append(command[i:i + 2])
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch == "\\" and i + 1 < len(command):
            out.append(command[i:i + 2])
            i += 2
            continue
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or command[i - 1] in " \t\n;&|()"):
            end = command.find("\n", i)
            i = len(command) if end < 0 else end
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def bash_actions(command: str, directory: Optional[str] = None, depth: int = 0) -> List[Action]:
    """Each `gh pr create` or `git push` the command runs, including inside `bash -c`, `eval`
    and `$(...)`. Pushes that publish no branch (`--delete`, `--tags`, `:branch`) are left out."""
    from bash_guard import WRAPPERS, _segments

    command = strip_comments(command)
    segments = _segments(command)
    if not segments:  # unbalanced quotes and the like: fall back to the raw text
        return ([Action("pr", directory, None, None)] if RAW_PR.search(command) else []) + (
            [Action("push", directory, None, None)] if RAW_PUSH.search(command) else [])
    found: List[Action] = []
    for seg in segments:
        if depth < 3:  # a substitution quoted into one token: "$(git push)"
            for tok in seg:
                m = SUBSTITUTION.search(tok)
                if m and " " in tok[m.end():]:
                    found.extend(bash_actions(tok[m.end():].rstrip(")`"), directory, depth + 1))
        seg = [t.rstrip(")`") for t in seg]
        i, wrapper = 0, ""
        while i < len(seg):  # past wrappers and what they take: `timeout -s KILL 60`, `FOO=bar`
            tok = seg[i].lstrip("$({`")
            value = ASSIGNMENT.sub("", tok, count=1) if ASSIGNMENT.match(tok) else None
            if value is not None and SUBSTITUTION.match(value):
                seg[i] = value  # x=$(git push): the substitution is the command
            elif not tok or tok in LEADERS or value is not None or DURATION.match(tok):
                i += 1
            elif tok in WRAPPERS or tok in WRAPPER_WITH_VALUE:
                wrapper, i = tok, i + 1
            elif tok.startswith("-"):
                i += 2 if tok in WRAPPER_WITH_VALUE.get(wrapper, ()) else 1
            else:
                break
        if i >= len(seg):
            continue
        prog, rest = seg[i].lstrip("$({`").rsplit("/", 1)[-1], seg[i + 1:]
        flag = next((j for j, t in enumerate(rest) if SHELL_C.fullmatch(t)), None)
        if prog == "cd" and rest:
            directory = _join(directory, rest[0])
        elif prog in SHELLS and flag is not None and flag + 1 < len(rest) and depth < 3:
            found.extend(bash_actions(rest[flag + 1], directory, depth + 1))
        elif prog == "eval" and rest and depth < 3:
            found.extend(bash_actions(" ".join(rest), directory, depth + 1))
        elif prog == "gh":
            if _positional(rest, GH_GLOBAL_WITH_VALUE)[:2] == ["pr", "create"]:
                head = _option(rest, ("-H", "--head"))
                branch = head.split(":")[-1] if head else None
                repo = _option(rest, ("-R", "--repo"))
                found.append(Action("pr", directory, branch, branch, repo))
        elif prog == "git":
            where = _option(rest, ("-C",))
            target = _join(directory, where) if where else directory
            words = _positional(rest, GIT_GLOBAL_WITH_VALUE)
            if words[:1] == ["push"]:
                after = rest[rest.index("push") + 1:]
                refspecs = _positional(after, PUSH_WITH_VALUE)[1:]  # after the remote
                cut = next((j for j, r in enumerate(refspecs) if "$" in r or "`" in r), None)
                refspecs = refspecs[:cut]  # a substitution spans tokens: `$(git branch ...)`
                if PUSH_DELETES & set(after) or ("--tags" in after and not refspecs):
                    continue
                pushed = [_pushed(r) for r in refspecs] if refspecs else [(None, None)]
                found.extend(Action("push", target, ref, name) for ref, name in pushed if ref != "")
    return found


def _join(base: Optional[str], path: str) -> str:
    path = os.path.expanduser(path)
    return path if os.path.isabs(path) or base is None else os.path.join(base, path)


def _pushed(refspec: str) -> Tuple[Optional[str], Optional[str]]:
    """(local branch, remote branch) of a refspec. None is HEAD or the current branch; a ref of
    "" is an empty source, which deletes the remote branch and publishes nothing."""
    def short(ref: str) -> str:
        return ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref

    src, sep, dst = refspec.lstrip("+").partition(":")
    if sep and not src:
        return "", short(dst)
    ref = None if src == "HEAD" else short(src)
    return ref, (short(dst) if dst and dst != "HEAD" else ref)


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


def _slug(repo: str) -> str:
    """`owner/repo` of a `gh -R` value, which may also be `HOST/OWNER/REPO` or a URL."""
    parts = [p for p in re.sub(r"\.git$", "", repo.strip()).replace(":", "/").split("/") if p]
    return "/".join(parts[-2:]).lower()


def _pick(candidates: List[Path], slug: str) -> Optional[Path]:
    """The checkout the PR is opened from. A checkout whose remotes name another repo is not
    it; one with no GitHub remote is accepted only when it is the only checkout."""
    slugs = {r: remote_slugs(r) for r in candidates}
    matching = [r for r, s in slugs.items() if slug in s]
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


def decide(root: Path, action: str, ref: Optional[str], name: Optional[str] = None) -> Optional[str]:
    """None to let the action through, else the reason it is denied. Records state.
    `ref` is the local branch whose tree is published, `name` the branch it lands on."""
    gate = mode(root)
    if gate == "off":
        return None
    name = name or ref or current_branch(root)
    gated = _gated_ref(root, ref)
    tree = ref_tree(root, gated) if gated else None
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
    targets: List[Tuple[Path, str, Optional[str], Optional[str]]] = []
    if tool.startswith("mcp__") and tool.endswith("create_pull_request"):
        slug = f"{tool_input.get('owner', '')}/{tool_input.get('repo', '')}".lower()
        root = _pick(session_roots(cwd), slug)
        head = tool_input.get("head") if isinstance(tool_input.get("head"), str) else None
        branch = head.split(":")[-1] if head else None
        if root is not None:
            targets.append((root, "pr", branch, branch))
    elif tool == "Bash":
        command = tool_input.get("command") if isinstance(tool_input.get("command"), str) else ""
        if not command or not QUICK.search(command):
            return
        for act in bash_actions(command):
            base = _join(cwd or os.getcwd(), act.directory) if act.directory else cwd
            root = repo_root(base)
            if root is None:
                continue
            slugs = remote_slugs(root)
            local_head = act.ref and ref_tree(root, f"refs/heads/{act.ref}")
            if act.repo and slugs and _slug(act.repo) not in slugs and not local_head:
                continue  # `gh -R` names a repo this checkout is not, and no local branch heads it
            targets.append((root, act.action, act.ref, act.name))
    for root, action, ref, name in targets:
        reason = decide(root, action, ref, name)
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
