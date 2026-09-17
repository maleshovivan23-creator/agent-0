"""Live worker: open bounty tasks published on GitHub.

This is the one channel that works with zero capital, zero wallet and zero
special access:

* bounties are public issues, found through GitHub's official search API;
* rewards are stated in labels/titles ("$700", "[BOUNTY: $1,500]");
* payment happens through the program that funds the bounty once a pull
  request is merged — no upfront cost for the operator.

What it does NOT do: it does not open pull requests on its own. Publishing a PR
under the operator's identity is a ``submit_deliverable_human_approved``
action, and this channel does not hold that capability.

Rate limits: GitHub search allows 30 requests/minute unauthenticated. The
worker makes at most a handful of queries per cycle and backs off on 403/429.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests

from agent.channels.base import Channel
from agent.config import get_env
from agent.http import build_session
from agent.ledger import Opportunity
from agent.scoring import looks_like_junk, score_opportunity

API = "https://api.github.com"

#: Base queries. Freshness-filtered variants are added at runtime, because the
#: single biggest factor in winning a bounty is being early: a $700 task with
#: 36 competitors pays nothing, the same task on day one pays full.
DEFAULT_QUERIES = [
    'label:"💎 Bounty" state:open type:issue',
    'label:bounty state:open type:issue',
    '"bounty" in:title state:open type:issue',
    'label:"help wanted" label:bounty state:open type:issue',
]

#: Added automatically with a `created:>` filter — newest first.
FRESH_QUERIES = [
    'label:"💎 Bounty" state:open type:issue',
    'label:bounty state:open type:issue',
]

#: Labels that usually mean "the money exists and the task is scoped".
FUNDED_LABELS = ("💎 bounty", "bounty", "bug bounty", "$")


def _age_days(iso: Optional[str]) -> int:
    if not iso:
        return 999
    try:
        moment = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return 999
    return max(0, (datetime.now(timezone.utc) - moment).days)


class GitHubBountyChannel(Channel):
    name = "github_bounties"
    title = "Открытые bounty-задачи на GitHub"
    capability = "read_public_data"
    description = (
        "Поиск публичных задач с наградой через официальный API GitHub: "
        "парсинг суммы, оценка трудозатрат, отсев приманок. Без вложений, "
        "без кошелька. Публикация PR — вручную оператором."
    )

    def __init__(self, queries: Optional[List[str]] = None, stars_budget: int = 12) -> None:
        super().__init__()
        self.queries = queries or self._queries_from_env()
        self.stars_budget = stars_budget
        token = get_env("GITHUB_TOKEN", "") or ""
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.session = build_session("AGENT-0-bounty-harvester/1.0", headers)
        self.last_error: str = ""
        self.rate_limited: bool = False

    @staticmethod
    def _queries_from_env() -> List[str]:
        raw = get_env("BOUNTY_QUERIES", "") or ""
        if raw.strip():
            return [q.strip() for q in raw.split("|") if q.strip()]

        fresh_days = int(get_env("FRESH_WINDOW_DAYS", "21") or 21)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=fresh_days)).strftime("%Y-%m-%d")
        queries = list(DEFAULT_QUERIES)
        queries += [f"{query} created:>{cutoff}" for query in FRESH_QUERIES]
        return queries

    # ------------------------------------------------------------------ http

    def _search(self, query: str, per_page: int) -> List[Dict[str, Any]]:
        if self.rate_limited:
            return []
        try:
            response = self.session.get(
                f"{API}/search/issues",
                params={"q": query, "per_page": per_page, "sort": "updated", "order": "desc"},
                timeout=20,
            )
        except requests.RequestException as exc:
            self.last_error = f"network: {exc.__class__.__name__}"
            return []

        if response.status_code in (403, 429):
            self.rate_limited = True
            self.last_error = (
                "GitHub rate limit (30 поисковых запросов в минуту для "
                "анонимных вызовов). Пауза до следующего цикла."
            )
            return []
        if response.status_code != 200:
            self.last_error = f"GitHub HTTP {response.status_code}"
            return []
        return list(response.json().get("items", []))

    def _stars(self, repo: str, budget: List[int]) -> int:
        if budget[0] <= 0 or not repo:
            return 0
        budget[0] -= 1
        try:
            response = self.session.get(f"{API}/repos/{repo}", timeout=15)
            if response.status_code == 200:
                return int(response.json().get("stargazers_count", 0))
        except requests.RequestException:
            return 0
        return 0

    # --------------------------------------------------------------- parsing

    @staticmethod
    def _repo_from(item: Dict[str, Any]) -> str:
        url = item.get("repository_url", "")
        return url.replace(f"{API}/repos/", "")

    @staticmethod
    def _labels(item: Dict[str, Any]) -> List[str]:
        return [str(label.get("name", "")) for label in item.get("labels", [])]

    def _to_opportunity(self, item: Dict[str, Any], stars: int, hourly_rate: float) -> Optional[Opportunity]:
        title = str(item.get("title", "")).strip()
        labels = self._labels(item)
        body = str(item.get("body") or "")
        comments = int(item.get("comments", 0) or 0)
        repo = self._repo_from(item)

        score = score_opportunity(
            labels=labels,
            title=title,
            body=body,
            comments=comments,
            repo_stars=stars,
            hourly_rate=hourly_rate,
            age_days=_age_days(item.get("created_at")),
        )

        junk = looks_like_junk(title, score.reward_usd, stars)
        if junk:
            return None

        number = item.get("number")
        html_url = str(item.get("html_url", ""))
        opp_id = f"github:{repo}#{number}"
        rationale = score.rationale
        if stars:
            rationale += f"; репозиторий {stars}★"

        return Opportunity(
            id=opp_id,
            channel=self.name,
            title=title or f"issue #{number}",
            url=html_url,
            repo=repo,
            reward_usd=score.reward_usd,
            reward_source=score.reward_source,
            score=score.total,
            rationale=rationale,
            payload={
                "labels": labels,
                "body": body[:1500],
                "comments": comments,
                "stars": stars,
                "effort_hours": score.effort_hours,
                "probability": score.probability,
                "ev_per_hour": score.ev_per_hour,
                "expected_value_usd": score.expected_value_usd,
                "created_at": item.get("created_at"),
                "updated_at": item.get("updated_at"),
                "age_days": _age_days(item.get("created_at")),
            },
        )

    # -------------------------------------------------------------- harvesting

    def harvest(self, limit: int = 25) -> List[Opportunity]:
        if not self.allowed:
            return []

        hourly_rate = 15.0
        try:
            from agent.config import get_float

            hourly_rate = get_float("OPERATOR_HOURLY_RATE", 15.0)
        except Exception:
            pass

        self.last_error = ""
        self.rate_limited = False
        raw_items: Dict[str, Dict[str, Any]] = {}

        per_query = max(5, limit // max(1, len(self.queries)))
        for index, query in enumerate(self.queries):
            if index:
                time.sleep(2.0)  # stay well inside 30 req/min
            for item in self._search(query, per_query):
                key = str(item.get("id") or item.get("html_url"))
                raw_items.setdefault(key, item)

        # Stage 1: cheap triage without any extra API calls. Junk is dropped
        # here so the small repo-lookup budget is spent only on real leads.
        candidates: List[tuple[float, Dict[str, Any]]] = []
        for item in raw_items.values():
            title = str(item.get("title", "")).strip()
            labels = self._labels(item)
            cheap = score_opportunity(
                labels=labels,
                title=title,
                body=str(item.get("body") or ""),
                comments=int(item.get("comments", 0) or 0),
                repo_stars=0,
                hourly_rate=hourly_rate,
                age_days=_age_days(item.get("created_at")),
            )
            if looks_like_junk(title, cheap.reward_usd, 0):
                continue
            candidates.append((cheap.total, item))

        # Stage 2: enrich the best candidates with repo popularity.
        candidates.sort(key=lambda pair: pair[0], reverse=True)
        stars_budget = [self.stars_budget]
        opportunities: List[Opportunity] = []
        for _, item in candidates:
            repo = self._repo_from(item)
            stars = self._stars(repo, stars_budget)
            opp = self._to_opportunity(item, stars, hourly_rate)
            if opp is not None:
                opportunities.append(opp)

        opportunities.sort(key=lambda o: o.score, reverse=True)
        return opportunities[:limit]
