"""Обучение: робот опирается на решения человека, а не на догадки."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent import learning


@pytest.fixture(autouse=True)
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "learning.db"
    monkeypatch.setenv("DB_PATH", str(path))
    return path


def test_without_data_there_is_nothing_to_generalise() -> None:
    lines = learning.summary_lines()
    assert "Пока нечего обобщать" in "\n".join(lines)
    assert learning.advise() is None


def test_query_weight_stays_neutral_until_enough_observations() -> None:
    for _ in range(learning.MIN_OBSERVATIONS - 1):
        learning.observe("label:bounty", 4)
    stats = learning.query_stats()
    assert stats[0].weight == 1.0, "мало данных — вес не должен меняться"


def test_productive_query_gets_more_weight_than_a_barren_one() -> None:
    good, bad = "label:bounty state:open", "label:help wanted"
    for _ in range(4):
        learning.observe(good, 8)
        learning.observe(bad, 0)
    learning.decide("github:a/b#1", "published", channel="github_bounties",
                    playbook="docs", reward_usd=200, query=good)
    learning.decide("github:a/b#2", "skipped", channel="github_bounties",
                    playbook="generic", query=bad)

    weights = learning.query_weights()
    assert weights[good] > 1.0
    assert weights[bad] < 1.0
    assert weights[bad] >= learning.MIN_WEIGHT, "запрос не выбрасывается навсегда"


def test_weight_never_exceeds_the_cap() -> None:
    for _ in range(50):
        learning.observe("идеальный запрос", 100)
    assert learning.query_weights()["идеальный запрос"] <= learning.MAX_WEIGHT


def test_queries_are_reordered_not_dropped() -> None:
    queries = ["шум", "полезный", "нейтральный"]
    for _ in range(4):
        learning.observe("шум", 0)
        learning.observe("полезный", 10)
    learning.decide("github:a/b#9", "published", channel="github_bounties", query="полезный")
    ordered = learning.order_queries(queries)
    assert sorted(ordered) == sorted(queries), "ни один запрос не теряется"
    assert ordered[0] == "полезный"
    assert ordered[-1] == "шум"


def test_published_work_raises_and_skipped_work_lowers_the_weight() -> None:
    for _ in range(3):
        learning.observe("запрос", 2)
    baseline = learning.query_weights()["запрос"]
    learning.decide("github:a/b#1", "skipped", channel="github_bounties", query="запрос")
    learning.decide("github:a/b#2", "skipped", channel="github_bounties", query="запрос")
    after_skips = learning.query_weights()["запрос"]
    assert after_skips < baseline

    learning.decide("github:a/b#3", "published", channel="github_bounties", query="запрос")
    assert learning.query_weights()["запрос"] > after_skips


def test_playbook_stats_point_at_what_actually_gets_sent() -> None:
    learning.decide("github:a/b#1", "published", channel="github_bounties", playbook="docs")
    learning.decide("github:a/b#2", "published", channel="github_bounties", playbook="docs")
    learning.decide("github:a/b#3", "skipped", channel="github_bounties", playbook="generic")
    stats = {item.subject: item for item in learning.playbook_stats()}
    assert stats["docs"].published == 2
    assert stats["generic"].published == 0


def test_verified_money_is_recorded_as_an_outcome() -> None:
    learning.outcome("audit_contests", 250.0, subject="audit_contests", detail="USDC")
    state = learning.learned_state()
    assert state["earned_usd"] == 250.0


def test_summary_names_the_productive_query() -> None:
    for _ in range(4):
        learning.observe("label:bounty state:open", 6)
    learning.decide("github:a/b#1", "published", channel="github_bounties",
                    query="label:bounty state:open")
    text = "\n".join(learning.summary_lines())
    assert "Продуктивный запрос" in text
    assert "label:bounty state:open" in text
