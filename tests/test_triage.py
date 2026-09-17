"""Triage tests — the module that decides whether work is worth doing.

All network access is replaced with fixtures built from real GitHub payload
shapes (Algora bot comment, `/attempt` comments, cross-referenced PR events).
"""

from __future__ import annotations

from typing import Any, Dict, List

from agent.triage import Triage, TriageClient, estimate_probability


class FakeSession:
    def __init__(self, comments: List[Dict[str, Any]], timeline: List[Dict[str, Any]]) -> None:
        self.comments = comments
        self.timeline = timeline
        self.headers: Dict[str, str] = {}

    def get(self, url: str, params: Any = None, timeout: int = 0) -> Any:
        class Response:
            status_code = 200

            def __init__(self, payload: Any) -> None:
                self._payload = payload

            def json(self) -> Any:
                return self._payload

        if "comments" in url:
            return Response(self.comments)
        return Response(self.timeline)


def comment(author: str, body: str) -> Dict[str, Any]:
    return {"user": {"login": author}, "body": body}


def attempt(author: str) -> Dict[str, Any]:
    return comment(author, "/attempt #743")


def cross_ref(state: str = "open", title: str = "Add feature") -> Dict[str, Any]:
    return {
        "event": "cross-referenced",
        "source": {"issue": {"state": state, "title": title, "pull_request": {"url": "x"}}},
    }


def client_with(comments: List[Dict[str, Any]], timeline: List[Dict[str, Any]]) -> TriageClient:
    client = TriageClient()
    client.session = FakeSession(comments, timeline)  # type: ignore[assignment]
    return client


def test_fresh_issue_without_competition_is_ready() -> None:
    triage = client_with([comment("alice", "Any update?")], []).triage(
        opportunity_id="github:acme/tool#743", repo="acme/tool", number=743
    )
    assert triage.verdict == "ready"
    assert triage.attempts == 0
    assert triage.probability > 0.15


def test_many_attempts_make_it_contested() -> None:
    comments = [attempt(f"user{index}") for index in range(12)]
    triage = client_with(comments, []).triage(
        opportunity_id="github:acme/tool#743", repo="acme/tool", number=743
    )
    assert triage.attempts == 12
    assert triage.verdict == "contested"
    assert triage.probability < 0.05


def test_single_open_pr_is_enough_to_warn() -> None:
    triage = client_with([], [cross_ref("open")]).triage(
        opportunity_id="github:acme/tool#1", repo="acme/tool", number=1
    )
    assert triage.open_prs == 1
    assert triage.verdict == "contested"


def test_closed_bounty_is_not_work() -> None:
    triage = client_with([], []).triage(
        opportunity_id="github:acme/tool#1",
        repo="acme/tool",
        number=1,
        issue_state="closed",
    )
    assert triage.verdict == "closed"
    assert triage.probability == 0.0


def test_paid_marker_is_detected() -> None:
    comments = [comment("algora-pbc[bot]", "The bounty has been paid out to @solver. Congratulations!")]
    triage = client_with(comments, []).triage(
        opportunity_id="github:acme/tool#1", repo="acme/tool", number=1
    )
    assert triage.paid is True
    assert triage.verdict == "paid"


def test_bot_comment_gives_amount_and_funder_not_assignee() -> None:
    """The name in brackets is the funder: verified against Tailcall/Space and Time."""
    bot = (
        "## 💎 $10,000 bounty [• Space and Time](https://algora.io/spaceandtime)\n"
        "### Steps to solve:\n1. **Start working**: Comment `/attempt #183`"
    )
    triage = client_with([comment("algora-pbc[bot]", bot)], []).triage(
        opportunity_id="github:spaceandtimefdn/sxt-proof-of-sql#183",
        repo="spaceandtimefdn/sxt-proof-of-sql",
        number=183,
    )
    assert triage.verified_amount == 10_000.0
    assert triage.funder == "Space and Time"
    assert not hasattr(triage, "assigned_to")
    assert triage.verdict == "ready"


def test_attempts_are_counted_per_person_not_per_comment() -> None:
    comments = [attempt("alice"), attempt("alice"), attempt("bob")]
    triage = client_with(comments, []).triage(
        opportunity_id="github:acme/tool#1", repo="acme/tool", number=1
    )
    assert triage.attempts == 2
    assert triage.claimants == ["alice", "bob"]


def test_probability_matches_reality_of_a_crowded_bounty() -> None:
    # The real #743 has 36 attempts and 15 open PRs: essentially unwinnable.
    assert estimate_probability(36, 15, 0) <= 0.005
    assert estimate_probability(0, 0, 0) > estimate_probability(3, 0, 0)
    assert estimate_probability(0, 1, 0) < estimate_probability(0, 0, 0)
    # Abandoned tasks are worth less than active ones.
    assert estimate_probability(0, 0, 400) < estimate_probability(0, 0, 3)


def test_missing_comments_do_not_crash() -> None:
    client = TriageClient()
    client.session = FakeSession([], [])  # type: ignore[assignment]
    triage = client.triage(opportunity_id="github:a/b#1", repo="a/b", number=1)
    assert isinstance(triage, Triage)
    assert triage.verdict in ("ready", "contested", "paid", "closed")
