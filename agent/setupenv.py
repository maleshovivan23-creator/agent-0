"""Настройка .env: заполнение секретов без переписки и без риска утечки.

Секреты не должны ходить через чат, скриншоты и пересылки. Эта команда делает
правильный путь самым простым: спрашивает значение скрытым вводом (как пароль в
терминале), проверяет формат, пишет в ``.env`` и ставит права 600 — файл читает
только владелец. Секреты никогда не печатаются: наружу показывается только маска.
"""

from __future__ import annotations

import getpass
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple  # noqa: F401

from agent import wallet
from agent.config import project_root

EVM_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
TRON_RE = re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$")

MASK_HINT = "значение скрыто"


def _always_ok(value: str) -> Optional[str]:
    return None


def _check_token(value: str) -> Optional[str]:
    if len(value) < 20:
        return "слишком короткий токен: скопируйте его целиком"
    if value != value.strip():
        return "в токене лишние пробелы — скопируйте без них"
    return None


def _check_country(value: str) -> Optional[str]:
    trimmed = value.strip().upper()
    if not re.match(r"^[A-Z]{2}$", trimmed):
        return "нужен код страны из двух букв, например DE или RU"
    return None


def _check_wallet(value: str) -> Optional[str]:
    """Общая проверка с agent.wallet: EVM, Solana (Phantom) и TRON."""
    url = wallet.classify(value)
    if url.ok:
        return None
    return url.problem + (f" — {url.advice}" if url.advice else "")


@dataclass
class Field:
    name: str
    title: str
    hint: str
    secret: bool = False
    check: Callable[[str], Optional[str]] = _always_ok
    required: bool = False
    default: str = ""


FIELDS: List[Field] = [
    Field(
        "GITHUB_TOKEN",
        "Токен GitHub",
        "создать: github.com/settings/tokens → Generate new token (classic), "
        "галочки ставить не нужно, достаточно публичного доступа. "
        "Он поднимает лимит поиска с 60 до 5000 запросов в час.",
        secret=True,
        check=_check_token,
    ),
    Field(
        "ELIGIBILITY_COUNTRY",
        "Страна получения денег",
        "код из двух букв: DE, RU, KZ, GE, AM, AE… По нему ферма выбирает канал "
        "выплаты и не берёт задачи, деньги за которые не дойдут.",
        check=_check_country,
        required=True,
    ),
    Field(
        "PAYOUT_WALLET",
        "Кошелёк для USDC (Base)",
        "адрес вида 0x… — можно пропустить, если получаете деньги на карту или счёт. "
        "Нужен для третьего направления (квесты площадок) и крипто-выплат. "
        "В Phantom это адрес EVM (после включения сети Base), а не адрес Solana: "
        "проверить и сохранить — python -m agent.main кошелёк 0x… --save. "
        "Seed-фразу не вводите никогда и нигде.",
        check=_check_wallet,
    ),
    Field(
        "AGENTHANSA_API_KEY",
        "Ключ площадки квестов",
        "выдаётся после регистрации агента на agenthansa.com (почта, без кошелька). "
        "Открывает третье направление: задания $10–500 с выплатой в USDC.",
        secret=True,
    ),
    Field(
        "TELEGRAM_BOT_TOKEN",
        "Токен Telegram-бота",
        "по желанию: бот у @BotFather присылает уведомления о свежих задачах. "
        "Нужен вместе с TELEGRAM_CHAT_ID.",
        secret=True,
    ),
    Field(
        "TELEGRAM_CHAT_ID",
        "Ваш chat_id в Telegram",
        "по желанию: узнать можно у бота @userinfobot.",
    ),
]


def env_path() -> Path:
    return project_root() / ".env"


def read_env(path: Optional[Path] = None) -> Dict[str, str]:
    target = path or env_path()
    values: Dict[str, str] = {}
    if not target.exists():
        return values
    for line in target.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def mask(value: str) -> str:
    if not value:
        return "не задано"
    if len(value) <= 8:
        return "•" * len(value)
    return f"{value[:4]}{'•' * 6}{value[-4:]}"


def update_env(updates: Dict[str, str], path: Optional[Path] = None) -> Path:
    """Пишет значения в .env, сохраняя комментарии, и закрывает файл правами 600."""
    target = path or env_path()
    if not target.exists():
        example = project_root() / ".env.example"
        target.write_text(
            example.read_text(encoding="utf-8") if example.exists() else "",
            encoding="utf-8",
        )

    lines = target.read_text(encoding="utf-8").splitlines()
    remaining = {key: value for key, value in updates.items() if value}
    out: List[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                out.append(f"{key}={remaining.pop(key)}")
                continue
        out.append(line)

    if remaining:
        out.append("")
        out.append("# Добавлено командой «настройка»")
        for key, value in remaining.items():
            out.append(f"{key}={value}")

    target.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    try:
        os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:  # на экзотических файловых системах права могут не поддержаться
        pass
    return target


def collect(answers: Dict[str, str], interactive: bool = True) -> Tuple[Dict[str, str], List[str], List[str]]:
    """Собрать значения: из аргументов, из файла или вопросом в терминале."""
    current = read_env()
    updates: Dict[str, str] = {}
    problems: List[str] = []
    notes: List[str] = []

    for field in FIELDS:
        provided = (answers.get(field.name) or "").strip()
        if provided:
            error = field.check(provided)
            if error:
                problems.append(f"{field.title}: {error}")
            else:
                updates[field.name] = provided.strip()
            continue

        existing = (current.get(field.name) or "").strip()
        if existing:
            notes.append(f"{field.title}: уже задано ({mask(existing)})")
            continue

        if not interactive:
            if field.required:
                problems.append(f"{field.title}: не задано (обязательно)")
            else:
                notes.append(f"{field.title}: пропущено")
            continue

        print()
        print(f"— {field.title}")
        print(f"  {field.hint}")
        suffix = "" if field.required else " [Enter — пропустить]"
        prompt = f"  Значение{suffix}: "
        value = getpass.getpass(prompt) if field.secret else input(prompt)
        value = (value or "").strip()
        if not value:
            if field.required:
                problems.append(f"{field.title}: не задано (обязательно)")
            else:
                notes.append(f"{field.title}: пропущено")
            continue
        error = field.check(value)
        if error:
            problems.append(f"{field.title}: {error}")
            continue
        updates[field.name] = value

    return updates, problems, notes


def summary(updates: Dict[str, str], problems: List[str], notes: List[str],
            path: Optional[Path] = None) -> List[str]:
    target = path or env_path()
    lines: List[str] = []
    if updates:
        lines.append(f"Записано в {target.name}:")
        for field in FIELDS:
            if field.name in updates:
                shown = mask(updates[field.name]) if field.secret else updates[field.name]
                lines.append(f"  {field.title}: {shown}")
    if notes:
        lines.append("")
        lines.extend(f"  {note}" for note in notes)
    if problems:
        lines.append("")
        lines.append("Не принято:")
        lines.extend(f"  ✗ {problem}" for problem in problems)
    return lines
