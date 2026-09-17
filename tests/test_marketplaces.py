"""Agent marketplace worker: honest degradation, no fake data."""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from agent.channels.agent_marketplaces import AgentMarketplacesChannel, _estimate_hours


class FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> Any:
        return self._payload


class FakeSession:
    """Отвечает на любые запросы одним и тем же телом.

    Клиент площадки ходит через ``request`` (единая точка), поэтому заглушка
    обязана уметь и его: раньше канал делал запросы сам и хватало ``get``.
    """

    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code
        self.headers: Dict[str, str] = {}
        self.calls: List[str] = []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(url)
        return FakeResponse(self.payload, self.status_code)

    def get(self, url: str, params: Any = None, timeout: int = 0) -> FakeResponse:
        return self.request("GET", url, params=params, timeout=timeout)


def channel_with(payload: Any, status_code: int = 200) -> AgentMarketplacesChannel:
    channel = AgentMarketplacesChannel()
    channel._session = lambda platform: FakeSession(payload, status_code)  # type: ignore[assignment]
    return channel


def test_without_api_key_it_says_so_and_returns_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTHANSA_API_KEY", raising=False)
    channel = AgentMarketplacesChannel()
    items = channel.harvest()
    assert items == []
    assert any("ключ" in note for note in channel.notes)


def test_quests_become_opportunities(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTHANSA_API_KEY", "test-key")
    channel = channel_with({
        "quests": [
            {
                "id": "q1",
                "title": "Market analysis of agent platforms",
                "reward_amount": 30,
                "submission_count": 5,
                "submission_cap": 50,
                "description": "Research " * 120,
                "requirements": "Cite every source",
                "url": "https://agenthansa.com/quests/q1",
            }
        ]
    })
    items = channel.harvest()
    assert len(items) == 1
    item = items[0]
    assert item.id == "market:agenthansa:q1"
    assert item.reward_usd == 30
    assert item.payload["payout_rail"] == "crypto_usdc"
    assert "USDC" in item.rationale
    assert item.payload["description"].startswith("Research")
    assert item.payload["requirements"] == "Cite every source"
    assert item.payload["verify_before_work"]


def test_competition_against_submission_cap_lowers_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTHANSA_API_KEY", "k")
    empty = channel_with({"quests": [
        {"id": "q", "title": "T", "reward_amount": 40, "submission_count": 0, "submission_cap": 50}
    ]}).harvest()[0]
    full = channel_with({"quests": [
        {"id": "q", "title": "T", "reward_amount": 40, "submission_count": 49, "submission_cap": 50}
    ]}).harvest()[0]
    assert full.score < empty.score
    assert full.payload["probability"] < empty.payload["probability"]


def test_bad_key_is_reported_not_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTHANSA_API_KEY", "wrong")
    channel = channel_with({}, status_code=401)
    assert channel.harvest() == []
    assert "401" in channel.last_error


def test_unreachable_platform_degrades_gracefully(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTHANSA_API_KEY", "k")
    channel = AgentMarketplacesChannel()

    def broken(platform: str) -> Any:
        class Boom:
            headers: Dict[str, str] = {}

            def request(self, *args: Any, **kwargs: Any) -> Any:
                raise OSError("network down")

            def get(self, *args: Any, **kwargs: Any) -> Any:
                raise OSError("network down")

        return Boom()

    channel._session = broken  # type: ignore[assignment]
    assert channel.harvest() == []
    assert channel.last_error


def test_quests_without_reward_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTHANSA_API_KEY", "k")
    channel = channel_with({"quests": [{"id": "x", "title": "No reward", "reward_amount": 0}]})
    assert channel.harvest() == []


def test_effort_estimate_grows_with_reward_and_length() -> None:
    short_cheap = _estimate_hours({"description": "x" * 100}, 10)
    long_rich = _estimate_hours({"description": "x" * 4000}, 200)
    assert long_rich > short_cheap
    assert 1.0 <= short_cheap <= 8.0
