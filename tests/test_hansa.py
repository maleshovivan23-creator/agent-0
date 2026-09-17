"""Площадка AgentHansa: клиент, разбор квестов и защита отправки.

Контракт проверяется по официальному клиенту площадки (agent-hansa-mcp 0.10.0):
маршруты, имена полей и защита от автоматической отправки работы.
"""

from __future__ import annotations

import shutil
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


# --- отчёт из GitHub Actions: запасной источник квестов ----------------------

def write_snapshot(tmp_path: Path, generated_at: str, quests: List[Dict[str, Any]]) -> None:
    import json

    (tmp_path / "snapshots").mkdir(exist_ok=True)
    (tmp_path / "snapshots" / "hansa-report.json").write_text(
        json.dumps({"generated_at": generated_at, "status": {}, "quests": quests},
                   ensure_ascii=False),
        encoding="utf-8",
    )


def recent_stamp(hours: float = 0.5) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def test_snapshot_is_read_with_age_and_quests(tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    write_snapshot(tmp_path, recent_stamp(2.0), [QUEST_JSON])
    monkeypatch.setattr(hansa, "project_root", lambda: tmp_path)

    snapshot = hansa.read_snapshot()
    assert snapshot.usable and not snapshot.problem
    assert len(snapshot.quests) == 1
    assert snapshot.quests[0].reward_usd == 120.0
    assert snapshot.age_hours == pytest.approx(2.0, abs=0.2)
    assert not snapshot.stale
    assert "GitHub Actions" in snapshot.note()


def test_snapshot_marks_free_quests_and_broken_files(tmp_path: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hansa, "project_root", lambda: tmp_path)
    assert "включите цикл" in hansa.read_snapshot().problem

    write_snapshot(tmp_path, recent_stamp(), [{"id": "x", "title": "Пусто", "reward": 0}])
    assert not hansa.read_snapshot().usable, "квест без награды не работа"

    (tmp_path / "snapshots" / "hansa-report.json").write_text("{битый json", encoding="utf-8")
    assert "не читается" in hansa.read_snapshot().problem


def test_old_snapshot_is_flagged_as_stale(tmp_path: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    write_snapshot(tmp_path, recent_stamp(40.0), [QUEST_JSON])
    monkeypatch.setattr(hansa, "project_root", lambda: tmp_path)
    snapshot = hansa.read_snapshot()
    assert snapshot.stale
    assert "устарел" in snapshot.note()


def test_channel_uses_snapshot_when_the_network_is_closed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Главный сценарий: домен площадки из песочницы закрыт, отчёт уже есть."""
    write_snapshot(tmp_path, recent_stamp(1.0), [QUEST_JSON])
    monkeypatch.setattr(hansa, "project_root", lambda: tmp_path)
    monkeypatch.delenv("AGENTHANSA_API_KEY", raising=False)

    channel = AgentMarketplacesChannel()
    items = channel.harvest()
    assert items, channel.last_error or channel.notes
    item = items[0]
    assert item.id == "market:agenthansa:q-77"
    assert item.payload["source"] == "snapshot"
    assert any("GitHub Actions" in note for note in channel.notes)
    assert any("откройте квест на площадке" in step for step in item.payload["verify_before_work"])


def test_channel_falls_back_to_snapshot_when_the_api_fails(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_snapshot(tmp_path, recent_stamp(3.0), [QUEST_JSON])
    monkeypatch.setattr(hansa, "project_root", lambda: tmp_path)
    monkeypatch.setenv("AGENTHANSA_API_KEY", "tabb_key")
    monkeypatch.setattr(hansa, "api_key", lambda: "tabb_key")
    monkeypatch.setattr(hansa, "build_session",
                        lambda *a, **k: FakeSession([FakeResponse({"error": "boom"},
                                                                  status_code=500)]))

    channel = AgentMarketplacesChannel()
    items = channel.harvest()
    assert items, "отчёт должен заменять живой ответ"
    assert channel.last_error, "причина сбоя видна в дозоре"
    assert items[0].payload["source"] == "snapshot"


def test_live_api_wins_over_the_snapshot(tmp_path: Path,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    write_snapshot(tmp_path, recent_stamp(1.0), [{"id": "старый", "title": "из отчёта",
                                                  "reward": 5}])
    monkeypatch.setattr(hansa, "project_root", lambda: tmp_path)
    monkeypatch.setattr(hansa, "api_key", lambda: "tabb_key")
    monkeypatch.setattr(hansa, "build_session",
                        lambda *a, **k: FakeSession([FakeResponse({"quests": [QUEST_JSON]})]))

    items = AgentMarketplacesChannel().harvest()
    assert [item.payload["quest_id"] for item in items] == ["q-77"]
    assert items[0].payload["source"] == "live"


# --- шаги для человека: у квестов площадки свой маршрут ----------------------

def test_next_actions_route_quests_to_the_platform_commands(tmp_path: Path,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    """Раньше всем предлагалось «досье → план», для квеста это неверный путь."""
    from agent import farm
    from agent.ledger import Opportunity, set_status, upsert_opportunities

    upsert_opportunities([Opportunity(
        id="market:agenthansa:q77", channel="agent_marketplaces",
        title="[AgentHansa] Обзор", reward_usd=120.0, repo="agenthansa",
        payload={"quest_id": "q77", "ev_per_hour": 18.0, "effort_hours": 4.0,
                 "source": "snapshot", "snapshot_age_hours": 2.0},
    )])
    set_status("market:agenthansa:q77", "queued")

    actions = farm.next_actions(limit=3)
    quest = [item for item in actions if item["id"] == "market:agenthansa:q77"][0]
    steps = " ".join(quest["next"])
    assert "площадка квест q77" in steps
    assert "черновик market:agenthansa:q77" in steps
    assert "отправить q77" in steps and "--подтверждаю" in steps
    assert "досье market:agenthansa:q77" not in steps


def test_next_actions_keep_the_github_route_for_bounty(tmp_path: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    from agent import farm
    from agent.ledger import Opportunity, set_status, upsert_opportunities

    upsert_opportunities([Opportunity(
        id="github:acme/parser#7", channel="github_bounties", title="Fix",
        repo="acme/parser", reward_usd=200.0,
        payload={"query": "label:bounty", "ev_per_hour": 25.0, "effort_hours": 4.0},
    )])
    set_status("github:acme/parser#7", "queued")

    quest = [item for item in farm.next_actions(limit=3)
             if item["id"] == "github:acme/parser#7"][0]
    steps = " ".join(quest["next"])
    assert "досье github:acme/parser#7" in steps and "план github:acme/parser#7" in steps
    assert "площадка" not in steps


def test_next_command_prints_the_calculated_steps(tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch,
                                                  capsys: pytest.CaptureFixture) -> None:
    """В cmd_next шаг был вписан текстом и не совпадал с расчётом."""
    from agent.ledger import Opportunity, set_status, upsert_opportunities

    upsert_opportunities([Opportunity(
        id="market:agenthansa:q88", channel="agent_marketplaces",
        title="[AgentHansa] Квест", reward_usd=90.0, repo="agenthansa",
        payload={"quest_id": "q88", "ev_per_hour": 15.0, "effort_hours": 3.0},
    )])
    set_status("market:agenthansa:q88", "queued")

    assert main.main(["дальше"]) == 0
    output = capsys.readouterr().out
    assert "площадка квест q88" in output
    assert "досье market:agenthansa:q88" not in output


# --- досье: у квеста нет репозитория, и выдумывать его нельзя ----------------

def test_quest_dossier_does_not_invent_a_github_repo(tmp_path: Path) -> None:
    from agent import dossier as dossier_mod
    from agent.ledger import Opportunity

    item = Opportunity(
        id="market:agenthansa:q77", channel="agent_marketplaces",
        title="[AgentHansa] Обзор продукта", reward_usd=120.0, repo="agenthansa",
        payload={"quest_id": "q77", "description": "Обзор на 800 слов",
                 "requirements": "Ссылки на источники", "submissions": 3,
                 "submission_cap": 50, "source": "snapshot", "snapshot_age_hours": 5.0,
                 "verify_before_work": ["Прочитать правила квеста"]},
    )
    card = dossier_mod.DossierBuilder().build({
        "id": item.id, "channel": item.channel, "title": item.title,
        "repo": item.repo, "reward_usd": item.reward_usd, "url": "",
        "payload": item.payload,
    })
    text = card.to_markdown()
    assert "github.com/agenthansa" not in text
    assert "Досье по квесту" in text
    assert "площадка агентов · квест q77" in text
    assert "Обзор на 800 слов" in text
    assert "Ссылки на источники" in text
    assert "отчёту 5.0ч" in text
    assert "Отправляет человек" in text


def test_quest_dossier_command_says_it_is_not_github(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture) -> None:
    from agent import dossier as dossier_mod
    from agent.ledger import Opportunity, upsert_opportunities

    monkeypatch.setattr(main, "project_root", lambda: tmp_path)
    monkeypatch.setattr(dossier_mod, "project_root", lambda: tmp_path)

    upsert_opportunities([Opportunity(
        id="market:agenthansa:q9", channel="agent_marketplaces", title="[AgentHansa] Квест",
        reward_usd=40.0, payload={"quest_id": "q9"},
    )])
    assert main.main(["досье", "market:agenthansa:q9"]) == 0
    output = capsys.readouterr().out
    assert "по квесту площадки" in output
    assert "github.com/" not in output


# --- дашборд: площадка видна на странице -------------------------------------

def reset_dashboard_cache() -> None:
    from agent import dashboard

    dashboard._HANSA["data"] = None
    dashboard._HANSA["at"] = 0.0


def test_dashboard_shows_the_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agent import dashboard

    write_snapshot(tmp_path, recent_stamp(1.5), [
        QUEST_JSON,
        {"id": "q-1", "title": "Мелкий квест", "reward_amount": 15},
    ])
    monkeypatch.setattr(hansa, "project_root", lambda: tmp_path)
    reset_dashboard_cache()

    hansa_state = dashboard._hansa()
    assert hansa_state["quest_count"] == 2
    assert hansa_state["best_reward"] == 120.0
    assert hansa_state["sum_reward"] == 135.0
    assert not hansa_state["stale"]
    assert hansa_state["quests"][0]["id"] == "q-77", "лучший квест идёт первым"


def test_dashboard_marks_a_stale_snapshot(tmp_path: Path,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    from agent import dashboard

    write_snapshot(tmp_path, recent_stamp(50.0), [QUEST_JSON])
    monkeypatch.setattr(hansa, "project_root", lambda: tmp_path)
    reset_dashboard_cache()
    assert dashboard._hansa()["stale"] is True


def test_dashboard_hansa_survives_a_broken_file(tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    from agent import dashboard

    (tmp_path / "snapshots").mkdir()
    (tmp_path / "snapshots" / "hansa-report.json").write_text("не json", encoding="utf-8")
    monkeypatch.setattr(hansa, "project_root", lambda: tmp_path)
    reset_dashboard_cache()
    state = dashboard._hansa()
    assert "не читается" in state["problem"] and state["quests"] == []


def test_dashboard_hansa_is_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Страница обновляется каждые 2.5 секунды — файл не перечитываем зря."""
    from agent import dashboard

    write_snapshot(tmp_path, recent_stamp(1.0), [QUEST_JSON])
    monkeypatch.setattr(hansa, "project_root", lambda: tmp_path)
    reset_dashboard_cache()
    assert dashboard._hansa()["quest_count"] == 1

    write_snapshot(tmp_path, recent_stamp(0.1), [])
    assert dashboard._hansa()["quest_count"] == 1, "второй вызов берёт кэш"


@pytest.mark.skipif(shutil.which("node") is None, reason="нужен node для проверки интерфейса")
def test_dashboard_renders_the_platform_block(tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    import json
    import re
    import subprocess

    from agent import dashboard

    write_snapshot(tmp_path, recent_stamp(0.5), [QUEST_JSON])
    monkeypatch.setattr(hansa, "project_root", lambda: tmp_path)
    reset_dashboard_cache()
    state = dashboard.collect_state()
    assert state["hansa"]["quest_count"] == 1

    js = re.search(r"<script>(.*?)</script>", dashboard.PAGE, re.S).group(1)
    harness = tmp_path / "ui.js"
    harness.write_text(
        "const document = {getElementById: () => ({innerHTML: '', style: {}, className: '', "
        "textContent: '', value: '', addEventListener: () => {}, disabled: false}), "
        "querySelectorAll: () => [], addEventListener: () => {}};\n"
        "const navigator = {clipboard: {writeText: async () => {}}};\n"
        "const STATE = " + json.dumps(state, ensure_ascii=False) + ";\n"
        "const fetch = async () => ({json: async () => STATE, text: async () => ''});\n" + js +
        "\nrefresh().then(() => process.exit(0), (e) => { console.error(e); process.exit(1); });\n",
        encoding="utf-8",
    )
    result = subprocess.run(["node", str(harness)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
