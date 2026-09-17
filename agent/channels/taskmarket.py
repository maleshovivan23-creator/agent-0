"""Площадка Taskmarket (taskmarket.dev): задачи в USDC, открытые агентам.

Вторая площадка после AgentHansa — и первая, где платят за работу с текстом и
кодом без кабинета и без банка: награду держит контракт на Base, а выплата
уходит в кошелёк. Ключ площадки для чтения задач не нужен вовсе: список открыт
на `taskmarket.dev/tasks`, и карточки содержат ровно то, что нужно для честной
оценки — награду, срок, режим задачи и число уже присланных работ.

Почему число работ здесь важнее всего: витрина полна задач «$2 USDC» с сотней
присланных решений — а это $2, поделённые на сто, при часе работы. Такие задачи в
очередь не попадают, а редкая задача с наградой $100+ и одной-двумя работами —
попадает. Оценка вероятности считается из числа работ, как в GitHub-канале из
числа комментариев.

Чего канал не делает: не регистрируется, не стейкает и не отправляет работу.
Отправка — действие человека с его кошельком (``submit_deliverable_human_approved``),
и площадка требует кошелёк на Base, который у фермы не хранится.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.channels.base import Channel
from agent.config import get_float, operator_value, project_root
from agent.http import build_session
from agent.ledger import Opportunity
from agent.scoring import looks_like_junk, score_opportunity

BASE = "https://taskmarket.dev"
LIST_URL = f"{BASE}/tasks"

#: Отчёт площадки, снятый на раннере GitHub: из песочницы домен закрыт.
SNAPSHOT_REL = "snapshots/taskmarket-report.json"
SNAPSHOT_FRESH_HOURS = 24.0

#: Сколько работ на задачу превращают её в лотерею без приза. При таком
#: соотношении шанс успеха падает почти до нуля, и звать человека туда нельзя.
CROWD_LIMIT = 25

#: Задача считается свежей, если висит не дольше этого срока.
DEFAULT_MAX_AGE_HOURS = 240.0

#: Карточка задачи в выдаче: режим, статус, заголовок, награда, срок, работы.
CARD_RE = re.compile(
    r"<li\b[^>]*>(?P<body>.*?)</li>", re.DOTALL | re.IGNORECASE,
)
LINK_RE = re.compile(r'href="(?P<href>/tasks/0x[0-9a-fA-F]+)"', re.IGNORECASE)
#: Награду берём по метке «Reward»: в заголовке задачи тоже бывает «USDC»
#: («share 199 USDC for verified…»), и поиск первого числа в карточке обрезал
#: заголовок до половины.
REWARD_RE = re.compile(r"Reward\s*(?P<amount>\d[\d,]*(?:\.\d+)?)\s*USDC", re.IGNORECASE)
FALLBACK_REWARD_RE = re.compile(r"(?P<amount>\d[\d,]*(?:\.\d+)?)\s*USDC", re.IGNORECASE)
SUBMISSIONS_RE = re.compile(r"(?P<count>\d+)\s*submissions?", re.IGNORECASE)
DUE_HOURS_RE = re.compile(r"(?P<count>\d+)\s*h(?:ours?)?\s*left", re.IGNORECASE)
DUE_DAYS_RE = re.compile(r"(?P<count>\d+)\s*d(?:ays?)?\s*left", re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")
MODE_RE = re.compile(r"\b(bounty|claim)\b", re.IGNORECASE)
STATUS_RE = re.compile(
    r"\b(Open|Awaiting buyer review|Claimed|Completed|Expired)\b", re.IGNORECASE,
)


def _text(fragment: str) -> str:
    """Текст карточки без тегов и лишних пробелов."""
    return re.sub(r"\s+", " ", unescape(TAG_RE.sub(" ", fragment))).strip()


def _mode_and_status(text: str) -> tuple[str, str]:
    """Режим и статус карточки.

    В разметке они слипаются с заголовком в одно слово («bountyOpen»), поэтому
    обычные границы слов не работают: сначала снимаем режим, потом статус.
    """
    mode = ""
    rest = text.strip()
    if (match := re.match(r"(bounty|claim)", rest, re.IGNORECASE)):
        mode = match.group(1).lower()
        rest = rest[match.end():]
    status = ""
    if (match := re.match(r"\s*(Open|Awaiting buyer review|Claimed|Completed|Expired)",
                          rest, re.IGNORECASE)):
        status = match.group(1)
    return mode, status


def parse_tasks(html: str, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Разобрать выдачу площадки в список задач.

    Разбор отделён от сети, поэтому проверяется на сохранённой странице: у
    площадки нет публичной схемы API, и разметка — это весь контракт, который у
    нас есть.
    """
    moment = now or datetime.now(timezone.utc)
    tasks: List[Dict[str, Any]] = []
    seen: set[str] = set()

    for card in CARD_RE.finditer(html):
        body = card.group("body")
        link = LINK_RE.search(body)
        if not link:
            continue
        href = link.group("href")
        identifier = href.rstrip("/").split("/")[-1]
        if identifier in seen:
            continue

        text = _text(body)
        reward_match = REWARD_RE.search(text) or FALLBACK_REWARD_RE.search(text)
        if not reward_match:
            continue
        reward = float(reward_match.group("amount").replace(",", ""))
        if reward <= 0:
            continue

        title = _title_from(text, reward_match)
        if not title:
            continue

        submissions_match = SUBMISSIONS_RE.search(text)
        submissions = int(submissions_match.group("count")) if submissions_match else 0

        hours_left: Optional[float] = None
        if (hours := DUE_HOURS_RE.search(text)):
            hours_left = float(hours.group("count"))
        elif (days := DUE_DAYS_RE.search(text)):
            hours_left = float(days.group("count")) * 24.0
        elif re.search(r"expired", text, re.IGNORECASE):
            hours_left = -1.0

        mode, status = _mode_and_status(text)
        seen.add(identifier)
        tasks.append({
            "id": identifier,
            "url": f"{BASE}{href}",
            "title": title,
            "reward_usd": reward,
            "submissions": submissions,
            "hours_left": hours_left,
            "mode": mode,
            "status": status,
            "parsed_at": moment.isoformat(timespec="seconds"),
        })
    return tasks


def _title_from(text: str, reward_match: re.Match[str]) -> str:
    """Заголовок карточки: всё до метки награды, без служебных слов режима и статуса."""
    head = text[: reward_match.start()].strip()
    head = re.sub(r"Reward\s*$", "", head, flags=re.IGNORECASE).strip()
    head = re.sub(r"^(bounty|claim)\s*", "", head, flags=re.IGNORECASE)
    head = re.sub(r"^(Open|Awaiting buyer review|Claimed|Completed|Expired)\s*",
                  "", head, flags=re.IGNORECASE)
    return head.strip(" ·-—")


def read_snapshot(root=None) -> Any:
    """Задачи площадки из отчёта GitHub Actions — без сети и без ключа."""
    from agent import snapshots

    return snapshots.read(SNAPSHOT_REL, fresh_hours=SNAPSHOT_FRESH_HOURS,
                          keys=("tasks", "items", "quests"), root=root)


#: Сколько слов считать нижней границей полезного ответа на площадке.
DRAFT_MIN_WORDS = 200


def draft_markdown(opportunity: Dict[str, Any]) -> str:
    """Черновик работы для задачи площадки: что сделать и как это отправить.

    Площадка платит за результат и доказательство, а не за заявку, поэтому
    черновик — это каркас ответа: критерии приёмки читаются на странице задачи,
    робот напоминает, что именно проверить, и не выдумывает то, чего не знает.
    """
    payload = opportunity.get("payload") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload or "{}")
        except Exception:
            payload = {}
    reward = float(opportunity.get("reward_usd") or payload.get("reward_usd") or 0.0)
    submissions = int(payload.get("submissions") or 0)
    hours_left = payload.get("hours_left")
    identifier = str(opportunity.get("id") or "")
    url = str(opportunity.get("url") or payload.get("url") or "")
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    wallet = operator_value("TASKMARKET_WALLET") or operator_value("PAYOUT_WALLET")

    lines: List[str] = [
        f"# Черновик работы: {opportunity.get('title', 'задача')}",
        "",
        f"- ID: `{identifier}`",
        f"- Награда: ${reward:,.2f} USDC на Base",
        f"- Работ уже прислано: {submissions}"
        + (" — конкурентов нет, можно успеть первым" if submissions == 0 else ""),
        "- Срок: " + (f"осталось {float(hours_left):.0f} ч" if hours_left is not None
                       else "смотрите на странице задачи"),
        f"- Страница задачи: {url}",
        f"- Кошелёк для выплаты: {wallet or 'не задан — `python -m agent.main кошелёк <адрес> --save`'}"
        + (" — отправка подписывается им же" if wallet else ""),
        f"- Подготовлено: {generated}",
        "",
    ]
    description = str(payload.get("description") or "").strip()
    if description:
        # Описание снято с самой страницы задачи: это условия, а не пересказ.
        lines += [
            "## Что просит площадка (снято со страницы задачи)",
            "",
            description,
            "",
            "Условия сняты с площадки целиком; если в тексте есть «…», середина "
            "описания скрыта — откройте страницу задачи, чтобы прочитать её.",

            "",
        ]
    lines += [
        "## Прочитать перед работой (этого робот знать не может)",
        "",
        "- Критерии приёмки: что именно считается результатом и в каком виде его ждут",
        "- Формат сдачи: текст, файл, ссылка, код — площадка пишет это сама",
        "- Срок: после него работу не примут, даже если она готова",
        "- Кошелёк на Base привязан — иначе выплата не уйдёт",
        "",
        "## Структура ответа",
        "",
        "1. **Результат** — по делу, без предисловий: что сделано и что получилось.",
        "2. **Как проверял** — конкретные шаги, чтобы проверяющий мог повторить.",
        "3. **Доказательство** — ссылка или файл, которые подтверждают слова.",
        "4. **Ограничения** — где решение не работает и что осталось за рамками.",
        "",
        f"Объём: от {DRAFT_MIN_WORDS} слов, если площадка не сказала иначе;",
        "для кода и данных объём заменяет рабочее доказательство.",
        "",
        "## Отправка (вручную)",
        "",
        "1. Вставить текст в форму отправки на странице задачи.",
        "2. Приложить ссылку-подтверждение, если площадка её ждёт.",
        "3. Отправить от своего имени — робот не отправляет работу сам.",
        "",
        "## После отправки",
        "",
        f"1. Записать время: `python -m agent.main часы --channel taskmarket "
        f"--hours <часы> --id {identifier}`",
        "2. Зафиксировать ожидаемую выплату: `python -m agent.main выплата-запись "
        f"--channel taskmarket --amount {reward:.2f}`",
        "3. После прихода USDC: `python -m agent.main выплата-подтвердить <id>` — "
        "только это считается доходом.",
        "",
    ]
    return "\n".join(lines)


def save_draft(opportunity: Dict[str, Any]) -> Path:
    """Записать черновик в reports/inbox и вернуть путь."""
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(opportunity.get("id") or "task")).strip("-")
    target = project_root() / "reports" / "inbox" / f"taskmarket-{safe}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(draft_markdown(opportunity), encoding="utf-8")
    return target


def fetch_detail(task_id: str, session: Any = None) -> str:
    """Описание задачи со страницы. Пустая строка — сети нет или разметка новая.

    Отдельный запрос на задачу: в выдаче описания нет, а оно и есть условия
    работы. Ошибку не поднимаем наверх — без описания черновик работает как
    раньше, а падать из-за одной страницы отчёту незачем.
    """
    from agent import task_detail

    if not task_id:
        return ""
    own = session is None
    session = session or build_session("AGENT-0-taskmarket/1.0 (open tasks, read only)")
    try:
        # Сначала API: страница рисуется на клиенте, и в её разметке лежит только
        # короткая мета-строка, а сам текст задачи отдаёт /api/tasks/<id>.
        try:
            response = session.get(task_detail.api_url(task_id),
                                   headers={"Accept": "application/json"}, timeout=25)
            if getattr(response, "status_code", 200) == 200:
                try:
                    payload = response.json()
                except Exception:
                    payload = None
                text = task_detail.extract_api_description(payload) if payload else ""
                if text:
                    return text
        except Exception:
            pass

        response = session.get(task_detail.task_url(task_id), timeout=25)
        if getattr(response, "status_code", 200) != 200:
            return ""
        return task_detail.extract_description(response.text)
    except Exception:
        return ""
    finally:
        if own:
            try:
                session.close()
            except Exception:
                pass


class TaskMarketChannel(Channel):
    name = "taskmarket"
    title = "Taskmarket: задачи в USDC на Base"
    capability = "read_public_data"
    description = (
        "Читает открытый список задач площадки taskmarket.dev: награда в USDC, "
        "срок, режим и число присланных работ. Задачи с толпой участников "
        "отсеиваются, редкие и дорогие — попадают в очередь с оценкой EV/час. "
        "Отправка работы и кошелёк остаются за человеком."
    )

    def __init__(self, max_age_hours: Optional[float] = None,
                 crowd_limit: Optional[int] = None) -> None:
        super().__init__()
        self.max_age_hours = (max_age_hours if max_age_hours is not None
                              else get_float("TASKMARKET_MAX_AGE_HOURS", DEFAULT_MAX_AGE_HOURS))
        self.crowd_limit = (crowd_limit if crowd_limit is not None
                            else int(get_float("TASKMARKET_CROWD_LIMIT", CROWD_LIMIT)))
        self.session = build_session(
            "AGENT-0-taskmarket/1.0",
            {"Accept": "text/html,application/xhtml+xml"},
        )
        self.last_error = ""

    # ------------------------------------------------------------------ сеть

    def _fetch(self) -> str:
        try:
            response = self.session.get(LIST_URL, timeout=25)
        except Exception as exc:
            self.last_error = f"площадка недоступна: {exc.__class__.__name__}"
            return ""
        if response.status_code in (403, 429):
            self.last_error = "площадка ограничила запросы"
            return ""
        if response.status_code != 200:
            self.last_error = f"ответ {response.status_code}"
            return ""
        return response.text

    # --------------------------------------------------------------- оценка

    @staticmethod
    def _probability(submissions: int, hours_left: Optional[float]) -> float:
        """Шанс, что работу возьмут: он падает с каждой присланной работой.

        Пять работ на задачу — ещё работа; сто — лотерея, где приз делят на всех.
        """
        if submissions <= 0:
            base = 0.35
        elif submissions <= 2:
            base = 0.25
        elif submissions <= 5:
            base = 0.15
        elif submissions <= 10:
            base = 0.07
        elif submissions <= 25:
            base = 0.03
        else:
            base = 0.01
        if hours_left is not None and hours_left < 0:
            return 0.0
        if hours_left is not None and hours_left < 8:
            base *= 0.5  # времени на работу почти не осталось
        return base

    def _to_opportunity(self, task: Dict[str, Any], hourly_rate: float) -> Optional[Opportunity]:
        # Отчёт мог быть снят прошлой версией канала или собран руками: неполная
        # строка не должна ронять весь проход.
        try:
            reward = float(task.get("reward_usd") or 0.0)
        except (TypeError, ValueError):
            return None
        if reward <= 0:
            return None
        try:
            submissions = max(0, int(task.get("submissions") or 0))
        except (TypeError, ValueError):
            submissions = 0
        hours_left = task.get("hours_left")
        try:
            hours_left = None if hours_left is None else float(hours_left)
        except (TypeError, ValueError):
            hours_left = None
        task = {**task, "reward_usd": reward, "submissions": submissions,
                "hours_left": hours_left,
                "title": str(task.get("title") or ""),
                "url": str(task.get("url") or "")}
        probability = self._probability(submissions, task["hours_left"])
        if probability <= 0:
            return None
        if submissions > self.crowd_limit:
            # Толпа уже победила: задача остаётся в базе как история, но не как работа.
            return Opportunity(
                id=f"taskmarket:{task['id']}",
                channel=self.name,
                title=str(task["title"])[:180],
                url=task["url"],
                reward_usd=float(task["reward_usd"]),
                reward_source="награда в USDC (страница задачи)",
                score=0.0,
                rationale=(
                    f"Taskmarket: ${task['reward_usd']:.2f} USDC, работ уже {submissions} — "
                    f"награда делится между всеми, в работу не берём"
                ),
                status="done",
                payload={**task, "expired": False, "crowded": True,
                         "probability": probability},
            )

        score = score_opportunity(
            labels=["bounty", "usdc"],
            title=str(task["title"]),
            body=str(task.get("mode") or ""),
            comments=submissions,
            repo_stars=0,
            hourly_rate=hourly_rate,
            triage_probability=probability,
            verified_amount=float(task["reward_usd"]),
        )
        if looks_like_junk(str(task["title"]), score.reward_usd, 0):
            return None

        rationale = (
            f"Taskmarket: ${task['reward_usd']:.2f} USDC; работ прислано {submissions}; "
            f"шанс ~{probability:.0%}; оценка {score.effort_hours:.1f}ч; "
            f"EV/час ~${score.ev_per_hour:.1f}"
        )
        if task["hours_left"] is not None and task["hours_left"] >= 0:
            rationale += f"; осталось {task['hours_left']:.0f}ч"
        if submissions == 0:
            rationale += "; конкурентов пока нет — это шанс быть первым"

        return Opportunity(
            id=f"taskmarket:{task['id']}",
            channel=self.name,
            title=str(task["title"])[:180],
            url=task["url"],
            reward_usd=float(task["reward_usd"]),
            reward_source="награда в USDC (страница задачи)",
            score=float(score.ev_per_hour),
            rationale=rationale,
            payload={
                **task,
                "payout_rail": "usdc_wallet",
                "payout_note": "USDC на Base — кошелёк оператора",
                "playbook": score.playbook,
                "effort_hours": score.effort_hours,
                "probability": probability,
                "ev_per_hour": score.ev_per_hour,
                "expected_value_usd": score.expected_value_usd,
                "verify_before_work": [
                    "Открыть задачу и прочитать критерии приёмки: они на странице площадки",
                    "Проверить срок: после него работу уже не примут",
                    "Проверить, что кошелёк на Base привязан — иначе выплата не уйдёт",
                ],
            },
        )

    # ------------------------------------------------------------- интерфейс

    def harvest(self, limit: int = 25) -> List[Opportunity]:
        if not self.allowed:
            return []
        self.last_error = ""
        hourly_rate = get_float("OPERATOR_HOURLY_RATE", 15.0)

        source = "live"
        note = ""
        tasks: List[Dict[str, Any]] = []

        html = self._fetch()
        if html:
            tasks = parse_tasks(html)
            if not tasks:
                self.last_error = "разметка площадки изменилась — задачи не разобрались"
        live_problem = self.last_error

        if not tasks:
            # Живой страницы нет (домен закрыт из песочницы или разметку сменили) —
            # берём отчёт, снятый на раннере GitHub. Источник и возраст честно
            # попадают в карточку задачи: человек должен знать, чему верит.
            snapshot = read_snapshot()
            if snapshot.usable:
                tasks = list(snapshot.rows)
                source = "snapshot"
                note = snapshot.note("задачи Taskmarket")
            else:
                # Человеку нужны обе причины: и почему не вышло вживую, и почему
                # не помог отчёт. Иначе он будет искать сбой канала на пустом месте.
                reasons = [reason for reason in (live_problem, snapshot.problem) if reason]
                self.last_error = "; ".join(reasons) or "задач нет"
                return []

        fresh = [task for task in tasks
                 if (task.get("hours_left") is None or float(task.get("hours_left")) >= -0.5)
                 and float(task.get("reward_usd") or 0) > 0]
        if not fresh and source == "live":
            self.last_error = "на площадке нет открытых задач"

        opportunities: List[Opportunity] = []
        for task in fresh:
            opportunity = self._to_opportunity(task, hourly_rate)
            if opportunity is not None:
                opportunity.payload["source"] = source
                if note:
                    opportunity.payload["snapshot_note"] = note
                    opportunity.rationale += f"; источник: {note}"
                opportunities.append(opportunity)

        if source == "snapshot" and live_problem:
            self.last_error = ""  # отчёт заменяет живую страницу, это не сбой канала
        opportunities.sort(key=lambda item: item.score, reverse=True)
        return opportunities[:limit]
