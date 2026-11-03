"""Record what a repo learned about the kit, and send it back to the kit's GitHub repo.

Two kinds of entry share one append-only log (default `~/.vtech-kit/learning.jsonl`):
an *idea* Claude records when it sees an improvement worth evaluating later, and a
*component* the SessionEnd hook records when a session added a skill, agent, MCP server
or dependency. Each carries the repo it came from. Nothing else is stored: no file
contents, no paths outside the kit's own folders, no secrets.

`sync` posts unsent entries to the kit repo as GitHub issues labelled `kit-learning`, one
issue per distinct gap or component. A repeat, from any repo, becomes a comment on the
existing issue, so the number of repos on an issue is the recurrence signal `improve`
ranks by. Entries that fail to send stay queued for the next sync.

Usage: backlog.py [--file PATH] add --task TEXT --idea TEXT --evidence TEXT [--repo OWNER/NAME]
       backlog.py [--file PATH] add-component --type TYPE --name NAME --repo OWNER/NAME
       backlog.py [--file PATH] list
       backlog.py [--file PATH] resolve --id ID --status STATUS --evidence TEXT
       backlog.py [--file PATH] sync --to OWNER/NAME
"""
from __future__ import annotations

import argparse
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
    return "[kit-learning] idea: " + str(record.get("idea", ""))[:80]


def _occurrence(record: Dict[str, Any]) -> str:
    repo = record.get("repo") or "an unnamed repo"
    if record.get("kind") == "component":
        return (f"- **{repo}** added {record.get('type')} `{record.get('name')}` "
                f"({str(record.get('created_at', ''))[:10]})")
    return (f"- **{repo}** ({str(record.get('created_at', ''))[:10]}), during: {record.get('task')}\n"
            f"  Evidence: {record.get('evidence')}")


Runner = Callable[[List[str]], subprocess.CompletedProcess]


def _gh_runner(gh: str, timeout: float) -> Runner:
    def run(args: List[str]) -> subprocess.CompletedProcess:
        return subprocess.run([gh, *args], capture_output=True, text=True, timeout=timeout)
    return run


def sync(path: Path, target: str, run: Optional[Runner] = None, budget_s: float = 45.0) -> Dict[str, Any]:
    """Send unsent entries to `target` as kit-learning issues. Never raises on a network
    or auth failure: what could not be sent stays queued."""
    if not SLUG.fullmatch(target):
        raise ValueError("--to must be OWNER/NAME")
    pending = [r for r in read_records(path).values() if not r.get("synced_to")]
    result: Dict[str, Any] = {"sent": [], "queued": len(pending), "error": ""}
    if not pending:
        return result
    if run is None:
        gh = shutil.which("gh")
        if gh is None:
            result["error"] = "gh is not installed; entries stay queued"
            return result
        run = _gh_runner(gh, timeout=15)
    deadline = time.monotonic() + budget_s
    try:
        listed = run(["issue", "list", "-R", target, "--label", LABEL, "--state", "all",
                      "--limit", "1000", "--json", "number,body,url"])
        if listed.returncode != 0:
            # The label may not exist yet; create it once and treat the list as empty.
            run(["label", "create", LABEL, "-R", target, "--color", "0E8A16",
                 "--description", "Reported by a repo that uses the kit"])
            issues: List[Dict[str, Any]] = []
        else:
            issues = json.loads(listed.stdout or "[]")
        known = {}
        for issue in issues:
            found = re.search(r"<!-- kit-learning:([0-9a-f]{12}) -->", issue.get("body") or "")
            if found:
                known[found.group(1)] = issue
        for record in pending:
            if time.monotonic() > deadline:
                break
            fp = fingerprint(record)
            if fp in known:
                done = run(["issue", "comment", str(known[fp]["number"]), "-R", target,
                            "--body", "Reported again:\n" + _occurrence(record)])
                url = known[fp].get("url") or f"#{known[fp]['number']}"
            else:
                body = (f"{_marker(fp)}\nReported automatically by a repo that uses the kit. "
                        "Each comment is another report; the number of distinct repos is how "
                        f"often this recurs.\n\n{_occurrence(record)}")
                done = run(["issue", "create", "-R", target, "--title", _title(record),
                            "--body", body, "--label", LABEL])
                url = (done.stdout or "").strip().splitlines()[-1] if done.stdout else ""
                if done.returncode == 0:
                    known[fp] = {"number": url.rsplit("/", 1)[-1], "url": url}
            if done.returncode != 0:
                result["error"] = (done.stderr or "gh failed").strip()[:300]
                break
            append(path, {"kind": "synced", "id": record["id"], "issue": url, "created_at": now()})
            result["sent"].append(url)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"[:300]
    result["queued"] = len(pending) - len(result["sent"])
    return result


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
    send = sub.add_parser("sync")
    send.add_argument("--to", required=True, help="the kit repo, OWNER/NAME")
    args = parser.parse_args()
    try:
        if args.action == "sync":
            result = sync(args.file, args.to)
            print(json.dumps(result))
            return 0 if not result["error"] else 1
        records = read_records(args.file)
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
