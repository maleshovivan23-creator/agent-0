"""Triage: is this bounty actually still winnable?

Every experienced bounty hunter checks three things before writing a line of
code, and skipping them is the most common way to waste a day:

1. **Is it still unclaimed?** On Algora-funded issues solvers comment
   ``/attempt #N``. Counting *distinct* users who did that gives the real size
   of the field. Real example: issue #743 in SecureBananaLabs/bug-bounty had
   76 ``/attempt`` comments on a $700 bounty.
2. **Is someone already finishing it?** PRs cross-referencing the issue are
   visible in the GitHub timeline. Open PRs there mean the work is likely gone.
3. **Has it already been paid?** The Algora bot posts awards in the issue. A
   closed issue that still carries the bounty label is finished work.

All three are read from public GitHub endpoints — no scraping of third-party
sites, no guessing. Two API calls per issue, and the caller controls the
budget so a cycle cannot burn the rate limit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from agent.http import build_session

#: Marks a solver's attempt. Algora's documented workflow.
_ATTEMPT = re.compile(r"/attempt\b", re.IGNORECASE)
_CLAIM = re.compile(r"/claim\b", re.IGNORECASE)
_ALGORA_AMOUNT = re.compile(r"\$([\d,]+)\s*bounty", re.IGNORECASE)
#: The bracket after the amount names the *funder* of the bounty, e.g.
#: "## 💎 $10,000 bounty [• Space and Time]". Verified against live issues.
_ALGORA_FUNDER = re.compile(r"\$[\d,]+\s*bounty\s*\[•\s*([^\]]+)\]", re.IGNORECASE)
_PAID = re.compile(
    r"(bounty\s+(?:has been\s+|was\s+|is\s+)?(?:paid|awarded|sent|claimed|resolved))"
    r"|(paid\s+out\s+to)"
    r"|(reward\s+(?:has been\s+)?(?:paid|sent))"
    r"|(congratulations[^\n]{0,40}(?:paid|won|awarded))",
    re.IGNORECASE,
)
_LINKED_PR = re.compile(r"(?:fixes|closes|resolves)\s+#\d+", re.IGNORECASE)

#: Bots whose comments are authoritative about bounty state.
_BOT_HINTS = ("algora", "opire", "bounty")


@dataclass
class Triage:
    opportunity_id: str
    repo: str
    number: int
    state: str = "unknown"
    verified_amount: Optional[float] = None
    funder: Optional[str] = None
    attempts: int = 0
    claimants: List[str] = field(default_factory=list)
    open_prs: int = 0
    pr_titles: List[str] = field(default_factory=list)
    paid: bool = False
    participants: int = 0
    verdict: str = "unknown"  # ready | contested | assigned | paid | closed
    probability: float = 0.0
    age_days: int = 0
    stale_days: int = 0
    notes: List[str] = field(default_factory=list)
    checked_at: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "opportunity_id": self.opportunity_id,
            "repo": self.repo,
            "number": self.number,
            "state": self.state,
            "verified_amount": self.verified_amount,
            "funder": self.funder,
            "attempts": self.attempts,
            "claimants": self.claimants[:10],
            "open_prs": self.open_prs,
            "pr_titles": self.pr_titles[:5],
            "paid": self.paid,
            "participants": self.participants,
            "verdict": self.verdict,
            "probability": self.probability,
            "age_days": self.age_days,
            "stale_days": self.stale_days,
            "notes": self.notes,
            "checked_at": self.checked_at,
        }


def estimate_probability(attempts: int, open_prs: int, stale_days: int) -> float:
    """Chance that our attempt converts into money.

    Calibrated to be pessimistic, because the failure mode we care about is
    spending a day on a bounty with a 40-person field. Field size dominates:
    every serious competitor roughly divides the remaining chance.
    """
    base = 0.22
    p = base / (1.0 + 0.35 * max(0, attempts))
    if open_prs:
        p *= 0.45 ** min(open_prs, 3)
    if stale_days > 120:
        p *= 0.6  # abandoned maintainer, PR likely never reviewed
    return max(0.005, min(p, 0.35))


def _days_since(iso: Optional[str]) -> int:
    if not iso:
        return 0
    try:
        moment = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return 0
    return max(0, (datetime.now(timezone.utc) - moment).days)


class TriageClient:
    """Reads public GitHub issue state. Never writes anything."""

    def __init__(self, token: str = "") -> None:
        headers = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.session = build_session("AGENT-0-triage/2.0", headers)

    def _get(self, url: str, params: Optional[Dict[str, Any]] = None) -> Any:
        try:
            response = self.session.get(url, params=params, timeout=20)
        except Exception:
            return None
        if response.status_code != 200:
            return None
        return response.json()

    def triage(
        self,
        *,
        opportunity_id: str,
        repo: str,
        number: int,
        created_at: Optional[str] = None,
        updated_at: Optional[str] = None,
        issue_state: str = "open",
    ) -> Triage:
        result = Triage(
            opportunity_id=opportunity_id,
            repo=repo,
            number=number,
            state=issue_state,
            age_days=_days_since(created_at),
            stale_days=_days_since(updated_at),
            checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

        comments = self._get(
            f"https://api.github.com/repos/{repo}/issues/{number}/comments",
            {"per_page": 100},
        )
        if isinstance(comments, list):
            self._read_comments(result, comments)
        else:
            result.notes.append("комментарии недоступны (лимит или приватный репозиторий)")

        timeline = self._get(
            f"https://api.github.com/repos/{repo}/issues/{number}/timeline",
            {"per_page": 100},
        )
        if isinstance(timeline, list):
            self._read_timeline(result, timeline)

        self._decide(result)
        return result

    # ------------------------------------------------------------- internals

    @staticmethod
    def _read_comments(result: Triage, comments: List[Dict[str, Any]]) -> None:
        claimants: List[str] = []
        participants = set()
        for comment in comments:
            author = str((comment.get("user") or {}).get("login", ""))
            body = str(comment.get("body") or "")
            if author:
                participants.add(author)
            if _ATTEMPT.search(body) and author:
                claimants.append(author)
            if author.lower().endswith("[bot]") or any(h in author.lower() for h in _BOT_HINTS):
                amount = _ALGORA_AMOUNT.search(body)
                if amount and result.verified_amount is None:
                    result.verified_amount = float(amount.group(1).replace(",", ""))
                funder = _ALGORA_FUNDER.search(body)
                if funder:
                    result.funder = funder.group(1).strip()
                    if result.funder:
                        result.notes.append(f"бюджет подтверждён платформой: {result.funder}")
            if _PAID.search(body):
                result.paid = True
                result.notes.append(f"найден маркер оплаты в комментарии от {author}")
            if _LINKED_PR.search(body):
                result.notes.append(f"{author} ссылается на PR по этой задаче")

        result.claimants = sorted(set(claimants))
        result.attempts = len(result.claimants)
        result.participants = len(participants)

    @staticmethod
    def _read_timeline(result: Triage, timeline: List[Dict[str, Any]]) -> None:
        for event in timeline:
            if event.get("event") != "cross-referenced":
                continue
            source = (event.get("source") or {}).get("issue") or {}
            if "pull_request" not in source:
                continue
            if source.get("state") == "open":
                result.open_prs += 1
            title = str(source.get("title") or "")
            if title:
                result.pr_titles.append(title[:80])

    @staticmethod
    def _decide(result: Triage) -> None:
        result.probability = estimate_probability(result.attempts, result.open_prs, result.stale_days)

        if result.paid or result.state == "closed":
            result.verdict = "paid" if result.paid else "closed"
            result.probability = 0.0
            if not result.paid:
                result.notes.append("задача закрыта: работа, скорее всего, уже сделана и оплачена")
            return

        # Any open PR means somebody is already most of the way there.
        if result.open_prs >= 1:
            result.verdict = "contested"
            result.notes.append(
                f"уже {result.open_prs} открытых PR ссылаются на задачу — работа может уйти "
                "в любой момент"
            )
            return

        if result.attempts >= 6:
            result.verdict = "contested"
            result.notes.append(
                f"{result.attempts} человек отметились через /attempt — очередь большая"
            )
            return

        result.verdict = "ready"
        if result.attempts:
            result.notes.append(
                f"заявок: {result.attempts} — конкуренция умеренная, ещё можно успеть"
            )
        else:
            result.notes.append("заявок не видно: вы можете успеть первым")
        if result.funder:
            result.notes.append(f"награда фондируется: {result.funder}")
        return
