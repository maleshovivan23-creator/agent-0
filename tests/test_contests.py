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


# --- тишина в репозитории: срок неизвестен, а правок нет давно ----------------

def quiet_contest(pushed_days: float) -> Dict[str, Any]:
    return {"code-423n4": [repo("2026-07-quiet-protocol", age_days=61,
                                 pushed_days=pushed_days)]}


def test_silent_repository_loses_its_place_in_the_queue() -> None:
    """Контест двухмесячной давности стоял первым с $22/час, хотя приём закрыт."""
    fresh = FakeContests(quiet_contest(1)).harvest()[0]
    stale = FakeContests(quiet_contest(22)).harvest()[0]

    assert stale.payload["suspect_expired"] is True
    assert fresh.payload["suspect_expired"] is False
    assert stale.score < fresh.score / 3, (
        f"подозрение на закрытый приём должно резко снижать EV/час: "
        f"{fresh.score} → {stale.score}"
    )
    assert "возможно, закрыт" in stale.rationale
    assert "22 дн" in stale.rationale


def test_silent_repository_warns_the_human_first() -> None:
    stale = FakeContests(quiet_contest(30)).harvest()[0]
    checks = stale.payload["verify_before_work"]
    assert any("СРОЧНО" in line and "30 дн" in line for line in checks), checks


class FakeWithReadme(FakeContests):
    """Тот же фейк, но README контеста отдаёт заданный текст (даты приёма работ)."""

    def __init__(self, repos, readme: str) -> None:
        super().__init__(repos)
        self._readme = readme

    def _get(self, url: str, params: Optional[Dict[str, Any]] = None) -> Any:
        if url.endswith("/contents/README.md"):
            return {"content": base64.b64encode(self._readme.encode()).decode()}
        return super()._get(url, params)


def test_known_deadline_is_not_treated_as_suspicion() -> None:
    """Если срок известен и работы ещё принимают — штрафа нет."""
    from datetime import datetime, timedelta, timezone

    # Платформы пишут сроки словами: «Ends March 13, 2026 20:00 UTC»
    future = (datetime.now(timezone.utc) + timedelta(days=5)).strftime("%B %d, %Y")
    channel = FakeWithReadme(
        quiet_contest(40),
        f"# Contest\n\n- Starts July 01, 2026 20:00 UTC\n- Ends {future} 20:00 UTC\n"
        f"- Total prize pool: $50,000\n",
    )
    opportunities = channel.harvest()
    assert opportunities, "контест с известным сроком должен остаться в очереди"
    assert opportunities[0].payload.get("hours_left") is not None, "даты из README не прочитались"
    assert opportunities[0].payload.get("suspect_expired") is False


def test_quiet_threshold_is_configurable(monkeypatch) -> None:
    monkeypatch.setenv("CONTEST_QUIET_DAYS", "60")
    channel = FakeContests(quiet_contest(22))
    assert channel.harvest()[0].payload["suspect_expired"] is False, (
        "порог из .env должен уважаться"
    )
