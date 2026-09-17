"""Submission drafts for agent-marketplace quests.

Quests are the one direction whose payout does not depend on a bank or Stripe:
the platform pays in USDC to a wallet. What it does depend on is a written
answer of 300–800 words that a reviewer accepts, so the bottleneck is text, not
access. This module turns a harvested quest into a ready-to-edit submission:
what is asked, what to check before writing, a draft, and the exact submit call.

Submitting stays a human action: the API key belongs to the operator, and the
platforms require content a person stands behind.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.config import project_root
from agent.llm import LLMClient, client as default_client

#: The platforms state this range explicitly; going over it gets a rejection.
WORD_MIN, WORD_MAX = 300, 800

PROMPT = """Ты — автор заявок на площадке задач для ИИ-агентов. Платят за короткий
полезный текст, который проверяющий примет с первого раза.

Задание: {title}
Описание: {description}
Награда: {reward} USD. Требования площадки: {words} слов, можно приложить ссылку-подтверждение.

Напиши готовый текст заявки на русском: по делу, без воды и без выдуманных фактов.
Структура: что именно вы делаете, как проверяли (конкретные шаги), что получилось,
где ограничения. Если данных для ответа не хватает — прямо укажи, какие входные
данные нужны, и предложи способ их получить. Ровно {words} слов."""


def _payload(opportunity: Dict[str, Any]) -> Dict[str, Any]:
    raw = opportunity.get("payload")
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw or "{}")
    except Exception:
        return {}


def word_count(text: str) -> int:
    return len(re.findall(r"\S+", text or ""))


def template_draft(opportunity: Dict[str, Any]) -> str:
    """Deterministic draft used when no model is configured."""
    payload = _payload(opportunity)
    title = str(opportunity.get("title") or "задача").replace("[AgentHansa] ", "")
    description = str(payload.get("description") or "").strip()
    hours = payload.get("effort_hours")
    hours_line = (
        f"- Оценка трудозатрат: {float(hours):.1f} ч."
        if hours
        else "- Оценка трудозатрат уточняется по объёму задания."
    )

    lines = [
        f"**Задача:** {title}",
        "",
        "**Что делаю**",
        "Беру задание и закрываю его по пунктам, которые перечислены на площадке"
        + (f": {description[:400]}" if description else "."),
        "",
        "**Как выполняю**",
        "1. Разбираю формулировку на проверяемые вопросы и фиксирую критерии приёмки.",
        "2. Собираю данные из первоисточников (документация, репозиторий, статистика).",
        "3. Сравниваю варианты по измеримым признакам и отбрасываю то, что не подтверждается.",
        "4. Свожу результат в короткий ответ с выводами и ограничениями.",
        "",
        "**Результат**",
        "Ниже — ответ по заданию. Ключевые пункты выделены, спорные места отмечены как "
        "ограничения, чтобы не выдавать догадку за факт.",
        "",
        "<вставьте готовый текст ответа здесь>",
        "",
        "**Ограничения и как проверял**",
        "- Все утверждения проверяемы по ссылкам на источники.",
        hours_line,
        "",
        "**Подтверждение работы**",
        "Ссылка на исходные данные и расчёты — в поле proof_url при отправке.",
    ]
    return "\n".join(lines)


def draft_text(opportunity: Dict[str, Any], llm: Optional[LLMClient] = None) -> Dict[str, Any]:
    """Return {'text', 'source'} — model output when available, else a template."""
    payload = _payload(opportunity)
    title = str(opportunity.get("title") or "").replace("[AgentHansa] ", "")
    description = str(payload.get("description") or "").strip() or "не указано в выдаче площадки"

    engine = llm if llm is not None else default_client()
    if engine.resolve_provider() != "none":
        prompt = PROMPT.format(
            title=title,
            description=description[:1500],
            reward=f"{float(opportunity.get('reward_usd') or 0):,.0f}",
            words=f"{WORD_MIN}–{WORD_MAX}",
        )
        completion = engine.generate(prompt, max_tokens=1200)
        if completion.text.strip():
            return {"text": completion.text.strip(), "source": f"модель: {completion.provider}"}
    return {"text": template_draft(opportunity), "source": "шаблон (модель не настроена)"}


def checks(opportunity: Dict[str, Any]) -> List[str]:
    """What a human must verify before pressing submit."""
    payload = _payload(opportunity)
    items = list(payload.get("verify_before_work") or [])
    hours_left = payload.get("hours_left")
    if isinstance(hours_left, (int, float)):
        items.append(f"Дедлайн: осталось ~{float(hours_left):.0f} ч — успеваете ли дописать и проверить?")
    submissions = payload.get("submissions")
    cap = payload.get("submission_cap")
    if submissions is not None:
        items.append(
            f"Конкуренция: заявок {submissions}" + (f" из {cap}" if cap else "")
            + " — при полном лимите квест закрывают без рассмотрения."
        )
    items.append(
        f"Объём: {WORD_MIN}–{WORD_MAX} слов — столько проверяющий читает целиком; "
        "сколько слов вышло, показывает `python -m agent.main черновик <id>`."
    )
    items.append("Факты и цифры в тексте должны быть проверяемы — выдуманные данные = отказ.")
    items.append("Отправка и привязка кошелька — только вашими руками, ключ в переписку не попадает.")
    return items


def submission_markdown(opportunity: Dict[str, Any], llm: Optional[LLMClient] = None) -> Dict[str, Any]:
    payload = _payload(opportunity)
    draft = draft_text(opportunity, llm=llm)
    words = word_count(draft["text"])
    volume_note = (
        f"Объём черновика: {words} слов (нужно {WORD_MIN}–{WORD_MAX})"
        + ("" if WORD_MIN <= words <= WORD_MAX else
           " — допишите: короткие заявки площадка не принимает")
    )
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    parts = [
        f"# Черновик заявки: {opportunity.get('title', 'задача')}",
        "",
        f"- ID: `{opportunity.get('id')}`",
        f"- Награда: ${float(opportunity.get('reward_usd') or 0):,.0f}",
        "- Канал выплаты: "
        + (
            f"{payload['payout_rail']} — {payload['payout_note']}"
            if payload.get("payout_rail") and payload.get("payout_note")
            else str(payload.get("payout_rail") or "уточните на площадке")
        ),
        f"- Дедлайн: {payload.get('deadline') or 'не указан'}",
        f"- Источник текста: {draft['source']}",
        f"- {volume_note}",
        f"- Подготовлено: {generated}",
        "",
        "## Проверить перед отправкой",
        "",
    ]
    parts += [f"- [ ] {item}" for item in checks(opportunity)]
    parts += ["", "## Черновик текста", "", draft["text"], ""]

    submit_route = payload.get("submit_route") or ""
    if submit_route:
        parts += [
            "## Отправка (вручную, ключ не публикуйте)",
            "",
            "```bash",
            f'curl -X POST "{submit_route}" \\',
            '  -H "Authorization: Bearer $AGENTHANSA_API_KEY" \\',
            '  -H "Content-Type: application/json" \\',
            "  -d '{\"content\": \"<текст заявки>\", \"proof_url\": \"<ссылка на подтверждение>\"}'",
            "```",
            "",
            f"Правила и формат: {payload.get('docs') or 'документация площадки'}",
            "",
        ]
    parts += [
        "## После отправки",
        "",
        "1. Записать время: `python -m agent.main часы --channel agent_marketplaces "
        "--hours <часы> --id " + str(opportunity.get("id") or "<id>") + "`",
        "2. Зафиксировать ожидаемую выплату: `python -m agent.main выплата-запись "
        "--channel agent_marketplaces --amount <сумма>`",
        "3. После прихода USDC: `python -m agent.main выплата-подтвердить <id>` — "
        "только это считается доходом.",
        "",
    ]

    markdown = "\n".join(parts)
    return {"markdown": markdown, "words": words, "source": draft["source"], "draft": draft["text"]}


def save_submission(opportunity: Dict[str, Any], llm: Optional[LLMClient] = None) -> Path:
    result = submission_markdown(opportunity, llm=llm)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(opportunity.get("id") or "quest")).strip("-")
    reports = project_root() / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / f"quest-{safe}.md"
    path.write_text(result["markdown"], encoding="utf-8")
    return path
