"""AgentHansa: работа на площадке, построенной под агентов.

Третье направление фермы. Площадка платит в USDC на Base, то есть там, где
карточные и Stripe-каналы не работают. Контракт взят не из догадок: он вычитан
из официального клиента площадки (``agent-hansa-mcp`` 0.10.0 с npm) и её же
agent-руководства (``skill.md``/``llms.txt``), поэтому здесь настоящие маршруты
и настоящие имена полей, а не предположения.

Что важного в устройстве площадки:

* **Регистрация — это один POST** с именем и описанием; ответ содержит ключ
  (``tabb_…``), который подписывает все дальнейшие запросы. Ключ — единственный
  секрет агента: ни паролей, ни ключей кошелька площадка не спрашивает.
* **Кошелёк привязывается адресом** (``PUT /api/agents/wallet``): без кошелька
  выплата держится 3-7 дней, с кошельком приходит сразу. Свой адрес ферма берёт
  из ``PAYOUT_WALLET`` и проверяет тем же модулем ``agent.wallet``.
* **Работа сабмитится вручную человеком** (``/alliance-war/quests/{id}/submit``,
  ``/collective/bounties/{id}/submit``). По политике фермы робот готовит текст и
  доказательство, но не отправляет их сам: методы сабмита требуют явного
  ``confirm=True``, а CLI — флага подтверждения.

Модуль намеренно ничего не делает в фоне: площадка pull-based, агент сам решает,
когда спросить ленту. Ни демонов, ни опроса в цикле.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from agent.config import get_env
from agent.http import build_session

#: Базовый адрес. Можно переопределить (AGENTHANSA_API) — например, для тестов.
DEFAULT_BASE = "https://www.agenthansa.com"

#: Площадка отдаёт награды в USDC на Base; минимум вывода — 10 USDC.
MIN_PAYOUT_USDC = 10.0

#: Сколько ждать ответа площадки.
TIMEOUT = 25

#: Пути, которые нужны для работы. Собраны из официального клиента.
ROUTES = {
    "register": "/api/agents/register",
    "me": "/api/agents/me",
    "checkin": "/api/agents/checkin",
    "feed": "/api/agents/feed",
    "inbox": "/api/agents/me/inbox",
    "work": "/api/agents/work",
    "quests": "/api/alliance-war/quests",
    "quest": "/api/alliance-war/quests/{quest_id}",
    "quest_submit": "/api/alliance-war/quests/{quest_id}/submit",
    "my_submissions": "/api/alliance-war/quests/my",
    "bounties": "/api/collective/bounties",
    "bounty_join": "/api/collective/bounties/{bounty_id}/join",
    "bounty_submit": "/api/collective/bounties/{bounty_id}/submit",
    "engagement": "/api/engagement",
    "engagement_submit": "/api/engagement/{assignment_id}/submit",
    "wallet": "/api/agents/wallet",
    "fluxa_wallet": "/api/agents/fluxa-wallet",
    "alliance": "/api/agents/alliance",
    "points": "/api/agents/points",
    "reputation": "/api/agents/reputation",
    "earnings": "/api/agents/earnings",
    "payouts": "/api/payouts",
    "onboarding": "/api/agents/onboarding-status",
    "claim_onboarding": "/api/agents/claim-onboarding-reward",
    "discord_code": "/api/agents/discord-code",
}


class HansaError(RuntimeError):
    """Ответ площадки, который нельзя считать успехом."""


class ConfirmationRequired(HansaError):
    """Попытка отправить работу без подтверждения человека — запрещена."""


def base_url() -> str:
    return (get_env("AGENTHANSA_API", "") or DEFAULT_BASE).rstrip("/")


def api_key() -> str:
    """Ключ агента. Площадка принимает только его — ни логина, ни пароля."""
    for name in ("AGENTHANSA_API_KEY", "BOUNTY_HUB_API_KEY"):
        value = (get_env(name, "") or "").strip()
        if value:
            return value
    return ""


def _mask(value: str) -> str:
    return f"{value[:8]}…{value[-4:]}" if len(value) > 14 else "…"


@dataclass
class Quest:
    """Квест в том виде, в каком он нужен очереди фермы."""

    id: str
    title: str
    reward_usd: float = 0.0
    description: str = ""
    requirements: str = ""
    deadline: str = ""
    submissions: int = 0
    cap: int = 0
    url: str = ""
    alliance: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "title": self.title, "reward_usd": self.reward_usd,
            "deadline": self.deadline, "submissions": self.submissions, "cap": self.cap,
            "url": self.url, "alliance": self.alliance,
        }


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _first(mapping: Dict[str, Any], *names: str) -> Any:
    for name in names:
        if mapping.get(name) not in (None, ""):
            return mapping[name]
    return None


def normalize_quest(raw: Dict[str, Any]) -> Quest:
    """Привести квест площадки к одному виду.

    Имена полей на площадке менялись между версиями (``reward`` и
    ``reward_amount``, ``cap`` и ``submission_cap``), поэтому берём первое
    подходящее, а не жёстко одно: иначе квест с наградой $200 выглядел бы как
    бесплатный.
    """
    reward = _number(_first(raw, "reward_amount", "reward_usd", "reward", "pay_usd", "budget"))
    return Quest(
        id=str(_first(raw, "id", "quest_id", "uuid") or ""),
        title=str(_first(raw, "title", "name", "summary") or "Квест без названия"),
        reward_usd=reward,
        description=str(_first(raw, "description", "details", "body") or ""),
        requirements=str(_first(raw, "requirements", "acceptance_criteria", "deliverable") or ""),
        deadline=str(_first(raw, "deadline", "deadline_at", "due_at", "expires_at") or ""),
        submissions=int(_number(_first(raw, "submission_count", "submissions", "entries"))),
        cap=int(_number(_first(raw, "submission_cap", "cap", "slots", "max_submissions"))),
        url=str(_first(raw, "url", "link", "permalink") or ""),
        alliance=str(_first(raw, "alliance", "alliance_name") or ""),
        raw=raw,
    )


class Hansa:
    """Клиент площадки. Все запросы — от имени агента по его ключу."""

    def __init__(self, key: Optional[str] = None, base: Optional[str] = None,
                 session: Any = None) -> None:
        self.base = (base or base_url()).rstrip("/")
        self.key = (key if key is not None else api_key()).strip()
        if session is not None:
            self.session = session
        else:
            self.session = build_session(
                "AGENT-0-agenthansa/1.0",
                {"Accept": "application/json", "Authorization": f"Bearer {self.key}"},
            )

    # ------------------------------------------------------------- транспорт

    def _call(self, method: str, path: str, payload: Any = None,
              params: Optional[Dict[str, Any]] = None) -> Any:
        url = path if path.startswith("http") else self.base + path
        response = self.session.request(method, url, json=payload, params=params, timeout=TIMEOUT)
        status = getattr(response, "status_code", 0)
        if status == 401:
            raise HansaError("ключ площадки не принят (401): проверьте AGENTHANSA_API_KEY")
        if status == 404:
            raise HansaError(f"площадка не знает такой маршрут (404): {path}")
        if status >= 400:
            detail = ""
            try:
                body = response.json()
                if isinstance(body, dict):
                    detail = str(body.get("error") or body.get("detail") or "")
            except Exception:
                detail = ""
            raise HansaError(f"площадка ответила {status}{': ' + detail if detail else ''}")
        try:
            return response.json()
        except Exception:
            return {}

    def get(self, route: str, **fmt: Any) -> Any:
        return self._call("GET", ROUTES[route].format(**fmt))

    def post(self, route: str, payload: Any = None, **fmt: Any) -> Any:
        return self._call("POST", ROUTES[route].format(**fmt), payload)

    def put(self, route: str, payload: Any, **fmt: Any) -> Any:
        return self._call("PUT", ROUTES[route].format(**fmt), payload)

    def patch(self, route: str, payload: Any, **fmt: Any) -> Any:
        return self._call("PATCH", ROUTES[route].format(**fmt), payload)

    # ------------------------------------------------------------- чтение

    def me(self) -> Dict[str, Any]:
        return _as_dict(self._call("GET", ROUTES["me"]))

    def feed(self) -> Dict[str, Any]:
        return _as_dict(self._call("GET", ROUTES["feed"]))

    def inbox(self) -> Dict[str, Any]:
        """Единая точка входа: пять систем заданий в одном ответе."""
        return _as_dict(self._call("GET", ROUTES["inbox"]))

    def work(self, page: int = 1, per_page: int = 20, kind: str = "all") -> Dict[str, Any]:
        return _as_dict(self._call(
            "GET", ROUTES["work"], params={"page": page, "per_page": per_page, "type": kind}
        ))

    def quests(self) -> List[Quest]:
        payload = self._call("GET", ROUTES["quests"])
        rows = payload.get("quests") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            return []
        return [normalize_quest(row) for row in rows if isinstance(row, dict)]

    def quest(self, quest_id: str) -> Quest:
        return normalize_quest(_as_dict(self._call(
            "GET", ROUTES["quest"].format(quest_id=quest_id)
        )))

    def my_submissions(self) -> List[Dict[str, Any]]:
        return _as_list(self._call("GET", ROUTES["my_submissions"]))

    def bounties(self) -> List[Dict[str, Any]]:
        return _as_list(self._call("GET", ROUTES["bounties"]))

    def engagement(self) -> List[Dict[str, Any]]:
        return _as_list(self._call("GET", ROUTES["engagement"]))

    def reputation(self) -> Dict[str, Any]:
        return _as_dict(self._call("GET", ROUTES["reputation"]))

    def earnings(self) -> Dict[str, Any]:
        return _as_dict(self._call("GET", ROUTES["earnings"]))

    def payouts(self) -> List[Dict[str, Any]]:
        return _as_list(self._call("GET", ROUTES["payouts"]))

    def points(self) -> Dict[str, Any]:
        return _as_dict(self._call("GET", ROUTES["points"]))

    # ------------------------------------------------------------- действия

    def checkin(self) -> Dict[str, Any]:
        """Отметиться: 10 XP и небольшая надбавка за серию дней."""
        return _as_dict(self._call("POST", ROUTES["checkin"]))

    def claim_onboarding(self) -> Dict[str, Any]:
        return _as_dict(self._call("POST", ROUTES["claim_onboarding"]))

    def onboarding(self) -> Dict[str, Any]:
        return _as_dict(self._call("GET", ROUTES["onboarding"]))

    def discord_code(self) -> Dict[str, Any]:
        return _as_dict(self._call("POST", ROUTES["discord_code"]))

    def bind_wallet(self, address: str) -> Dict[str, Any]:
        """Привязать адрес: с ним выплата приходит сразу, без него держится 3-7 дней."""
        return _as_dict(self._call("PUT", ROUTES["wallet"], {"wallet_address": address.strip()}))

    def bind_fluxa(self, fluxa_agent_id: str) -> Dict[str, Any]:
        return _as_dict(self._call("PUT", ROUTES["fluxa_wallet"],
                                   {"fluxa_agent_id": fluxa_agent_id.strip()}))

    def choose_alliance(self, alliance: str) -> Dict[str, Any]:
        name = alliance.strip().lower()
        if name not in ("red", "blue", "green"):
            raise HansaError("альянс выбирается из red, blue, green")
        return _as_dict(self._call("PATCH", ROUTES["alliance"], {"alliance": name}))

    def join_bounty(self, bounty_id: str) -> Dict[str, Any]:
        return _as_dict(self._call("POST", ROUTES["bounty_join"].format(bounty_id=bounty_id)))

    # --- отправка работы: только с явным подтверждением человека -----------

    def submit_quest(self, quest_id: str, content: str, proof_url: str = "",
                     confirm: bool = False) -> Dict[str, Any]:
        """Отправить работу по квесту. Без ``confirm=True`` — отказ.

        Политика фермы: публикует, отправляет и подтверждает человек. Робот
        готовит текст и доказательство, человек нажимает «отправить». Поэтому
        метод физически не может выстрелить сам: флаг обязателен и по умолчанию
        выключен, а вызывающая сторона обязана передать его осознанно.
        """
        if not confirm:
            raise ConfirmationRequired(
                "отправка работы требует подтверждения человека: передайте confirm=True"
            )
        if not content.strip():
            raise HansaError("пустой текст работы отправлять нельзя")
        return _as_dict(self._call(
            "POST", ROUTES["quest_submit"].format(quest_id=quest_id),
            {"content": content, "proof_url": proof_url},
        ))

    def submit_bounty(self, bounty_id: str, description: str, url: str = "",
                      confirm: bool = False) -> Dict[str, Any]:
        if not confirm:
            raise ConfirmationRequired(
                "отправка доказательства требует подтверждения человека"
            )
        return _as_dict(self._call(
            "POST", ROUTES["bounty_submit"].format(bounty_id=bounty_id),
            {"description": description, "url": url},
        ))

    def submit_engagement(self, assignment_id: str, comment_url: str = "", notes: str = "",
                          proof_image_urls: Optional[List[str]] = None,
                          confirm: bool = False) -> Dict[str, Any]:
        if not confirm:
            raise ConfirmationRequired("отправка задания требует подтверждения человека")
        body: Dict[str, Any] = {}
        if comment_url:
            body["comment_url"] = comment_url
        if notes:
            body["notes"] = notes
        if proof_image_urls:
            body["proof_image_urls"] = proof_image_urls
        return _as_dict(self._call(
            "POST", ROUTES["engagement_submit"].format(assignment_id=assignment_id), body
        ))


def register(name: str, description: str, base: Optional[str] = None,
             session: Any = None) -> Dict[str, Any]:
    """Создать агента. Ключ возвращается один раз — сохраните его сразу.

    Ключ не печатается: вызывающая сторона кладёт его в ``.env`` и показывает
    только маску. Терять его нельзя, но и восстановить есть чем —
    ``POST /api/agents/regenerate-key``.
    """
    url = (base or base_url()).rstrip("/") + ROUTES["register"]
    client = session or build_session("AGENT-0-agenthansa-register/1.0",
                                     {"Accept": "application/json"})
    response = client.request("POST", url, json={"name": name, "description": description},
                              timeout=TIMEOUT)
    status = getattr(response, "status_code", 0)
    payload = {}
    try:
        payload = response.json()
    except Exception:
        payload = {}
    if status >= 400:
        detail = ""
        if isinstance(payload, dict):
            detail = str(payload.get("error") or payload.get("detail") or "")
        raise HansaError(f"регистрация не прошла: {status}{': ' + detail if detail else ''}")
    if not isinstance(payload, dict) or not payload.get("api_key"):
        raise HansaError("площадка не вернула api_key — регистрация не считается успешной")
    return payload


def masked_key(key: str = "") -> str:
    return _mask(key or api_key())


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> List[Dict[str, Any]]:
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    if isinstance(value, dict):
        for name in ("items", "quests", "bounties", "engagements", "payouts", "results"):
            rows = value.get(name)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
    return []
