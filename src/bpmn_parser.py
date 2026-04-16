"""
BPMN 2.0 parser.

Reads a single .bpmn file (XML in the OMG BPMN 2.0 namespace) and
extracts the elements relevant to a data-inventory: pools/lanes
(actors), tasks (process steps), data objects, data stores, gateways,
events, text annotations, sequence flows, message flows, and
associations.

Each extracted element carries an `evidence` dict explaining HOW it
was identified (XML tag, attributes used, etc.). That evidence is
later surfaced in draw.io tooltips so reviewers can see the reasoning.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# BPMN 2.0 namespaces
NS = {
    "bpmn": "http://www.omg.org/spec/BPMN/20100524/MODEL",
    "bpmndi": "http://www.omg.org/spec/BPMN/20100524/DI",
    "dc": "http://www.omg.org/spec/DD/20100524/DC",
    "di": "http://www.omg.org/spec/DD/20100524/DI",
}


@dataclass
class BpmnElement:
    """Generic container for any BPMN element extracted from XML."""

    id: str
    name: str
    kind: str                # 'task' | 'dataObject' | 'lane' | ...
    subtype: str = ""        # e.g. 'userTask', 'exclusiveGateway'
    process_id: str = ""
    lane_id: str = ""
    attributes: dict = field(default_factory=dict)
    evidence: dict = field(default_factory=dict)  # how we identified it

    def short_id(self) -> str:
        """Last part of the BPMN id, useful in labels."""
        return self.id.split("_")[-1] if "_" in self.id else self.id


@dataclass
class ParsedBpmn:
    """Everything we extracted from one .bpmn file."""

    source_file: str
    process_name: str = ""
    participants: list[BpmnElement] = field(default_factory=list)   # pools
    lanes: list[BpmnElement] = field(default_factory=list)          # actors
    tasks: list[BpmnElement] = field(default_factory=list)          # process steps
    data_objects: list[BpmnElement] = field(default_factory=list)
    data_stores: list[BpmnElement] = field(default_factory=list)
    gateways: list[BpmnElement] = field(default_factory=list)
    events: list[BpmnElement] = field(default_factory=list)
    annotations: list[BpmnElement] = field(default_factory=list)
    sequence_flows: list[BpmnElement] = field(default_factory=list)
    message_flows: list[BpmnElement] = field(default_factory=list)
    associations: list[BpmnElement] = field(default_factory=list)
    data_associations: list[BpmnElement] = field(default_factory=list)

    def all_elements(self) -> list[BpmnElement]:
        return (
            self.participants + self.lanes + self.tasks + self.data_objects
            + self.data_stores + self.gateways + self.events + self.annotations
            + self.sequence_flows + self.message_flows + self.associations
            + self.data_associations
        )


# --- Helpers ---------------------------------------------------------------

def _strip_ns(tag: str) -> str:
    """`{http://...}task` -> `task`."""
    return tag.split("}", 1)[1] if "}" in tag else tag


def _attr(el: ET.Element, name: str, default: str = "") -> str:
    return el.get(name, default) or default


# --- Per-element extractors ------------------------------------------------

# Tags that count as "tasks" (process steps). BPMN has many task subtypes.
TASK_TAGS = {
    "task", "userTask", "serviceTask", "manualTask", "scriptTask",
    "businessRuleTask", "sendTask", "receiveTask", "callActivity",
    "subProcess",
}

EVENT_TAGS = {
    "startEvent", "endEvent", "intermediateThrowEvent",
    "intermediateCatchEvent", "boundaryEvent",
}

GATEWAY_TAGS = {
    "exclusiveGateway", "parallelGateway", "inclusiveGateway",
    "eventBasedGateway", "complexGateway",
}


def _extract_lanes(process: ET.Element, process_id: str) -> list[BpmnElement]:
    lanes: list[BpmnElement] = []
    for lane in process.iter(f"{{{NS['bpmn']}}}lane"):
        lane_id = _attr(lane, "id")
        name = _attr(lane, "name")
        # collect referenced flow nodes so we know which task lives in which lane
        flow_node_refs = [
            ref.text for ref in lane.findall("bpmn:flowNodeRef", NS) if ref.text
        ]
        lanes.append(BpmnElement(
            id=lane_id,
            name=name or "(naamloze lane)",
            kind="lane",
            subtype="lane",
            process_id=process_id,
            attributes={"flow_node_refs": flow_node_refs},
            evidence={
                "reason": "Element <bpmn:lane> binnen <bpmn:laneSet>; "
                          "een lane representeert een rol/actor in het proces.",
                "xml_tag": "bpmn:lane",
                "name_attr": name,
            },
        ))
    return lanes


def _build_lane_lookup(lanes: list[BpmnElement]) -> dict[str, str]:
    """flow_node_id -> lane_id."""
    lookup: dict[str, str] = {}
    for lane in lanes:
        for ref in lane.attributes.get("flow_node_refs", []):
            lookup[ref] = lane.id
    return lookup


def _extract_tasks(process: ET.Element, process_id: str,
                   lane_lookup: dict[str, str]) -> list[BpmnElement]:
    tasks: list[BpmnElement] = []
    for child in process.iter():
        local = _strip_ns(child.tag)
        if local not in TASK_TAGS:
            continue
        task_id = _attr(child, "id")
        name = _attr(child, "name")
        # data input/output associations on the task
        data_inputs = [
            _attr(e, "id") for e in child.findall("bpmn:dataInputAssociation", NS)
        ]
        data_outputs = [
            _attr(e, "id") for e in child.findall("bpmn:dataOutputAssociation", NS)
        ]
        tasks.append(BpmnElement(
            id=task_id,
            name=name or f"(naamloze {local})",
            kind="task",
            subtype=local,
            process_id=process_id,
            lane_id=lane_lookup.get(task_id, ""),
            attributes={
                "data_inputs": data_inputs,
                "data_outputs": data_outputs,
            },
            evidence={
                "reason": f"XML-tag <bpmn:{local}> is een {local}-activiteit "
                          f"(processtap). Geclassificeerd als 'task' omdat "
                          f"{local} in de BPMN 2.0 spec onder Activity valt.",
                "xml_tag": f"bpmn:{local}",
                "in_lane": lane_lookup.get(task_id, "(geen lane)"),
            },
        ))
    return tasks


def _extract_data_objects(process: ET.Element, process_id: str
                          ) -> list[BpmnElement]:
    """Combine <dataObject> and <dataObjectReference>."""
    items: list[BpmnElement] = []
    # Underlying data objects (definitions)
    for el in process.iter(f"{{{NS['bpmn']}}}dataObject"):
        items.append(BpmnElement(
            id=_attr(el, "id"),
            name=_attr(el, "name") or "(naamloos dataObject)",
            kind="dataObject",
            subtype="dataObject",
            process_id=process_id,
            evidence={
                "reason": "XML-tag <bpmn:dataObject>: dit is de definitie "
                          "van een data-object dat in het proces ontstaat of "
                          "wordt gebruikt. In de data-inventarisatie wordt dit "
                          "behandeld als entiteit (vaak een aggregaat van "
                          "attributen).",
                "xml_tag": "bpmn:dataObject",
            },
        ))
    # References (instances pointing to a dataObject)
    for el in process.iter(f"{{{NS['bpmn']}}}dataObjectReference"):
        items.append(BpmnElement(
            id=_attr(el, "id"),
            name=_attr(el, "name") or "(naamloze dataObjectReference)",
            kind="dataObject",
            subtype="dataObjectReference",
            process_id=process_id,
            attributes={"ref": _attr(el, "dataObjectRef")},
            evidence={
                "reason": "XML-tag <bpmn:dataObjectReference>: een verwijzing "
                          "naar een dataObject. De naam staat doorgaans op de "
                          "reference, niet op het onderliggende dataObject.",
                "xml_tag": "bpmn:dataObjectReference",
                "points_to": _attr(el, "dataObjectRef"),
            },
        ))
    return items


def _extract_data_stores(root: ET.Element) -> list[BpmnElement]:
    stores: list[BpmnElement] = []
    # dataStore can be on root level (definitions) or as references in processes
    for el in root.iter(f"{{{NS['bpmn']}}}dataStore"):
        stores.append(BpmnElement(
            id=_attr(el, "id"),
            name=_attr(el, "name") or "(naamloze dataStore)",
            kind="dataStore",
            subtype="dataStore",
            evidence={
                "reason": "XML-tag <bpmn:dataStore>: een persistente "
                          "opslagplaats (database, register). Mapt naar 'Bron "
                          "(master)' in de data-inventarisatie.",
                "xml_tag": "bpmn:dataStore",
            },
        ))
    for el in root.iter(f"{{{NS['bpmn']}}}dataStoreReference"):
        stores.append(BpmnElement(
            id=_attr(el, "id"),
            name=_attr(el, "name") or "(naamloze dataStoreReference)",
            kind="dataStore",
            subtype="dataStoreReference",
            attributes={"ref": _attr(el, "dataStoreRef")},
            evidence={
                "reason": "XML-tag <bpmn:dataStoreReference>: verwijzing "
                          "naar een dataStore vanuit een proces.",
                "xml_tag": "bpmn:dataStoreReference",
                "points_to": _attr(el, "dataStoreRef"),
            },
        ))
    return stores


def _extract_gateways(process: ET.Element, process_id: str,
                      lane_lookup: dict[str, str]) -> list[BpmnElement]:
    gws: list[BpmnElement] = []
    for child in process.iter():
        local = _strip_ns(child.tag)
        if local not in GATEWAY_TAGS:
            continue
        gw_id = _attr(child, "id")
        gws.append(BpmnElement(
            id=gw_id,
            name=_attr(child, "name") or f"(naamloze {local})",
            kind="gateway",
            subtype=local,
            process_id=process_id,
            lane_id=lane_lookup.get(gw_id, ""),
            attributes={"default": _attr(child, "default")},
            evidence={
                "reason": f"XML-tag <bpmn:{local}>: routerings-gateway. "
                          f"Geen data-object maar een procesbeslissing; "
                          f"in de inventarisatie genoteerd als procesattribuut "
                          f"(condition expressions, default flows).",
                "xml_tag": f"bpmn:{local}",
            },
        ))
    return gws


def _extract_events(process: ET.Element, process_id: str,
                    lane_lookup: dict[str, str]) -> list[BpmnElement]:
    events: list[BpmnElement] = []
    for child in process.iter():
        local = _strip_ns(child.tag)
        if local not in EVENT_TAGS:
            continue
        ev_id = _attr(child, "id")
        # detect event sub-definition (message, timer, signal, ...)
        sub_def = ""
        for sub in child:
            sub_local = _strip_ns(sub.tag)
            if sub_local.endswith("EventDefinition"):
                sub_def = sub_local
                break
        events.append(BpmnElement(
            id=ev_id,
            name=_attr(child, "name") or f"(naamloos {local})",
            kind="event",
            subtype=f"{local}/{sub_def}" if sub_def else local,
            process_id=process_id,
            lane_id=lane_lookup.get(ev_id, ""),
            evidence={
                "reason": f"XML-tag <bpmn:{local}>"
                          + (f" met <bpmn:{sub_def}>" if sub_def else "")
                          + f": event-element. Vooral relevant voor "
                          f"data-inventarisatie als het een MessageEvent is "
                          f"(communicatie / data-uitwisseling).",
                "xml_tag": f"bpmn:{local}",
                "event_definition": sub_def or "(geen)",
            },
        ))
    return events


def _extract_annotations(parent: ET.Element) -> list[BpmnElement]:
    notes: list[BpmnElement] = []
    for el in parent.iter(f"{{{NS['bpmn']}}}textAnnotation"):
        text_el = el.find("bpmn:text", NS)
        text = (text_el.text or "").strip() if text_el is not None else ""
        notes.append(BpmnElement(
            id=_attr(el, "id"),
            name=text[:60] + ("…" if len(text) > 60 else ""),
            kind="annotation",
            subtype="textAnnotation",
            attributes={"text": text},
            evidence={
                "reason": "XML-tag <bpmn:textAnnotation>: vrije tekstnotitie "
                          "naast een activiteit. Bevat vaak business rules of "
                          "verduidelijkingen die in de inventarisatie als "
                          "'Opmerkingen' terugkomen.",
                "xml_tag": "bpmn:textAnnotation",
            },
        ))
    return notes


def _extract_flows(parent: ET.Element) -> tuple[list[BpmnElement],
                                                list[BpmnElement],
                                                list[BpmnElement],
                                                list[BpmnElement]]:
    seq, msg, assoc, data_assoc = [], [], [], []
    for el in parent.iter(f"{{{NS['bpmn']}}}sequenceFlow"):
        seq.append(BpmnElement(
            id=_attr(el, "id"),
            name=_attr(el, "name"),
            kind="sequenceFlow",
            attributes={
                "source": _attr(el, "sourceRef"),
                "target": _attr(el, "targetRef"),
            },
            evidence={
                "reason": "XML-tag <bpmn:sequenceFlow>: pijl tussen twee "
                          "elementen binnen één pool. Geeft volgorde aan, "
                          "geen data-overdracht.",
                "xml_tag": "bpmn:sequenceFlow",
            },
        ))
    for el in parent.iter(f"{{{NS['bpmn']}}}messageFlow"):
        msg.append(BpmnElement(
            id=_attr(el, "id"),
            name=_attr(el, "name") or "(naamloze messageFlow)",
            kind="messageFlow",
            attributes={
                "source": _attr(el, "sourceRef"),
                "target": _attr(el, "targetRef"),
            },
            evidence={
                "reason": "XML-tag <bpmn:messageFlow>: pijl tussen twee "
                          "pools. Indiceert communicatie/data-uitwisseling "
                          "tussen actoren — relevant voor data-inventarisatie.",
                "xml_tag": "bpmn:messageFlow",
            },
        ))
    for el in parent.iter(f"{{{NS['bpmn']}}}association"):
        assoc.append(BpmnElement(
            id=_attr(el, "id"),
            name="",
            kind="association",
            attributes={
                "source": _attr(el, "sourceRef"),
                "target": _attr(el, "targetRef"),
            },
            evidence={
                "reason": "XML-tag <bpmn:association>: stippellijn die "
                          "bijvoorbeeld een textAnnotation aan een activiteit "
                          "koppelt.",
                "xml_tag": "bpmn:association",
            },
        ))
    for tag in ("dataInputAssociation", "dataOutputAssociation"):
        for el in parent.iter(f"{{{NS['bpmn']}}}{tag}"):
            src_el = el.find("bpmn:sourceRef", NS)
            tgt_el = el.find("bpmn:targetRef", NS)
            data_assoc.append(BpmnElement(
                id=_attr(el, "id"),
                name="",
                kind="dataAssociation",
                subtype=tag,
                attributes={
                    "source": (src_el.text if src_el is not None else "") or "",
                    "target": (tgt_el.text if tgt_el is not None else "") or "",
                },
                evidence={
                    "reason": f"XML-tag <bpmn:{tag}>: koppelt een dataObject "
                              f"als input of output aan een activiteit. Dit "
                              f"is de directe bron voor 'welk dataobject "
                              f"hoort bij welke processtap'.",
                    "xml_tag": f"bpmn:{tag}",
                },
            ))
    return seq, msg, assoc, data_assoc


# --- Public API ------------------------------------------------------------

def parse_bpmn(path: str | Path) -> ParsedBpmn:
    """Parse a single .bpmn file into a ParsedBpmn structure."""
    path = Path(path)
    tree = ET.parse(path)
    root = tree.getroot()

    parsed = ParsedBpmn(source_file=path.name)

    # Collaboration: pools (participants), message flows, top-level annotations
    for collab in root.iter(f"{{{NS['bpmn']}}}collaboration"):
        for participant in collab.findall("bpmn:participant", NS):
            parsed.participants.append(BpmnElement(
                id=_attr(participant, "id"),
                name=_attr(participant, "name") or "(naamloze pool)",
                kind="participant",
                subtype="participant",
                attributes={"processRef": _attr(participant, "processRef")},
                evidence={
                    "reason": "XML-tag <bpmn:participant> in <bpmn:collaboration>: "
                              "een pool — een organisatie of systeem. "
                              "Behandelen als 'Actor' op het hoogste niveau.",
                    "xml_tag": "bpmn:participant",
                },
            ))
        # collaboration-level message flows + annotations
        seq, msg, assoc, data_assoc = _extract_flows(collab)
        parsed.message_flows.extend(msg)
        parsed.associations.extend(assoc)
        parsed.annotations.extend(_extract_annotations(collab))

    # Processes
    for process in root.iter(f"{{{NS['bpmn']}}}process"):
        process_id = _attr(process, "id")
        process_name = _attr(process, "name") or process_id
        if not parsed.process_name:
            parsed.process_name = process_name

        lanes = _extract_lanes(process, process_id)
        parsed.lanes.extend(lanes)
        lookup = _build_lane_lookup(lanes)

        parsed.tasks.extend(_extract_tasks(process, process_id, lookup))
        parsed.data_objects.extend(_extract_data_objects(process, process_id))
        parsed.gateways.extend(_extract_gateways(process, process_id, lookup))
        parsed.events.extend(_extract_events(process, process_id, lookup))
        parsed.annotations.extend(_extract_annotations(process))

        seq, msg, assoc, data_assoc = _extract_flows(process)
        parsed.sequence_flows.extend(seq)
        parsed.message_flows.extend(msg)
        parsed.associations.extend(assoc)
        parsed.data_associations.extend(data_assoc)

    # data stores live at root or process level
    parsed.data_stores.extend(_extract_data_stores(root))

    return parsed


def parse_all(directory: str | Path) -> list[ParsedBpmn]:
    """Parse every .bpmn in a directory."""
    directory = Path(directory)
    return [parse_bpmn(p) for p in sorted(directory.glob("*.bpmn"))]


if __name__ == "__main__":
    import sys
    for f in sys.argv[1:]:
        p = parse_bpmn(f)
        print(f"=== {p.source_file} ({p.process_name}) ===")
        print(f"  pools:     {len(p.participants)}")
        print(f"  lanes:     {len(p.lanes)} -> {[l.name for l in p.lanes]}")
        print(f"  tasks:     {len(p.tasks)}")
        print(f"  data obj:  {len(p.data_objects)} -> "
              f"{sorted({d.name for d in p.data_objects})}")
        print(f"  data stor: {len(p.data_stores)}")
        print(f"  gateways:  {len(p.gateways)}")
        print(f"  events:    {len(p.events)}")
        print(f"  annot:     {len(p.annotations)}")
