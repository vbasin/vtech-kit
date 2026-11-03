"""Record what a repo learned about the kit, and send it back to the kit's GitHub repo.

Two kinds of entry share one append-only log (default `~/.vtech-kit/learning.jsonl`):
an *idea* Claude records when it sees an improvement worth evaluating later, and a
*component* the SessionEnd hook records when a session added a skill, agent, MCP server
or dependency. Each carries the repo it came from. Nothing else is stored: no file
contents, no paths outside the kit's own folders, no secrets.

`sync` posts one repo's unsent entries to the kit repo as GitHub issues labelled
`kit-learning`, one issue per distinct gap or component. Only entries recorded in the repo
being synced are sent: the log is machine-wide, and a repo that never opted in must not be
published by one that did. A repeat, from any repo, becomes a comment on the
existing issue, so the number of repos on an issue is the recurrence signal `improve`
ranks by. Entries that fail to send stay queued for the next sync.

Usage: backlog.py [--file PATH] add --task TEXT --idea TEXT --evidence TEXT [--repo OWNER/NAME]
       backlog.py [--file PATH] add-component --type TYPE --name NAME --repo OWNER/NAME
       backlog.py [--file PATH] list
       backlog.py [--file PATH] resolve --id ID --status STATUS --evidence TEXT
       backlog.py [--file PATH] target [OWNER/NAME]
       backlog.py [--file PATH] status
       backlog.py [--file PATH] sync [--to OWNER/NAME] [--repo OWNER/NAME]
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

DEFAULT = Path(os.environ.get("KIT_LEARNING_FILE") or Path.home() / ".vtech-kit" / "learning.jsonl")
LABEL = "kit-learning"
SLUG = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
COMPONENT_TYPES = ("skill", "agent", "mcp-server", "dependency")
NAME = re.compile(r"^[A-Za-z0-9@][A-Za-z0-9_.@/-]{0,99}$")


def clean(text: str) -> str:
    text = " ".join(text.split())
    if not text or len(text) > 1000:
        raise ValueError("fields must contain 1 to 1000 characters")
    return text


def repo_slug(cwd: Optional[Path] = None) -> str:
    """`owner/name` from the origin remote, or "" outside a GitHub checkout."""
    try:
        url = subprocess.run(["git", "remote", "get-url", "origin"], cwd=str(cwd or Path.cwd()),
                             capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    match = re.search(r"github\.com[:/]+([^/]+/[^/]+?)(?:\.git)?/?$", url)
    return match.group(1) if match else ""


def read_records(path: Path) -> Dict[str, Dict[str, Any]]:
    """Replay the log. A line that does not parse, such as one cut short by a crash, is
    skipped with a warning: one bad line must not lock every later add and list."""
    records: Dict[str, Dict[str, Any]] = {}
    if not path.exists():
        return records
    skipped = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            event = None
        ident = event.get("id") if isinstance(event, dict) else None
        kind = event.get("kind") if isinstance(event, dict) else None
        if not isinstance(ident, str) or not ident or kind not in ("idea", "component", "resolution", "synced"):
            skipped += 1
        elif kind in ("idea", "component") and ident not in records:
            records[ident] = event
        elif kind == "resolution" and ident in records:
            records[ident] = {**records[ident], "status": event.get("status"),
                              "resolution_evidence": event.get("evidence")}
        elif kind == "synced" and ident in records:
            records[ident] = {**records[ident], "synced_to": event.get("issue")}
        else:
            skipped += 1
    if skipped:
        print(f"backlog: skipped {skipped} unreadable line(s) in {path}", file=sys.stderr)
    return records


def append(path: Path, event: Dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = (json.dumps(event, ensure_ascii=True, separators=(",", ":")) + "\n").encode("utf-8")
    if path.exists() and path.stat().st_size:
        with path.open("rb") as handle:
            handle.seek(-1, os.SEEK_END)
            if handle.read(1) != b"\n":  # a line cut short must not swallow this one
                payload = b"\n" + payload
    if len(payload) > 16384:
        raise ValueError("event is too large")
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        written = os.write(fd, payload)
        if written != len(payload):
            raise OSError("incomplete backlog write")
    finally:
        os.close(fd)


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def add_component(path: Path, component_type: str, name: str, repo: str) -> Dict[str, Any]:
    if component_type not in COMPONENT_TYPES:
        raise ValueError(f"component type must be one of {', '.join(COMPONENT_TYPES)}")
    if not NAME.fullmatch(name):
        raise ValueError("component name must be a plain identifier")
    if repo and not SLUG.fullmatch(repo):
        raise ValueError("repo must be OWNER/NAME")
    event = {"kind": "component", "id": uuid.uuid4().hex, "type": component_type, "name": name,
             "repo": repo, "status": "proposed", "created_at": now()}
    append(path, event)
    return event


def fingerprint(record: Dict[str, Any]) -> str:
    """The same gap or component from any repo maps to one issue."""
    if record.get("kind") == "component":
        key = f"component:{record.get('type')}:{str(record.get('name')).lower()}"
    else:
        words = re.findall(r"[a-z0-9]+", str(record.get("idea", "")).lower())
        key = "idea:" + " ".join(words[:12])
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def _marker(fp: str) -> str:
    return f"<!-- kit-learning:{fp} -->"


def _title(record: Dict[str, Any]) -> str:
    if record.get("kind") == "component":
        return f"[kit-learning] {record.get('type')}: {record.get('name')}"
    return "[kit-learning] idea: " + scrub(record.get("idea", ""))[:80]


SECRETS = re.compile(
    r"(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w{20,}|sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16}"
    r"|xox[abpr]-[A-Za-z0-9-]{10,}|[A-Za-z][A-Za-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@\S+"
    r"|[A-Fa-f0-9]{32,}|[A-Za-z0-9+/_-]{40,}={0,2}|(?:/home|/Users|/root)/\S+|[A-Za-z]:\\Users\\\S+)")


def scrub(text: Any) -> str:
    """Free text leaves the machine: drop what looks like a credential or a local path."""
    return SECRETS.sub("[redacted]", str(text))


def _occurrence(record: Dict[str, Any]) -> str:
    repo = record.get("repo") or "an unnamed repo"
    if record.get("kind") == "component":
        return (f"- **{repo}** added {record.get('type')} `{record.get('name')}` "
                f"({str(record.get('created_at', ''))[:10]})")
    return (f"- **{repo}** ({str(record.get('created_at', ''))[:10]}): {scrub(record.get('idea'))}\n"
            f"  During: {scrub(record.get('task'))}\n  Evidence: {scrub(record.get('evidence'))}")


# The destination is the machine owner's choice, not the repo's: a repo's committed config
# can only opt in to sending (`learning.share`), so a cloned repo cannot redirect this
# machine's `gh` login to post somewhere else.
def config_path(path: Path) -> Path:
    return path.parent / "config.json"


def report_target(path: Path) -> str:
    try:
        value = json.loads(config_path(path).read_text(encoding="utf-8")).get("report_to")
    except (OSError, ValueError, AttributeError):
        return ""
    return value if isinstance(value, str) and SLUG.fullmatch(value) else ""


def set_report_target(path: Path, target: str) -> None:
    if target and not SLUG.fullmatch(target):
        raise ValueError("target must be OWNER/NAME")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    config_path(path).write_text(json.dumps({"report_to": target}) + "\n", encoding="utf-8")


def status_path(path: Path) -> Path:
    return path.parent / "sync-status.json"


Runner = Callable[[List[str], float], subprocess.CompletedProcess]


def _gh_runner(gh: str) -> Runner:
    env = {**os.environ, "GH_PROMPT_DISABLED": "1"}

    def run(args: List[str], timeout: float) -> subprocess.CompletedProcess:
        return subprocess.run([gh, *args], capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL, env=env)
    return run


def _lock(path: Path) -> Optional[Path]:
    """One sync per machine at a time, so two sessions ending together cannot both create
    an issue for the same gap. A lock older than two minutes belongs to a killed sync."""
    lock = path.parent / "sync.lock"
    try:
        if time.time() - lock.stat().st_mtime > 120:
            lock.unlink()
    except OSError:
        pass
    try:
        os.close(os.open(str(lock), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        return lock
    except OSError:
        return None


def sync(path: Path, target: str, repo: str, run: Optional[Runner] = None,
         budget_s: float = 40.0) -> Dict[str, Any]:
    """Send `repo`'s unsent entries to `target` as kit-learning issues, within `budget_s`.
    Never raises on a network or auth failure: what could not be sent stays queued, and
    the outcome is kept in `sync-status.json` next to the log (`backlog.py status`)."""
    if not SLUG.fullmatch(target) or not SLUG.fullmatch(repo):
        raise ValueError("--to and --repo must be OWNER/NAME")
    def unsent() -> List[Dict[str, Any]]:
        return [r for r in read_records(path).values()
                if r.get("repo") == repo and not r.get("synced_to")]

    pending = unsent()
    result: Dict[str, Any] = {"sent": [], "queued": len(pending), "error": ""}
    if not pending:
        return result
    if run is None:
        gh = shutil.which("gh")
        run = _gh_runner(gh) if gh else None
    lock = _lock(path) if run else None
    if run is None:
        result["error"] = "gh is not installed; entries stay queued"
    elif lock is None:
        result["error"] = "another sync is running; entries stay queued"
    else:
        try:
            pending = unsent()  # re-read under the lock: a sync that just finished sent some
            if pending:
                _send(path, target, pending, run, time.monotonic() + budget_s, result)
        finally:
            lock.unlink()
    result["queued"] = len(pending) - len(result["sent"])
    with contextlib.suppress(OSError):
        status_path(path).write_text(json.dumps({"at": now(), "repo": repo, "target": target, **result}),
                                     encoding="utf-8")
    return result


def _send(path: Path, target: str, pending: List[Dict[str, Any]], run: Runner, deadline: float,
          result: Dict[str, Any]) -> None:
    def call(args: List[str]) -> Optional[subprocess.CompletedProcess]:
        left = deadline - time.monotonic()
        # A call cut off mid-create would post an issue the log never hears about.
        return run(args, min(15.0, left)) if left > 5 else None

    try:
        listed = call(["issue", "list", "-R", target, "--label", LABEL, "--state", "all",
                       "--limit", "1000", "--json", "number,body,url,state"])
        if listed is None or listed.returncode != 0:
            # Not knowing which issues exist is not knowing that none do: creating now
            # could split one gap across two issues. Leave everything queued.
            result["error"] = ((listed.stderr if listed else "") or "gh issue list failed").strip()[:300]
            return
        known = {}
        for issue in json.loads(listed.stdout or "[]"):
            found = re.search(r"<!-- kit-learning:([0-9a-f]{12}) -->", issue.get("body") or "")
            if found:
                known[found.group(1)] = issue
        labelled = False
        for record in pending:
            fp = fingerprint(record)
            issue = known.get(fp)
            if issue:
                if str(issue.get("state", "")).upper() == "CLOSED":
                    # A new report is new evidence: put it back in front of `improve`.
                    reopened = call(["issue", "reopen", str(issue["number"]), "-R", target])
                    if reopened is None or reopened.returncode != 0:
                        # A comment on an issue that stays closed is never seen: keep it queued.
                        reason = (reopened.stderr if reopened else "") or "gh issue reopen failed"
                        result["error"] = reason.strip()[:300]
                        return
                    issue["state"] = "OPEN"
                done = call(["issue", "comment", str(issue["number"]), "-R", target,
                             "--body", "Reported again:\n" + _occurrence(record)])
                url = issue.get("url") or f"#{issue['number']}"
            else:
                if not labelled:  # listing a missing label succeeds empty; creating with it fails
                    call(["label", "create", LABEL, "-R", target, "--force", "--color", "0E8A16",
                          "--description", "Reported by a repo that uses the kit"])
                    labelled = True
                body = (f"{_marker(fp)}\nReported automatically by a repo that uses the kit. "
                        "Each comment is another report; the number of distinct repos named "
                        f"here is how often this recurs.\n\n{_occurrence(record)}")
                done = call(["issue", "create", "-R", target, "--title", _title(record),
                             "--body", body, "--label", LABEL])
                lines = (done.stdout or "").strip().splitlines() if done else []
                url = next((ln for ln in reversed(lines) if "/issues/" in ln), "")
                if url:
                    known[fp] = {"number": url.rsplit("/", 1)[-1], "url": url, "state": "OPEN"}
            if done is None:
                result["error"] = "out of time; entries stay queued"
                return
            if done.returncode != 0 or not url:
                result["error"] = (done.stderr or "gh gave no issue URL").strip()[:300]
                return
            append(path, {"kind": "synced", "id": record["id"], "issue": url, "created_at": now()})
            result["sent"].append(url)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"[:300]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", type=Path, default=DEFAULT)
    sub = parser.add_subparsers(dest="action", required=True)
    add = sub.add_parser("add")
    add.add_argument("--task", required=True)
    add.add_argument("--idea", required=True)
    add.add_argument("--evidence", required=True)
    add.add_argument("--repo", default=None, help="OWNER/NAME; defaults to this checkout's origin")
    comp = sub.add_parser("add-component")
    comp.add_argument("--type", required=True, choices=COMPONENT_TYPES)
    comp.add_argument("--name", required=True)
    comp.add_argument("--repo", default=None)
    sub.add_parser("list")
    resolve = sub.add_parser("resolve")
    resolve.add_argument("--id", required=True)
    resolve.add_argument("--status", choices=("evaluated", "rejected", "promoted"), required=True)
    resolve.add_argument("--evidence", required=True)
    target = sub.add_parser("target", help="show, or set, where this machine sends learning")
    target.add_argument("repo", nargs="?", default=None, help="the kit repo, OWNER/NAME; '' to stop sending")
    sub.add_parser("status", help="what is queued and how the last sync went")
    send = sub.add_parser("sync")
    send.add_argument("--to", default=None, help="the kit repo, OWNER/NAME; defaults to the target")
    send.add_argument("--repo", default=None, help="whose entries to send; defaults to this checkout's origin")
    args = parser.parse_args()
    try:
        if args.action == "target":
            if args.repo is not None:
                set_report_target(args.file, args.repo)
            print(json.dumps({"report_to": report_target(args.file)}))
            return 0
        if args.action == "sync":
            to = args.to or report_target(args.file)
            if not to:
                raise ValueError("no target: run `backlog.py target OWNER/NAME` or pass --to")
            result = sync(args.file, to, repo_slug() if args.repo is None else args.repo)
            print(json.dumps(result))
            return 0 if not result["error"] else 1
        records = read_records(args.file)
        if args.action == "status":
            try:
                last = json.loads(status_path(args.file).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                last = None
            unsent = [r for r in records.values() if not r.get("synced_to")]
            print(json.dumps({"report_to": report_target(args.file), "queued": len(unsent),
                              "queued_by_repo": {r: sum(1 for u in unsent if u.get("repo") == r)
                                                 for r in sorted({str(u.get("repo")) for u in unsent})},
                              "last_sync": last}, indent=2))
            return 0
        if args.action == "list":
            print(json.dumps(list(records.values()), indent=2))
            return 0
        if args.action == "add-component":
            print(json.dumps(add_component(args.file, args.type, args.name,
                                           repo_slug() if args.repo is None else args.repo)))
            return 0
        if args.action == "add":
            repo = repo_slug() if args.repo is None else args.repo
            if repo and not SLUG.fullmatch(repo):
                raise ValueError("repo must be OWNER/NAME")
            event = {"kind": "idea", "id": uuid.uuid4().hex, "task": clean(args.task),
                     "idea": clean(args.idea), "evidence": clean(args.evidence), "repo": repo,
                     "status": "proposed", "created_at": now()}
        else:
            if args.id not in records:
                raise ValueError("unknown backlog id")
            event = {"kind": "resolution", "id": args.id, "status": args.status,
                     "evidence": clean(args.evidence), "created_at": now()}
        append(args.file, event)
        print(json.dumps(event))
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(f"backlog: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
