"""Многозадачный режим: несколько заказов сразу, свои робот берёт сам.

Здесь проверяется то, из-за чего такой режим вообще имеет смысл: задачи идут
параллельно, одна сломанная не роняет остальные, робот берёт только то, что
действительно доводит до готового текста, и ни при каких условиях модуль не
трогает выплаты.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

from agent import multitask
from agent.ledger import Opportunity, connect, get_opportunity, upsert_opportunities


def quest(ident: str, *, reward: float = 120.0, ev: float = 8.0,
          channel: str = "agent_marketplaces", title: str = "Квест") -> Opportunity:
    return Opportunity(id=ident, channel=channel, title=title, reward_usd=reward,
                       payload={"quest_id": ident.split(":")[-1], "ev_per_hour": ev,
                                "effort_hours": 2.0})


def item(ident: str, kind: str = "quest", title: str = "Квест") -> Dict[str, Any]:
    return {"id": ident, "title": title, "channel": "agent_marketplaces",
            "kind": kind, "reward_usd": 100.0, "ev_per_hour": 10.0}


# --- кто закрывает задачу ------------------------------------------------------

def test_robot_takes_the_channels_it_can_finish() -> None:
    owner, reason = multitask.capability("agent_marketplaces", {})
    assert owner == "auto" and "человек отправляет" in reason
    owner, reason = multitask.capability("github_bounties", {})
    assert owner == "auto" and "человек публикует" in reason


def test_recon_needs_a_human() -> None:
    owner, reason = multitask.capability("bug_recon", {})
    assert owner == "human"
    assert "scope.yaml" in reason


def test_closed_task_is_not_taken() -> None:
    owner, reason = multitask.capability("agent_marketplaces", {"expired": True})
    assert owner == "human" and "закрыто" in reason


def test_unknown_channel_is_not_taken_silently() -> None:
    owner, reason = multitask.capability("mystery_channel", {})
    assert owner == "human" and "нет автоматического маршрута" in reason


# --- план ----------------------------------------------------------------------

def test_plan_takes_several_tasks_and_respects_the_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIN_EV_PER_HOUR", "3")
    upsert_opportunities([
        quest("market:agenthansa:one", ev=9.0),
        quest("market:agenthansa:two", ev=7.0),
        quest("market:agenthansa:three", ev=5.0),
        quest("market:agenthansa:cheap", ev=0.5),
    ])
    plan = multitask.plan(limit=2)
    taken = [entry["id"] for entry in plan["items"]]
    assert taken == ["market:agenthansa:one", "market:agenthansa:two"], taken
    assert all(entry["owner"] == "robot" or entry["owner"] == "робот" or "робот" in entry["reason"]
               for entry in plan["items"])
    assert plan["floor"] == 3.0


def test_plan_separates_what_is_left_to_a_human() -> None:
    upsert_opportunities([quest("market:agenthansa:one", ev=9.0)])
    plan = multitask.plan(limit=1)
    assert plan["items"], "робот должен взять хотя бы одну задачу"
    for entry in plan["left_to_human"]:
        assert entry["owner"] == "human" and entry["reason"]


def test_plan_is_empty_when_nothing_is_worth_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIN_EV_PER_HOUR", "50")
    upsert_opportunities([quest("market:agenthansa:one", ev=5.0)])
    plan = multitask.plan(limit=3)
    assert plan["items"] == []
    assert plan["queued"] >= 1


# --- параллельный проход -------------------------------------------------------

def test_tasks_run_in_parallel() -> None:
    """Три задачи по 0.3 с параллельно должны уложиться заметно быстрее 0.9 с."""
    started = threading.Event()
    concurrent = {"now": 0, "peak": 0}
    lock = threading.Lock()

    def worker(work: multitask.WorkItem) -> multitask.WorkResult:
        with lock:
            concurrent["now"] += 1
            concurrent["peak"] = max(concurrent["peak"], concurrent["now"])
        started.wait(0)
        time.sleep(0.3)
        with lock:
            concurrent["now"] -= 1
        return multitask.WorkResult(item_id=work.id, kind=work.kind, ok=True)

    outcome = multitask.run([item(f"market:agenthansa:q{i}") for i in range(3)],
                            parallel=3, worker=worker, take=False, journal=False)
    assert outcome["ok"] == 3
    assert concurrent["peak"] == 3, f"задачи не пошли параллельно: пик {concurrent['peak']}"
    assert outcome["seconds"] < 0.85, outcome["seconds"]


def test_one_broken_task_does_not_stop_the_others() -> None:
    def worker(work: multitask.WorkItem) -> multitask.WorkResult:
        if work.id.endswith("bad"):
            raise RuntimeError("сломалось")
        return multitask.WorkResult(item_id=work.id, kind=work.kind, ok=True, path="reports/x.md")

    outcome = multitask.run([item("market:agenthansa:ok"), item("market:agenthansa:bad")],
                            parallel=2, worker=worker, take=False, journal=False)
    assert outcome["ok"] == 1 and outcome["failed"] == 1
    broken = [r for r in outcome["results"] if not r["ok"]][0]
    assert "сломалось" in broken["error"]


def test_timeout_is_reported_not_hidden() -> None:
    def worker(work: multitask.WorkItem) -> multitask.WorkResult:
        time.sleep(3)
        return multitask.WorkResult(item_id=work.id, kind=work.kind, ok=True)

    outcome = multitask.run([item("market:agenthansa:slow")], parallel=1,
                            timeout=0.2, worker=worker, take=False, journal=False)
    assert outcome["failed"] == 1
    assert "не уложился" in outcome["results"][0]["error"]


def test_taking_a_task_marks_it_working() -> None:
    upsert_opportunities([quest("market:agenthansa:take")])

    def worker(work: multitask.WorkItem) -> multitask.WorkResult:
        return multitask.WorkResult(item_id=work.id, kind=work.kind, ok=True)

    multitask.run([item("market:agenthansa:take")], parallel=1, worker=worker,
                  take=True, journal=False)
    assert get_opportunity("market:agenthansa:take")["status"] == "working"


def test_dry_run_does_not_touch_the_queue() -> None:
    upsert_opportunities([quest("market:agenthansa:dry", ev=9.0)])
    multitask.command(limit=1, dry_run=True)
    assert get_opportunity("market:agenthansa:dry")["status"] == "queued"


# --- журнал --------------------------------------------------------------------

def test_journal_is_written_in_russian_with_the_human_step(tmp_path: Path,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(multitask, "project_root", lambda: tmp_path)
    journal = multitask.write_journal({
        "started_at": "2026-09-17T12:00:00+00:00", "seconds": 4.2, "parallel": 3,
        "taken": ["a", "b"], "ok": 2, "failed": 0,
        "results": [{"id": "a", "kind": "quest", "ok": True, "seconds": 2.0,
                     "path": "reports/quest-a.md", "note": "", "error": "",
                     "channel": "agent_marketplaces"}],
    })
    text = journal.read_text(encoding="utf-8")
    assert "Многозадачный проход" in text
    assert "площадка отправить" in text, "человеку нужна точная команда отправки"
    assert "выплата-подтвердить" in text
    assert "Часы робота и часы человека считаются отдельно" in text
    assert "reports/quest-a.md" in text


def test_state_counts_what_waits_for_a_human() -> None:
    from agent import inbox

    inbox.add(opportunity_id="market:agenthansa:wait", channel="agent_marketplaces",
              kind="quest", title="Готовый черновик", path="reports/quest-wait.md",
              summary="готово")
    state = multitask.state()
    assert state["waiting_for_human"] >= 1
    assert "quest" in state["kinds"]


# --- границы -------------------------------------------------------------------

def test_the_module_never_records_payouts() -> None:
    """Многозадачность не имеет права трогать деньги: выплату подтверждает человек."""
    source = Path("agent/multitask.py").read_text(encoding="utf-8")
    assert "record_payout" not in source
    assert "verify_payout" not in source

    def payout_rows() -> List[tuple]:
        return [tuple(row) for row in connect().execute(
            "SELECT id, channel, amount, verified FROM payouts ORDER BY id").fetchall()]

    before = payout_rows()
    upsert_opportunities([quest("market:agenthansa:money", ev=9.0)])

    def worker(work: multitask.WorkItem) -> multitask.WorkResult:
        return multitask.WorkResult(item_id=work.id, kind=work.kind, ok=True)

    multitask.run([item("market:agenthansa:money")], parallel=1, worker=worker,
                  take=True, journal=False)
    assert payout_rows() == before


def test_cli_plan_shows_nothing_to_do_without_lying(capsys: pytest.CaptureFixture,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    from agent import main

    monkeypatch.setenv("MIN_EV_PER_HOUR", "500")
    upsert_opportunities([quest("market:agenthansa:cli", ev=5.0)])
    assert main.main(["потоки", "--plan"]) == 0
    output = capsys.readouterr().out
    assert "Робот ничего не берёт" in output
    assert "выше порога" in output


def test_cli_status_is_honest_about_parallelism(capsys: pytest.CaptureFixture) -> None:
    from agent import main

    assert main.main(["потоки", "--status"]) == 0
    output = capsys.readouterr().out
    assert "одновременно до" in output
    assert json.dumps(output[:20])  # вывод — текст, не падение


def test_human_step_matches_the_channel(tmp_path: Path,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """Для GitHub-задачи нельзя подсказывать команду площадки — это чужой канал."""
    monkeypatch.setattr(multitask, "project_root", lambda: tmp_path)
    journal = multitask.write_journal({
        "started_at": "2026-09-17T13:00:00+00:00", "seconds": 1.0, "parallel": 2,
        "taken": ["github:acme/parser#7"], "ok": 1, "failed": 0,
        "results": [{"id": "github:acme/parser#7", "kind": "application", "ok": True,
                     "seconds": 1.0, "path": "reports/inbox/app.md", "note": "",
                     "error": "", "channel": "github_bounties"}],
    })
    text = journal.read_text(encoding="utf-8")
    assert "Опубликовать заявку в задаче на GitHub" in text
    assert "площадка отправить" not in text, "чужая команда только путает"


def test_journal_keeps_the_earlier_passes_of_the_day(tmp_path: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(multitask, "project_root", lambda: tmp_path)
    base = {"seconds": 1.0, "parallel": 1, "taken": [], "ok": 0, "failed": 0, "results": []}
    first = multitask.write_journal({**base, "started_at": "2026-09-17T08:00:00+00:00"})
    second = multitask.write_journal({**base, "started_at": "2026-09-17T18:00:00+00:00"})
    assert first == second, "за день должен оставаться один файл"
    text = second.read_text(encoding="utf-8")
    assert "08:00:00" in text and "18:00:00" in text, "прошлые проходы не должны пропадать"


def test_cli_names_the_right_human_step(capsys: pytest.CaptureFixture,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """Подсказка после прохода должна соответствовать каналу задачи."""
    from agent import main

    monkeypatch.setenv("MIN_EV_PER_HOUR", "1")
    monkeypatch.setenv("ELIGIBILITY_COUNTRY", "DE")
    monkeypatch.setenv("LLM_PROVIDER", "none")
    upsert_opportunities([quest("github:acme/parser#7", ev=5.0, channel="github_bounties",
                                title="Fix the tokenizer")])
    assert main.main(["потоки", "--limit", "1", "--parallel", "1"]) == 0
    output = capsys.readouterr().out
    assert "Опубликовать заявку в задаче на GitHub" in output
    assert "площадка отправить" not in output
