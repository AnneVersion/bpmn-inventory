"""
User-dictionary voor objecten + attributen.

Opgeslagen in `output/definitions.json` (globaal, gedeeld over alle
sessies). De dictionary heeft voorrang boven de ingebouwde
`bpmn_erd.ATTRIBUTE_HINTS` bij R101-suggesties.

Schema:
{
  "objects": {
    "Organisatie": [
      {"name": "kvknummer", "type": "string", "required": true, "unique": true},
      {"name": "naam", "type": "string", "required": true},
      ...
    ],
    ...
  }
}
"""

from __future__ import annotations

import json
from pathlib import Path


def defs_path(root: Path) -> Path:
    return root / "output" / "definitions.json"


def load(root: Path) -> dict:
    p = defs_path(root)
    if not p.exists():
        return {"objects": {}}
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if "objects" not in data or not isinstance(data["objects"], dict):
            data["objects"] = {}
        return data
    except (json.JSONDecodeError, OSError):
        return {"objects": {}}


def save(root: Path, data: dict) -> None:
    p = defs_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def upsert_object(root: Path, name: str, attributes: list[dict]) -> dict:
    data = load(root)
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("Object-naam is leeg")
    norm = []
    for a in attributes:
        if isinstance(a, str):
            parts = a.split(":")
            norm.append({
                "name": parts[0].strip(),
                "type": (parts[1] if len(parts) > 1 else "string").strip(),
                "required": "required" in parts[2:] if len(parts) > 2 else False,
                "unique":   "uniek"    in parts[2:] if len(parts) > 2 else False,
            })
        elif isinstance(a, dict):
            nm = (a.get("name") or "").strip()
            if not nm:
                continue
            norm.append({
                "name": nm,
                "type": (a.get("type") or "string").strip(),
                "required": bool(a.get("required", False)),
                "unique": bool(a.get("unique", False)),
            })
    data["objects"][clean_name] = norm
    save(root, data)
    return data


def delete_object(root: Path, name: str) -> dict:
    data = load(root)
    data["objects"].pop(name, None)
    save(root, data)
    return data
