"""Sub-agents: a small pipeline that turns an opportunity into a work brief.

The farm is not "one agent that does everything". It is a pipeline of narrow
roles, which is how the work actually gets done well:

* **Scout**   — should we spend time on this at all? (fit, risk, honest hours)
* **Analyst** — what facts matter: repo conventions, files to read first
* **Planner** — numbered execution steps for this exact task type
* **Reviewer**— definition of done, and what maintainers reject PRs for
* **Writer**  — the PR description / report text

Each role is a separate prompt with a narrow job, and every role has a
deterministic fallback built from ``playbooks``. If there is no LLM available,
the pipeline still produces a usable brief — it just loses the improvisation.

Prices on the operator's side do not change: this raises the chance that a
submitted PR is merged (the "probability of success" term in the EV formula),
which is where most of the money is actually lost.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from agent import playbooks
from agent.llm import LLMClient, client as default_client

#: Cap on sub-agent calls per brief, so an autonomous loop cannot run away.
MAX_LLM_CALLS = 4


@dataclass
class Brief:
    opportunity_id: str
    title: str
    playbook: str
    provider: str
    sections: Dict[str, str] = field(default_factory=dict)
    llm_used: int = 0
    warnings: List[str] = field(default_factory=list)

    def to_markdown(self) -> str:
        parts = [f"# План работ: {self.title}", ""]
        parts.append(f"- Плейбук: **{playbooks.get(self.playbook).title}**")
        parts.append(f"- Модель: {self.provider}")
        parts.append(f"- ID: `{self.opportunity_id}`")
        parts.append("")
        for name, body in self.sections.items():
            parts.append(f"## {name}")
            parts.append("")
            parts.append(body.strip())
            parts.append("")
        if self.warnings:
            parts.append("## Предупреждения")
            parts.append("")
            for warning in self.warnings:
                parts.append(f"- {warning}")
        return "\n".join(parts)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "opportunity_id": self.opportunity_id,
            "title": self.title,
            "playbook": self.playbook,
            "provider": self.provider,
            "sections": self.sections,
            "llm_used": self.llm_used,
            "warnings": self.warnings,
        }


def _context(opportunity: Dict[str, Any]) -> str:
    payload = opportunity.get("payload") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = {}
    lines = [
        f"Название: {opportunity.get('title')}",
        f"Репозиторий: {opportunity.get('repo')}",
        f"Ссылка: {opportunity.get('url')}",
        f"Награда: {opportunity.get('reward_usd')} "
        f"({opportunity.get('reward_source')})",
        f"Оценка фермы: {opportunity.get('rationale')}",
    ]
    if payload.get("labels"):
        lines.append(f"Метки: {', '.join(payload['labels'])}")
    if payload.get("comments") is not None:
        lines.append(f"Комментариев: {payload.get('comments')}")
    if payload.get("attempts") is not None:
        lines.append(f"Заявок /attempt: {payload.get('attempts')}")
    if payload.get("open_prs"):
        lines.append(f"Открытых PR по задаче: {payload.get('open_prs')}")
    if payload.get("triage_verdict"):
        lines.append(f"Триаж: {payload.get('triage_verdict')} — {'; '.join(payload.get('triage_notes', []))}")
    if payload.get("scope_sample"):
        lines.append(f"Файлы в scope: {', '.join(payload['scope_sample'])}")
    if payload.get("verify_before_work"):
        lines.append("Проверить до работы: " + "; ".join(payload["verify_before_work"]))
    return "\n".join(lines)


def _numbered(items: List[str]) -> str:
    return "\n".join(f"{index}. {item}" for index, item in enumerate(items, 1))


def _bullets(items: List[str]) -> str:
    return "\n".join(f"- {item}" for item in items)


class SubAgentPipeline:
    def __init__(self, llm: Optional[LLMClient] = None, max_calls: int = MAX_LLM_CALLS) -> None:
        self.llm = llm or default_client()
        self.max_calls = max_calls
        self.calls = 0          # calls attempted
        self.successes = 0      # calls that produced usable text

    # --------------------------------------------------------------- helpers

    def _ask(self, prompt: str, fallback: str, max_tokens: int = 700) -> str:
        if self.calls >= self.max_calls:
            return fallback
        self.calls += 1
        completion = self.llm.generate(prompt, max_tokens=max_tokens)
        if not completion.ok or not completion.text.strip():
            return fallback
        # Guard against the model echoing the prompt or writing a refusal.
        text = completion.text.strip()
        if len(text) < 40 or text.lower().startswith(("i cannot", "i'm sorry", "не могу")):
            return fallback
        self.successes += 1
        return text

    # ---------------------------------------------------------------- pipeline

    def build_brief(self, opportunity: Dict[str, Any]) -> Brief:
        payload = opportunity.get("payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {}

        labels = list(payload.get("labels") or [])
        # Contest opportunities carry no labels, but they are audit work: without
        # this hint the pipeline would hand the operator a generic checklist.
        if payload.get("platform") or payload.get("scope_solidity") or payload.get("scope_rust"):
            labels.append("audit")
        body = str(payload.get("body") or "")
        playbook = playbooks.classify(labels, str(opportunity.get("title", "")), body)
        context = _context(opportunity)

        brief = Brief(
            opportunity_id=str(opportunity.get("id")),
            title=str(opportunity.get("title", "")),
            playbook=playbook.key,
            provider=self.llm.describe(),
        )

        # Role: Scout — honest go / no-go.
        scout_fallback = (
            f"Оценка фермы: {opportunity.get('rationale')}\n\n"
            + (
                "Триаж показал, что задача уже занята или оплачена — начинать не стоит "
                "без подтверждения её статуса вручную."
                if payload.get("triage_verdict") in ("paid", "assigned", "contested")
                else "Триаж не нашёл блокирующих признаков: задача выглядит доступной."
            )
            + "\n\nПеред стартом убедитесь, что выплата придёт именно вам "
            "(проверка страны и платёжного канала: python -m agent.main eligibility --country XX)."
        )
        brief.sections["1. Стоит ли браться"] = self._ask(
            "Ты — Scout в команде охотников за bounty. Оцени, стоит ли брать задачу. "
            "Отвечай кратко: 3-6 строк, без воды, честно про риски.\n\n" + context,
            scout_fallback,
            max_tokens=450,
        )

        # Role: Analyst — facts and conventions.
        analyst_fallback = (
            f"Тип задачи: {playbook.title}\n\n"
            "Что посмотреть в первую очередь:\n"
            + _bullets(
                [
                    "README и CONTRIBUTING репозитория — правила оформления PR",
                    "соседние файлы рядом с изменяемым кодом: они задают стиль и паттерны",
                    "историю похожих PR, влитых в репозиторий (git log по пути)",
                    "CI-конфиг: какие проверки обязаны пройти до ревью",
                ]
            )
        )
        brief.sections["2. Что изучить в репозитории"] = self._ask(
            "Ты — Analyst. Составь короткий список фактов, которые нужно установить в репозитории "
            "перед правками (5-7 пунктов, конкретно, без общих слов).\n\n" + context,
            analyst_fallback,
            max_tokens=600,
        )

        # Role: Planner — steps from the playbook, adapted to the task.
        planner_fallback = _numbered(playbook.steps)
        brief.sections["3. План работ"] = self._ask(
            "Ты — Planner. Составь пронумерованный план работ (4-7 шагов) по этой задаче. "
            "Каждый шаг — проверяемое действие. Учитывай типовые ошибки исполнителей.\n\n"
            f"Плейбук (базовые шаги):\n{_numbered(playbook.steps)}\n\n{context}",
            planner_fallback,
            max_tokens=700,
        )

        # Role: Reviewer — definition of done + rejection reasons.
        reviewer_fallback = (
            "Критерии готовности:\n"
            + _bullets(playbook.review_checks)
            + "\n\nЧастые причины отказа:\n"
            + _bullets(playbook.traps)
            + f"\n\nЗаметка для PR: {playbook.submission_notes}"
        )
        brief.sections["4. Критерии готовности и причины отказа"] = self._ask(
            "Ты — Reviewer, который отклоняет слабые PR. Перечисли, что должно быть сделано, "
            "и 3-5 конкретных причин, по которым такую работу отклоняют. Коротко.\n\n" + context,
            reviewer_fallback,
            max_tokens=500,
        )

        brief.sections["5. Текст для PR"] = _writer_text(opportunity, playbook)
        brief.warnings = _warnings(opportunity, payload)
        brief.llm_used = self.successes
        return brief


def _writer_text(opportunity: Dict[str, Any], playbook: playbooks.Playbook) -> str:
    title = opportunity.get("title", "")
    return (
        f"**Что сделано:** {title}\n\n"
        "**Как проверено:**\n"
        "- [команда запуска и вывод]\n\n"
        "**Почему это исправляет проблему:**\n"
        "[корневая причина в двух строках]\n\n"
        "**Риски и обратная совместимость:**\n"
        "[что может затронуть]\n\n"
        f"_Заметка: {playbook.submission_notes}_"
    )


def _warnings(opportunity: Dict[str, Any], payload: Dict[str, Any]) -> List[str]:
    warnings: List[str] = []
    verdict = payload.get("triage_verdict")
    if verdict == "paid":
        warnings.append("Задача, похоже, уже оплачена — не начинайте без проверки.")
    elif verdict == "contested":
        warnings.append(
            f"Конкуренция высокая (заявок: {payload.get('attempts')}, "
            f"открытых PR: {payload.get('open_prs')}) — беритесь только если уверены в скорости."
        )
    if payload.get("funder"):
        warnings.append(
            f"Бюджет подтверждён платформой, заказчик: {payload['funder']} "
            f"(сумма {payload.get('verified_amount') or 'н/д'})."
        )
    if not opportunity.get("reward_usd"):
        warnings.append("Сумма награды не подтверждена: уточните её до начала работы.")
    if (payload.get("comments") or 0) > 100:
        warnings.append("Очень длинная история комментариев: прочитайте последние 20, чтобы не повторять чужую работу.")
    return warnings


def build_brief(opportunity: Dict[str, Any], llm: Optional[LLMClient] = None) -> Brief:
    return SubAgentPipeline(llm=llm).build_brief(opportunity)
