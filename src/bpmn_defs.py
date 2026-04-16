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


def auto_discover(root: Path, model, session_id: str = "") -> dict:
    """Auto-upsert ontdekte entities + actoren uit een MergedModel.

    - Entities: unieke canonical dataObject-namen landen in
      `objects.<naam>` met lege attributenlijst als ze nog niet
      bestaan. Bestaande user-defined objecten worden NIET
      overschreven; ze krijgen alleen extra metadata over waar ze
      gezien zijn.
    - Actoren: unieke (lane + extern) actor-namen landen in
      `actors.<naam>` met metadata.

    Schema wordt uitgebreid met:
      "objects": {
        "Organisatie": [...attrs] OR metadata-wrapper
      },
      "actors": {
        "KCC": { "type": "intern", "seen_in": [session_ids] }
      }

    Om backwards-compat te houden: `objects.<naam>` blijft een lijst
    van attribute-dicts (user-edited). De discovery metadata gaat in
    een parallele key `discovered_objects` met dezelfde namen.
    """
    data = load(root)
    data.setdefault("objects", {})
    data.setdefault("actors", {})
    data.setdefault("discovered_objects", {})

    # Canonical entity-namen uit het model
    try:
        from bpmn_erd import canonicalize
    except Exception:
        canonicalize = lambda x: x  # noqa: E731

    seen_entities: dict[str, dict] = {}
    for parsed in getattr(model, "bpmns", []):
        proc_name = parsed.process_name or parsed.source_file
        for d in getattr(parsed, "data_objects", []):
            if not d.name or d.name.startswith("(naamloos"):
                continue
            canonical = canonicalize(d.name)
            if not canonical:
                continue
            entry = seen_entities.setdefault(canonical, {
                "aliases": set(),
                "processes": set(),
                "source_files": set(),
            })
            entry["aliases"].add(d.name)
            entry["processes"].add(proc_name)
            entry["source_files"].add(parsed.source_file)

    for name, meta in seen_entities.items():
        meta_dict = {
            "aliases": sorted(meta["aliases"]),
            "processes": sorted(meta["processes"]),
            "source_files": sorted(meta["source_files"]),
            "discovered": True,
        }
        existing = data["discovered_objects"].get(name, {})
        # Merge: als al ontdekt, voeg aliases/processen samen
        for k in ("aliases", "processes", "source_files"):
            merged = sorted(set(meta_dict[k]) | set(existing.get(k, [])))
            meta_dict[k] = merged
        if session_id:
            seen_sessions = set(existing.get("sessions", []))
            seen_sessions.add(session_id)
            meta_dict["sessions"] = sorted(seen_sessions)
        data["discovered_objects"][name] = meta_dict

        # Zorg dat er een stub in objects staat, zodat hij verschijnt in
        # de /definities sidebar en de gebruiker attributen kan toevoegen.
        if name not in data["objects"]:
            data["objects"][name] = []

    # Actoren
    seen_actors: dict[str, dict] = {}
    for parsed in getattr(model, "bpmns", []):
        for lane in getattr(parsed, "lanes", []):
            if not lane.name or lane.name.startswith("(naamloze"):
                continue
            a = seen_actors.setdefault(lane.name, {
                "type": "intern", "processes": set(), "source_files": set(),
            })
            a["processes"].add(parsed.process_name or parsed.source_file)
            a["source_files"].add(parsed.source_file)
        for p in getattr(parsed, "participants", []):
            if p.attributes.get("processRef"):
                continue  # eigen org, geen externe actor
            if not p.name:
                continue
            a = seen_actors.setdefault(p.name, {
                "type": "extern", "processes": set(), "source_files": set(),
            })
            a["processes"].add(parsed.process_name or parsed.source_file)
            a["source_files"].add(parsed.source_file)

    for name, meta in seen_actors.items():
        entry = data["actors"].get(name, {
            "type": meta["type"], "processes": [], "source_files": []
        })
        entry["type"] = meta["type"]
        entry["processes"] = sorted(set(entry.get("processes", [])) | meta["processes"])
        entry["source_files"] = sorted(set(entry.get("source_files", [])) | meta["source_files"])
        if session_id:
            seen = set(entry.get("sessions", []))
            seen.add(session_id)
            entry["sessions"] = sorted(seen)
        data["actors"][name] = entry

    save(root, data)
    return data
