"""Sub-agent pipeline must work even with no LLM available at all."""

from __future__ import annotations

from agent.llm import Completion
from agent.subagents import SubAgentPipeline, build_brief


class DeadLLM:
    """Stands in for a machine with no model: every call fails."""

    def describe(self) -> str:
        return "без модели (тест)"

    def generate(self, prompt: str, max_tokens: int = 0) -> Completion:
        return Completion("", "none", "", False, "нет модели")


def opportunity(**overrides: object) -> dict:
    base = {
        "id": "github:acme/tool#42",
        "title": "Write unit tests for leaderboard updates",
        "repo": "acme/tool",
        "url": "https://github.com/acme/tool/issues/42",
        "reward_usd": 50.0,
        "reward_source": "label:$50",
        "rationale": "награда $50; EV/час ~$2.4",
        "payload": {
            "labels": ["tests", "$50"],
            "comments": 3,
            "attempts": 0,
            "open_prs": 0,
            "body": "Add tests for leaderboard updates",
        },
    }
    base.update(overrides)
    return base


def test_brief_has_all_sections_without_llm() -> None:
    brief = build_brief(opportunity(), llm=DeadLLM())
    assert brief.playbook == "tests"
    assert brief.llm_used == 0
    assert len(brief.sections) == 5
    markdown = brief.to_markdown()
    assert "План работ" in markdown
    assert "Критерии готовности" in markdown
    assert "Текст для PR" in markdown


def test_brief_warns_about_crowded_bounty() -> None:
    item = opportunity()
    item["payload"].update({"attempts": 36, "open_prs": 15, "triage_verdict": "contested"})
    brief = build_brief(item, llm=DeadLLM())
    assert brief.warnings
    assert any("Конкуренция" in warning for warning in brief.warnings)


def test_brief_reports_verified_funder() -> None:
    item = opportunity()
    item["payload"].update({"funder": "Tailcall Inc.", "verified_amount": 100.0})
    brief = build_brief(item, llm=DeadLLM())
    assert any("Tailcall Inc." in warning for warning in brief.warnings)


def test_llm_calls_are_capped() -> None:
    class ChattyLLM:
        def __init__(self) -> None:
            self.calls = 0

        def describe(self) -> str:
            return "chatty"

        def generate(self, prompt: str, max_tokens: int = 0) -> Completion:
            self.calls += 1
            return Completion("Полезный ответ длиной больше сорока символов, чтобы пройти проверку.", "test", "m", True)

    llm = ChattyLLM()
    pipeline = SubAgentPipeline(llm=llm, max_calls=2)  # type: ignore[arg-type]
    brief = pipeline.build_brief(opportunity())
    assert llm.calls == 2
    assert brief.llm_used == 2
    # Remaining roles still produced deterministic content.
    assert len(brief.sections) == 5


def test_missing_reward_is_flagged() -> None:
    item = opportunity(reward_usd=0)
    item["payload"]["labels"] = []
    brief = build_brief(item, llm=DeadLLM())
    assert any("награды" in warning for warning in brief.warnings)
