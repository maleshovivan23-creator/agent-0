"""Payout eligibility: can you actually receive the money?

This module exists because of a hard, practical fact that most "make money
online" guides skip: earning a bounty and *receiving* it are two different
problems. Algora and Opire pay through Stripe Connect, and Stripe does not
serve every country. If you are in a country Stripe cannot pay, a merged pull
request can earn you nothing at all.

The module is deliberately conservative and explicit about uncertainty:

* it flags the rails that are blocked by sanctions/Stripe policy;
* it never claims to know the full, current country list — that changes, so it
  points at the authoritative source and requires the operator to confirm;
* it always offers a crypto-native alternative rail, which many web3
  programs use (Hats Finance, Immunefi programs that pay USDC on-chain).

``python -m agent.main eligibility --country DE`` prints the assessment.
Any claim/apply action requires this check first — see ``preflight()``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List

#: Rails and who uses them. Facts verified against platform documentation.
RAILS: Dict[str, Dict[str, str]] = {
    "stripe_connect": {
        "title": "Stripe Connect (Algora, Opire)",
        "source": "https://algora.io/docs/payments",
        "note": (
            "Выплата приходит после мержа PR, обычно через 1-5 рабочих дней. "
            "Требуется KYC и аккаунт Stripe Express."
        ),
    },
    "crypto_usdc": {
        "title": "USDC на кошелек (AgentHansa, Hats Finance, часть программ Immunefi)",
        "source": "https://agenthansa.com/onboarding",
        "note": (
            "Не зависит от банковских ограничений, но требует кошелёк и "
            "внимательного отношения к налоговой отчётности в вашей стране. "
            "Адрес проверяется командой: python -m agent.main кошелёк 0x… --save"
        ),
    },
    "crypto_onchain": {
        "title": "Ончейн-выплата в токенах проекта (Hats Finance, Immunefi)",
        "source": "https://immunefi.com/",
        "note": "Сумма зависит от ликвидности токена: награда в токенах не равна награде в деньгах.",
    },
}

#: Countries/regions where Stripe-based rails do not operate. Sanctions or
#: platform policy — either way the money cannot arrive through this rail.
STRIPE_BLOCKED = {
    "RU": "Россия",
    "BY": "Беларусь",
    "IR": "Иран",
    "CU": "Куба",
    "KP": "КНДР",
    "SY": "Сирия",
    "MM": "Мьянма",
    "AF": "Афганистан",
    "VE": "Венесуэла",
    "LY": "Ливия",
    "SD": "Судан",
    "SS": "Южный Судан",
    "YE": "Йемен",
    "SO": "Сомали",
    "LB": "Ливан",
    "IQ": "Ирак",
    "ML": "Мали",
    "NE": "Нигер",
    "CF": "ЦАР",
    "CD": "ДР Конго",
    "ER": "Эритрея",
    "GW": "Гвинея-Бисау",
}

#: Regions where Stripe works but international payouts are restricted to
#: registered businesses rather than individuals.
STRIPE_BUSINESS_ONLY = {
    "IN": "Индия",
    "AE": "ОАЭ",
    "TH": "Таиланд",
    "ID": "Индонезия",
}

#: Large, well-covered region — Stripe Connect covers these broadly.
STRIPE_LIKELY_OK = {
    "DE": "Германия", "AT": "Австрия", "CH": "Швейцария", "FR": "Франция",
    "NL": "Нидерланды", "ES": "Испания", "IT": "Италия", "PL": "Польша",
    "PT": "Португалия", "SE": "Швеция", "NO": "Норвегия", "DK": "Дания",
    "FI": "Финляндия", "IE": "Ирландия", "CZ": "Чехия", "RO": "Румыния",
    "UA": "Украина", "GE": "Грузия", "AM": "Армения", "KZ": "Казахстан",
    "RS": "Сербия", "TR": "Турция", "US": "США", "CA": "Канада",
    "GB": "Великобритания", "AU": "Австралия", "NZ": "Новая Зеландия",
    "JP": "Япония", "KR": "Южная Корея", "SG": "Сингапур", "BR": "Бразилия",
    "MX": "Мексика", "ZA": "Южная Африка", "NG": "Нигерия", "KE": "Кения",
}

#: Date the facts above were last checked. The country lists change.
VERIFIED_ON = date(2026, 9, 17).isoformat()


@dataclass
class Verdict:
    country: str
    country_name: str
    rail: str
    status: str  # ok | business_only | blocked | unknown
    reason: str
    alternatives: List[str] = field(default_factory=list)
    source: str = ""

    @property
    def usable(self) -> bool:
        return self.status in ("ok", "business_only")


def assess(country: str, rail: str = "stripe_connect") -> Verdict:
    code = (country or "").strip().upper()
    rail_info = RAILS.get(rail, RAILS["stripe_connect"])
    name = (
        STRIPE_BLOCKED.get(code)
        or STRIPE_BUSINESS_ONLY.get(code)
        or STRIPE_LIKELY_OK.get(code)
        or code
    )

    if rail in ("crypto_usdc", "crypto_onchain"):
        return Verdict(
            country=code,
            country_name=name,
            rail=rail,
            status="ok",
            reason=(
                "Крипто-выплата не зависит от банковских ограничений. "
                "Проверьте правила программы и налоговые обязательства в своей стране."
            ),
            source=rail_info["source"],
        )

    if code in STRIPE_BLOCKED:
        return Verdict(
            country=code,
            country_name=name,
            rail=rail,
            status="blocked",
            reason=(
                f"Stripe не обслуживает {name}: выплаты через Algora и Opire до вас не дойдут, "
                "даже если PR примут. Это ограничение платёжного провайдера, а не вашей работы."
            ),
            alternatives=[
                "Каналы с ончейн-выплатой: Hats Finance, программы Immunefi с оплатой в USDC.",
                "Профильные площадки для агентов с выплатой в USDC (например AgentHansa).",
                "Bug bounty-программы, которые платят в крипте напрямую по правилам программы.",
                "Если у вас есть легальное лицо/счёт в поддерживаемой стране — выплата возможна на него.",
            ],
            source=rail_info["source"],
        )

    if code in STRIPE_BUSINESS_ONLY:
        return Verdict(
            country=code,
            country_name=name,
            rail=rail,
            status="business_only",
            reason=(
                f"В {name} международные выплаты через Stripe обычно доступны только "
                "ИП/юрлицам, не физлицам. Уточните это до начала работы."
            ),
            alternatives=[
                "Проверить требования Stripe для вашей страны перед первым PR.",
                "Как альтернатива — каналы с ончейн-выплатой.",
            ],
            source=rail_info["source"],
        )

    if code in STRIPE_LIKELY_OK:
        return Verdict(
            country=code,
            country_name=name,
            rail=rail,
            status="ok",
            reason=(
                f"{name} входит в список стран, куда Stripe Connect выплачивает средства. "
                "При первой выплате потребуется KYC."
            ),
            source=rail_info["source"],
        )

    return Verdict(
        country=code,
        country_name=name,
        rail=rail,
        status="unknown",
        reason=(
            "Страна не входит в локальный список модуля. Список выплат у платформы "
            "меняется, поэтому проверьте себя по официальной странице ДО начала работы."
        ),
        alternatives=["Проверить официальный список выплат.", "Рассмотреть ончейн-выплату."],
        source=rail_info["source"],
    )


def preflight(country: str, rail: str = "stripe_connect") -> Verdict:
    """Gate used before generating any claim/apply text."""
    return assess(country, rail)


def recommend_rail(country: str) -> str:
    """Which rail to aim for, given the operator's country."""
    code = (country or "").strip().upper()
    return "crypto_usdc" if code in STRIPE_BLOCKED or code in STRIPE_BUSINESS_ONLY else "stripe_connect"


def summary_table() -> List[Dict[str, str]]:
    return [
        {"rail": key, "title": value["title"], "note": value["note"], "source": value["source"]}
        for key, value in RAILS.items()
    ]
