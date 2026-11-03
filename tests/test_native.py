"""Host adapter contract and install tests without live model calls."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/kit/scripts"
sys.path.insert(0, str(SCRIPTS))
import install_native  # noqa: E402


def call_hook(repo, host, event, data, tmp_path):
    folder = ".agents" if host == "codex" else ".grok"
    script = repo / folder / "skills/kit-deliver/scripts/native_hook.py"
    env = {**os.environ, "CLAUDE_PLUGIN_DATA": str(tmp_path / "state")}
    proc = subprocess.run([sys.executable, str(script), host, event], input=json.dumps(data),
                          cwd=repo, env=env, capture_output=True, text=True, check=True)
    return json.loads(proc.stdout)


def test_codex_start_stop_retry_and_reviewer_install(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".kit").mkdir()
    cfg = {"verification": {"enabled": True, "gates": [{"name": "test", "paths": ["*.txt"],
           "argv": [sys.executable, "-c", "raise SystemExit(1)"]}]}}
    (repo / ".kit/kit.json").write_text(json.dumps(cfg))
    (repo / "work.txt").write_text("before")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=t@t", "commit", "-qm", "base"],
                   check=True)
    assert install_native.install(repo, "codex") == "installed"
    assert install_native.install(repo, "codex") == "unchanged"
    cfg_hooks = json.loads((repo / ".codex/hooks.json").read_text())["hooks"]
    assert {"SessionStart", "PreToolUse", "Stop"} == set(cfg_hooks)
    assert (repo / ".codex/agents/kit_reviewer.toml").is_file()
    subdir = repo / "nested"
    subdir.mkdir()
    command = cfg_hooks["SessionStart"][0]["hooks"][0]["command"]
    spawned = subprocess.run(command, shell=True, cwd=subdir, input=json.dumps({
        "cwd": str(subdir), "session_id": "shell-check"}), capture_output=True, text=True,
        env={**os.environ, "CLAUDE_PLUGIN_DATA": str(tmp_path / "state")}, check=True)
    assert "kit-deliver" in json.loads(spawned.stdout)["hookSpecificOutput"]["additionalContext"]
    data = {"cwd": str(repo), "session_id": "session-1"}
    start = call_hook(repo, "codex", "start", data, tmp_path)
    assert "kit-deliver" in start["hookSpecificOutput"]["additionalContext"]
    (repo / "work.txt").write_text("after")
    failed = call_hook(repo, "codex", "stop", data, tmp_path)
    assert failed["decision"] == "block" and "Gate test failed" in failed["reason"]
    retry = call_hook(repo, "codex", "stop", {**data, "stop_hook_active": True}, tmp_path)
    assert "incomplete" in retry["systemMessage"] and "decision" not in retry
    cfg["verification"]["gates"][0]["argv"][-1] = "raise SystemExit(0)"
    (repo / ".kit/kit.json").write_text(json.dumps(cfg))
    # Cover the modified config too so the gate doesn't report an unrelated gap.
    cfg["verification"]["gates"][0]["paths"].append(".kit/*")
    (repo / ".kit/kit.json").write_text(json.dumps(cfg))
    assert call_hook(repo, "codex", "stop", data, tmp_path) == {}


def test_codex_stop_verifies_first_commit_in_new_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".kit").mkdir()
    (repo / ".kit/kit.json").write_text(json.dumps({"verification": {"enabled": True, "gates": [
        {"name": "failure", "paths": ["*.txt"], "argv": [sys.executable, "-c", "raise SystemExit(1)"]}
    ]}}))
    install_native.install(repo, "codex")
    data = {"cwd": str(repo), "session_id": "unborn"}
    call_hook(repo, "codex", "start", data, tmp_path)
    (repo / "new file.txt").write_text("new work")
    subprocess.run(["git", "-C", str(repo), "add", "new file.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=t@t", "commit", "-qm", "first"],
                   check=True)
    result = call_hook(repo, "codex", "stop", data, tmp_path)
    assert result["decision"] == "block" and "Gate failure failed" in result["reason"]


@pytest.mark.parametrize("host,folder", [("codex", ".codex"), ("grok", ".grok")])
def test_native_guard_deny_and_ask_contract(tmp_path, host, folder):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".kit").mkdir()
    config = {"guards": {"sql_destructive": "deny"}}
    (repo / ".kit/kit.json").write_text(json.dumps(config))
    install_native.install(repo, host)
    data = {"cwd": str(repo), "tool_input": {"command": "psql -c 'DROP TABLE t'"},
            "toolInput": {"command": "psql -c 'DROP TABLE t'"}}
    out = call_hook(repo, host, "guard", data, tmp_path)
    if host == "codex":
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    else:
        assert out["decision"] == "deny"
    config["guards"]["sql_destructive"] = "ask"
    (repo / ".kit/kit.json").write_text(json.dumps(config))
    out = call_hook(repo, host, "guard", data, tmp_path)
    assert "decision" not in out
    if host == "grok":
        assert out == {}
    else:
        assert "additionalContext" in out["hookSpecificOutput"]


def test_install_refuses_to_overwrite_user_config(tmp_path):
    target = tmp_path / ".codex/hooks.json"
    target.parent.mkdir()
    target.write_text('{"my-hooks": true}')
    with pytest.raises(ValueError, match="reconcile"):
        install_native.install(tmp_path, "codex")
    assert target.read_text() == '{"my-hooks": true}'
    assert not (tmp_path / ".agents/skills/kit-deliver").exists()


def test_shared_workflow_preserves_existing_instructions(tmp_path):
    instructions = tmp_path / "AGENTS.md"
    instructions.write_text("Existing project instructions.\n")
    assert install_native.install(tmp_path, "grok") == "installed"
    first = instructions.read_text()
    assert first.startswith("Existing project instructions.\n")
    assert first.count(install_native.START) == 1
    assert install_native.install(tmp_path, "codex") == "installed"
    assert instructions.read_text() == first
    instructions.write_text(first.replace("Complete the user's task first", "Changed instruction"))
    with pytest.raises(ValueError, match="reconcile"):
        install_native.install(tmp_path, "grok")
    assert "Changed instruction" in instructions.read_text()


@pytest.mark.parametrize("host,folder", [("codex", ".agents"), ("grok", ".grok")])
def test_native_install_reports_portable_updates_and_additions(tmp_path, monkeypatch, host, folder):
    portable = install_native.install_portable
    source = tmp_path / "source"
    shutil.copytree(portable.SOURCE, source)
    skills = tmp_path / "skills"
    skills.mkdir()
    monkeypatch.setattr(portable, "SOURCE", source)
    monkeypatch.setattr(portable, "SKILLS", skills)
    repo = tmp_path / "repo"
    repo.mkdir()
    assert install_native.install(repo, host) == "installed"
    assert install_native.install(repo, host) == "unchanged"
    revised = (source / "SKILL.md").read_text() + "\nUpdated workflow.\n"
    (source / "SKILL.md").write_text(revised)
    assert install_native.install(repo, host) == "installed"
    assert (repo / folder / "skills/kit-deliver/SKILL.md").read_text() == revised
    assert install_native.install(repo, host) == "unchanged"
    added = skills / "new-skill"
    added.mkdir()
    (added / ".kit-portable").write_text("")
    (added / "SKILL.md").write_text("---\nname: new-skill\ndescription: New capability\n---\n")
    assert install_native.install(repo, host) == "installed"
    assert (repo / folder / "skills/new-skill/SKILL.md").is_file()
    assert install_native.install(repo, host) == "unchanged"


def test_codex_stop_reports_gaps_alongside_failures(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".kit").mkdir()
    (repo / ".kit/kit.json").write_text(json.dumps({"verification": {"enabled": True, "gates": [
        {"name": "failure", "paths": ["*.txt"], "argv": [sys.executable, "-c", "raise SystemExit(1)"]}
    ]}}))
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=t@t", "commit", "-qm", "base"],
                   check=True)
    install_native.install(repo, "codex")
    data = {"cwd": str(repo), "session_id": "gaps"}
    call_hook(repo, "codex", "start", data, tmp_path)
    (repo / "work.txt").write_text("work")
    (repo / "notes.md").write_text("notes")
    result = call_hook(repo, "codex", "stop", data, tmp_path)
    assert result["decision"] == "block" and "Gate failure failed" in result["reason"]
    assert "notes.md" in result["reason"], "the uncovered file vanished behind the failure"


def _origin_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/acme/pipeline.git"],
                   check=True)
    return repo


def _components(learning):
    if not learning.exists():
        return []
    return sorted(f"{r['type']}:{r['name']}" for r in map(json.loads, learning.read_text().splitlines())
                  if r.get("kind") == "component")


def test_codex_and_grok_capture_what_the_session_added_once(tmp_path, monkeypatch):
    """Learning capture is not Claude-only: Codex's Stop and Grok's Stop record what the
    session added, the kit's own installed files are not mistaken for it, and a component
    seen by both hosts is recorded once."""
    learning = tmp_path / "learning.jsonl"
    monkeypatch.setenv("KIT_LEARNING_FILE", str(learning))
    repo = _origin_repo(tmp_path)
    assert install_native.install(repo, "codex") == "installed"
    assert install_native.install(repo, "grok") == "installed"
    installed = repo / ".agents/skills/kit-deliver/scripts/learning_report.py"
    assert installed.is_file(), "the copied runtime must carry the capture code"
    hooks = json.loads((repo / ".grok/hooks/kit.json").read_text())["hooks"]
    assert {"SessionStart", "PreToolUse", "Stop"} == set(hooks)

    codex = {"cwd": str(repo), "session_id": "c1"}
    grok = {"cwd": str(repo), "sessionId": "g1"}
    call_hook(repo, "codex", "start", codex, tmp_path)
    assert call_hook(repo, "grok", "start", grok, tmp_path) == {}
    skill = repo / ".agents/skills/trace-repro"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: trace-repro\ndescription: d\n---\n")
    call_hook(repo, "codex", "stop", codex, tmp_path)
    assert _components(learning) == ["skill:trace-repro"], "kit_* agents and kit skills are the kit's, not learned"
    call_hook(repo, "codex", "stop", codex, tmp_path)  # Stop runs every turn
    assert call_hook(repo, "grok", "stop", grok, tmp_path) == {}
    assert _components(learning) == ["skill:trace-repro"], "a repeat or a second host must not re-record"

    (repo / ".codex/agents").mkdir(exist_ok=True)
    (repo / ".codex/agents/triage.toml").write_text('name = "triage"\n')
    call_hook(repo, "grok", "stop", grok, tmp_path)
    assert _components(learning) == ["agent:triage", "skill:trace-repro"]


def test_learning_never_changes_the_codex_stop_answer(tmp_path, monkeypatch):
    monkeypatch.setenv("KIT_LEARNING_FILE", str(tmp_path / "no-such-dir" / "x" / "learning.jsonl"))
    repo = _origin_repo(tmp_path)
    install_native.install(repo, "codex")
    data = {"cwd": str(repo), "session_id": "c2"}
    call_hook(repo, "codex", "start", data, tmp_path)
    (tmp_path / "no-such-dir").write_text("a file where the log's folder should be")
    (repo / ".agents/skills/new").mkdir(parents=True)
    (repo / ".agents/skills/new/SKILL.md").write_text("---\nname: new\ndescription: d\n---\n")
    assert call_hook(repo, "codex", "stop", data, tmp_path) == {}


def test_grok_first_bash_call_sets_the_baseline_when_start_did_not_run(tmp_path, monkeypatch):
    learning = tmp_path / "learning.jsonl"
    monkeypatch.setenv("KIT_LEARNING_FILE", str(learning))
    repo = _origin_repo(tmp_path)
    install_native.install(repo, "grok")
    data = {"cwd": str(repo), "sessionId": "g2", "toolInput": {"command": "ls"}}
    assert call_hook(repo, "grok", "guard", data, tmp_path) == {}
    (repo / ".grok/skills/notes").mkdir(parents=True)
    (repo / ".grok/skills/notes/SKILL.md").write_text("---\nname: notes\ndescription: d\n---\n")
    call_hook(repo, "grok", "guard", data, tmp_path)  # a later call must not move the baseline
    call_hook(repo, "grok", "stop", data, tmp_path)
    assert _components(learning) == ["skill:notes"]


def test_install_upgrades_its_own_unchanged_files_and_keeps_edited_ones(tmp_path, monkeypatch):
    """A Grok install made before the learning hooks has the guard-only kit.json; reinstalling
    must upgrade it, not refuse. A file the user edited is still refused."""
    repo = tmp_path / "repo"
    repo.mkdir()
    old = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command":
           'python3 "$(git rev-parse --show-toplevel)/.grok/skills/kit-deliver/scripts/native_hook.py" grok guard',
           "timeout": 15}]}]}}
    (repo / ".grok/hooks").mkdir(parents=True)
    (repo / ".grok/hooks/kit.json").write_text(json.dumps(old, indent=2) + "\n")
    assert install_native.install(repo, "grok") == "installed"
    hooks = json.loads((repo / ".grok/hooks/kit.json").read_text())["hooks"]
    assert {"SessionStart", "PreToolUse", "Stop"} == set(hooks)
    assert install_native.install(repo, "grok") == "unchanged"

    # A later kit version changes the file: the manifest says the old one is ours.
    monkeypatch.setattr(install_native, "desired", lambda host: {".grok/hooks/kit.json": "{}\n"})
    assert install_native.install(repo, "grok") == "installed"
    assert (repo / ".grok/hooks/kit.json").read_text() == "{}\n"

    (repo / ".grok/hooks/kit.json").write_text('{"mine": true}\n')
    monkeypatch.setattr(install_native, "desired", lambda host: {".grok/hooks/kit.json": "{\"v\": 3}\n"})
    with pytest.raises(ValueError, match="reconcile"):
        install_native.install(repo, "grok")
    assert (repo / ".grok/hooks/kit.json").read_text() == '{"mine": true}\n'


def test_grok_stop_verifies_like_codex(tmp_path):
    """Grok 1.0.70's Stop can block in Claude's format, so the native Grok Stop verifies."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".kit").mkdir()
    (repo / ".kit/kit.json").write_text(json.dumps({"verification": {"enabled": True, "gates": [
        {"name": "failure", "paths": ["*.txt"], "argv": [sys.executable, "-c", "raise SystemExit(1)"]}]}}))
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=t@t", "commit", "-qm", "base"],
                   check=True)
    install_native.install(repo, "grok")
    hooks = json.loads((repo / ".grok/hooks/kit.json").read_text())["hooks"]
    assert hooks["Stop"][0]["hooks"][0]["timeout"] == 480
    data = {"cwd": str(repo), "sessionId": "g-verify", "session_id": "g-verify"}
    assert call_hook(repo, "grok", "start", data, tmp_path) == {}
    (repo / "work.txt").write_text("work")
    result = call_hook(repo, "grok", "stop", data, tmp_path)
    assert result["decision"] == "block" and "Gate failure failed" in result["reason"]
    retry = call_hook(repo, "grok", "stop", {**data, "stop_hook_active": True}, tmp_path)
    assert "decision" not in retry and "failed" in retry["systemMessage"]


def test_native_stop_does_not_rerun_gates_on_an_unchanged_diff(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    runs = tmp_path / "runs"
    (repo / ".kit").mkdir()
    (repo / ".kit/kit.json").write_text(json.dumps({"verification": {"enabled": True, "gates": [
        {"name": "count", "paths": ["*.txt", ".kit/*"],
         "argv": [sys.executable, "-c", f"open({str(runs)!r}, 'a').write('x')"]}]}}))
    install_native.install(repo, "grok")
    data = {"cwd": str(repo), "sessionId": "cache", "session_id": "cache"}
    call_hook(repo, "grok", "start", data, tmp_path)
    (repo / "work.txt").write_text("work")
    assert call_hook(repo, "grok", "stop", data, tmp_path) == {}
    assert call_hook(repo, "grok", "stop", data, tmp_path) == {}
    assert runs.read_text() == "x", "a clean verdict on the same diff is not paid for twice"
    (repo / "work.txt").write_text("more work")
    call_hook(repo, "grok", "stop", data, tmp_path)
    assert runs.read_text() == "xx"


def test_grok_session_end_stop_does_no_work(tmp_path):
    """Grok fires one more Stop at session end, capped near 1.5 s and ignored; only
    `reason == "end_turn"` is a real turn end."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    runs = tmp_path / "runs"
    (repo / ".kit").mkdir()
    (repo / ".kit/kit.json").write_text(json.dumps({"verification": {"enabled": True, "gates": [
        {"name": "count", "paths": ["*.txt"],
         "argv": [sys.executable, "-c", f"open({str(runs)!r}, 'a').write('x')"]}]}}))
    install_native.install(repo, "grok")
    data = {"cwd": str(repo), "sessionId": "end", "session_id": "end"}
    call_hook(repo, "grok", "start", data, tmp_path)
    (repo / "work.txt").write_text("work")
    assert call_hook(repo, "grok", "stop", {**data, "reason": "session_end"}, tmp_path) == {}
    assert not runs.exists()
    call_hook(repo, "grok", "stop", {**data, "reason": "end_turn"}, tmp_path)
    assert runs.read_text() == "x"
