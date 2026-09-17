"""Audit-contest worker: the highest-ceiling income channel in the farm."""

from __future__ import annotations

import base64
from typing import Any, Dict, List, Optional

from agent.channels.audit_contests import AuditContestsChannel


class FakeContests(AuditContestsChannel):
    """Contests worker with the HTTP layer replaced by a fixture."""

    def __init__(self, repos: Dict[str, List[Dict[str, Any]]], scope: Optional[str] = None) -> None:
        super().__init__(max_age_days=75, scope_budget=4)
        self._repos = repos
        self._scope = scope

    def _get(self, url: str, params: Optional[Dict[str, Any]] = None) -> Any:
        if "/orgs/" in url and "/repos" in url:
            org = url.split("/orgs/")[1].split("/")[0]
            return self._repos.get(org, [])
        if url.endswith("/contents/scope.txt"):
            if self._scope is None:
                return {"message": "Not Found"}
            return {"content": base64.b64encode(self._scope.encode()).decode()}
        if url.endswith("/git/trees/main"):
            if self._repos.get("__tree__"):
                return self._repos["__tree__"][0]
            return {"tree": []}
        if "/repos/" in url and url.count("/") == 5:
            return {"default_branch": "main"}
        return None


def repo(name: str, age_days: int = 10, org: str = "code-423n4", pushed_days: int = 2) -> Dict[str, Any]:
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    return {
        "name": name,
        "full_name": f"{org}/{name}",
        "html_url": f"https://github.com/{org}/{name}",
        "created_at": (now - timedelta(days=age_days)).isoformat(),
        "pushed_at": (now - timedelta(days=pushed_days)).isoformat(),
        "language": "Solidity",
        "stargazers_count": 5,
    }


def test_only_contest_repositories_are_kept() -> None:
    channel = FakeContests(
        {
            "code-423n4": [
                repo("2026-09-fresh-protocol"),
                repo("some-random-repo"),
                repo("2026-08-old-protocol", age_days=400),
            ],
            "sherlock-audit": [repo("2026-09-sherlock-thing", org="sherlock-audit")],
            "hats-finance": [repo("Test-0xdc9f7d06771e3ae96e6daefb3393f6b4a70efa93", org="hats-finance")],
        }
    )
    items = channel.harvest(limit=20)
    names = {item.title for item in items}
    assert any("fresh-protocol" in name for name in names)
    assert any("sherlock-thing" in name for name in names)
    assert any("Test-0x" in name for name in names)
    assert not any("some-random-repo" in name for name in names)
    assert not any("old-protocol" in name for name in names)


def test_judging_repositories_are_skipped() -> None:
    channel = FakeContests(
        {"code-423n4": [], "sherlock-audit": [repo("2025-07-thing-judging", org="sherlock-audit")]}
    )
    assert channel.harvest(limit=10) == []


def test_scope_file_drives_the_effort_estimate() -> None:
    scope = "\n".join(f"src/Contract{i}.sol" for i in range(20))
    channel = FakeContests(
        {"code-423n4": [repo("2026-09-scoped")], "sherlock-audit": [], "hats-finance": []},
        scope=scope,
    )
    items = channel.harvest(limit=5)
    assert len(items) == 1
    payload = items[0].payload
    assert payload["scope_solidity"] == 20
    # 6 + 20 * 0.45 = 15 hours, not the unknown-scope fallback.
    assert 14 <= payload["effort_hours"] <= 16
    assert payload["probability"] == 0.30
    assert payload["ev_per_hour"] > 10


def test_missing_scope_falls_back_to_tree_counting() -> None:
    tree = {
        "tree": [
            {"path": f"src/Vault{i}.sol", "type": "blob"} for i in range(12)
        ]
        + [{"path": f"lib/dependency{i}.sol", "type": "blob"} for i in range(50)]
    }
    channel = FakeContests(
        {"code-423n4": [repo("2026-09-noscope")], "sherlock-audit": [], "hats-finance": [],
         "__tree__": [tree]}
    )
    items = channel.harvest(limit=5)
    payload = items[0].payload
    assert payload["scope_solidity"] == 12, "dependency folders must not inflate the estimate"
    assert payload.get("scope_source", "").startswith("git tree")


def test_every_opportunity_states_its_assumptions() -> None:
    channel = FakeContests(
        {"code-423n4": [repo("2026-09-honest")], "sherlock-audit": [], "hats-finance": []}
    )
    payload = channel.harvest(limit=1)[0].payload
    assert payload["assumptions"], "estimates must be auditable"
    assert payload["verify_before_work"], "operator must know what to verify"
    assert "медианная выплата" in payload["assumptions"][0]
