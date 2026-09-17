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
