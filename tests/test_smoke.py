"""Дымовой тест: каждая команда должна отвечать без падения.

Смысл теста — не проверить логику (для этого есть отдельные файлы), а поймать
то, что ломается только на живом запуске: опечатка в имени поля, обращение к
ключам результата, который пришёл не по тому пути. Такие ошибки не видны в
модульных тестах и всплывают ровно тогда, когда робот работает ночью.

Сеть подменяется: тест не должен зависеть от лимитов GitHub и доступности
площадок. Команды, которые честно отвечают «не найдено» или «нужны настройки»,
проходят проверку — важен только факт, что код не упал.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from agent import main
from agent.ledger import Opportunity, set_status, upsert_opportunities

#: Команда → аргументы. Список команд берётся из самого парсера: если добавится
#: новая команда и её забудут здесь, тест об этом скажет.
COMMAND_ARGS: Dict[str, List[str]] = {
    "status": [],
    "next": [],
    "queue": ["--limit", "3"],
    "directions": [],
    "analytics": [],
    "ledger": [],
    "policy": [],
    "whoami": [],
    "wallet": ["0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"],
    "hansa": ["статус"],
    "report": [],
    "contests": [],
    "learning": [],
    "watchdog": [],
    "inbox": ["--limit", "3"],
    "inbox-done": ["999"],
    "inbox-skip": ["999"],
    "hours-add": ["--channel", "github_bounties", "--hours", "1"],
    "payout-add": ["--channel", "github_bounties", "--amount", "5"],
    "payout-verify": ["999"],
    "payout-rails": ["--country", "DE"],
    "eligibility": ["--country", "DE"],
    "doctor": [],
    "setup": ["--no-ask"],
    "shift": ["--hours", "1"],
    "triage": ["github:acme/parser#7"],
    "plan": ["github:acme/parser#7"],
    "apply": ["github:acme/parser#7", "--country", "DE"],
    "dossier": ["github:acme/parser#7"],
    "quest": ["market:agenthansa:q7"],
    "followup": [],
    "status-set": ["github:acme/parser#7", "working"],
    "cycle": ["--limit", "3", "--channel", "github_bounties"],
    "autopilot": ["--status"],
}

#: Команды, которые по устройству работают вечно: их нельзя запускать в тесте.
#: Для них проверяем только то, что парсер их знает.
LONG_RUNNING = {"loop": [], "serve": []}

#: Команды, которые намеренно останавливаются по условию (нет задачи, нужны
#: настройки) — для них важен не код возврата, а отсутствие падения.
ALWAYS_RUN = {
    "status", "next", "queue", "directions", "analytics", "ledger", "policy",
    "whoami", "report", "contests", "learning", "watchdog", "inbox", "inbox-done", "wallet", "hansa",
    "inbox-skip", "payout-rails", "eligibility", "doctor", "setup", "shift",
    "cycle", "autopilot", "followup", "triage",
}


class FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200,
                 headers: Optional[Dict[str, str]] = None) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {
            "x-ratelimit-remaining": "42", "x-ratelimit-limit": "60",
        }
        self.ok = status_code < 400

    def json(self) -> Any:
        return self._payload


class FakeSession:
    """Отвечает пустыми, но правдоподобными данными на любые запросы."""

    def __init__(self, *_: Any, **__: Any) -> None:
        self.headers: Dict[str, str] = {}

    def get(self, url: str, params: Any = None, timeout: int = 0) -> FakeResponse:
        if "rate_limit" in url:
            return FakeResponse({"resources": {"search": {"remaining": 42}}})
        if "search/issues" in url:
            return FakeResponse({"items": []})
        if "timeline" in url or "/comments" in url:
            return FakeResponse([])
        if "/contents/" in url:
            return FakeResponse({}, status_code=404)
        if "/git/trees/" in url:
            return FakeResponse({"tree": []})
        return FakeResponse({})

    def post(self, url: str, **_: Any) -> FakeResponse:
        return FakeResponse({})

    def request(self, *_: Any, **__: Any) -> FakeResponse:
        return FakeResponse({})


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Никаких реальных запросов и никакой записи в рабочую базу."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "smoke.db"))
    monkeypatch.setenv("ELIGIBILITY_COUNTRY", "DE")
    monkeypatch.setenv("AGENTHANSA_API_KEY", "")
    monkeypatch.setenv("AUTOPILOT_ENABLED", "true")

    import agent.autopilot as autopilot_mod
    import agent.channels.agent_marketplaces as marketplaces
    import agent.channels.audit_contests as contests
    import agent.channels.github_bounties as bounties
    import agent.dossier as dossier_mod
    import agent.http as http_mod
    import agent.llm as llm_mod
    import agent.triage as triage_mod

    for module in (http_mod, bounties, contests, marketplaces, triage_mod, dossier_mod):
        monkeypatch.setattr(module, "build_session", FakeSession, raising=False)

    # модель не должна ходить в сеть и ждать таймаут
    monkeypatch.setenv("LLM_PROVIDER", "none")
    monkeypatch.setattr(llm_mod.LLMClient, "resolve_provider", lambda self: "none")

    # автопилот и его артефакты пишут только во временный каталог
    monkeypatch.setattr(autopilot_mod, "project_root", lambda: tmp_path)
    (tmp_path / "reports").mkdir(exist_ok=True)
    (tmp_path / "data").mkdir(exist_ok=True)

    # в очереди должны быть задачи обоих видов: иначе половина путей не проверится
    upsert_opportunities([
        Opportunity(id="github:acme/parser#7", channel="github_bounties",
                    title="Fix the tokenizer", repo="acme/parser", reward_usd=200.0,
                    payload={"ev_per_hour": 25.0, "effort_hours": 4.0,
                             "expected_value_usd": 100.0, "triage_verdict": "ready",
                             "query": "label:bounty", "playbook": "docs"}),
        Opportunity(id="market:agenthansa:q7", channel="agent_marketplaces",
                    title="[AgentHansa] Сравнить площадки", reward_usd=40.0,
                    payload={"ev_per_hour": 12.0, "effort_hours": 2.0,
                             "expected_value_usd": 24.0, "submissions": 3,
                             "submission_cap": 50, "payout_rail": "crypto_usdc",
                             "submit_route": "https://agenthansa.com/x"}),
        Opportunity(id="contest:acme/2026-01-x", channel="audit_contests",
                    title="Code4rena: 2026-01-x", repo="acme/2026-01-x",
                    reward_usd=2000.0,
                    payload={"ev_per_hour": 22.0, "effort_hours": 20.0,
                             "expected_value_usd": 600.0, "scope_entries": 30}),
    ])
    set_status("github:acme/parser#7", "queued")


def parser_commands() -> set:
    parser = main.build_parser()
    return set(parser._subparsers._group_actions[0].choices)  # type: ignore[union-attr]


def test_every_command_from_the_parser_is_covered() -> None:
    real = {name for name in parser_commands() if name not in main.ALIASES}
    missing = real - set(COMMAND_ARGS) - set(LONG_RUNNING)
    assert not missing, f"новая команда без дымового теста: {sorted(missing)}"


def test_long_running_commands_are_known_to_the_parser() -> None:
    assert set(LONG_RUNNING) <= parser_commands()


@pytest.mark.parametrize("command", sorted(COMMAND_ARGS))
def test_command_does_not_crash(command: str, capsys: pytest.CaptureFixture) -> None:
    code = main.main([command] + COMMAND_ARGS[command])
    output = capsys.readouterr()
    if code not in (0, 1, 2):
        pytest.fail(f"{command}: неожиданный код возврата {code}\n{output.out[-2000:]}")
    assert "Traceback" not in output.out + output.err, (
        f"{command} упал с ошибкой:\n{output.out[-2000:]}{output.err[-2000:]}"
    )


@pytest.mark.parametrize("alias,canonical", sorted(main.ALIASES.items()))
def test_russian_alias_runs_too(alias: str, canonical: str,
                                capsys: pytest.CaptureFixture) -> None:
    """Русские имена ведут в те же обработчики, что и английские."""
    args = COMMAND_ARGS.get(canonical)
    if args is None:
        pytest.skip(f"{canonical} работает вечно — в дымовом тесте не запускается")
    code = main.main([alias] + args)
    output = capsys.readouterr()
    assert code in (0, 1, 2)
    assert "Traceback" not in output.out + output.err, output.out[-1500:]
