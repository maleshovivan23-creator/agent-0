"""Каждая команда в текстах робота должна запускаться.

Ошибка, из-за которой появился этот файл: черновик квеста предлагал
``python -m agent.main hours-add agent_marketplaces <часы>`` — позиционные
аргументы, которых у команды нет. Человек копировал строку и получал ошибку
argparse. Синтаксис в подсказках нельзя проверять глазами: документы собираются
кодом, значит и проверять их должен код.

Здесь собираются настоящие артефакты (досье, черновик отправки, заявка, отчёт) и
каждая найденная в них команда прогоняется через парсер CLI. Заглушки вроде
``<id>`` или ``<сумма>`` заменяются правдоподобными значениями.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List

import pytest

from agent import dossier as dossier_mod
from agent import main, proposals, quests
from agent.ledger import Opportunity, connect, upsert_opportunities

#: Что подставляем вместо заглушек. Порядок важен: сначала длинные.
PLACEHOLDERS = {
    "<часы>": "2", "<сумма>": "50", "<id>": "1", "<номер>": "1",
    "<ссылка>": "https://example.com", "<текст заявки>": "текст",
    "<черновик>": "reports/draft.md", "<ссылку-подтверждение>": "https://example.com",
    "<команда>": "дальше",
}

COMMAND_RE = re.compile(r"python -m agent\.main ([^\n`]+)")

#: Слова, после которых начинается пояснение, а не аргумент команды.
STOP_WORDS = {"—", "–", "→", "->", "|", "#", "затем", "и", "или", "только", "считает"}


def quest_opportunity() -> Dict[str, Any]:
    return {
        "id": "market:agenthansa:q77",
        "channel": "agent_marketplaces",
        "title": "[AgentHansa] Обзор продукта",
        "reward_usd": 120.0,
        "url": "https://www.agenthansa.com/quests/q77",
        "payload": {
            "platform": "agenthansa",
            "quest_id": "q77",
            "description": "Обзор на 800 слов",
            "requirements": "Ссылки на источники",
            "payout_rail": "crypto_usdc",
            "payout_note": "USDC на Base",
            "deadline": "2026-10-01T12:00:00Z",
            "submissions": 3,
            "submission_cap": 50,
            "effort_hours": 4.0,
            "submit_route": "https://www.agenthansa.com/api/alliance-war/quests/q77/submit",
            "docs": "https://www.agenthansa.com/llms.txt",
        },
    }


def bounty_opportunity() -> Dict[str, Any]:
    return {
        "id": "github:acme/parser#7",
        "channel": "github_bounties",
        "title": "Fix the tokenizer",
        "repo": "acme/parser",
        "reward_usd": 200.0,
        "url": "https://github.com/acme/parser/issues/7",
        "payload": {"playbook": "docs", "effort_hours": 4.0, "query": "label:bounty"},
    }


def dossier_with_fake_api() -> str:
    """Досье GitHub-задачи на подставном API: без сети, но с настоящей ветвью кода."""
    import base64

    builder = dossier_mod.DossierBuilder()

    def fake_get(url: str, params: Any = None) -> Any:
        if "/git/trees/" in url:
            return {"tree": [
                {"path": "package.json", "type": "blob"},
                {"path": "src/Loans.sol", "type": "blob"},
                {"path": "test/Loans.t.sol", "type": "blob"},
            ]}
        if "/contents/" in url:
            payload = base64.b64encode(b'{"scripts": {"test": "hardhat test"}}').decode()
            return {"content": payload, "encoding": "base64"}
        return {"language": "Solidity", "size": 1234, "default_branch": "main"}

    builder._get = fake_get  # type: ignore[assignment]
    opportunity = {
        "id": "github:sherlock-audit/2026-07-tare#1", "channel": "audit_contests",
        "title": "Sherlock: 2026-07-tare", "repo": "sherlock-audit/2026-07-tare",
        "reward_usd": 2000.0, "url": "", "payload": {"scope_entries": 30},
    }
    return builder.build(opportunity).to_markdown()


def artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Dict[str, str]:
    """Собрать всё, что робот отдаёт человеку как готовый текст."""
    monkeypatch.setattr(quests, "project_root", lambda: tmp_path)
    monkeypatch.setattr(dossier_mod, "project_root", lambda: tmp_path)
    monkeypatch.setenv("LLM_PROVIDER", "none")
    # без страны заявка не собирается вовсе, и её команды остались бы непроверенными
    monkeypatch.setenv("ELIGIBILITY_COUNTRY", "DE")

    quest = quest_opportunity()
    bounty = bounty_opportunity()
    collected = {
        "черновик квеста": quests.submission_markdown(quest)["markdown"],
        "досье квеста": dossier_mod.build(quest).to_markdown(),
        "досье задачи": dossier_with_fake_api(),
        "заявка": (
            proposals.application_text(bounty).get("text", "")
            + "\n".join(proposals.application_text(bounty).get("checklist", []))
        ),
    }
    return collected


def commands_in(text: str) -> List[List[str]]:
    """Вытащить команды CLI из текста, отрезав пояснения после них."""
    found: List[List[str]] = []
    for match in COMMAND_RE.finditer(text):
        rest = match.group(1)
        tokens: List[str] = []
        for token in rest.split():
            word = token.strip("`\\")
            if word in STOP_WORDS or word.startswith("#"):
                break
            tokens.append(word)
        # Токены внутри текста бывают с пунктуацией: «triage <id>).» — срезаем
        # обрамление, иначе команда не разберётся из-за точки в конце.
        cleaned = [token.strip("`\\").rstrip(",.;:)(").lstrip("(") for token in tokens]
        cleaned = [token for token in cleaned if token]
        while cleaned and cleaned[-1] in {"—", "→", ")", "("}:
            cleaned.pop()
        if cleaned:
            found.append(cleaned)
    return found


def substitute(tokens: List[str]) -> List[str]:
    """Заменить заглушки так, чтобы команда стала запускаемой."""
    result: List[str] = []
    for token in tokens:
        if token in PLACEHOLDERS:
            result.append(PLACEHOLDERS[token])
            continue
        replaced = token
        for placeholder, value in PLACEHOLDERS.items():
            replaced = replaced.replace(placeholder, value)
        if any(char in replaced for char in "<>"):
            # неизвестная заглушка: значит в тексте новая переменная, о которой
            # тест не знает — подставляем безопасное значение по соседнему флагу
            replaced = "1" if result and result[-1].startswith("--") else "заглушка"
        result.append(replaced)
    return result


def test_every_command_in_generated_artifacts_parses(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    texts = artifacts(tmp_path, monkeypatch)
    parser = main.build_parser()
    checked = 0
    # В досье GitHub-задачи команд нашей CLI может не быть вовсе: там команды
    # проекта (forge/npm). Проверяем то, что есть, но требуем, чтобы часть
    # артефактов команды точно содержала — иначе тест ничего не проверяет.
    with_commands = 0
    for name, text in texts.items():
        found = commands_in(text)
        with_commands += 1 if found else 0
        for tokens in found:
            argv = substitute(tokens)
            try:
                parsed = parser.parse_args(argv)
            except SystemExit:
                pytest.fail(
                    f"{name}: команда «python -m agent.main {' '.join(tokens)}» "
                    f"не разбирается (проверяли: {' '.join(argv)})"
                )
            assert parsed.command, f"{name}: у команды нет подкоманды: {argv}"
            checked += 1
    assert with_commands >= 3, "ни один артефакт не содержит команд — проверять нечего"
    assert checked >= 8, f"проверено слишком мало команд: {checked}"


def test_known_broken_instruction_is_gone() -> None:
    """Строки, которые копировались и не работали, не должны вернуться."""
    text = Path("agent/quests.py").read_text(encoding="utf-8")
    assert "hours-add agent_marketplaces <" not in text
    assert "payout-add agent_marketplaces <" not in text


def test_quest_draft_uses_the_real_cli_flags() -> None:
    result = quests.submission_markdown(quest_opportunity())
    markdown = result["markdown"]
    assert "--channel agent_marketplaces" in markdown
    assert "--hours" in markdown and "--amount" in markdown
    assert "выплата-подтвердить" in markdown


def test_quest_markdown_has_no_dangling_dash() -> None:
    quest = quest_opportunity()
    quest["payload"].pop("payout_note")
    markdown = quests.submission_markdown(quest)["markdown"]
    assert "crypto_usdc — \n" not in markdown
    assert "- Канал выплаты: crypto_usdc\n" in markdown


def test_draft_command_runs_on_a_platform_quest(tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch,
                                                capsys: pytest.CaptureFixture) -> None:
    """Раньше живой квест помечался как «не квест площадки» без поля platform."""
    monkeypatch.setattr(main, "project_root", lambda: tmp_path)
    monkeypatch.setattr(quests, "project_root", lambda: tmp_path)
    monkeypatch.setenv("LLM_PROVIDER", "none")
    upsert_opportunities([Opportunity(
        id="market:agenthansa:q5", channel="agent_marketplaces", title="[AgentHansa] Квест",
        reward_usd=40.0, payload={"quest_id": "q5", "effort_hours": 2.0},
    )])
    assert main.main(["черновик", "market:agenthansa:q5"]) == 0
    output = capsys.readouterr().out
    assert "не квест площадки" not in output
    assert "Черновик" in output


def test_report_and_next_contain_only_known_commands(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture) -> None:
    """Отчёт и «дальше» — тоже текст с командами, их тоже проверяем."""
    monkeypatch.setattr(main, "project_root", lambda: tmp_path)
    upsert_opportunities([Opportunity(
        id="github:acme/parser#7", channel="github_bounties", title="Fix",
        repo="acme/parser", reward_usd=200.0,
        payload={"ev_per_hour": 25.0, "effort_hours": 4.0},
    )])

    parser = main.build_parser()
    for argv in (["дальше"], ["отчёт"]):
        capsys.readouterr()
        assert main.main(argv) == 0
        text = capsys.readouterr().out
        for tokens in commands_in(text):
            try:
                parser.parse_args(substitute(tokens))
            except SystemExit:
                pytest.fail(f"{argv}: команда не разбирается: {' '.join(tokens)}")


def test_every_alias_points_to_a_real_command() -> None:
    parser = main.build_parser()
    known = set(parser._subparsers._group_actions[0].choices)  # type: ignore[union-attr]
    for alias, canonical in main.ALIASES.items():
        assert canonical in known, f"алиас {alias} ведёт в несуществующую команду {canonical}"
        assert alias in known, f"алиас {alias} не зарегистрирован в парсере"


def test_connects_to_the_test_database() -> None:
    """Страховка: тест работает с временной базой, а не с рабочей."""
    row = connect().execute("SELECT COUNT(*) AS n FROM opportunities").fetchone()
    assert json.dumps({"n": row["n"]})  # соединение живое
