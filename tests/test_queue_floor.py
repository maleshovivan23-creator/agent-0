"""«Дальше» не должно врать про пустую очередь.

Порог ``MIN_EV_PER_HOUR`` отсекает задачи, которые не окупают время. Когда его не
проходит ни одна задача, прежний ответ «Очередь пуста. Запустите цикл» был
неправдой: в базе лежали десятки задач, а человек шёл перезапускать сбор. Теперь
команда объясняет расклад: сколько задач, сколько из них без оценки, что стоит
ближе всего к порогу и что делать.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from agent import farm, main
from agent.ledger import Opportunity, upsert_opportunities


def queued(ident: str, *, reward: float = 750.0, ev: float = 1.08,
           title: str = "[PAID BOUNTY] Email Threads API") -> Opportunity:
    payload: Dict[str, Any] = {"playbook": "generic"}
    if ev is not None:
        payload["ev_per_hour"] = ev
    return Opportunity(id=ident, channel="github_bounties", title=title,
                       reward_usd=reward, payload=payload)


def test_floor_report_counts_what_the_threshold_hid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIN_EV_PER_HOUR", "3")
    upsert_opportunities([
        queued("github:acme/one#1", ev=1.08),
        queued("github:acme/two#2", ev=1.55),
        queued("github:acme/three#3", ev=None),  # триаж не проходил
        queued("github:acme/well#4", ev=12.0),
    ])
    report = farm.floor_report()
    assert report["floor"] == 3.0
    assert report["queued"] == 4
    assert report["above"] == 1
    assert report["below"] == 3
    assert report["unmeasured"] == 1
    assert [item["ev_per_hour"] for item in report["near"]][:3] == [1.55, 1.08, 0.0], (
        "ближайшие к порогу идут по убыванию ценности часа"
    )


def test_floor_report_respects_the_configured_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    upsert_opportunities([queued("github:acme/one#1", ev=1.08)])
    monkeypatch.setenv("MIN_EV_PER_HOUR", "1")
    assert farm.floor_report()["above"] == 1
    monkeypatch.setenv("MIN_EV_PER_HOUR", "5")
    assert farm.floor_report()["above"] == 0


def test_next_actions_returns_nothing_below_the_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIN_EV_PER_HOUR", "3")
    upsert_opportunities([queued("github:acme/one#1", ev=1.08)])
    assert farm.next_actions() == []


def test_next_says_the_truth_instead_of_queue_is_empty(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setenv("MIN_EV_PER_HOUR", "3")
    upsert_opportunities([
        queued("github:acme/one#1", ev=1.55, title="[PAID BOUNTY - $960] Attachment Summarizer"),
        queued("github:acme/two#2", ev=1.08),
    ])
    assert main.main(["дальше"]) == 0
    output = capsys.readouterr().out
    assert "Очередь пуста" not in output, "в очереди есть задачи — так говорить нельзя"
    assert "не проходит порог $3.00/час" in output
    assert "Attachment Summarizer" in output, "ближайшая к порогу задача должна быть видна"
    assert "MIN_EV_PER_HOUR" in output, "человек должен знать, где порог настраивается"


def test_empty_queue_still_says_to_run_the_cycle(capsys: pytest.CaptureFixture) -> None:
    assert main.main(["дальше"]) == 0
    output = capsys.readouterr().out
    assert "Очередь пуста" in output
    assert "python -m agent.main цикл" in output, "подсказка должна быть на русском"


def test_unmeasured_tasks_are_named_as_unmeasured(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setenv("MIN_EV_PER_HOUR", "3")
    upsert_opportunities([queued("github:acme/one#1", ev=None)])
    assert main.main(["дальше"]) == 0
    output = capsys.readouterr().out
    assert "без оценки ценности" in output, "нулевой EV и невыясненный EV — разные вещи"
