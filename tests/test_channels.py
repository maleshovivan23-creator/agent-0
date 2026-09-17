"""Channel behaviour: offline-safe, policy-gated, no surprises."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest

from agent.channels import DISABLED_CHANNELS, build_all, build_channel
from agent.channels.bug_recon import BugReconChannel, load_scope
from agent.channels.github_bounties import GitHubBountyChannel
from agent.policy import evaluate


class FakeResponse:
    def __init__(self, payload: Dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> Dict[str, Any]:
        return self._payload


def _item(number: int, title: str, labels: List[str], comments: int = 0) -> Dict[str, Any]:
    return {
        "id": number,
        "number": number,
        "title": title,
        "body": "Описание задачи. " * 40,
        "comments": comments,
        "html_url": f"https://github.com/acme/tool/issues/{number}",
        "repository_url": "https://api.github.com/repos/acme/tool",
        "labels": [{"name": name} for name in labels],
        "created_at": "2026-08-01T00:00:00Z",
        "updated_at": "2026-09-01T00:00:00Z",
    }


class FakeSession:
    """Stands in for requests.Session so tests never touch the network."""

    def __init__(self, items: List[Dict[str, Any]], search_status: int = 200) -> None:
        self.items = items
        self.search_status = search_status
        self.headers: Dict[str, str] = {}
        self.search_calls = 0
        self.repo_calls = 0

    def get(self, url: str, params: Any = None, timeout: int = 0) -> FakeResponse:
        if "search/issues" in url:
            self.search_calls += 1
            if self.search_status != 200:
                return FakeResponse({}, self.search_status)
            return FakeResponse({"items": self.items})
        self.repo_calls += 1
        return FakeResponse({"stargazers_count": 2500})


def test_every_registered_channel_is_policy_allowed() -> None:
    for name, channel in build_all().items():
        assert channel.allowed, f"{name} is registered but denied by policy"


def test_no_disabled_channel_is_actually_allowed() -> None:
    for entry in DISABLED_CHANNELS:
        assert evaluate(entry["capability"]).allowed is False


def test_github_harvest_filters_junk_and_ranks(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = GitHubBountyChannel(queries=["label:bounty"], stars_budget=1)
    channel.session = FakeSession(
        [
            _item(1, "[Bounty] $999999999999999999 BOUNTY FOR THE UNIVERSE", ["bounty"]),
            _item(2, "Add qdrant support to JS SDK", ["💎 Bounty", "$700"]),
            _item(3, "Small docs fix", ["bounty", "$30"], comments=40),
        ]
    )
    monkeypatch.setattr("agent.channels.github_bounties.time.sleep", lambda _s: None)

    items = channel.harvest(limit=10)
    titles = [item.title for item in items]
    assert any("qdrant" in title for title in titles)
    assert not any("UNIVERSE" in title for title in titles)
    assert items[0].reward_usd >= items[-1].reward_usd
    assert items[0].id.startswith("github:acme/tool#")
    assert items[0].payload["stars"] == 2500


def test_github_rate_limit_is_handled_without_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = GitHubBountyChannel(queries=["label:bounty"])
    channel.session = FakeSession([], search_status=403)
    monkeypatch.setattr("agent.channels.github_bounties.time.sleep", lambda _s: None)

    assert channel.harvest(limit=5) == []
    assert channel.rate_limited is True
    assert "rate limit" in channel.last_error.lower()


def test_bug_recon_refuses_without_scope() -> None:
    channel = BugReconChannel(scope=None)
    assert channel.allowed is True  # capability is legitimate...
    assert channel.ready is False  # ...but it cannot run without authorization
    assert channel.harvest() == []


def test_scope_parsing_without_pyyaml(tmp_path: Path) -> None:
    scope_file = tmp_path / "scope.yaml"
    scope_file.write_text(
        "authorization:\n"
        "  authorized_by: operator\n"
        "  program_url: https://example.com/policy\n"
        "passive_only: true\n"
        "targets:\n"
        "  - host: example.com\n"
        "    program: Example program\n",
        encoding="utf-8",
    )
    scope = load_scope(scope_file)
    assert scope is not None
    assert scope.complete is True
    assert scope.targets[0].host == "example.com"

    incomplete = tmp_path / "empty.yaml"
    incomplete.write_text("targets:\n  - host: example.com\n", encoding="utf-8")
    assert load_scope(incomplete).complete is False


def test_build_channel_rejects_unknown_name() -> None:
    with pytest.raises(KeyError):
        build_channel("faucet_farm")
