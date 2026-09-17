"""Policy gate: the part of AGENT-0 that says "no".

The farm is an autonomous runner, so the only thing standing between it and a
very bad idea is this module. It is deliberately *fail-closed*:

* an action must be mapped to a capability from ``ALLOWED``;
* unknown capabilities are denied, not permitted;
* capabilities that require authorization or human approval carry those
  requirements with them, and the caller must satisfy them.

The DENIED registry is not decoration. Every entry there is a documented
reason why a "make money online" tactic is either economically worthless,
against the rules of the venue, illegal, or all three. Those entries are
rendered in the dashboard so the operator sees why the farm refuses work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


class PolicyViolation(RuntimeError):
    """Raised when a channel tries to do something the farm must not do."""


@dataclass(frozen=True)
class Denied:
    capability: str
    label: str
    why_it_fails: str
    legal_or_tos_risk: str
    do_instead: str


@dataclass(frozen=True)
class Allowed:
    capability: str
    label: str
    requires_human: bool = False
    requires_authorization: bool = False
    note: str = ""


@dataclass
class Decision:
    allowed: bool
    capability: str
    label: str = ""
    reason: str = ""
    requirements: List[str] = field(default_factory=list)
    alternatives: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Denied capabilities. Each one is a tactic the farm will never implement.
# --------------------------------------------------------------------------

DENIED: Dict[str, Denied] = {
    "faucet_claim_automation": Denied(
        capability="faucet_claim_automation",
        label="Автоматический сбор крипто-кранов",
        why_it_fails=(
            "Экономика кранов — $0.001-$0.008 за час (замеры 2026 года). "
            "Чтобы получить $10, нужно ~1250 часов непрерывного клейма. "
            "Это не низкая доходность, это отрицательная: электричество и "
            "комиссии на вывод дороже награды."
        ),
        legal_or_tos_risk=(
            "Автоматизация клейма прямо запрещена правилами кранов; почти все "
            "они защищены капчей, а её обход — это уже не серая зона, а обман "
            "защитной меры."
        ),
        do_instead=(
            "Каналы с реальной оплатой за проверяемую работу: открытые "
            "bounty-задачи на GitHub, призовые соревнования, программа "
            "bug bounty с ручной проверкой."
        ),
    ),
    "captcha_bypass": Denied(
        capability="captcha_bypass",
        label="Обход капчи / анти-бот защит",
        why_it_fails="Награда за обход капчи почти всегда ниже стоимости самого обхода.",
        legal_or_tos_risk=(
            "Обход технической защиты — самостоятельное основание для блокировки "
            "аккаунта и вывода средств, а в ряде юрисдикций ещё и состав нарушения."
        ),
        do_instead="Работать только там, где автоматизация разрешена правилами площадки.",
    ),
    "sybil_multi_wallet": Denied(
        capability="sybil_multi_wallet",
        label="Сибил-ферма кошельков / мультиаккаунты",
        why_it_fails=(
            "Проекты исключают сибил-кластеры до выплаты: Optimism отсеял "
            "~17 000 адресов, сгорело около $18.6 млн наград. Работа делается, "
            "деньги не приходят."
        ),
        legal_or_tos_risk=(
            "Нарушение условий раздачи; адреса попадают в чёрные списки и "
            "исключаются из будущих кампаний."
        ),
        do_instead=(
            "Один честный аккаунт и реальная работа за вознаграждение. "
            "Если интересует крипта без капитала — фокус на грантах и "
            "bounty с публичной приёмкой результата, а не на раздачах."
        ),
    ),
    "automated_airdrop_farming": Denied(
        capability="automated_airdrop_farming",
        label="Автоматическая airdrop-ферма",
        why_it_fails=(
            "Ферма требует начальных вложений (газ, бриджи, комиссии), то есть "
            "нарушает само условие «без вложений», а выплата не гарантирована."
        ),
        legal_or_tos_risk="Правила кампаний запрещают автоматизированный фарм и мультиадресность.",
        do_instead="Публичные bounty-программы и соревнования с гарантированным призом.",
    ),
    "unauthorized_scanning": Denied(
        capability="unauthorized_scanning",
        label="Сканирование чужих систем без разрешения",
        why_it_fails="Найденное «на удачу» нельзя и продать: отчёт вне программы никто не примет.",
        legal_or_tos_risk=(
            "Несанкционированный доступ/сканирование — уголовная и гражданская "
            "ответственность. В РФ — ст. 272 УК; за рубежом — CFAA и аналоги."
        ),
        do_instead=(
            "Тестировать только хосты из явного файла авторизации (data/scope.yaml), "
            "в рамках опубликованных правил программы."
        ),
    ),
    "auto_exploitation": Denied(
        capability="auto_exploitation",
        label="Автоматическая эксплуатация найденных уязвимостей",
        why_it_fails="Нельзя безопасно и осмысленно подтвердить находку без человека в контуре.",
        legal_or_tos_risk=(
            "Автоматическая эксплуатация легко выходит за рамки scope и за "
            "рамки правил программы — это конец аккаунта и начало разбирательства."
        ),
        do_instead=(
            "Автоматизировать пассивную разведку и черновик отчёта; "
            "эксплуатацию и подачу делает человек."
        ),
    ),
    "tos_violating_platform_automation": Denied(
        capability="tos_violating_platform_automation",
        label="Автоматизация площадок, где она запрещена правилами",
        why_it_fails="Выплату не отдадут после первой проверки на бота.",
        legal_or_tos_risk="Блокировка аккаунта и конфискация баланса.",
        do_instead="Подключаться только к площадкам с публичным API и разрешённой автоматизацией.",
    ),
    "fake_engagement": Denied(
        capability="fake_engagement",
        label="Накрутка активности / отзывов / рефералов",
        why_it_fails="Оплата зависит от честности метрик, накрутка выявляется автоматически.",
        legal_or_tos_risk="Обман платформы и заказчика.",
        do_instead="Реальный результат, который можно проверить.",
    ),
    "multi_account_creation": Denied(
        capability="multi_account_creation",
        label="Массовая регистрация аккаунтов",
        why_it_fails="Ни один легальный канал не платит за дубликаты одного и того же человека.",
        legal_or_tos_risk="Нарушение правил площадок, обман при верификации личности.",
        do_instead="Одна подтверждённая личность — один аккаунт.",
    ),
    "credential_stuffing": Denied(
        capability="credential_stuffing",
        label="Подбор/перебор чужих учётных данных",
        why_it_fails="Это не заработок, это преступление с нулевым ожидаемым доходом.",
        legal_or_tos_risk="Уголовная ответственность.",
        do_instead="Никогда. Такой функциональности в ферме нет и не будет.",
    ),
}


# --------------------------------------------------------------------------
# Allowed capabilities.
# --------------------------------------------------------------------------

ALLOWED: Dict[str, Allowed] = {
    "read_public_data": Allowed(
        capability="read_public_data",
        label="Чтение публичных данных через официальные API",
        note="Только публичные эндпоинты и уважение к rate limit сервиса.",
    ),
    "compute_analysis": Allowed(
        capability="compute_analysis",
        label="Анализ и оценка возможностей (скоринг, дедупликация)",
    ),
    "draft_deliverable": Allowed(
        capability="draft_deliverable",
        label="Подготовка черновика результата (код, отчёт, текст)",
    ),
    "submit_deliverable_human_approved": Allowed(
        capability="submit_deliverable_human_approved",
        label="Подача результата на площадку",
        requires_human=True,
        note="Публикация PR/отчёта/заявки всегда требует подтверждения человека.",
    ),
    "passive_recon_authorized_scope": Allowed(
        capability="passive_recon_authorized_scope",
        label="Пассивная разведка в пределах авторизованного scope",
        requires_authorization=True,
        note="DNS/TLS/HTTP-заголовки, без фаззинга и без полезной нагрузки.",
    ),
    "active_test_authorized_scope": Allowed(
        capability="active_test_authorized_scope",
        label="Активная проверка в пределах авторизованного scope",
        requires_human=True,
        requires_authorization=True,
        note="По умолчанию выключено; включается только правилами конкретной программы.",
    ),
    "record_verified_income": Allowed(
        capability="record_verified_income",
        label="Учёт дохода с обязательным подтверждением",
        requires_human=True,
    ),
}


@dataclass(frozen=True)
class Requested:
    """A literal tactic from the operator's wishlist, and where it landed."""

    request: str
    capability: str
    status: str  # "blocked" | "allowed" | "allowed_with_human"
    comment: str


#: What was asked for, and the honest verdict for each item.
REQUESTED_TACTICS: List[Requested] = [
    Requested(
        request="Bug bounty «на полном автомате»",
        capability="auto_exploitation",
        status="blocked",
        comment=(
            "Автоматизируется только разведка и черновик отчёта. Эксплуатация "
            "и подача — человек. Полный автомат в bug bounty = выход за scope."
        ),
    ),
    Requested(
        request="Ферма крипто-кранов",
        capability="faucet_claim_automation",
        status="blocked",
        comment="$0.001-$0.008 в час и запрет автоматизации — отрицательный результат.",
    ),
    Requested(
        request="Автоматическая airdrop-ферма",
        capability="automated_airdrop_farming",
        status="blocked",
        comment="Нужны вложения на газ, а сибил-адреса отсекают до выплаты.",
    ),
    Requested(
        request="Агент берёт заказы на площадках для агентов",
        capability="read_public_data",
        status="allowed_with_human",
        comment=(
            "Разрешено: поиск и скорборд заказов, черновик результата. "
            "Отправка — только после подтверждения человеком и только там, "
            "где автоматизация разрешена правилами."
        ),
    ),
    Requested(
        request="Открытые bounty-задачи (GitHub / Opire / Algora)",
        capability="read_public_data",
        status="allowed",
        comment="Работает без вложений и без кошелька: задача, PR, выплата.",
    ),
]


def evaluate(capability: str) -> Decision:
    """Fail-closed evaluation of a single capability."""
    if capability in DENIED:
        denied = DENIED[capability]
        return Decision(
            allowed=False,
            capability=capability,
            label=denied.label,
            reason=denied.why_it_fails,
            requirements=[denied.legal_or_tos_risk],
            alternatives=[denied.do_instead],
        )
    if capability in ALLOWED:
        allowed = ALLOWED[capability]
        requirements: List[str] = []
        if allowed.requires_human:
            requirements.append("human_approval")
        if allowed.requires_authorization:
            requirements.append("authorized_scope")
        return Decision(
            allowed=True,
            capability=capability,
            label=allowed.label,
            reason=allowed.note or "Разрешено.",
            requirements=requirements,
        )
    return Decision(
        allowed=False,
        capability=capability,
        label="Неизвестная возможность",
        reason="Возможность не описана в реестре. Fail-closed: запрещено.",
        alternatives=["Добавить capability в agent/policy.py с обоснованием."],
    )


def assert_allowed(capability: str, *, satisfied: Optional[List[str]] = None) -> Decision:
    """Raise PolicyViolation unless the capability is allowed *and* satisfied."""
    decision = evaluate(capability)
    if not decision.allowed:
        raise PolicyViolation(
            f"Denied: {decision.label}. Reason: {decision.reason} "
            f"Instead: {'; '.join(decision.alternatives)}"
        )
    satisfied = satisfied or []
    missing = [r for r in decision.requirements if r not in satisfied]
    if missing:
        raise PolicyViolation(
            f"Capability {capability} requires {', '.join(missing)}. "
            f"Provide it explicitly (human approval or authorized scope file)."
        )
    return decision


def report() -> Dict[str, object]:
    """Machine-readable policy report for the dashboard / CLI."""
    return {
        "allowed": [
            {
                "capability": a.capability,
                "label": a.label,
                "requires_human": a.requires_human,
                "requires_authorization": a.requires_authorization,
                "note": a.note,
            }
            for a in ALLOWED.values()
        ],
        "denied": [
            {
                "capability": d.capability,
                "label": d.label,
                "why_it_fails": d.why_it_fails,
                "risk": d.legal_or_tos_risk,
                "instead": d.do_instead,
            }
            for d in DENIED.values()
        ],
        "requested": [
            {
                "request": r.request,
                "capability": r.capability,
                "status": r.status,
                "comment": r.comment,
            }
            for r in REQUESTED_TACTICS
        ],
    }
