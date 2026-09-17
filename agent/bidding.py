from __future__ import annotations

from agent.models import BidRecommendation, Task


def calculate_bid(task: Task, bid_margin: float = 1.2) -> BidRecommendation:
    cost = max(task.budget * 0.35, 1.0)
    bid = round(cost * bid_margin, 2)
    if task.competition > 0:
        bid = min(task.budget, max(bid, task.budget * 0.45))
    if task.budget > 0:
        bid = min(task.budget, max(bid, task.budget * 0.5))
    reason = (
        f"Cost-based bid with margin {bid_margin}. "
        f"Task budget ${task.budget}, competition {task.competition}, difficulty {task.difficulty}."
    )
    confidence = 0.65 + min(task.difficulty, 0.3) + min(task.competition * 0.1, 0.1)
    return BidRecommendation(
        task_id=task.id,
        platform=task.platform,
        bid=bid,
        reason=reason,
        confidence=max(0.0, min(confidence, 0.95)),
    )


def should_bid(task: Task) -> bool:
    if task.budget <= 0:
        return False
    if task.category in {"data_analysis", "research", "writing"}:
        return True
    return task.budget >= 5.0
