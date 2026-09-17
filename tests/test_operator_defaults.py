"""Публичные настройки оператора: их нельзя потерять вместе с окружением.

Песочница (и любая машина) может перезапуститься: `.venv`, `.env` и база
исчезнут. Адрес кошелька, по которому придёт выплата, терять нельзя — поэтому он
лежит в git, а окружение только переопределяет его при необходимости.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from agent import config


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(config, "project_root", lambda: tmp_path)
    for name in ("PAYOUT_WALLET", "TASKMARKET_WALLET", "ELIGIBILITY_COUNTRY"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def _write(root: Path, values: dict) -> None:
    (root / "data" / "operator.yaml").write_text(
        yaml.safe_dump(values, allow_unicode=True), encoding="utf-8")


def test_file_fills_what_the_environment_missing(workspace: Path) -> None:
    _write(workspace, {"payout_wallet": "0x1111111111111111111111111111111111111111",
                       "country": "DE"})
    config.apply_operator_defaults()
    assert os.environ["PAYOUT_WALLET"] == "0x1111111111111111111111111111111111111111"
    assert os.environ["ELIGIBILITY_COUNTRY"] == "DE"
    assert "TASKMARKET_WALLET" not in os.environ, "чего нет в файле — то не появляется"


def test_environment_beats_the_file(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(workspace, {"payout_wallet": "0x1111111111111111111111111111111111111111"})
    monkeypatch.setenv("PAYOUT_WALLET", "0x2222222222222222222222222222222222222222")
    config.apply_operator_defaults()
    assert os.environ["PAYOUT_WALLET"] == "0x2222222222222222222222222222222222222222"


def test_empty_value_counts_as_missing(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`PAYOUT_WALLET=` в .env — это «не настроено», а не «пустой кошелёк»."""
    _write(workspace, {"payout_wallet": "0x3333333333333333333333333333333333333333"})
    monkeypatch.setenv("PAYOUT_WALLET", "   ")
    config.apply_operator_defaults()
    assert os.environ["PAYOUT_WALLET"] == "0x3333333333333333333333333333333333333333"


def test_broken_file_does_not_break_the_run(workspace: Path) -> None:
    (workspace / "data" / "operator.yaml").write_text("{{{ не yaml", encoding="utf-8")
    config.apply_operator_defaults()  # не должно бросить
    assert "PAYOUT_WALLET" not in os.environ


#: Настоящий корень репозитория: conftest подменяет project_root на временный,
#: а этот тест проверяет именно тот файл, который лежит в git.
REAL_ROOT = Path(__file__).resolve().parent.parent


def test_shipped_file_has_only_public_data() -> None:
    """Страховка: в git попадают адреса, но никогда — ключи и seed-фразы.

    Проверяются значения, а не текст файла: в комментариях слово «seed» стоит
    именно для того, чтобы напомнить — ему там не место.
    """
    import re

    raw = (REAL_ROOT / config.OPERATOR_REL).read_text(encoding="utf-8")
    data = yaml.safe_load(raw) or {}
    assert isinstance(data, dict)

    for key, value in data.items():
        text = str(value)
        if key.endswith("_wallet"):
            assert re.fullmatch(r"0x[0-9a-fA-F]{40}", text), f"{key}: это не EVM-адрес"
        else:
            assert len(text) <= 8, f"{key}: значение длиннее кода страны — что это?"

    # Приватный ключ — это 64 шестнадцатеричных символа; seed-фраза — 12+ слов.
    assert not re.search(r"\b[0-9a-fA-F]{64}\b", raw), "похоже на приватный ключ"
    assert not re.search(r"(?:\b[a-z]{3,8}\b\s+){11,}", raw), "похоже на seed-фразу"


def test_restore_script_is_executable_and_mentions_the_robot() -> None:
    """Скрипт восстановления — первый шаг после перезапуска среды, он должен быть на месте."""
    script = REAL_ROOT / "scripts" / "restore.sh"
    assert script.exists()
    assert os.access(script, os.X_OK), "restore.sh должен быть исполняемым"
    text = script.read_text(encoding="utf-8")
    assert "requirements-dev.txt" in text
    assert "проверка" in text, "скрипт заканчивается проверкой готовности"
