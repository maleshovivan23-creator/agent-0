"""Новизна: робот должен отличать «уже видели» от «появилось сейчас»."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.ledger import (
    Opportunity,
    age_hours,
    fresh_opportunities,
    connect,
    upsert_opportunities,
)


@pytest.fixture(autouse=True)
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "novelty.db"
    monkeypatch.setenv("DB_PATH", str(path))
    return path


def item(opportunity_id: str = "github:acme/tool#1", reward: float = 200.0) -> Opportunity:
    return Opportunity(
        id=opportunity_id, channel="github_bounties", title="Fix the parser",
        reward_usd=reward, payload={"ev_per_hour": 20.0},
    )


def test_first_sighting_counts_as_new_and_repeat_does_not() -> None:
    assert upsert_opportunities([item()], fresh_only=True) == 1
    assert upsert_opportunities([item()], fresh_only=True) == 0
    assert upsert_opportunities([item()], fresh_only=True) == 0


def test_full_mode_still_reports_every_touched_row() -> None:
    upsert_opportunities([item()])
    assert upsert_opportunities([item()]) == 1


def test_first_seen_survives_later_updates(tmp_path: Path) -> None:
    upsert_opportunities([item()])
    conn = connect()
    first = conn.execute("SELECT first_seen FROM opportunities").fetchone()["first_seen"]
    conn.close()

    upsert_opportunities([item(reward=250.0)])
    conn = connect()
    row = conn.execute("SELECT first_seen, reward_usd FROM opportunities").fetchone()
    conn.close()

    assert row["first_seen"] == first, "момент появления не должен сдвигаться"
    assert row["reward_usd"] == 250.0, "данные при этом обновляются"


def test_fresh_list_shows_only_recent_appearances(tmp_path: Path) -> None:
    upsert_opportunities([item("github:acme/tool#1"), item("github:acme/tool#2")])
    conn = connect()
    conn.execute(
        "UPDATE opportunities SET first_seen = datetime('now', '-10 hours') "
        "WHERE id = 'github:acme/tool#1'"
    )
    conn.commit()
    conn.close()

    fresh = [row["id"] for row in fresh_opportunities(minutes=180)]
    assert fresh == ["github:acme/tool#2"]
    assert len(fresh_opportunities(minutes=1440)) == 2


def test_age_hours_handles_empty_and_broken_values() -> None:
    assert age_hours(None) is None
    assert age_hours("не дата") is None
    assert age_hours("2020-01-01T00:00:00+00:00") > 24 * 300


def test_migration_adds_columns_to_an_old_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """База прошлой версии должна открываться и получать новые столбцы."""
    import sqlite3

    path = tmp_path / "old.db"
    legacy = sqlite3.connect(str(path))
    legacy.execute(
        "CREATE TABLE opportunities (id TEXT PRIMARY KEY, channel TEXT, title TEXT, "
        "reward_usd REAL, score REAL, status TEXT, payload TEXT, fetched_at TEXT)"
    )
    legacy.execute(
        "INSERT INTO opportunities(id, channel, title, status, fetched_at) "
        "VALUES('old', 'github_bounties', 'старая задача', 'queued', '2026-01-01T00:00:00+00:00')"
    )
    legacy.commit()
    legacy.close()

    monkeypatch.setenv("DB_PATH", str(path))
    conn = connect()
    row = conn.execute("SELECT first_seen, last_seen FROM opportunities").fetchone()
    conn.close()

    assert row["first_seen"] == "2026-01-01T00:00:00+00:00"
    assert row["last_seen"] == "2026-01-01T00:00:00+00:00"
