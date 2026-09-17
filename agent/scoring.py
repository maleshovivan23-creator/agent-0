"""Opportunity scoring.

The farm is only useful if it can tell a $700 funded bounty apart from a joke
issue titled "BOUNTY $999999999999999999". This module does three things:

1. extracts a realistic reward amount from labels / title / body;
2. estimates effort in hours from issue metadata;
3. converts both into expected value per hour, with an explicit probability of
   success, so the operator sees *why* a task was ranked where it was.

All numbers are intentionally conservative. An over-optimistic scorer is worse
than no scorer: it sends the agent to lose time on unwinnable tasks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

#: Anything above this is almost certainly a joke or a scam bait.
MAX_PLAUSIBLE_REWARD = 50_000.0
#: Below this a task is not worth the context switch.
MIN_USEFUL_REWARD = 20.0

_MONEY_IN_TEXT = re.compile(
    r"(?:\$|USD\s*|USDC\s*)(\d[\d,]*(?:\.\d+)?)|(\d[\d,]*)\s*(?:USD|USDC|\$)",
    re.IGNORECASE,
)
_LABEL_MONEY = re.compile(r"^\$?\s*(\d[\d,]*(?:\.\d+)?)\s*\$?$")


@dataclass
class Effort:
    hours: float
    components: Dict[str, float] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)


@dataclass
class Score:
    total: float
    expected_value_usd: float
    ev_per_hour: float
    probability: float
    reward_usd: float
    reward_source: str
    effort_hours: float
    rationale: str
    components: Dict[str, float] = field(default_factory=dict)


def parse_amount(raw: object) -> Optional[float]:
    """Parse a single money token. Returns None if implausible."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
    else:
        text = str(raw).strip()
        if not text:
            return None
        match = _LABEL_MONEY.match(text)
        if match:
            value = float(match.group(1).replace(",", ""))
        else:
            found = _MONEY_IN_TEXT.search(text)
            if not found:
                return None
            token = found.group(1) or found.group(2)
            value = float(token.replace(",", ""))
    if value < 1 or value > MAX_PLAUSIBLE_REWARD:
        return None
    return value


def extract_reward(
    labels: List[str], title: str, body: str
) -> tuple[float, str]:
    """Best-effort reward extraction.

    Priority: explicit money labels > title > body. Returns (amount, source).
    """
    candidates: List[tuple[float, str]] = []

    for label in labels:
        amount = parse_amount(label)
        if amount:
            candidates.append((amount, f"label:{label}"))

    title_amount = parse_amount(title)
    if title_amount:
        candidates.append((title_amount, "title"))

    body_amount = parse_amount(body[:1500]) if body else None
    if body_amount:
        candidates.append((body_amount, "body"))

    if not candidates:
        return 0.0, "none"

    # Labels are the most reliable signal (bounty platforms label the amount).
    by_source = {source.split(":")[0]: (amount, source) for amount, source in reversed(candidates)}
    for key in ("label", "title", "body"):
        if key in by_source:
            amount, source = by_source[key]
            return amount, source
    amount, source = candidates[-1]
    return amount, source


def looks_like_junk(title: str, reward_usd: float, repo_stars: int) -> Optional[str]:
    """Filter obvious spam, joke tasks and bait. Returns a reason or None."""
    lowered = title.lower()
    if reward_usd > MAX_PLAUSIBLE_REWARD:
        return "награда нереалистична (похоже на шутку или приманку)"
    if re.search(r"(\d)\1{6,}", title.replace(",", "").replace(" ", "")):
        return "повторяющиеся цифры в сумме награды"
    if "test issue" in lowered or lowered.startswith("test"):
        return "тестовая задача, не оплачивается"
    junk_markers = ("the universe", "hello world", "printer go brrr")
    if any(marker in lowered for marker in junk_markers):
        return "шутливая задача без реального заказчика"
    if repo_stars == 0 and reward_usd >= 5_000:
        return "очень крупная награда в репозитории без истории"
    return None


def estimate_effort(
    comments: int,
    labels: List[str],
    body_len: int,
) -> Effort:
    """Rough effort estimate in hours. Deliberately padded."""
    hours = 2.0
    components: Dict[str, float] = {"base": 2.0}
    notes: List[str] = []

    lowered = [label.lower() for label in labels]
    if any("good first issue" in label for label in lowered):
        hours -= 0.5
        components["good_first_issue"] = -0.5
        notes.append("помечена как good first issue")
    if any("documentation" in label for label in lowered):
        hours -= 0.5
        components["documentation"] = -0.5
        notes.append("документация — быстрый тип задачи")
    if any("ai agent friendly" in label for label in lowered):
        hours -= 0.25
        components["agent_friendly"] = -0.25
        notes.append("явно помечена как дружелюбная к агентам")

    competition = min(comments / 25.0, 3.0)
    if competition > 0:
        hours += competition
        components["competition"] = round(competition, 2)
        notes.append(f"{comments} комментариев — высокая конкуренция")

    if body_len < 200:
        hours += 0.5
        components["vague_spec"] = 0.5
        notes.append("короткое описание, требования придётся уточнять")
    elif body_len > 4000:
        hours += 0.5
        components["large_scope"] = 0.5
        notes.append("очень объёмное описание")

    hours = max(0.5, min(hours, 12.0))
    return Effort(hours=hours, components=components, notes=notes)


def probability_of_success(labels: List[str], comments: int, repo_stars: int) -> float:
    """Conservative chance that our attempt actually gets merged and paid."""
    p = 0.12
    lowered = [label.lower() for label in labels]
    if any("good first issue" in label for label in lowered):
        p += 0.08
    if any("ai agent friendly" in label for label in lowered):
        p += 0.06
    if repo_stars >= 1000:
        p += 0.04
    elif repo_stars == 0:
        p -= 0.04
    if comments > 50:
        p -= 0.05
    if comments > 200:
        p -= 0.05
    return max(0.02, min(p, 0.45))


def score_opportunity(
    *,
    labels: List[str],
    title: str,
    body: str,
    comments: int,
    repo_stars: int,
    hourly_rate: float,
) -> Score:
    reward, reward_source = extract_reward(labels, title, body)
    effort = estimate_effort(comments, labels, len(body or ""))
    probability = probability_of_success(labels, comments, repo_stars)

    expected = probability * reward - effort.hours * hourly_rate * 0.15
    ev_per_hour = expected / effort.hours if effort.hours else 0.0

    # Composite score: mostly EV/hour, lightly tempered by reward size so that
    # several small wins can outrank one lottery ticket.
    total = max(0.0, ev_per_hour) + min(reward / 100.0, 8.0)
    if reward < MIN_USEFUL_REWARD:
        total *= 0.3

    rationale_bits = [
        f"награда ${reward:,.0f}" if reward else "награда не указана",
        f"источник: {reward_source}",
        f"оценка {effort.hours:.1f}ч",
        f"шанс успеха ~{probability:.0%}",
        f"EV/час ~${ev_per_hour:,.1f}",
    ]
    rationale_bits.extend(effort.notes)
    rationale = "; ".join(rationale_bits)

    return Score(
        total=round(total, 2),
        expected_value_usd=round(expected, 2),
        ev_per_hour=round(ev_per_hour, 2),
        probability=round(probability, 3),
        reward_usd=reward,
        reward_source=reward_source,
        effort_hours=effort.hours,
        rationale=rationale,
        components=effort.components,
    )
