"""Исправленные ошибки: чтобы не вернулись.

Каждый тест здесь появился из настоящей поломки, найденной на живом прогоне,
а не из соображений красоты.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

from agent import autopilot, dashboard, farm, followup, learning, watchdog
from agent.ledger import Opportunity, connect, record_run, upsert_opportunities
from agent.triage import Triage


@pytest.fixture(autouse=True)
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "robust.db"
    monkeypatch.setenv("DB_PATH", str(path))
    monkeypatch.setenv("ELIGIBILITY_COUNTRY", "DE")
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "reports").mkdir(exist_ok=True)
    monkeypatch.setattr(watchdog, "project_root", lambda: tmp_path)
    monkeypatch.setattr(watchdog, "load_environment", lambda: None)
    monkeypatch.setattr(autopilot, "project_root", lambda: tmp_path)
    return path


# --- 1. ошибка канала должна попадать в журнал проходов -----------------------


def test_channel_error_is_recorded_for_the_watchdog(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingChannel:
        name = "agent_marketplaces"
        capability = "read"
        title = "Площадки"
        allowed = True
        decision = type("D", (), {"label": "разрешено", "reason": "", "requirements": []})()
        last_error = "GitHub вернул 403: лимит запросов исчерпан."
        notes: List[str] = []

        def harvest(self, limit: int = 0) -> List[Opportunity]:
            return []

    monkeypatch.setattr(farm, "build_channel", lambda name: FailingChannel())
    outcome = farm.run_channel("agent_marketplaces")

    assert outcome["error"], "ошибка канала должна быть в результате"
    conn = connect()
    row = conn.execute(
        "SELECT policy, note FROM runs WHERE channel='agent_marketplaces' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert row["policy"] == "error"
    assert "ошибка:" in row["note"]

    problems = {problem.key for problem in watchdog.inspect()}
    assert not problems  # одиночный сбой — ещё не повод для тревоги

    for _ in range(6):
        record_run("agent_marketplaces", "read", 0, 0, policy="error",
                   note="0.4s, новых 0, ошибка: 403 лимит")
    assert "channel_errors" in {problem.key for problem in watchdog.inspect()}


# --- 2. новые задачи считаются только по доступным ---------------------------


def test_finished_contest_is_not_counted_as_a_new_task(monkeypatch: pytest.MonkeyPatch) -> None:
    class ContestChannel:
        name = "audit_contests"
        capability = "read"
        title = "Контесты"
        allowed = True
        decision = type("D", (), {"label": "разрешено", "reason": "", "requirements": []})()
        last_error = ""
        notes: List[str] = []

        def harvest(self, limit: int = 0) -> List[Opportunity]:
            upsert_opportunities([])
            return [
                Opportunity(id="contest:old/2025-01-x", channel="audit_contests",
                            title="Старый контест", status="done", payload={"expired": True}),
            ]

    monkeypatch.setattr(farm, "build_channel", lambda name: ContestChannel())
    outcome = farm.run_channel("audit_contests")
    assert outcome["new"] == 0, "завершённый контест — не новая возможность"
    assert outcome["kept"] == 1, "но запись всё равно сохраняется"


# --- 3. обучение не растёт бесконечно ---------------------------------------


def test_observations_are_summed_per_day() -> None:
    for value in (2, 3, 5):
        learning.observe("label:bounty", value)
    stats = {item.subject: item for item in learning.query_stats()}
    assert stats["label:bounty"].new_items == 10
    assert stats["label:bounty"].observations == 3, "проходы считаются, но строк в базе — одна"

    conn = connect()
    rows = conn.execute(
        "SELECT COUNT(*) AS n FROM learning_events WHERE kind='observation' "
        "AND subject='label:bounty'"
    ).fetchone()
    conn.close()
    assert rows["n"] == 1, "за сутки по запросу хранится одна строка, а не сотни"


def test_decisions_are_never_merged() -> None:
    learning.decide("github:a/b#1", "published", channel="github_bounties")
    learning.decide("github:a/b#2", "skipped", channel="github_bounties")
    totals = learning.totals()
    assert totals["published"] == 1 and totals["skipped"] == 1


def test_pruning_keeps_decisions_and_outcomes() -> None:
    learning.observe("старый запрос", 5)
    learning.decide("github:a/b#3", "published", channel="github_bounties")
    learning.outcome("audit_contests", 100.0)

    conn = connect()
    conn.execute("UPDATE learning_events SET created_at = datetime('now', '-400 days')")
    conn.commit()
    conn.close()

    removed = learning.prune(days=120)
    assert removed == 1, "удаляются только наблюдения"
    totals = learning.totals()
    assert totals["published"] == 1
    assert totals["earned_usd"] == 100.0


def test_old_learning_table_gets_the_day_column() -> None:
    """База предыдущей версии должна открываться без ошибок."""
    import sqlite3

    from agent.ledger import db_path

    path = db_path()
    path.unlink(missing_ok=True)
    legacy = sqlite3.connect(str(path))
    legacy.execute(
        "CREATE TABLE learning_events (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, "
        "subject TEXT, channel TEXT, value REAL, detail TEXT, "
        "created_at TEXT DEFAULT CURRENT_TIMESTAMP)"
    )
    legacy.execute("INSERT INTO learning_events(kind, subject, value) VALUES('observation','q',4)")
    legacy.commit()
    legacy.close()

    learning.observe("q", 1)
    stats = {item.subject: item for item in learning.query_stats()}
    assert stats["q"].new_items >= 4


# --- 4. изменение в задаче доходит до уведомления ----------------------------


class FakeClient:
    def __init__(self, verdict: Triage) -> None:
        self.verdict = verdict

    def triage(self, **_: Any) -> Triage:
        return self.verdict


def triage_state(**overrides: Any) -> Triage:
    data: Dict[str, Any] = {
        "opportunity_id": "github:acme/parser#7", "repo": "acme/parser", "number": 7,
        "state": "open", "attempts": 1, "open_prs": 0, "stale_days": 1,
    }
    data.update(overrides)
    return Triage(**data)


def test_growing_competition_is_reported_not_swallowed() -> None:
    upsert_opportunities([
        Opportunity(id="github:acme/parser#7", channel="github_bounties",
                    title="Fix", reward_usd=100.0, payload={})
    ])
    from agent.ledger import set_status

    set_status("github:acme/parser#7", "working")
    followup.check(triage_client=FakeClient(triage_state(attempts=2)))   # базовый замер

    lines = followup.summary_lines(client=FakeClient(triage_state(attempts=8)))
    assert lines, "рост конкуренции должен попадать в уведомления"
    assert "заявок" in lines[0] or "внимательнее" in lines[0].lower()


def test_alerting_returns_only_actionable_verdicts() -> None:
    upsert_opportunities([
        Opportunity(id="github:acme/parser#7", channel="github_bounties",
                    title="Fix", reward_usd=100.0, payload={})
    ])
    from agent.ledger import set_status

    set_status("github:acme/parser#7", "working")
    assert followup.alerting(client=FakeClient(triage_state())) == []
    assert len(followup.alerting(client=FakeClient(triage_state(state="closed")))) == 1


# --- 5. дашборд не ходит в сеть на каждое обновление -------------------------


def test_dashboard_caches_followup(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def counting() -> Dict[str, Any]:
        calls["n"] += 1
        return {"watched": 0, "verdicts": {}, "items": []}

    monkeypatch.setattr(dashboard.followup_mod, "state", counting)
    dashboard._FOLLOWUP["data"] = None
    dashboard._FOLLOWUP["at"] = 0.0

    dashboard._followup()
    dashboard._followup()
    dashboard._followup()
    assert calls["n"] == 1, "повторные обращения должны брать кэш"


def test_dashboard_survives_a_broken_followup(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> Dict[str, Any]:
        raise RuntimeError("сеть недоступна")

    monkeypatch.setattr(dashboard.followup_mod, "state", broken)
    dashboard._FOLLOWUP["data"] = None
    dashboard._FOLLOWUP["at"] = 0.0
    data = dashboard._followup()
    assert data["watched"] == 0
    assert "сеть недоступна" in data["error"]


# --- 6. база переживает большие выдачи --------------------------------------


def test_large_batch_upsert_does_not_hit_sqlite_limits() -> None:
    items = [
        Opportunity(id=f"github:acme/parser#{n}", channel="github_bounties",
                    title=f"Задача {n}", reward_usd=10.0, payload={})
        for n in range(1200)
    ]
    assert upsert_opportunities(items, fresh_only=True) == 1200
    assert upsert_opportunities(items, fresh_only=True) == 0

    conn = connect()
    total = conn.execute("SELECT COUNT(*) AS n FROM opportunities").fetchone()["n"]
    conn.close()
    assert total == 1200


def test_migration_does_not_rewrite_every_row_on_each_connect() -> None:
    upsert_opportunities([
        Opportunity(id="github:acme/parser#1", channel="github_bounties",
                    title="Задача", payload={})
    ])
    conn = connect()
    conn.execute("UPDATE opportunities SET last_seen = '2000-01-01T00:00:00+00:00'")
    conn.commit()
    conn.close()

    connect()  # повторное подключение не должно переписывать строки
    conn = connect()
    row = conn.execute("SELECT last_seen FROM opportunities").fetchone()
    conn.close()
    assert row["last_seen"] == "2000-01-01T00:00:00+00:00"


def test_smoke_reference_time_is_recent() -> None:
    """Заглушка на будущее: тесты не должны зависеть от часов машины."""
    assert datetime.now(timezone.utc).year >= 2026


# --- 7. интерфейс дашборда не должен ломаться на неполном ответе API ----------

NODE = shutil.which("node")
DASHBOARD_JS = re.search(r"<script>(.*?)</script>", dashboard.PAGE, re.S).group(1)


def run_dashboard_js(state: Dict[str, Any], tmp_path: Path) -> "subprocess.CompletedProcess[str]":
    harness = tmp_path / "ui-check.js"
    harness.write_text(
        "const document = {getElementById: () => ({innerHTML: '', style: {}, className: '', "
        "textContent: '', value: '', addEventListener: () => {}, disabled: false}), "
        "querySelectorAll: () => [], addEventListener: () => {}};\n"
        "const navigator = {clipboard: {writeText: async () => {}}};\n"
        "const STATE = " + json.dumps(state, ensure_ascii=False) + ";\n"
        "const fetch = async () => ({json: async () => STATE, text: async () => ''});\n"
        + DASHBOARD_JS +
        "\nrefresh().then(() => process.exit(0), (e) => { console.error(e); process.exit(1); });\n",
        encoding="utf-8",
    )
    return subprocess.run([NODE, str(harness)], capture_output=True, text=True, timeout=60)


@pytest.mark.skipif(NODE is None, reason="нужен node для проверки интерфейса")
def test_dashboard_renders_a_full_state(tmp_path: Path) -> None:
    result = run_dashboard_js(dashboard.collect_state(), tmp_path)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(NODE is None, reason="нужен node для проверки интерфейса")
@pytest.mark.parametrize("broken", [
    {"ledger": {"counters": {}}},                                  # сервер отдал огрызок
    {"ledger": {}, "stats": None, "runner": {}, "farm": {},        # нет статистики
     "queue": [], "next": [], "low_value": [], "contests": []},
])
def test_dashboard_survives_a_partial_state(broken: Dict[str, Any], tmp_path: Path) -> None:
    result = run_dashboard_js(broken, tmp_path)
    assert result.returncode == 0, result.stderr
