"""Readiness check: what exactly is missing before the farm can earn money.

The farm can collect opportunities forever, but money only appears when three
things are true: it can see the market (network + token + country), it has a way
to receive funds (a payout rail that works for that country), and a human does
the parts machines must not do (post the claim, submit the work, confirm the
payment).

`doctor` checks each of those in one pass and returns the blocking items first
with the exact command or link that fixes them. Nothing here is guesswork: every
check either inspects the environment or makes a real request.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import requests

from agent import payouts
from agent import wallet
from agent.config import get_env, project_root
from agent.http import build_session
from agent.ledger import connect

#: Statuses, from "ready" to "cannot start".
OK = "ok"
WARN = "warn"
BLOCK = "block"



@dataclass
class Check:
    key: str
    title: str
    status: str
    detail: str
    impact: str = ""
    fix: str = ""
    required: bool = False

    @property
    def needs_action(self) -> bool:
        return self.status in (WARN, BLOCK)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "impact": self.impact,
            "fix": self.fix,
            "required": self.required,
        }


def _mask(address: str) -> str:
    return f"{address[:6]}…{address[-4:]}" if len(address) > 12 else address


def check_env_file() -> Check:
    env_path = project_root() / ".env"
    if env_path.exists():
        return Check("env_file", "Файл .env", OK, "настройки на месте")
    return Check(
        "env_file",
        "Файл .env",
        WARN,
        "работает на значениях по умолчанию из .env.example",
        impact="без .env нельзя прописать токен GitHub, страну выплаты и ключ площадки",
        fix="cp .env.example .env",
        required=True,
    )


def _import_ok(module: str) -> bool:
    try:
        __import__(module)
        return True
    except Exception:
        return False


def check_python() -> Check:
    version = ".".join(str(part) for part in sys.version_info[:3])
    missing = [name for name in ("requests", "dotenv") if not _import_ok(name)]
    if sys.version_info < (3, 9):
        return Check(
            "python", "Python", BLOCK, f"версия {version} слишком старая",
            impact="код использует синтаксис 3.9+", fix="поставьте Python 3.11+",
            required=True,
        )
    if missing:
        return Check(
            "python", "Python", BLOCK, f"{version}, нет модулей: {', '.join(missing)}",
            impact="ферма не запустится",
            fix="pip install -r requirements.txt   (или .venv/bin/pip install -r requirements.txt)",
            required=True,
        )
    return Check("python", "Python", OK, f"{version}, зависимости на месте")


def check_database() -> Check:
    try:
        conn = connect()
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
    except Exception as exc:
        return Check(
            "database", "База данных", BLOCK, f"не открывается: {exc.__class__.__name__}: {exc}",
            impact="некуда сохранять найденные задачи", fix="проверьте DB_PATH в .env",
            required=True,
        )
    return Check("database", "База данных", OK, "пишется и читается")


def check_github_token() -> Check:
    token = (get_env("GITHUB_TOKEN", "") or "").strip()
    if token:
        return Check("github_token", "Токен GitHub", OK, f"задан ({_mask(token)})")
    return Check(
        "github_token",
        "Токен GitHub",
        WARN,
        "не задан — поиск работает, но лимит 60 запросов/час вместо 5000",
        impact="меньше свежих bounty за один цикл: лучшие задачи забирают в первые часы",
        fix="github.com/settings/tokens → создать токен без прав (public search достаточно) "
            "→ GITHUB_TOKEN=... в .env",
        required=True,
    )


def _default_session() -> Any:
    return build_session("AGENT-0-doctor")


def check_github_api(session_factory: Optional[Callable[[], Any]] = None) -> Check:
    factory = session_factory or _default_session
    session = factory()
    try:
        response = session.get("https://api.github.com/rate_limit", timeout=15)
    except requests.RequestException as exc:
        return Check(
            "github_api", "Связь с GitHub", BLOCK,
            f"нет доступа к api.github.com ({exc.__class__.__name__})",
            impact="первое направление (bounty) не сможет искать задачи",
            fix="проверьте интернет, прокси или TLS: python -m agent.main кто-я",
            required=True,
        )
    if response.status_code == 403:
        return Check(
            "github_api", "Связь с GitHub", BLOCK, "лимит запросов исчерпан (403)",
            impact="поиск остановится до сброса лимита",
            fix="добавьте GITHUB_TOKEN в .env — лимит вырастет с 60 до 5000 запросов/час",
            required=True,
        )
    if response.status_code >= 400:
        return Check(
            "github_api", "Связь с GitHub", BLOCK, f"HTTP {response.status_code}",
            impact="поиск задач недоступен", fix="проверьте доступ к api.github.com",
            required=True,
        )
    remaining = "н/д"
    try:
        data = response.json()
        remaining = str(data["resources"]["search"]["remaining"])
    except Exception:
        pass
    return Check("github_api", "Связь с GitHub", OK, f"API отвечает, запросов поиска осталось: {remaining}")


def check_country() -> Check:
    country = (get_env("ELIGIBILITY_COUNTRY", "") or "").strip().upper()
    if len(country) == 2:
        return Check("country", "Страна выплаты", OK, country)
    return Check(
        "country", "Страна выплаты", BLOCK,
        "ELIGIBILITY_COUNTRY не задана (нужен код из двух букв, напр. RU или DE)",
        impact="нельзя проверить, дойдут ли деньги, и apply отказывается отправлять заявку",
        fix="ELIGIBILITY_COUNTRY=RU в .env, затем python -m agent.main payout-rails --country RU",
        required=True,
    )


def check_payout_rail() -> Check:
    country = (get_env("ELIGIBILITY_COUNTRY", "") or "").strip().upper()
    if len(country) != 2:
        return Check(
            "payout_rail", "Канал получения денег", BLOCK,
            "не проверяется без ELIGIBILITY_COUNTRY",
            fix="задайте ELIGIBILITY_COUNTRY в .env", required=True,
        )
    assessment = payouts.recommend(country)
    if assessment.blocked:
        return Check(
            "payout_rail", "Канал получения денег", WARN,
            f"{country}: карта/Stripe не работают, рабочие каналы — "
            f"{', '.join(assessment.recommended[:3])}",
            impact="нужен кошелёк или сервис-посредник, иначе заработанное не дойдёт",
            fix=f"python -m agent.main каналы-выплат --country {country}",
        )
    return Check(
        "payout_rail", "Канал получения денег", OK,
        f"{country}: доступен {assessment.recommended[0]} и альтернативы",
    )


def check_wallet() -> Check:
    country = (get_env("ELIGIBILITY_COUNTRY", "") or "").strip().upper()
    if len(country) != 2:
        return Check("wallet", "Кошелёк для USDC", WARN, "проверка невозможна без страны",
                     fix="задайте ELIGIBILITY_COUNTRY в .env")
    assessment = payouts.recommend(country)
    card_rail = "stripe_card"
    card_works = card_rail not in assessment.blocked

    # Адрес проверяется всегда, когда он вообще есть: крипто-перевод не отменить,
    # и битый адрес опасен даже тогда, когда карта в стране работает. Раньше при
    # рабочей карте сохранённый адрес не проверялся — ошибка прошла бы молча.
    address = (get_env("PAYOUT_WALLET", "") or "").strip()
    if address:
        info = wallet.classify(address)
        if info.ok:
            note = f"указан ({_mask(address)}) — {info.title.lower()}"
            if card_works:
                note += "; карта/Stripe тоже доступны"
            return Check("wallet", "Кошелёк для USDC", OK, note)
        return Check(
            "wallet", "Кошелёк для USDC", WARN, f"адрес не принят: {info.problem}",
            impact="выплата может уйти в никуда — крипто-перевод не отменить",
            fix="проверьте и пересохраните адрес: python -m agent.main кошелёк 0x… --save",
        )

    if card_works:
        return Check("wallet", "Кошелёк для USDC", OK,
                     "не обязателен: карта/Stripe в вашей стране работают")
    return Check(
        "wallet", "Кошелёк для USDC", WARN, "адрес не указан",
        impact="крипто-выплаты (AgentHansa, часть программ) требуют привязанного кошелька",
        fix="создайте кошелёк (Phantom/MetaMask/Rabby), включите сеть Base, сохраните "
            "seed-фразу офлайн → python -m agent.main кошелёк 0x… --save "
            "(нужен адрес EVM, не Solana) и привяжите его на площадке",
    )


def check_marketplace_key() -> Check:
    key = (get_env("AGENTHANSA_API_KEY", "") or "").strip()
    if key:
        return Check("marketplace_key", "Третье направление (площадки)", OK,
                     f"ключ задан ({_mask(key)})")
    return Check(
        "marketplace_key", "Третье направление (площадки)", WARN,
        "AGENTHANSA_API_KEY пуст — канал простаивает",
        impact="незакрытым остаётся канал выплат без банка: квесты $10–500 в USDC",
        fix="зарегистрировать агента на agenthansa.com (e-mail, без кошелька) → "
            "AGENTHANSA_API_KEY=... в .env → python -m agent.main цикл",
    )


def check_llm() -> Check:
    from agent.llm import client

    try:
        describe = client().describe()
    except Exception as exc:
        return Check("llm", "Языковая модель (планы)", WARN,
                     f"не настроена ({exc.__class__.__name__})",
                     impact="планы и черновики будут шаблонными, но рабочими",
                     fix="LLM_PROVIDER=anthropic + ANTHROPIC_API_KEY в .env или локальная Ollama")
    if "без модели" in describe.lower():
        return Check(
            "llm", "Языковая модель (планы)", WARN, describe,
            impact="подагенты отвечают по шаблонам: планы и черновики рабочие, но грубее",
            fix="LLM_PROVIDER=anthropic + ANTHROPIC_API_KEY в .env, либо локальная Ollama "
                "(бесплатно, без ключа)",
        )
    return Check("llm", "Языковая модель (планы)", OK, describe)


def check_scope() -> Check:
    path = project_root() / "data" / "scope.yaml"
    if path.exists():
        return Check("scope", "Авторизация разведки", OK, "data/scope.yaml заполнен")
    return Check(
        "scope", "Авторизация разведки", WARN, "нет data/scope.yaml",
        impact="канал bug_recon не работает (это осознанное ограничение, не поломка)",
        fix="cp data/scope.example.yaml data/scope.yaml — только для программ, "
            "где у вас есть письменное разрешение",
    )


def check_telegram() -> Check:
    token = (get_env("TELEGRAM_BOT_TOKEN", "") or "").strip()
    chat = (get_env("TELEGRAM_CHAT_ID", "") or "").strip()
    if token and chat:
        return Check("telegram", "Уведомления Telegram", OK, "включены")
    if token or chat:
        return Check("telegram", "Уведомления Telegram", WARN, "заполнена только половина настроек",
                     fix="нужны оба значения: TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID")
    return Check("telegram", "Уведомления Telegram", WARN, "выключены",
                 impact="свежие bounty не разбудят вас ночью — а их забирают в первые часы",
                 fix="создайте бота у @BotFather, узнайте chat_id, добавьте оба значения в .env")


def check_gh_cli() -> Check:
    path = shutil.which("gh")
    if not path:
        return Check(
            "gh_cli", "GitHub CLI", WARN, "gh не установлен (не обязателен)",
            impact="удобнее открывать PR и проверять статус, но ферма работает через API",
            fix="установите gh и выполните gh auth login (по желанию)",
        )
    try:
        result = subprocess.run(["gh", "auth", "status"], capture_output=True,
                                text=True, timeout=10)
    except Exception as exc:
        return Check("gh_cli", "GitHub CLI", WARN, f"не удалось проверить: {exc.__class__.__name__}")
    if result.returncode == 0:
        return Check("gh_cli", "GitHub CLI", OK, "установлен и авторизован")
    return Check("gh_cli", "GitHub CLI", WARN, "установлен, но не авторизован",
                 impact="не критично: заявки и PR можно делать через веб-интерфейс",
                 fix="gh auth login")


CHECKS: List[Callable[..., Check]] = [
    check_env_file,
    check_python,
    check_database,
    check_country,
    check_payout_rail,
    check_wallet,
    check_github_token,
    check_github_api,
    check_marketplace_key,
    check_llm,
    check_telegram,
    check_scope,
    check_gh_cli,
]


def run_checks(session_factory: Optional[Callable[[], Any]] = None) -> List[Check]:
    """Run every check; a broken check becomes a BLOCK, never an exception."""
    results: List[Check] = []
    for checker in CHECKS:
        try:
            if checker is check_github_api:
                results.append(checker(session_factory))  # type: ignore[call-arg]
            else:
                results.append(checker())
        except Exception as exc:  # a diagnostic must never crash the doctor
            results.append(
                Check(
                    getattr(checker, "__name__", "check"),
                    "Проверка не удалась",
                    BLOCK,
                    f"{exc.__class__.__name__}: {exc}",
                    fix="сообщите о проблеме: python -m agent.main кто-я",
                )
            )
    return results


def start_plan() -> List[str]:
    """The ordered path from a fresh clone to the first confirmed payout."""
    country = (get_env("ELIGIBILITY_COUNTRY", "") or "RU").strip().upper()
    return [
        "Шаг 0. Проверить себя: python -m agent.main проверка — "
        "в выводе не должно остаться блокеров.",
        "Шаг 1. Прописать в .env: GITHUB_TOKEN (github.com/settings/tokens) и "
        "ELIGIBILITY_COUNTRY (код страны, куда получаете деньги).",
        f"Шаг 2. Выбрать канал получения денег: "
        f"python -m agent.main каналы-выплат --country {country} "
        "— завести кошелёк или счёт из первой строки. Адрес кошелька проверить и "
        "сохранить: python -m agent.main кошелёк 0x… --save, затем привязать его "
        "в кабинете площадки.",
        "Шаг 3. Собрать рынок: python -m agent.main цикл — затем python -m agent.main дальше.",
        "Шаг 4. Взять самую свежую задачу со статусом «свободна» → "
        "python -m agent.main конкуренция <id> → python -m agent.main план <id> → "
        "python -m agent.main заявка <id> и опубликовать текст руками.",
        "Шаг 5. Сделать работу, записать время (часы) и статус (статус … done).",
        "Шаг 6. После приёмки работы: выплата-запись → выплата-подтвердить — "
        "только это считается доходом.",
        "Параллельно третье направление: зарегистрировать агента на площадке квестов, "
        "положить ключ в .env — python -m agent.main черновик <id> подготовит текст ответа.",
        "Раз в неделю: python -m agent.main цикл --channel audit_contests — аудит-контесты "
        "дают в 5–10 раз больше за час, чем задачи на GitHub.",
    ]


def readiness(session_factory: Optional[Callable[[], Any]] = None) -> Dict[str, Any]:
    checks = run_checks(session_factory)
    blockers = [c for c in checks if c.status == BLOCK]
    warnings = [c for c in checks if c.status == WARN]
    # Blockers first, then the warnings that cost money, then the nice-to-haves.
    action = sorted(
        blockers + warnings,
        key=lambda c: (0 if c.status == BLOCK else 1, 0 if c.required else 1),
    )
    return {
        "checks": [check.as_dict() for check in checks],
        "blockers": [check.as_dict() for check in blockers],
        "warnings": [check.as_dict() for check in warnings],
        "ok": len(checks) - len(action),
        "can_start": not blockers,
        "next_actions": [
            {"key": check.key, "title": check.title, "status": check.status,
             "detail": check.detail, "fix": check.fix, "impact": check.impact,
             "required": check.required}
            for check in action
        ],
        "start_plan": start_plan(),
        "verdict": _verdict(blockers, warnings),
    }


def _verdict(blockers: List[Check], warnings: List[Check]) -> str:
    if blockers:
        return f"Работать ещё нельзя: {len(blockers)} блокирующих пунктов."
    if warnings:
        return (f"Зарабатывать можно, но {len(warnings)} пунктов ослабляют результат "
                f"— закройте их по порядку.")
    return "Всё готово: запускайте cycle и берите лучшую задачу."
