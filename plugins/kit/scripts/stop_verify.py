"""Stop: lint and test what this session changed before Claude may finish.

Runs only when the repo opts in through `.claude/kit.json` (`lint.enabled`, `tests.enabled`).
Scope is the session's own changes: files edited through Claude's tools plus anything
that changed on disk or in commits since SessionStart, deletions included. A file this
session never edited, whose content is what another session's recorded edit in the same
checkout left, is that session's to verify. Tests are
picked per changed file: its own test file, `test_<module>*.py`, every test file whose
imports reach it through the repo's own modules (so a test of B still runs when A, which B
imports, changed), and for a `conftest.py` the test files beneath it. That set is capped at
`tests.max_files`, minus any `tests.exclude` globs, and always passed to pytest as
explicit paths. `tests.scope: "all"` runs every discovered test file instead, for a repo
whose whole suite is cheap. Each pytest run gets an empty bytecode cache, so a stale `.pyc`
can't stand in for the edited source.

With `verification.enabled`, the repo's gates check what they claim. A Python file no gate
claims gets related tests the same way (when `tests.enabled`), so it is only a gap when no
test reaches it. Files matched by `verification.no_checks` (documentation, data, images)
have nothing executable to run and are not reported.

Failures (lint findings, failing tests or gates, a missing interpreter) block the stop.
Gaps (files no gate covers, tests capped or excluded, nothing selected) are reported to
the user once per state of the diff and do not block: Claude cannot fix a gap by trying
again, and the hook fires at the end of every turn. A diff that already passed, or whose
gaps were already reported, is not re-run.

Loop safety: a failure blocks the stop once. If Claude tries to stop again with the
same failure, the stop is allowed and the user sees a notice instead, so a
pre-existing failure can't trap the session.

Manual use (the `kit:verify` skill): `python3 stop_verify.py --manual [FILE ...]`
checks the given files, or every dirty file when none are given, and prints a report.
It exits 0 when verified, 1 when something failed or is incomplete, 2 when unavailable.
"""

from __future__ import annotations

import ast
import fnmatch
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from gates import UNCOVERED, run_gates, uncovered_by_gates
from kit_common import (
    CONFIG_RELPATH,
    changed_since,
    dirty_files,
    emit,
    file_digest,
    load_config,
    load_state,
    read_input,
    relative_to_root,
    repo_root,
    resolve_python,
    run,
    run_lint,
    save_state,
    tail,
    written_by_others,
)

PYTEST_NO_TESTS = 5
INCOMPLETE = "Verification incomplete: "


def _is_test_file(rel: str, test_dirs: List[str]) -> bool:
    name = Path(rel).name
    in_dir = any(rel == d or rel.startswith(d.rstrip("/") + "/") for d in test_dirs)
    return in_dir and name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


DOTTED = re.compile(r"[A-Za-z_]\w*(?:\.\w+)+")
IMPORT_FALLBACK = re.compile(r"^\s*(?:from\s+([\w.]+)\s+import\s+\(?([\w\s,]+)|import\s+([\w.]+))", re.M)


def imported_names(source: str, package: str = "") -> Set[str]:
    """Dotted names a file refers to: imported modules, `module.name` for each from-import,
    and dotted strings under an imported package, such as `mock.patch("pkg.mod.func")`.
    Relative imports resolve against `package`, the dotted package the file sits in."""
    names: Set[str] = set()
    strings: Set[str] = set()
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        for mod, members, plain in IMPORT_FALLBACK.findall(source):
            if plain:
                names.add(plain)
            else:
                names.add(mod)
                names.update(f"{mod}.{m.strip()}" for m in members.split(",") if m.strip())
        return names
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and (node.module or node.level):
            module = node.module or ""
            if node.level:
                parts = package.split(".") if package else []
                if node.level - 1 > len(parts):
                    continue
                base = parts[:len(parts) - (node.level - 1)]
                module = ".".join(base + ([module] if module else []))
                if not module:
                    continue
            names.add(module)
            names.update(f"{module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and DOTTED.fullmatch(node.value):
            strings.add(node.value)
    packages = {n.split(".", 1)[0] for n in names}
    names.update(s for s in strings if s.split(".", 1)[0] in packages)
    return names


SKIP_DIRS = {"node_modules", "__pycache__", "site-packages", "venv", "build", "dist"}


def _module_names(rel: str, strip_roots: Set[str]) -> List[str]:
    """The dotted names a file is importable as: its path, and its path below an import root."""
    parts = list(Path(rel).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return []
    names = [".".join(parts)]
    if parts[0] in strip_roots and len(parts) > 1:
        names.append(".".join(parts[1:]))
    return names


def _python_files(root: Path) -> List[str]:
    found: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS)
        base = Path(dirpath).relative_to(root)
        found.extend((base / f).as_posix() for f in sorted(filenames) if f.endswith(".py"))
    return found


def _importers(root: Path, changed: List[str], strip_roots: Set[str]) -> Dict[str, Set[str]]:
    """For each repo file, the repo files that import it.

    The graph is the repo's own modules only. Importing `pkg.mod` counts as importing
    `pkg` too, as it does at run time. Imports Python resolves at run time (importlib,
    registries) are not seen.
    """
    files = _python_files(root)
    index: Dict[str, Set[str]] = {}
    for rel in files + changed:  # changed files may be deleted; their importers still matter
        for name in _module_names(rel, strip_roots):
            index.setdefault(name, set()).add(rel)
    reverse: Dict[str, Set[str]] = {}
    for rel in files:
        names = _module_names(rel, strip_roots)
        package = names[-1] if names else ""
        if names and Path(rel).name != "__init__.py":
            package = package.rpartition(".")[0]
        try:
            source = (root / rel).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for ref in imported_names(source, package):
            parts = ref.split(".")
            for n in range(len(parts), 0, -1):
                for target in index.get(".".join(parts[:n]), ()):
                    if target != rel:
                        reverse.setdefault(target, set()).add(rel)
    return reverse


def _dependents(reverse: Dict[str, Set[str]], changed: List[str]) -> Dict[str, int]:
    """Every file whose imports reach a changed file, with its import distance, so a test
    of B is found when A, which B imports, changed."""
    distance: Dict[str, int] = {rel: 0 for rel in changed}
    frontier = list(changed)
    while frontier:
        nxt: List[str] = []
        for rel in frontier:
            for user in sorted(reverse.get(rel, ())):
                if user not in distance:
                    distance[user] = distance[rel] + 1
                    nxt.append(user)
        frontier = nxt
    return distance


def _all_tests(root: Path, test_dirs: List[str]) -> List[str]:
    found: Set[str] = set()
    for d in test_dirs:
        base = root / d
        if base.is_dir():
            found.update(
                p.relative_to(root).as_posix() for p in base.rglob("*.py")
                if p.name.startswith("test_") or p.name.endswith("_test.py")
            )
    return sorted(found)


def _under(conftest: str, tests: List[str]) -> List[str]:
    scope = Path(conftest).parent.as_posix()
    return [t for t in tests if scope == "." or t.startswith(scope + "/")]


def select_tests(root: Path, changed_py: List[str],
                 tests_cfg: Dict[str, Any]) -> Tuple[List[str], List[str], int, List[str]]:
    """Explicit test paths, the related ones the cap left out, how many were excluded, and
    the changed source files that no test reaches at all.

    Changed files may have been deleted: their tests are exactly the ones that should fail.
    """
    test_dirs = [d.strip("/") for d in tests_cfg.get("test_dirs", ["tests"])]
    strip_roots = set(tests_cfg.get("import_roots", ["src"]))
    all_tests = _all_tests(root, test_dirs)
    untested: List[str] = []
    if tests_cfg.get("scope") == "all":
        ordered = all_tests
    else:
        direct = [rel for rel in changed_py if _is_test_file(rel, test_dirs)]
        by_conftest: List[str] = []
        sources: List[str] = []
        for rel in changed_py:
            if rel in direct:
                continue
            if Path(rel).name == "conftest.py":
                by_conftest.extend(_under(rel, all_tests))
            else:
                sources.append(rel)
        tests = set(all_tests)
        reverse = _importers(root, sources, strip_roots) if sources else {}

        def related(src: str) -> Dict[str, int]:
            """Tests this one source reaches: by name, through imports, and through a
            conftest that imports it, whose fixtures its tests use without importing."""
            names = _module_names(src, strip_roots)
            stem = names[0].rsplit(".", 1)[-1] if names else ""
            found = {t: 0 for t in all_tests
                     if stem and (Path(t).stem == f"test_{stem}" or Path(t).stem.startswith(f"test_{stem}_"))}
            for rel, dist in _dependents(reverse, [src]).items():
                if rel in tests:
                    found.setdefault(rel, dist)
                elif Path(rel).name == "conftest.py":
                    for t in _under(rel, all_tests):
                        found.setdefault(t, dist + 1)
            return found

        reached: Dict[str, int] = {}
        for src in sources:
            found = related(src)
            if not found:
                untested.append(src)
            for rel, dist in found.items():
                reached[rel] = min(dist, reached.get(rel, dist))
        ordered = direct + sorted(reached, key=lambda rel: (reached[rel], rel)) + by_conftest

    excluded = tests_cfg.get("exclude", []) or []
    selected: List[str] = []
    omitted: Set[str] = set()
    for rel in ordered:
        if rel in selected or not (root / rel).is_file():
            continue
        if any(fnmatch.fnmatch(rel, pat) for pat in excluded):
            omitted.add(rel)
            continue
        selected.append(rel)
    cap = len(selected) if tests_cfg.get("scope") == "all" else int(tests_cfg.get("max_files", 8))
    return selected[:cap], selected[cap:], len(omitted), untested


def find_tests(root: Path, changed_py: List[str], tests_cfg: Dict[str, Any]) -> Tuple[List[str], List[str], int]:
    tests, capped, excluded, _ = select_tests(root, changed_py, tests_cfg)
    return tests, capped, excluded


def lint_files(root: Path, python: str, cfg: Dict[str, Any], files: List[str],
               deadline: Optional[float] = None) -> Optional[str]:
    lint = cfg.get("lint", {})
    if not lint.get("enabled") or not files:
        return None
    try:
        return run_lint(root, python, lint.get("args", []), files, deadline=deadline)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"lint could not run: {exc}"


def run_tests(root: Path, python: str, cfg: Dict[str, Any], tests: List[str],
              deadline: Optional[float] = None) -> Optional[str]:
    tcfg = cfg.get("tests", {})
    if not tcfg.get("enabled"):
        return None
    if not tests:
        return None
    cmd = [python, "-m", "pytest", *tcfg.get("pytest_args", []), *tests]
    # An empty bytecode cache per run: a same-size edit within the mtime resolution of an
    # existing __pycache__ entry would otherwise run the old code and pass.
    cache = tempfile.mkdtemp(prefix="kit-pycache-")
    env = {"PYTHONPYCACHEPREFIX": cache, "PYTHONDONTWRITEBYTECODE": "1", **(tcfg.get("env") or {})}
    try:
        timeout = int(tcfg.get("timeout_s", 300))
        if deadline is not None:
            timeout = min(timeout, deadline - time.monotonic())
            if timeout <= 0:
                return INCOMPLETE + "the hook's time budget ran out before pytest started."
        proc = run(
            cmd, root, timeout=timeout,
            env_file=tcfg.get("env_file"), extra_env=env, kill_tree=True,
        )
    except subprocess.TimeoutExpired:
        return (INCOMPLETE + f"pytest timed out after {tcfg.get('timeout_s', 300)}s on: {' '.join(tests)}. "
                "Use the repo's scoped gate, or raise tests.timeout_s.")
    except OSError as exc:
        return f"pytest could not run: {exc}"
    finally:
        shutil.rmtree(cache, ignore_errors=True)
    if proc.returncode == 0:
        return None
    if proc.returncode == PYTEST_NO_TESTS:
        return INCOMPLETE + "pytest collected no tests."
    if "No module named pytest" in proc.stderr:
        return (f"pytest is not installed for {python}, so no tests ran. Install it there, point "
                "`python` in .claude/kit.json at an interpreter that has it, or set tests.enabled to false.")
    return tail(proc.stdout + proc.stderr, max_lines=60)


def verify(root: Path, cfg: Dict[str, Any], changed: List[str],
           deadline: Optional[float] = None) -> Tuple[List[str], List[str], List[str]]:
    """Return (failures, tests run, gaps).

    A failure is something Claude can fix by changing code. A gap is coverage this run
    could not provide; it is reported, but another attempt would not close it.
    """
    gated = bool(cfg.get("verification", {}).get("enabled"))
    tcfg = cfg.get("tests", {})
    changed_py = [f for f in changed if f.endswith(".py")]
    reserve = 120 if changed_py and cfg.get("lint", {}).get("enabled") else 0
    gate_deadline = deadline - reserve if deadline is not None else None
    failures, _ = run_gates(root, cfg, changed, deadline=gate_deadline)
    gaps: List[str] = []
    # Python files that only related tests can check: every changed one without gates, and
    # in a gated repo the ones no gate claims (they used to be reported as a bare gap).
    test_targets = changed_py
    if gated:
        failures = [f for f in failures if not f.startswith(UNCOVERED)]
        uncovered = uncovered_by_gates(cfg, changed)
        test_targets = [f for f in uncovered if f.endswith(".py")] if tcfg.get("enabled") else []
        rest = [f for f in uncovered if f not in test_targets]
        if rest:
            gaps.append(UNCOVERED + ", ".join(rest))
    else:
        non_python = [f for f in changed if not f.endswith(".py")]
        if non_python:
            gaps.append("No configured verification gates cover these non-Python files: "
                        + ", ".join(non_python))
    if not changed_py:
        return failures, [], gaps
    python = resolve_python(cfg, root)
    if python is None:
        if not (cfg.get("lint", {}).get("enabled") or (tcfg.get("enabled") and test_targets)):
            return failures, [], gaps
        return failures + [f"No interpreter from `python` in .claude/kit.json exists here "
                f"({cfg.get('python') or ['python3']}), "
                "so lint and tests did not run. Create it, add a fallback such as \"python3\", or "
                "disable lint and tests."], [], gaps
    lint_out = lint_files(root, python, cfg, [f for f in changed_py if (root / f).is_file()], deadline=deadline)
    if lint_out:
        failures.append(f"Lint ({' '.join(cfg['lint'].get('args', []))}):\n{lint_out}")
    if not tcfg.get("enabled") or not test_targets:
        return failures, [], gaps
    tests, capped, excluded_count, untested = select_tests(root, test_targets, tcfg)
    if untested:
        gaps.append(INCOMPLETE + "no test reaches " + ", ".join(untested))
    if capped:
        shown = " ".join(capped[:10]) + (f" and {len(capped) - 10} more" if len(capped) > 10 else "")
        gaps.append(INCOMPLETE + f"{len(capped)} related test files, further from the change "
                    f"than the ones run, were omitted by tests.max_files: {shown}")
    if excluded_count:
        gaps.append(INCOMPLETE + f"{excluded_count} test files were omitted by exclusions.")
    if not tests:
        if not untested:
            gaps.append(INCOMPLETE + "no related tests were selected.")
        return failures, [], gaps
    test_out = run_tests(root, python, cfg, tests, deadline=deadline)
    if test_out and test_out.startswith(INCOMPLETE):
        gaps.append(test_out)
    elif test_out:
        failures.append(f"Tests (pytest {' '.join(tests)}):\n{test_out}")
    return failures, tests, gaps


def verdict_digest(root: Path, files: List[str]) -> str:
    """The state a verdict was reached on: the changed files and the config that judged them."""
    h = hashlib.sha256()
    for rel in [*files, CONFIG_RELPATH.as_posix(), ".kit/kit.json"]:
        h.update(rel.encode())
        h.update((file_digest(root / rel) or "-").encode())
    return h.hexdigest()


def hook_main() -> int:
    # The configured hook limit is 480 seconds; leave time for setup and reporting.
    deadline = time.monotonic() + 450
    data = read_input()
    session_id = str(data.get("session_id") or "")
    root = repo_root(data.get("cwd"))
    if root is None or not session_id:
        return 0
    try:
        cfg = load_config(root, strict=True)
    except ValueError as exc:
        message = f"Verification unavailable: {exc}. Correct the configuration or report the task unverified."
        if data.get("stop_hook_active"):
            emit({"systemMessage": message})
        else:
            emit({"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": message}})
        return 0
    if not (cfg.get("lint", {}).get("enabled") or cfg.get("tests", {}).get("enabled")
            or cfg.get("verification", {}).get("enabled")):
        return 0
    state = load_state(session_id)
    changed = changed_since(root, state.get("baseline", {}), state.get("edited", []),
                            others=lambda: written_by_others(root, session_id))
    if not changed:
        return 0
    digest = verdict_digest(root, changed)
    # The hook fires at the end of every turn. A diff already judged clean, or whose gaps
    # were already reported, gets the same verdict again, so do not pay for it twice.
    if digest in (state.get("passed_digest"), state.get("gaps_digest")):
        return 0

    failures, _, gaps = verify(root, cfg, changed, deadline=deadline)
    if not failures:
        state.pop("failed_digest", None)
        if gaps:
            state["gaps_digest"] = digest
            save_state(session_id, state)
            emit({"systemMessage": "kit verify: nothing failed, but verification is INCOMPLETE. "
                  "Do not report this change as verified.\n" + "\n".join(gaps)})
            return 0
        state["passed_digest"] = digest
        save_state(session_id, state)
        return 0

    if data.get("stop_hook_active") and state.get("failed_digest") == digest:
        emit({"systemMessage": "kit verify: still failing after one retry; letting Claude stop. "
              "Verification FAILED or INCOMPLETE. Report this explicitly; do not claim the task is verified."})
        return 0
    state["failed_digest"] = digest
    save_state(session_id, state)
    note = ("\n\nAlso not covered, and not something to fix now:\n" + "\n".join(gaps)) if gaps else ""
    # additionalContext keeps Claude going like decision=block, but reads as feedback, not a hook error.
    emit({"hookSpecificOutput": {
        "hookEventName": "Stop",
        "additionalContext": "Verification of the files changed in this session failed (kit Stop hook).\n\n"
        + "\n\n".join(failures) + note
        + "\n\nFix what your change broke, then finish. If a failure predates your change and is unrelated, "
        "say so plainly in your reply instead of fixing it. Use the configured test scope "
        "and report any remaining gaps.",
    }})
    return 0


def manual_main(files: List[str]) -> int:
    root = repo_root(None)
    if root is None:
        print("kit verify: not inside a git repository")
        return 2
    try:
        cfg = load_config(root, strict=True)
    except ValueError as exc:
        print(f"kit verify: unavailable: {exc}")
        return 2
    changed = [relative_to_root(root, f) or f for f in files] or dirty_files(root, include_deleted=True)
    failures, tests, gaps = verify(root, cfg, changed)
    py = [f for f in changed if f.endswith(".py")]
    print(f"kit verify: {len(py)} changed Python file(s); tests run: {' '.join(tests) or 'none'}")
    enabled = any(cfg.get(k, {}).get("enabled") for k in ("lint", "tests", "verification"))
    if not enabled:
        print("  no checks are enabled: add .claude/kit.json to this repo to enable them")
    if failures or gaps:
        print("\n\n".join(failures + gaps))
        print("kit verify: FAILED" if failures else "kit verify: INCOMPLETE")
        return 1
    if not enabled:
        print("kit verify: SKIPPED (no checks enabled)")
        return 2
    print("kit verify: OK")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--manual":
        sys.exit(manual_main(sys.argv[2:]))
    try:
        sys.exit(hook_main())
    except Exception as exc:  # noqa: BLE001 — surface failures without crashing the session
        emit({"systemMessage":
              f"kit verify: unavailable ({type(exc).__name__}); task is unverified."})
        sys.exit(0)
