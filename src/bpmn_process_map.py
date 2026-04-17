"""
Processenkaart: detecteer welke processen aan welke gerelateerd zijn
op basis van gedeelde entities en hun lifecycle.

Regel: als proces A entity X creëert of wijzigt, en proces B leest X,
dan is er een relatie A -> B met label 'via X'. Meerdere entities
tussen 2 processen worden gecombineerd in 1 label.

Output: Mermaid flowchart + lijst relaties met evidence (welke entity
waarom).
"""

from __future__ import annotations

import re
from collections import defaultdict


def _safe_id(text: str) -> str:
    """Mermaid node-id: alleen letters/cijfers/underscore."""
    s = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")[:40]
    if not s or s[0].isdigit():
        s = "P_" + s
    return s


def build_process_map(erd_entities: list) -> tuple[str, list[dict]]:
    """Bouw een Mermaid flowchart + lijst relaties uit ERD entities.

    `erd_entities` = lijst van Entity-dataclass uit bpmn_erd.build_erd.
    Elke entity heeft creators (set), updaters (set), readers (set)
    met proces-namen.

    Returns (mermaid_str, relations_list).
    """
    # Relatie-map: (A, B) -> set(entity_names), met richting A zendt -> B ontvangt
    rel: dict[tuple[str, str], dict] = defaultdict(lambda: {"entities": set()})

    for e in erd_entities:
        # e kan dataclass of dict zijn
        if hasattr(e, "creators"):
            creators = set(e.creators)
            updaters = set(e.updaters)
            readers = set(e.readers)
            name = e.name
        else:
            lc = e.get("lifecycle", {})
            creators = set(lc.get("creators", []))
            updaters = set(lc.get("updaters", []))
            readers = set(lc.get("readers", []))
            name = e.get("name", "")

        producers = creators | updaters
        for a in producers:
            for b in readers:
                if a == b:
                    continue
                rel[(a, b)]["entities"].add(name)
        # Updater downstream van creator
        for a in creators:
            for b in updaters:
                if a == b:
                    continue
                rel[(a, b)]["entities"].add(name)

    # Alle procesnamen die ergens voorkomen = nodes
    all_procs: set[str] = set()
    for e in erd_entities:
        if hasattr(e, "creators"):
            all_procs |= e.creators | e.updaters | e.readers
        else:
            lc = e.get("lifecycle", {})
            all_procs |= set(lc.get("creators", []))
            all_procs |= set(lc.get("updaters", []))
            all_procs |= set(lc.get("readers", []))

    if not all_procs:
        return "", []

    # Mermaid flowchart
    node_ids: dict[str, str] = {}
    for p in sorted(all_procs):
        node_ids[p] = _safe_id(p)

    lines = ["flowchart LR"]
    # Nodes
    for p, nid in node_ids.items():
        safe_label = p.replace('"', "'")[:60]
        lines.append(f'    {nid}["{safe_label}"]')
    # Edges
    relations: list[dict] = []
    for (a, b), info in sorted(rel.items()):
        ents = sorted(info["entities"])
        label = "via " + (", ".join(ents[:3])
                          + (f" (+{len(ents) - 3})" if len(ents) > 3 else ""))
        lines.append(f'    {node_ids[a]} -->|{label}| {node_ids[b]}')
        relations.append({
            "from": a, "to": b,
            "via_entities": ents,
            "label": label,
        })

    return "\n".join(lines), relations
