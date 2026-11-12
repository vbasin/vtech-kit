"""Shared helpers for the kit plugin's hook scripts.

Stdlib only, Python 3.8+. Every hook reads one JSON object on stdin and must never
crash the session: on unexpected errors a hook exits 0 and stays silent.

Per-repo behaviour comes from `.claude/kit.json` at the repository root. A repo
without that file gets the guards with their defaults and no lint or test runs.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set

try:  # POSIX only; elsewhere state updates are unserialised, as before
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

CONFIG_RELPATH = Path(".claude") / "kit.json"
STATE_TTL_S = 7 * 24 * 3600

DEFAULT_CONFIG: Dict[str, Any] = {
    "python": ["python3"],
    "lint": {"enabled": False, "args": ["-m", "ruff", "check", "--output-format", "concise"]},
    "tests": {
        "enabled": False,
        "test_dirs": ["tests"],
        "max_files": 8,
        "pytest_args": ["-x", "-q", "--tb=short", "-p", "no:cacheprovider"],
        "env_file": None,
        "timeout_s": 300,
    },
    "guards": {"sql_destructive": "ask", "pytest_full_suite": "off"},
    # Read by session_policy.py; `gates` and `reviewers` are read by the `deliver` skill.
    "routing": {"enabled": True, "note": "", "gates": [], "reviewers": []},
    # Read by review_gate.py: "once" | "deny" | "off".
    "review": {"gate": "once"},
}


# ── input / output ─────────────────────────────────────────────────────────────
def read_input() -> Dict[str, Any]:
    """The hook's stdin payload. Exits quietly instead when Grok is running this Claude plugin
    hook in a repo that has the kit's Grok-native hooks (`stand_aside`)."""
    try:
        data = json.load(sys.stdin)
        data = data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — malformed input must not break the session
        data = {}
    if stand_aside(data):
        sys.exit(0)
    return data


def stand_aside(data: Dict[str, Any]) -> bool:
    """Grok loads Claude plugins, hooks included, so in a repo where install_native.py also
    wrote `.grok/hooks/kit.json` a kit hook can run twice. For each event the native install
    handles, the plugin's copy steps aside; events it does not handle (PostToolUse lint,
    SessionEnd) keep running. Start and Stop step aside only when the installed native hook
    verifies at Stop (`GROK_STOP_VERIFIES`), since an older copy only recorded learning there.
    Grok sets `GROK_HOOK_EVENT` on every hook it runs (Grok 1.0.70 built-in docs); Claude
    Code does not."""
    event = os.environ.get("GROK_HOOK_EVENT", "")
    needs = {"pre_tool_use": "guard", "session_start": "stop", "stop": "stop"}.get(event)
    if not needs:
        return False
    root = repo_root(data.get("cwd") if isinstance(data.get("cwd"), str) else None)
    if root is None:
        return False
    try:
        hooks = json.loads((root / ".grok" / "hooks" / "kit.json").read_text(encoding="utf-8"))
        native = (root / ".grok" / "skills" / "kit-deliver" / "scripts" / "native_hook.py").read_text(encoding="utf-8")
        commands = [h.get("command", "") for groups in hooks["hooks"].values() for group in groups
                    for h in group.get("hooks", [])]
    except (OSError, ValueError, KeyError, AttributeError, TypeError):
        return False
    if not any(isinstance(c, str) and c.endswith(f" grok {needs}") for c in commands):
        return False
    return needs == "guard" or "GROK_STOP_VERIFIES = True" in native


def emit(obj: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()


# ── repo + config ──────────────────────────────────────────────────────────────
def git(args: List[str], cwd: Path, timeout: int = 30,
        env: Optional[Dict[str, str]] = None) -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=timeout, env=env
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def repo_root(cwd: Optional[str]) -> Optional[Path]:
    start = Path(cwd or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    top = git(["rev-parse", "--show-toplevel"], start)
    return Path(top.strip()) if top else None


def session_roots(cwd: Optional[str]) -> List[Path]:
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


def _merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def validate_verification_config(cfg: Dict[str, Any]) -> None:
    """Validate settings consumed by the manual verifier and Stop hook."""
    def strings(value: Any) -> bool:
        return isinstance(value, list) and all(isinstance(v, str) for v in value)

    python = cfg.get("python")
    if not (isinstance(python, str) or strings(python)):
        raise ValueError("Invalid kit configuration: python must be a string or list of strings")
    for section in ("lint", "tests"):
        if not isinstance(cfg[section].get("enabled"), bool):
            raise ValueError(f"Invalid kit configuration: {section}.enabled must be a boolean")
    if not isinstance(cfg.get("verification", {}).get("enabled", False), bool):
        raise ValueError("Invalid kit configuration: verification.enabled must be a boolean")
    no_checks = cfg.get("verification", {}).get("no_checks")
    if no_checks is not None and not strings(no_checks):
        raise ValueError("Invalid kit configuration: verification.no_checks must be a list of strings")
    for section, keys in (("lint", ("args",)),
                          ("tests", ("test_dirs", "import_roots", "exclude", "pytest_args"))):
        for key in keys:
            if key in cfg[section] and not strings(cfg[section][key]):
                raise ValueError(f"Invalid kit configuration: {section}.{key} must be a list of strings")
    for key in ("max_files", "timeout_s"):
        value = cfg["tests"].get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"Invalid kit configuration: tests.{key} must be a positive integer")
    if cfg["tests"].get("scope", "related") not in ("related", "all"):
        raise ValueError('Invalid kit configuration: tests.scope must be "related" or "all"')
    env = cfg["tests"].get("env")
    if env is not None and (not isinstance(env, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in env.items()
    )):
        raise ValueError("Invalid kit configuration: tests.env must be an object of strings")
    env_file = cfg["tests"].get("env_file")
    if env_file is not None and not isinstance(env_file, str):
        raise ValueError("Invalid kit configuration: tests.env_file must be a string or null")


def load_config(root: Optional[Path], strict: bool = False) -> Dict[str, Any]:
    if root is None:
        return DEFAULT_CONFIG
    path = root / CONFIG_RELPATH
    if not path.exists():
        path = root / ".kit" / "kit.json"
    try:
        user = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return DEFAULT_CONFIG
    except (OSError, ValueError) as exc:
        if strict:
            raise ValueError(f"Invalid kit configuration: {exc}") from exc
        return DEFAULT_CONFIG
    if strict and (not isinstance(user, dict) or any(
        k in user and not isinstance(user[k], dict) for k in ("lint", "tests", "guards", "routing", "verification")
    )):
        raise ValueError("Invalid kit configuration: expected objects for lint, tests, "
                         "guards, routing and verification")
    cfg = _merge(DEFAULT_CONFIG, user if isinstance(user, dict) else {})
    if strict:
        validate_verification_config(cfg)
    return cfg


def resolve_python(config: Dict[str, Any], root: Path) -> Optional[str]:
    """First usable interpreter: paths are relative to the repo root, bare names come from PATH.

    The path is made absolute but symlinks are kept, so a venv's python still finds its venv.
    """
    candidates = config.get("python") or ["python3"]
    if isinstance(candidates, str):
        candidates = [candidates]
    for cand in candidates:
        if "/" in cand:
            path = Path(cand) if os.path.isabs(cand) else root / cand
            if path.is_file() and os.access(str(path), os.X_OK):
                return str(path.absolute())
            continue
        found = shutil.which(cand)
        if found:
            return found
    return None


# ── per-session state ──────────────────────────────────────────────────────────
def state_dir() -> Path:
    base = os.environ.get("CLAUDE_PLUGIN_DATA") or os.path.join(
        os.path.expanduser("~"), ".cache", "vtech-kit"
    )
    path = Path(base) / "state"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _state_path(session_id: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in session_id) or "nosession"
    return state_dir() / f"{safe}.json"


def load_state(session_id: str) -> Dict[str, Any]:
    try:
        return json.loads(_state_path(session_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(session_id: str, state: Dict[str, Any]) -> None:
    path = _state_path(session_id)
    # One temp file per writer: two hooks of one session can run at once (parallel tool calls).
    tmp = path.with_name(f"{path.stem}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    os.replace(tmp, path)


def update_state(session_id: str, change: Callable[[Dict[str, Any]], None]) -> None:
    """Read, change and write a session's state under an exclusive lock. Parallel tool calls
    run several hooks of one session at once; without the lock the last writer won and the
    others' records were lost."""
    lock_path = _state_path(session_id).with_suffix(".lock")
    with open(lock_path, "a", encoding="utf-8") as lock:
        locked = False
        if fcntl is not None:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                locked = True
            except OSError:
                pass  # a filesystem without flock: update unserialised, as without fcntl
        try:
            # Opening for append never moves mtime; touch it so it marks the last update and
            # prune_old_state can age locks like the state they guard.
            os.utime(lock_path)
            state = load_state(session_id)
            change(state)
            save_state(session_id, state)
        finally:
            if locked:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def prune_old_state() -> None:
    cutoff = time.time() - STATE_TTL_S
    # .tmp: a writer that crashed between write and replace; .lock: update_state's lock
    # files, whose mtime update_state refreshes on every update.
    for entry in [*state_dir().glob("*.json"), *state_dir().glob("*.tmp"), *state_dir().glob("*.lock")]:
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
        except OSError:
            pass


# ── change tracking ────────────────────────────────────────────────────────────
HASH_LIMIT_BYTES = 20 * 1024 * 1024


def file_digest(path: Path) -> Optional[str]:
    """Content hash, or size+mtime for big files so a dirty dataset can't stall a hook."""
    try:
        st = path.stat()
        if st.st_size > HASH_LIMIT_BYTES:
            return f"stat:{st.st_size}:{st.st_mtime_ns}"
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def dirty_files(root: Path, include_deleted: bool = False) -> List[str]:
    """Modified, added and untracked files relative to the repo root, plus deleted ones on request.

    A staged rename's old path counts as deleted.
    """
    out = git(["status", "--porcelain", "-z", "--untracked-files=all"], root)
    if not out:
        return []
    files: List[str] = []
    entries = out.split("\0")
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        status, name = entry[:2], entry[3:]
        if "R" in status or "C" in status:
            source = entries[i] if i < len(entries) else ""
            i += 1  # the original path of a rename/copy follows as its own entry
            if "R" in status and include_deleted and source:
                files.append(source)
        if "D" in status and not include_deleted:
            continue
        files.append(name)
    return files


def snapshot(root: Path) -> Dict[str, Any]:
    head = (git(["rev-parse", "HEAD"], root) or "").strip()
    dirty = {name: file_digest(root / name) for name in dirty_files(root, include_deleted=True)}
    return {"head": head, "dirty": dirty, "root": str(root)}


def record_edit(state: Dict[str, Any], root: Path, rel: str) -> None:
    """Remember a tool edit, and the content the file had right after it. That content is
    how another session in the same checkout recognises the file as this session's
    (`written_by_others`)."""
    edited = state.setdefault("edited", [])
    if rel not in edited:
        edited.append(rel)
    digest = file_digest(root / rel)
    if digest is not None:
        digests = state.get("edited_digest")
        if not isinstance(digests, dict):
            digests = state["edited_digest"] = {}
        digests[rel] = digest


def written_by_others(root: Path, session_id: str) -> Dict[str, Set[str]]:
    """For each file another session edited in this checkout: the content it had right after
    those edits. A file that still has that content was last written by that session, so
    it is that session's to verify, not this one's.

    Only tool edits are recorded. A change another session made any other way (a shell
    command, a generator) cannot be told from this session's own and stays in scope.

    Known gap: the other session is not checked for being alive. If it ended with its file
    still failing, having said so at its own Stop, or was interrupted or killed before any
    Stop ran, no later Stop of a session that never edited the file picks it up. That matches
    a file already dirty at SessionStart, which is not this session's either. Records expire
    with the state files (`STATE_TTL_S`, counted from that session's last write).
    Edits are matched by path relative to the checkout, so a session that edited a nested
    checkout records paths relative to that one.
    """
    found: Dict[str, Set[str]] = {}
    try:
        own = _state_path(session_id)
        entries = sorted(state_dir().glob("*.json"))
    except OSError:
        return found
    for entry in entries:
        if entry == own:
            continue
        try:
            other = json.loads(entry.read_text(encoding="utf-8"))
            if other["baseline"]["root"] != str(root):
                continue
            digests = other["edited_digest"].items()
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
        for rel, digest in digests:
            if isinstance(rel, str) and isinstance(digest, str) and digest:
                found.setdefault(rel, set()).add(digest)
    return found


def changed_since(root: Path, baseline: Dict[str, Any], edited: Iterable[str],
                  others: Optional[Callable[[], Dict[str, Set[str]]]] = None) -> List[str]:
    """Files changed during the session: tool edits, plus git changes since the baseline.

    Deleted files are included, since their tests still need to run. A file that was already
    dirty at the baseline counts only if its content has changed since, whether it was
    committed, edited further or restored.

    `others` returns `written_by_others`. With it, a file this session never edited whose
    content is what another session's edit left is not counted: several sessions can share
    one checkout, and each verifies its own work. It is called at most once, and only when
    a file needs it.
    """
    changed = set()
    own = set(edited)
    for rel in own:
        if (root / rel).is_file():
            changed.add(rel)
    if baseline:
        before = baseline.get("dirty", {})
        candidates = set(dirty_files(root, include_deleted=True)) | set(before)
        head = baseline.get("head")
        if head:
            committed = git(["diff", "--name-only", "--no-renames", "-z", f"{head}..HEAD"], root) or ""
            candidates.update(n for n in committed.split("\0") if n)
        elif git(["rev-parse", "--verify", "HEAD"], root):
            # A first commit appeared after an unborn-HEAD baseline. Compare its
            # current tree with the baseline's pre-existing dirty files below.
            committed = git(["ls-tree", "-r", "--name-only", "-z", "HEAD"], root)
            if committed is None:
                raise ValueError("Could not enumerate files committed since the session started")
            candidates.update(n for n in committed.split("\0") if n)
        theirs: Optional[Dict[str, Set[str]]] = None
        for name in candidates:
            current = file_digest(root / name)
            if name not in before or before[name] != current:
                if others is not None and current is not None and name not in own:
                    if theirs is None:
                        theirs = others()
                    if current in theirs.get(name, ()):
                        continue
                changed.add(name)
    return sorted(changed)


def relative_to_root(root: Path, file_path: str) -> Optional[str]:
    try:
        return Path(file_path).resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        return None


# ── subprocess ─────────────────────────────────────────────────────────────────
def run(
    cmd: List[str],
    cwd: Path,
    timeout: float,
    env_file: Optional[str] = None,
    extra_env: Optional[Dict[str, str]] = None,
    kill_tree: bool = False,
) -> subprocess.CompletedProcess:
    """Run a command, optionally terminating its process group/tree on timeout or Ctrl-C.

    `env_file` is sourced relative to `cwd`. Tree cleanup covers ordinary child
    workers, not processes that deliberately escape their process group.
    """
    if env_file:
        env_path = Path(env_file) if os.path.isabs(env_file) else Path(cwd) / env_file
        if env_path.is_file():
            cmd = ["bash", "-c", 'set -a; . "$0"; set +a; exec "$@"', str(env_path), *cmd]
    env = dict(os.environ)
    for key, value in (extra_env or {}).items():
        env[str(key)] = str(value).replace("{root}", str(cwd))
    if not kill_tree:
        return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, env=env)
    options = {"start_new_session": True} if os.name == "posix" else {
        "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP,
    }
    with subprocess.Popen(cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, env=env, **options) as proc:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            try:
                if os.name == "posix":
                    with suppress(ProcessLookupError):
                        os.killpg(proc.pid, signal.SIGKILL)
                else:
                    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=5, check=False)
            finally:
                # Always reap the direct child, even if platform tree cleanup fails.
                if proc.poll() is None:
                    proc.kill()
                proc.wait()
            raise
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def run_lint(root: Path, python: str, args: List[str], files: List[str], timeout: int = 120,
             deadline: Optional[float] = None) -> Optional[str]:
    """Lint `files`; return the report on findings, None when clean or the linter is missing.

    `args` like ["-m", "ruff", ...] run under the repo's interpreter; if that interpreter
    lacks the module, fall back to the same tool as a standalone executable on PATH.
    """
    cmd = [python, *args, *files]
    def remaining() -> float:
        available = timeout if deadline is None else min(timeout, deadline - time.monotonic())
        if available <= 0:
            raise subprocess.TimeoutExpired(cmd, timeout)
        return available
    proc = run(cmd, root, timeout=remaining())
    if "No module named" in proc.stderr and len(args) >= 2 and args[0] == "-m":
        exe = shutil.which(args[1])
        if exe is None:
            return f"lint unavailable: {args[1]} is not installed"
        proc = run([exe, *args[2:], *files], root, timeout=remaining())
    if proc.returncode == 0:
        return None
    return tail(proc.stdout + proc.stderr, max_lines=40)


def tail(text: str, max_lines: int = 60, max_chars: int = 6000) -> str:
    lines = text.strip().splitlines()
    if len(lines) > max_lines:
        lines = [f"... ({len(lines) - max_lines} earlier lines trimmed)"] + lines[-max_lines:]
    out = "\n".join(lines)
    return out[-max_chars:]
