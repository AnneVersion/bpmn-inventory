"""
Globale ankerobjecten-repository.

Waar `bpmn_defs.py` user-defined entities + auto-discovered per sessie
beheert, verzamelt `bpmn_anchors` **cross-project** de ankerobjecten:
entities die in meerdere processen/projecten verschijnen. Deze store
leeft buiten één specifieke sessie/project zodat de tool bij iedere
volgende analyse weet welke entities centraal zijn in de organisatie.

Opslag: `output/global_anchors.json`

Schema:
{
  "entities": {
    "Lidmaatschap": {
      "total_appearances": 7,
      "processes":   ["P01 Inschrijven", "P02 Wijziging", ...],
      "projects":    ["5a669f3ce3fc", "20fa74244082", ...],
      "first_seen":  "2026-04-16T18:22:30",
      "last_seen":   "2026-04-17T09:14:02",
      "is_master":   false,
      "aliases":     ["Lidmaatschap", "Ledengegevens", "Lidgegevens"]
    },
    ...
  }
}
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def anchors_path(root: Path) -> Path:
    p = root / "output" / "global_anchors.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def load(root: Path) -> dict:
    p = anchors_path(root)
    if not p.exists():
        return {"entities": {}}
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if "entities" not in data:
            data["entities"] = {}
        return data
    except (json.JSONDecodeError, OSError):
        return {"entities": {}}


def save(root: Path, data: dict) -> None:
    with anchors_path(root).open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def ingest_from_model(root: Path, erd_entities, project_id: str) -> dict:
    """Upsert elke ERD-entity in de globale repository.

    `erd_entities` = lijst Entity-objecten uit bpmn_erd.build_erd (of
    dicts uit summary.erd.entities als strings).

    Return: de bijgewerkte data.
    """
    data = load(root)
    now = datetime.now().isoformat(timespec="seconds")
    for e in erd_entities:
        # Accepteer zowel Entity-dataclass als dict (uit summary.erd.entities)
        if hasattr(e, "name"):
            name = e.name
            processes = list(e.source_processes) if isinstance(
                e.source_processes, set) else list(e.source_processes)
            aliases = list(e.aliases) if isinstance(e.aliases, set) else e.aliases
            is_master = bool(e.is_master)
        else:
            name = e.get("name", "")
            processes = e.get("source_processes", [])
            aliases = e.get("aliases", [])
            is_master = bool(e.get("is_master", False))
        if not name:
            continue

        entry = data["entities"].get(name, {
            "total_appearances": 0,
            "processes": [],
            "projects": [],
            "first_seen": now,
            "last_seen": now,
            "is_master": False,
            "aliases": [],
        })
        existing_procs = set(entry.get("processes", []))
        new_procs = set(processes) - existing_procs
        entry["processes"] = sorted(existing_procs | set(processes))
        entry["total_appearances"] = entry.get("total_appearances", 0) + len(new_procs)
        entry["projects"] = sorted(set(entry.get("projects", [])) | {project_id})
        entry["last_seen"] = now
        entry["is_master"] = entry.get("is_master", False) or is_master
        entry["aliases"] = sorted(set(entry.get("aliases", [])) | set(aliases))
        data["entities"][name] = entry

    save(root, data)
    return data


def get_anchors(root: Path, min_processes: int = 2) -> list[dict]:
    """Retourneer alle entities die in >= N processen voorkomen,
    gesorteerd op appearances desc."""
    data = load(root)
    out = []
    for name, info in data.get("entities", {}).items():
        if len(info.get("processes", [])) >= min_processes:
            out.append({
                "name": name,
                **info,
                "process_count": len(info.get("processes", [])),
                "project_count": len(info.get("projects", [])),
            })
    out.sort(key=lambda e: e["process_count"], reverse=True)
    return out


def get_all_known(root: Path) -> list[dict]:
    """Alle gesignaleerde entities (inclusief die maar 1 proces)."""
    data = load(root)
    return sorted([
        {"name": n, **info, "process_count": len(info.get("processes", []))}
        for n, info in data.get("entities", {}).items()
    ], key=lambda e: e["process_count"], reverse=True)
