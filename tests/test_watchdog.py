"""Дозор: робот сам замечает, что встал."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent import inbox, watchdog
from agent.ledger import record_run, set_state


@pytest.fixture(autouse=True)
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("DB_PATH", str(tmp_path / "watchdog.db"))
    monkeypatch.setenv("ELIGIBILITY_COUNTRY", "DE")
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(watchdog, "project_root", lambda: tmp_path)
    # дозор сам подгружает .env; в тестах это подменило бы подготовленное окружение
    monkeypatch.setattr(watchdog, "load_environment", lambda: None)
    return tmp_path


def keys(problems) -> set[str]:
    return {problem.key for problem in problems}


def test_silent_robot_is_critical() -> None:
    old = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat(timespec="seconds")
    set_state("autopilot.last_tick", old)
    problems = watchdog.inspect()
    assert "heartbeat" in keys(problems)
    assert watchdog.state()["status"] == "critical"


def test_fresh_heartbeat_is_not_a_problem() -> None:
    set_state("autopilot.last_tick", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    assert "heartbeat" not in keys(watchdog.inspect())


def test_a_channel_that_finds_nothing_for_many_runs_is_reported() -> None:
    for _ in range(6):
        record_run("github_bounties", "search", found=0, kept=0, policy="allowed",
                   note="0.5s, новых 0, триаж 0, отсеяно 0")
    assert "channel_quiet" in keys(watchdog.inspect())


def test_a_channel_without_a_key_is_not_a_problem(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTHANSA_API_KEY", raising=False)
    for _ in range(8):
        record_run("agent_marketplaces", "search", found=0, kept=0, policy="allowed",
                   note="нужен ключ")
    assert "channel_quiet" not in keys(watchdog.inspect())
    assert any("agent_marketplaces" in note for note in watchdog.notes())


def test_working_channel_is_not_reported() -> None:
    for _ in range(8):
        record_run("github_bounties", "search", found=5, kept=5, policy="allowed", note="ок")
    assert "channel_quiet" not in keys(watchdog.inspect())


def test_exhausted_quota_asks_for_a_token() -> None:
    set_state("github.rate_limit", {"remaining": 0, "limit": 60, "reset": None, "token": False})
    problems = {problem.key: problem for problem in watchdog.inspect()}
    assert "github_quota" in problems
    assert "GITHUB_TOKEN" in problems["github_quota"].advice


def test_quota_with_remaining_requests_is_silent() -> None:
    set_state("github.rate_limit", {"remaining": 12, "limit": 60, "reset": None, "token": False})
    assert "github_quota" not in keys(watchdog.inspect())


def test_work_waiting_for_a_human_is_nudged() -> None:
    item_id = inbox.add("market:agenthansa:q1", "agent_marketplaces", "quest", "Квест",
                        summary="$40")
    # состариваем запись: очередь ждёт человека уже сутки
    from agent.ledger import connect

    conn = connect()
    conn.execute("UPDATE inbox SET created_at = datetime('now', '-20 hours') WHERE id = ?",
                 (item_id,))
    conn.commit()
    conn.close()

    problems = {problem.key: problem for problem in watchdog.inspect()}
    assert "inbox_aging" in problems
    assert "входящие show" in problems["inbox_aging"].advice


def test_missing_country_is_critical(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ELIGIBILITY_COUNTRY", raising=False)
    problems = {problem.key: problem for problem in watchdog.inspect()}
    assert problems["no_country"].severity == "critical"


def test_clean_state_reports_ok() -> None:
    set_state("autopilot.last_tick", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    assert watchdog.state()["status"] == "ok"
    assert watchdog.nudge_lines() == []


# --- работающий процесс на старом коде ------------------------------------------

def hours_ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")


def test_stale_code_is_flagged_when_the_tree_changed_after_start(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Python держит модули в памяти: правки в дереве до процесса не доходят."""
    set_state("autopilot.process_started_at", hours_ago(3.0))
    problem = watchdog.check_stale_code(newest=lambda: datetime.now(timezone.utc).timestamp() - 3600)
    assert problem is not None
    assert problem.key == "stale_code"
    assert "перезапустить" in problem.advice


def test_fresh_process_is_not_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    set_state("autopilot.process_started_at", hours_ago(0.1))
    assert watchdog.check_stale_code(
        newest=lambda: datetime.now(timezone.utc).timestamp() - 3600) is None


def test_code_changed_before_start_is_not_flagged() -> None:
    set_state("autopilot.process_started_at", hours_ago(1.0))
    assert watchdog.check_stale_code(
        newest=lambda: datetime.now(timezone.utc).timestamp() - 3 * 3600) is None


def test_no_marker_means_no_claim() -> None:
    """Пока процесса не было, предупреждать не о чем — не выдумываем проблему."""
    assert watchdog.check_stale_code(newest=lambda: 0.0) is None


def test_newest_source_mtime_reads_the_agent_tree(tmp_path: Path) -> None:
    import os

    (tmp_path / "agent").mkdir()
    old = tmp_path / "agent" / "old.py"
    new = tmp_path / "agent" / "new.py"
    old.write_text("", encoding="utf-8")
    new.write_text("", encoding="utf-8")
    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))
    assert watchdog._newest_source_mtime(tmp_path) == 2000.0
