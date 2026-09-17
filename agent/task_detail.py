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
from typing import Any, List, Optional

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

#: Публичный API площадки: отдаёт саму задачу, включая полное описание (до 10 000
#: знаков), тогда как в разметке страницы лежит только короткая мета-строка.
#: Адрес вычитан из официального клиента площадки (@lucid-agents/taskmarket).
API_BASE = "https://api.taskmarket.dev"

#: Сколько текста брать, если площадка отдала всё описание целиком.
FULL_DESCRIPTION_CHARS = 4000

#: Сколько текста попадает в черновик: условия бывают на десять экранов, а
#: человеку нужен рабочий текст. Обрезаем умно: начало (что сделать) и конец
#: (сроки, чек-лист доказательств) — середина описания обычно пересказ.
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


def trim_for_human(text: str, limit: int = MAX_DESCRIPTION_CHARS) -> str:
    """Обрезать описание, сохранив начало и конец.

    Сроки и чек-лист доказательств обычно стоят в конце описания — отрезать их
    ради экономии места значило бы выбросить самое нужное.
    """
    text = text.strip()
    if len(text) <= limit:
        return text
    head = int(limit * 0.7)
    tail = max(0, limit - head - 7)
    return f"{text[:head].rstrip()}\n…\n{text[-tail:].lstrip()}" if tail else text[:limit]


def extract_api_description(payload: Any) -> str:
    """Описание из ответа API площадки.

    Ответ бывает и объектом, и обёрткой вида ``{"task": {...}}`` — проверяем оба,
    потому что формат менять может только площадка, а не мы.
    """
    if not isinstance(payload, dict):
        return ""
    candidates: List[Any] = [payload.get("description")]
    for key in ("task", "data", "result"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            candidates.append(nested.get("description"))
    for value in candidates:
        text = _clean(str(value or ""))
        if len(text) >= 40:
            return trim_for_human(text, FULL_DESCRIPTION_CHARS)
    return ""


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


def api_url(task_id: str, base: str = API_BASE) -> str:
    """Адрес задачи в API площадки: сначала он, страница — запасной вариант."""
    return f"{base}/api/tasks/{task_id}"


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
