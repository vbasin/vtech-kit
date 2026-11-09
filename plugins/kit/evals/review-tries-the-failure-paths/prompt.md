---
description: A committed branch whose only real defects are a failed lookup read as "nothing there" and a parser that handles one input form. Does the review find them?
tags: [review, standards]
max_turns: 16
timeout_seconds: 540
allowed_tools: [Read, Glob, Grep, Skill, Agent, TodoWrite]
expected_outcome: Both defects reported — a failed `gh issue list` makes every report a new, duplicate issue; the dependency reader misses one-line arrays and reads unrelated quoted lines as dependencies.
---

This branch is committed and its tests pass. Review it before I open the PR.

```python
# report/sync.py
import json
import subprocess


def gh(*args):
    return subprocess.run(["gh", *args], capture_output=True, text=True, timeout=15)


def sync(entries, repo):
    """Send each entry as an issue; an entry seen before becomes a comment on its issue."""
    listed = gh("issue", "list", "--repo", repo, "--label", "report",
                "--json", "number,body", "--limit", "1000")
    issues = json.loads(listed.stdout) if listed.returncode == 0 else []
    known = {}
    for issue in issues:
        for line in issue["body"].splitlines():
            if line.startswith("fingerprint: "):
                known[line.split(": ", 1)[1]] = issue["number"]
    for entry in entries:
        if entry["fingerprint"] in known:
            gh("issue", "comment", str(known[entry["fingerprint"]]), "--repo", repo,
               "--body", entry["text"])
        else:
            gh("issue", "create", "--repo", repo, "--label", "report", "--title",
               entry["title"], "--body", f"fingerprint: {entry['fingerprint']}\n\n{entry['text']}")


# report/deps.py
import re

DEP = re.compile(r'^\s*"([A-Za-z0-9_.-]+)[^"]*"\s*,?\s*$')


def added_dependencies(before: str, after: str) -> set:
    """Dependencies in pyproject.toml `after` that `before` did not have."""
    def names(text):
        return {m.group(1).lower() for line in text.splitlines() if (m := DEP.match(line))}
    return names(after) - names(before)
```

The tests cover a new entry, a repeated entry, and adding `"pytest>=8",` on its own line.
