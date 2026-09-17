"""Quest drafts: text a reviewer can accept, submitted by a human."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

import pytest

from agent import quests


def opportunity(**overrides: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "platform": "agenthansa",
        "quest_id": "q42",
        "description": "Собрать таблицу по пяти площадкам: комиссия, выплата, порог.",
        "effort_hours": 1.5,
        "hours_left": 30,
        "submissions": 6,
        "submission_cap": 50,
        "payout_rail": "crypto_usdc",
        "payout_note": "USDC на Base, минимум 10",
        "deadline": "2026-09-19T12:00:00Z",
        "submit_route": "https://agenthansa.com/api/alliance-war/quests/q42/submit",
        "docs": "https://agenthansa.com/llms.txt",
        "verify_before_work": ["Прочитать правила квеста."],
    }
    payload.update(overrides.pop("payload", {}))
    data = {
        "id": "market:agenthansa:q42",
        "channel": "agent_marketplaces",
        "title": "[AgentHansa] Сравнить 5 платформ",
        "reward_usd": 40,
        "payload": payload,
    }
    data.update(overrides)
    return data


def test_word_count_matches_whitespace_tokens() -> None:
    assert quests.word_count("одно два три") == 3
    assert quests.word_count("") == 0


def test_template_draft_covers_what_a_reviewer_checks() -> None:
    text = quests.template_draft(opportunity())
    for marker in ("Что делаю", "Как выполняю", "Результат", "Ограничения", "Подтверждение"):
        assert marker in text


def test_submission_contains_checklist_route_and_next_steps() -> None:
    result = quests.submission_markdown(opportunity())
    markdown = result["markdown"]
    assert "Проверить перед отправкой" in markdown
    assert "Прочитать правила квеста." in markdown
    assert "quests/q42/submit" in markdown
    assert "payout-verify" in markdown
    assert "300–800" in markdown
    assert result["source"].startswith("шаблон")


def test_short_draft_is_flagged_not_hidden() -> None:
    result = quests.submission_markdown(opportunity())
    assert result["words"] < quests.WORD_MIN
    assert "допишите" in result["markdown"]


def test_model_draft_is_used_when_available() -> None:
    class FakeEngine:
        def resolve_provider(self) -> str:
            return "fake"

        def generate(self, prompt: str, max_tokens: int = 0) -> Any:
            assert "Сравнить 5 платформ" in prompt
            return SimpleNamespace(text="Готовый текст заявки.", provider="fake")

    result = quests.draft_text(opportunity(), llm=FakeEngine())
    assert result["text"] == "Готовый текст заявки."
    assert result["source"] == "модель: fake"


def test_missing_description_still_produces_a_draft() -> None:
    result = quests.draft_text(opportunity(payload={"description": None}))
    assert result["text"].strip()


def test_submission_is_saved_where_the_operator_can_find_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(quests, "project_root", lambda: tmp_path)
    path = quests.save_submission(opportunity())
    assert path.exists()
    assert path.name == "quest-market-agenthansa-q42.md"
    assert "Черновик заявки" in path.read_text(encoding="utf-8")


def test_checks_include_competition_and_deadline() -> None:
    items = " ".join(quests.checks(opportunity()))
    assert "заявок 6 из 50" in items
    assert "осталось ~30 ч" in items
