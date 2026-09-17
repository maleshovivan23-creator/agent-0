"""Общие правила для тестов: рабочее дерево не трогаем.

Раньше часть тестов сохраняла артефакты (досье, черновики, отчёты) прямо в
``reports/`` проекта, потому что брала ``project_root()`` из настоящего
каталога. Файлы попадали в рабочее дерево и путались с результатами живых
прогонов: в ``reports/inbox`` лежали досье по выдуманным задачам тестов.

Здесь каталог проекта один раз подменяется на временный, и это работает для всех
модулей: те, что импортируют ``project_root`` на уровне модуля, патчатся по
именам, а остальные берут функцию из ``agent.config`` во время вызова.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

#: Модули, которые держат ссылку на project_root в своём пространстве имён.
_MODULES_WITH_ROOT = (
    "agent.autopilot",
    "agent.doctor",
    "agent.dossier",
    "agent.hansa",
    "agent.main",
    "agent.quests",
    "agent.setupenv",
    "agent.watchdog",
    "agent.inbox",
    "agent.learning",
    "agent.ledger",
    "agent.dashboard",
)


@pytest.fixture(autouse=True)
def sandbox_project_root(tmp_path_factory: pytest.TempPathFactory,
                         monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Подменить каталог проекта на временный для всего прогона тестов."""
    import importlib

    root = tmp_path_factory.mktemp("project")
    (root / "data").mkdir(exist_ok=True)
    (root / "reports").mkdir(exist_ok=True)

    import agent.config as config

    monkeypatch.setattr(config, "project_root", lambda: root)
    for name in _MODULES_WITH_ROOT:
        try:
            module = importlib.import_module(name)
        except Exception:  # модуль необязателен — пропускаем
            continue
        if hasattr(module, "project_root"):
            monkeypatch.setattr(module, "project_root", lambda: root, raising=False)
    yield root
