"""Возможности, которые ферма осознанно не берёт — с причиной.

Не всё, что выгодно по EV/час, можно сделать. GPU-конкурс может давать $17/час
на бумаге и быть невыполнимым, если у фермы нет NVIDIA-карты, а у задачи —
требование официального прогона на чужом раннере. «Отсеяно триажем» такого не
объясняет: триаж смотрит на конкуренцию, а здесь причина в наших возможностях.

Поэтому решение об отказе хранится отдельно от базы (``data/veto.yaml``, в git):
оно переживает пересборку ``data/agent.db`` — иначе после каждого обнуления базы
ферма снова предлагала бы задачу, которую человек уже отклонил.

Формат::

    - id: taskmarket:0x5f59…
      reason: нужен NVIDIA GPU и официальный прогон Yukon — не автоматизируется
      decided: 2026-09-17

Отказ — не приговор: убрать строку из файла достаточно, чтобы задача вернулась в
очередь.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.config import project_root

#: Где лежит список отказов относительно корня проекта.
VETO_REL = "data/veto.yaml"


def path(root: Optional[Path] = None) -> Path:
    return (root or project_root()) / VETO_REL


def entries(root: Optional[Path] = None) -> List[Dict[str, str]]:
    """Разобранный список отказов. Битый файл не роняет ферму, а просто пуст."""
    target = path(root)
    if not target.exists():
        return []
    try:
        import yaml

        raw: Any = yaml.safe_load(target.read_text(encoding="utf-8")) or []
    except Exception:
        return []
    if not isinstance(raw, list):
        return []
    result: List[Dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        identifier = str(item.get("id") or "").strip()
        if not identifier:
            continue
        result.append(
            {
                "id": identifier,
                "reason": str(item.get("reason") or "").strip(),
                "decided": str(item.get("decided") or "").strip(),
            }
        )
    return result


def reasons(root: Optional[Path] = None) -> Dict[str, str]:
    """{id: причина} — то, что нужно фильтру рекомендаций."""
    return {item["id"]: item["reason"] for item in entries(root)}


def reason(opportunity_id: str, root: Optional[Path] = None) -> str:
    return reasons(root).get(str(opportunity_id), "")
