"""Playbooks: how to do a given *type* of bounty task fast.

This module is the main "income multiplier" in the project, and it is
deliberately unglamorous. Bounty earnings obey:

    доход = награда × вероятность_успеха / часы

Most tools only optimise the first factor (find bigger bounties). The cheapest
factor to move is the denominator: doing a documentation fix, a test-writing
task or a small integration 2-3x faster than the first time, because the steps
and the definition of done are already written down.

Each playbook contains: the repeatable steps, common traps, the exact things a
maintainer checks in review (the reason PRs get rejected and earn nothing), and
a time estimate for a prepared operator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class Playbook:
    key: str
    title: str
    match_labels: List[str] = field(default_factory=list)
    match_keywords: List[str] = field(default_factory=list)
    typical_hours: float = 3.0
    steps: List[str] = field(default_factory=list)
    traps: List[str] = field(default_factory=list)
    review_checks: List[str] = field(default_factory=list)
    submission_notes: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "typical_hours": self.typical_hours,
            "steps": self.steps,
            "traps": self.traps,
            "review_checks": self.review_checks,
            "submission_notes": self.submission_notes,
        }


PLAYBOOKS: Dict[str, Playbook] = {
    "documentation": Playbook(
        key="documentation",
        title="Документация / примеры",
        match_labels=["documentation", "docs", "readme"],
        match_keywords=["doc", "readme", "docs", "example", "guide", "typo"],
        typical_hours=1.5,
        steps=[
            "Найти точку входа: README, docs/ или комментарии к модулю, к которому относится задача.",
            "Проверить, что описываемое поведение совпадает с кодом на текущем HEAD, а не с прошлым релизом.",
            "Скопировать существующий пример и прогнать его локально — пример, который не запускается, хуже отсутствующего.",
            "Править минимально: один PR — одна тема. Не переформатировать чужой текст.",
        ],
        traps=[
            "Молчаливая правка чужого стиля — ревьюер просит откатить, время потеряно.",
            "Ссылки на внутренние ресурсы и скриншоты, которые никто не может проверить.",
        ],
        review_checks=[
            "Пример команды действительно выполнен и вывод вставлен настоящий.",
            "Ссылки живые (curl -I по каждой).",
            "Ничего, кроме описания, не изменено.",
        ],
        submission_notes="В PR написать: что было неверно, какая команда это подтверждает, что изменено.",
    ),
    "tests": Playbook(
        key="tests",
        title="Покрытие тестами",
        match_labels=["tests", "testing", "good first issue"],
        match_keywords=["test", "coverage", "unit test", "spec"],
        typical_hours=2.0,
        steps=[
            "Найти существующий тестовый файл рядом с кодом и скопировать его структуру импортов и фикстур.",
            "Определить границы: что тестируем, что явно вне задачи (это пишем в PR).",
            "Написать сначала падающий тест на конкретном кейсе, потом убедиться, что он проходит.",
            "Проверить, что тест падает при откате исправления — иначе он ничего не проверяет.",
        ],
        traps=[
            "Тест, который проходит и без изменений в коде (не проверяет ничего).",
            "Слишком широкий PR: ревьюер просит разделить — это второй круг ревью.",
        ],
        review_checks=[
            "Тест падает без исправления и проходит с ним.",
            "Все существующие тесты остаются зелёными.",
            "Нет закомментированного кода и мёртвых фикстур.",
        ],
        submission_notes="Указать команду запуска тестов и приложить вывод.",
    ),
    "bug_fix": Playbook(
        key="bug_fix",
        title="Исправление бага",
        match_labels=["bug", "bugfix", "defect"],
        match_keywords=["fix", "bug", "broken", "crash", "regression", "error"],
        typical_hours=3.0,
        steps=[
            "Воспроизвести баг минимальным скриптом до правки кода.",
            "Найти корневую причину (не симптом): грепом по функции, а не по строке вывода.",
            "Сделать минимальную правку в одном месте.",
            "Добавить регрессионный тест, который ловит именно этот случай.",
        ],
        traps=[
            "Правка симптома: ревьюер закрывает PR.",
            "Переписывание функции целиком — большой дифф отпугивает и затягивает ревью.",
        ],
        review_checks=[
            "Есть минимальный воспроизводящий скрипт или тест.",
            "Правка локализована, дифф читается за минуту.",
            "В описании PR указано, почему баг возникал.",
        ],
        submission_notes="Структура PR: repro → cause → fix → proof.",
    ),
    "feature": Playbook(
        key="feature",
        title="Небольшая фича / интеграция",
        match_labels=["enhancement", "feature", "good first issue"],
        match_keywords=["add support", "implement", "integrate", "add option", "sdk"],
        typical_hours=5.0,
        steps=[
            "Проверить, нет ли уже открытого PR по этой задаче (иначе работа уйдёт в никуда).",
            "Написать в задаче короткий план (3-5 строк) и получить реакцию мейнтейнера — это резко повышает шанс, что PR примут.",
            "Найти самое близкое существующее решение в репозитории и повторить его паттерн.",
            "Реализовать, добавить тест, обновить документацию в том же PR.",
        ],
        traps=[
            "Писать код до подтверждения дизайна — половина работы уходит в стол.",
            "Игнорировать требования по форматированию и lint: CI красный — ревью не начинается.",
        ],
        review_checks=[
            "Значение по умолчанию сохранено (обратная совместимость).",
            "Есть тест и строчка в документации.",
            "CI зелёный, дифф без случайных файлов.",
        ],
        submission_notes="В PR объяснить выбор подхода и что рассматривалось как альтернатива.",
    ),
    "smart_contract_audit": Playbook(
        key="smart_contract_audit",
        title="Аудит-контест (Solidity)",
        match_labels=["audit", "solidity"],
        match_keywords=["solidity", "vault", "erc20", "oracle", "proxy", "invariant"],
        typical_hours=12.0,
        steps=[
            "Скачать репозиторий контеста и прочитать scope.txt/README: что именно в скоупе.",
            "Посчитать SLOC в скоупе и выбрать 1-2 контракта с наибольшей денежной логикой (учёт балансов, вывод средств).",
            "Пройти чек-лист типовых ошибок: проверка возвращаемых значений, порядок проверок, реentrancy, округление, доступ, оракул, ликвидация.",
            "Проверить инварианты: что должно быть верно всегда (сумма балансов, доли), и где это можно нарушить.",
            "Написать PoC в foundry-тесте. Без PoC отчёт не примут.",
        ],
        traps=[
            "Отчёты про централизацию и админские функции — их обычно считают недействительными.",
            "Дубли: ваш отчёт признают дубликатом и заплатят долю.",
            "Без PoC находка уходит в invalid — это ноль.",
        ],
        review_checks=[
            "PoC воспроизводится командой forge test.",
            "Указан файл и функция в скоупе, точная строка.",
            "Описан ущерб: что именно может потерять протокол или пользователь.",
        ],
        submission_notes="Формат отчёта: severity, impact, PoC, рекомендация. Пишите для судьи, не для себя.",
    ),
    "translation_content": Playbook(
        key="translation_content",
        title="Текст / перевод / контент",
        match_labels=["translation", "content", "writing"],
        match_keywords=["translate", "translation", "article", "blog", "content"],
        typical_hours=2.0,
        steps=[
            "Уточнить требуемый объём и формат (markdown, docs, отдельный файл).",
            "Сохранить терминологию проекта: взять её из существующего переводa или глoссария в репозитории.",
            "Разбить на разделы и сдавать по частям, если объём большой.",
        ],
        traps=[
            "Свободный пересказ вместо перевода — такую работу не принимают.",
            "Единый мега-PR на 5000 строк: ревьюить невозможно.",
        ],
        review_checks=[
            "Терминология совпадает с глоссарием проекта.",
            "Ссылки и код-блоки не переведены, а сохранены как есть.",
        ],
        submission_notes="Указать, по какому глоссарию работали.",
    ),
    "generic": Playbook(
        key="generic",
        title="Универсальный чек-лист",
        match_labels=[],
        match_keywords=[],
        typical_hours=3.5,
        steps=[
            "Проверить, что задача ещё открыта и не занята (python -m agent.main triage <id>).",
            "Прочитать требования дважды и выписать критерии приёмки своими словами.",
            "Сделать минимальный работающий вариант, а не идеальный.",
            "Проверить результат так, как его будет проверять заказчик.",
        ],
        traps=[
            "Начать без подтверждения, что награда ещё активна.",
            "Расширить объём работ за пределы задачи.",
        ],
        review_checks=[
            "Все критерии приёмки из задачи закрыты.",
            "Есть доказательство работы (лог, тест, скриншот, вывод команды).",
        ],
        submission_notes="В описании указать, как проверить результат за 1 минуту.",
    ),
}

#: Order matters: more specific playbooks are matched first.
_MATCH_ORDER = [
    "smart_contract_audit",
    "tests",
    "documentation",
    "translation_content",
    "bug_fix",
    "feature",
]


def classify(labels: List[str], title: str, body: str = "") -> Playbook:
    """Pick the playbook that fits a task."""
    haystack_labels = [str(label).lower() for label in labels]
    haystack_text = f"{title} {body[:400]}".lower()

    for key in _MATCH_ORDER:
        playbook = PLAYBOOKS[key]
        for label in playbook.match_labels:
            if any(label in candidate for candidate in haystack_labels):
                return playbook
        for keyword in playbook.match_keywords:
            if keyword in haystack_text:
                return playbook
    return PLAYBOOKS["generic"]


def get(key: str) -> Playbook:
    return PLAYBOOKS.get(key, PLAYBOOKS["generic"])


def all_playbooks() -> List[Playbook]:
    return list(PLAYBOOKS.values())


def leverage_hours(playbook_key: str, base_hours: float) -> float:
    """Apply the playbook's time advantage to a metadata-based estimate.

    A prepared operator doing a repeatable task beats the generic estimate, but
    the gain is capped (max 45% faster) — pipelines lie when they promise more.
    """
    playbook = get(playbook_key)
    if playbook.key == "generic":
        return base_hours
    factor = 0.75 if playbook.key in ("documentation", "tests", "translation_content") else 0.85
    adjusted = base_hours * factor
    return max(0.5, min(adjusted, base_hours))
