"""Schema checks for the kit plugin's eval suite.

`claude plugin eval` only reports a malformed case when you run the suite, which costs
model calls. These tests read the same files and fail for free on anything the command
would reject, plus one thing it accepts silently: a case whose every grader is excluded
from scoring in a two-arm run, which measures nothing.

Field names come from the eval suite reference:
https://code.claude.com/docs/en/plugin-evals
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

EVALS = Path(__file__).resolve().parents[1] / "plugins" / "kit" / "evals"

CASE_FIELDS = {
    "schema_version", "name", "description", "tags", "plugins", "runs",
    "expected_outcome", "model", "max_turns", "timeout_seconds", "allowed_tools",
    "append_system_prompt", "env",
}
GRADER_COMMON = {"type", "weight", "arm"}
GRADER_FIELDS = {
    "regex": {"pattern", "flags", "match", "target"},
    "tool_used": {"tool", "input_match", "min", "max"},
    "tool_order": {"before", "after"},
    "file_exists": {"path", "exists"},
    "llm": {"criteria", "focus"},
    "baseline": {"baseline_file", "criteria"},
}
READ_ONLY_TOOLS = {
    "Read", "Glob", "Grep", "NotebookRead", "Skill", "AskUserQuestion", "Agent",
    "TodoWrite", "TaskCreate", "TaskGet", "TaskList", "TaskUpdate", "TaskStop",
}


def frontmatter(path: Path) -> dict:
    """Parse the flat `key: value` frontmatter these files use. Not a YAML parser."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}
    block = text.split("---\n", 2)[1]
    out = {}
    for line in block.splitlines():
        key, sep, value = line.partition(":")
        if not sep or not key or not key[0].isalpha():
            continue
        value = value.strip()
        if value[:1] in {'"', "'"} and value[-1:] == value[:1] and len(value) > 1:
            value = value[1:-1]
        out[key.strip()] = value
    return out


def cases() -> list[Path]:
    found = [d for d in sorted(EVALS.iterdir()) if d.is_dir() and (d / "prompt.md").is_file()]
    assert found, f"no eval cases under {EVALS}"
    return found


def graders(case: Path) -> list[Path]:
    return sorted((case / "graders").glob("*.md"))


def scored_in_both_arms(meta: dict) -> bool:
    """Whether a grader counts toward the score when the no-plugin baseline runs.

    Claude Code drops graders that cannot pass without the plugin, so they can't
    inflate the delta. `arm: both` forces one back in.
    """
    if meta.get("arm") == "both":
        return True
    if meta.get("arm") == "with-only":
        return False
    if meta.get("type") == "tool_used" and meta.get("tool") == "Skill":
        return False
    if meta.get("type") == "regex" and meta.get("target") == "mock_calls":
        return False
    return not (meta.get("type") == "llm" and meta.get("focus") == "mock_calls")


@pytest.mark.parametrize("case", cases(), ids=lambda p: p.name)
def test_case_fields_are_real_fields(case: Path) -> None:
    """An unknown key in prompt.md frontmatter is an error, not a warning."""
    unknown = set(frontmatter(case / "prompt.md")) - CASE_FIELDS
    assert not unknown, f"{case.name}: unknown prompt.md fields {sorted(unknown)}"


@pytest.mark.parametrize("case", cases(), ids=lambda p: p.name)
def test_every_tool_the_case_wants_is_grantable(case: Path) -> None:
    """Runs never prompt for permission. A tool outside the read-only set is removed
    from the session unless the operator passes --allow-tools, so a case that lists one
    silently tests a Claude that cannot call it."""
    listed = frontmatter(case / "prompt.md").get("allowed_tools", "[]")
    wanted = {t.strip() for t in listed.strip("[]").split(",") if t.strip()}
    assert wanted <= READ_ONLY_TOOLS, (
        f"{case.name}: {sorted(wanted - READ_ONLY_TOOLS)} needs --allow-tools; "
        "say so in evals/README.md or drop it from the case"
    )


@pytest.mark.parametrize("case", cases(), ids=lambda p: p.name)
def test_graders_load(case: Path) -> None:
    files = graders(case)
    assert files, f"{case.name}: a case with no grader fails to load"
    for path in files:
        meta = frontmatter(path)
        kind = meta.get("type")
        assert kind in GRADER_FIELDS, f"{path.name}: unknown grader type {kind!r}"
        unknown = set(meta) - GRADER_COMMON - GRADER_FIELDS[kind]
        assert not unknown, f"{path.name}: {kind} grader has no field {sorted(unknown)}"
        if kind == "regex":
            assert meta.get("pattern"), f"{path.name}: regex grader needs a pattern"
            re.compile(meta["pattern"])
        if meta.get("input_match"):
            # input_match is a regex too, and an unparseable one survives every check
            # above: its type is known and its field name belongs to that type.
            re.compile(meta["input_match"])
        if kind == "llm":
            body = path.read_text(encoding="utf-8").split("---\n", 2)[2]
            assert "PASS" in body and "FAIL" in body, (
                f"{path.name}: write the rubric as concrete PASS and FAIL conditions"
            )


@pytest.mark.parametrize("case", cases(), ids=lambda p: p.name)
def test_every_case_scores_something_against_the_baseline(case: Path) -> None:
    """The failure this catches: a case graded only on "the skill fired". Claude Code
    excludes that from scoring in both arms, so the case reports a delta of zero and
    proves nothing about the plugin."""
    scored = [p.name for p in graders(case) if scored_in_both_arms(frontmatter(p))]
    assert scored, (
        f"{case.name}: every grader is excluded from the two-arm score, so the case "
        "cannot show what the plugin contributed"
    )


PROSE_TARGETS = {"last_message", "trace", "files", ""}


@pytest.mark.parametrize("case", cases(), ids=lambda p: p.name)
def test_regex_graders_match_literals_not_concepts(case: Path) -> None:
    """A regex cannot tell a recommendation from a refusal.

    The first real run caught two graders doing exactly that: `picks-fan-out` passed a
    reply that said "Skip subagents, /batch and worktrees", because the words were
    there, and `names-the-standing-cost` failed "all four other repos" because they
    weren't. Both patterns were alternations of concept words standing in for a
    judgement. Alternation is the signature, so it is what this blocks: a regex grader
    on prose matches an identifier or a literal string, and anything that needs reading
    comprehension belongs in an `llm` rubric.
    """
    for path in graders(case):
        meta = frontmatter(path)
        if meta.get("type") != "regex":
            continue
        if meta.get("target", "last_message") not in PROSE_TARGETS:
            continue
        assert "|" not in meta.get("pattern", ""), (
            f"{case.name}/{path.name}: this pattern is a list of concept words, which "
            "passes when the reply mentions them to reject them. Move the claim into an "
            "llm grader, or narrow the pattern to one literal."
        )


def test_the_suite_covers_what_the_kit_claims() -> None:
    """Each claim the kit makes in writing needs a case that could falsify it."""
    names = {c.name for c in cases()}
    for claim, case in {
        "the design is Claude's to settle": "design-not-handed-back",
        "trivial work stays trivial": "obvious-fix-stays-cheap",
        "duplication is a reportable defect": "duplication-is-a-defect",
        "a review tries every failure outcome and every input form": "review-tries-the-failure-paths",
        "independent work gets fanned out": "sweep-gets-fanned-out",
        "a decision already on record is reused, not re-derived": "prior-decision-is-reused",
        "a decision whose reversal condition fired is reopened from the old field": "reversed-decision-reopens",
        "a generic addition is offered to the kit, never promoted quietly": "promotion-is-offered-not-taken",
        "a delivered change fixes the cause, not only the line pointed at": "delivery-applies-the-floor",
        "the improvement loop never runs ahead of the user's task": "task-first",
        "a missing prerequisite is obtained during the task, not deferred": "capability-required",
        "a reusable candidate is scored against a baseline before it is shared": "reusable-capability",
        "learning reported by other repos becomes a scored PR, most-reported first": "learning-becomes-a-pr",
        "what already ships is searched and is the baseline a new component must beat": "existing-tool-is-the-baseline",
    }.items():
        assert case in names, f"no case covers: {claim}"
