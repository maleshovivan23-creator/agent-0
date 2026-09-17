"""Ledger behaviour: income only counts when a human verified it."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent import ledger


@pytest.fixture(autouse=True)
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(path))
    return path


def test_claimed_payout_is_not_income() -> None:
    payout_id = ledger.record_payout("github_bounties", 150.0, evidence="tx 0xdead")
    data = ledger.summary()
    assert data["verified_usd"] == 0.0
    assert data["claimed_usd"] == 150.0

    assert ledger.verify_payout(payout_id) is True
    data = ledger.summary()
    assert data["verified_usd"] == 150.0
    assert data["claimed_usd"] == 0.0


def test_verify_unknown_payout_fails() -> None:
    assert ledger.verify_payout(4242) is False


def test_opportunities_upsert_and_rank() -> None:
    items = [
        ledger.Opportunity(
            id="github:a/b#1", channel="github_bounties", title="Small", reward_usd=50, score=1.0
        ),
        ledger.Opportunity(
            id="github:a/b#2", channel="github_bounties", title="Big", reward_usd=900, score=9.0
        ),
    ]
    assert ledger.upsert_opportunities(items) == 2
    # Re-inserting the same id must update rather than duplicate.
    items[0].score = 3.5
    ledger.upsert_opportunities(items)

    rows = ledger.top_opportunities(limit=10)
    assert len(rows) == 2
    assert rows[0]["id"] == "github:a/b#2"
    assert rows[1]["score"] == 3.5


def test_policy_blocks_are_recorded() -> None:
    ledger.record_action("faucet_farm", "faucet_claim_automation", "-", False, "denied")
    data = ledger.summary()
    assert data["policy_blocks"]
    assert data["policy_blocks"][0]["capability"] == "faucet_claim_automation"


def test_run_history() -> None:
    ledger.upsert_opportunities([
        ledger.Opportunity(
            id="github:a/b#3", channel="github_bounties", title="Task",
            reward_usd=120, score=4.2,
        )
    ])
    ledger.record_run("github_bounties", "read_public_data", found=12, kept=12, note="3.1s")
    data = ledger.summary()
    assert data["runs"][0]["found"] == 12
    assert data["channels"][0]["channel"] == "github_bounties"


# --- путь к базе: от корня проекта, а не от текущего каталога ------------------

def test_relative_db_path_follows_the_project_root(tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """Запуск из чужого каталога раньше создавал рядом вторую пустую базу.

    Из-за этого ферма «теряла» историю часов и выплат, а тесты, запущенные не из
    корня, писали свои выдуманные задачи прямо в рабочую базу.
    """
    monkeypatch.delenv("DB_PATH", raising=False)
    elsewhere = tmp_path / "another-place"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    resolved = ledger.db_path()
    assert resolved.is_absolute()
    assert resolved.name == "agent.db"
    assert not (elsewhere / "data").exists(), "база не должна появляться рядом с cwd"
    assert ledger.project_root() in resolved.parents


def test_relative_db_path_from_env_is_also_project_relative(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_PATH", "data/custom.db")
    monkeypatch.chdir(tmp_path)
    resolved = ledger.db_path()
    assert resolved == ledger.project_root() / "data" / "custom.db"


def test_absolute_db_path_is_respected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "probe.db"
    monkeypatch.setenv("DB_PATH", str(target))
    assert ledger.db_path() == target


def test_tests_never_touch_the_live_database() -> None:
    """Общий conftest подменяет корень проекта — база тестов едет вместе с ним.

    Именно эта проверка поймала бы исходную поломку: раньше тесты писали
    выдуманные задачи в рабочую базу, и робот показывал их как настоящие.
    """
    live = Path(__file__).resolve().parent.parent / "data" / "agent.db"
    assert ledger.db_path() != live
    assert live not in ledger.db_path().parents


def test_dropped_keeps_the_reason(tmp_path, monkeypatch) -> None:
    """Отсев без причины через месяц неотличим от забытой задачи."""
    from agent.ledger import (Opportunity, connect, get_opportunity, set_status,
                              upsert_opportunities)

    monkeypatch.setenv("DB_PATH", str(tmp_path / "l.db"))
    upsert_opportunities([Opportunity(
        id="taskmarket:0xabc", channel="taskmarket", title="GPU-конкурс",
        reward_usd=199.0, rationale="EV/час 17.0",
    )])
    assert set_status("taskmarket:0xabc", "dropped", note="нужен NVIDIA GPU") is True

    row = get_opportunity("taskmarket:0xabc")
    assert row["status"] == "dropped"
    assert "EV/час 17.0" in row["rationale"]          # прежнее обоснование цело
    assert "нужен NVIDIA GPU" in row["rationale"]     # и причина рядом
    conn = connect()
    try:
        queued = conn.execute(
            "SELECT COUNT(*) FROM opportunities WHERE status='queued'").fetchone()[0]
    finally:
        conn.close()
    assert queued == 0, "отсеянная задача не должна снова попадать в рекомендации"
