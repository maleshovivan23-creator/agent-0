"""Русские названия команд и воркеров должны работать так же, как английские."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent import main
from agent.farm import resolve_channel


def test_every_alias_points_to_a_real_command() -> None:
    for alias, canonical in main.ALIASES.items():
        assert canonical in main.HANDLERS, f"{alias} -> {canonical} без обработчика"


#: Минимальные аргументы для команд с обязательными параметрами.
SAMPLE_ARGS: dict[str, list[str]] = {
    "triage": ["id"],
    "plan": ["id"],
    "apply": ["id"],
    "quest": ["id"],
    "status-set": ["id", "working"],
    "hours-add": ["--channel", "контесты", "--hours", "1"],
    "payout-add": ["--channel", "контесты", "--amount", "10"],
    "payout-verify": ["1"],
    "inbox-done": ["1"],
    "inbox-skip": ["1"],
    "payout-rails": ["--country", "DE"],
    "eligibility": ["--country", "DE"],
    "dossier": ["id"],
}


def test_russian_commands_are_parsed_and_dispatched() -> None:
    parser = main.build_parser()
    for russian, canonical in main.ALIASES.items():
        args = parser.parse_args([russian] + SAMPLE_ARGS.get(canonical, []))
        assert args.command == russian
        assert main.ALIASES.get(args.command, args.command) == canonical


def test_aliases_keep_their_arguments() -> None:
    parser = main.build_parser()
    assert parser.parse_args(["входящие", "show", "3"]).args == ["show", "3"]
    assert parser.parse_args(["входящие", "7"]).args == ["7"]
    assert parser.parse_args(["каналы-выплат", "--country", "DE"]).country == "DE"
    assert parser.parse_args(["цикл", "--channel", "контесты"]).channel == "контесты"
    assert parser.parse_args(["входящие-отправлено", "5"]).item_id == 5
    assert main.inbox_show_id(SimpleNamespace(args=["show", "5"]).args) == 5


def test_help_lists_russian_commands() -> None:
    help_text = main.build_parser().format_help()
    for word in ("проверка", "автопилот", "входящие", "смена", "направления"):
        assert word in help_text


def test_inbox_accepts_both_spellings() -> None:
    assert main.inbox_show_id(["show", "3"]) == 3
    assert main.inbox_show_id(["3"]) == 3
    assert main.inbox_show_id([]) is None
    assert main.inbox_show_id(["мусор"]) is None


def test_unknown_command_reports_usage() -> None:
    with pytest.raises(SystemExit) as exit_info:
        main.build_parser().parse_args(["нет-такой-команды"])
    assert exit_info.value.code == 2


@pytest.mark.parametrize(
    "russian,canonical",
    [
        ("контесты", "audit_contests"),
        ("площадки", "agent_marketplaces"),
        ("гитхаб", "github_bounties"),
        ("разведка", "bug_recon"),
    ],
)
def test_channel_aliases_resolve(russian: str, canonical: str) -> None:
    assert resolve_channel(russian) == canonical
    assert resolve_channel(canonical) == canonical
    assert resolve_channel(None) is None


def test_a_cycle_can_be_limited_by_a_russian_channel_name(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent import farm

    called: list[str] = []

    def fake_run_channel(name: str, limit: int = 0, triage_budget: object = None,
                         do_triage: bool = True) -> dict:
        called.append(name)
        return {"name": name, "allowed": True, "kept": 0, "found": 0}

    monkeypatch.setattr(farm, "run_channel", fake_run_channel)
    farm.run_cycle(channels=["площадки", "контесты"], parallel=False)
    assert called == ["agent_marketplaces", "audit_contests"]
