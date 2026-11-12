"""Run repository-defined verification gates without a shell (Python 3.8+)."""
from __future__ import annotations

import fnmatch
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from kit_common import run, tail

UNCOVERED = "No configured gate covers: "


def valid_gate(gate: Any) -> bool:
    """A gate run_gates will run: a name, non-empty string paths and an argv of strings."""
    if not isinstance(gate, dict):
        return False
    name, paths, argv = gate.get("name"), gate.get("paths"), gate.get("argv")
    return (isinstance(name, str) and bool(name) and isinstance(paths, list) and bool(paths)
            and all(isinstance(p, str) and p for p in paths)
            and isinstance(argv, list) and bool(argv) and all(isinstance(a, str) for a in argv))


def gate_paths(config: Dict[str, Any]) -> List[str]:
    """Every path pattern a gate that will actually run declares."""
    gates = config.get("verification", {}).get("gates")
    return [p for gate in (gates if isinstance(gates, list) else []) if valid_gate(gate)
            for p in gate["paths"]]


def uncovered_by_gates(config: Dict[str, Any], changed: List[str]) -> List[str]:
    """Changed files no gate claims, minus those the repo declares have no checks to run
    (`verification.no_checks`: documentation, data, images)."""
    patterns = gate_paths(config)
    no_checks = config.get("verification", {}).get("no_checks") or []
    return sorted(f for f in changed
                  if not any(fnmatch.fnmatchcase(f, p) for p in patterns)
                  and not any(fnmatch.fnmatchcase(f, p) for p in no_checks))


def run_gates(root: Path, config: Dict[str, Any], changed: List[str],
              deadline: Optional[float] = None) -> Tuple[List[str], List[str]]:
    profile = config.get("verification", {})
    if not isinstance(profile, dict):
        return ["verification must be an object"], []
    if not profile.get("enabled", False):
        return [], []
    gates = profile.get("gates")
    if not isinstance(gates, list) or not gates:
        return ["verification is enabled but no gates are configured"], []
    failures: List[str] = []
    ran: List[str] = []
    for gate in gates:
        if not isinstance(gate, dict):
            failures.append("invalid gate: expected an object")
            continue
        if not valid_gate(gate):
            failures.append("invalid gate: name, paths, and argv are required")
            continue
        name, paths, argv = gate["name"], gate["paths"], gate["argv"]
        if not any(fnmatch.fnmatchcase(f, pattern) for f in changed for pattern in paths):
            continue
        try:
            cwd = (root / gate.get("cwd", ".")).resolve()
            cwd.relative_to(root.resolve())
            timeout = int(gate.get("timeout_s", 300))
            if timeout <= 0:
                raise ValueError("timeout must be positive")
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    failures.append(f"Gate {name} incomplete: hook time budget exhausted")
                    continue
                timeout = min(timeout, remaining)
            result = run(argv, cwd, timeout, kill_tree=True)
            ran.append(name)
            if result.returncode:
                output = tail(result.stdout + result.stderr)
                failures.append(f"Gate {name} failed (exit {result.returncode}):\n{output}")
        except (OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
            failures.append(f"Gate {name} unavailable: {exc}")
    uncovered = uncovered_by_gates(config, changed)
    if uncovered:
        failures.append(UNCOVERED + ", ".join(uncovered))
    return failures, ran
