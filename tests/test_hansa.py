"""Площадка AgentHansa: клиент, разбор квестов и защита отправки.

Контракт проверяется по официальному клиенту площадки (agent-hansa-mcp 0.10.0):
маршруты, имена полей и защита от автоматической отправки работы.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from agent import hansa, main
from agent.channels.agent_marketplaces import AgentMarketplacesChannel

QUEST_JSON = {
    "id": "q-77",
    "title": "Написать обзор продукта",
    "reward_amount": "120",
    "description": "Обзор на 800 слов с примерами использования.",
    "submission_count": 8,
    "submission_cap": 50,
    "deadline": "2026-10-01T12:00:00Z",
    "url": "https://www.agenthansa.com/quests/q-77",
}


@pytest.fixture(autouse=True)
def keep_environment() -> Any:
    """Регистрация пишет ключ прямо в os.environ (так и задумано для CLI).

    В тестах это утекало в соседние проверки: следующий тест видел ключ и лез в
    сеть. Поэтому окружение восстанавливаем после каждого теста.
    """
    import os

    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)


class FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def json(self) -> Any:
        return self.payload


class FakeSession:
    """Запоминает запросы и отвечает тем, что положили в очередь."""

    def __init__(self, responses: Optional[List[FakeResponse]] = None,
                 error: Optional[Exception] = None) -> None:
        self.responses = list(responses or [])
        self.error = error
        self.calls: List[Dict[str, Any]] = []
        self.headers: Dict[str, str] = {}

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        if self.error is not None:
            raise self.error
        if not self.responses:
            return FakeResponse({})
        return self.responses.pop(0)

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        return self.request("GET", url, **kwargs)


# --- разбор квестов ----------------------------------------------------------

def test_quest_fields_from_the_live_api_are_understood() -> None:
    quest = hansa.normalize_quest(QUEST_JSON)
    assert quest.id == "q-77"
    assert quest.reward_usd == 120.0
    assert quest.submissions == 8 and quest.cap == 50
    assert quest.deadline.startswith("2026-10-01")


@pytest.mark.parametrize("reward_field", ["reward_amount", "reward", "reward_usd", "pay_usd", "budget"])
def test_reward_is_read_from_any_of_the_names_the_platform_used(reward_field: str) -> None:
    """Имена полей на площадке менялись: квест с наградой не должен стать бесплатным."""
    raw = {"id": "q1", "title": "Задача", reward_field: 250}
    assert hansa.normalize_quest(raw).reward_usd == 250.0


@pytest.mark.parametrize("cap_field", ["submission_cap", "cap", "slots", "max_submissions"])
def test_submission_cap_is_read_from_any_name(cap_field: str) -> None:
    raw = {"id": "q1", "title": "Задача", "reward": 10, cap_field: 12}
    assert hansa.normalize_quest(raw).cap == 12


def test_quest_without_id_or_title_still_parses() -> None:
    quest = hansa.normalize_quest({"reward": 5})
    assert quest.title == "Квест без названия"
    assert quest.id == ""


# --- клиент ------------------------------------------------------------------

def test_client_sends_the_bearer_key() -> None:
    session = FakeSession([FakeResponse({"name": "agent-0", "balance": "0.05"})])
    client = hansa.Hansa(key="tabb_secret", base="https://example.test", session=session)
    assert client.me()["name"] == "agent-0"
    call = session.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == "https://example.test/api/agents/me"


def test_401_explains_the_key_problem() -> None:
    session = FakeSession([FakeResponse({"error": "unauthorized"}, status_code=401)])
    client = hansa.Hansa(key="bad", base="https://example.test", session=session)
    with pytest.raises(hansa.HansaError) as exc:
        client.me()
    assert "401" in str(exc.value) and "AGENTHANSA_API_KEY" in str(exc.value)


def test_unknown_route_is_reported_as_such() -> None:
    session = FakeSession([FakeResponse({}, status_code=404)])
    client = hansa.Hansa(key="k", base="https://example.test", session=session)
    with pytest.raises(hansa.HansaError) as exc:
        client.quests()
    assert "404" in str(exc.value)


def test_quests_parse_both_list_and_wrapped_answers() -> None:
    wrapped = FakeSession([FakeResponse({"quests": [QUEST_JSON]})])
    assert hansa.Hansa(key="k", base="https://x.test", session=wrapped).quests()[0].id == "q-77"

    plain = FakeSession([FakeResponse([QUEST_JSON])])
    assert hansa.Hansa(key="k", base="https://x.test", session=plain).quests()[0].id == "q-77"


def test_garbage_answer_does_not_crash_the_channel() -> None:
    session = FakeSession([FakeResponse({"quests": "не список"})])
    assert hansa.Hansa(key="k", base="https://x.test", session=session).quests() == []


def test_wallet_binding_sends_the_address() -> None:
    session = FakeSession([FakeResponse({"wallet_address": "0x" + "a" * 40})])
    client = hansa.Hansa(key="k", base="https://x.test", session=session)
    client.bind_wallet("0x" + "a" * 40)
    assert session.calls[0]["json"] == {"wallet_address": "0x" + "a" * 40}
    assert session.calls[0]["method"] == "PUT"


def test_alliance_is_validated_before_the_request() -> None:
    session = FakeSession([])
    client = hansa.Hansa(key="k", base="https://x.test", session=session)
    with pytest.raises(hansa.HansaError):
        client.choose_alliance("фиолетовый")
    assert session.calls == []


def test_checkin_uses_post() -> None:
    session = FakeSession([FakeResponse({"xp": 10, "streak": 3})])
    result = hansa.Hansa(key="k", base="https://x.test", session=session).checkin()
    assert session.calls[0]["method"] == "POST"
    assert result["streak"] == 3


# --- отправка работы: только человек -----------------------------------------

def test_submitting_work_without_confirmation_is_refused() -> None:
    """Политика фермы: публикует и отправляет человек, а не робот."""
    session = FakeSession([FakeResponse({})])
    client = hansa.Hansa(key="k", base="https://x.test", session=session)
    with pytest.raises(hansa.ConfirmationRequired):
        client.submit_quest("q-77", "готовый текст")
    assert session.calls == [], "без подтверждения запрос уходить не должен"


def test_submitting_work_with_confirmation_goes_through() -> None:
    session = FakeSession([FakeResponse({"ok": True})])
    client = hansa.Hansa(key="k", base="https://x.test", session=session)
    client.submit_quest("q-77", "готовый текст", "https://proof", confirm=True)
    call = session.calls[0]
    assert call["url"].endswith("/api/alliance-war/quests/q-77/submit")
    assert call["json"] == {"content": "готовый текст", "proof_url": "https://proof"}


def test_empty_work_is_not_submitted_even_with_confirmation() -> None:
    session = FakeSession([FakeResponse({})])
    client = hansa.Hansa(key="k", base="https://x.test", session=session)
    with pytest.raises(hansa.HansaError):
        client.submit_quest("q-77", "   ", confirm=True)
    assert session.calls == []


def test_engagement_and_bounty_submissions_are_guarded_too() -> None:
    session = FakeSession([FakeResponse({})])
    client = hansa.Hansa(key="k", base="https://x.test", session=session)
    with pytest.raises(hansa.ConfirmationRequired):
        client.submit_bounty("b1", "описание", confirm=False)
    with pytest.raises(hansa.ConfirmationRequired):
        client.submit_engagement("a1", notes="заметка", confirm=False)
    assert session.calls == []


# --- регистрация -------------------------------------------------------------

def test_registration_stores_the_key_but_never_prints_it(tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch,
                                                         capsys: pytest.CaptureFixture) -> None:
    from agent import setupenv

    (tmp_path / ".env.example").write_text("AGENTHANSA_API_KEY=\n", encoding="utf-8")
    monkeypatch.setattr(setupenv, "project_root", lambda: tmp_path)
    monkeypatch.setattr(main, "setupenv", setupenv)
    monkeypatch.delenv("AGENTHANSA_API_KEY", raising=False)

    key = "tabb_" + "x" * 30
    monkeypatch.setattr(hansa, "register",
                        lambda name, description, **_: {"id": "uuid-1", "name": name,
                                                        "api_key": key, "balance": "0.05"})
    assert main.main(["площадка", "регистрация", "--имя", "agent-0"]) == 0
    output = capsys.readouterr().out
    assert key not in output, "ключ не должен попадать в вывод"
    assert "tabb_xxx" in output, "нужна маска, чтобы человек узнал ключ"
    assert f"AGENTHANSA_API_KEY={key}" in (tmp_path / ".env").read_text(encoding="utf-8")


def test_registration_failure_is_reported_not_swallowed(monkeypatch: pytest.MonkeyPatch,
                                                        capsys: pytest.CaptureFixture) -> None:
    def boom(name: str, description: str, **_: Any) -> Dict[str, Any]:
        raise hansa.HansaError("регистрация не прошла: 409: name taken")

    monkeypatch.setattr(hansa, "register", boom)
    assert main.main(["площадка", "регистрация"]) == 2
    assert "409" in capsys.readouterr().out


def test_register_requires_a_key_in_the_answer() -> None:
    session = FakeSession([FakeResponse({"id": "uuid"}, status_code=200)])
    with pytest.raises(hansa.HansaError):
        hansa.register("agent-0", "описание", base="https://x.test", session=session)


# --- команда «площадка» ------------------------------------------------------

def test_command_without_key_explains_how_to_register(capsys: pytest.CaptureFixture,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTHANSA_API_KEY", raising=False)
    monkeypatch.delenv("BOUNTY_HUB_API_KEY", raising=False)
    assert main.main(["площадка", "статус"]) == 2
    output = capsys.readouterr().out
    assert "регистрация" in output and "AGENTHANSA_API_KEY" in output


def test_network_check_reports_the_sandbox_limitation(monkeypatch: pytest.MonkeyPatch,
                                                      capsys: pytest.CaptureFixture) -> None:
    class Boom:
        def get(self, *_: Any, **__: Any) -> Any:
            raise OSError("network unreachable")

    monkeypatch.setattr(hansa, "build_session", lambda *a, **k: Boom())
    assert main.main(["площадка", "сети"]) == 2
    output = capsys.readouterr().out
    assert "недоступна" in output and "hansa.yml" in output


def test_status_with_key_prints_agent_summary(monkeypatch: pytest.MonkeyPatch,
                                              capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setenv("AGENTHANSA_API_KEY", "tabb_key")
    answers = {
        hansa.ROUTES["me"]: {"name": "agent-0", "alliance": "red", "balance": "0.05"},
        hansa.ROUTES["reputation"]: {"score": 12, "tier": "Newcomer", "payout_multiplier": "50%"},
        hansa.ROUTES["points"]: {"balance": 10000},
        hansa.ROUTES["earnings"]: {"total_usd": 0},
        hansa.ROUTES["payouts"]: [],
    }

    class Session:
        def request(self, method: str, url: str, **_: Any) -> FakeResponse:
            for route, payload in answers.items():
                if url.endswith(route):
                    return FakeResponse(payload)
            return FakeResponse({})

    monkeypatch.setattr(hansa, "build_session", lambda *a, **k: Session())
    assert main.main(["площадка", "статус"]) == 0
    output = capsys.readouterr().out
    assert "agent-0" in output and "Newcomer" in output and "не привязан" in output


def test_wallet_binding_refuses_a_broken_address(monkeypatch: pytest.MonkeyPatch,
                                                 capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setenv("AGENTHANSA_API_KEY", "tabb_key")
    monkeypatch.setenv("PAYOUT_WALLET", "0x" + "g" * 40)
    assert main.main(["площадка", "кошелёк"]) == 2
    assert "не годен" in capsys.readouterr().out


def test_wallet_binding_uses_the_saved_address(monkeypatch: pytest.MonkeyPatch,
                                               capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setenv("AGENTHANSA_API_KEY", "tabb_key")
    address = "0x42C7377430A1E888de244fe167059dFD44988a3c"
    monkeypatch.setenv("PAYOUT_WALLET", address)
    sent: List[Dict[str, Any]] = []

    class Session:
        def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
            sent.append({"method": method, "url": url, **kwargs})
            return FakeResponse({"wallet_address": address})

    monkeypatch.setattr(hansa, "build_session", lambda *a, **k: Session())
    assert main.main(["площадка", "кошелёк"]) == 0
    assert sent[0]["json"] == {"wallet_address": address}
    assert "привязан" in capsys.readouterr().out


def test_submit_without_confirmation_prints_the_draft_path(tmp_path: Path,
                                                           monkeypatch: pytest.MonkeyPatch,
                                                           capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setenv("AGENTHANSA_API_KEY", "tabb_key")
    monkeypatch.setattr(main, "project_root", lambda: tmp_path)
    draft = tmp_path / "reports" / "draft.md"
    draft.parent.mkdir()
    draft.write_text("готовый текст работы", encoding="utf-8")

    assert main.main(["площадка", "отправить", "q-77", "--файл", "reports/draft.md"]) == 2
    output = capsys.readouterr().out
    assert "действие человека" in output and "--подтверждаю" in output


def test_quest_list_sorts_by_reward(monkeypatch: pytest.MonkeyPatch,
                                    capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setenv("AGENTHANSA_API_KEY", "tabb_key")
    quests = [hansa.normalize_quest({"id": "small", "title": "мелкая", "reward": 10}),
              hansa.normalize_quest({"id": "big", "title": "крупная", "reward": 200})]
    monkeypatch.setattr(hansa.Hansa, "quests", lambda self: quests)
    assert main.main(["площадка", "квесты"]) == 0
    output = capsys.readouterr().out
    assert output.index("крупная") < output.index("мелкая")


# --- канал фермы -------------------------------------------------------------

def test_channel_reports_the_missing_key_honestly() -> None:
    channel = AgentMarketplacesChannel()
    assert channel.harvest() == []
    assert any("нужен ключ" in note for note in channel.notes)


def test_channel_turns_quests_into_ranked_opportunities(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTHANSA_API_KEY", "tabb_key")
    monkeypatch.setattr(hansa, "api_key", lambda: "tabb_key")
    session = FakeSession([FakeResponse({"quests": [QUEST_JSON]})])
    monkeypatch.setattr(hansa, "build_session", lambda *a, **k: session)

    channel = AgentMarketplacesChannel()
    items = channel.harvest(limit=5)
    assert items, channel.last_error
    item = items[0]
    assert item.id == "market:agenthansa:q-77"
    assert item.reward_usd == 120.0
    assert item.payload["quest_id"] == "q-77"
    assert item.payload["ev_per_hour"] > 0
    assert item.payload["payout_rail"] == "crypto_usdc"
    assert "submit" in item.payload["submit_route"]


def test_channel_surfaces_platform_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTHANSA_API_KEY", "tabb_key")
    monkeypatch.setattr(hansa, "api_key", lambda: "tabb_key")
    session = FakeSession([FakeResponse({"error": "bad key"}, status_code=401)])
    monkeypatch.setattr(hansa, "build_session", lambda *a, **k: session)

    channel = AgentMarketplacesChannel()
    assert channel.harvest() == []
    assert "401" in channel.last_error
