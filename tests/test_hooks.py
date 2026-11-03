"""End-to-end tests for the kit plugin's hook scripts.

Each test builds a throwaway git repo, feeds the hook the JSON Claude Code would send
on stdin, and checks what the hook prints. Requires pytest and ruff in the interpreter
running the tests (`pip install pytest ruff`).
"""

from __future__ import annotations

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
from kit_common import changed_since, snapshot  # noqa: E402
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
case "$1 $2" in
  "issue list") [ -n "$GH_LIST_FAIL" ] && exit 1; cat "$GH_ISSUES" 2>/dev/null || echo "[]";;
  "issue create") [ -n "$GH_FAIL" ] && exit 1; echo "https://github.com/o/kit/issues/7";;
  "issue comment") echo ok;;
  "issue reopen") [ -n "$GH_REOPEN_FAIL" ] && exit 1; echo ok;;
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
    assert "label create kit-learning -R o/kit --force" in log, "a fresh kit repo has no label to file under"
    assert "issue create -R o/kit --title [kit-learning] skill: trace-repro" in log

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
    assert "issue reopen 7 -R o/kit" in log and "issue comment 7 -R o/kit" in log
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
    assert "issue create" not in gh_log(tmp_path), "created without knowing what exists"
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
    assert "issue comment" not in gh_log(tmp_path) and [e["kind"] for e in events] == ["component"]


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
