"""
Export MergedModel + afgeleide ERD naar ArchiMate 3.1 Open Exchange XML.

Blue Dolphin, Archi en andere ArchiMate-tools importeren dit formaat
direct. BPMN-elementen krijgen een ArchiMate-tegenhanger volgens:

  BPMN                         ArchiMate 3.1
  ---------------------------- --------------------------------
  Participant (Pool)           BusinessActor
  Lane                         BusinessRole
  Process                      BusinessProcess (top-level)
  Task / SubProcess            BusinessProcess (fijnmaziger)
  DataObject                   DataObject
  DataStore (systeem)          ApplicationComponent
  ERD-entity (afgeleid)        DataObject  (als nog niet bestond)

  SequenceFlow                 TriggeringRelationship
  MessageFlow                  FlowRelationship
  dataInputAssociation         AccessRelationship (Read)
  dataOutputAssociation        AccessRelationship (Write)
  Lane → Task                  AssignmentRelationship (Role → Process)
  Pool → Lane                  CompositionRelationship
  Process → Task               CompositionRelationship
  ERD-relationship             AssociationRelationship (met label)

Er worden GEEN `<views>` gegenereerd — Blue Dolphin berekent layouts
zelf bij import, en een automatisch gegenereerde view wordt toch altijd
bijgesteld door de architect. Elementen worden wel georganiseerd in
folders ("Organizations") per categorie.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

from merger import MergedModel


NS_ARCHIMATE = "http://www.opengroup.org/xsd/archimate/3.0/"
NS_XSI = "http://www.w3.org/2001/XMLSchema-instance"
SCHEMA_LOC = (f"{NS_ARCHIMATE} "
              "http://www.opengroup.org/xsd/archimate/3.0/archimate3_Model.xsd")


_ID_RE = re.compile(r"[^A-Za-z0-9]+")


def _safe_id(prefix: str, raw: str) -> str:
    """ArchiMate identifiers moeten beginnen met een letter en alleen
    letters/cijfers/'-'/'_' bevatten. Voeg prefix toe om globaal uniek
    te blijven over verschillende categorieen."""
    clean = _ID_RE.sub("-", raw or "").strip("-")
    if not clean:
        clean = "x"
    return f"id-{prefix}-{clean}"[:80]


def _add_name(parent: ET.Element, text: str, lang: str = "nl") -> None:
    name = ET.SubElement(parent, "name")
    name.set("{http://www.w3.org/XML/1998/namespace}lang", lang)
    name.text = text or ""


def _add_doc(parent: ET.Element, text: str, lang: str = "nl") -> None:
    if not text:
        return
    doc = ET.SubElement(parent, "documentation")
    doc.set("{http://www.w3.org/XML/1998/namespace}lang", lang)
    doc.text = text


def _add_property(parent: ET.Element, key: str, value: str,
                  props_root: ET.Element | None = None) -> None:
    """ArchiMate properties vereisen een propertyDefinitions-sectie met
    propertyDefinitionRefs. Om het simpel te houden gebruiken we één
    propertyDefinition per property-naam."""
    if not value:
        return
    props = parent.find("properties")
    if props is None:
        props = ET.SubElement(parent, "properties")
    p = ET.SubElement(props, "property", {
        "propertyDefinitionRef": f"propdef-{_ID_RE.sub('-', key.lower())}",
    })
    v = ET.SubElement(p, "value")
    v.set("{http://www.w3.org/XML/1998/namespace}lang", "nl")
    v.text = str(value)


# ---------------------------------------------------------------------------
# Bouwers per ArchiMate-element
# ---------------------------------------------------------------------------

def _make_element(parent: ET.Element, xsi_type: str,
                  identifier: str, name: str,
                  documentation: str = "",
                  properties: dict | None = None) -> ET.Element:
    el = ET.SubElement(parent, "element", {
        "identifier": identifier,
        f"{{{NS_XSI}}}type": xsi_type,
    })
    _add_name(el, name)
    _add_doc(el, documentation)
    for k, v in (properties or {}).items():
        _add_property(el, k, v)
    return el


def _make_relationship(parent: ET.Element, xsi_type: str,
                       identifier: str, source: str, target: str,
                       name: str = "",
                       access_type: str = "",
                       extra_attrs: dict | None = None) -> ET.Element:
    attrs = {
        "identifier": identifier,
        "source": source,
        "target": target,
        f"{{{NS_XSI}}}type": xsi_type,
    }
    if access_type:
        attrs["accessType"] = access_type
    for k, v in (extra_attrs or {}).items():
        attrs[k] = v
    r = ET.SubElement(parent, "relationship", attrs)
    if name:
        _add_name(r, name)
    return r


# ---------------------------------------------------------------------------
# Hoofd-functie
# ---------------------------------------------------------------------------

def write_archimate(model: MergedModel,
                    entities: list,       # list[bpmn_erd.Entity]
                    relationships: list,  # list[bpmn_erd.Relationship]
                    out_path: str | Path,
                    model_name: str = "BPMN -> ArchiMate export") -> Path:
    """Schrijf één .xml-file in ArchiMate 3.1 Open Exchange formaat.

    `entities` en `relationships` komen uit `bpmn_erd.build_erd()` zodat
    we de gededupliceerde DataObjects en hun relaties kunnen exporteren.
    """
    ET.register_namespace("", NS_ARCHIMATE)
    ET.register_namespace("xsi", NS_XSI)

    root = ET.Element("model", {
        "identifier": "id-model-bpmn-import",
        f"{{{NS_XSI}}}schemaLocation": SCHEMA_LOC,
    })
    root.set("xmlns", NS_ARCHIMATE)
    root.set(f"xmlns:xsi", NS_XSI)
    _add_name(root, model_name)
    _add_doc(root,
             f"Gegenereerd uit {len(model.bpmns)} BPMN-bestanden. "
             f"{len(entities)} ERD-entiteiten, "
             f"{len(relationships)} ERD-relaties.")

    # PropertyDefinitions (referenced by _add_property)
    pdefs = ET.SubElement(root, "propertyDefinitions")
    for key, typ in [
        ("source-bpmn", "string"),
        ("source-file", "string"),
        ("bpmn-kind", "string"),
        ("detection", "string"),
        ("cardinality", "string"),
        ("is-anchor", "string"),
        ("is-master", "string"),
    ]:
        pd = ET.SubElement(pdefs, "propertyDefinition", {
            "identifier": f"propdef-{key}",
            "type": typ,
        })
        _add_name(pd, key)

    elements_root = ET.SubElement(root, "elements")
    relationships_root = ET.SubElement(root, "relationships")

    # --- Stap 1: verzamel unieke elementen over alle BPMNs heen ---
    # Elementen worden gededupliceerd op (xsi_type, naam-lowercase) zodat
    # dezelfde pool in meerdere BPMNs 1x in ArchiMate voorkomt.
    el_by_key: dict[tuple[str, str], str] = {}  # (type, lowered name) -> id
    el_attrs: dict[str, dict] = {}              # id -> {name, doc, props}

    def _get_or_add(xsi_type: str, name: str, source: str = "",
                    extra_props: dict | None = None,
                    prefix: str = "e") -> str:
        key = (xsi_type, (name or "").strip().lower())
        if key in el_by_key:
            eid = el_by_key[key]
            # Voeg extra bron-bestand toe aan property (multi-valued via \n)
            if source:
                existing = el_attrs[eid]["props"].get("source-bpmn", "")
                if source not in existing.split("\n"):
                    el_attrs[eid]["props"]["source-bpmn"] = (
                        (existing + "\n" if existing else "") + source
                    )
            if extra_props:
                el_attrs[eid]["props"].update(extra_props)
            return eid
        eid = _safe_id(prefix, f"{xsi_type}-{name}")
        el_by_key[key] = eid
        el_attrs[eid] = {
            "type": xsi_type,
            "name": name or "(naamloos)",
            "doc": "",
            "props": {"source-bpmn": source} if source else {},
        }
        if extra_props:
            el_attrs[eid]["props"].update(extra_props)
        return eid

    # Per-BPMN element-id → ArchiMate element-id (nodig voor relaties)
    bpmn_id_to_aid: dict[tuple[str, str], str] = {}  # (source_file, bpmn_id) -> aid

    # Bouw elementen
    for parsed in model.bpmns:
        src = parsed.source_file
        # Pools → BusinessActor
        for p in parsed.participants:
            aid = _get_or_add("BusinessActor", p.name, source=src,
                              extra_props={"bpmn-kind": "participant/pool"},
                              prefix="actor")
            bpmn_id_to_aid[(src, p.id)] = aid
        # Lanes → BusinessRole
        for lane in parsed.lanes:
            aid = _get_or_add("BusinessRole", lane.name, source=src,
                              extra_props={"bpmn-kind": "lane"},
                              prefix="role")
            bpmn_id_to_aid[(src, lane.id)] = aid
        # Processes → BusinessProcess (top-level; één per BPMN)
        proc_name = parsed.process_name or parsed.source_file
        proc_aid = _get_or_add("BusinessProcess", proc_name, source=src,
                               extra_props={"bpmn-kind": "process"},
                               prefix="proc")
        # Tasks → BusinessProcess (fijnmaziger)
        for t in parsed.tasks:
            if not t.name:
                continue
            aid = _get_or_add("BusinessProcess", t.name, source=src,
                              extra_props={"bpmn-kind": "task"},
                              prefix="task")
            bpmn_id_to_aid[(src, t.id)] = aid
        # DataStores → ApplicationComponent (systemen)
        for ds in parsed.data_stores:
            if not ds.name:
                continue
            aid = _get_or_add("ApplicationComponent", ds.name, source=src,
                              extra_props={"bpmn-kind": "dataStore"},
                              prefix="sys")
            bpmn_id_to_aid[(src, ds.id)] = aid
        # DataObjects → DataObject (dedupe per naam globaal)
        for d in parsed.data_objects:
            if not d.name:
                continue
            aid = _get_or_add("DataObject", d.name, source=src,
                              extra_props={"bpmn-kind": "dataObject"},
                              prefix="data")
            bpmn_id_to_aid[(src, d.id)] = aid

    # ERD-entities — één DataObject per canonical name (anti-dupliceer)
    entity_name_to_aid: dict[str, str] = {}
    for e in entities:
        props = {"detection": "ERD-afleiding"}
        if getattr(e, "is_anchor", False): props["is-anchor"] = "true"
        if getattr(e, "is_master", False): props["is-master"] = "true"
        sources = ", ".join(sorted(e.source_processes))[:240]
        if sources:
            props["source-file"] = sources
        aid = _get_or_add("DataObject", e.name,
                          extra_props=props, prefix="ent")
        entity_name_to_aid[e.name] = aid
        # Documentatie: aliases + herkomst
        aliases = ", ".join(sorted(e.aliases))
        doc_bits = []
        if aliases:
            doc_bits.append(f"Aliases: {aliases}")
        if e.detection_sources:
            kinds = defaultdict(int)
            for ds in e.detection_sources:
                kinds[ds.get("kind", "?")] += 1
            doc_bits.append("Herkomst: " +
                            ", ".join(f"{k}×{v}" for k, v in kinds.items()))
        el_attrs[aid]["doc"] = " | ".join(doc_bits)

    # --- Stap 2: schrijf elementen (grouped nog niet, alleen flat) ---
    for aid, info in el_attrs.items():
        el = _make_element(elements_root,
                           xsi_type=info["type"], identifier=aid,
                           name=info["name"],
                           documentation=info["doc"],
                           properties=info["props"])

    # --- Stap 3: relaties uit BPMN + ERD ---
    rel_counter = [0]

    def _rel_id() -> str:
        rel_counter[0] += 1
        return f"id-rel-{rel_counter[0]}"

    def _safe_rel(xsi: str, s: str, t: str, name: str = "",
                  access: str = "") -> None:
        if not s or not t or s == t:
            return
        _make_relationship(relationships_root, xsi, _rel_id(), s, t,
                           name=name, access_type=access)

    for parsed in model.bpmns:
        src = parsed.source_file
        proc_name = parsed.process_name or parsed.source_file
        proc_aid = el_by_key.get(("BusinessProcess",
                                  proc_name.strip().lower()))

        # Pool (Actor) ←Assignment— Role (Lane)
        for lane in parsed.lanes:
            role_aid = bpmn_id_to_aid.get((src, lane.id))
            # welke pool bevat deze lane? bpmn_parser zet process_id niet
            # altijd per lane op pool-niveau; voor eenvoud: koppel role aan
            # alle pools in deze BPMN.
            for p in parsed.participants:
                actor_aid = bpmn_id_to_aid.get((src, p.id))
                if actor_aid and role_aid:
                    _safe_rel("AssignmentRelationship", actor_aid, role_aid)

        # Process —Composition→ Task
        for t in parsed.tasks:
            task_aid = bpmn_id_to_aid.get((src, t.id))
            if proc_aid and task_aid:
                _safe_rel("CompositionRelationship", proc_aid, task_aid)
            # Lane —Assignment→ Task (Role uitvoerend op Process)
            if t.lane_id:
                role_aid = bpmn_id_to_aid.get((src, t.lane_id))
                if role_aid and task_aid:
                    _safe_rel("AssignmentRelationship", role_aid, task_aid)

        # SequenceFlow → Triggering tussen taken
        for sf in parsed.sequence_flows:
            s = bpmn_id_to_aid.get((src, sf.attributes.get("source", "")))
            tg = bpmn_id_to_aid.get((src, sf.attributes.get("target", "")))
            if s and tg:
                _safe_rel("TriggeringRelationship", s, tg,
                          name=sf.name or "")

        # MessageFlow → Flow
        for mf in parsed.message_flows:
            s = bpmn_id_to_aid.get((src, mf.attributes.get("source", "")))
            tg = bpmn_id_to_aid.get((src, mf.attributes.get("target", "")))
            if s and tg:
                _safe_rel("FlowRelationship", s, tg, name=mf.name or "")

        # dataAssociations → Access
        for da in parsed.data_associations:
            s = bpmn_id_to_aid.get((src, da.attributes.get("source", "")))
            tg = bpmn_id_to_aid.get((src, da.attributes.get("target", "")))
            if not (s and tg):
                continue
            if da.subtype == "dataInputAssociation":
                # dataObject (s) → task (tg): Access Read, richting naar data
                _safe_rel("AccessRelationship", tg, s,
                          name="leest", access="Read")
            else:
                _safe_rel("AccessRelationship", s, tg,
                          name="schrijft", access="Write")

    # ERD-relationships → Association
    for rel in relationships:
        s_aid = entity_name_to_aid.get(rel.left)
        t_aid = entity_name_to_aid.get(rel.right)
        if s_aid and t_aid:
            label = rel.label
            if rel.left_card or rel.right_card:
                label = f"{label} ({rel.left_card}..{rel.right_card})"
            _safe_rel("AssociationRelationship", s_aid, t_aid, name=label)

    # --- Stap 4: Organizations (folders) ---
    orgs = ET.SubElement(root, "organizations")
    folder_map = {
        "BusinessActor":       ("Actoren", "actors"),
        "BusinessRole":        ("Rollen", "roles"),
        "BusinessProcess":     ("Processen & Taken", "processes"),
        "DataObject":          ("Data", "data"),
        "ApplicationComponent": ("Systemen", "systems"),
    }
    by_type: dict[str, list[str]] = defaultdict(list)
    for aid, info in el_attrs.items():
        by_type[info["type"]].append(aid)
    for xsi_type, (title, key) in folder_map.items():
        ids = by_type.get(xsi_type, [])
        if not ids:
            continue
        item = ET.SubElement(orgs, "item", {"identifier": f"org-{key}"})
        _add_name(item, title)
        for aid in ids:
            ET.SubElement(item, "item", {"identifierRef": aid})

    # --- Stap 5: schrijf netjes naar file ---
    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tree.write(str(out), encoding="utf-8", xml_declaration=True)
    return out
