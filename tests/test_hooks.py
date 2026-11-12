"""End-to-end tests for the kit plugin's hook scripts.

Each test builds a throwaway git repo, feeds the hook the JSON Claude Code would send
on stdin, and checks what the hook prints. Requires pytest and ruff in the interpreter
running the tests (`pip install pytest ruff`).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "kit" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from bash_guard import check, is_full_suite  # noqa: E402
from gates import run_gates  # noqa: E402
import kit_common  # noqa: E402
from kit_common import changed_since, snapshot, written_by_others  # noqa: E402
from stop_verify import find_tests  # noqa: E402

PY_CANDIDATES = [".venv/bin/python"]


def hook(script: str, payload: dict, data_dir: Path) -> dict:
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / script)],
        input=json.dumps(payload), capture_output=True, text=True, timeout=120,
        env={"CLAUDE_PLUGIN_DATA": str(data_dir), "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


def sh(cwd: Path, *args: str) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def commit(cwd: Path, message: str) -> None:
    sh(cwd, "git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    sh(cwd, "git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", message)


def manual(cwd: Path, *files: str, data_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "stop_verify.py"), "--manual", *files],
        cwd=cwd, capture_output=True, text=True, timeout=120,
        env={"CLAUDE_PLUGIN_DATA": str(data_dir), "PATH": "/usr/bin:/bin"},
    )


def set_config(root: Path, **changes: dict) -> None:
    path = root / ".claude" / "kit.json"
    cfg = json.loads(path.read_text())
    for key, value in changes.items():
        cfg[key] = {**cfg.get(key, {}), **value} if isinstance(value, dict) else value
    path.write_text(json.dumps(cfg))


def wrapper_python(root: Path, *flags: str) -> None:
    """`.venv/bin/python` as a script that runs this interpreter, so it resolves only from the repo root."""
    exe = root / ".venv" / "bin" / "python"
    exe.parent.mkdir(parents=True)
    exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" {" ".join(flags)} "$@"\n')
    exe.chmod(0o755)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / ".claude").mkdir()
    (root / "src" / "pkg" / "__init__.py").write_text("")
    (root / "src" / "pkg" / "mathx.py").write_text("def double(x):\n    return x * 2\n")
    (root / "src" / "pkg" / "other.py").write_text("def other():\n    return 1\n")
    (root / "tests" / "test_mathx.py").write_text(
        "from pkg.mathx import double\n\ndef test_double():\n    assert double(2) == 4\n"
    )
    (root / "tests" / "test_uses_import.py").write_text(
        "from pkg import mathx\n\ndef test_via_import():\n    assert mathx.double(1) == 2\n"
    )
    (root / "tests" / "test_unrelated.py").write_text("def test_ok():\n    assert True\n")
    (root / "pyproject.toml").write_text('[tool.pytest.ini_options]\npythonpath = [".", "src"]\n')
    (root / ".claude" / "kit.json").write_text(json.dumps({
        "python": [sys.executable],
        "lint": {
            "enabled": True,
            "args": ["-m", "ruff", "check", "--select", "E9,F63,F7,F82", "--output-format", "concise"],
        },
        "tests": {"enabled": True, "test_dirs": ["tests"], "import_roots": ["src"]},
        "guards": {"pytest_full_suite": "ask"},
    }))
    sh(root, "git", "init", "-q")
    sh(root, "git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    sh(root, "git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    return root


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "plugin-data"
    d.mkdir()
    return d


# ── bash guard ─────────────────────────────────────────────────────────────────
CFG = {"guards": {"sql_destructive": "ask", "pytest_full_suite": "ask"}, "tests": {"test_dirs": ["tests"]}}


@pytest.mark.parametrize("command", [
    'psql -c "DROP TABLE prices"',
    "psql -c 'TRUNCATE ticks'",
    'psql -c "DELETE FROM ticks;"',
    "python -c \"conn.execute('SELECT drop_chunks(\\'ticks\\', older_than => now())')\"",
    "pytest",
    "pytest -q",
    "python -m pytest tests/",
    "cd /opt/app && PYTHONPATH=. timeout 300 python -m pytest -k foo tests",
    "pytest -m slow",
    "uv run pytest --tb=short",
    # DELETE without WHERE, however the statement ends
    "psql -c 'DELETE FROM ticks RETURNING id;'",
    'psql -c "DELETE FROM ONLY ticks"',
    'psql -c "DELETE FROM ticks AS t;"',
    'psql -c "DELETE FROM ticks" && psql -c "SELECT 1 WHERE true"',
    "python -c \"conn.execute('DELETE FROM ticks'); conn.execute('SELECT 1 WHERE true')\"",
    "psql <<'SQL'\nDELETE FROM ticks;\nSQL",
    # a WHERE in a comment or a subquery does not filter the DELETE
    "psql -c 'DELETE FROM ticks /* WHERE id = 1 */;'",
    'psql -c "DELETE FROM ticks -- WHERE id = 1"',
    "psql <<'SQL'\nDELETE FROM ticks -- WHERE id = 1\n;\nSQL",
    "psql -c 'DELETE FROM ticks USING (SELECT 1 WHERE true) AS s;'",
    # options that take a value do not name a test path
    "pytest --log-cli-level INFO",
    "python -m pytest --capture sys",
    # an unquoted newline ends a command; redirections are not test paths
    "cd /opt/app\npython -m pytest",
    "pytest -q 2>&1 | tail -5",
    # a suite root next to a file still runs the whole suite
    "pytest tests tests/test_mathx.py",
])
def test_guard_asks(command: str) -> None:
    hit = check(command, CFG)
    assert hit is not None and hit[0] == "ask", command


@pytest.mark.parametrize("command", [
    "pip install pytest",
    "pytest tests/test_mathx.py",
    "python -m pytest -x tests/test_a.py::test_b",
    "pytest --collect-only -q",
    "pytest tests/architecture",
    'psql -c "DELETE FROM ticks WHERE ts < now()"',
    "make test-acquisition",
    "grep -r pytest .",
    # quoted identifiers are not statement ends; WHERE on a later line still counts
    "psql -c 'DELETE FROM \"ticks\" WHERE id = 1;'",
    'psql -c "DELETE FROM \\"ticks\\" WHERE id = 1;"',
    "psql <<'SQL'\nDELETE FROM ticks\nWHERE ts < now();\nSQL",
    "python -c \"conn.execute('DELETE FROM ticks WHERE id = %s', (1,))\"",
    "psql -c 'DELETE FROM ticks /* stale rows */ WHERE ts < now();'",
    "psql -c 'DELETE FROM ticks t USING (SELECT id FROM old) s WHERE t.id = s.id;'",
    "pytest --log-cli-level INFO tests/test_a.py",
    # a quoted newline or a line continuation does not split the command
    "python -m pytest \\\n  tests/test_a.py",
    'psql -c "SELECT 1\nFROM x"\npytest tests/test_a.py',
    "pytest tests/test_a.py 2>&1 | tail -5",
])
def test_guard_allows(command: str) -> None:
    assert check(command, CFG) is None, command


def test_filtered_delete_passes_in_deny_mode() -> None:
    deny = {"guards": {"sql_destructive": "deny"}}
    assert check("psql -c 'DELETE FROM \"ticks\" WHERE id = 1;'", deny) is None
    assert check("psql -c 'DELETE FROM ticks;'", deny) == ("deny", check("psql -c 'DELETE FROM ticks;'", CFG)[1])
    assert check("psql -c 'DELETE FROM ticks /* WHERE id = 1 */;'", deny)[0] == "deny"


def test_full_suite_guard_resolves_absolute_paths() -> None:
    root = Path("/opt/app")
    assert check("python -m pytest /opt/app/tests -m acquisition", CFG, root)[0] == "ask"
    assert check("pytest /opt/app/", CFG, root)[0] == "ask"
    assert check("pytest ./tests/", CFG, root)[0] == "ask"
    assert check("pytest /opt/app/tests/test_a.py", CFG, root) is None
    assert check("pytest /elsewhere/tests", CFG, root) is None
    assert check("pytest ../tests", CFG, root, "/opt/app/src")[0] == "ask"
    assert check("pytest ../tests/test_a.py", CFG, root, "/opt/app/src") is None


def test_full_suite_guard_off_by_default() -> None:
    assert check("pytest", {"guards": {}, "tests": {}}) is None
    assert is_full_suite("pytest", ["tests"])


def test_guard_hook_output(repo: Path, data_dir: Path) -> None:
    for command in ("pytest", f"python -m pytest {repo}/tests -q"):
        out = hook("bash_guard.py", {"cwd": str(repo), "tool_input": {"command": command}}, data_dir)
        assert out["hookSpecificOutput"]["permissionDecision"] == "ask", command
    assert hook("bash_guard.py", {"cwd": str(repo), "tool_input": {"command": "ls"}}, data_dir) == {}


# ── test selection ─────────────────────────────────────────────────────────────
def test_find_tests_by_name_and_import(repo: Path) -> None:
    tests, capped, _ = find_tests(repo, ["src/pkg/mathx.py"], {"test_dirs": ["tests"], "import_roots": ["src"]})
    assert tests == ["tests/test_mathx.py", "tests/test_uses_import.py"]
    assert capped == []


def test_unborn_baseline_tracks_new_commits_but_not_unchanged_existing_files(tmp_path: Path) -> None:
    root = tmp_path / "new-repo"
    root.mkdir()
    sh(root, "git", "init", "-q")
    (root / "existing.txt").write_text("present before session")
    baseline = snapshot(root)
    (root / "created file.txt").write_text("created during session")
    commit(root, "first commit")
    assert changed_since(root, baseline, []) == ["created file.txt"]


def test_find_tests_changed_test_file_and_cap(repo: Path) -> None:
    tests, capped, _ = find_tests(
        repo, ["tests/test_unrelated.py", "src/pkg/mathx.py"], {"test_dirs": ["tests"], "max_files": 2}
    )
    assert tests == ["tests/test_unrelated.py", "tests/test_mathx.py"]
    assert capped == ["tests/test_uses_import.py"]


def test_find_tests_multiline_import_and_patch_target(repo: Path) -> None:
    (repo / "tests" / "test_grouped.py").write_text("from pkg import (\n    other,\n)\n")
    (repo / "tests" / "test_patched.py").write_text(
        "import pkg\nfrom unittest import mock\n\nTARGET = mock.patch('pkg.other.other')\n"
    )
    (repo / "tests" / "test_decoy.py").write_text("import json\nNAME = 'other.csv'\n")
    tests, _, _ = find_tests(repo, ["src/pkg/other.py"], {"test_dirs": ["tests"], "import_roots": ["src"]})
    assert tests == ["tests/test_grouped.py", "tests/test_patched.py"]


def test_find_tests_for_conftest(repo: Path) -> None:
    (repo / "tests" / "sub").mkdir()
    (repo / "tests" / "sub" / "test_inner.py").write_text("def test_inner():\n    pass\n")
    cfg = {"test_dirs": ["tests"]}
    tests, _, _ = find_tests(repo, ["tests/sub/conftest.py"], cfg)
    assert tests == ["tests/sub/test_inner.py"]
    tests, _, _ = find_tests(repo, ["tests/conftest.py"], cfg)
    assert set(tests) == {
        "tests/sub/test_inner.py", "tests/test_mathx.py", "tests/test_unrelated.py", "tests/test_uses_import.py",
    }


def test_find_tests_exclude(repo: Path) -> None:
    tests, _, _ = find_tests(
        repo, ["src/pkg/mathx.py"], {"test_dirs": ["tests"], "import_roots": ["src"], "exclude": ["tests/test_uses_*"]}
    )
    assert tests == ["tests/test_mathx.py"]


def test_excluded_changed_test_is_reported(repo: Path, data_dir: Path) -> None:
    set_config(repo, lint={"enabled": False}, tests={"exclude": ["tests/test_unrelated.py"]})
    result = manual(repo, "tests/test_mathx.py", "tests/test_unrelated.py", data_dir=data_dir)
    assert result.returncode == 1
    assert "1 test files were omitted by exclusions" in result.stdout


def test_bad_gate_cwd_does_not_skip_other_gates(repo: Path) -> None:
    gates = [{"name": "bad", "paths": ["*.py"], "argv": [sys.executable, "-c", "pass"], "cwd": None},
             {"name": "good", "paths": ["*.py"], "argv": [sys.executable, "-c", "pass"]}]
    failures, ran = run_gates(repo, {"verification": {"enabled": True, "gates": gates}}, ["x.py"])
    assert any("Gate bad unavailable" in f for f in failures)
    assert ran == ["good"]


def test_gate_deadline_reports_unstarted_checks(repo: Path) -> None:
    import time
    gates = [{"name": "slow", "paths": ["*.py"], "argv": [sys.executable, "-c", "import time; time.sleep(2)"]},
             {"name": "later", "paths": ["*.py"], "argv": [sys.executable, "-c", "pass"]}]
    failures, _ = run_gates(repo, {"verification": {"enabled": True, "gates": gates}}, ["x.py"],
                            deadline=time.monotonic() + 0.1)
    assert any("Gate slow unavailable" in f for f in failures)
    assert any("Gate later incomplete" in f for f in failures)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group cleanup")
@pytest.mark.parametrize("wrapper_exits", [False, True])
def test_timed_out_gate_stops_descendants(repo: Path, wrapper_exits: bool) -> None:
    import time
    # Both a live wrapper and an exited wrapper with children holding its pipes
    # must have their workers stopped before the next verification check.
    worker = ("import pathlib,time; pathlib.Path('worker-started').write_text('ready'); "
              "time.sleep(1.5); pathlib.Path('late-worker-write').write_text('leaked')")
    wrapper = (f"import subprocess,sys,time; subprocess.Popen([sys.executable, '-c', {worker!r}]); "
               + ("pass" if wrapper_exits else "time.sleep(30)"))
    gates = [{"name": "worker", "paths": ["*.py"], "argv": [sys.executable, "-c", wrapper],
              "timeout_s": 1}]
    failures, ran = run_gates(repo, {"verification": {"enabled": True, "gates": gates}}, ["x.py"])
    assert (repo / "worker-started").exists()
    assert any("Gate worker unavailable" in f and "timed out" in f for f in failures)
    assert ran == []
    time.sleep(0.8)
    assert not (repo / "late-worker-write").exists()


@pytest.mark.parametrize("enabled", ["false", "true", 0, 1, None, [], {}])
def test_invalid_verification_enabled_never_reports_success(repo: Path, data_dir: Path, enabled) -> None:
    set_config(repo, lint={"enabled": False}, verification={"enabled": enabled, "gates": [
        {"name": "passing", "paths": ["*.py"], "argv": [sys.executable, "-c", "pass"]},
    ]})
    result = manual(repo, "src/pkg/mathx.py", data_dir=data_dir)
    assert result.returncode == 2
    assert "verification.enabled must be a boolean" in result.stdout
    assert "kit verify: OK" not in result.stdout
    base = {"session_id": "invalid-verification", "cwd": str(repo)}
    first = hook("stop_verify.py", base, data_dir)
    assert "verification.enabled must be a boolean" in first["hookSpecificOutput"]["additionalContext"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX SIGINT and process-group cleanup")
@pytest.mark.parametrize("wrapper_exits", [False, True])
def test_cancelled_manual_gate_stops_descendants(repo: Path, data_dir: Path, wrapper_exits: bool) -> None:
    import signal
    import time
    from contextlib import suppress

    worker = (
        "import pathlib,time\n"
        "pathlib.Path('worker-started').write_text('ready')\n"
        "while not pathlib.Path('release-worker').exists(): time.sleep(0.02)\n"
        "pathlib.Path('late-worker-write').write_text('leaked')\n"
    )
    wrapper = (
        "import os,pathlib,subprocess,sys,time; "
        "pathlib.Path('gate-pgid').write_text(str(os.getpgrp())); "
        f"subprocess.Popen([sys.executable, '-c', {worker!r}]); "
        + ("pass" if wrapper_exits else "time.sleep(30)")
    )
    set_config(repo, lint={"enabled": False}, verification={"enabled": True, "gates": [
        {"name": "worker", "paths": ["*.py"], "argv": [sys.executable, "-c", wrapper], "timeout_s": 30},
    ]})
    proc = subprocess.Popen(
        [sys.executable, str(SCRIPTS / "stop_verify.py"), "--manual", "src/pkg/mathx.py"],
        cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
        env={**os.environ, "CLAUDE_PLUGIN_DATA": str(data_dir)},
    )
    try:
        deadline = time.monotonic() + 5
        while not (repo / "worker-started").exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert (repo / "worker-started").exists(), "gate worker never became ready"
        proc.send_signal(signal.SIGINT)
        stdout, stderr = proc.communicate(timeout=3)
        assert proc.returncode != 0 and "KeyboardInterrupt" in stderr
        assert "kit verify: OK" not in stdout
        (repo / "release-worker").write_text("continue if still alive")
        time.sleep(0.5)
        assert not (repo / "late-worker-write").exists(), "cancelled gate worker kept writing"
    finally:
        # Clean up even when the regression fails against the old implementation.
        pgid_file = repo / "gate-pgid"
        if pgid_file.exists():
            with suppress(ProcessLookupError):
                os.killpg(int(pgid_file.read_text()), signal.SIGKILL)
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=3)


def test_gate_mode_does_not_require_project_python(repo: Path, data_dir: Path) -> None:
    set_config(repo, python=["nonexistent-python-kit"], lint={"enabled": False},
               verification={"enabled": True, "gates": [{"name": "external", "paths": ["*.py"],
                              "argv": [sys.executable, "-c", "pass"]}]})
    result = manual(repo, "src/pkg/mathx.py", data_dir=data_dir)
    assert result.returncode == 0, result.stdout


def test_overlapping_test_dirs_run_each_file_once(repo: Path, data_dir: Path) -> None:
    (repo / "tests" / "sub").mkdir()
    (repo / "tests" / "sub" / "test_once.py").write_text("import pkg.mathx\ndef test_once(): pass\n")
    set_config(repo, lint={"enabled": False}, tests={"test_dirs": ["tests", "tests/sub"]})
    result = manual(repo, "src/pkg/mathx.py", data_dir=data_dir)
    assert result.returncode == 0, result.stdout
    summary = result.stdout.split("tests run:", 1)[1].splitlines()[0]
    assert summary.count("tests/sub/test_once.py") == 1


@pytest.mark.parametrize("settings", [
    {"test_dirs": None}, {"max_files": 0}, {"timeout_s": "300"},
    {"env": {"KEY": 1}}, {"pytest_args": "-q"},
])
def test_invalid_nested_settings_are_reported(repo: Path, data_dir: Path, settings: dict) -> None:
    set_config(repo, tests=settings)
    result = manual(repo, "tests/test_mathx.py", data_dir=data_dir)
    assert result.returncode == 2 and "Invalid kit configuration" in result.stdout
    base = {"session_id": "bad-settings", "cwd": str(repo)}
    assert "additionalContext" in hook("stop_verify.py", base, data_dir)["hookSpecificOutput"]
    retry = hook("stop_verify.py", {**base, "stop_hook_active": True}, data_dir)
    assert "unverified" in retry["systemMessage"] and "hookSpecificOutput" not in retry


# ── session flow ───────────────────────────────────────────────────────────────
def test_stop_blocks_once_then_lets_go(repo: Path, data_dir: Path) -> None:
    sid = str(uuid.uuid4())
    base = {"session_id": sid, "cwd": str(repo)}
    hook("session_start.py", {**base, "source": "startup"}, data_dir)

    target = repo / "src" / "pkg" / "mathx.py"
    target.write_text("def double(x):\n    return x * 3\n")
    hook("post_edit.py", {**base, "tool_name": "Edit", "tool_input": {"file_path": str(target)}}, data_dir)

    first = hook("stop_verify.py", {**base, "stop_hook_active": False}, data_dir)
    feedback = first["hookSpecificOutput"]["additionalContext"]
    assert "tests/test_mathx.py" in feedback and "assert 6 == 4" in feedback
    assert "tests/test_unrelated.py" not in feedback, "a test nothing links to the change ran"
    assert hook("stop_verify.py", {**base, "stop_hook_active": False}, data_dir)[
        "hookSpecificOutput"], "a new turn on the same failing diff still blocks once"


    again = hook("stop_verify.py", {**base, "stop_hook_active": True}, data_dir)
    assert "hookSpecificOutput" not in again and "still failing" in again["systemMessage"]

    target.write_text("def double(x):\n    return x * 2\n")
    assert hook("stop_verify.py", {**base, "stop_hook_active": True}, data_dir) == {}


def test_preexisting_dirty_file_is_ignored(repo: Path, data_dir: Path) -> None:
    (repo / "src" / "pkg" / "mathx.py").write_text("def double(x):\n    return x * 3\n")  # broken before the session
    sid = str(uuid.uuid4())
    base = {"session_id": sid, "cwd": str(repo)}
    set_config(repo, tests={"enabled": False})
    hook("session_start.py", {**base, "source": "startup"}, data_dir)
    assert hook("stop_verify.py", {**base, "stop_hook_active": False}, data_dir) == {}

    other = repo / "src" / "pkg" / "other.py"
    other.write_text("def other():\n    return 2\n")  # changed via Bash, not an Edit tool
    assert hook("stop_verify.py", {**base, "stop_hook_active": False}, data_dir) == {}


def _session(repo: Path, data_dir: Path) -> dict:
    base = {"session_id": str(uuid.uuid4()), "cwd": str(repo)}
    hook("session_start.py", {**base, "source": "startup"}, data_dir)
    return base


def _tool_edit(base: dict, target: Path, text: str, data_dir: Path) -> None:
    target.write_text(text)
    hook("post_edit.py", {**base, "tool_name": "Edit", "tool_input": {"file_path": str(target)}}, data_dir)


def _blocked(base: dict, data_dir: Path) -> bool:
    return "hookSpecificOutput" in hook("stop_verify.py", {**base, "stop_hook_active": False}, data_dir)


BROKEN = "def double(x):\n    return x * 3\n"


def test_another_sessions_edit_is_left_to_that_session(repo: Path, data_dir: Path) -> None:
    mine, theirs = _session(repo, data_dir), _session(repo, data_dir)
    target = repo / "src" / "pkg" / "mathx.py"
    _tool_edit(theirs, target, BROKEN, data_dir)

    assert not _blocked(mine, data_dir), "a session was held on a file only another session edited"
    assert _blocked(theirs, data_dir), "the session that made the edit must still verify it"


def test_a_change_no_session_recorded_after_anothers_edit_stays_in_scope(repo: Path, data_dir: Path) -> None:
    mine, theirs = _session(repo, data_dir), _session(repo, data_dir)
    target = repo / "src" / "pkg" / "mathx.py"
    _tool_edit(theirs, target, "def double(x):\n    return x * 2  # theirs\n", data_dir)
    assert not _blocked(mine, data_dir)

    recorded = target.stat()
    target.write_text(BROKEN)  # written by a shell command: no session's edit tool saw it
    os.utime(target, ns=(recorded.st_atime_ns, recorded.st_mtime_ns))  # even with the same timestamp
    assert _blocked(mine, data_dir), "a write after the other session's edit could be this session's own"


def test_anothers_edit_is_recognised_by_content_not_by_timestamp(repo: Path, data_dir: Path) -> None:
    mine, theirs = _session(repo, data_dir), _session(repo, data_dir)
    target = repo / "src" / "pkg" / "mathx.py"
    _tool_edit(theirs, target, BROKEN, data_dir)
    recorded = target.stat()
    os.utime(target, ns=(recorded.st_atime_ns, recorded.st_mtime_ns + 5_000_000_000))  # a checkout or stash rewrote it
    assert not _blocked(mine, data_dir)


def test_a_file_another_session_edited_and_someone_deleted_stays_in_scope(repo: Path, data_dir: Path) -> None:
    mine, theirs = _session(repo, data_dir), _session(repo, data_dir)
    target = repo / "src" / "pkg" / "mathx.py"
    _tool_edit(theirs, target, "def double(x):\n    return x * 2  # theirs\n", data_dir)
    target.unlink()
    assert _blocked(mine, data_dir), "a deletion cannot be attributed, and its tests must fail"


def test_parallel_hooks_of_one_session_do_not_share_a_temp_file(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(data_dir))
    replaced = []
    monkeypatch.setattr(kit_common.os, "replace", lambda src, dst: replaced.append(Path(src).name))
    monkeypatch.setattr(kit_common.os, "getpid", lambda: 111)
    kit_common.save_state("s", {"edited": []})
    monkeypatch.setattr(kit_common.os, "getpid", lambda: 222)
    kit_common.save_state("s", {"edited": []})
    assert len(set(replaced)) == 2, replaced
    assert not list((data_dir / "state").glob("*.json")), "a temp file must never look like a session's state"


def test_a_file_both_sessions_edited_is_still_checked(repo: Path, data_dir: Path) -> None:
    mine, theirs = _session(repo, data_dir), _session(repo, data_dir)
    target = repo / "src" / "pkg" / "mathx.py"
    _tool_edit(mine, target, "def double(x):\n    return x * 2  # mine\n", data_dir)
    _tool_edit(theirs, target, BROKEN, data_dir)
    assert _blocked(mine, data_dir), "this session edited the file too, so its content is still its concern"


def test_another_sessions_committed_edit_is_left_to_that_session(repo: Path, data_dir: Path) -> None:
    mine, theirs = _session(repo, data_dir), _session(repo, data_dir)
    _tool_edit(theirs, repo / "src" / "pkg" / "mathx.py", BROKEN, data_dir)
    commit(repo, "the other session commits its work")
    assert not _blocked(mine, data_dir)


def test_only_sessions_in_the_same_checkout_are_consulted(repo: Path, data_dir: Path, tmp_path: Path,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(data_dir))
    mine, theirs = _session(repo, data_dir), _session(repo, data_dir)
    target = repo / "src" / "pkg" / "mathx.py"
    _tool_edit(theirs, target, BROKEN, data_dir)
    content = hashlib.sha256(target.read_bytes()).hexdigest()

    state = data_dir / "state"
    elsewhere = json.loads((state / f"{theirs['session_id']}.json").read_text())
    elsewhere["baseline"]["root"] = str(tmp_path / "another-checkout")
    elsewhere["edited_digest"] = {"src/pkg/other.py": "0" * 64}
    (state / "elsewhere.json").write_text(json.dumps(elsewhere))
    (state / "torn.json").write_text("{not json")
    (state / "old-format.json").write_text(json.dumps({"baseline": {"root": str(repo)}, "edited": ["src/pkg/other.py"]}))
    (state / "wrong-types.json").write_text(json.dumps({"baseline": {"root": str(repo)}, "edited_digest": {"x.py": 1, "y.py": True, "z.py": ""}}))

    assert written_by_others(repo, mine["session_id"]) == {"src/pkg/mathx.py": {content}}
    assert written_by_others(repo, theirs["session_id"]) == {}, "a session does not read its own record"


def test_post_edit_reports_lint_errors(repo: Path, data_dir: Path) -> None:
    target = repo / "src" / "pkg" / "other.py"
    target.write_text("def other():\n    return undefined_name\n")
    out = hook("post_edit.py", {
        "session_id": "s", "cwd": str(repo), "tool_name": "Write", "tool_input": {"file_path": str(target)},
    }, data_dir)
    assert out["decision"] == "block" and "F821" in out["reason"]


def test_repo_without_config_is_untouched(tmp_path: Path, data_dir: Path) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    sh(root, "git", "init", "-q")
    (root / "a.py").write_text("x = undefined\n")
    base = {"session_id": "p", "cwd": str(root)}
    hook("session_start.py", base, data_dir)
    assert hook("post_edit.py", {**base, "tool_input": {"file_path": str(root / "a.py")}}, data_dir) == {}
    assert hook("stop_verify.py", base, data_dir) == {}


def test_lint_falls_back_to_ruff_on_path(repo: Path, data_dir: Path) -> None:
    cfg = json.loads((repo / ".claude" / "kit.json").read_text())
    cfg["python"] = ["/usr/bin/python3"]  # interpreter without the ruff module
    (repo / ".claude" / "kit.json").write_text(json.dumps(cfg))
    target = repo / "src" / "pkg" / "other.py"
    target.write_text("def other():\n    return undefined_name\n")
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "post_edit.py")],
        input=json.dumps({"session_id": "f", "cwd": str(repo), "tool_input": {"file_path": str(target)}}),
        capture_output=True, text=True, timeout=60,
        env={"CLAUDE_PLUGIN_DATA": str(data_dir), "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin"},
    )
    assert "F821" in json.loads(proc.stdout)["reason"]


def test_repo_patterns() -> None:
    cfg = {"guards": {"patterns": [
        {"regex": r"scripts[/.]data[/.]download", "reason": "check cost", "decision": "ask"},
        {"regex": "(", "reason": "invalid regex is skipped"},
    ]}}
    assert check("python scripts/data/download_databento.py --day 2026-10-01", cfg) == ("ask", "check cost")
    assert check("python -m scripts.data.download_mbp10", cfg) == ("ask", "check cost")
    assert check("python scripts/research/run.py", cfg) is None


def test_deleted_module_still_runs_its_tests(repo: Path, data_dir: Path) -> None:
    for committed in (False, True):
        sid = str(uuid.uuid4())
        base = {"session_id": sid, "cwd": str(repo)}
        hook("session_start.py", {**base, "source": "startup"}, data_dir)
        (repo / "src" / "pkg" / "mathx.py").unlink()
        if committed:
            commit(repo, "drop mathx")
        out = hook("stop_verify.py", {**base, "stop_hook_active": False}, data_dir)
        feedback = out["hookSpecificOutput"]["additionalContext"]
        assert "tests/test_mathx.py" in feedback and "mathx" in feedback, committed
        sh(repo, "git", "checkout", "HEAD~1" if committed else "HEAD", "--", "src/pkg/mathx.py")
        if committed:
            commit(repo, "restore mathx")


def test_staged_rename_runs_tests_of_the_old_name(repo: Path, data_dir: Path) -> None:
    sid = str(uuid.uuid4())
    base = {"session_id": sid, "cwd": str(repo)}
    hook("session_start.py", {**base, "source": "startup"}, data_dir)
    sh(repo, "git", "mv", "src/pkg/mathx.py", "src/pkg/renamed.py")
    out = hook("stop_verify.py", {**base, "stop_hook_active": False}, data_dir)
    feedback = out["hookSpecificOutput"]["additionalContext"]
    assert "tests/test_mathx.py" in feedback and "mathx" in feedback


def test_restored_baseline_dirty_file_runs_its_tests(repo: Path, data_dir: Path) -> None:
    target = repo / "src" / "pkg" / "mathx.py"
    target.write_text("def double(x):\n    return x * 3\n")
    commit(repo, "break double")
    target.write_text("def double(x):\n    return x * 2\n")  # fixed, uncommitted, before the session
    sid = str(uuid.uuid4())
    base = {"session_id": sid, "cwd": str(repo)}
    hook("session_start.py", {**base, "source": "startup"}, data_dir)
    sh(repo, "git", "checkout", "--", "src/pkg/mathx.py")  # Bash throws the fix away
    out = hook("stop_verify.py", {**base, "stop_hook_active": False}, data_dir)
    assert "assert 6 == 4" in out["hookSpecificOutput"]["additionalContext"]


def test_missing_interpreter_is_a_failure(repo: Path, data_dir: Path) -> None:
    set_config(repo, python=PY_CANDIDATES)  # .venv not created yet
    proc = manual(repo, "src/pkg/mathx.py", data_dir=data_dir)
    assert proc.returncode == 1 and "lint and tests did not run" in proc.stdout
    set_config(repo, lint={"enabled": False}, tests={"enabled": False})
    assert manual(repo, "src/pkg/mathx.py", data_dir=data_dir).returncode == 2


def test_committed_baseline_dirty_file_is_ignored(repo: Path, data_dir: Path) -> None:
    (repo / "src" / "pkg" / "mathx.py").write_text("def double(x):\n    return x * 3\n")  # broken before the session
    sid = str(uuid.uuid4())
    base = {"session_id": sid, "cwd": str(repo)}
    set_config(repo, tests={"enabled": False})
    hook("session_start.py", {**base, "source": "startup"}, data_dir)
    (repo / "src" / "pkg" / "other.py").write_text("def other():\n    return 2\n")
    commit(repo, "session work, sweeping in the pre-existing edit")
    assert hook("stop_verify.py", {**base, "stop_hook_active": False}, data_dir) == {}


def test_stale_bytecode_is_not_used(repo: Path, data_dir: Path) -> None:
    target = repo / "src" / "pkg" / "mathx.py"
    env = {**os.environ, "PYTHONPATH": "src"}
    sh_env = {"cwd": repo, "env": env, "capture_output": True, "text": True, "check": True}
    subprocess.run([sys.executable, "-c", "import pkg.mathx"], **sh_env)  # writes __pycache__
    sid = str(uuid.uuid4())
    base = {"session_id": sid, "cwd": str(repo)}
    hook("session_start.py", {**base, "source": "startup"}, data_dir)
    before = target.stat()
    target.write_text("def double(x):\n    return x * 3\n")  # same size
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))  # same mtime
    stale = subprocess.run([sys.executable, "-c", "import pkg.mathx; print(pkg.mathx.double(2))"], **sh_env)
    assert stale.stdout.strip() == "4"  # plain Python really does run the old bytecode here
    hook("post_edit.py", {**base, "tool_name": "Edit", "tool_input": {"file_path": str(target)}}, data_dir)
    out = hook("stop_verify.py", {**base, "stop_hook_active": False}, data_dir)
    assert "assert 6 == 4" in out["hookSpecificOutput"]["additionalContext"]


def test_missing_pytest_is_a_failure(repo: Path, data_dir: Path) -> None:
    wrapper_python(repo, "-S")  # no site-packages, so no pytest
    set_config(repo, python=PY_CANDIDATES, lint={"enabled": False})
    proc = manual(repo, "src/pkg/mathx.py", data_dir=data_dir)
    assert proc.returncode == 1 and "pytest is not installed" in proc.stdout


def test_lint_only_reports_no_tests(repo: Path, data_dir: Path) -> None:
    set_config(repo, tests={"enabled": False})
    proc = manual(repo, "src/pkg/mathx.py", data_dir=data_dir)
    assert proc.returncode == 0 and "tests run: none" in proc.stdout


def test_manual_from_subdirectory_uses_root_relative_paths(repo: Path, data_dir: Path) -> None:
    wrapper_python(repo)
    (repo / ".env.test").write_text("KIT_MARKER=from-env\n")
    (repo / "tests" / "test_envcheck.py").write_text(
        "import os\n\nfrom pkg.mathx import double\n\n"
        "def test_env():\n    assert os.environ.get('KIT_MARKER') == 'from-env' and double(1) == 2\n"
    )
    set_config(repo, python=PY_CANDIDATES, tests={"env_file": ".env.test"})
    proc = manual(repo / "src", "pkg/mathx.py", data_dir=data_dir)
    assert "tests/test_envcheck.py" in proc.stdout and proc.stdout.rstrip().endswith("kit verify: OK"), proc.stdout


# ── session policy ─────────────────────────────────────────────────────────────
def policy_context(payload: dict, data_dir: Path) -> str:
    out = hook("session_policy.py", payload, data_dir)
    specific = out.get("hookSpecificOutput", {})
    assert specific.get("hookEventName") == "SessionStart", out
    return specific.get("additionalContext", "")


def test_policy_is_added_as_session_context(repo: Path, data_dir: Path) -> None:
    text = policy_context({"cwd": str(repo), "session_id": uuid.uuid4().hex}, data_dir)
    assert "deliver" in text and "ultracode" in text


def test_policy_puts_the_design_decision_on_claude(repo: Path, data_dir: Path) -> None:
    """The approach is Claude's to settle, not the user's to supply."""
    text = policy_context({"cwd": str(repo)}, data_dir)
    assert "approach" in text and "never the user's to supply" in text


def test_the_design_stage_is_wired_end_to_end() -> None:
    """deliver hands the design stage to deliberate, which names a critic that exists."""
    kit = Path(__file__).resolve().parents[1] / "plugins" / "kit"
    deliver = (kit / "skills" / "deliver" / "SKILL.md").read_text()
    deliberate = (kit / "skills" / "deliberate" / "SKILL.md").read_text()
    assert "deliberate" in deliver, "deliver never reaches the design stage"
    assert "kit:design-critic" in deliberate, "the loop has no adversary"
    assert (kit / "agents" / "design-critic.md").is_file(), "the critic it names does not exist"


def test_a_decision_is_written_to_be_found_again() -> None:
    """The loop only avoids redoing work if step 2 reads what step 8 wrote. Both ends
    name the same directory, and the template carries the two fields step 2 acts on."""
    kit = Path(__file__).resolve().parents[1] / "plugins" / "kit"
    deliberate = (kit / "skills" / "deliberate" / "SKILL.md").read_text()
    look_up, record = deliberate.split("## 8. Record it", 1)
    assert "routing.decisions" in look_up, "step 2 never reads the decision logs"
    assert "routing.decisions" in record, "step 8 writes somewhere step 2 does not read"
    assert "Supersedes" in record, "a log cannot replace an earlier one"
    assert "What would change this decision" in record, "no reversal condition to check"
    # Reading the log is only half of it. A log whose condition has fired has to reopen
    # the decision from the field it already holds, or the next run pays for the
    # comparison twice. `evals/reversed-decision-reopens` is what scores that happening.
    assert "Now true" in look_up, "step 2 only handles a decision that still stands"
    assert "starting set" in look_up, (
        "a reopened decision has to start from the recorded candidates, not from nothing"
    )
    config = json.loads((kit / "templates" / ".claude" / "kit.json").read_text())
    assert "decisions" in config["routing"], "the template never points anywhere"


def test_a_generic_addition_is_offered_to_the_kit() -> None:
    """Without this, every repo rebuilds the same skill and the kit never learns."""
    kit = Path(__file__).resolve().parents[1] / "plugins" / "kit"
    rule = (kit / "portable" / "kit-deliver" / "references" / "capabilities.md").read_text()
    assert "belongs in the kit" in rule, "nothing promotes a generic addition"
    assert "after evaluation" in rule, "promotion requires evaluation"
    assert "positive Δ" in rule, "an instructions-only skill needs a scored eval case"


def test_the_capability_rule_has_one_owner() -> None:
    """Four files used to restate it, and the four copies had already drifted apart."""
    kit = Path(__file__).resolve().parents[1] / "plugins" / "kit"
    readers = [kit / "skills" / name / "SKILL.md" for name in ("deliver", "deliberate", "improve")]
    readers.append(kit / "portable" / "kit-deliver" / "SKILL.md")
    for path in readers:
        text = path.read_text()
        assert "capabilities.md" in text, f"{path.parent.name} no longer points at the rule"
        assert "belongs in the kit" not in text, f"{path.parent.name} restates the rule"
    deliver = (kit / "skills" / "deliver" / "SKILL.md").read_text()
    assert "portable/kit-deliver/SKILL.md" not in deliver, (
        "deliver loads the other hosts' protocol on top of its own"
    )


def test_policy_can_be_turned_off(repo: Path, data_dir: Path) -> None:
    set_config(repo, routing={"enabled": False})
    assert hook("session_policy.py", {"cwd": str(repo)}, data_dir) == {}


def test_policy_appends_the_repo_note(repo: Path, data_dir: Path) -> None:
    set_config(repo, routing={"note": "Work on dated branches.\n  Gates live in the Makefile."})
    text = policy_context({"cwd": str(repo)}, data_dir)
    assert text.endswith("Work on dated branches. Gates live in the Makefile.")


def test_policy_caps_the_repo_note(repo: Path, data_dir: Path) -> None:
    set_config(repo, routing={"note": "x" * 900})
    text = policy_context({"cwd": str(repo)}, data_dir)
    assert text.count("x") == 400


def test_policy_outside_a_repo_still_prints(tmp_path: Path, data_dir: Path) -> None:
    assert "deliver" in policy_context({"cwd": str(tmp_path)}, data_dir)


def test_policy_survives_a_broken_config(repo: Path, data_dir: Path) -> None:
    (repo / ".claude" / "kit.json").write_text("{ not json")
    assert "deliver" in policy_context({"cwd": str(repo)}, data_dir)


# ── plugin metadata ────────────────────────────────────────────────────────────
def test_every_frontmatter_value_is_valid_yaml() -> None:
    """A plain YAML scalar may not contain ': '. An unquoted description that does
    makes the whole frontmatter unparseable, and the skill or agent silently fails
    to load."""
    root = Path(__file__).resolve().parents[1] / "plugins"
    offenders = []
    for path in sorted(root.rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---\n"):
            continue
        for line in text.split("---\n", 2)[1].splitlines():
            key, sep, value = line.partition(": ")
            if not sep or not key or not key[0].isalpha() or " " in key:
                continue
            if ": " in value and value[:1] not in {'"', "'", "|", ">"}:
                offenders.append(f"{path.relative_to(root)}: {key}")
    assert not offenders, f"unquoted ': ' in frontmatter: {offenders}"


def test_missing_linter_is_not_a_pass(repo: Path, data_dir: Path) -> None:
    set_config(repo, lint={"args": ["-m", "kit_missing_linter_82479"]}, tests={"enabled": False})
    result = manual(repo, "src/pkg/mathx.py", data_dir=data_dir)
    assert result.returncode == 1 and "lint unavailable" in result.stdout


def test_transitive_failure_and_cap_are_not_hidden(repo: Path, data_dir: Path) -> None:
    (repo / "src/pkg/consumer.py").write_text("from pkg.mathx import double\n")
    for n in range(9):
        (repo / f"tests/test_indirect_{n:02}.py").write_text(
            "from pkg.consumer import double\n" + f"def test_indirect(): assert double(2) == {4 if n < 8 else 9}\n"
        )
    result = manual(repo, "src/pkg/mathx.py", data_dir=data_dir)
    assert result.returncode == 1 and "test_indirect_08" in result.stdout


def test_empty_collection_is_not_a_pass(repo: Path, data_dir: Path) -> None:
    set_config(repo, tests={"pytest_args": ["-q", "-k", "no_such_test_82479"]})
    result = manual(repo, "src/pkg/mathx.py", data_dir=data_dir)
    assert result.returncode == 1 and "collected no tests" in result.stdout


def test_broken_config_reports_unavailable(repo: Path, data_dir: Path) -> None:
    (repo / ".claude/kit.json").write_text('{"lint": null}')
    result = manual(repo, "src/pkg/mathx.py", data_dir=data_dir)
    assert result.returncode == 2 and "Invalid kit configuration" in result.stdout
    out = hook("stop_verify.py", {"session_id": "bad-config", "cwd": str(repo)}, data_dir)
    assert "Verification unavailable" in out["hookSpecificOutput"]["additionalContext"]


def test_changed_test_config_invalidates_prior_pass(repo: Path, data_dir: Path) -> None:
    (repo / "tests/test_env.py").write_text(
        "import os\nimport pkg.mathx\ndef test_env(): assert os.getenv('KIT_TEST') == 'good'\n"
    )
    set_config(repo, tests={"env": {"KIT_TEST": "good"}})
    base = {"session_id": "cache-check", "cwd": str(repo)}
    hook("session_start.py", base, data_dir)
    (repo / "src/pkg/mathx.py").write_text("def double(x):\n    return x * 2 + 0\n")
    assert hook("stop_verify.py", base, data_dir) == {}
    set_config(repo, tests={"env": {"KIT_TEST": "bad"}})
    assert "test_env" in hook("stop_verify.py", base, data_dir)["hookSpecificOutput"]["additionalContext"]


def test_language_neutral_gate_pass_fail_and_uncovered(repo: Path, data_dir: Path) -> None:
    source = repo / "src/app.ts"
    source.write_text("export const answer = 42;\n")
    set_config(repo, lint={"enabled": False}, tests={"enabled": False}, verification={
        "enabled": True, "gates": [{"name": "typescript", "paths": ["src/*.ts"],
                                    "argv": [sys.executable, "-c", "import sys; sys.exit(0)"]}]})
    result = manual(repo, "src/app.ts", data_dir=data_dir)
    assert result.returncode == 0, result.stdout
    set_config(repo, verification={"gates": [{"name": "typescript", "paths": ["src/*.ts"],
                                               "argv": [sys.executable, "-c", "import sys; sys.exit(3)"]}]})
    result = manual(repo, "src/app.ts", data_dir=data_dir)
    assert result.returncode == 1 and "Gate typescript failed (exit 3)" in result.stdout
    result = manual(repo, "README.md", data_dir=data_dir)
    assert result.returncode == 1 and "No configured gate covers" in result.stdout


GATE_OK = {"name": "ts", "paths": ["src/*.ts"], "argv": [sys.executable, "-c", "import sys; sys.exit(0)"]}


def test_gated_repo_tests_a_python_file_no_gate_claims(repo: Path, data_dir: Path) -> None:
    # Gates and related tests together: a .py no gate claims runs its related tests instead of
    # being reported as a bare gap (the gap fired on every session in a partly gated repo).
    (repo / "src/app.ts").write_text("x\n")
    set_config(repo, verification={"enabled": True, "gates": [GATE_OK]})
    result = manual(repo, "src/app.ts", "src/pkg/mathx.py", data_dir=data_dir)
    assert result.returncode == 0, result.stdout
    assert "tests/test_mathx.py" in result.stdout and "No configured gate covers" not in result.stdout
    (repo / "src/pkg/mathx.py").write_text("def double(x):\n    return x * 3\n")
    result = manual(repo, "src/pkg/mathx.py", data_dir=data_dir)
    assert result.returncode == 1 and "kit verify: FAILED" in result.stdout
    result = manual(repo, "tests/test_unrelated.py", data_dir=data_dir)      # a changed test runs itself
    assert result.returncode == 0 and "tests/test_unrelated.py" in result.stdout


def test_gated_repo_python_file_no_test_reaches_is_still_a_gap(repo: Path, data_dir: Path) -> None:
    set_config(repo, verification={"enabled": True, "gates": [GATE_OK]})
    result = manual(repo, "src/pkg/other.py", data_dir=data_dir)
    assert result.returncode == 1 and "no test reaches src/pkg/other.py" in result.stdout
    set_config(repo, tests={"enabled": False})                                  # no tests: a plain gap
    result = manual(repo, "src/pkg/mathx.py", data_dir=data_dir)
    assert result.returncode == 1 and "No configured gate covers: src/pkg/mathx.py" in result.stdout


def test_no_checks_files_are_not_gaps(repo: Path, data_dir: Path) -> None:
    (repo / "src/app.ts").write_text("x\n")
    (repo / "docs").mkdir()
    (repo / "docs/note.md").write_text("n\n")
    set_config(repo, verification={"enabled": True, "gates": [GATE_OK], "no_checks": ["docs/**", "*.md"]})
    result = manual(repo, "src/app.ts", "docs/note.md", "README.md", data_dir=data_dir)
    assert result.returncode == 0, result.stdout
    set_config(repo, verification={"no_checks": ["**/*.md"]})                  # needs a slash: misses root
    result = manual(repo, "src/app.ts", "README.md", data_dir=data_dir)
    assert result.returncode == 1 and "No configured gate covers: README.md" in result.stdout
    (repo / "data.csv").write_text("a\n")
    result = manual(repo, "data.csv", data_dir=data_dir)                        # not declared: still a gap
    assert result.returncode == 1 and "No configured gate covers: data.csv" in result.stdout


def test_gated_repo_without_runnable_gates_reports_only_the_config(repo: Path, data_dir: Path) -> None:
    set_config(repo, verification={"enabled": True, "gates": [{"name": "broken", "paths": ["src/**"]}]})
    result = manual(repo, "src/pkg/mathx.py", data_dir=data_dir)
    assert result.returncode == 1 and "invalid gate" in result.stdout
    assert "tests run: none" in result.stdout and "No configured gate covers" not in result.stdout


def test_no_checks_applies_without_gates(repo: Path, data_dir: Path) -> None:
    (repo / "docs").mkdir()
    (repo / "docs/note.md").write_text("n\n")
    set_config(repo, verification={"no_checks": ["*.md"]})
    result = manual(repo, "src/pkg/mathx.py", "docs/note.md", data_dir=data_dir)
    assert result.returncode == 0 and "non-Python" not in result.stdout, result.stdout


def test_parallel_edit_hooks_keep_every_record(repo: Path, data_dir: Path) -> None:
    # Parallel tool calls run post_edit hooks of one session at once; each must keep its record.
    names = [f"src/pkg/f{i}.py" for i in range(12)]
    for n in names:
        (repo / n).write_text("x = 1\n")
    env = {"CLAUDE_PLUGIN_DATA": str(data_dir), "PATH": "/usr/bin:/bin"}
    procs = [subprocess.Popen([sys.executable, str(SCRIPTS / "post_edit.py")], stdin=subprocess.PIPE,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True, env=env)
             for _ in names]
    for proc, n in zip(procs, names):
        proc.stdin.write(json.dumps({"session_id": "par", "cwd": str(repo),
                                     "tool_input": {"file_path": str(repo / n)}}))
        proc.stdin.close()
    for proc in procs:
        proc.wait(timeout=60)
    state = json.loads(next((data_dir).rglob("par.json")).read_text())
    assert sorted(state["edited"]) == sorted(names) and len(state["edited_digest"]) == len(names)


def test_state_update_without_flock_support_still_records(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Some NFS/FUSE mounts refuse flock; the edit must still be recorded, just unserialised.
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(data_dir))

    class NoFlock:
        LOCK_EX, LOCK_UN = 2, 8

        @staticmethod
        def flock(fd: int, op: int) -> None:
            raise OSError(37, "No locks available")

    monkeypatch.setattr(kit_common, "fcntl", NoFlock)
    kit_common.update_state("nolock", lambda st: st.setdefault("edited", []).append("a.py"))
    assert kit_common.load_state("nolock")["edited"] == ["a.py"]


def test_prune_keeps_a_live_sessions_lock(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Opening a lock for append never moves its mtime; update_state must, or a session older
    # than the TTL would lose its lock while live and its writers would stop serialising.
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(data_dir))
    state = data_dir / "state"
    old = kit_common.time.time() - kit_common.STATE_TTL_S - 60
    kit_common.update_state("live", lambda st: st.update(edited=[]))
    os.utime(state / "live.lock", (old, old))
    kit_common.update_state("live", lambda st: st["edited"].append("a.py"))
    stale = state / "gone.lock"
    stale.write_text("")
    os.utime(stale, (old, old))
    kit_common.prune_old_state()
    assert (state / "live.lock").exists() and (state / "live.json").exists()
    assert not stale.exists()


@pytest.mark.parametrize("by_fd", [True, False])
def test_a_failed_lock_touch_keeps_the_update(
        data_dir: Path, monkeypatch: pytest.MonkeyPatch, by_fd: bool) -> None:
    # The touch is only a TTL hint for pruning; it must never cost the edit record.
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(data_dir))
    touched = []

    def utime(target: object, *a: object, **k: object) -> None:
        touched.append(target)
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(kit_common.os, "supports_fd", {utime} if by_fd else set())
    monkeypatch.setattr(kit_common.os, "utime", utime)
    kit_common.update_state("touch", lambda st: st.setdefault("edited", []).append("a.py"))
    assert kit_common.load_state("touch")["edited"] == ["a.py"]
    assert len(touched) == 1 and isinstance(touched[0], int) is by_fd


@pytest.mark.parametrize("retry", [False, True])
def test_stop_retries_once_when_a_failure_cannot_be_recorded(
        repo: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch, retry: bool) -> None:
    # Without a saved failed_digest the retry guard never matches. Claude still gets the first
    # chance to fix it; on the retry Stop lets it stop instead of blocking every turn.
    import stop_verify
    monkeypatch.setattr(stop_verify, "_record_verdict", lambda *a, **k: False)
    monkeypatch.setattr(stop_verify, "verify", lambda *a, **k: (["tests failed"], [], ["a gap"]))
    monkeypatch.setattr(stop_verify, "changed_since", lambda *a, **k: ["src/pkg/mathx.py"])
    monkeypatch.setattr(stop_verify, "load_config", lambda *a, **k: {"lint": {"enabled": True}})
    monkeypatch.setattr(stop_verify, "read_input",
                        lambda: {"session_id": "nostate", "cwd": str(repo), "stop_hook_active": retry})
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(data_dir))
    out = []
    monkeypatch.setattr(stop_verify, "emit", out.append)
    assert stop_verify.hook_main() == 0 and len(out) == 1, out
    text = out[0]["systemMessage"] if retry else out[0]["hookSpecificOutput"]["additionalContext"]
    assert "tests failed" in text and "a gap" in text
    assert ("FAILED" in text) is retry


def test_stop_verdict_keeps_edits_recorded_during_verify(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Stop reads state, verifies for minutes, then records a verdict. Edits post_edit recorded
    # meanwhile must survive, and only the verdict keys may change.
    import stop_verify
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(data_dir))
    kit_common.save_state("v", {"edited": ["a.py"], "failed_digest": "old"})
    stale = kit_common.load_state("v")
    kit_common.update_state("v", lambda st: st["edited"].append("b.py"))
    stop_verify._record_verdict("v", passed_digest="d1", failed_digest=None)
    state = kit_common.load_state("v")
    assert state["edited"] == ["a.py", "b.py"] and state["passed_digest"] == "d1"
    assert "failed_digest" not in state and stale["edited"] == ["a.py"]


def test_invalid_no_checks_is_rejected(repo: Path, data_dir: Path) -> None:
    set_config(repo, verification={"enabled": True, "gates": [GATE_OK], "no_checks": "docs/**"})
    result = manual(repo, "src/pkg/mathx.py", data_dir=data_dir)
    assert result.returncode == 2 and "no_checks must be a list of strings" in result.stdout


def test_stop_is_silent_for_a_gated_repo_change_its_tests_verify(repo: Path, data_dir: Path) -> None:
    set_config(repo, verification={"enabled": True, "gates": [GATE_OK], "no_checks": ["*.md"]})
    base = {"session_id": "gated-related", "cwd": str(repo)}
    hook("session_start.py", base, data_dir)
    (repo / "src/pkg/mathx.py").write_text("def double(x):\n    return x + x\n")
    (repo / "NOTES.md").write_text("why\n")
    assert hook("stop_verify.py", base, data_dir) == {}


def test_gate_command_does_not_use_shell_and_cannot_escape_repo(repo: Path, data_dir: Path) -> None:
    (repo / "src/app.ts").write_text("x\n")
    set_config(repo, lint={"enabled": False}, tests={"enabled": False}, verification={
        "enabled": True, "gates": [{"name": "bad", "paths": ["src/*.ts"],
                                    "argv": ["this-does-not-exist;touch", "marker"]}]})
    result = manual(repo, "src/app.ts", data_dir=data_dir)
    assert result.returncode == 1 and not (repo / "marker").exists()
    set_config(repo, verification={"gates": [{"name": "bad", "paths": ["src/*.ts"],
                                               "cwd": "..", "argv": [sys.executable, "-V"]}]})
    result = manual(repo, "src/app.ts", data_dir=data_dir)
    assert result.returncode == 1 and "unavailable" in result.stdout


def test_non_python_change_without_gate_is_unverified(repo: Path, data_dir: Path) -> None:
    (repo / "src/app.ts").write_text("x\n")
    result = manual(repo, "src/app.ts", data_dir=data_dir)
    assert result.returncode == 1 and "non-Python" in result.stdout


def test_mixed_language_change_without_gate_is_unverified(repo: Path, data_dir: Path) -> None:
    (repo / "src/app.ts").write_text("x\n")
    result = manual(repo, "src/pkg/mathx.py", "src/app.ts", data_dir=data_dir)
    assert result.returncode == 1 and "src/app.ts" in result.stdout


# ── which tests a source change runs ───────────────────────────────────────────
def test_a_test_reached_through_a_relative_import_runs(repo: Path) -> None:
    (repo / "src/pkg/consumer.py").write_text("from .mathx import double\n")
    (repo / "tests/test_consumer_only.py").write_text("from pkg.consumer import double\n")
    tests, _, _ = find_tests(repo, ["src/pkg/mathx.py"], {"test_dirs": ["tests"], "import_roots": ["src"]})
    assert "tests/test_consumer_only.py" in tests
    assert "tests/test_unrelated.py" not in tests, "selection fell back to the whole suite"


def test_importers_of_a_deleted_module_still_run(repo: Path) -> None:
    (repo / "src/pkg/consumer.py").write_text("from pkg.gone import thing\n")
    (repo / "tests/test_consumer_only.py").write_text("import pkg.consumer\n")
    tests, _, _ = find_tests(repo, ["src/pkg/gone.py"], {"test_dirs": ["tests"], "import_roots": ["src"]})
    assert tests == ["tests/test_consumer_only.py"]


def test_scope_all_runs_every_test_file_uncapped(repo: Path) -> None:
    cfg = {"test_dirs": ["tests"], "import_roots": ["src"], "scope": "all", "max_files": 1}
    tests, capped, _ = find_tests(repo, ["src/pkg/other.py"], cfg)
    assert set(tests) == {"tests/test_mathx.py", "tests/test_unrelated.py", "tests/test_uses_import.py"}
    assert capped == []


def test_a_clean_diff_is_not_re_run_on_the_next_turn(repo: Path, data_dir: Path, tmp_path: Path) -> None:
    runs = tmp_path / "runs.txt"
    (repo / "tests/test_counted.py").write_text(
        f"import pkg.mathx\ndef test_counted():\n    open({str(runs)!r}, 'a').write('x')\n"
    )
    commit(repo, "counted test")
    base = {"session_id": "digest-check", "cwd": str(repo)}
    hook("session_start.py", base, data_dir)
    (repo / "src/pkg/mathx.py").write_text("def double(x):\n    return x + x\n")
    assert hook("stop_verify.py", base, data_dir) == {}
    assert hook("stop_verify.py", base, data_dir) == {}
    assert runs.read_text() == "x", "the hook re-ran the tests on a diff it had already passed"


def test_a_gap_is_reported_once_and_does_not_block(repo: Path, data_dir: Path) -> None:
    base = {"session_id": "gap-check", "cwd": str(repo)}
    hook("session_start.py", base, data_dir)
    (repo / "README.md").write_text("docs\n")
    first = hook("stop_verify.py", base, data_dir)
    assert "hookSpecificOutput" not in first, "a docs edit Claude cannot fix blocked the stop"
    assert "README.md" in first["systemMessage"]
    assert hook("stop_verify.py", base, data_dir) == {}, "the same gap was reported every turn"


def test_tests_using_a_conftest_fixture_run_when_its_import_changes(repo: Path) -> None:
    (repo / "tests/conftest.py").write_text(
        "import pytest\nfrom pkg.other import other\n\n@pytest.fixture\ndef one():\n    return other()\n"
    )
    (repo / "tests/test_fixture_only.py").write_text("def test_one(one):\n    assert one == 1\n")
    cfg = {"test_dirs": ["tests"], "import_roots": ["src"], "max_files": 50}
    tests, _, _ = find_tests(repo, ["src/pkg/other.py"], cfg)
    assert "tests/test_fixture_only.py" in tests


def test_an_untested_module_is_a_gap_even_beside_a_tested_one(repo: Path, data_dir: Path) -> None:
    (repo / "src/pkg/lonely.py").write_text("def lonely():\n    return 0\n")
    set_config(repo, lint={"enabled": False})
    result = manual(repo, "src/pkg/mathx.py", "src/pkg/lonely.py", data_dir=data_dir)
    assert result.returncode == 1 and "no test reaches src/pkg/lonely.py" in result.stdout
    assert "INCOMPLETE" in result.stdout


def test_a_gap_notice_never_says_the_checks_passed(repo: Path, data_dir: Path) -> None:
    base = {"session_id": "gap-wording", "cwd": str(repo)}
    hook("session_start.py", base, data_dir)
    (repo / "README.md").write_text("docs\n")
    message = hook("stop_verify.py", base, data_dir)["systemMessage"]
    assert "INCOMPLETE" in message and "passed" not in message


# ── learning report (SessionEnd) ───────────────────────────────────────────────
FAKE_GH = """#!/bin/sh
echo "$@" >> "$GH_LOG"
case "$*" in
  "api repos/o/kit/issues?"*) [ -n "$GH_LIST_FAIL" ] && { echo "$GH_LIST_FAIL" >&2; exit 1; }
    cat "$GH_ISSUES" 2>/dev/null || echo "[]";;
  "api repos/o/kit/issues --input "*) cat "$4" >> "$GH_LOG"; echo >> "$GH_LOG"
    [ -n "$GH_FAIL" ] && { echo "$GH_FAIL" >&2; exit 1; }; echo "https://github.com/o/kit/issues/7";;
  "api -X PATCH repos/o/kit/issues/"*) [ -n "$GH_REOPEN_FAIL" ] && exit 1; echo ok;;
  "api repos/o/kit/issues/"*"/comments "*) echo ok;;
  "api repos/o/kit/labels/kit-learning")
    [ -n "$GH_LABEL_EXISTS" ] && { echo '{"name":"kit-learning"}'; exit 0; }
    echo "HTTP 404" >&2; exit 1;;
  "api repos/o/kit/labels "*)
    [ -n "$GH_LABEL_RACE" ] && { echo '{"errors":[{"code":"already_exists"}]}'; exit 1; }
    [ -n "$GH_LABEL_FAIL" ] && { echo "HTTP 403" >&2; exit 1; }
    echo '{"name":"kit-learning"}';;
esac
"""
ORIGIN = "https://github.com/acme/pipeline.git"


def end_session(repo: Path, data_dir: Path, tmp_path: Path, sid: str, **env: str) -> list:
    """Run the SessionEnd hook with a fake `gh`; return the backlog records."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "gh").write_text(FAKE_GH)
    (bin_dir / "gh").chmod(0o755)
    learning = tmp_path / "learning.jsonl"
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "learning_report.py")],
        input=json.dumps({"session_id": sid, "cwd": str(repo)}), capture_output=True, text=True,
        timeout=120, env={"CLAUDE_PLUGIN_DATA": str(data_dir), "PATH": f"{bin_dir}:/usr/bin:/bin",
                          "KIT_LEARNING_FILE": str(learning), "GH_LOG": str(tmp_path / "gh.log"), **env},
    )
    assert proc.returncode == 0 and proc.stdout == "", proc.stderr
    if not learning.exists():
        return []
    return [json.loads(line) for line in learning.read_text().splitlines()]


def opt_in(repo: Path, tmp_path: Path, target: str = "o/kit") -> None:
    """The machine names the kit repo; the repo consents to sharing."""
    sh(repo, "git", "remote", "add", "origin", ORIGIN)
    set_config(repo, learning={"share": True})
    commit(repo, "share")
    (tmp_path / "config.json").write_text(json.dumps({"report_to": target}))


def backlog_module():
    sys.path.insert(0, str(SCRIPTS.parent / "portable" / "kit-deliver" / "scripts"))
    import backlog
    return backlog


def gh_log(tmp_path: Path) -> str:
    log = tmp_path / "gh.log"
    return log.read_text() if log.exists() else ""


def test_session_end_records_what_the_session_added(repo: Path, data_dir: Path, tmp_path: Path) -> None:
    sh(repo, "git", "remote", "add", "origin", "git@github.com:acme/pipeline.git")
    (repo / "requirements-dev.txt").write_text("pytest\n")
    commit(repo, "deps")
    base = {"session_id": "learn-1", "cwd": str(repo)}
    hook("session_start.py", base, data_dir)
    (repo / ".claude/skills/trace-repro").mkdir(parents=True)
    (repo / ".claude/skills/trace-repro/SKILL.md").write_text("---\nname: trace-repro\n---\n")
    (repo / ".claude/agents").mkdir()
    (repo / ".claude/agents/sql-checker.md").write_text("---\nname: sql-checker\n---\n")
    (repo / ".mcp.json").write_text(json.dumps({"mcpServers": {"playwright": {"command": "npx"}}}))
    (repo / "requirements-dev.txt").write_text("pytest\npytest-xdist>=3\n# comment\n-r base.txt\n")
    (repo / "requirements.txt").write_text("polars\n")  # runtime: the project's business
    (repo / "package.json").write_text(json.dumps({"dependencies": {"react": "1"},
                                                   "devDependencies": {"@playwright/test": "1"}}))
    events = end_session(repo, data_dir, tmp_path, "learn-1")
    got = {(e["type"], e["name"], e["repo"]) for e in events if e["kind"] == "component"}
    assert got == {("skill", "trace-repro", "acme/pipeline"), ("agent", "sql-checker", "acme/pipeline"),
                   ("mcp-server", "playwright", "acme/pipeline"), ("dependency", "pytest-xdist", "acme/pipeline"),
                   ("dependency", "@playwright/test", "acme/pipeline")}
    assert not gh_log(tmp_path), "sent without the repo opting in"
    assert len(end_session(repo, data_dir, tmp_path, "learn-1")) == len(events), "recorded twice"


def test_only_what_the_session_added_is_recorded(repo: Path, data_dir: Path, tmp_path: Path) -> None:
    # Already dirty or untracked at SessionStart is not this session's work.
    (repo / "requirements-dev.txt").write_text("pytest\nhttpx\n")
    (repo / ".claude/skills/old").mkdir(parents=True)
    (repo / ".claude/skills/old/SKILL.md").write_text("x")
    (repo / "pyproject.toml").write_text("[project]\nname = 'p'\n[dependency-groups]\ndev = [\"mypy\"]\n")
    hook("session_start.py", {"session_id": "learn-6", "cwd": str(repo)}, data_dir)
    (repo / "requirements-dev.txt").write_text("pytest\nhttpx\nhypothesis\n")
    (repo / ".claude/skills/old/SKILL.md").write_text("edited")
    (repo / "pyproject.toml").write_text(
        "[project]\nname = 'p'\nkeywords = [\n  \"ml\",\n]\ndependencies = [\"uvicorn[standard]>=0.2\"]\n"
        "[project.optional-dependencies]\ndev = [\n  \"ruff\",  # lint\n  \"nox[uv]>=1\",\n]\n"
        "postgres = [\"psycopg\"]\n"
        "[dependency-groups]\ndev = [\"mypy\", {include-group = \"lint\"}]\n"
        "[tool.pytest.ini_options]\ntestpaths = [\n  \"tests\",\n]\n"
        "[tool.poetry.dependencies]\npython = \"^3.9\"\npolars = \"^1\"\n"
        "[tool.poetry.group.dev.dependencies]\nblack = \"^24\"\n"
        "[tool.poetry.group.ml.dependencies]\ntorch = \"^2\"\n")
    events = end_session(repo, data_dir, tmp_path, "learn-6")
    assert {e["name"] for e in events} == {"hypothesis", "ruff", "nox", "black"}


def test_opted_in_repo_sends_new_entries_and_comments_on_repeats(
        repo: Path, data_dir: Path, tmp_path: Path) -> None:
    opt_in(repo, tmp_path)
    hook("session_start.py", {"session_id": "learn-2", "cwd": str(repo)}, data_dir)
    (repo / ".claude/skills/trace-repro").mkdir(parents=True)
    (repo / ".claude/skills/trace-repro/SKILL.md").write_text("x")
    events = end_session(repo, data_dir, tmp_path, "learn-2")
    assert [e["issue"] for e in events if e["kind"] == "synced"] == ["https://github.com/o/kit/issues/7"]
    log = gh_log(tmp_path)
    assert "api repos/o/kit/labels -f name=kit-learning" in log, "a fresh kit repo has no label to file under"
    assert '"title": "[kit-learning] skill: trace-repro"' in log

    # The same skill reported again is a comment that reopens a closed issue; an idea
    # sends its whole text, scrubbed; an entry another repo on this machine recorded stays.
    backlog = backlog_module()
    fp = backlog.fingerprint({"kind": "component", "type": "skill", "name": "Trace-Repro"})
    issues = tmp_path / "issues.json"
    issues.write_text(json.dumps([{"number": 7, "url": "u7", "state": "CLOSED",
                                   "body": f"<!-- kit-learning:{fp} -->"}]))
    learning = tmp_path / "learning.jsonl"
    backlog.add_component(learning, "skill", "trace-repro", "acme/pipeline")
    backlog.add_component(learning, "skill", "private-thing", "acme/secret")
    idea = ("retry flaky HTTP calls in tests " + "with backoff " * 10 + "UNLESS the endpoint is idempotent; "
            "token ghp_abcdefghijklmnopqrstuvwxyz0123 in /home/v/notes.txt")
    backlog.append(learning, {"kind": "idea", "id": "i1", "task": "t", "idea": idea, "evidence": "e",
                              "repo": "acme/pipeline", "created_at": "2026-11-03"})
    backlog.append(learning, {"kind": "idea", "id": "i2", "task": "t", "evidence": "e",
                              "idea": "ghp_abcdefghijklmnopqrstuvwxyz0123 leaked in a fixture",
                              "repo": "acme/pipeline", "created_at": "2026-11-03"})
    (tmp_path / "gh.log").unlink()
    hook("session_start.py", {"session_id": "learn-3", "cwd": str(repo)}, data_dir)
    events = end_session(repo, data_dir, tmp_path, "learn-3", GH_ISSUES=str(issues))
    log = gh_log(tmp_path)
    assert "api -X PATCH repos/o/kit/issues/7 -f state=open" in log
    assert "api repos/o/kit/issues/7/comments" in log
    assert "UNLESS the endpoint is idempotent" in log, "the idea was cut to its title"
    assert "ghp_" not in log and "/home/v" not in log, "a credential or local path left the machine"
    assert "private-thing" not in log, "sent another repo's entry without its consent"
    sent = {e["id"] for e in events if e["kind"] == "synced"}
    assert [e["name"] for e in events if e["kind"] == "component" and e["id"] not in sent] == ["private-thing"]
    status = json.loads((tmp_path / "sync-status.json").read_text())
    assert status["error"] == "" and status["queued"] == 0


def test_the_repo_cannot_choose_where_learning_is_sent(repo: Path, data_dir: Path, tmp_path: Path) -> None:
    # A cloned repo's config names a destination; with no machine target, nothing is sent.
    sh(repo, "git", "remote", "add", "origin", ORIGIN)
    set_config(repo, learning={"share": True, "report_to": "attacker/x"})
    commit(repo, "hostile")
    hook("session_start.py", {"session_id": "learn-7", "cwd": str(repo)}, data_dir)
    (repo / ".claude/agents").mkdir()
    (repo / ".claude/agents/a.md").write_text("x")
    end_session(repo, data_dir, tmp_path, "learn-7")
    assert not gh_log(tmp_path)
    # Without the repo's consent, the machine's target alone sends nothing either.
    set_config(repo, learning={"share": False})
    (tmp_path / "config.json").write_text(json.dumps({"report_to": "o/kit"}))
    end_session(repo, data_dir, tmp_path, "learn-7")
    assert not gh_log(tmp_path)


def test_a_failed_send_stays_queued(repo: Path, data_dir: Path, tmp_path: Path) -> None:
    opt_in(repo, tmp_path)
    hook("session_start.py", {"session_id": "learn-4", "cwd": str(repo)}, data_dir)
    (repo / ".claude/agents").mkdir()
    (repo / ".claude/agents/a.md").write_text("x")
    events = end_session(repo, data_dir, tmp_path, "learn-4", GH_FAIL="1")
    assert [e["kind"] for e in events] == ["component"], "an entry was marked sent when sending failed"
    assert json.loads((tmp_path / "sync-status.json").read_text())["queued"] == 1
    events = end_session(repo, data_dir, tmp_path, "learn-4")
    assert [e["kind"] for e in events] == ["component", "synced"], "the queued entry was not retried"


def test_a_failed_issue_list_creates_nothing(repo: Path, data_dir: Path, tmp_path: Path) -> None:
    opt_in(repo, tmp_path)
    hook("session_start.py", {"session_id": "learn-5", "cwd": str(repo)}, data_dir)
    (repo / ".claude/agents").mkdir()
    (repo / ".claude/agents/a.md").write_text("x")
    events = end_session(repo, data_dir, tmp_path, "learn-5", GH_LIST_FAIL="1")
    assert "--input" not in gh_log(tmp_path), "created without knowing what exists"
    assert [e["kind"] for e in events] == ["component"]


def test_a_running_sync_is_not_joined(repo: Path, data_dir: Path, tmp_path: Path) -> None:
    opt_in(repo, tmp_path)
    (tmp_path / "sync.lock").write_text("")
    hook("session_start.py", {"session_id": "learn-8", "cwd": str(repo)}, data_dir)
    (repo / ".claude/agents").mkdir()
    (repo / ".claude/agents/a.md").write_text("x")
    events = end_session(repo, data_dir, tmp_path, "learn-8")
    assert not gh_log(tmp_path) and [e["kind"] for e in events] == ["component"]


def test_a_failed_reopen_keeps_the_report_queued(repo: Path, data_dir: Path, tmp_path: Path) -> None:
    # A comment on an issue that stays closed is outside `improve`'s inbox for good.
    opt_in(repo, tmp_path)
    backlog = backlog_module()
    fp = backlog.fingerprint({"kind": "component", "type": "agent", "name": "a"})
    issues = tmp_path / "issues.json"
    issues.write_text(json.dumps([{"number": 3, "url": "u3", "state": "CLOSED",
                                   "body": f"<!-- kit-learning:{fp} -->"}]))
    hook("session_start.py", {"session_id": "learn-9", "cwd": str(repo)}, data_dir)
    (repo / ".claude/agents").mkdir()
    (repo / ".claude/agents/a.md").write_text("x")
    events = end_session(repo, data_dir, tmp_path, "learn-9", GH_ISSUES=str(issues), GH_REOPEN_FAIL="1")
    assert "/comments" not in gh_log(tmp_path) and [e["kind"] for e in events] == ["component"]


def test_a_sync_rereads_the_log_once_it_holds_the_lock(tmp_path: Path, monkeypatch) -> None:
    # Another sync sent the entry between this one's first read and taking the lock.
    backlog = backlog_module()
    learning = tmp_path / "learning.jsonl"
    entry = backlog.add_component(learning, "skill", "s", "acme/pipeline")
    real_lock = backlog._lock

    def lock_after_another_sync(path: Path):
        backlog.append(path, {"kind": "synced", "id": entry["id"], "issue": "u1", "created_at": "x"})
        return real_lock(path)

    calls = []
    monkeypatch.setattr(backlog, "_lock", lock_after_another_sync)
    def run(args: list, timeout: float) -> subprocess.CompletedProcess:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "[]", "")

    result = backlog.sync(learning, "o/kit", "acme/pipeline", run=run)
    assert calls == [] and result["sent"] == [] and result["queued"] == 0


def test_backfill_records_what_the_repo_already_had(repo: Path, tmp_path: Path) -> None:
    # Built by sessions before the hook existed: SessionEnd never sees it, backfill does.
    opt_in(repo, tmp_path)
    (repo / ".claude/skills/trace-repro").mkdir(parents=True)
    (repo / ".claude/skills/trace-repro/SKILL.md").write_text("x")
    commit(repo, "old skill")
    learning = tmp_path / "learning.jsonl"
    backlog = backlog_module()
    backlog.append(learning, {"kind": "idea", "id": "old", "task": "t", "idea": "an old idea",
                              "evidence": "e", "status": "proposed", "created_at": "2026-10-01"})
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(FAKE_GH)
    (bin_dir / "gh").chmod(0o755)
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "KIT_LEARNING_FILE": str(learning), "GH_LOG": str(tmp_path / "gh.log")}

    def backfill(*flags: str) -> dict:
        proc = subprocess.run([sys.executable, str(SCRIPTS / "learning_report.py"), "backfill", *flags],
                              cwd=repo, capture_output=True, text=True, timeout=120, env=env)
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)

    out = backfill("--claim-ideas")
    assert out["recorded"] == ["skill:trace-repro"] and out["claimed_ideas"] == 1
    log = gh_log(tmp_path)
    assert "skill: trace-repro" in log and "an old idea" in log
    again = backfill("--claim-ideas")
    assert again["recorded"] == [] and again["claimed_ideas"] == 0, "backfill is not idempotent"
    ideas = [r for r in backlog.read_records(learning).values() if r["kind"] == "idea"]
    assert len(ideas) == 1 and ideas[0]["repo"] == "acme/pipeline" and ideas[0].get("synced_to"), \
        "a claimed idea must be the original entry, sent once, not a second copy"


def test_backfill_writes_nothing_without_a_github_origin(repo: Path, tmp_path: Path) -> None:
    (repo / ".claude/skills/s").mkdir(parents=True)
    (repo / ".claude/skills/s/SKILL.md").write_text("x")
    learning = tmp_path / "learning.jsonl"
    proc = subprocess.run([sys.executable, str(SCRIPTS / "learning_report.py"), "backfill"],
                          cwd=repo, capture_output=True, text=True, timeout=120,
                          env={"PATH": "/usr/bin:/bin", "KIT_LEARNING_FILE": str(learning)})
    assert proc.returncode == 1 and "origin" in json.loads(proc.stdout)["error"]
    assert not learning.exists(), "recorded entries that can never be sent"


@pytest.mark.parametrize("env,sent", [
    ({"GH_LABEL_EXISTS": "1", "GH_LABEL_FAIL": "1"}, True),  # may apply the label, may not create one
    ({"GH_LABEL_RACE": "1"}, True),  # another machine created it between the look and the create
    ({"GH_LABEL_FAIL": "1"}, False),
])
def test_a_label_that_exists_is_fine_and_one_that_cannot_be_made_keeps_entries_queued(
        repo: Path, data_dir: Path, tmp_path: Path, env: dict, sent: bool) -> None:
    """Old distro builds of gh have no `gh label`; a failed label step used to pass silently
    and then fail every issue create with "label not found"."""
    opt_in(repo, tmp_path)
    hook("session_start.py", {"session_id": "label", "cwd": str(repo)}, data_dir)
    (repo / ".claude/skills/trace-repro").mkdir(parents=True)
    (repo / ".claude/skills/trace-repro/SKILL.md").write_text("x")
    events = end_session(repo, data_dir, tmp_path, "label", **env)
    assert any(e["kind"] == "synced" for e in events) is sent
    if not sent:
        assert "--input" not in gh_log(tmp_path)
    if "GH_LABEL_EXISTS" in env:
        assert "api repos/o/kit/labels -f" not in gh_log(tmp_path), "an existing label is not re-created"


def _stand_aside(repo: Path, event: str, monkeypatch: pytest.MonkeyPatch) -> bool:
    import kit_common
    monkeypatch.setenv("GROK_HOOK_EVENT", event)
    return kit_common.stand_aside({"cwd": str(repo)})


def test_plugin_hooks_stand_aside_only_for_what_the_native_grok_hooks_cover(
        repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Grok loads Claude plugins, hooks included. Where install_native.py wrote Grok's own
    hooks, the plugin's copy of an event they handle must not run a second time; an event
    they do not handle, or one an older native copy handles less well, still runs."""
    sys.path.insert(0, str(SCRIPTS))
    import install_native
    assert not _stand_aside(repo, "stop", monkeypatch), "no native install: the plugin is all there is"
    install_native.install(repo, "grok")
    assert _stand_aside(repo, "pre_tool_use", monkeypatch)
    assert _stand_aside(repo, "session_start", monkeypatch)
    assert _stand_aside(repo, "stop", monkeypatch)
    assert not _stand_aside(repo, "post_tool_use", monkeypatch), "no native PostToolUse: lint must still run"
    assert not _stand_aside(repo, "session_end", monkeypatch)
    native = repo / ".grok/skills/kit-deliver/scripts/native_hook.py"
    native.write_text(native.read_text().replace("GROK_STOP_VERIFIES = True", ""))
    assert _stand_aside(repo, "pre_tool_use", monkeypatch)
    assert not _stand_aside(repo, "stop", monkeypatch), "an older native Stop only learned: keep the plugin's"
    assert not _stand_aside(repo, "session_start", monkeypatch), "the plugin's Stop needs its baseline"
    monkeypatch.delenv("GROK_HOOK_EVENT")
    import kit_common
    assert not kit_common.stand_aside({"cwd": str(repo)}), "Claude Code never stands aside"


# ── learning at Stop: cloud layout, context, asks ──────────────────────────────
def learn(tmp_path: Path, data_dir: Path, payload: dict, *args: str, **env: str) -> dict:
    """Run session_start.py (args == ("start",)) or learning_report.py with a fake `gh`, a
    private HOME and the scratch learning log; return the hook's JSON output."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "gh").write_text(FAKE_GH)
    (bin_dir / "gh").chmod(0o755)
    (tmp_path / "home").mkdir(exist_ok=True)
    script = ["session_start.py"] if args == ("start",) else ["learning_report.py", *args]
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / script[0]), *script[1:]], input=json.dumps(payload),
        capture_output=True, text=True, timeout=120,
        env={"CLAUDE_PLUGIN_DATA": str(data_dir), "PATH": f"{bin_dir}:/usr/bin:/bin",
             "HOME": str(tmp_path / "home"), "KIT_LEARNING_FILE": str(tmp_path / "learning.jsonl"),
             "GH_LOG": str(tmp_path / "gh.log"), **env})
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


def records(tmp_path: Path) -> dict:
    return backlog_module().read_records(tmp_path / "learning.jsonl")


def add_skill(root: Path, name: str, description: str = "") -> None:
    (root / ".claude/skills" / name).mkdir(parents=True, exist_ok=True)
    (root / ".claude/skills" / name / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n")


def test_stop_asks_why_and_to_commit_then_sends_the_reason_with_the_report(
        repo: Path, data_dir: Path, tmp_path: Path) -> None:
    """Names alone tell `improve` nothing: what the skill is for, why the session needed it
    and the decision behind it go with the report. A skill left uncommitted is lost."""
    opt_in(repo, tmp_path)
    (repo / "docs/decisions").mkdir(parents=True)
    (repo / "docs/decisions/old.md").write_text("- Task: an older question\n")
    commit(repo, "old decision")
    base = {"session_id": "why-1", "cwd": str(repo)}
    learn(tmp_path, data_dir, base, "start")
    add_skill(repo, "trace-repro", "Reproduce a flaky trace deterministically")
    (repo / "docs/decisions/2026-11-03-flaky-traces.md").write_text(
        "# Flaky traces\n- Task: make trace failures reproducible · Done-check: x\n"
        "- Binding constraint: no network in CI,\n  and the runner has no GPU\n")
    out = learn(tmp_path, data_dir, base, "stop")
    assert out["decision"] == "block"
    (entry,) = [r for r in records(tmp_path).values() if r["kind"] == "component"]
    assert entry["context"]["what"] == "Reproduce a flaky trace deterministically"
    assert "make trace failures reproducible" in entry["context"]["decision"]
    assert "no network in CI, and the runner has no GPU" in entry["context"]["decision"], "lost a wrapped line"
    assert "older question" not in entry["context"]["decision"]
    assert f"why --id {entry['id']}" in out["reason"] and ".claude/skills/trace-repro" in out["reason"]
    assert not gh_log(tmp_path), "sent before Claude could say why"
    backlog = backlog_module()
    assert backlog.sync(tmp_path / "learning.jsonl", "o/kit", "acme/pipeline",
                        run=lambda a, t: pytest.fail("another session sent a held entry"))["queued"] == 0

    backlog_module().add_why(tmp_path / "learning.jsonl", entry["id"], "needed to stop guessing at CI failures")
    assert learn(tmp_path, data_dir, {**base, "stop_hook_active": True}, "stop") == {}
    log = gh_log(tmp_path)
    assert "What it is for: Reproduce a flaky trace" in log and "needed to stop guessing" in log
    assert "Decision log: 2026-11-03-flaky-traces" in log
    assert learn(tmp_path, data_dir, base, "stop") == {}, "asked twice about the same skill"


def test_a_cloud_session_above_its_checkouts_still_learns(data_dir: Path, tmp_path: Path) -> None:
    """A claude.ai cloud session starts in the home folder, with each repo cloned beneath it."""
    top = tmp_path / "work"
    child = top / "proj"
    child.mkdir(parents=True)
    sh(child, "git", "init", "-q")
    sh(child, "git", "remote", "add", "origin", ORIGIN)
    (child / "README.md").write_text("x")
    commit(child, "init")
    base = {"session_id": "cloud-1", "cwd": str(top)}
    learn(tmp_path, data_dir, base, "start")
    add_skill(child, "site-audit", "Audit a site")
    commit(child, "skill")
    out = learn(tmp_path, data_dir, base, "stop")
    assert [(r["name"], r["repo"]) for r in records(tmp_path).values()] == [("site-audit", "acme/pipeline")]
    assert "not committed" not in out["reason"], "a committed skill was reported as uncommitted"


def test_a_skill_saved_only_for_the_user_is_moved_into_the_repo(
        repo: Path, data_dir: Path, tmp_path: Path) -> None:
    base = {"session_id": "user-1", "cwd": str(repo)}
    learn(tmp_path, data_dir, base, "start")
    add_skill(tmp_path / "home", "scratch-helper")
    out = learn(tmp_path, data_dir, base, "stop")
    assert "`scratch-helper` was saved under ~/.claude" in out["reason"] and "repo (repo)" in out["reason"]
    assert learn(tmp_path, data_dir, base, "stop") == {}


def test_in_a_project_whose_only_repo_is_the_kit_a_reusable_skill_becomes_a_kit_pr(
        repo: Path, data_dir: Path, tmp_path: Path) -> None:
    opt_in(repo, tmp_path, target="acme/pipeline")  # this checkout is the kit itself
    base = {"session_id": "user-2", "cwd": str(repo)}
    learn(tmp_path, data_dir, base, "start")
    add_skill(tmp_path / "home", "site-audit")
    out = learn(tmp_path, data_dir, base, "stop")
    assert "no repo of its own" in out["reason"] and "pull request" in out["reason"]


@pytest.mark.parametrize("remote,asked,continuing", [("true", True, False), ("", False, False),
                                                     ("true", True, True)])
def test_a_cloud_session_without_access_to_the_kit_repo_is_told_how_to_get_it(
        repo: Path, data_dir: Path, tmp_path: Path, remote: str, asked: bool, continuing: bool) -> None:
    """Also on a continuation: a new skill's ask blocks first, so sending happens on one."""
    opt_in(repo, tmp_path)
    base = {"session_id": f"access-{remote}", "cwd": str(repo), "stop_hook_active": continuing}
    learn(tmp_path, data_dir, base, "start")
    (repo / "requirements-dev.txt").write_text("pytest\n")  # a dependency: nothing to ask
    # What gh prints in a claude.ai cloud session for a repo not attached to it.
    env = {"GH_LIST_FAIL": "gh: GitHub access to this repository is not enabled for this session. (HTTP 403)",
           "CLAUDE_CODE_REMOTE": remote}
    out = learn(tmp_path, data_dir, base, "stop", **env)
    assert ("add_repo" in out.get("reason", "")) is asked
    assert learn(tmp_path, data_dir, base, "stop", **env) == {}, "asked about access twice"


def test_why_cannot_change_a_report_already_sent(tmp_path: Path) -> None:
    backlog = backlog_module()
    learning = tmp_path / "learning.jsonl"
    entry = backlog.add_component(learning, "skill", "s", "acme/pipeline", {"what": "ghp_" + "a" * 30})
    assert entry["context"]["what"] == "[redacted]", "context leaves the machine: it is scrubbed"
    backlog.append(learning, {"kind": "synced", "id": entry["id"], "issue": "u", "created_at": "x"})
    with pytest.raises(ValueError):
        backlog.add_why(learning, entry["id"], "too late")


def test_issue_listing_reads_paginated_json_lines() -> None:
    backlog = backlog_module()
    assert backlog._issues('{"number": 1}\n{"number": 2}\n') == [{"number": 1}, {"number": 2}]
    assert backlog._issues('[{"number": 3}]') == [{"number": 3}]


def test_a_reason_never_given_releases_the_report_on_the_next_stop(
        repo: Path, data_dir: Path, tmp_path: Path) -> None:
    opt_in(repo, tmp_path)
    base = {"session_id": "release-1", "cwd": str(repo)}
    learn(tmp_path, data_dir, base, "start")
    add_skill(repo, "trace-repro", "x")
    assert learn(tmp_path, data_dir, base, "stop")["decision"] == "block"
    learn(tmp_path, data_dir, {**base, "stop_hook_active": True}, "stop")
    assert '"title": "[kit-learning] skill: trace-repro"' in gh_log(tmp_path), "held for good"


def test_a_skill_moved_in_on_a_continuation_is_still_asked_about(
        repo: Path, data_dir: Path, tmp_path: Path) -> None:
    opt_in(repo, tmp_path)
    base = {"session_id": "move-1", "cwd": str(repo)}
    learn(tmp_path, data_dir, base, "start")
    add_skill(tmp_path / "home", "helper")
    assert "saved under ~/.claude" in learn(tmp_path, data_dir, base, "stop")["reason"]
    add_skill(repo, "helper", "Help")  # Claude moved it, answering the block
    out = learn(tmp_path, data_dir, {**base, "stop_hook_active": True}, "stop")
    assert "why --id" in out["reason"] and not gh_log(tmp_path)


def test_context_is_scrubbed_whole_before_it_is_cut(tmp_path: Path) -> None:
    """Cut first, a token straddling the limit loses a character and the scrubber misses it."""
    backlog = backlog_module()
    learning = tmp_path / "learning.jsonl"
    token = "f" * 40
    entry = backlog.add_component(learning, "skill", "s", "acme/pipeline",
                                  {"what": "x" * (backlog.CONTEXT_LIMIT - 10) + token})
    assert "fffff" not in entry["context"]["what"]
    backlog.add_why(learning, entry["id"], "y" * (backlog.CONTEXT_LIMIT - 10) + " " + token)
    assert "fffff" not in backlog._occurrence(backlog.read_records(learning)[entry["id"]])


def test_a_codex_agent_reports_its_toml_description(repo: Path) -> None:
    sys.path.insert(0, str(SCRIPTS))
    import learning_report
    (repo / ".codex/agents").mkdir(parents=True)
    (repo / ".codex/agents/sql_checker.toml").write_text('name = "sql_checker"\ndescription = "Check SQL"\n')
    assert learning_report.describe(repo, "agent:sql_checker") == "Check SQL"


@pytest.mark.parametrize("frontmatter", [
    "description: >\n  Audit a site\n  for broken links\n",
    "description: |-\n  Audit a site\n  for broken links\n",
    "description: 'Audit a site for broken links'\n",
])
def test_a_yaml_block_description_is_read(repo: Path, frontmatter: str) -> None:
    sys.path.insert(0, str(SCRIPTS))
    import learning_report
    (repo / ".claude/skills/audit").mkdir(parents=True)
    (repo / ".claude/skills/audit/SKILL.md").write_text(f"---\nname: audit\n{frontmatter}---\n")
    assert learning_report.describe(repo, "skill:audit") == "Audit a site for broken links"


def test_session_end_releases_what_the_last_stop_held(repo: Path, data_dir: Path, tmp_path: Path) -> None:
    """The session can end right after the ask: a hold nobody releases loses the report."""
    opt_in(repo, tmp_path)
    base = {"session_id": "end-1", "cwd": str(repo)}
    learn(tmp_path, data_dir, base, "start")
    add_skill(repo, "trace-repro", "x")
    assert learn(tmp_path, data_dir, base, "stop")["decision"] == "block"
    learn(tmp_path, data_dir, base)
    assert '"title": "[kit-learning] skill: trace-repro"' in gh_log(tmp_path)


# ── review gate ────────────────────────────────────────────────────────────────
from review_gate import bash_actions  # noqa: E402


def _gate(repo: Path, data_dir: Path, event: str, **fields) -> dict:
    return hook("review_gate.py", {"hook_event_name": event, "cwd": str(repo), **fields}, data_dir)


def _review(repo: Path, data_dir: Path, agent: str = "a1", agent_type: str = "kit:reviewer",
            edit_during: bool = False, message: str = "Looks correct.\nReview complete: no defects") -> None:
    _gate(repo, data_dir, "SubagentStart", agent_id=agent, agent_type=agent_type)
    if edit_during:
        (repo / "src" / "pkg" / "other.py").write_text("def other():\n    return 3\n")
    _gate(repo, data_dir, "SubagentStop", agent_id=agent, agent_type=agent_type,
          last_assistant_message=message)


def _open_pr(repo: Path, data_dir: Path, head: str = "") -> dict:
    return _gate(repo, data_dir, "PreToolUse", tool_name="mcp__github__create_pull_request",
                 tool_input={"owner": "o", "repo": "r", "head": head, "base": "main"})


def _denied(out: dict) -> bool:
    return out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def _work(repo: Path, message: str = "work") -> None:
    (repo / "src" / "pkg" / "mathx.py").write_text("def double(x):\n    return x + x\n")
    commit(repo, message)


def test_a_pr_from_an_unreviewed_tree_is_denied_and_a_reviewed_one_goes_through(
        repo: Path, data_dir: Path) -> None:
    set_config(repo, review={"gate": "deny"})
    _work(repo)
    out = _open_pr(repo, data_dir)
    assert _denied(out) and "kit:reviewer" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert _denied(_open_pr(repo, data_dir)), "deny mode keeps denying"
    _review(repo, data_dir)
    assert _open_pr(repo, data_dir) == {}


def test_a_review_before_the_commit_covers_the_commit(repo: Path, data_dir: Path) -> None:
    set_config(repo, review={"gate": "deny"})
    commit(repo, "config")
    (repo / "src" / "pkg" / "new.py").write_text("X = 1\n")  # untracked, then committed
    _review(repo, data_dir)
    commit(repo, "work")
    assert _open_pr(repo, data_dir) == {}


def test_a_fix_after_the_review_needs_its_own_review(repo: Path, data_dir: Path) -> None:
    set_config(repo, review={"gate": "deny"})
    _work(repo)
    _review(repo, data_dir)
    (repo / "src" / "pkg" / "mathx.py").write_text("def double(x):\n    return 2 * x\n")
    commit(repo, "fix")
    assert _denied(_open_pr(repo, data_dir))


@pytest.mark.parametrize("case", ["edited-while-reading", "other-agent", "no-answer", "stopped-early", "no-start"])
def test_only_a_finished_reviewer_on_an_unchanged_tree_stamps_it(
        repo: Path, data_dir: Path, case: str) -> None:
    set_config(repo, review={"gate": "deny"})
    _work(repo)
    if case == "edited-while-reading":
        _review(repo, data_dir, edit_during=True)
        commit(repo, "edit")
    elif case == "other-agent":
        _review(repo, data_dir, agent_type="Explore")
    elif case == "no-answer":
        _review(repo, data_dir, message="  ")
    elif case == "stopped-early":
        _review(repo, data_dir, message="You've hit your usage limit.")
    else:
        _gate(repo, data_dir, "SubagentStop", agent_id="zz", agent_type="kit:reviewer",
              last_assistant_message="fine")
    assert _denied(_open_pr(repo, data_dir))


def test_once_mode_reminds_once_per_tree_then_lets_the_retry_through(repo: Path, data_dir: Path) -> None:
    _work(repo)  # default mode is "once"
    first = _open_pr(repo, data_dir)
    assert _denied(first) and "one reminder" in first["hookSpecificOutput"]["permissionDecisionReason"]
    assert _open_pr(repo, data_dir) == {}
    # a new tree gets its own reminder
    (repo / "src" / "pkg" / "other.py").write_text("def other():\n    return 2\n")
    commit(repo, "more")
    assert _denied(_open_pr(repo, data_dir))


def test_off_mode_never_gates_and_never_stamps(repo: Path, data_dir: Path) -> None:
    set_config(repo, review={"gate": "off"})
    _work(repo)
    _review(repo, data_dir)
    assert _open_pr(repo, data_dir) == {}
    common = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=repo, capture_output=True,
                            text=True).stdout.strip()
    assert not (repo / common / "kit-review.json").exists()


def test_pushes_are_gated_only_on_a_branch_that_has_a_pr(repo: Path, data_dir: Path) -> None:
    set_config(repo, review={"gate": "deny"})
    sh(repo, "git", "checkout", "-qb", "feature")
    _work(repo)
    push = {"tool_name": "Bash", "tool_input": {"command": "git push -u origin feature"}}
    assert _gate(repo, data_dir, "PreToolUse", **push) == {}, "no PR yet: a push is work in progress"
    _review(repo, data_dir)
    assert _open_pr(repo, data_dir, head="feature") == {}
    (repo / "src" / "pkg" / "other.py").write_text("def other():\n    return 5\n")
    commit(repo, "fix after review")
    push["tool_input"]["command"] = "git push"
    out = _gate(repo, data_dir, "PreToolUse", **push)
    assert _denied(out) and "push" in out["hookSpecificOutput"]["permissionDecisionReason"]
    _review(repo, data_dir, agent="a2")
    assert _gate(repo, data_dir, "PreToolUse", **push) == {}


def test_gh_pr_create_is_gated_like_the_mcp_tool(repo: Path, data_dir: Path) -> None:
    set_config(repo, review={"gate": "deny"})
    _work(repo)
    out = _gate(repo, data_dir, "PreToolUse", tool_name="Bash",
                tool_input={"command": "git status && GH_PAGER= gh pr create --fill"})
    assert _denied(out)


@pytest.mark.parametrize("command,expected", [
    ("gh pr create --fill", [("pr", None, None, None, None)]),
    ("gh -R o/r pr create -H feat", [("pr", None, "feat", "feat", "o/r")]),
    ("gh pr view 3", []),
    ("git -C sub push origin x", [("push", "sub", "x", "x", None)]),
    ("timeout 60 git -c a=b push", [("push", None, None, None, None)]),
    ("git log --grep push", []),
    ("echo 'git push'", []),
    ("git status\ngit push", [("push", None, None, None, None)]),
    ("gh pr create -Hfeat", [("pr", None, "feat", "feat", None)]),
    ("gh pr create --head me:feat", [("pr", None, "feat", "feat", None)]),
    ("bash -c 'gh pr create --fill'", [("pr", None, None, None, None)]),
    ("(git push)", [("push", None, None, None, None)]),
    ("{ git push; }", [("push", None, None, None, None)]),
    ("if true; then git push; fi", [("push", None, None, None, None)]),
    ("! git push", [("push", None, None, None, None)]),
    ("sudo git push", [("push", None, None, None, None)]),
    ("env -u X git push", [("push", None, None, None, None)]),
    ("timeout -s KILL 60 git push", [("push", None, None, None, None)]),
    ("git push origin feature", [("push", None, "feature", "feature", None)]),
    ("git push -u origin +HEAD:refs/heads/x", [("push", None, None, "x", None)]),
    ("git push origin a b:c", [("push", None, "a", "a", None), ("push", None, "b", "c", None)]),
    ("cd sub && git push", [("push", "sub", None, None, None)]),
    ("cd /abs && git -C inner push", [("push", "/abs/inner", None, None, None)]),
    ("echo \"it's && git push", [("push", None, None, None, None)]),
    ("git add . # note\ngit push", [("push", None, None, None, None)]),
    ("echo '# not a comment' && git push", [("push", None, None, None, None)]),
    ("sudo -n git push", [("push", None, None, None, None)]),
    ("nice -n 5 git push", [("push", None, None, None, None)]),
    ("bash -lc 'git push'", [("push", None, None, None, None)]),
    ("eval 'git push'", [("push", None, None, None, None)]),
    ("x=$(git push)", [("push", None, None, None, None)]),
    ('echo "$(gh pr create --fill)"', [("pr", None, None, None, None)]),
    ("git push origin :feat", []),
    ("git push --tags", []),
    ("git push origin --delete feat", []),
    ("git push origin HEAD:feat", [("push", None, None, "feat", None)]),
    ("sudo -u root git push origin feat", [("push", None, "feat", "feat", None)]),
    ("xargs -I {} git push", [("push", None, None, None, None)]),
    ("git push --tags origin $(git branch --show-current)", [("push", None, None, None, None)]),
    ("git push origin feat --tags", [("push", None, "feat", "feat", None)]),
    ("git push origin $(git branch --show-current)", [("push", None, None, None, None)]),
    ('git push origin "$(git branch --show-current)"', [("push", None, None, None, None)]),
])
def test_bash_actions_finds_pr_creation_and_pushes(command: str, expected: list) -> None:
    assert bash_actions(command) == expected


def test_the_gate_finds_the_repo_beneath_a_cloud_session(repo: Path, data_dir: Path) -> None:
    set_config(repo, review={"gate": "deny"})
    sh(repo, "git", "remote", "add", "origin", "https://github.com/o/r.git")
    _work(repo)
    out = hook("review_gate.py", {"hook_event_name": "PreToolUse", "cwd": str(repo.parent),
                                  "tool_name": "mcp__github__create_pull_request",
                                  "tool_input": {"owner": "O", "repo": "R", "head": ""}}, data_dir)
    assert _denied(out)


def test_the_gate_fails_open_outside_a_repo(tmp_path: Path, data_dir: Path) -> None:
    empty = tmp_path / "nowhere"
    empty.mkdir()
    assert _open_pr(empty, data_dir) == {}
    assert hook("review_gate.py", {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                                   "tool_input": {"command": None}}, data_dir) == {}


def test_an_untracked_file_at_review_time_does_not_block_the_reviewed_commit(
        repo: Path, data_dir: Path) -> None:
    set_config(repo, review={"gate": "deny"})
    _work(repo)
    (repo / "notes.txt").write_text("scratch\n")  # never committed
    _review(repo, data_dir)
    assert _open_pr(repo, data_dir) == {}


def test_a_verdict_quoted_mid_message_is_not_a_finished_review(repo: Path, data_dir: Path) -> None:
    set_config(repo, review={"gate": "deny"})
    _work(repo)
    _review(repo, data_dir, message="It should end with `Review complete: N findings`.\nI stopped here.")
    assert _denied(_open_pr(repo, data_dir))


def test_a_push_of_another_branch_is_judged_by_that_branch(repo: Path, data_dir: Path) -> None:
    set_config(repo, review={"gate": "deny"})
    sh(repo, "git", "checkout", "-qb", "feature")
    _work(repo)
    _review(repo, data_dir)
    assert _open_pr(repo, data_dir, head="feature") == {}
    (repo / "src" / "pkg" / "other.py").write_text("def other():\n    return 9\n")
    commit(repo, "unreviewed fix")
    sh(repo, "git", "checkout", "-q", "-")
    push = {"tool_name": "Bash", "tool_input": {"command": "git push origin feature"}}
    assert _denied(_gate(repo, data_dir, "PreToolUse", **push)), "checked out elsewhere, feature is still gated"


def test_a_pr_for_another_repo_is_not_judged_by_this_checkout(repo: Path, data_dir: Path) -> None:
    set_config(repo, review={"gate": "deny"})
    sh(repo, "git", "remote", "add", "origin", "https://github.com/o/r.git")
    _work(repo)
    out = _gate(repo, data_dir, "PreToolUse", tool_name="mcp__github__create_pull_request",
                tool_input={"owner": "o", "repo": "elsewhere", "head": "x"})
    assert out == {}


def test_a_stale_lock_does_not_stop_the_stamp(repo: Path, data_dir: Path) -> None:
    set_config(repo, review={"gate": "deny"})
    _work(repo)
    lock = repo / ".git" / "kit-review.json.lock"
    lock.write_text("")
    os.utime(lock, (1, 1))
    _review(repo, data_dir)
    assert _open_pr(repo, data_dir) == {}
    assert not lock.exists()


def test_a_pr_from_a_fork_to_its_upstream_is_judged_by_the_fork(repo: Path, data_dir: Path) -> None:
    set_config(repo, review={"gate": "deny"})
    sh(repo, "git", "remote", "add", "origin", "https://github.com/me/r.git")
    sh(repo, "git", "remote", "add", "upstream", "git@github.com:o/r.git")
    _work(repo)
    assert _denied(_open_pr(repo, data_dir)), "owner/repo names the upstream, not origin"


def test_two_checkouts_of_other_repos_leave_the_pr_unjudged(tmp_path: Path, repo: Path,
                                                             data_dir: Path) -> None:
    set_config(repo, review={"gate": "deny"})
    sh(repo, "git", "remote", "add", "origin", "https://github.com/o/r.git")
    _work(repo)
    twin = tmp_path / "twin"
    sh(tmp_path, "git", "clone", "-q", str(repo), str(twin))
    sh(twin, "git", "remote", "set-url", "origin", "https://github.com/o/r.git")
    out = hook("review_gate.py", {"hook_event_name": "PreToolUse", "cwd": str(tmp_path),
                                  "tool_name": "mcp__github__create_pull_request",
                                  "tool_input": {"owner": "o", "repo": "r"}}, data_dir)
    assert out == {}, "two checkouts of o/r: which one opens the PR is unknown, so fail open"


def test_gh_pr_create_for_another_repo_is_not_judged_here(repo: Path, data_dir: Path) -> None:
    set_config(repo, review={"gate": "deny"})
    sh(repo, "git", "remote", "add", "origin", "https://github.com/o/r.git")
    _work(repo)
    other = {"tool_name": "Bash", "tool_input": {"command": "gh pr create -R x/y --fill"}}
    assert _gate(repo, data_dir, "PreToolUse", **other) == {}
    same = {"tool_name": "Bash", "tool_input": {"command": "gh pr create -R github.com/O/R --fill"}}
    assert _denied(_gate(repo, data_dir, "PreToolUse", **same))
    sh(repo, "git", "checkout", "-qb", "feat")
    fork = {"tool_name": "Bash", "tool_input": {"command": "gh pr create -R x/y --head me:feat"}}
    assert _denied(_gate(repo, data_dir, "PreToolUse", **fork)), "a local head branch is judged"


def test_a_push_under_another_name_is_gated_by_the_pr_branch_name(repo: Path, data_dir: Path) -> None:
    set_config(repo, review={"gate": "deny"})
    sh(repo, "git", "checkout", "-qb", "local")
    _work(repo)
    sh(repo, "git", "update-ref", "refs/remotes/origin/feat", "HEAD")  # as `push HEAD:feat` leaves it
    _review(repo, data_dir)
    assert _open_pr(repo, data_dir, head="feat") == {}  # PR head is feat, pushed from local
    (repo / "src" / "pkg" / "other.py").write_text("def other():\n    return 7\n")
    commit(repo, "unreviewed fix")
    push = {"tool_name": "Bash", "tool_input": {"command": "git push origin HEAD:feat"}}
    assert _denied(_gate(repo, data_dir, "PreToolUse", **push))
    delete = {"tool_name": "Bash", "tool_input": {"command": "git push origin --delete feat"}}
    assert _gate(repo, data_dir, "PreToolUse", **delete) == {}, "a delete publishes no tree"


def test_a_branch_known_only_on_the_remote_is_judged_by_its_remote_tree(
        repo: Path, data_dir: Path) -> None:
    from review_gate import _gated_ref
    _work(repo)
    sh(repo, "git", "update-ref", "refs/remotes/origin/far", "HEAD")
    assert _gated_ref(repo, "far") == "refs/remotes/origin/far"
    assert _gated_ref(repo, "nowhere") is None, "unknown branch: fail open"


def test_a_stale_lock_is_broken_once_and_a_live_one_is_put_back(tmp_path: Path) -> None:
    from review_gate import _break_stale
    stale = tmp_path / "s.lock"
    stale.write_text("")
    os.utime(stale, (1, 1))
    _break_stale(stale)
    assert not stale.exists()
    live = tmp_path / "l.lock"  # another waiter replaced the stale lock before our rename
    live.write_text("")
    _break_stale(live)
    assert live.exists(), "a fresh lock taken by mistake is restored"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["l.lock"]
