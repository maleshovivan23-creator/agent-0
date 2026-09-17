"""How to actually receive the money.

The question "заработал — а как получить?" is where most honest guides stop and
where most money is lost. Card rails and Stripe do not reach every country, and
in some countries a bank card is simply not a working endpoint for foreign
income. This module catalogues the *receiving* rails, not the earning channels,
and answers one question concretely: what can you do with the balance?

Design rules:

* every rail states what it requires, what it costs, and where it breaks;
* country restrictions come from platform documents and are dated;
* nothing here is legal or tax advice — the module says so and points to what
  a person must verify locally;
* a rail that cannot be verified is still listed with ``status="verify"``
  instead of being silently dropped, because "I don't know" is useful.

Command: ``python -m agent.main payout-rails --country RU``
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional

VERIFIED_ON = date(2026, 9, 17).isoformat()


@dataclass
class Rail:
    key: str
    title: str
    summary: str
    requires: List[str] = field(default_factory=list)
    costs: str = ""
    breaks_when: List[str] = field(default_factory=list)
    steps: List[str] = field(default_factory=list)
    source: str = ""
    sandbox_verified: bool = False

    def as_dict(self) -> Dict[str, object]:
        return {
            "key": self.key,
            "title": self.title,
            "summary": self.summary,
            "requires": self.requires,
            "costs": self.costs,
            "breaks_when": self.breaks_when,
            "steps": self.steps,
            "source": self.source,
            "sandbox_verified": self.sandbox_verified,
        }


RAILS: Dict[str, Rail] = {
    "stripe_card": Rail(
        key="stripe_card",
        title="Stripe Connect → банковская карта/счёт",
        summary=(
            "Так платят Algora и Opire: после мержа PR деньги приходят через "
            "Stripe на карту или счёт. Работает не во всех странах."
        ),
        requires=["Аккаунт Stripe Express", "KYC: паспорт и подтверждение адреса"],
        costs="0% со стороны исполнителя",
        breaks_when=[
            "Страна не обслуживается Stripe (в списке ниже).",
            "В части стран выплаты только ИП/юрлицам, не физлицам.",
            "KYC не пройден — выплата замораживается до верификации.",
        ],
        steps=[
            "Проверить свою страну в официальном списке (ссылка в источнике).",
            "Пройти KYC до начала работы, а не после мержа.",
            "Указать реквизиты карты/счёта в личном кабинете платформы.",
        ],
        source="https://algora.io/docs/payments",
    ),
    "usdc_wallet": Rail(
        key="usdc_wallet",
        title="USDC на кошелёк (Base / Ethereum)",
        summary=(
            "Крипто-выплата: не зависит от банков и Stripe. Так платят AgentHansa "
            "(USDC на Base, минимум 10 USDC), часть программ Immunefi и Hats Finance."
        ),
        requires=[
            "Кошелёк с EVM-адресом на Base (Phantom, MetaMask, Rabby, Trust)",
            "Обычно e-mail и минимальная активность; KYC чаще нет",
        ],
        costs="Сетевой сбор за перевод (центы на Base)",
        breaks_when=[
            "Кошелёк не привязан к аккаунту платформы.",
            "Не достигнут минимальный порог выплаты.",
            "Обмен USDC на местную валюту зависит от доступных бирж/P2P в вашей стране.",
        ],
        steps=[
            "Создать кошелёк, сохранить seed-фразу офлайн (никому не отправлять).",
            "Привязать адрес в кабинете платформы.",
            "Проверить, что принимаете сеть Base (не Ethereum, если платформа платит на Base).",
        ],
        source="https://agenthansa.com/llms.txt",
        sandbox_verified=False,
    ),
    "usdt_trc20": Rail(
        key="usdt_trc20",
        title="USDT в сети TRC-20",
        summary=(
            "Самый дешёвый и распространённый способ получить оплату от частного "
            "заказчика напрямую, без площадки и банка."
        ),
        requires=["Кошелёк TRON", "Договорённость с заказчиком"],
        costs="~$1-3 за перевод, в зависимости от загрузки сети",
        breaks_when=[
            "Заказчик отказывается платить в крипте.",
            "Обмен на местную валюту через P2P: банк может запросить пояснения по операциям.",
            "Учёт дохода: в ряде стран крипто-доход требует отдельной отчётности.",
        ],
        steps=[
            "Создать кошелёк TRON, проверить адрес на тестовом переводе.",
            "Согласовать с заказчиком сумму, сеть и комиссию до работы.",
            "Сохранить переписку и чек — это подтверждение дохода и основание для налогов.",
        ],
        source="https://companies.rbc.ru/news/tcRBd6PjQk/",
    ),
    "intermediary_service": Rail(
        key="intermediary_service",
        title="Сервис-посредник (Mellow, EasyStaff, Kleos, Ruul)",
        summary=(
            "Платформа принимает оплату от зарубежного заказчика и выплачивает вам "
            "на карту, счёт или в крипте. Часто берёт на себя документы и налог."
        ),
        requires=["Регистрация и, как правило, статус самозанятого/ИП"],
        costs="≈5-8% от суммы (у разных сервисов отличается)",
        breaks_when=[
            "Заказчик не готов платить через конкретный сервис.",
            "Для отдельных категорий услуг сервис может отказать (санкционные ограничения).",
        ],
        steps=[
            "Выбрать сервис по способу вывода (карта РФ, счёт, крипта).",
            "Выставить инвойс из сервиса и отправить заказчику.",
            "После оплаты вывести средства удобным способом.",
        ],
        source="https://e-kontur.ru/enquiry/2711/kak-prinimat-oplatu-ot-zarubezhnyh-zakazchikov",
    ),
    "foreign_account": Rail(
        key="foreign_account",
        title="Зарубежный счёт или карта (Армения, Казахстан, Грузия, ОАЭ)",
        summary=(
            "Самый предсказуемый канал для регулярных сумм: обычный SWIFT-перевод "
            "на ваш зарубежный счёт, дальше распоряжаетесь сами."
        ),
        requires=["Оформление счёта/карты в банке другой страны", "Иногда поездка или ВНЖ"],
        costs="Обслуживание счёта + комиссия за перевод",
        breaks_when=[
            "Банк требует личное присутствие.",
            "Не готовы документы о происхождении средств.",
        ],
        steps=[
            "Уточнить требования банка конкретной страны к нерезидентам.",
            "Открыть счёт, получить IBAN/SWIFT-реквизиты.",
            "Отдавать эти реквизиты заказчику или платформе как обычный банковский перевод.",
        ],
        source="https://avo.cards/blog/kak-frilanseru-poluchat-oplatu-iz-za-rubeja-2026/index.php",
    ),
    "legal_entity": Rail(
        key="legal_entity",
        title="Своя компания за рубежом (если суммы крупные)",
        summary=(
            "Компания (например, в США, ОАЭ, Гонконге) даёт доступ к Stripe, Wise и "
            "нормальным корпоративным выплатам. Имеет смысл при устойчивом доходе."
        ),
        requires=["Регистрация компании, бухгалтерия, отчётность"],
        costs="От нескольких сотен долларов в год + бухгалтер",
        breaks_when=[
            "Нет стабильного потока платежей — расходы не окупаются.",
            "Банк отказывает в открытии счёта без реальной связи со страной.",
        ],
        steps=[
            "Оценить годовой оборот: смысл появляется на регулярных суммах.",
            "Выбрать юрисдикцию под вашу ситуацию и налоги, желательно с консультантом.",
            "Открыть корпоративный счёт и настроить выплаты себе.",
        ],
        source="https://companies.rbc.ru/amp/news/b7ee4728-2717-469b-ba05-48772195f5e4/",
    ),
}

#: Countries where Stripe-based rails do not work at all (see eligibility.py for
#: the earning-side implications). Receiving money there needs another rail.
STRIPE_UNAVAILABLE = {
    "RU", "BY", "IR", "CU", "KP", "SY", "MM", "AF", "VE", "LY", "SD", "SS",
    "YE", "SO", "LB", "IQ", "ML", "NE", "CF", "CD", "ER", "GW",
}

#: Country-specific practical notes. Kept short and factual.
COUNTRY_NOTES: Dict[str, List[str]] = {
    "RU": [
        "Карточные выплаты через Stripe (Algora, Opire) недоступны — это ограничение провайдера.",
        "Прямой вывод на карту российского банка не работает: нужен зарубежный счёт/карта "
        "или сервис-посредник.",
        "Крипто-выплата (USDC/USDT) — рабочий канал, но требует отдельного учёта дохода "
        "и осторожности при обмене (риск блокировки карты при P2P).",
        "Направления без банка: площадки для агентов (USDC), Hats Finance, программы "
        "Immunefi с оплатой в USDC.",
    ],
    "BY": [
        "Stripe недоступен. Рабочие варианты: сервисы-посредники, зарубежный счёт, крипто-выплаты.",
    ],
    "DE": [
        "Stripe Connect доступен: Algora и Opire выплачивают на карту/счёт после KYC.",
        "Крипто-выплаты тоже работают; по налогам в Германии доход от крипты учитывается отдельно.",
    ],
}


@dataclass
class Assessment:
    country: str
    recommended: List[str]
    notes: List[str]
    blocked: List[str]

    def as_dict(self) -> Dict[str, object]:
        return {
            "country": self.country,
            "recommended": self.recommended,
            "notes": self.notes,
            "blocked": self.blocked,
            "verified_on": VERIFIED_ON,
        }


def recommend(country: str) -> Assessment:
    """Which receiving rails to use for a given country."""
    code = (country or "").strip().upper()
    blocked: List[str] = []
    recommended: List[str] = []

    if code in STRIPE_UNAVAILABLE:
        blocked.append("stripe_card")
        recommended = ["usdc_wallet", "intermediary_service", "foreign_account", "usdt_trc20"]
    else:
        recommended = ["stripe_card", "usdc_wallet", "intermediary_service"]

    if not code:
        return Assessment(
            country="",
            recommended=["usdc_wallet", "stripe_card"],
            notes=["Укажите страну, чтобы получить точный ответ: --country RU"],
            blocked=[],
        )

    notes = COUNTRY_NOTES.get(code, [])
    if not notes:
        notes = [
            "Проверьте список стран Stripe перед первой выплатой.",
            "Крипто-кошелёк полезно иметь как резервный канал: он не зависит от банка.",
        ]
    return Assessment(country=code, recommended=recommended, notes=notes, blocked=blocked)


def rail(key: str) -> Optional[Rail]:
    return RAILS.get(key)


def table() -> List[Dict[str, object]]:
    return [item.as_dict() for item in RAILS.values()]


def checklist(country: str) -> List[str]:
    """Concrete pre-work checklist so no work is done for unreachable money."""
    code = (country or "").strip().upper()
    items = [
        "Определить платёжный канал ДО начала работы и записать его рядом с задачей.",
        "Проверить минимальную сумму выплаты на платформе (на AgentHansa это 10 USDC).",
        "Убедиться, что KYC пройден, если платформа его требует.",
    ]
    if code in STRIPE_UNAVAILABLE:
        items.append(
            "Stripe-выплаты недоступны: выбрать крипто-канал или сервис-посредник, "
            "иначе работа не будет оплачена."
        )
    items += [
        "Сохранять подтверждения: ссылку на мерж, инвойс, хеш транзакции — это доказательство дохода.",
        "Уточнить у местного консультанта налоги и отчётность по выбранному каналу "
        "(этот проект не даёт юридических консультаций).",
    ]
    return items
