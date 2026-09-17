"""Playbooks decide how fast a task type gets done — the income multiplier."""

from __future__ import annotations

from agent import playbooks


def test_classify_prefers_specific_match() -> None:
    audit = playbooks.classify(["audit"], "Add qdrant support", "")
    assert audit.key == "smart_contract_audit"

    tests = playbooks.classify(["tests"], "Write unit tests for leaderboard", "")
    assert tests.key == "tests"

    docs = playbooks.classify([], "Fix typo in README", "")
    assert docs.key == "documentation"

    bug = playbooks.classify(["bug"], "Fix crash on empty input", "")
    assert bug.key == "bug_fix"


def test_generic_fallback_for_unknown_tasks() -> None:
    playbook = playbooks.classify([], "Do the thing properly", "some body")
    assert playbook.key == "generic"
    assert playbook.steps


def test_every_playbook_is_actionable() -> None:
    for playbook in playbooks.all_playbooks():
        assert playbook.steps, playbook.key
        assert playbook.review_checks, playbook.key
        assert playbook.traps, playbook.key
        assert playbook.typical_hours > 0


def test_leverage_never_promises_magic() -> None:
    base = 4.0
    for key in ("documentation", "tests", "bug_fix", "feature", "generic"):
        adjusted = playbooks.leverage_hours(key, base)
        assert 0.5 <= adjusted <= base, key
    assert playbooks.leverage_hours("generic", base) == base
    assert playbooks.leverage_hours("documentation", base) < base


def test_get_unknown_key_returns_generic() -> None:
    assert playbooks.get("nope").key == "generic"


def test_dictionary_view_is_serialisable() -> None:
    data = playbooks.get("tests").as_dict()
    assert data["key"] == "tests"
    assert isinstance(data["steps"], list)
