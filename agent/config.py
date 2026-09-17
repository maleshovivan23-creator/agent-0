from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


def project_root() -> Path:
    """Repository root, regardless of the current working directory."""
    return Path(__file__).resolve().parent.parent


def _project_root() -> Path:  # kept for backwards compatibility
    return project_root()


#: Публичные настройки оператора. Не секрет: адрес кошелька можно называть кому
#: угодно, приватный ключ ферме не нужен вовсе. Поэтому эти значения лежат в git
#: и переживают пересборку окружения и перезапуск машины, а секреты (ключи
#: площадок, токены ботов) остаются только в .env.
OPERATOR_REL = "data/operator.yaml"
OPERATOR_ENV_KEYS = {
    "payout_wallet": "PAYOUT_WALLET",
    "taskmarket_wallet": "TASKMARKET_WALLET",
    "country": "ELIGIBILITY_COUNTRY",
}


def apply_operator_defaults() -> None:
    """Дочитать публичные настройки оператора, если окружение их не задало.

    Порядок силы: настоящие переменные окружения и ``.env`` сильнее файла —
    файл только заполняет то, чего нет. Пустая строка считается отсутствием:
    ``PAYOUT_WALLET=`` в ``.env`` ничего не значит, и адрес из файла нужен.
    """
    raw = _operator_data()
    if not raw:
        return
    for key, env_name in OPERATOR_ENV_KEYS.items():
        if (os.getenv(env_name) or "").strip():
            continue
        value = str(raw.get(key) or "").strip()
        if value:
            os.environ[env_name] = value


def _operator_data() -> dict:
    path = project_root() / OPERATOR_REL
    if not path.exists():
        return {}
    try:
        import yaml

        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def operator_value(env_name: str) -> str:
    """Значение настройки: окружение сильнее файла, файл — сильнее пустоты.

    Нужно там, где модуль работает сам по себе — например, черновик работы
    печатает кошелёк, не запуская полную загрузку окружения.
    """
    value = (os.getenv(env_name) or "").strip()
    if value:
        return value
    key = next((k for k, name in OPERATOR_ENV_KEYS.items() if name == env_name), "")
    return str(_operator_data().get(key) or "").strip()


def load_environment() -> None:
    env_path = project_root() / ".env"
    if env_path.exists():
        load_dotenv(env_path)
    else:
        load_dotenv(project_root() / ".env.example")
    apply_operator_defaults()


def get_env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name, default)
    return value


def get_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "y", "on"}


def get_float(name: str, default: float = 0.0) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default
