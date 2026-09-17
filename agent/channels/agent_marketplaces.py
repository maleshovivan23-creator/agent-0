"""Live worker: marketplaces built specifically for AI agents.

Third income direction, next to GitHub bounties and audit contests. These
platforms were designed for agents: registration with an API key instead of a
human profile, structured JSON instead of web forms, payouts in USDC instead of
bank rails. That last part matters a lot if Stripe-based payouts are not
available in your country.

What is verified and what is not
--------------------------------
* Endpoints below come from the platform's own agent onboarding material
  (AgentHansa documents them at ``/llms.txt`` and in its agent guide).
* The sandbox this project was built in cannot reach ``agenthansa.com``, so the
  worker is written to *degrade honestly*: without an API key it reports
  "нужен ключ", with a key it uses the documented routes, and any HTTP failure
  is surfaced instead of being papered over.
* Numbers are the platform's own published ranges: quests $10-500, red packets
  $0.10-1.00, minimum payout 10 USDC, payouts in USDC on Base.

Why it still belongs in the farm: it is the only direction with **no country
gate on the payout rail** and a very low entry cost, which makes it a sensible
baseline income while the higher-ceiling directions are still learning.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from agent.channels.base import Channel
from agent.config import get_env
from agent.http import build_session
from agent.ledger import Opportunity

#: Documented AgentHansa routes (agent API, Bearer auth).
AGENTHANSA = {
    "name": "agenthansa",
    "title": "AgentHansa (квесты, USDC на Base)",
    "base": "https://agenthansa.com/api",
    "quests": "/alliance-war/quests",
    "earnings": "/agents/earnings",
    "submit_template": "/alliance-war/quests/{quest_id}/submit",
    "min_payout_usdc": 10.0,
    "reward_range": (10.0, 500.0),
    "docs": "https://agenthansa.com/llms.txt",
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
        key = self.api_key(platform)
        headers = {"Accept": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return build_session(f"AGENT-0-agent-marketplace/{platform}", headers)

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
        key = self.api_key("agenthansa")
        if not key:
            self.notes.append(
                "AgentHansa: нужен ключ. Регистрация агента возвращает API-ключ "
                f"({AGENTHANSA['docs']}), после этого добавьте AGENTHANSA_API_KEY в .env"
            )
            return []

        base = (get_env("AGENTHANSA_BASE_URL", "") or AGENTHANSA["base"]).rstrip("/")
        session = self._session("agenthansa")
        try:
            response = session.get(base + AGENTHANSA["quests"], timeout=25)
        except Exception as exc:
            self.last_error = f"AgentHansa недоступна из этой среды: {exc.__class__.__name__}"
            return []

        if response.status_code == 401:
            self.last_error = "AgentHansa: ключ не принят (401). Проверьте AGENTHANSA_API_KEY."
            return []
        if response.status_code != 200:
            self.last_error = f"AgentHansa HTTP {response.status_code}"
            return []

        payload = response.json()
        quests = payload.get("quests") if isinstance(payload, dict) else payload
        if not isinstance(quests, list):
            self.last_error = "AgentHansa: неожиданный формат ответа"
            return []

        now = datetime.now(timezone.utc)
        items: List[Opportunity] = []
        for quest in quests[:limit]:
            if not isinstance(quest, dict):
                continue
            reward = _number(quest.get("reward_amount") or quest.get("reward"))
            if reward <= 0:
                continue
            title = str(quest.get("title") or "Квест без названия")
            quest_id = str(quest.get("id") or quest.get("quest_id") or title[:40])
            submissions = int(_number(quest.get("submission_count") or quest.get("submissions")))
            cap = int(_number(quest.get("submission_cap") or quest.get("cap")))

            hours = _estimate_hours(quest, reward)
            # Competition on these platforms is measured directly: submissions
            # against the cap. 50/50 means the queue is full.
            probability = 0.35
            if cap:
                probability *= max(0.05, 1.0 - submissions / cap)
            elif submissions:
                probability *= max(0.05, 1.0 / (1.0 + submissions / 20.0))
            expected = probability * reward
            ev_per_hour = (expected - hours * 5.0 * 0.15) / hours if hours else 0.0

            deadline_note = ""
            hours_left = _hours_left(quest.get("deadline"), now)
            if hours_left is not None:
                deadline_note = f"; до дедлайна {hours_left:.0f}ч"
                if hours_left < MIN_HOURS_LEFT:
                    probability *= 0.2

            rationale = (
                f"платформа {AGENTHANSA['title']}; выплата USDC на Base "
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
                    url=str(quest.get("url") or quest.get("link") or ""),
                    repo=AGENTHANSA["name"],
                    reward_usd=reward,
                    reward_source="награда квеста (площадка)",
                    score=round(max(0.0, ev_per_hour), 2),
                    rationale=rationale,
                    payload={
                        "platform": AGENTHANSA["name"],
                        "quest_id": quest_id,
                        "description": _short(quest.get("description"), 2000),
                        "requirements": _short(quest.get("requirements"), 800),
                        "proof_hint": _short(quest.get("proof_hint"), 400),
                        "submissions": submissions,
                        "submission_cap": cap,
                        "deadline": quest.get("deadline"),
                        "hours_left": hours_left,
                        "effort_hours": hours,
                        "probability": round(probability, 3),
                        "ev_per_hour": round(ev_per_hour, 2),
                        "expected_value_usd": round(expected, 2),
                        "payout_rail": "crypto_usdc",
                        "payout_note": (
                            "Выплата в USDC на Base через кошелёк площадки; "
                            "нужен кошелёк и привязка к аккаунту агента."
                        ),
                        "docs": AGENTHANSA["docs"],
                        "submit_route": base + AGENTHANSA["submit_template"].format(
                            quest_id=quest_id
                        ),
                        "verify_before_work": [
                            "Прочитать правила квеста и критерии приёмки на площадке.",
                            "Проверить дедлайн и лимит заявок: часть квестов закрывается быстро.",
                            "Убедиться, что кошелёк привязан (иначе выплата не уйдёт).",
                        ],
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
