"""The outbox: prepared work that waits for exactly one human action."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest

from agent import inbox
from agent.ledger import Opportunity, upsert_opportunities


@pytest.fixture(autouse=True)
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "inbox.db"
    monkeypatch.setenv("DB_PATH", str(path))
    return path


def seed_opportunity(opportunity_id: str = "github:org/repo#7") -> Dict[str, Any]:
    upsert_opportunities([
        Opportunity(id=opportunity_id, channel="github_bounties", title="Fix the parser",
                    reward_usd=120.0, payload={})
    ])
    return {"id": opportunity_id}


def test_requeueing_the_same_work_returns_the_same_item() -> None:
    first = inbox.add("github:org/repo#7", "github_bounties", "application", "Fix the parser")
    again = inbox.add("github:org/repo#7", "github_bounties", "application", "Fix the parser")
    assert first == again
    assert inbox.counts()["ready"] == 1


def test_different_kinds_are_separate_items() -> None:
    inbox.add("github:org/repo#7", "github_bounties", "application", "Fix")
    inbox.add("github:org/repo#7", "github_bounties", "brief", "План")
    assert inbox.counts()["ready"] == 2


def test_every_kind_carries_an_explicit_human_action() -> None:
    item_id = inbox.add("market:agenthansa:q1", "agent_marketplaces", "quest", "Квест")
    item = inbox.get(item_id)
    assert item is not None
    assert "Отправить" in item["action"]
    assert item["kind_title"] == "Черновик квеста"


def test_resolution_is_one_way() -> None:
    item_id = inbox.add("github:org/repo#7", "github_bounties", "application", "Fix")
    assert inbox.resolve(item_id, "published")
    assert not inbox.resolve(item_id, "published")
    assert inbox.counts()["published"] == 1
    assert inbox.pending() == []


def test_only_ready_items_are_pending() -> None:
    first = inbox.add("a", "github_bounties", "application", "A")
    inbox.add("b", "github_bounties", "application", "B")
    inbox.resolve(first, "skipped")
    pending = inbox.pending()
    assert [item["title"] for item in pending] == ["B"]


def test_resolve_rejects_unknown_status() -> None:
    with pytest.raises(ValueError):
        inbox.resolve(1, "почти-готово")


def test_text_reads_the_artifact_file(tmp_path: Path) -> None:
    artifact = tmp_path / "claim.md"
    artifact.write_text("/attempt #7\n\n**План:** фикс парсера", encoding="utf-8")
    item_id = inbox.add("github:org/repo#7", "github_bounties", "application", "Fix",
                        path=str(artifact))
    assert "фикс парсера" in inbox.text(item_id)


def test_publishing_the_claim_closes_the_item() -> None:
    seed_opportunity()
    inbox.add("github:org/repo#7", "github_bounties", "application", "Fix")
    inbox.mark_published("github:org/repo#7")
    assert inbox.counts()["ready"] == 0
    assert inbox.counts()["published"] == 1
