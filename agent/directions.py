"""Three income directions, worked in parallel.

The farm runs three directions at once instead of betting everything on one:

1. **Быстрые деньги** — открытые bounty на GitHub. Низкий порог входа, деньги
   небольшие, зато можно получить первую выплату быстрее всего.
2. **Потолок** — аудит-контесты. В разы больше за час, но требует навыка и
   времени на обучение.
3. **Без банка** — площадки для агентов с выплатой в USDC. Небольшие суммы за
   задачу, зато выплата не зависит от страны и Stripe, и порог входа минимален.

Working all three in parallel is the point: direction 1 pays the bills while you
learn, direction 2 raises the ceiling, direction 3 keeps money flowing even when
bank rails are unavailable. This module turns that idea into numbers — it reads
the ledger and says where the next hour is worth spending.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

from agent.ledger import analytics, connect


@dataclass
class Direction:
    key: str
    title: str
    channel: str
    promise: str
    entry_cost: str
    ceiling: str
    payout_rail: str
    next_step: str
    tasks: int = 0
    ev_per_hour: float = 0.0
    pipeline_hours: float = 0.0
    verified_usd: float = 0.0
    hours_logged: float = 0.0
    share: float = 0.0
    status: str = "нет задач"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "channel": self.channel,
            "promise": self.promise,
            "entry_cost": self.entry_cost,
            "ceiling": self.ceiling,
            "payout_rail": self.payout_rail,
            "next_step": self.next_step,
            "tasks": self.tasks,
            "ev_per_hour": round(self.ev_per_hour, 2),
            "pipeline_hours": round(self.pipeline_hours, 1),
            "verified_usd": round(self.verified_usd, 2),
            "hours_logged": round(self.hours_logged, 1),
            "share": round(self.share, 3),
            "status": self.status,
        }


DIRECTIONS: List[Direction] = [
    Direction(
        key="fast_money",
        title="1. Быстрые деньги: bounty на GitHub",
        channel="github_bounties",
        promise="первая выплата быстрее всего, порог входа низкий",
        entry_cost="низкий: нужен только GitHub",
        ceiling="$50–$2,500 за задачу",
        payout_rail="stripe_card (Algora, Opire) — проверьте свою страну",
        next_step="python -m agent.main дальше (только задачи со свободной конкуренцией)",
    ),
    Direction(
        key="ceiling",
        title="2. Потолок: аудит-контесты",
        channel="audit_contests",
        promise="в разы больше за час, потому что платят за качество находки",
        entry_cost="высокий: нужен Solidity/Rust и практика",
        ceiling="медиана находки ~$2,000; топовые результаты — десятки тысяч",
        payout_rail="USDC / ончейн / Stripe (зависит от платформы)",
        next_step="python -m agent.main досье <id> — он же покажет scope и файлы",
    ),
    Direction(
        key="no_bank",
        title="3. Без банка: площадки для агентов",
        channel="agent_marketplaces",
        promise="выплата в USDC не зависит от страны и Stripe",
        entry_cost="минимальный: API-ключ, кошелёк",
        ceiling="$10–$500 за квест; регулярность важнее размера",
        payout_rail="usdc_wallet (USDC на Base, минимум 10 USDC)",
        next_step="получить AGENTHANSA_API_KEY и добавить в .env",
    ),
]


def _queue_stats() -> Dict[str, Dict[str, float]]:
    """Per-channel: number of attractive tasks, EV/hour, hours, EV total."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT channel, payload FROM opportunities WHERE status='queued'"
        ).fetchall()
    finally:
        conn.close()

    import json

    stats: Dict[str, Dict[str, float]] = {}
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except Exception:
            payload = {}
        ev_hour = float(payload.get("ev_per_hour") or 0.0)
        if ev_hour <= 0:
            continue
        entry = stats.setdefault(
            row["channel"], {"tasks": 0, "ev_sum": 0.0, "hours": 0.0, "best": 0.0}
        )
        entry["tasks"] += 1
        entry["ev_sum"] += ev_hour
        entry["hours"] += float(payload.get("effort_hours") or 0.0)
        entry["best"] = max(entry["best"], ev_hour)
    return stats


def _logged_hours() -> Dict[str, float]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT channel, SUM(hours) AS hours FROM hours GROUP BY channel"
        ).fetchall()
    finally:
        conn.close()
    return {row["channel"]: float(row["hours"] or 0.0) for row in rows}


def portfolio() -> Dict[str, Any]:
    """Live state of all three directions plus a time-allocation suggestion."""
    stats = _queue_stats()
    hours_by_channel = _logged_hours()
    income = analytics()

    directions: List[Direction] = []
    for template in DIRECTIONS:
        item = Direction(**{k: v for k, v in template.__dict__.items()})
        channel_stats = stats.get(item.channel, {})
        item.tasks = int(channel_stats.get("tasks", 0))
        item.ev_per_hour = round(float(channel_stats.get("best", 0.0)), 2)
        item.pipeline_hours = round(float(channel_stats.get("hours", 0.0)), 1)
        item.hours_logged = round(hours_by_channel.get(item.channel, 0.0), 1)
        item.verified_usd = round(
            next(
                (
                    row["verified_usd"]
                    for row in income["by_channel"]
                    if row["channel"] == item.channel
                ),
                0.0,
            ),
            2,
        )
        if item.tasks:
            item.status = f"{item.tasks} задач в очереди, лучший EV/час ${item.ev_per_hour:,.1f}"
        elif item.key == "no_bank":
            item.status = "нужен API-ключ площадки"
        elif item.key == "ceiling":
            item.status = "нет свежих контестов — проверять раз в неделю"
        else:
            item.status = "нет подходящих задач — запустить cycle"
        directions.append(item)

    # Allocation: weight by measured EV/hour, but never zero out a direction.
    # A direction with no tasks yet keeps a small share so it still gets attention.
    weights = []
    for item in directions:
        if item.ev_per_hour > 0:
            weights.append(min(item.ev_per_hour, 40.0))
        else:
            weights.append(2.0)
    total_weight = sum(weights) or 1.0
    for item, weight in zip(directions, weights):
        item.share = round(weight / total_weight, 3)

    best = max(directions, key=lambda d: d.ev_per_hour)
    return {
        "directions": [item.as_dict() for item in directions],
        "planned": {
            "verified_usd": income["verified_usd"],
            "hours_logged": income["hours_logged"],
            "effective_usd_per_hour": income["effective_usd_per_hour"],
            "pipeline_ev_usd": income["pipeline_ev_usd"],
        },
        "focus": best.key,
        "advice": _advice(directions, income),
    }


def _advice(directions: List[Direction], income: Dict[str, Any]) -> List[str]:
    advice: List[str] = []
    by_key = {item.key: item for item in directions}

    fast = by_key["fast_money"]
    ceiling = by_key["ceiling"]
    no_bank = by_key["no_bank"]

    if ceiling.tasks and ceiling.ev_per_hour > fast.ev_per_hour:
        advice.append(
            f"Ставка на контесты: EV/час ${ceiling.ev_per_hour:,.1f} против "
            f"${fast.ev_per_hour:,.1f} у bounty. Это направление с самым высоким потолком."
        )
    if not no_bank.tasks:
        advice.append(
            "Третье направление простаивает: получите ключ площадки для агентов — "
            "это единственный канал, который не зависит от банковских ограничений."
        )
    if income["hours_logged"] == 0:
        advice.append(
            "Часы пока не логируются: без этого нельзя узнать реальную ставку. "
            "Записывайте время на каждой задаче (hours-add)."
        )
    if income["verified_usd"] == 0:
        advice.append(
            "Подтверждённого дохода ещё нет — это нормальный старт. Первая цель: "
            "довести одну задачу до выплаты и зафиксировать её через payout-verify."
        )
    if fast.tasks == 0:
        advice.append(
            "По bounty нет привлекательных задач: рынок перегружен. Запускайте cycle "
            "чаще (loop) — выигрывают те, кто берёт задачу в первые часы."
        )
    return advice


def summary_lines() -> List[str]:
    data = portfolio()
    lines = ["Три направления — где вы сейчас:"]
    for item in data["directions"]:
        lines.append(
            f"  {item['title']}: {item['status']}"
            f" · доля времени {item['share'] * 100:.0f}%"
            f" · выплата: {item['entry_cost']}"
        )
    lines.append("")
    for line in data["advice"]:
        lines.append(f"  • {line}")
    return lines
