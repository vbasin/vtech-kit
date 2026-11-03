"""Install shared task and reusable skills into a coding-agent workspace.

Usage: python install_portable.py --repo PATH --host codex|grok|claude
The installer updates only unchanged skill directories it previously installed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Dict, List, Tuple

from learning import validate_skill_name

PLUGIN = Path(__file__).resolve().parents[1]
SOURCE = PLUGIN / "portable" / "kit-deliver"
SKILLS = PLUGIN / "skills"
MARKER = ".kit-portable"
MANIFEST = ".kit-installed.json"
RUNTIME_SCRIPTS = ("kit_common.py", "gates.py", "stop_verify.py", "learning.py", "bash_guard.py",
                   "learning_report.py")
REVIEW_REFERENCES = {
    "design-critic.md": PLUGIN / "agents/design-critic.md",
    "reviewer.md": PLUGIN / "agents/reviewer.md",
    "security-reviewer.md": PLUGIN / "agents/security-reviewer.md",
    "test-runner.md": PLUGIN / "agents/test-runner.md",
    "causality-reviewer.md": PLUGIN.parent / "quant/agents/causality-reviewer.md",
    "query-reviewer.md": PLUGIN.parent / "quant/agents/query-reviewer.md",
}
HOST_PATHS = {"codex": ".agents/skills", "grok": ".grok/skills", "claude": ".claude/skills"}
NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
CACHE = re.compile(r"^(.+)\.cpython-\d+(?:\.opt-[12])?\.pyc$")


def generated_bytecode(path: Path) -> bool:
    """Recognize only bytecode for a neighboring Python source file."""
    match = CACHE.fullmatch(path.name)
    if not match or path.parent.name != "__pycache__":
        return False
    source = path.parent.parent / (match.group(1) + ".py")
    return source.is_file() and not source.is_symlink()


def file_hashes(folder: Path) -> Dict[str, str]:
    hashes = {}
    cache_dirs = set()
    for path in sorted(folder.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"symlinks are not supported: {path}")
        if path.is_dir() and "__pycache__" in path.relative_to(folder).parts[:-1]:
            raise ValueError(f"unmanaged directory in bytecode cache: {path}")
        if path.parent.name == "__pycache__" and path.is_file() and not generated_bytecode(path):
            raise ValueError(f"unmanaged file in bytecode cache: {path}")
        if path.is_file() and generated_bytecode(path):
            cache_dirs.add(str(path.parent.relative_to(folder)))
        elif path.is_file() and path != folder / MANIFEST:
            hashes[path.relative_to(folder).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif not path.is_dir() and not path.is_file():
            raise ValueError(f"unsupported file type in skill: {path}")
    expected_dirs = {str(parent) for name in hashes for parent in Path(name).parents}
    for path in folder.rglob("*"):
        if path.is_dir() and str(path.relative_to(folder)) not in expected_dirs | cache_dirs:
            raise ValueError(f"unmanaged directory in skill: {path}")
    return hashes


def sources() -> List[Tuple[str, Path, bool]]:
    found = [("kit-deliver", SOURCE, True)]
    for skill in sorted(SKILLS.iterdir()):
        if not (skill / MARKER).exists():
            continue
        if skill.is_symlink() or not skill.is_dir() or not NAME.fullmatch(skill.name):
            raise ValueError(f"invalid portable skill directory: {skill}")
        validate_skill_name(skill, skill.name)
        if skill.name == "kit-deliver":
            raise ValueError("portable skill conflicts with kit-deliver")
        found.append((skill.name, skill, False))
    return found


def install(repo: Path, host: str) -> str:
    if host not in HOST_PATHS:
        raise ValueError(f"unsupported host: {host}")
    root = repo.resolve()
    if not root.is_dir():
        raise ValueError("repo must be an existing directory")
    parent = root / HOST_PATHS[host]
    parent.resolve().relative_to(root)
    plans = []
    # Validate all destinations before mutating any of them.
    for name, source, include_runtime in sources():
        target = parent / name
        if target.is_symlink():
            raise ValueError("skill destination must not be a symlink")
        hashes = file_hashes(source)
        if not (source / "SKILL.md").is_file() or not hashes:
            raise ValueError(f"portable skill source is missing: {name}")
        if include_runtime:
            for script in RUNTIME_SCRIPTS:
                path = Path(__file__).resolve().parent / script
                hashes[f"scripts/{script}"] = hashlib.sha256(path.read_bytes()).hexdigest()
            for brief, path in REVIEW_REFERENCES.items():
                hashes[f"references/reviewers/{brief}"] = hashlib.sha256(path.read_bytes()).hexdigest()
        if target.exists():
            if not target.is_dir():
                raise ValueError("skill destination is not a directory")
            try:
                previous = json.loads((target / MANIFEST).read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ValueError("existing skill is not managed by this installer") from exc
            if not isinstance(previous, dict) or previous.get("files") != file_hashes(target):
                raise ValueError("existing installed skill was modified; preserve it and reconcile manually")
            if previous["files"] == hashes:
                continue
        plans.append((source, target, hashes, include_runtime))
    if not plans:
        return "unchanged"
    parent.mkdir(parents=True, exist_ok=True)
    for source, target, hashes, include_runtime in plans:
        with tempfile.TemporaryDirectory(prefix=".kit-install-", dir=str(parent)) as tmp:
            staged = Path(tmp) / target.name
            # Generated caches are not part of the installed artifact. The validation
            # above rejects any unexpected files in these directories first.
            shutil.copytree(source, staged, ignore=shutil.ignore_patterns("__pycache__"))
            if include_runtime:
                for script in RUNTIME_SCRIPTS:
                    shutil.copy2(Path(__file__).resolve().parent / script, staged / "scripts" / script)
                reviews = staged / "references" / "reviewers"
                reviews.mkdir(parents=True, exist_ok=True)
                for brief, path in REVIEW_REFERENCES.items():
                    shutil.copy2(path, reviews / brief)
            (staged / MANIFEST).write_text(json.dumps({"files": hashes}, indent=2) + "\n", encoding="utf-8")
            backup = parent / f".kit-backup-{uuid.uuid4().hex}"
            moved_old = False
            try:
                if target.exists():
                    os.replace(target, backup)
                    moved_old = True
                os.replace(staged, target)
            except OSError:
                if moved_old and not target.exists():
                    os.replace(backup, target)
                raise
            if moved_old:
                shutil.rmtree(backup)
    return "installed"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--host", required=True, choices=sorted(HOST_PATHS))
    args = parser.parse_args()
    try:
        print(f"kit portable skills: {install(args.repo, args.host)}")
        return 0
    except (OSError, ValueError) as exc:
        print(f"kit portable skills: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
