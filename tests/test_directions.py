"""The three directions must stay in sync with the registered channels."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent import directions
from agent.channels import ACTIVE_CHANNELS
from agent.ledger import Opportunity, log_hours, upsert_opportunities


@pytest.fixture(autouse=True)
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "directions.db"
    monkeypatch.setenv("DB_PATH", str(path))
    return path


def test_three_directions_exist_and_match_channels() -> None:
    keys = [item.channel for item in directions.DIRECTIONS]
    assert len(keys) == 3
    for channel in keys:
        assert channel in ACTIVE_CHANNELS, f"{channel} must be a registered worker"


def test_every_direction_states_its_entry_cost_and_ceiling() -> None:
    for item in directions.DIRECTIONS:
        assert item.promise and item.entry_cost and item.ceiling, item.key
        assert item.payout_rail, item.key
        assert item.next_step, item.key


def test_allocation_never_zeroes_out_a_direction() -> None:
    data = directions.portfolio()
    shares = [item["share"] for item in data["directions"]]
    assert len(shares) == 3
    assert all(share > 0 for share in shares), "every direction keeps a share"
    assert abs(sum(shares) - 1.0) < 0.01


def test_higher_ev_channel_gets_more_time() -> None:
    upsert_opportunities([
        Opportunity(
            id="contest:org/2026-09-thing", channel="audit_contests", title="Contest",
            reward_usd=2000, score=23.0,
            payload={"ev_per_hour": 23.0, "effort_hours": 20.0, "expected_value_usd": 600.0},
        ),
        Opportunity(
            id="github:a/b#1", channel="github_bounties", title="Small bounty",
            reward_usd=50, score=1.5,
            payload={"ev_per_hour": 1.5, "effort_hours": 2.0, "expected_value_usd": 3.0},
        ),
    ])
    data = directions.portfolio()
    shares = {item["key"]: item["share"] for item in data["directions"]}
    assert shares["ceiling"] > shares["fast_money"]
    assert data["focus"] == "ceiling"


def test_advice_mentions_missing_hours_and_missing_marketplace_key() -> None:
    data = directions.portfolio()
    advice = " ".join(data["advice"])
    assert "часы" in advice.lower()
    assert "агент" in advice.lower()


def test_logged_hours_are_attributed_to_a_direction() -> None:
    log_hours("audit_contests", 4.0, "contest:x", "чтение scope")
    data = directions.portfolio()
    ceiling = next(item for item in data["directions"] if item["key"] == "ceiling")
    assert ceiling["hours_logged"] == 4.0
