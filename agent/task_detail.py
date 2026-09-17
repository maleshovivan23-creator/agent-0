"""Описание задачи с площадки: критерии приёмки читает робот, а не человек.

Карточка в выдаче говорит только «$199, две работы, 29 дней» — по ней можно
оценить выгоду и нельзя понять работу. Настоящие условия (что считать
результатом, в каком виде сдавать, что именно проверят) лежат на странице задачи.
Из песочницы разработки домен площадки закрыт, поэтому страницу читает раннер
GitHub Actions командой ``снапшот taskmarket --detail N``: описание попадает в
отчёт и доезжает до фермы вместе с наградой.

Разметку площадки никто не обещал хранить неизменной, поэтому разбор идёт по
нескольким стратегиям — от самой надёжной к самой терпимой, — и пустой результат
считается нормальным: без описания черновик работает как раньше, с ним — лучше.
"""

from __future__ import annotations

import json
import re
from html import unescape
from typing import Optional

#: Мета-описания страницы: их отдают сами поисковики, поэтому они переживают
#: любые переделки разметки.
#: Мета-описания страницы: их отдают сами поисковики, поэтому они переживают
#: любые переделки разметки. Кавычку запоминаем backreference-ом: в реальном
#: тексте встречается апостроф («Improve Yukon's …»), и наивный `[^"']+`
#: обрывал описание на первом же апострофе.
_META_HEAD = r'<meta[^>]+(?:name|property)=["\'](?:og:description|description)["\'][^>]*?'
_META_TAIL = r'<meta[^>]+content=(?P<q>["\'])(?P<text>.*?)(?P=q)[^>]*?'
META_PATTERNS = (
    re.compile(_META_HEAD + r'content=(?P<q>["\'])(?P<text>.*?)(?P=q)',
               re.IGNORECASE | re.DOTALL),
    re.compile(_META_TAIL + r'(?:name|property)=["\'](?:og:description|description)["\']',
               re.IGNORECASE | re.DOTALL),
)
JSON_LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(?P<body>.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
#: Данные приложения внутри страницы: и обычный JSON, и экранированный (RSC/Next).
JSON_DESCRIPTION_RE = re.compile(r'\\?"description\\?"\s*:\s*\\?"(?P<text>(?:[^"\\]|\\.)*)\\?"')
TAG_RE = re.compile(r"<[^>]+>")

#: Сколько текста брать: описание задачи бывает длинным, но в черновик человеку
#: нужны условия, а не простыня на десять экранов.
MAX_DESCRIPTION_CHARS = 2000


def _clean(text: str) -> str:
    """Текст без разметки, лишних пробелов и экранирования JSON."""
    if not text:
        return ""
    text = text.replace("\\r", "\n").replace("\\n", "\n").replace("\\t", " ")
    text = text.replace("\\/", "/").replace('\\"', '"').replace("\\u0026", "&")
    text = unescape(text)
    text = TAG_RE.sub(" ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _from_json_ld(html: str) -> str:
    for match in JSON_LD_RE.finditer(html):
        try:
            payload = json.loads(match.group("body").strip())
        except Exception:
            continue
        candidates = payload if isinstance(payload, list) else [payload]
        for item in candidates:
            if isinstance(item, dict) and item.get("description"):
                return _clean(str(item["description"]))
    return ""


def extract_description(html: str) -> str:
    """Описание задачи из страницы: первая сработавшая стратегия побеждает.

    Порядок не случаен. Мета-описание короткое и есть почти всегда. JSON-LD
    даёт текст целиком, если площадка его отдаёт. Данные приложения — последний
    шанс: их формат меняется чаще всего.
    """
    if not html:
        return ""
    for pattern in META_PATTERNS:
        match = pattern.search(html)
        if match:
            text = _clean(match.group("text"))
            if len(text) >= 40:
                return text[:MAX_DESCRIPTION_CHARS]

    text = _from_json_ld(html)
    if text:
        return text[:MAX_DESCRIPTION_CHARS]

    # Из всех совпадений берём самое длинное: короткие — это служебные поля
    # вроде описания самого сайта или метаданных сборки.
    found = [_clean(match.group("text")) for match in JSON_DESCRIPTION_RE.finditer(html)]
    found = [item for item in found if len(item) >= 40]
    if found:
        return max(found, key=len)[:MAX_DESCRIPTION_CHARS]
    return ""


def describe_problem(html: str) -> str:
    """Почему описания нет — для диагностики на раннере, а не для человека.

    ``пусто`` означает «страница пришла, но разметка другая» — это повод
    дополнить стратегии разбора, а не «задача без описания».
    """
    if not html:
        return "страница не пришла"
    if extract_description(html):
        return "описание найдено"
    return f"разметка незнакома: {len(html)} байт"


def summarize_page(html: str) -> str:
    """Короткая подпись страницы для логов раннера: заголовок и размер."""
    if not html:
        return "пусто"
    title = re.search(r"<title[^>]*>(?P<text>.*?)</title>", html, re.DOTALL | re.IGNORECASE)
    name = _clean(title.group("text")) if title else ""
    return f"{len(html)} байт, заголовок: {name[:80]!r}"


def task_url(task_id: str, base: str = "https://taskmarket.dev") -> str:
    return f"{base}/tasks/{task_id}"


def pick_for_detailing(rows: list, limit: int) -> list:
    """Кого стоит детально читать: самые дорогие задачи с наименьшей толпой.

    Описание стоит одного запроса к площадке, поэтому берём верх списка, а не
    все сто задач: у дешёвых и многолюдных работа всё равно не окупается, и
    тратить на них сеть раннера незачем.
    """

    def key(row: dict) -> tuple:
        try:
            reward = float(row.get("reward_usd") or 0.0)
        except (TypeError, ValueError):
            reward = 0.0
        try:
            submissions = int(row.get("submissions") or 0)
        except (TypeError, ValueError):
            submissions = 0
        return (-reward, submissions)

    ordered = sorted(rows, key=key)
    return [row for row in ordered if str(row.get("id") or "")][: max(0, limit)]


def apply_description(rows: list, task_id: str, description: str) -> bool:
    """Записать описание в строку отчёта. False — если строка не найдена."""
    if not description:
        return False
    for row in rows:
        if str(row.get("id")) == str(task_id):
            row["description"] = description
            return True
    return False


def description_from_row(row: dict) -> Optional[str]:
    """Описание из строки отчёта, если оно там есть."""
    text = str(row.get("description") or "").strip()
    return text or None
