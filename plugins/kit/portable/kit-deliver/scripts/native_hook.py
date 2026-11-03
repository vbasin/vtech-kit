"""Native Codex and Grok lifecycle adapters for the portable kit (Python 3.8+)."""
from __future__ import annotations

import contextlib
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict

from bash_guard import check
from kit_common import changed_since, load_config, load_state, repo_root, save_state, snapshot
from stop_verify import verdict_digest, verify

try:  # copied in by install_portable; absent in an older install
    import learning_report
except Exception:  # noqa: BLE001 — learning is optional, verification is not
    learning_report = None  # type: ignore[assignment]

POLICY = ("Use the kit-deliver skill for substantive requests. Complete the user's task first; "
          "use existing relevant capabilities, obtain missing task-critical ones, compare material "
          "options, and verify the final result. Use independent reviewers when available and authorized.")


def root_for(data: Dict[str, Any]) -> Path:
    root = repo_root(data.get("cwd") or data.get("workspaceRoot"))
    if root is None:
        raise ValueError("not in a git repository")
    return root


def state_id(data: Dict[str, Any], host: str = "codex") -> str:
    session = data.get("session_id") or data.get("sessionId")
    if not isinstance(session, str) or not session:
        raise ValueError("missing session id")
    return f"{host}-" + hashlib.sha256(session.encode()).hexdigest()


def begin(host: str, data: Dict[str, Any]) -> Path:
    """Session baseline for verification, and the component inventory learning diffs."""
    root = root_for(data)
    sid = state_id(data, host)
    state = load_state(sid)
    if not state.get("baseline"):
        state["baseline"] = snapshot(root)
        state["edited"] = []
        if learning_report is not None and learning_report.backlog is not None:
            with contextlib.suppress(Exception):  # without an inventory nothing is captured
                state["components"] = learning_report.inventory(root)
        save_state(sid, state)
    return root


def learn(host: str, data: Dict[str, Any], budget_s: float) -> None:
    """Record and send what the session added. Stop runs every turn on both hosts, and a
    repeat records nothing new, so this is safe there. Never affects the hook's answer."""
    if learning_report is None or learning_report.backlog is None or budget_s < 5:
        return
    try:
        root = root_for(data)
        sid = state_id(data, host)
        state = load_state(sid)
        learning_report.capture(root, state, learning_report.backlog.DEFAULT, budget_s=min(15.0, budget_s))
        save_state(sid, state)
    except Exception:  # noqa: BLE001
        pass


def start(data: Dict[str, Any]) -> Dict[str, Any]:
    root = begin("codex", data)
    cfg = load_config(root)
    if cfg.get("routing", {}).get("enabled") is False:
        return {}
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": POLICY}}


def stop(host: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """Codex, and Grok from 1.0.70 (whose Stop can block with Claude's output format), both
    get verification here, then learning capture in the time left."""
    if host == "grok" and data.get("reason") not in (None, "end_turn"):
        # Grok's extra observe-only Stop at session end runs under a ~1.5 s cap with its answer
        # ignored: a gate or a `gh` send started now would be killed part-way. The previous
        # turn's Stop already verified and captured.
        return {}
    began = time.monotonic()
    try:
        return verify_stop(host, data)
    finally:
        learn(host, data, STOP_TIMEOUT - 10 - (time.monotonic() - began))


STOP_TIMEOUT = 480  # install_native's Stop hook timeout on both hosts
GROK_STOP_VERIFIES = True  # read by kit_common.stand_aside: the plugin's Stop may step aside


def verify_stop(host: str, data: Dict[str, Any]) -> Dict[str, Any]:
    root = root_for(data)
    sid = state_id(data, host)
    state = load_state(sid)
    if not state.get("baseline"):
        return {"systemMessage": "kit verification unavailable: session baseline is missing; report unverified."}
    try:
        cfg = load_config(root, strict=True)
    except ValueError as exc:
        reason = f"kit verification unavailable: {exc}; report unverified."
        return {"systemMessage": reason} if data.get("stop_hook_active") else {"decision": "block", "reason": reason}
    if not any(cfg.get(k, {}).get("enabled") for k in ("lint", "tests", "verification")):
        return {}
    changed = changed_since(root, state["baseline"], state.get("edited", []))
    if not changed:
        return {}
    # Stop fires every turn. A diff already judged clean, or whose gaps were reported, gets
    # the same verdict again, so it is not paid for twice (as in the plugin's Stop hook).
    digest = verdict_digest(root, changed)
    if digest in (state.get("passed_digest"), state.get("gaps_digest")):
        return {}
    failures, _, gaps = verify(root, cfg, changed, deadline=time.monotonic() + 440)
    if not failures:
        state["gaps_digest" if gaps else "passed_digest"] = digest
        save_state(sid, state)
        return {"systemMessage": "kit verification incomplete:\n" + "\n".join(gaps)} if gaps else {}
    reason = "kit verification failed or is incomplete:\n" + "\n\n".join(failures + gaps)
    reason += "\nFix applicable failures or report remaining gaps; do not claim verification passed."
    if data.get("stop_hook_active"):
        return {"systemMessage": reason}
    return {"decision": "block", "reason": reason}


def guard(host: str, data: Dict[str, Any]) -> Dict[str, Any]:
    root = root_for(data)
    tool_input = data.get("tool_input") if host == "codex" else data.get("toolInput")
    if not isinstance(tool_input, dict) or not isinstance(tool_input.get("command"), str):
        return {}
    hit = check(tool_input["command"], load_config(root), root, data.get("cwd"))
    if not hit:
        return {}
    decision, reason = hit
    if decision == "deny":
        if host == "grok":
            return {"decision": "deny", "reason": reason}
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": reason}}
    if decision == "ask" and host == "codex":
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext":
                                       "Kit guard: " + reason + " Follow the host's permission policy."}}
    return {}


def main() -> int:
    try:
        host, event = sys.argv[1:3]
        data = json.load(sys.stdin)
        if not isinstance(data, dict) or host not in ("codex", "grok"):
            raise ValueError("invalid host or input")
        if event == "guard":
            if host == "grok":  # fallback baseline if Grok's passive SessionStart did not run
                with contextlib.suppress(Exception):  # the guard's answer must not depend on it
                    begin("grok", data)
            output = guard(host, data)
        elif host == "codex" and event == "start":
            output = start(data)
        elif event == "stop":
            output = stop(host, data)
        elif host == "grok" and event == "start":
            begin("grok", data)  # Grok ignores SessionStart output: record state only
            output = {}
        else:
            raise ValueError("unsupported host event")
        print(json.dumps(output))
    except Exception as exc:  # noqa: BLE001 — never claim success when verification could not run
        print(json.dumps({"systemMessage": f"kit hook unavailable ({type(exc).__name__}); report unverified."}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
