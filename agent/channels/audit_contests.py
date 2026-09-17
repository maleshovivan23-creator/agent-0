"""Live worker: smart-contract audit contests with real prize pools.

Why this worker exists
----------------------
Open GitHub bounties are capped at a few hundred dollars. The same skill applied
to a competitive audit contest has a far higher ceiling: public warden records
show single-finding awards of $1k-$10k and contest wins into the tens of
thousands, and Code4rena/Sherlock/Hats run dozens of contests per year.

How it finds them, without guessing
-----------------------------------
All three platforms publish contest code in public GitHub organisations:

* ``code-423n4``    — Code4rena (repos named ``YYYY-MM-protocol``)
* ``sherlock-audit``— Sherlock (same naming, plus ``-judging`` repos)
* ``hats-finance``  — Hats Finance (repos named ``Protocol-0xaddress``)

The worker lists the organisation's newest repositories, keeps the ones that
match a contest naming pattern, and checks the repository's own files. A real
Code4rena contest repository contains ``scope.txt``, ``src/``, sometimes
``out_of_scope.txt`` — so scope size (and therefore effort) can be estimated
from the repository itself instead of from a marketing page.

Honesty rules built in
----------------------
* "Open" cannot be proven from GitHub alone, so the worker marks contests as
  ``recent`` and tells the operator to confirm the deadline on the platform.
* Prize pools are not in the repository. The estimate uses published median
  warden awards and states its assumptions explicitly in the rationale.
* This is the highest-skill channel in the farm: it needs Solidity/Rust/Audit
  knowledge. The farm prepares the workspace, the scope map and the checklist;
  finding the bug is human work.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from agent.channels.base import Channel
from agent.config import get_env
from agent.contest_page import parse as parse_contest_page
from agent.http import build_session
from agent.ledger import Opportunity

API = "https://api.github.com"

#: Org -> platform name and how to recognise a contest repository.
ORGS: Dict[str, Dict[str, str]] = {
    "code-423n4": {
        "platform": "Code4rena",
        "page": "https://code4rena.com/audits",
        "pattern": r"^\d{4}-\d{2}-",
    },
    "sherlock-audit": {
        "platform": "Sherlock",
        "page": "https://audits.sherlock.xyz/contests",
        "pattern": r"^\d{4}-\d{2}-",
    },
    "hats-finance": {
        "platform": "Hats Finance",
        "page": "https://hats.finance/",
        # Hats names repositories "Protocol-0x<address>".
        "pattern": r"^.+?-0x[0-9a-fA-F]{6,}$",
    },
}

#: Files that prove a repository is a contest with a real scope.
SCOPE_FILES = ("scope.txt", "scope.md", "out_of_scope.txt", "README.md")

#: Published medians used for the estimate. Sources are named in the payload so
#: the operator can audit the assumption instead of trusting the number.
MEDIAN_FINDING_AWARD = 2_000.0  # Immunefi/Code4rena public reporting
PROBABILITY_AT_LEAST_ONE = 0.30  # prepared operator working the playbook
COST_PER_HOUR = 15.0  # opportunity cost of the operator's time

_SKIP_SUFFIXES = ("-judging", "-mitigation", "_judging")


def _age_days(iso: Optional[str]) -> int:
    if not iso:
        return 9999
    try:
        moment = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return 9999
    return max(0, (datetime.now(timezone.utc) - moment).days)


class AuditContestsChannel(Channel):
    name = "audit_contests"
    title = "Аудит-контесты (Code4rena, Sherlock, Hats)"
    capability = "read_public_data"
    description = (
        "Ищет активные аудит-контесты в публичных орг-репозиториях платформ, "
        "читает scope.txt, оценивает объём работ и EV/час. Самый высокий потолок "
        "дохода из каналов фермы — и самый высокий порог входа (нужен Solidity/Rust)."
    )

    def __init__(self, max_age_days: Optional[int] = None, scope_budget: int = 4,
                 page_budget: int = 4) -> None:
        super().__init__()
        self.max_age_days = max_age_days if max_age_days is not None else int(
            get_env("CONTEST_MAX_AGE_DAYS", "75") or 75
        )
        self.scope_budget = scope_budget
        self.page_budget = page_budget
        self.session = build_session(
            "AGENT-0-contest-scout/2.0",
            {"Accept": "application/vnd.github+json"},
        )
        token = get_env("GITHUB_TOKEN", "") or ""
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        self.last_error = ""

    # ------------------------------------------------------------------ http

    def _get(self, url: str, params: Optional[Dict[str, Any]] = None) -> Any:
        try:
            response = self.session.get(url, params=params, timeout=20)
        except Exception:
            return None
        if response.status_code in (403, 429):
            self.last_error = "лимит запросов GitHub"
            return None
        if response.status_code != 200:
            return None
        return response.json()

    def _read_scope(self, repo: str) -> Dict[str, Any]:
        """Read scope.txt when present: gives an honest size estimate.

        Not every platform ships ``scope.txt`` (Sherlock keeps the code in a
        subdirectory), so when it is missing we fall back to walking the git
        tree and counting source files — one extra call, still no guessing.
        """
        data = self._get(f"{API}/repos/{repo}/contents/scope.txt")
        if not isinstance(data, dict) or "content" not in data:
            return self._count_sources(repo)
        import base64

        try:
            text = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        except Exception:
            return {}
        files = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        solidity = [f for f in files if f.endswith(".sol")]
        rust = [f for f in files if f.endswith(".rs")]
        return {
            "scope_entries": len(files),
            "scope_solidity": len(solidity),
            "scope_rust": len(rust),
            "scope_sample": files[:8],
            "scope_text_excerpt": text[:1200],
        }

    def _read_page(self, repo: str) -> Dict[str, Any]:
        """README контеста: пул, даты и репозиторий с кодом — из первых рук."""
        data = self._get(f"{API}/repos/{repo}/contents/README.md")
        if not isinstance(data, dict) or "content" not in data:
            return {}
        import base64

        try:
            text = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        except Exception:
            return {}
        page = parse_contest_page(text)
        return page.as_dict() if page.found_anything else {}

    def _count_sources(self, repo: str) -> Dict[str, Any]:
        """Count source files in the repository tree when scope.txt is absent."""
        info = self._get(f"{API}/repos/{repo}")
        branch = "main"
        if isinstance(info, dict):
            branch = info.get("default_branch") or "main"
        tree = self._get(f"{API}/repos/{repo}/git/trees/{branch}", {"recursive": "1"})
        if not isinstance(tree, dict):
            return {}
        files = [
            str(node.get("path", ""))
            for node in tree.get("tree", [])
            if node.get("type") == "blob"
        ]
        solidity = [f for f in files if f.endswith(".sol")]
        rust = [f for f in files if f.endswith(".rs")]
        # Ignore dependency folders: they inflate the estimate hugely.
        solidity = [f for f in solidity if not f.startswith(("lib/", "node_modules/", "test/"))]
        rust = [f for f in rust if "/target/" not in f]
        return {
            "scope_entries": len(solidity) + len(rust),
            "scope_solidity": len(solidity),
            "scope_rust": len(rust),
            "scope_sample": (solidity or rust)[:8],
            "scope_source": "git tree (scope.txt отсутствует)",
        }

    # ------------------------------------------------------------- estimating

    @staticmethod
    def _estimate(scope: Dict[str, Any]) -> Dict[str, float]:
        solidity = int(scope.get("scope_solidity") or 0)
        rust = int(scope.get("scope_rust") or 0)
        entries = int(scope.get("scope_entries") or 0)

        # Effort: reading scope + hunting + writing a PoC. Calibrated on public
        # warden timelines (a 1.5k-SLOC scope takes a prepared person ~15-20h).
        if solidity:
            # Files are not equal: ~25 substantive contracts is a typical scope.
            # Calibration: a 20-file scope takes a prepared warden 12-18 hours.
            hours = 6.0 + min(solidity, 40) * 0.45
        elif rust:
            hours = 8.0 + min(rust, 30) * 0.5
        elif entries:
            hours = 8.0 + entries * 0.1
        else:
            hours = 16.0  # scope unknown: assume a full evening, no illusions
        hours = max(4.0, min(hours, 45.0))

        expected = PROBABILITY_AT_LEAST_ONE * MEDIAN_FINDING_AWARD
        ev_per_hour = (expected - hours * COST_PER_HOUR * 0.2) / hours
        return {
            "hours": round(hours, 1),
            "expected_value_usd": round(expected, 2),
            "ev_per_hour": round(ev_per_hour, 2),
        }

    # -------------------------------------------------------------- harvesting

    def harvest(self, limit: int = 25) -> List[Opportunity]:
        if not self.allowed:
            return []
        self.last_error = ""
        opportunities: List[Opportunity] = []

        for org, meta in ORGS.items():
            repos = self._get(
                f"{API}/orgs/{org}/repos",
                {"sort": "created", "direction": "desc", "per_page": 30},
            )
            if not isinstance(repos, list):
                continue
            pattern = re.compile(meta["pattern"])

            for repo in repos:
                name = str(repo.get("name", ""))
                if not pattern.match(name):
                    continue
                if any(name.endswith(suffix) for suffix in _SKIP_SUFFIXES):
                    continue
                age = _age_days(repo.get("created_at"))
                if age > self.max_age_days:
                    continue

                full_name = str(repo.get("full_name"))
                scope: Dict[str, Any] = {}
                if self.scope_budget > 0:
                    scope = self._read_scope(full_name)
                    self.scope_budget -= 1

                page: Dict[str, Any] = {}
                if self.page_budget > 0:
                    page = self._read_page(full_name)
                    self.page_budget -= 1

                # Контест с прошедшей датой — не возможность, а история. Раньше
                # такие попадали в очередь как «$23/час», хотя приём работ закрыт.
                hours_left = page.get("hours_left")
                expired = hours_left is not None and hours_left < 0
                if expired:
                    opportunities.append(
                        Opportunity(
                            id=f"contest:{full_name}",
                            channel=self.name,
                            title=f"{meta['platform']}: {name} (закончился)",
                            url=str(repo.get("html_url")),
                            repo=full_name,
                            reward_usd=0.0,
                            reward_source="контест завершён",
                            score=0.0,
                            rationale=(
                                f"{meta['platform']}: приём работ закрыт по датам из README"
                                + (f" (конец {page.get('ends_at')})" if page.get("ends_at") else "")
                            ),
                            status="done",
                            payload={**page, "expired": True, "platform": meta["platform"]},
                        )
                    )
                    continue

                estimate = self._estimate(scope)
                pushed_age = _age_days(repo.get("pushed_at"))
                open_hint = "recent" if pushed_age <= 30 else "stale"

                probability = PROBABILITY_AT_LEAST_ONE
                pool = page.get("prize_pool_usd")
                if hours_left is not None and hours_left < estimate["hours"]:
                    # Времени меньше, чем нужно на саму работу: шанс падает резко,
                    # и это честнее показать, чем держать оценку на прежнем уровне.
                    probability *= 0.25
                if pool and pool < 20_000:
                    probability *= 0.6
                expected = probability * MEDIAN_FINDING_AWARD
                ev_per_hour = (expected - estimate["hours"] * COST_PER_HOUR * 0.2) / estimate["hours"]
                estimate = {
                    **estimate,
                    "probability": round(probability, 3),
                    "expected_value_usd": round(expected, 2),
                    "ev_per_hour": round(ev_per_hour, 2),
                }

                rationale = (
                    f"{meta['platform']}; создан {age} дн. назад; "
                    f"объём работ ~{estimate['hours']}ч; "
                    f"ориентир: медианная выплата за находку ${MEDIAN_FINDING_AWARD:,.0f}, "
                    f"шанс хотя бы одной зачётной находки ~{probability:.0%}; "
                    f"EV/час ~${estimate['ev_per_hour']:,.1f}"
                )
                if pool:
                    rationale += f"; призовой пул ${pool:,.0f} (из README контеста)"
                if hours_left is not None:
                    rationale += f"; до конца приёма работ {hours_left:.0f}ч"
                    if hours_left < estimate["hours"]:
                        rationale += " — времени меньше, чем нужно на саму работу"
                if scope.get("scope_solidity"):
                    rationale += f"; в scope {scope['scope_solidity']} Solidity-файлов"
                elif scope.get("scope_rust"):
                    rationale += f"; в scope {scope['scope_rust']} Rust-файлов"
                if pushed_age > 30:
                    rationale += "; давно нет активности — проверьте, не закрыт ли контест"

                opportunities.append(
                    Opportunity(
                        id=f"contest:{full_name}",
                        channel=self.name,
                        title=f"{meta['platform']}: {name}",
                        url=str(repo.get("html_url")),
                        repo=full_name,
                        reward_usd=MEDIAN_FINDING_AWARD,
                        reward_source="оценка по публичной медиане выплат",
                        score=round(max(0.0, estimate["ev_per_hour"]), 2),
                        rationale=rationale,
                        payload={
                            "platform": meta["platform"],
                            "platform_page": meta["page"],
                            "age_days": age,
                            "pushed_age_days": pushed_age,
                            "open_hint": open_hint,
                            "language": repo.get("language"),
                            "effort_hours": estimate["hours"],
                            "probability": estimate["probability"],
                            "ev_per_hour": estimate["ev_per_hour"],
                            "expected_value_usd": estimate["expected_value_usd"],
                            "assumptions": [
                                f"медианная выплата за находку ${MEDIAN_FINDING_AWARD:,.0f} "
                                "(публичные данные платформ)",
                                f"вероятность хотя бы одной зачётной находки {PROBABILITY_AT_LEAST_ONE:.0%} "
                                "для подготовленного участника",
                                "призовой пул конкретного контеста указан на платформе",
                            ],
                            "verify_before_work": [
                                f"Открыт ли контест и какой дедлайн: {meta['page']}",
                                "Размер призового пула и правила начисления долей",
                                "Есть ли у вас нужный стек (Solidity/Rust/Cairo)",
                            ],
                            **scope,
                            **page,
                        },
                    )
                )

        opportunities.sort(key=lambda o: o.score, reverse=True)
        return opportunities[:limit]
