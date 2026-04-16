"""
Theoretisch onderbouwde ERD-afleiding uit een MergedModel.

De pipeline volgt de Chen / Crow's-Foot conventie:

1. **Entity-identificatie** — Unieke dataObject-namen en dataStore-namen
   worden gecanonicaliseerd (suffixes als 'gegevens' weg, synoniemen
   samengevoegd via `CANONICAL_MAP`).

2. **Attribuut-inferentie** — Per canonical entity-naam leveren we
   attribuut-suggesties uit `ATTRIBUTE_HINTS` (Nederlandse zakelijke
   woordenschat). Iedere entity krijgt een surrogate PK `id`.

3. **Cardinaliteits-inferentie** — Van taak-I/O naar relatie-cardinaliteit
   * task input(A) + output(B): A produceert B (1:N)
   * 2 outputs (A,B) op zelfde task: A en B co-existeren (N:M)
   * ankerobject + niet-anker: 1:N (anker is parent)
   * master (dataStore) + niet-master: 1:N
   * Anders: N:M

4. **Zwakke entiteit-detectie** — Entity B die alleen voorkomt in taken
   waar ook A voorkomt, wordt als zwakke entiteit van A gemarkeerd +
   krijgt een FK.

5. **Master / Anker** — dataStore -> master; naam in 2+ processen -> anker.

De output is een lijst `Entity` + lijst `Relationship` en een
Mermaid-erDiagram string die Crow's-Foot cardinaliteit gebruikt.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from merger import MergedModel


# ---------------------------------------------------------------------------
# Domein-woordenschat (uitbreidbaar)
# ---------------------------------------------------------------------------

#: Mapping van gebruikte dataObject-namen naar canonical entity-namen.
#: Zo worden 'Persoonsgegevens', 'persoonsgegeven' en 'Persoon' allen de
#: entity 'Persoon'.
CANONICAL_MAP: dict[str, str] = {
    "persoon": "Persoon",
    "persoonsgegeven": "Persoon",
    "persoonsgegevens": "Persoon",
    "lid": "Lid",
    "leden": "Lid",
    "lidgegeven": "Lid",
    "lidgegevens": "Lid",
    "ledengegevens": "Lid",
    "organisatie": "Organisatie",
    "organisatiegegeven": "Organisatie",
    "organisatiegegevens": "Organisatie",
    "bedrijfsgegeven": "Organisatie",
    "bedrijfsgegevens": "Organisatie",
    "bedrijf": "Organisatie",
    "werkgever": "Werkgever",          # subtype van Organisatie (TODO: ISA)
    "lidmaatschap": "Lidmaatschap",
    "lidmaatschapgegevens": "Lidmaatschap",
    "contributie": "Contributie",
    "contributiegegevens": "Contributie",
    "iban-machtiging": "Machtiging",
    "ibanmachtiging": "Machtiging",
    "iban machtiging": "Machtiging",
    "machtiging": "Machtiging",
    "incasso": "Incasso",
    "aandrager": "Persoon",
    "contract": "Contract",
    "cao": "CAO",
    "factuur": "Factuur",
    "betaling": "Betaling",
    "jaaropgave": "Jaaropgave",
    "bestuurder": "Bestuurder",
    "dossier": "Dossier",
    "aanvraag": "Aanvraag",
    "melding": "Melding",
    "adres": "Adres",
}

#: Per canonical entity-naam (lowercase match) een lijstje attribuut-
#: suggesties van de vorm 'naam:type[:pk|fk|required]'. Iedere entity
#: krijgt daarnaast een surrogate PK `id:string:pk`.
ATTRIBUTE_HINTS: dict[str, list[str]] = {
    "persoon":      ["naam:string:required", "bsn:string:uniek",
                     "geboortedatum:date", "geslacht:enum", "emailadres:string"],
    "adres":        ["straat:string:required", "huisnummer:string:required",
                     "postcode:string", "woonplaats:string"],
    "lid":          ["lidnummer:string:uniek", "naam:string:required",
                     "emailadres:string", "status:enum"],
    "lidmaatschap": ["lidnummer:string", "startdatum:date:required",
                     "einddatum:date", "tariefgroep:enum", "status:enum"],
    "machtiging":   ["machtigingskenmerk:string:uniek",
                     "iban:string:required", "tenaamstelling:string",
                     "datum_afgifte:date", "einddatum:date"],
    "incasso":      ["bedrag:number:required", "datum:date:required",
                     "status:enum", "retourcode:string"],
    "contract":     ["contractnummer:string:uniek", "startdatum:date",
                     "einddatum:date", "status:enum"],
    "factuur":      ["factuurnummer:string:uniek", "datum:date",
                     "bedrag:number", "btw:number", "status:enum"],
    "organisatie":  ["kvknummer:string:uniek", "naam:string:required",
                     "rechtsvorm:enum", "hoofdvestiging:string"],
    "werkgever":    ["kvknummer:string:uniek", "naam:string:required",
                     "cao_code:string", "sector:enum"],
    "bestuurder":   ["naam:string:required", "functie:string",
                     "benoemingsdatum:date"],
    "cao":          ["cao_code:string:uniek", "naam:string",
                     "startdatum:date", "einddatum:date"],
    "contributie":  ["bedrag:number:required", "periode:enum",
                     "tariefgroep:enum"],
    "jaaropgave":   ["jaar:number:required", "totaalbedrag:number",
                     "datum:date"],
    "dossier":      ["dossiernummer:string:uniek", "status:enum",
                     "aangemaakt_op:date"],
    "aanvraag":     ["aanvraagnummer:string:uniek", "type:enum",
                     "datum:date", "status:enum"],
    "melding":      ["meldingsnummer:string:uniek", "type:enum",
                     "datum:date", "status:enum"],
    "betaling":     ["bedrag:number", "datum:date", "iban:string"],
}


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class Attribute:
    name: str
    type: str = "string"
    required: bool = False
    unique: bool = False
    is_pk: bool = False
    is_fk: bool = False
    fk_to: str = ""
    derived_from: str = ""


@dataclass
class Entity:
    name: str                                 # canonical, bv. 'Lidmaatschap'
    id: str                                   # SCREAMING_SNAKE voor mermaid
    attributes: list[Attribute] = field(default_factory=list)
    source_processes: set[str] = field(default_factory=set)
    source_bpmn_ids: list[str] = field(default_factory=list)
    aliases: set[str] = field(default_factory=set)   # originele namen
    is_anchor: bool = False
    is_master: bool = False
    is_weak: bool = False
    parent: str = ""


@dataclass
class Relationship:
    left: str                                 # entity.name
    right: str
    left_card: str                            # "1" | "0..1" | "0..N" | "1..N"
    right_card: str
    label: str                                # verb
    is_identifying: bool = False
    evidence: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STRIP_SUFFIXES = ["gegevens", "gegeven", "informatie", "info"]


def canonicalize(raw: str) -> str:
    """Zet een dataObject-naam om naar een canonical entity-naam.

    Regels (in volgorde):
    1. Lookup in `CANONICAL_MAP` op lowercase.
    2. Strip een '*gegevens'-achtige suffix en probeer opnieuw.
    3. Val terug op Title-cased eerste 'woord' (alleen letters).
    """
    if not raw:
        return ""
    low = re.sub(r"\s+", " ", raw.strip().lower())
    low = low.replace("-", " ").replace("_", " ").strip()

    if low in CANONICAL_MAP:
        return CANONICAL_MAP[low]
    # Probeer met streepjes/spaties samengevoegd
    compact = low.replace(" ", "")
    if compact in CANONICAL_MAP:
        return CANONICAL_MAP[compact]

    # Strip suffix
    for suf in _STRIP_SUFFIXES:
        if low.endswith(" " + suf):
            base = low[: -(len(suf) + 1)].strip()
            if base in CANONICAL_MAP:
                return CANONICAL_MAP[base]
            if base:
                return base.capitalize()
        if low.endswith(suf) and len(low) > len(suf):
            base = low[: -len(suf)].rstrip(" -_")
            if base in CANONICAL_MAP:
                return CANONICAL_MAP[base]
            if base:
                return base.capitalize()

    # Fallback: eerste woord
    words = re.findall(r"[A-Za-z]+", raw)
    if words:
        first = words[0].lower()
        if first in CANONICAL_MAP:
            return CANONICAL_MAP[first]
        return words[0][0].upper() + words[0][1:].lower()
    return raw


def _snake(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper()[:40]


def _parse_attr_spec(spec: str) -> Attribute:
    """'naam:string:required' -> Attribute."""
    parts = spec.split(":")
    name = parts[0]
    typ = parts[1] if len(parts) > 1 else "string"
    flags = parts[2:] if len(parts) > 2 else []
    return Attribute(
        name=name,
        type=typ,
        required="required" in flags,
        unique="uniek" in flags or "unique" in flags,
        derived_from="ATTRIBUTE_HINTS",
    )


def _infer_attributes(canonical_name: str) -> list[Attribute]:
    key = canonical_name.lower()
    if key in ATTRIBUTE_HINTS:
        return [_parse_attr_spec(s) for s in ATTRIBUTE_HINTS[key]]
    # Probeer deel-match (bv. 'lidmaatschapsadres' bevat 'adres')
    for hint_key, specs in ATTRIBUTE_HINTS.items():
        if hint_key in key and len(hint_key) > 3:
            return [_parse_attr_spec(s) for s in specs]
    return []


# ---------------------------------------------------------------------------
# Hoofd-algoritme
# ---------------------------------------------------------------------------

def build_erd(model: "MergedModel"
              ) -> tuple[list[Entity], list[Relationship]]:
    """Bouw lijst van entities + relationships volgens ERD-theorie."""
    entities: dict[str, Entity] = {}        # canonical -> Entity

    def _get_or_create(canonical: str, original: str) -> Entity:
        if canonical not in entities:
            attrs = [Attribute(name="id", type="string", required=True,
                               unique=True, is_pk=True,
                               derived_from="surrogate key")]
            attrs += _infer_attributes(canonical)
            entities[canonical] = Entity(
                name=canonical,
                id=_snake(canonical),
                attributes=attrs,
            )
        entities[canonical].aliases.add(original)
        return entities[canonical]

    # --- Step 1: entities uit dataObjects + dataStores
    for parsed in model.bpmns:
        for d in parsed.data_objects:
            if not d.name or d.name.startswith("(naamloos"):
                continue
            canonical = canonicalize(d.name)
            if not canonical:
                continue
            e = _get_or_create(canonical, d.name)
            e.source_processes.add(parsed.process_name or parsed.source_file)
            e.source_bpmn_ids.append(d.id)

        for ds in parsed.data_stores:
            if not ds.name or ds.name.startswith("(naamloos"):
                continue
            canonical = canonicalize(ds.name)
            if not canonical:
                continue
            e = _get_or_create(canonical, ds.name)
            e.is_master = True
            e.source_processes.add(parsed.process_name or parsed.source_file)

    if not entities:
        return [], []

    # --- Step 2: mark anchors (entity verschijnt in 2+ processen)
    for e in entities.values():
        if len(e.source_processes) >= 2:
            e.is_anchor = True

    # --- Step 3: task I/O verzamelen per processing
    task_io: list[tuple[str, str, set[str], set[str]]] = []  # (bestand, taak, ins, outs)
    for parsed in model.bpmns:
        dobj_by_id = {d.id: d for d in parsed.data_objects}
        task_ids = {t.id: t for t in parsed.tasks}
        ins_by_task: dict[str, set[str]] = defaultdict(set)
        outs_by_task: dict[str, set[str]] = defaultdict(set)
        for a in parsed.data_associations:
            src = a.attributes.get("source", "")
            tgt = a.attributes.get("target", "")
            if a.subtype == "dataInputAssociation":
                # source = dataObject, target = task
                d = dobj_by_id.get(src)
                if d and tgt in task_ids and d.name:
                    c = canonicalize(d.name)
                    if c:
                        ins_by_task[tgt].add(c)
            else:
                # output: source = task (meestal), target = dataObject
                d = dobj_by_id.get(tgt) or dobj_by_id.get(src)
                task_id = src if src in task_ids else tgt
                if d and task_id in task_ids and d.name:
                    c = canonicalize(d.name)
                    if c:
                        outs_by_task[task_id].add(c)

        for t in parsed.tasks:
            ins = ins_by_task.get(t.id, set())
            outs = outs_by_task.get(t.id, set())
            if ins or outs:
                task_io.append((parsed.source_file, t.name, ins, outs))

    # --- Step 4: relationship evidence verzamelen
    # Alle paren (A,B) die in dezelfde task voorkomen, met direction.
    pair_evidence: dict[tuple[str, str], list[dict]] = defaultdict(list)

    def _pair_key(a: str, b: str) -> tuple[str, str]:
        return (a, b) if a < b else (b, a)

    for src_file, task_name, ins, outs in task_io:
        all_objs = sorted(ins | outs)
        for i in range(len(all_objs)):
            for j in range(i + 1, len(all_objs)):
                a, b = all_objs[i], all_objs[j]
                direction = ""
                # Als a alleen input en b alleen output -> a "produceert" b
                if a in ins and a not in outs and b in outs and b not in ins:
                    direction = "a_produces_b"
                elif b in ins and b not in outs and a in outs and a not in ins:
                    direction = "b_produces_a"
                elif a in ins and b in ins:
                    direction = "both_input"
                elif a in outs and b in outs:
                    direction = "both_output"
                pair_evidence[_pair_key(a, b)].append({
                    "task": task_name, "file": src_file,
                    "direction": direction,
                })

    # --- Step 5: relationships bouwen met cardinaliteit-heuristiek
    relationships: list[Relationship] = []
    for (a, b), evid in pair_evidence.items():
        if a not in entities or b not in entities:
            continue
        ea, eb = entities[a], entities[b]

        # Bepaal "produceert"-richting als consistent
        produces_direction = None
        directions = {e["direction"] for e in evid}
        if directions == {"a_produces_b"}:
            produces_direction = "a->b"
        elif directions == {"b_produces_a"}:
            produces_direction = "b->a"

        # Cardinaliteit + label
        if ea.is_master and not eb.is_master:
            left_c, right_c, label = "1", "0..N", f"levert {eb.name}"
        elif eb.is_master and not ea.is_master:
            left_c, right_c, label = "0..N", "1", f"hoort bij {eb.name}"
        elif produces_direction == "a->b":
            left_c, right_c, label = "1", "0..N", f"produceert {eb.name}"
        elif produces_direction == "b->a":
            left_c, right_c, label = "0..N", "1", f"geproduceerd door {eb.name}"
        elif ea.is_anchor and not eb.is_anchor:
            left_c, right_c, label = "1", "0..N", f"heeft {eb.name}"
        elif eb.is_anchor and not ea.is_anchor:
            left_c, right_c, label = "0..N", "1", f"hoort bij {eb.name}"
        else:
            left_c, right_c, label = "0..N", "0..N", "gerelateerd"

        ev_tasks = sorted({e["task"] for e in evid})[:3]
        relationships.append(Relationship(
            left=ea.name, right=eb.name,
            left_card=left_c, right_card=right_c,
            label=label, evidence=ev_tasks,
        ))

    # --- Step 6: zwakke entiteiten detecteren
    # B is zwak van A als iedere taak waarin B voorkomt ook A bevat,
    # en A is een anker (stabiele parent). Voeg FK toe.
    for b_name, b_ent in entities.items():
        if b_ent.is_master or b_ent.is_anchor:
            continue
        b_tasks = [(sf, tn, ins, outs) for sf, tn, ins, outs in task_io
                   if b_name in ins or b_name in outs]
        if not b_tasks:
            continue
        for a_name, a_ent in entities.items():
            if a_name == b_name or not a_ent.is_anchor:
                continue
            if all(a_name in ins or a_name in outs for _, _, ins, outs in b_tasks):
                b_ent.is_weak = True
                b_ent.parent = a_name
                # FK toevoegen (als niet al aanwezig)
                fk_name = f"{_snake(a_name).lower()}_id"
                if not any(a.name == fk_name for a in b_ent.attributes):
                    b_ent.attributes.append(Attribute(
                        name=fk_name, type="string", required=True,
                        is_fk=True, fk_to=a_name,
                        derived_from=f"Zwakke entiteit van {a_name}",
                    ))
                # Zet identifying flag op bestaande relationship
                for rel in relationships:
                    if {rel.left, rel.right} == {a_name, b_name}:
                        rel.is_identifying = True
                break

    # --- Step 7: sorteer voor stabiele output
    entities_list = sorted(
        entities.values(),
        key=lambda e: (not e.is_master, not e.is_anchor, e.name)
    )
    relationships.sort(key=lambda r: (r.left, r.right))
    return entities_list, relationships


# ---------------------------------------------------------------------------
# Mermaid-export met Crow's-Foot cardinaliteit
# ---------------------------------------------------------------------------

# Mermaid erDiagram cardinality notation:
#   ||        exactly one
#   o|  / |o  zero or one
#   }|  / |{  one or many
#   }o  / o{  zero or many
_CARD_LEFT = {
    "1":    "||",
    "0..1": "o|",
    "1..N": "}|",
    "0..N": "}o",
    "N":    "}o",
}
_CARD_RIGHT = {
    "1":    "||",
    "0..1": "|o",
    "1..N": "|{",
    "0..N": "o{",
    "N":    "o{",
}


def to_mermaid(entities: list[Entity],
               relationships: list[Relationship]) -> str:
    """Genereer een Mermaid erDiagram string."""
    if not entities:
        return ""
    lines: list[str] = ["erDiagram"]
    # Entiteiten (incl. attributen)
    for e in entities:
        # Mermaid kan in v10+ aliases: ID["Display"]
        display = e.name
        if e.is_master:
            display += " [MASTER]"
        elif e.is_weak:
            display += f" [ZWAK van {e.parent}]"
        elif e.is_anchor:
            display += " [ANKER]"
        lines.append(f'    {e.id}["{display}"] {{')
        for a in e.attributes:
            flag = ""
            if a.is_pk: flag = " PK"
            elif a.is_fk: flag = " FK"
            comment = ""
            marks = []
            if a.required: marks.append("required")
            if a.unique:   marks.append("uniek")
            if marks: comment = ' "' + " / ".join(marks) + '"'
            # Mermaid type moet een woord zijn; vervang spaces
            typ = re.sub(r"[^A-Za-z0-9_]", "_", a.type) or "string"
            lines.append(f"        {typ} {a.name}{flag}{comment}")
        lines.append("    }")

    # Relaties
    for r in relationships:
        left = next((e for e in entities if e.name == r.left), None)
        right = next((e for e in entities if e.name == r.right), None)
        if not left or not right:
            continue
        lc = _CARD_LEFT.get(r.left_card, "}o")
        rc = _CARD_RIGHT.get(r.right_card, "o{")
        conn = "--" if r.is_identifying else ".."
        label = r.label.replace('"', "'")
        lines.append(f'    {left.id} {lc}{conn}{rc} {right.id} : "{label}"')

    return "\n".join(lines)


def summarize(entities: list[Entity],
              relationships: list[Relationship]) -> dict:
    """Compacte JSON-summary van het ERD (voor inline rendering)."""
    return {
        "entities": [{
            "name": e.name,
            "id": e.id,
            "is_master": e.is_master,
            "is_anchor": e.is_anchor,
            "is_weak": e.is_weak,
            "parent": e.parent,
            "aliases": sorted(e.aliases),
            "source_processes": sorted(e.source_processes),
            "attributes": [{
                "name": a.name, "type": a.type,
                "pk": a.is_pk, "fk": a.is_fk, "fk_to": a.fk_to,
                "required": a.required, "unique": a.unique,
                "derived_from": a.derived_from,
            } for a in e.attributes],
        } for e in entities],
        "relationships": [{
            "left": r.left, "right": r.right,
            "left_card": r.left_card, "right_card": r.right_card,
            "label": r.label,
            "is_identifying": r.is_identifying,
            "evidence": r.evidence,
        } for r in relationships],
    }
