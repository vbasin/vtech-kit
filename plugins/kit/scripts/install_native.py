"""Install host-native hooks and reviewers alongside the portable workflow.

Usage: python install_native.py --repo PATH --host codex|grok
Refuses to overwrite a file it did not write. A file it wrote earlier, unchanged since, is
upgraded: its hash is in the host's `.kit-native.json` (or, for installs made before that
record existed, in `LEGACY`). Trust hooks in the host after review; an upgrade may need
trusting again.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Dict

import install_portable

ROLES = {
    "kit_design_critic": ("design-critic.md", "Challenge design choices before implementation."),
    "kit_reviewer": ("reviewer.md", "Independently review correctness and regressions."),
    "kit_security_reviewer": ("security-reviewer.md", "Review security risks in relevant changes."),
    "kit_test_runner": ("test-runner.md", "Run appropriate scoped verification and report results."),
}
START = "<!-- vtech-kit workflow: start -->"
END = "<!-- vtech-kit workflow: end -->"
MANIFEST = {"codex": ".codex/.kit-native.json", "grok": ".grok/.kit-native.json"}
# Hashes of every earlier version this installer generated, for installs with no manifest.
LEGACY = {".grok/hooks/kit.json": {"be9f14a65863fc7ce60216f8c40f46c55864966d4baebff53534de557fbf2a68"}}
WORKFLOW = (START + "\n"
            "For substantive requests, use the installed kit-deliver skill when available. "
            "Complete the user's task first, use suitable existing tools, and obtain a missing "
            "task-critical capability when needed. Compare material choices, verify results, "
            "and use independent reviewers when available and authorized. Follow existing "
            "project instructions and report unavailable checks.\n" + END)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def agent_files() -> Dict[str, str]:
    files = {}
    for name, (source, description) in ROLES.items():
        raw = (install_portable.PLUGIN / "agents" / source).read_text(encoding="utf-8")
        parts = raw.split("---\n", 2)
        body = parts[2].strip() if len(parts) == 3 else raw
        instructions = "Do not edit files. Follow the parent session's permissions and instructions.\n\n" + body
        files[f".codex/agents/{name}.toml"] = (
            f"name = {json.dumps(name)}\ndescription = {json.dumps(description)}\n"
            f"developer_instructions = {json.dumps(instructions, ensure_ascii=True)}\n"
        )
    return files


def desired(host: str) -> Dict[str, str]:
    if host == "codex":
        script = 'python3 "$(git rev-parse --show-toplevel)/.agents/skills/kit-deliver/scripts/native_hook.py"'
        groups = {
            "SessionStart": [{"hooks": [{"type": "command", "command": script + " codex start", "timeout": 30}]}],
            "PreToolUse": [{"matcher": "Bash", "hooks": [
                {"type": "command", "command": script + " codex guard", "timeout": 15}]}],
            "Stop": [{"hooks": [{"type": "command", "command": script + " codex stop", "timeout": 480}]}],
        }
        return {".codex/hooks.json": json.dumps({"hooks": groups}, indent=2) + "\n", **agent_files()}
    if host == "grok":
        script = 'python3 "$(git rev-parse --show-toplevel)/.grok/skills/kit-deliver/scripts/native_hook.py"'
        hooks = {"hooks": {
            "SessionStart": [{"hooks": [{"type": "command", "command": script + " grok start", "timeout": 30}]}],
            "PreToolUse": [{"matcher": "Bash", "hooks": [
                {"type": "command", "command": script + " grok guard", "timeout": 15}]}],
            "Stop": [{"hooks": [{"type": "command", "command": script + " grok stop", "timeout": 30}]}],
        }}
        return {".grok/hooks/kit.json": json.dumps(hooks, indent=2) + "\n"}
    raise ValueError("host must be codex or grok")


def install(repo: Path, host: str) -> str:
    root = repo.resolve()
    if not root.is_dir():
        raise ValueError("repo must be an existing directory")
    files = desired(host)
    record = root / MANIFEST[host]
    try:
        written = json.loads(record.read_text(encoding="utf-8")) if record.is_file() else {}
    except ValueError:
        written = {}
    if not isinstance(written, dict):
        written = {}
    for name, content in files.items():
        path = root / name
        path.parent.resolve().relative_to(root)
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError(f"existing native configuration differs; preserve and reconcile manually: {name}")
        if path.exists() and path.read_text(encoding="utf-8") != content:
            found = _sha(path.read_text(encoding="utf-8"))
            if found != written.get(name) and found not in LEGACY.get(name, ()):
                raise ValueError(f"existing native configuration differs; preserve and reconcile manually: {name}")
    instructions = root / "AGENTS.md"
    if instructions.is_symlink() or (instructions.exists() and not instructions.is_file()):
        raise ValueError("AGENTS.md is not a regular file")
    old = instructions.read_text(encoding="utf-8") if instructions.exists() else ""
    if START in old or END in old:
        if old.count(START) != 1 or old.count(END) != 1 or WORKFLOW not in old:
            raise ValueError("existing kit workflow in AGENTS.md differs; reconcile manually")
        new = old
    else:
        new = old + ("\n\n" if old and not old.endswith("\n") else "\n" if old else "") + WORKFLOW + "\n"
    created = install_portable.install(root, host) != "unchanged"
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            with path.open("x", encoding="utf-8") as out:
                out.write(content)
            created = True
        elif path.read_text(encoding="utf-8") != content:  # ours and unchanged since: checked above
            path.write_text(content, encoding="utf-8")
            created = True
    hashes = {name: _sha(content) for name, content in files.items()}
    if written != hashes:
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps(hashes, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if new != old:
        instructions.write_text(new, encoding="utf-8")
        created = True
    return "installed" if created else "unchanged"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--host", choices=("codex", "grok"), required=True)
    args = parser.parse_args()
    try:
        print(f"kit native integration: {install(args.repo, args.host)}")
        return 0
    except (OSError, ValueError) as exc:
        print(f"kit native integration: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
