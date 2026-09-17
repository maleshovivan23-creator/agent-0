"""Autopilot: cadence, guardrails and honest stopping."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

from agent import autopilot, config, inbox
from agent.ledger import Opportunity, set_state, upsert_opportunities


@pytest.fixture(autouse=True)
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("DB_PATH", str(tmp_path / "auto.db"))
    monkeypatch.setattr(autopilot, "project_root", lambda: tmp_path)
    # artifact paths are stored relative to the project root, so both readers
    # (autopilot writes, inbox reads) must agree on that root
    monkeypatch.setattr(config, "project_root", lambda: tmp_path)
    (tmp_path / "reports").mkdir()
    (tmp_path / "data").mkdir()
    return tmp_path


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test runs on a controlled harvest result."""
    class Result:
        found = 0
        kept = 0
        triaged = 0
        pipeline_ev_usd = 0.0
        notes: List[str] = []
        blocked: List[Dict[str, Any]] = []

    monkeypatch.setattr(autopilot, "run_cycle", lambda *a, **k: Result())


def test_kill_switch_stops_everything(workspace: Path) -> None:
    (workspace / "data" / autopilot.KILL_SWITCH_NAME).write_text("stop", encoding="utf-8")
    assert not autopilot.enabled()
    assert autopilot.KILL_SWITCH_NAME in autopilot.stop_reason()
    result = autopilot.tick()
    assert result.stopped
    assert autopilot.health()["status"] == "stopped"


def test_env_switch_stops_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOPILOT_ENABLED", "false")
    assert not autopilot.enabled()


def test_quiet_market_backs_off_and_new_work_speeds_up(monkeypatch: pytest.MonkeyPatch) -> None:
    low, high = autopilot.intervals()
    autopilot.tick()
    first = float(autopilot.status()["interval"])
    assert first > low, "пустой проход должен увеличить паузу"

    monkeypatch.setattr(
        autopilot, "prepare",
        lambda limit=None: ([{"inbox_id": 1, "kind": "quest", "opportunity_id": "x",
                              "title": "Квест", "path": "reports/inbox/q.md"}], []),
    )
    autopilot.tick()
    assert float(autopilot.status()["interval"]) == low
    assert autopilot.intervals()[1] == high


def test_prepare_gates_on_the_payout_rail(monkeypatch: pytest.MonkeyPatch) -> None:
    """An application is not written when the money cannot reach the operator."""
    upsert_opportunities([
        Opportunity(id="github:org/repo#7", channel="github_bounties", title="Fix",
                    reward_usd=200.0, payload={"ev_per_hour": 30.0, "triage_verdict": "ready"})
    ])
    monkeypatch.setattr(
        autopilot, "next_actions",
        lambda limit=5, min_ev_per_hour=None: [{"id": "github:org/repo#7"}],
    )
    monkeypatch.delenv("ELIGIBILITY_COUNTRY", raising=False)

    prepared, skipped = autopilot.prepare()
    assert prepared == []
    assert any("страна" in note for note in skipped)
    assert inbox.counts()["ready"] == 0


def test_prepare_creates_one_actionable_item_per_task(
    monkeypatch: pytest.MonkeyPatch, workspace: Path
) -> None:
    _fake_dossier(monkeypatch, workspace)
    upsert_opportunities([
        Opportunity(id="github:org/repo#7", channel="github_bounties", title="Fix the parser",
                    reward_usd=200.0, payload={"ev_per_hour": 30.0, "triage_verdict": "ready",
                                               "playbook": "docs"}),
    ])
    monkeypatch.setattr(
        autopilot, "next_actions",
        lambda limit=5, min_ev_per_hour=None: [{"id": "github:org/repo#7"}],
    )
    monkeypatch.setenv("ELIGIBILITY_COUNTRY", "DE")

    prepared, skipped = autopilot.prepare()
    assert len(prepared) == 1
    assert prepared[0]["kind"] == "application"
    item = inbox.pending()[0]
    assert "/attempt" in inbox.text(item["id"])
    assert item["action"].startswith("Опубликовать")


def _fake_dossier(monkeypatch: pytest.MonkeyPatch, workspace: Path) -> None:
    """Досье в тестах не ходит в сеть: проверяем логику очереди, а не GitHub."""
    from types import SimpleNamespace

    def fake_save(opportunity: Dict[str, Any]) -> Any:
        path = workspace / "reports" / "inbox" / "dossier-fake.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Досье", encoding="utf-8")
        return SimpleNamespace(repo="acme/parser", candidates=[], commands=[],
                               total_files=3), path

    monkeypatch.setattr(autopilot.dossier_mod, "save", fake_save)


def test_prepare_does_not_duplicate_work(monkeypatch: pytest.MonkeyPatch, workspace: Path) -> None:
    _fake_dossier(monkeypatch, workspace)
    upsert_opportunities([
        Opportunity(id="github:org/repo#7", channel="github_bounties", title="Fix",
                    reward_usd=200.0, payload={"ev_per_hour": 30.0, "triage_verdict": "ready"})
    ])
    monkeypatch.setattr(
        autopilot, "next_actions",
        lambda limit=5, min_ev_per_hour=None: [{"id": "github:org/repo#7"}],
    )
    monkeypatch.setenv("ELIGIBILITY_COUNTRY", "DE")
    autopilot.prepare()
    prepared, skipped = autopilot.prepare()
    assert prepared == []
    assert any("уже в очереди" in note for note in skipped), skipped
    # заявка и досье к ней: повторный проход не должен добавлять новые элементы
    kinds = sorted(item["kind"] for item in inbox.pending())
    assert kinds == ["application", "dossier"]


def test_status_and_health_track_the_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELIGIBILITY_COUNTRY", "DE")
    assert autopilot.status()["state"] == "idle"
    assert autopilot.health()["status"] == "degraded"

    set_state("autopilot.last_tick", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    assert autopilot.status()["state"] == "running"
    assert autopilot.health()["status"] == "ok"


def test_shift_report_names_the_next_human_action(monkeypatch: pytest.MonkeyPatch) -> None:
    inbox.add("market:agenthansa:q1", "agent_marketplaces", "quest", "Квест про таблицы",
              summary="$40 · 320 слов")
    report = autopilot.shift_report(12)
    assert "Смена за последние 12 ч" in report
    assert "Квест про таблицы" in report
    assert "входящие show" in report
    assert autopilot.KILL_SWITCH_NAME in report
