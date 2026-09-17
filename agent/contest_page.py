"""Разбор страницы контеста: пул, даты и репозиторий с кодом.

README аудит-контеста — самый информативный документ во всём цикле: в нём
написано, сколько денег в призовом пуле, когда контест начинается и
заканчивается, и где лежит код. Раньше ферма работала с медианой выплат и
догадкой о сроке; теперь берёт факты, когда они опубликованы.

Здесь только разбор текста — никаких запросов и никаких предположений: если
дата не написана, поле остаётся пустым, а не заполняется «примерно».
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

#: «Total Prize Pool: $107,000 in USDC» и варианты формулировки.
POOL_PATTERNS = (
    re.compile(r"total\s+prize\s+pool[:\s]*\$([\d,]+)", re.IGNORECASE),
    re.compile(r"prize\s+pool[:\s]*\$([\d,]+)", re.IGNORECASE),
    re.compile(r"total\s+awards?[:\s]*\$([\d,]+)", re.IGNORECASE),
    re.compile(r"\$([\d,]+)\s+(?:in\s+\w+\s+)?(?:prize|awards?)", re.IGNORECASE),
)

#: Даты в формате «February 12, 2026 20:00 UTC» (так пишет Code4rena и Sherlock).
DATE_PATTERNS = (
    re.compile(
        r"starts?\s+([A-Z][a-z]+ \d{1,2}, \d{4}(?:\s+\d{1,2}:\d{2})?)\s*(UTC)?",
        re.IGNORECASE,
    ),
    re.compile(
        r"ends?\s+([A-Z][a-z]+ \d{1,2}, \d{4}(?:\s+\d{1,2}:\d{2})?)\s*(UTC)?",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:contest\s+period|audit\s+period)[:\s]*([A-Z][a-z]+ \d{1,2}, \d{4})",
        re.IGNORECASE,
    ),
)

#: Где лежит код: «Repo: https://github.com/…», «[Code](https://github.com/…)».
REPO_HINTS = (
    re.compile(r"(?:repo|repository|code|код)\b[^\n]{0,60}?github\.com/([\w.-]+/[\w.-]+)", re.IGNORECASE),
    re.compile(r"\[(?:code|repo|repository)\]\(https?://github\.com/([\w.-]+/[\w.-]+)\)", re.IGNORECASE),
)
ANY_REPO = re.compile(r"github\.com/([\w.-]+/[\w.-]+)")

DATE_FORMATS = ("%B %d, %Y %H:%M", "%B %d, %Y")


@dataclass
class ContestPage:
    prize_pool_usd: Optional[float] = None
    starts_at: Optional[str] = None
    ends_at: Optional[str] = None
    hours_left: Optional[float] = None
    code_repos: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def found_anything(self) -> bool:
        return bool(self.prize_pool_usd or self.ends_at or self.code_repos)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "prize_pool_usd": self.prize_pool_usd,
            "starts_at": self.starts_at,
            "ends_at": self.ends_at,
            "hours_left": self.hours_left,
            "code_repos": self.code_repos,
            "notes": self.notes,
        }


def _money(raw: str) -> Optional[float]:
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None


def _parse_date(raw: str) -> Optional[datetime]:
    cleaned = re.sub(r"\s+", " ", raw.strip())
    for fmt in DATE_FORMATS:
        try:
            moment = datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
        return moment.replace(tzinfo=timezone.utc)
    return None


def parse(text: str, now: Optional[datetime] = None) -> ContestPage:
    """Вытащить из README то, что там действительно написано."""
    page = ContestPage()
    if not text:
        return page

    for pattern in POOL_PATTERNS:
        match = pattern.search(text)
        if match:
            amount = _money(match.group(1))
            if amount and amount >= 100:  # «$50 in gas» — не призовой пул
                page.prize_pool_usd = amount
                break

    start_match = DATE_PATTERNS[0].search(text)
    if start_match:
        moment = _parse_date(start_match.group(1))
        if moment:
            page.starts_at = moment.isoformat(timespec="seconds")

    end_match = DATE_PATTERNS[1].search(text)
    if end_match:
        moment = _parse_date(end_match.group(1))
        if moment:
            page.ends_at = moment.isoformat(timespec="seconds")

    if not page.ends_at:
        period_match = DATE_PATTERNS[2].search(text)
        if period_match:
            moment = _parse_date(period_match.group(1))
            if moment:
                page.ends_at = moment.isoformat(timespec="seconds")

    repos: List[str] = []
    for pattern in REPO_HINTS:
        for match in pattern.findall(text):
            cleaned = match.rstrip(").,/").removesuffix(".git")
            if cleaned and cleaned not in repos:
                repos.append(cleaned)
    if not repos:
        mentions: Dict[str, int] = {}
        for match in ANY_REPO.findall(text):
            cleaned = match.rstrip(").,/").removesuffix(".git")
            if cleaned:
                mentions[cleaned] = mentions.get(cleaned, 0) + 1
        repos = [name for name, count in sorted(mentions.items(), key=lambda pair: -pair[1])[:2]
                 if count >= 2]
    page.code_repos = repos[:3]

    reference = now or datetime.now(timezone.utc)
    if page.ends_at:
        try:
            end = datetime.fromisoformat(page.ends_at)
        except ValueError:
            end = None
        if end:
            page.hours_left = round((end - reference).total_seconds() / 3600.0, 1)
            if page.hours_left < 0:
                page.notes.append("контест уже закончился по датам из README")
            elif page.hours_left < 48:
                page.notes.append(
                    f"до конца {page.hours_left:.0f} ч — успеть можно только с готовым стеком"
                )
    if page.prize_pool_usd and page.prize_pool_usd < 5_000:
        page.notes.append(
            f"призовой пул небольшой (${page.prize_pool_usd:,.0f}): при большом числе "
            "участников выплата за находку будет ниже медианной"
        )
    return page
