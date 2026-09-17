"""Live worker: marketplaces built specifically for AI agents.

Third income direction, next to GitHub bounties and audit contests. These
platforms were designed for agents: registration with an API key instead of a
human profile, structured JSON instead of web forms, payouts in USDC instead of
bank rails. That last part matters a lot if Stripe-based payouts are not
available in your country.

What is verified and what is not
--------------------------------
* Endpoints and field names come from the platform's own open-source CLI
  (``agent-hansa-mcp`` 0.10.0 from npm) and its agent guide, so the worker speaks
  the real API instead of guessing. All HTTP goes through ``agent.hansa``.
* This sandbox cannot reach the domain (GitHub/PyPI/npm only), so the worker is
  written to *degrade honestly*: without a key it says "нужен ключ", and when the
  network or the key fails the reason is surfaced instead of being papered over.
  The loop itself runs where the internet is open (GitHub Actions or a laptop).
* Numbers are the platform's own published ranges: quests $10-500, red packets
  $0.10-1.00, minimum payout 10 USDC, payouts in USDC on Base.

Why it still belongs in the farm: it is the only direction with **no country
gate on the payout rail** and a very low entry cost, which makes it a sensible
baseline income while the higher-ceiling directions are still learning.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from agent import hansa
from agent.channels.base import Channel
from agent.config import get_env
from agent.ledger import Opportunity

#: Documented AgentHansa routes (agent API, Bearer auth).
AGENTHANSA = {
    "name": "agenthansa",
    "title": "AgentHansa (квесты, USDC на Base)",
    "base": "https://www.agenthansa.com/api",
    "quests": "/alliance-war/quests",
    "earnings": "/agents/earnings",
    "submit_template": "/alliance-war/quests/{quest_id}/submit",
    "min_payout_usdc": 10.0,
    "reward_range": (10.0, 500.0),
    "docs": "https://www.agenthansa.com/llms.txt",
}

#: A quest with a stated deadline sooner than this is probably not worth starting.
MIN_HOURS_LEFT = 3.0


class AgentMarketplacesChannel(Channel):
    name = "agent_marketplaces"
    title = "Площадки для агентов (USDC, без банка)"
    capability = "read_public_data"
    description = (
        "Читает открытые квесты площадок, построенных под агентов (AgentHansa и "
        "подобные). Выплата в USDC, банк и Stripe не нужны — то есть направление "
        "работает там, где карточные выплаты недоступны. Публикация результата — "
        "только после подтверждения человеком."
    )

    def __init__(self, platforms: Optional[List[str]] = None) -> None:
        super().__init__()
        self.platforms = platforms or ["agenthansa"]
        self.last_error = ""
        self.notes: List[str] = []

    # ------------------------------------------------------------------ keys

    @staticmethod
    def api_key(platform: str) -> str:
        env_name = f"{platform.upper()}_API_KEY"
        return get_env(env_name, "") or ""

    def _session(self, platform: str):
        """Сессия собирается там же, где всё остальное общение с площадкой.

        Раньше канал делал свою сессию, и тесты подменяли сеть в одном месте,
        а код ходил через другое — ошибка вылезала только на живом запуске.
        """
        key = self.api_key(platform)
        headers = {"Accept": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return hansa.build_session(f"AGENT-0-agent-marketplace/{platform}", headers)

    # -------------------------------------------------------------- harvesting

    def harvest(self, limit: int = 25) -> List[Opportunity]:
        if not self.allowed:
            return []

        opportunities: List[Opportunity] = []
        for platform in self.platforms:
            if platform != "agenthansa":
                self.notes.append(f"платформа {platform} ещё не подключена")
                continue
            opportunities.extend(self._harvest_agenthansa(limit))
        opportunities.sort(key=lambda item: item.score, reverse=True)
        return opportunities[:limit]

    def _harvest_agenthansa(self, limit: int) -> List[Opportunity]:
        """Забрать квесты площадки через настоящий клиент (agent/hansa.py).

        Раньше здесь был свой набор угаданных полей и адрес без www: и то и
        другое ломало разбор на живом API. Теперь контракт один — на клиент,
        который вычитан из официального onboarding-материала площадки.
        """
        quests: List[hansa.Quest] = []
        origin = ""

        if hansa.api_key():
            client = hansa.Hansa(session=self._session("agenthansa"))
            try:
                quests = client.quests()
                origin = "живой API"
            except hansa.HansaError as exc:
                self.last_error = f"AgentHansa: {exc}"
            except Exception as exc:
                # Сеть до площадки может отвалиться целиком (в песочнице
                # разработки домен вообще закрыт). Обещание модуля — деградировать
                # честно: канал сообщает причину и не валит весь цикл фермы.
                self.last_error = f"AgentHansa недоступна: {exc.__class__.__name__}: {exc}"
        else:
            self.notes.append(
                "AgentHansa: нужен ключ. Регистрация агента возвращает API-ключ "
                f"({AGENTHANSA['docs']}), после этого добавьте AGENTHANSA_API_KEY в .env"
            )

        # Живого ответа нет — берём последний отчёт цикла площадки. Он снят там,
        # где домен открыт (GitHub Actions), и лежит в репозитории, поэтому
        # ферма видит квесты даже из песочницы с закрытой сетью.
        snapshot: Optional[hansa.Snapshot] = None
        if not quests:
            snapshot = hansa.read_snapshot()
            if snapshot.usable:
                quests = snapshot.quests
                origin = snapshot.note()
                self.notes.append(f"AgentHansa: {origin}")
            elif snapshot.problem:
                self.notes.append(f"AgentHansa: {snapshot.problem}")

        if not quests:
            if hansa.api_key():
                self.notes.append("AgentHansa: открытых квестов нет — заходите позже")
            return []

        now = datetime.now(timezone.utc)
        items: List[Opportunity] = []
        for quest in quests[:limit]:
            reward = quest.reward_usd
            if reward <= 0:
                continue
            title = quest.title
            quest_id = quest.id or title[:40]
            submissions, cap = quest.submissions, quest.cap

            hours = _estimate_hours({"description": quest.description}, reward)
            # Конкуренция на площадке измеряется напрямую: заявки против лимита.
            # Награды разбирают три альянса, поэтому базовая вероятность ниже,
            # чем у одиночного bounty: выигрывает один из многих.
            probability = 0.30
            if cap:
                probability *= max(0.05, 1.0 - submissions / cap)
            elif submissions:
                probability *= max(0.05, 1.0 / (1.0 + submissions / 20.0))
            expected = probability * reward
            ev_per_hour = (expected - hours * 5.0 * 0.15) / hours if hours else 0.0

            deadline_note = ""
            hours_left = _hours_left(quest.deadline, now)
            if hours_left is not None:
                deadline_note = f"; до дедлайна {hours_left:.0f}ч"
                if hours_left < MIN_HOURS_LEFT:
                    probability *= 0.2

            rationale = (
                f"платформа {AGENTHANSA['title']}; источник: {origin or 'неизвестен'}; "
                f"выплата USDC на Base "
                f"(минимум {AGENTHANSA['min_payout_usdc']:.0f} USDC, банк не нужен); "
                f"награда ${reward:,.0f}; заявок {submissions}"
                f"{f' из {cap}' if cap else ''}{deadline_note}; "
                f"оценка {hours:.1f}ч; EV/час ~${ev_per_hour:,.1f}"
            )

            items.append(
                Opportunity(
                    id=f"market:{AGENTHANSA['name']}:{quest_id}",
                    channel=self.name,
                    title=f"[AgentHansa] {title}",
                    url=quest.url,
                    repo=AGENTHANSA["name"],
                    reward_usd=reward,
                    reward_source="награда квеста (площадка)",
                    score=round(max(0.0, ev_per_hour), 2),
                    rationale=rationale,
                    payload={
                        "platform": AGENTHANSA["name"],
                        "quest_id": quest_id,
                        "source": "snapshot" if snapshot else "live",
                        "snapshot_age_hours": snapshot.age_hours if snapshot else None,
                        "description": _short(quest.description, 2000),
                        "requirements": _short(quest.requirements, 800),
                        "submissions": submissions,
                        "submission_cap": cap,
                        "deadline": quest.deadline,
                        "hours_left": hours_left,
                        "effort_hours": hours,
                        "probability": round(probability, 3),
                        "ev_per_hour": round(ev_per_hour, 2),
                        "expected_value_usd": round(expected, 2),
                        "payout_rail": "crypto_usdc",
                        "payout_note": (
                            "Выплата в USDC на Base по адресу из PAYOUT_WALLET: "
                            "привязать его на площадке командой "
                            "python -m agent.main площадка кошелёк"
                        ),
                        "docs": AGENTHANSA["docs"],
                        "submit_route": (hansa.base_url() + hansa.ROUTES["quest_submit"]).format(
                            quest_id=quest_id
                        ),
                        "verify_before_work": [
                            "Прочитать правила квеста и критерии приёмки на площадке.",
                            "Проверить дедлайн и лимит заявок: часть квестов закрывается быстро.",
                            "Убедиться, что кошелёк привязан (иначе выплата не уйдёт).",
                        ]
                        + (["Строки взяты из отчёта GitHub Actions, а не из живого API: "
                            "перед работой откройте квест на площадке."] if snapshot else []),
                    },
                )
            )
        return items


def _short(value: Any, limit: int) -> str:
    """Quest text is kept for drafting, but never unbounded."""
    text = " ".join(str(value or "").split())
    return text[:limit]


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _estimate_hours(quest: Dict[str, Any], reward: float) -> float:
    """Effort estimate. These quests are research/writing/review work."""
    text = str(quest.get("description") or "")
    length = len(text)
    hours = 1.5 + min(length / 1500.0, 3.0)
    if reward >= 100:
        hours += 1.5
    elif reward >= 40:
        hours += 0.5
    return max(1.0, min(hours, 8.0))


def _hours_left(deadline: Any, now: datetime) -> Optional[float]:
    if not deadline:
        return None
    try:
        moment = datetime.fromisoformat(str(deadline).replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0.0, (moment - now).total_seconds() / 3600.0)
