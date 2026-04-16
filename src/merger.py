"""
Merge multiple ParsedBpmn results, classify elements into inventory
types, and identify "anchor" data objects that appear across processes.

Classification logic (used to fill the data-inventarisatie template
and surfaced as a tooltip in the draw.io export):

    BPMN element                            -> Inventory category
    -------------------------------------------------------------
    participant (pool)                       -> Actor (extern)
    lane                                     -> Actor (intern, rol)
    dataObject(Reference)                    -> Entiteit
    dataStore(Reference)                     -> Bron / master
    task (any subtype)                       -> Processtap
    gateway                                  -> Procesattribuut (routering)
    intermediateThrowEvent + MessageEvent    -> Procesevent (data-uitwisseling)
    other event                              -> Procesevent
    textAnnotation                           -> Opmerking / business rule
    messageFlow                              -> Communicatie tussen actoren

Each classification carries a justification string explaining WHY,
so reviewers can audit the inventory.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from bpmn_parser import BpmnElement, ParsedBpmn


# Crude PII / classification heuristics, based on the FNV legenda
# (Openbaar / Intern / Vertrouwelijk / Bijzonder persoonsgegeven).
SPECIAL_CATEGORY_KEYWORDS = [
    "vakbond", "lidmaatschap", "gezondheid", "etnisch", "religie",
    "politiek", "biometr", "seksueel",
]
CONFIDENTIAL_KEYWORDS = [
    "iban", "salaris", "geboorte", "bsn", "machtiging", "incasso",
    "betaal", "loon", "bedrag", "aandrager", "bewijs",
]


def classify_sensitivity(name: str, attribute: str = "") -> tuple[str, str]:
    """Return (classification, justification)."""
    text = f"{name} {attribute}".lower()
    for kw in SPECIAL_CATEGORY_KEYWORDS:
        if kw in text:
            return ("Bijzonder persoonsgegeven",
                    f"Naam/attribuut bevat '{kw}' — valt onder AVG art. 9.")
    for kw in CONFIDENTIAL_KEYWORDS:
        if kw in text:
            return ("Vertrouwelijk",
                    f"Naam/attribuut bevat '{kw}' — financieel of gevoelig.")
    return ("Intern", "Geen PII-kenmerken in naam; standaard 'Intern'.")


@dataclass
class InventoryRow:
    """One row in the data-inventarisatie template."""
    process: str           # which BPMN file / process
    process_step: str      # task name
    step_id: str           # short id
    data_object: str
    attribute: str
    required: str          # 'Ja' / 'Nee' / ''
    purpose: str           # doelbinding
    classification: str    # Openbaar / Intern / Vertrouwelijk / Bijzonder
    authorization: str
    retention: str
    source: str            # master, e.g. Salesforce, BPMN-model
    remarks: str
    # extras (used for tooltips, not in xlsx)
    bpmn_id: str = ""
    bpmn_kind: str = ""
    classification_reason: str = ""
    extraction_reason: str = ""


@dataclass
class MergedModel:
    """Aggregate of all parsed BPMNs."""
    bpmns: list[ParsedBpmn] = field(default_factory=list)
    actors: list[BpmnElement] = field(default_factory=list)
    data_object_index: dict[str, list[tuple[str, BpmnElement]]] = field(
        default_factory=lambda: defaultdict(list)
    )  # name (lower) -> [(source_file, element), ...]
    inventory: list[InventoryRow] = field(default_factory=list)

    def anchor_objects(self) -> list[str]:
        """Data objects appearing in more than one BPMN file."""
        return sorted({
            elements[0][1].name
            for name, elements in self.data_object_index.items()
            if len({sf for sf, _ in elements}) >= 2
        })


# --- Step-id derivation ----------------------------------------------------

def _derive_step_id(task: BpmnElement, counter: dict[str, int]) -> str:
    """Generate a stable A1, A2, ... id per process file."""
    counter[task.process_id] = counter.get(task.process_id, 0) + 1
    return f"A{counter[task.process_id]}"


def _task_step_index(parsed: ParsedBpmn) -> dict[str, str]:
    """Map task.id -> 'A1', 'A2' (per file)."""
    counter: dict[str, int] = {}
    return {t.id: _derive_step_id(t, counter) for t in parsed.tasks}


# --- Linking data objects to tasks via associations ------------------------

def _data_obj_links(parsed: ParsedBpmn
                    ) -> dict[str, list[tuple[str, BpmnElement]]]:
    """task_id -> [('input'|'output', dataObjectElement), ...]."""
    by_id = {d.id: d for d in parsed.data_objects}
    links: dict[str, list[tuple[str, BpmnElement]]] = defaultdict(list)
    for assoc in parsed.data_associations:
        src = assoc.attributes.get("source", "")
        tgt = assoc.attributes.get("target", "")
        if assoc.subtype == "dataInputAssociation":
            # source is the dataObjectReference, target is the task
            data_el = by_id.get(src)
            task_id = tgt
            direction = "input"
        else:
            data_el = by_id.get(tgt) or by_id.get(src)
            task_id = src
            direction = "output"
        if data_el and task_id:
            links[task_id].append((direction, data_el))
    return links


# --- Actor extraction ------------------------------------------------------

def extract_actors(bpmns: list[ParsedBpmn]) -> list[BpmnElement]:
    """Deduplicate actors across all files (lanes + external pools).

    Pools whose `processRef` is empty are external actors (e.g. the
    customer / member / employer). Pools with a processRef are the
    organisation that owns the process — their lanes are the real
    internal actors.
    """
    seen: dict[str, BpmnElement] = {}

    for parsed in bpmns:
        # Internal actors = lanes
        for lane in parsed.lanes:
            key = lane.name.strip().lower()
            if key not in seen:
                actor = BpmnElement(
                    id=f"actor_{len(seen)}",
                    name=lane.name,
                    kind="actor",
                    subtype="intern",
                    evidence={
                        **lane.evidence,
                        "appears_in": [parsed.source_file],
                        "actor_type": "Intern (rol/lane)",
                        "classification_reason":
                            "Element is een <bpmn:lane> binnen het proces — "
                            "vertegenwoordigt een interne rol/afdeling.",
                    },
                )
                seen[key] = actor
            else:
                seen[key].evidence["appears_in"].append(parsed.source_file)

        # External actors = pools without processRef
        for pool in parsed.participants:
            if pool.attributes.get("processRef"):
                continue  # this is the owning organisation, not an external actor
            key = pool.name.strip().lower()
            if key not in seen:
                actor = BpmnElement(
                    id=f"actor_{len(seen)}",
                    name=pool.name,
                    kind="actor",
                    subtype="extern",
                    evidence={
                        **pool.evidence,
                        "appears_in": [parsed.source_file],
                        "actor_type": "Extern (pool zonder processRef)",
                        "classification_reason":
                            "Element is een <bpmn:participant> zónder "
                            "processRef — externe partij die alleen via "
                            "messageFlows met het proces interacteert.",
                    },
                )
                seen[key] = actor
            else:
                seen[key].evidence["appears_in"].append(parsed.source_file)

    return sorted(seen.values(), key=lambda a: (a.subtype, a.name))


# --- Inventory rows --------------------------------------------------------

def _retention_for(classification: str) -> str:
    """Reuse the retention conventions from the example sheet."""
    if classification == "Bijzonder persoonsgegeven":
        return "Duur lidmaatschap + 7 jaar"
    if classification == "Vertrouwelijk":
        return "Duur lidmaatschap + 7 jaar"
    return "Duur lidmaatschap + 2 jaar"


def _purpose_for(task_name: str, data_obj_name: str) -> str:
    """Naïeve doelbinding op basis van taak + object."""
    return f"Ondersteunt taak '{task_name}' met data-object '{data_obj_name}'"


def build_inventory(parsed: ParsedBpmn) -> list[InventoryRow]:
    """Generate inventory rows for one parsed BPMN."""
    rows: list[InventoryRow] = []
    step_ids = _task_step_index(parsed)
    obj_links = _data_obj_links(parsed)

    # Lookup tables
    annotations_by_assoc_target = defaultdict(list)
    for a in parsed.associations:
        src = a.attributes.get("source", "")
        tgt = a.attributes.get("target", "")
        # textAnnotation is usually the *target*; the task is the source
        for annot in parsed.annotations:
            if annot.id == tgt:
                annotations_by_assoc_target[src].append(annot.attributes.get("text", ""))

    for task in parsed.tasks:
        step = step_ids[task.id]
        # Linked data objects via dataInput/Output associations
        linked = obj_links.get(task.id, [])
        # Annotations connected to this task
        notes = " | ".join(annotations_by_assoc_target.get(task.id, []))

        if linked:
            for direction, data_el in linked:
                cls, cls_reason = classify_sensitivity(data_el.name)
                rows.append(InventoryRow(
                    process=parsed.process_name,
                    process_step=task.name,
                    step_id=step,
                    data_object=data_el.name,
                    attribute=f"({direction})",
                    required="Ja",
                    purpose=_purpose_for(task.name, data_el.name),
                    classification=cls,
                    authorization="(nader te bepalen)",
                    retention=_retention_for(cls),
                    source="BPMN dataObject",
                    remarks=notes,
                    bpmn_id=data_el.id,
                    bpmn_kind=data_el.subtype,
                    classification_reason=cls_reason,
                    extraction_reason=(
                        f"Gevonden via <bpmn:{direction}DataAssociation> "
                        f"tussen task {task.id} en dataObject {data_el.id}."
                    ),
                ))
        else:
            # Task without explicit data link — register the step itself
            rows.append(InventoryRow(
                process=parsed.process_name,
                process_step=task.name,
                step_id=step,
                data_object="(geen expliciet dataObject in BPMN)",
                attribute="—",
                required="",
                purpose="",
                classification="Intern",
                authorization="(nader te bepalen)",
                retention="",
                source="BPMN-model",
                remarks=notes or "★ Geen dataInput/Output association — "
                                  "object ontbreekt in BPMN of moet alsnog "
                                  "afgeleid worden.",
                bpmn_id=task.id,
                bpmn_kind=task.subtype,
                classification_reason="Standaard 'Intern' — geen PII-signalen.",
                extraction_reason=(
                    f"Task <bpmn:{task.subtype}> zonder data-association. "
                    f"Lane: {task.lane_id or '(geen lane)'}."
                ),
            ))

    # Standalone data objects (not attached to a task)
    referenced = {d.id for direction, lst in obj_links.items() for direction, d in lst}
    for data_el in parsed.data_objects:
        if data_el.id in referenced:
            continue
        cls, cls_reason = classify_sensitivity(data_el.name)
        rows.append(InventoryRow(
            process=parsed.process_name,
            process_step="(geen processtap gekoppeld)",
            step_id="-",
            data_object=data_el.name,
            attribute="(losstaand)",
            required="",
            purpose="",
            classification=cls,
            authorization="(nader te bepalen)",
            retention=_retention_for(cls),
            source="BPMN dataObject",
            remarks="Object niet via dataAssociation aan een taak gekoppeld.",
            bpmn_id=data_el.id,
            bpmn_kind=data_el.subtype,
            classification_reason=cls_reason,
            extraction_reason=(
                f"Element <bpmn:{data_el.subtype}> zonder inkomende "
                f"data-association in dit proces."
            ),
        ))

    # Gateways as process attributes (matches your manual ★ corrections)
    for gw in parsed.gateways:
        rows.append(InventoryRow(
            process=parsed.process_name,
            process_step=f"Gateway {gw.name}",
            step_id=gw.short_id(),
            data_object=f"Gateway {gw.name or gw.short_id()}",
            attribute="conditionExpression / default flow",
            required="Ja",
            purpose=f"Routering: {gw.subtype}",
            classification="Intern",
            authorization="Procesontwerper / Beheer",
            retention="N.v.t. (procesattribuut)",
            source="BPMN-model",
            remarks=f"Gateway-type {gw.subtype}; default='{gw.attributes.get('default','')}'",
            bpmn_id=gw.id,
            bpmn_kind=gw.subtype,
            classification_reason="Procesattribuut, geen persoonsgegeven.",
            extraction_reason=f"XML-tag <bpmn:{gw.subtype}>.",
        ))

    return rows


def merge(bpmns: list[ParsedBpmn]) -> MergedModel:
    """Aggregate everything into one MergedModel."""
    model = MergedModel(bpmns=bpmns)
    model.actors = extract_actors(bpmns)

    # Index data objects across files
    for parsed in bpmns:
        for d in parsed.data_objects:
            if d.name and not d.name.startswith("(naamloos"):
                model.data_object_index[d.name.lower()].append(
                    (parsed.source_file, d)
                )
        rows = build_inventory(parsed)
        model.inventory.extend(rows)

    return model
