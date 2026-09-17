"""Настройка .env: секреты не печатаются, файл закрыт от посторонних."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from agent import setupenv


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    example = tmp_path / ".env.example"
    example.write_text(
        "# комментарий остаётся на месте\nGITHUB_TOKEN=\nELIGIBILITY_COUNTRY=\n"
        "MIN_EV_PER_HOUR=3\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(setupenv, "project_root", lambda: tmp_path)
    return tmp_path / ".env"


def test_update_env_creates_file_from_example(env: Path) -> None:
    setupenv.update_env({"ELIGIBILITY_COUNTRY": "DE"})
    text = env.read_text(encoding="utf-8")
    assert "# комментарий остаётся на месте" in text
    assert "ELIGIBILITY_COUNTRY=DE" in text
    assert "MIN_EV_PER_HOUR=3" in text, "значения по умолчанию не должны теряться"


def test_env_file_is_private(env: Path) -> None:
    setupenv.update_env({"ELIGIBILITY_COUNTRY": "DE"})
    mode = stat.S_IMODE(env.stat().st_mode)
    assert mode == 0o600, f"файл с секретом должен быть закрыт, а не {oct(mode)}"


def test_unknown_keys_are_appended_with_a_comment(env: Path) -> None:
    env.write_text("ELIGIBILITY_COUNTRY=DE\n", encoding="utf-8")
    setupenv.update_env({"TELEGRAM_CHAT_ID": "12345"})
    text = env.read_text(encoding="utf-8")
    assert "TELEGRAM_CHAT_ID=12345" in text
    assert "Добавлено командой" in text


def test_read_env_ignores_comments_and_blank_lines(env: Path) -> None:
    setupenv.update_env({"ELIGIBILITY_COUNTRY": "KZ", "GITHUB_TOKEN": "ghp_" + "x" * 30})
    values = setupenv.read_env()
    assert values["ELIGIBILITY_COUNTRY"] == "KZ"
    assert len(values["GITHUB_TOKEN"]) == 34


def test_secrets_are_masked_and_never_printed(env: Path) -> None:
    secret = "ghp_" + "s" * 30
    shown = setupenv.mask(secret)
    assert secret not in shown
    assert shown.startswith("ghp_") and shown.endswith("ssss")
    assert setupenv.mask("") == "не задано"
    assert set(setupenv.mask("корот")) == {"•"}


def test_summary_masks_the_token(env: Path) -> None:
    secret = "ghp_" + "s" * 30
    setupenv.update_env({"GITHUB_TOKEN": secret})
    text = "\n".join(setupenv.summary({"GITHUB_TOKEN": secret}, [], [], path=env))
    assert secret not in text
    assert "Токен GitHub" in text


@pytest.mark.parametrize(
    "country,expected_error",
    [("DE", False), ("ru", False), ("", True), ("Германия", True), ("D", True)],
)
def test_country_validation(country: str, expected_error: bool) -> None:
    error = setupenv._check_country(country)
    assert (error is not None) is expected_error


@pytest.mark.parametrize(
    "wallet,ok",
    [
        ("0x" + "a" * 40, True),
        # Настоящий адрес USDT TRC-20: раньше здесь стоял выдуманный "T" + "1" * 33,
        # который проходил только по длине, а проверку base58check не проходит —
        # на такой адрес деньги ушли бы в никуда.
        ("TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", True),
        ("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", True),  # Phantom, сеть Solana
        ("0x" + "a" * 39, False),
        ("T" + "1" * 33, False),
        ("мой кошелёк", False),
        ("", False),
    ],
)
def test_wallet_validation(wallet: str, ok: bool) -> None:
    assert (setupenv._check_wallet(wallet) is None) is ok


def test_token_must_not_be_truncated() -> None:
    assert setupenv._check_token("ghp_" + "x" * 36) is None
    assert setupenv._check_token("ghp_short") is not None
    assert setupenv._check_token(" ghp_" + "x" * 36) is not None


def test_non_interactive_mode_reports_missing_required(env: Path) -> None:
    updates, problems, notes = setupenv.collect({}, interactive=False)
    assert updates == {}
    assert any("обязательно" in problem for problem in problems)
    assert any("пропущено" in note for note in notes)


def test_non_interactive_mode_writes_what_it_was_given(env: Path) -> None:
    updates, problems, notes = setupenv.collect(
        {"ELIGIBILITY_COUNTRY": "DE", "GITHUB_TOKEN": "ghp_" + "x" * 36},
        interactive=False,
    )
    assert problems == []
    assert updates["ELIGIBILITY_COUNTRY"] == "DE"


def test_existing_value_is_not_asked_again(env: Path) -> None:
    setupenv.update_env({"ELIGIBILITY_COUNTRY": "KZ"})
    updates, problems, _ = setupenv.collect({}, interactive=False)
    assert updates == {}
    assert problems == []
