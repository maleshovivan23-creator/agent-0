"""Кошелёк для выплат: адрес проверяется до того, как на него пообещали заплатить.

Крипто-перевод необратим, поэтому здесь проверяются векторы контрольных сумм
(EIP-55, base58check TRON) и живые примеры адресов Phantom/Solana.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent import doctor, main, setupenv, wallet

#: Официальные примеры EIP-55 из спецификации Ethereum.
EIP55_SAMPLES = [
    "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed",
    "0xfB6916095ca1df60bB79Ce92cE3Ea74c37c5d359",
    "0xdbF03B407c01E7cD3CBea99509d93f8DDDC8C6FB",
    "0xD1220A0cf47c7B9Be7A2E6BA89F429762e7b9aDb",
]

#: Реальный адрес Solana (аккаунт USDC) — формат, который выдаёт Phantom.
SOLANA_ADDRESS = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
#: Реальный адрес TRON (USDT TRC-20).
TRON_ADDRESS = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


# --- контрольные суммы -------------------------------------------------------


@pytest.mark.parametrize("data,expected", [
    (b"", "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"),
    (b"abc", "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"),
])
def test_keccak256_matches_the_known_vectors(data: bytes, expected: str) -> None:
    """Keccak-256 — не SHA3-256: у них разное заполнение, хеши не совпадают."""
    assert wallet.keccak256(data).hex() == expected


@pytest.mark.parametrize("address", EIP55_SAMPLES)
def test_eip55_examples_are_accepted(address: str) -> None:
    info = wallet.classify(address)
    assert info.ok and info.kind == "evm"
    assert info.normalized == address, "адрес уже в правильном регистре — не переписываем"


def test_a_single_flipped_letter_is_caught() -> None:
    """Живой риск: опечатка в регистре отправляет деньги на другой адрес."""
    broken = EIP55_SAMPLES[0][:-1] + "D"
    info = wallet.classify(broken)
    assert not info.ok
    assert "EIP-55" in info.problem
    assert "Copy" in info.advice or "кошел" in info.advice.lower()


def test_all_lowercase_evm_address_is_accepted_and_normalized() -> None:
    """Кошельки часто копируют адрес строчными — это не ошибка, а формат."""
    info = wallet.classify(EIP55_SAMPLES[0].lower())
    assert info.ok
    assert info.normalized == EIP55_SAMPLES[0]


def test_short_evm_address_is_rejected_with_an_explanation() -> None:
    info = wallet.classify("0x1234")
    assert not info.ok
    assert "40" in info.problem


# --- Solana (Phantom) и TRON -------------------------------------------------


def test_solana_address_from_phantom_is_usable() -> None:
    info = wallet.classify(SOLANA_ADDRESS)
    assert info.ok and info.kind == "solana"
    assert "Solana" in info.networks
    assert "контрольной суммы" in info.advice, (
        "у адресов Solana нет контрольной суммы — человек должен знать, что опечатка невидима"
    )
    assert "Base" in info.advice, "нужно объяснить, что для Base нужен EVM-адрес"


@pytest.mark.parametrize("broken", [
    SOLANA_ADDRESS[:-1] + "O",   # буква O запрещена в base58
    SOLANA_ADDRESS[:-1] + "0",   # ноль тоже запрещён: его путают с O
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v" + "1" * 20,
])
def test_broken_solana_address_is_rejected(broken: str) -> None:
    assert not wallet.classify(broken).ok


def test_tron_address_passes_base58check() -> None:
    info = wallet.classify(TRON_ADDRESS)
    assert info.ok and info.kind == "tron"
    assert "TRC-20" in info.advice or "USDT" in info.advice


def test_tampered_tron_address_is_rejected() -> None:
    broken = TRON_ADDRESS[:-1] + "u"
    info = wallet.classify(broken)
    assert not info.ok
    assert "base58check" in info.problem


def test_garbage_is_rejected_without_guessing() -> None:
    info = wallet.classify("не адрес")
    assert not info.ok
    assert "не распознан" in info.problem


def test_address_is_masked_in_output() -> None:
    masked = wallet.mask(EIP55_SAMPLES[0])
    assert masked.startswith("0x5aAe") and masked.endswith("eAed")
    assert EIP55_SAMPLES[0] not in masked


# --- команда «кошелёк» -------------------------------------------------------


@pytest.fixture()
def env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / ".env.example").write_text("ELIGIBILITY_COUNTRY=DE\nPAYOUT_WALLET=\n",
                                           encoding="utf-8")
    monkeypatch.setattr(setupenv, "project_root", lambda: tmp_path)
    monkeypatch.delenv("PAYOUT_WALLET", raising=False)
    return tmp_path / ".env"


def test_wallet_command_accepts_a_valid_address(env_file: Path,
                                                capsys: pytest.CaptureFixture) -> None:
    assert main.main(["wallet", EIP55_SAMPLES[0]]) == 0
    output = capsys.readouterr().out
    assert "годен" in output
    # Адрес получателя — публичные данные, поэтому в подсказке для копирования
    # он печатается целиком; маска нужна только в коротких строках статуса
    # (там она помогает глазами сверить начало и конец адреса).
    assert wallet.mask(EIP55_SAMPLES[0]) in output
    assert f"кошелёк {EIP55_SAMPLES[0]} --save" in output


def test_wallet_command_refuses_a_broken_address(env_file: Path,
                                                 capsys: pytest.CaptureFixture) -> None:
    address = EIP55_SAMPLES[0][:-1] + "D"
    assert main.main(["wallet", address]) == 2
    output = capsys.readouterr().out
    assert "не годен" in output
    assert not env_file.exists() or address not in env_file.read_text(encoding="utf-8")


def test_russian_alias_can_save_the_address(env_file: Path,
                                            capsys: pytest.CaptureFixture) -> None:
    assert main.main(["кошелёк", EIP55_SAMPLES[0], "--save"]) == 0
    capsys.readouterr()
    text = env_file.read_text(encoding="utf-8")
    assert f"PAYOUT_WALLET={EIP55_SAMPLES[0]}" in text
    assert oct(env_file.stat().st_mode & 0o777) == "0o600"


def test_solana_address_gives_a_warning_about_base(env_file: Path,
                                                   capsys: pytest.CaptureFixture) -> None:
    assert main.main(["wallet", SOLANA_ADDRESS, "--save"]) == 0
    output = capsys.readouterr().out
    assert "нет контрольной суммы" in output


def test_saving_a_different_address_does_not_claim_it_is_already_saved(
        env_file: Path, capsys: pytest.CaptureFixture) -> None:
    """Живой случай: в .env был EVM-адрес, а на проверку дали адрес Solana."""
    setupenv.update_env({"PAYOUT_WALLET": EIP55_SAMPLES[0]})
    assert main.main(["wallet", SOLANA_ADDRESS]) == 0
    output = capsys.readouterr().out
    assert "уже записан" not in output


def test_json_output_is_machine_readable(env_file: Path,
                                         capsys: pytest.CaptureFixture) -> None:
    import json

    assert main.main(["wallet", EIP55_SAMPLES[0], "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True and data["kind"] == "evm"
    assert EIP55_SAMPLES[0] not in data["address"]


# --- связка с проверкой готовности ------------------------------------------


def test_broken_saved_address_is_reported_even_when_the_card_works(
        env_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Раньше при рабочей карте сохранённый адрес не проверялся вообще."""
    monkeypatch.setenv("ELIGIBILITY_COUNTRY", "DE")
    monkeypatch.setenv("PAYOUT_WALLET", EIP55_SAMPLES[0][:-1] + "D")
    check = doctor.check_wallet()
    assert check.status == doctor.WARN
    assert "0x… --save" in check.fix


def test_valid_saved_address_is_confirmed(env_file: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELIGIBILITY_COUNTRY", "DE")
    monkeypatch.setenv("PAYOUT_WALLET", SOLANA_ADDRESS)
    check = doctor.check_wallet()
    assert check.status == doctor.OK
    assert "solana" in check.detail.lower()


def test_wallet_is_optional_only_when_nothing_is_saved(
        env_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELIGIBILITY_COUNTRY", "DE")
    monkeypatch.delenv("PAYOUT_WALLET", raising=False)
    assert doctor.check_wallet().status == doctor.OK


def test_setupenv_uses_the_same_checks(env_file: Path) -> None:
    assert setupenv._check_wallet(SOLANA_ADDRESS) is None
    assert setupenv._check_wallet(TRON_ADDRESS) is None
    assert setupenv._check_wallet("мусор")
    assert setupenv._check_wallet(EIP55_SAMPLES[0][:-1] + "D")
