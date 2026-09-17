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


# --- отчёт площадки: мост из GitHub Actions в локальную ферму ----------------

def snapshot_file(root: Path, hours_ago: float = 1.0, tasks: List[Dict[str, Any]] = None) -> Path:
    import json
    from datetime import datetime, timedelta, timezone

    target = root / "snapshots" / "taskmarket-report.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    stamp = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    target.write_text(json.dumps({
        "channel": "taskmarket", "generated_at": stamp,
        "count": len(tasks or []), "tasks": tasks or [],
    }, ensure_ascii=False), encoding="utf-8")
    return target


class OfflineTaskMarket(FakeTaskMarket):
    """Ферма без доступа к домену площадки: живой страницы нет, есть отчёт."""

    def _fetch(self) -> str:
        self.last_error = "площадка недоступна: SSLError"
        return ""


def test_snapshot_replaces_the_live_page(tmp_path: Path,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    from agent import snapshots

    snapshot_file(tmp_path, 2.0, parse_tasks(PAGE))
    monkeypatch.setattr(snapshots, "project_root", lambda: tmp_path)

    found = OfflineTaskMarket().harvest()
    assert found, "задачи из отчёта должны попадать в очередь без сети"
    quest = next(opp for opp in found if "Quantum-Safe" in opp.title)
    assert quest.payload["source"] == "snapshot"
    assert "GitHub Actions" in quest.rationale
    assert "2.0ч назад" in quest.rationale


def test_an_old_snapshot_is_called_old(tmp_path: Path,
                                      monkeypatch: pytest.MonkeyPatch) -> None:
    from agent import snapshots

    snapshot_file(tmp_path, 40.0, parse_tasks(PAGE))
    monkeypatch.setattr(snapshots, "project_root", lambda: tmp_path)
    quest = [opp for opp in OfflineTaskMarket().harvest() if "Quantum-Safe" in opp.title][0]
    assert "устарел" in quest.rationale


def test_without_a_snapshot_the_channel_says_why(tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    from agent import snapshots

    monkeypatch.setattr(snapshots, "project_root", lambda: tmp_path)
    channel = OfflineTaskMarket()
    assert channel.harvest() == []
    assert "отчёта" in channel.last_error, "нет отчёта — так и скажи"
    assert "SSLError" in channel.last_error, "и причину сбоя живой страницы тоже"


def test_snapshot_command_writes_a_readable_report(tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    from agent import main, snapshots
    monkeypatch.setattr(snapshots, "project_root", lambda: tmp_path)
    monkeypatch.setattr(main, "project_root", lambda: tmp_path)
    monkeypatch.setattr(main, "build_channel", lambda name: FakeTaskMarket())

    assert main.main(["снапшот", "taskmarket"]) == 0
    report = snapshots.read("snapshots/taskmarket-report.json", root=tmp_path)
    assert report.usable
    assert report.rows
    assert any("Quantum-Safe" in row["title"] for row in report.rows)


def test_snapshot_command_does_not_wipe_a_good_report(tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    """Один сбой сети на раннере не должен обнулять вчерашние задачи."""
    import json

    from agent import main, snapshots

    target = snapshot_file(tmp_path, 1.0, [{"id": "keep", "title": "Старая добрая задача",
                                            "reward_usd": 42.0}])
    monkeypatch.setattr(snapshots, "project_root", lambda: tmp_path)
    monkeypatch.setattr(main, "project_root", lambda: tmp_path)
    monkeypatch.setattr(main, "build_channel", lambda name: OfflineTaskMarket())

    before = json.loads(target.read_text(encoding="utf-8"))
    assert main.main(["снапшот", "taskmarket"]) == 1
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["count"] == 1 and payload["tasks"][0]["id"] == "keep"
    assert payload["generated_at"] == before["generated_at"], (
        "переписанный отчёт выдал бы старые данные за свежие"
    )


def test_dashboard_shows_the_platform_from_the_report(tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    from agent import dashboard, snapshots

    snapshot_file(tmp_path, 3.0, parse_tasks(PAGE))
    monkeypatch.setattr(snapshots, "project_root", lambda: tmp_path)
    dashboard._TASKMARKET["data"] = None
    dashboard._TASKMARKET["at"] = 0.0

    state = dashboard._taskmarket()
    assert state["task_count"] >= 3
    assert state["best_reward"] == 199.0
    assert state["free_slots"] >= 1
    assert not state["stale"]


def test_dashboard_names_a_missing_report(tmp_path: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    from agent import dashboard, snapshots

    monkeypatch.setattr(snapshots, "project_root", lambda: tmp_path)
    dashboard._TASKMARKET["data"] = None
    dashboard._TASKMARKET["at"] = 0.0
    state = dashboard._taskmarket()
    assert "отчёта" in state["problem"] and state["tasks"] == []


def test_cli_and_dashboard_agree_on_the_snapshot(tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    """Один и тот же файл: команда пишет, канал и дашборд читают то же самое."""
    from agent import main, snapshots
    monkeypatch.setattr(snapshots, "project_root", lambda: tmp_path)
    monkeypatch.setattr(main, "project_root", lambda: tmp_path)
    monkeypatch.setattr(main, "build_channel", lambda name: FakeTaskMarket())
    assert main.main(["снапшот", "taskmarket", "--json"]) == 0

    queued = OfflineTaskMarket().harvest()
    from agent import dashboard
    dashboard._TASKMARKET["data"] = None
    dashboard._TASKMARKET["at"] = 0.0
    shown = dashboard._taskmarket()
    ids_in_queue = {opp.id for opp in queued}
    ids_on_page = {f"taskmarket:{task['id']}" for task in shown["tasks"]}
    assert ids_in_queue & ids_on_page, "дашборд и очередь должны видеть одни задачи"


def test_unknown_channel_name_is_explained_not_a_traceback(capsys: pytest.CaptureFixture) -> None:
    from agent import main

    assert main.main(["снапшот", "agenthansa"]) == 2
    output = capsys.readouterr().out
    assert "Канала «agenthansa» нет" in output
    assert "taskmarket" in output, "человеку нужен список существующих каналов"


# --- шаги для человека: у площадки свой маршрут --------------------------------

def test_steps_open_the_task_instead_of_a_github_tool() -> None:
    from agent.farm import _steps_for

    steps = _steps_for("taskmarket", "taskmarket:0xabc", {
        "url": "https://taskmarket.dev/tasks/0xabc", "source": "snapshot",
        "reward_usd": 199.0,
    })
    text = " ".join(steps)
    assert "taskmarket.dev/tasks/0xabc" in text
    assert "черновик taskmarket:0xabc" in text
    assert "человек отправляет работу" in text
    assert "досье" not in text and "план" not in text, "у площадки нет репозитория"


def test_steps_warn_that_the_snapshot_may_be_stale() -> None:
    from agent.farm import _steps_for

    steps = _steps_for("taskmarket", "taskmarket:0xabc", {"source": "snapshot"})
    assert any("сверьте награду и срок" in step for step in steps)
    live = _steps_for("taskmarket", "taskmarket:0xabc", {"source": "live"})
    assert not any("сверьте награду" in step for step in live)


def test_snapshot_run_kind_is_named_in_the_note(tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from agent import snapshots

    target = tmp_path / "snapshots" / "taskmarket-report.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({
        "channel": "taskmarket", "run": "manual", "generated_at": "2026-09-17T10:00:00+00:00",
        "tasks": [],
    }), encoding="utf-8")
    report = snapshots.read("snapshots/taskmarket-report.json", root=tmp_path)
    assert "снятого вручную" in report.note("задачи")


def test_scheduled_report_is_credited_to_actions(tmp_path: Path) -> None:
    import json

    from agent import snapshots

    target = tmp_path / "snapshots" / "hansa-report.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({
        "run": "scheduled", "generated_at": "2026-09-17T10:00:00+00:00", "quests": [],
    }), encoding="utf-8")
    report = snapshots.read("snapshots/hansa-report.json", root=tmp_path)
    assert "GitHub Actions" in report.note("квесты")
