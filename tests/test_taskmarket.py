"""Taskmarket: вторая площадка с оплатой в USDC без банка.

Разметка страницы — это весь контракт, который у нас есть (публичной схемы API у
площадки нет), поэтому разбор проверяется на сохранённой странице, а не на
живом сайте: живой сайт меняется и недоступен из песочницы, а тест должен
падать тогда, когда сломается разбор, а не тогда, когда площадка недоступна.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest

from agent.channels.taskmarket import CROWD_LIMIT, TaskMarketChannel, parse_tasks

#: Фрагмент выдачи площадки в том виде, в каком его отдаёт сайт: режим и статус
#: слипаются с заголовком, награда и число работ идут текстом карточки.
PAGE = """
<ul class="task-list">
  <li class="task-row">
    <a href="/tasks/0x5f596b1a81417834a4366655bd4e6194819f5404a62c919c6953ae9bc92860bc">bountyOpen
      Quantum-Safe Bitcoin: share 199 USDC for verified Yukon improvements
      Reward 199 USDC
      Due 29d left
      2 submissions</a>
  </li>
  <li class="task-row">
    <a href="/tasks/0xd7cc0add611322a2277d6124a0490d968ba835a3675b7cc7fbfe411b8cf72a9a">bountyOpen
      A Travel Poster for a Place You Can Never Visit
      Reward 2 USDC
      Due 14h left
      44 submissions</a>
  </li>
  <li class="task-row">
    <a href="/tasks/0x935a2d3c8c949e8c58feacc6f9469142a1ad0d4a07c2922d0797bf2929a0a7b9">bountyOpen
      Execute one bounded onchain action through KeeperHub
      Reward 0.01 USDC
      Due Expired
      23 submissions</a>
  </li>
  <li class="task-row">
    <a href="/tasks/0xfeb54178b019ece8cfe2cc207d0ee8e310b4a1ebac1be5d9171109dd14265e68">claimClaimed
      Short research summary for a pipeline check
      Reward 4 USDC
      Due 120h left
      0 submissions</a>
  </li>
</ul>
"""


class FakeTaskMarket(TaskMarketChannel):
    """Канал на сохранённой странице: сеть подменена."""

    def __init__(self, html: str = PAGE, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._html = html
        self.fetches = 0

    def _fetch(self) -> str:
        self.fetches += 1
        return self._html


def by_title(tasks: List[Dict[str, Any]], marker: str) -> Dict[str, Any]:
    return next(task for task in tasks if marker in task["title"])


# --- разбор страницы ------------------------------------------------------------

def test_cards_are_parsed_with_money_and_competition() -> None:
    tasks = parse_tasks(PAGE)
    assert len(tasks) == 4

    rich = by_title(tasks, "Quantum-Safe")
    assert rich["reward_usd"] == 199.0
    assert rich["submissions"] == 2
    assert rich["hours_left"] == 29 * 24
    assert rich["mode"] == "bounty"
    assert rich["status"] == "Open"
    assert rich["url"].startswith("https://taskmarket.dev/tasks/0x")
    assert rich["title"] == "Quantum-Safe Bitcoin: share 199 USDC for verified Yukon improvements"


def test_expired_task_is_recognised() -> None:
    expired = by_title(parse_tasks(PAGE), "KeeperHub")
    assert expired["hours_left"] < 0


def test_hours_left_is_read_from_hours_and_days() -> None:
    tasks = parse_tasks(PAGE)
    assert by_title(tasks, "Travel Poster")["hours_left"] == 14.0
    assert by_title(tasks, "Quantum-Safe")["hours_left"] == 696.0


def test_claim_mode_and_status_are_kept() -> None:
    claim = by_title(parse_tasks(PAGE), "pipeline check")
    assert claim["mode"] == "claim"
    assert claim["status"] == "Claimed"


def test_garbage_html_does_not_pretend_to_have_tasks() -> None:
    assert parse_tasks("<html><body>ничего</body></html>") == []
    assert parse_tasks("") == []


def test_broken_markup_is_reported_not_hidden() -> None:
    channel = FakeTaskMarket(html="<html><body>площадка переделала вёрстку</body></html>")
    assert channel.harvest() == []
    assert "разметка" in channel.last_error


# --- оценка: толпа отсеивается, редкая дорогая задача остаётся ----------------

def test_a_crowded_two_dollar_task_is_not_offered_as_work() -> None:
    channel = FakeTaskMarket()
    harvested = {opp.title: opp for opp in channel.harvest()}
    poster = next(opp for title, opp in harvested.items() if "Travel Poster" in title)
    assert poster.status == "done", "44 работы за $2 — это не работа, а лотерея"
    assert "делится между всеми" in poster.rationale


def test_a_rare_expensive_task_gets_a_real_score() -> None:
    offered = [opp for opp in FakeTaskMarket().harvest() if opp.status == "queued"]
    rich = next(opp for opp in offered if "Quantum-Safe" in opp.title)
    assert rich.score > 3.0, f"$199 при двух работах стоит больше порога: {rich.score}"
    assert rich.payload["payout_rail"] == "usdc_wallet"
    assert rich.payload["probability"] > 0.2
    assert "конкурентов пока нет" not in rich.rationale


def test_the_first_submitter_is_told_so() -> None:
    offered = [opp for opp in FakeTaskMarket().harvest() if opp.status == "queued"]
    for opp in offered:
        if opp.payload["submissions"] == 0:
            assert "шанс быть первым" in opp.rationale


def test_expired_tasks_never_reach_the_queue() -> None:
    for opp in FakeTaskMarket().harvest():
        if opp.status == "queued":
            assert opp.payload["hours_left"] is None or opp.payload["hours_left"] >= 0


def test_crowd_limit_is_configurable() -> None:
    assert CROWD_LIMIT == 25
    strict = FakeTaskMarket(crowd_limit=1).harvest()
    assert all(opp.status == "done" or opp.payload["submissions"] <= 1 for opp in strict)


def test_probability_drops_with_every_submission() -> None:
    channel = FakeTaskMarket()
    chances = [channel._probability(count, 100.0) for count in (0, 1, 3, 8, 20, 60)]
    assert chances == sorted(chances, reverse=True)
    assert chances[0] > chances[-1] * 10


def test_little_time_left_halves_the_chance() -> None:
    channel = FakeTaskMarket()
    assert channel._probability(2, 4.0) == pytest.approx(channel._probability(2, 100.0) / 2)


# --- связь с фермой -------------------------------------------------------------

def test_the_channel_is_registered_and_allowed() -> None:
    from agent.channels import ACTIVE_CHANNELS, build_channel

    assert "taskmarket" in ACTIVE_CHANNELS
    channel = build_channel("taskmarket")
    assert channel.allowed, channel.decision.reason


def test_the_no_bank_direction_counts_both_platforms() -> None:
    """Taskmarket должен попадать в направление «без банка», а не висеть отдельно."""
    from agent import directions

    no_bank = next(item for item in directions.DIRECTIONS if item.key == "no_bank")
    assert no_bank.extra_channels == ["taskmarket"]
    payload = no_bank.as_dict()
    assert payload["channels"] == ["agent_marketplaces", "taskmarket"]


def test_harvest_uses_no_key_and_no_wallet() -> None:
    """Канал читает открытую страницу: никаких ключей и кошельков у фермы нет."""
    source = Path("agent/channels/taskmarket.py").read_text(encoding="utf-8")
    for forbidden in ("private_key", "seed_phrase", "PRIVATE_KEY", "sign_transaction"):
        assert forbidden not in source
    channel = FakeTaskMarket()
    channel.harvest()
    assert channel.fetches == 1
