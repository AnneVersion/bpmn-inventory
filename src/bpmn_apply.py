"""
Past automatische fixes toe op een BPMN-bestand en bewaart versies.

De volgende fixes worden ondersteund (start-set; uitbreidbaar):
- ADD_DATA_OBJECT: voeg een <bpmn:dataObject> + <bpmn:dataObjectReference>
  toe aan het proces en koppel via data(Input|Output)Association aan een
  specifieke taak. Gebaseerd op finding R101.

Versiebeheer:
- De originele file blijft altijd op disk als `<file>.bpmn`.
- Iedere fix-apply schrijft een nieuwe versie naar
  `versions/<basename>/v<N>.bpmn` en schrijft ook een `versions.json`
  met de geschiedenis (who, what, when, finding_ref).
- `load_versions()` geeft alle beschikbare versies terug. `get_path()`
  geeft het pad naar een specifieke versie.
"""

from __future__ import annotations

import json
import re
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

BPMN_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL"
NS = {"bpmn": BPMN_NS}

# Zorgt dat output geen ns0: krijgt maar bpmn:
ET.register_namespace("bpmn", BPMN_NS)
ET.register_namespace("bpmndi", "http://www.omg.org/spec/BPMN/20100524/DI")
ET.register_namespace("di", "http://www.omg.org/spec/DD/20100524/DI")
ET.register_namespace("dc", "http://www.omg.org/spec/DD/20100524/DC")


@dataclass
class VersionEntry:
    version: int
    file: str                 # relatief pad tov session/project dir
    created_at: str
    action: str               # 'upload' | 'fix' | 'revert'
    description: str
    applied_finding: dict | None = None


# ---------------------------------------------------------------------------
# XML helpers
# ---------------------------------------------------------------------------

def _qname(tag: str) -> str:
    return f"{{{BPMN_NS}}}{tag}"


def _safe_id(raw: str, prefix: str) -> str:
    base = re.sub(r"[^A-Za-z0-9]+", "_", raw).strip("_") or "X"
    suffix = uuid.uuid4().hex[:6]
    return f"{prefix}_{base}_{suffix}"


def _find_element_by_id(root: ET.Element, elem_id: str) -> ET.Element | None:
    """Zoek een element op id binnen de BPMN-namespace."""
    for el in root.iter():
        if el.get("id") == elem_id:
            return el
    return None


def _find_containing_process(root: ET.Element,
                             task_el: ET.Element) -> ET.Element | None:
    """Zoek het <bpmn:process> dat de task bevat."""
    for proc in root.iter(_qname("process")):
        if task_el in proc.iter():
            return proc
    return None


# ---------------------------------------------------------------------------
# Apply fix: voeg dataObject + association toe aan een task
# ---------------------------------------------------------------------------

def _add_dataobject_in_tree(
    root: ET.Element,
    task_id: str,
    object_name: str,
    action_type: str,
    attributes: list[dict] | None = None,
) -> bool:
    """In-place: voeg dataObject + reference + association toe. Returns True als
    de wijziging is toegepast, False als task of process niet gevonden is."""
    task = _find_element_by_id(root, task_id)
    if task is None:
        return False
    proc = _find_containing_process(root, task)
    if proc is None:
        return False

    do_id = _safe_id(object_name, "DO")
    dor_id = _safe_id(object_name, "DOR")
    data_obj = ET.SubElement(proc, _qname("dataObject"),
                             {"id": do_id, "name": object_name})
    if attributes:
        doc = ET.SubElement(data_obj, _qname("documentation"))
        doc.text = ("ATTRIBUTES_JSON="
                    + json.dumps({"attributes": attributes}, ensure_ascii=False))
    ET.SubElement(proc, _qname("dataObjectReference"),
                  {"id": dor_id, "name": object_name, "dataObjectRef": do_id})

    if action_type in ("READ", "UPDATE"):
        ia = ET.SubElement(task, _qname("dataInputAssociation"),
                           {"id": _safe_id(object_name, "IA")})
        src = ET.SubElement(ia, _qname("sourceRef"))
        src.text = dor_id
    if action_type in ("WRITE", "UPDATE"):
        oa = ET.SubElement(task, _qname("dataOutputAssociation"),
                           {"id": _safe_id(object_name, "OA")})
        tgt = ET.SubElement(oa, _qname("targetRef"))
        tgt.text = dor_id
    if action_type not in ("READ", "WRITE", "UPDATE"):
        # Onbekend -> beide kanten
        ia = ET.SubElement(task, _qname("dataInputAssociation"),
                           {"id": _safe_id(object_name, "IA")})
        ET.SubElement(ia, _qname("sourceRef")).text = dor_id
        oa = ET.SubElement(task, _qname("dataOutputAssociation"),
                           {"id": _safe_id(object_name, "OA")})
        ET.SubElement(oa, _qname("targetRef")).text = dor_id
    return True


def apply_add_dataobject(
    bpmn_path: Path,
    task_id: str,
    object_name: str,
    action_type: Literal["READ", "WRITE", "UPDATE"],
    attributes: list[dict] | None = None,
) -> Path:
    """File-I/O wrapper rond `_add_dataobject_in_tree`."""
    tree = ET.parse(bpmn_path)
    if not _add_dataobject_in_tree(tree.getroot(), task_id, object_name,
                                   action_type, attributes):
        raise ValueError(
            f"Task {task_id!r} of bijbehorend <bpmn:process> niet gevonden in "
            f"{bpmn_path}"
        )
    tree.write(bpmn_path, xml_declaration=True, encoding="UTF-8")
    return bpmn_path


def _remove_gateway_in_tree(root: ET.Element, gateway_id: str) -> bool:
    """In-place: verwijder een gateway met exact 1 uitgaande flow.

    Gedrag: incoming-flow.target wordt herrouteerd naar de target van de
    uitgaande flow. De uitgaande flow en de gateway zelf worden verwijderd.
    Returns True bij succes; False als gateway of flows niet passen.
    """
    gw = _find_element_by_id(root, gateway_id)
    if gw is None:
        return False
    proc = _find_containing_process_or_root(root, gw)
    if proc is None:
        return False

    # Zoek alle sequenceFlows met deze gateway als source of target
    incoming = []
    outgoing = []
    for sf in proc.iter(_qname("sequenceFlow")):
        if sf.get("sourceRef") == gateway_id:
            outgoing.append(sf)
        if sf.get("targetRef") == gateway_id:
            incoming.append(sf)

    if len(outgoing) != 1:
        return False  # veilig: alleen removen als degenerate
    out_flow = outgoing[0]
    out_target = out_flow.get("targetRef", "")
    if not out_target:
        return False

    # Route alle incoming flows naar de outgoing target
    for inc in incoming:
        inc.set("targetRef", out_target)

    # Verwijder de outgoing flow en de gateway
    proc.remove(out_flow)
    proc.remove(gw)

    # Poging om DI shape voor de gateway te verwijderen
    for plane in root.iter("{http://www.omg.org/spec/BPMN/20100524/DI}BPMNPlane"):
        to_remove = []
        for shape in plane:
            if shape.get("bpmnElement") in (gateway_id, out_flow.get("id")):
                to_remove.append(shape)
        for s in to_remove:
            plane.remove(s)
    return True


def _add_datastore_in_tree(
    root: ET.Element,
    task_id: str,
    system_name: str,
    action_type: str,
) -> bool:
    """In-place: voeg <bpmn:dataStoreReference> toe + koppel aan task."""
    task = _find_element_by_id(root, task_id)
    if task is None:
        return False
    proc = _find_containing_process(root, task)
    if proc is None:
        return False

    ds_id = _safe_id(system_name, "DS")
    dsr_id = _safe_id(system_name, "DSR")
    # dataStore staat op root (volgens spec), dataStoreReference in process
    defs = root if root.tag.endswith("}definitions") else root
    ET.SubElement(defs, _qname("dataStore"),
                  {"id": ds_id, "name": system_name})
    ET.SubElement(proc, _qname("dataStoreReference"),
                  {"id": dsr_id, "name": system_name, "dataStoreRef": ds_id})

    if action_type in ("READ", "UPDATE", ""):
        ia = ET.SubElement(task, _qname("dataInputAssociation"),
                           {"id": _safe_id(system_name, "IA")})
        ET.SubElement(ia, _qname("sourceRef")).text = dsr_id
    if action_type in ("WRITE", "UPDATE"):
        oa = ET.SubElement(task, _qname("dataOutputAssociation"),
                           {"id": _safe_id(system_name, "OA")})
        ET.SubElement(oa, _qname("targetRef")).text = dsr_id
    return True


def _find_containing_process_or_root(
    root: ET.Element, el: ET.Element
) -> ET.Element | None:
    """Zoek <bpmn:process> dat `el` bevat; val terug op root als niets past."""
    p = _find_containing_process(root, el)
    return p if p is not None else root


def _rename_element_in_tree(
    root: ET.Element, element_id: str, new_name: str
) -> bool:
    """In-place: zet/overschrijf het `name`-attribuut op een element."""
    el = _find_element_by_id(root, element_id)
    if el is None:
        return False
    el.set("name", new_name)
    return True


# ---------------------------------------------------------------------------
# Unified dispatch for all single-finding fixes
# ---------------------------------------------------------------------------

def apply_fix(
    bpmn_path: Path,
    rule: str,
    params: dict,
) -> tuple[bool, str]:
    """Centrale dispatch: voer een fix uit op bpmn_path op basis van rule-id.

    Returns (ok, description). Bij False is description de foutmelding.
    """
    tree = ET.parse(bpmn_path)
    root = tree.getroot()
    desc = ""

    try:
        if rule == "R007":
            # Params: {gateway_id}
            gw_id = params.get("gateway_id") or params.get("element_id", "")
            if not gw_id:
                return False, "gateway_id ontbreekt"
            if not _remove_gateway_in_tree(root, gw_id):
                return False, f"Gateway {gw_id} niet gevonden of geen degenerate flow"
            desc = f"Overbodige gateway {gw_id} verwijderd"

        elif rule in ("R009", "R010", "R103"):
            # Params: {element_id, new_name}
            el_id = params.get("element_id", "")
            nm = (params.get("new_name") or "").strip()
            if not (el_id and nm):
                return False, "element_id of new_name ontbreekt"
            if not _rename_element_in_tree(root, el_id, nm):
                return False, f"Element {el_id} niet gevonden"
            desc = f"{el_id} hernoemd naar '{nm}'"

        elif rule == "R102":
            # Params: {task_id, system_name, action_type}
            task_id = params.get("task_id", "")
            system = (params.get("system_name") or "").strip()
            action = (params.get("action_type") or "READ").upper()
            if not (task_id and system):
                return False, "task_id of system_name ontbreekt"
            if not _add_datastore_in_tree(root, task_id, system, action):
                return False, f"Task {task_id} niet gevonden"
            desc = f"<bpmn:dataStoreReference name=\"{system}\"> toegevoegd op {task_id}"

        elif rule == "R101":
            # Params: {task_id, object_name, action_type, attributes}
            task_id = params.get("task_id", "")
            obj = (params.get("object_name") or params.get("object") or "").strip()
            action = (params.get("action_type") or params.get("action") or "WRITE").upper()
            attrs = params.get("attributes") or []
            if not (task_id and obj):
                return False, "task_id of object_name ontbreekt"
            if not _add_dataobject_in_tree(root, task_id, obj, action, attrs):
                return False, f"Task {task_id} niet gevonden"
            desc = (f"<bpmn:dataObject name=\"{obj}\"> + {action}-association "
                    f"toegevoegd op {task_id}")

        elif rule == "R001":
            # Params: {element_id, lane_id}
            el_id = params.get("element_id", "")
            lane_id = params.get("lane_id", "")
            if not (el_id and lane_id):
                return False, "element_id of lane_id ontbreekt"
            lane = _find_element_by_id(root, lane_id)
            if lane is None:
                return False, f"Lane {lane_id} niet gevonden"
            # Voeg flowNodeRef toe als niet al aanwezig
            existing = [ref.text for ref in lane.findall(_qname("flowNodeRef"))]
            if el_id not in existing:
                ref = ET.SubElement(lane, _qname("flowNodeRef"))
                ref.text = el_id
            desc = f"{el_id} toegewezen aan lane {lane_id}"

        elif rule == "R006":
            # Params: {dataobject_id, task_id, direction}
            do_id = params.get("dataobject_id", "")
            task_id = params.get("task_id", "")
            direction = (params.get("direction") or "input").lower()
            if not (do_id and task_id):
                return False, "dataobject_id of task_id ontbreekt"
            task = _find_element_by_id(root, task_id)
            if task is None:
                return False, f"Task {task_id} niet gevonden"
            if direction == "input":
                ia = ET.SubElement(task, _qname("dataInputAssociation"),
                                   {"id": _safe_id(do_id, "IA")})
                ET.SubElement(ia, _qname("sourceRef")).text = do_id
            else:
                oa = ET.SubElement(task, _qname("dataOutputAssociation"),
                                   {"id": _safe_id(do_id, "OA")})
                ET.SubElement(oa, _qname("targetRef")).text = do_id
            desc = f"{do_id} gekoppeld aan {task_id} als {direction}"

        else:
            return False, f"Geen apply-handler voor regel {rule}"
    except Exception as e:
        return False, f"Onverwachte fout: {e}"

    tree.write(bpmn_path, xml_declaration=True, encoding="UTF-8")
    return True, desc


def build_improved_preview(
    bpmn_path: Path,
    findings: list[dict],
    user_defs: dict | None = None,
) -> tuple[bytes, list[dict]]:
    """Genereer een 'BPMN volgens de regels' preview door alle *veilige*
    auto-fixes toe te passen op een kopie van de XML. Retourneert
    (xml_bytes, changes_list).

    Fixes die automatisch worden toegepast:
    - R007: overbodige gateways verwijderen
    - R101: ontbrekende <bpmn:dataObject> toevoegen + koppelen
    - R102: ontbrekende <bpmn:dataStoreReference> toevoegen + koppelen

    Fixes die NIET automatisch worden toegepast (vereisen menselijke keuze
    of DI-manipulatie):
    - R001, R002, R003, R004, R005, R006, R008, R009, R010, R103
    """
    import copy
    import io

    tree = ET.parse(bpmn_path)
    root = tree.getroot()
    changes: list[dict] = []

    # R007 eerst (simpelste structuurwijziging)
    for f in findings:
        if f.get("rule") != "R007":
            continue
        if f.get("source_file") != bpmn_path.name:
            continue
        gw_id = f.get("element_id", "")
        if not gw_id:
            continue
        if _remove_gateway_in_tree(root, gw_id):
            changes.append({
                "rule": "R007",
                "element": f.get("element_name") or gw_id,
                "description": (f"Overbodige gateway '{f.get('element_name') or gw_id}' "
                                "verwijderd — inkomende flow direct doorgezet."),
            })

    # R101 dataObjects toevoegen
    for f in findings:
        if f.get("rule") != "R101":
            continue
        if f.get("source_file") != bpmn_path.name:
            continue
        if not f.get("fixable") or not f.get("suggested_object"):
            continue
        task_id = f.get("element_id", "")
        obj = f.get("suggested_object", "")
        action = (f.get("action_type") or "WRITE").upper()
        attrs = f.get("suggested_attributes", []) or []
        if _add_dataobject_in_tree(root, task_id, obj, action, attrs):
            changes.append({
                "rule": "R101",
                "element": f.get("element_name") or task_id,
                "description": (f"<bpmn:dataObject name=\"{obj}\"> toegevoegd "
                                f"+ {action}-association op taak "
                                f"'{f.get('element_name')}'."),
            })

    # R102 dataStores toevoegen
    for f in findings:
        if f.get("rule") != "R102":
            continue
        if f.get("source_file") != bpmn_path.name:
            continue
        params = f.get("fix_params", {}) or {}
        task_id = params.get("task_id") or f.get("element_id", "")
        system = params.get("system_name", "")
        action = (params.get("action_type") or "READ").upper()
        if not system:
            continue
        if _add_datastore_in_tree(root, task_id, system, action):
            changes.append({
                "rule": "R102",
                "element": f.get("element_name") or task_id,
                "description": (f"<bpmn:dataStoreReference name=\"{system}\"> "
                                f"toegevoegd + {action}-association op taak "
                                f"'{f.get('element_name')}'."),
            })

    buf = io.BytesIO()
    tree.write(buf, xml_declaration=True, encoding="UTF-8")
    return buf.getvalue(), changes


# ---------------------------------------------------------------------------
# Apply fix: zet default-flow + slimme conditie-expressies op een gateway
# ---------------------------------------------------------------------------

# Vaak voorkomende flow-naam -> conditie-expressie mapping
_FLOW_CONDITION_GUESSES = {
    "ja":          "${conditie == true}",
    "yes":         "${conditie == true}",
    "true":        "${conditie == true}",
    "nee":         "${conditie == false}",
    "no":          "${conditie == false}",
    "false":       "${conditie == false}",
    "goedgekeurd": "${status == 'goedgekeurd'}",
    "afgekeurd":   "${status == 'afgekeurd'}",
    "akkoord":     "${status == 'akkoord'}",
    "niet akkoord":"${status == 'niet_akkoord'}",
    "geldig":      "${status == 'geldig'}",
    "ongeldig":    "${status == 'ongeldig'}",
    "correct":     "${status == 'correct'}",
    "incorrect":   "${status == 'incorrect'}",
    "wel":         "${voorwaarde == true}",
    "niet":        "${voorwaarde == false}",
    "bestaat":     "${bestaat == true}",
    "nieuw":       "${bestaat == false}",
    "fout":        "${status == 'fout'}",
    "ok":          "${status == 'ok'}",
}


def _guess_condition(flow_name: str) -> str:
    """Raad een conditie-expressie op basis van de flow-naam."""
    if not flow_name:
        return ""
    low = flow_name.strip().lower()
    if low in _FLOW_CONDITION_GUESSES:
        return _FLOW_CONDITION_GUESSES[low]
    for key, expr in _FLOW_CONDITION_GUESSES.items():
        if key in low:
            return expr
    # Fallback: sanitize flow name naar identifier
    safe = re.sub(r"[^A-Za-z0-9_]+", "_", low).strip("_")
    return f"${{TODO_{safe or 'conditie'}}}"


def apply_set_default_flow(
    bpmn_path: Path,
    gateway_id: str,
    default_flow_id: str | None,
    guess_conditions: bool = True,
) -> Path:
    """Zet default-attribuut op een gateway + optioneel conditie-stubs op de
    overige uitgaande flows.

    `default_flow_id = None` = alleen condities toevoegen, geen default zetten.
    `guess_conditions = True` = voeg aan niet-default flows een
    <bpmn:conditionExpression> toe o.b.v. hun naam, als die nog ontbreekt.
    """
    tree = ET.parse(bpmn_path)
    root = tree.getroot()

    gw = _find_element_by_id(root, gateway_id)
    if gw is None:
        raise ValueError(f"Gateway {gateway_id!r} niet gevonden")

    # Set default attribute
    if default_flow_id:
        gw.set("default", default_flow_id)

    # Vind uitgaande flows en voeg condities toe
    if guess_conditions:
        for sf in root.iter(_qname("sequenceFlow")):
            if sf.get("sourceRef") != gateway_id:
                continue
            if sf.get("id") == default_flow_id:
                continue  # default heeft geen conditie nodig
            # Check of er al een conditionExpression is
            existing = sf.find(_qname("conditionExpression"))
            if existing is not None and (existing.text or "").strip():
                continue
            cond = _guess_condition(sf.get("name", ""))
            if not cond:
                continue
            if existing is None:
                cx = ET.SubElement(sf, _qname("conditionExpression"), {
                    "{http://www.w3.org/2001/XMLSchema-instance}type":
                        "bpmn:tFormalExpression",
                })
            else:
                cx = existing
            cx.text = cond

    tree.write(bpmn_path, xml_declaration=True, encoding="UTF-8")
    return bpmn_path


# ---------------------------------------------------------------------------
# Versiebeheer per BPMN-bestand binnen een sessie-folder
# ---------------------------------------------------------------------------

def _versions_dir(session_dir: Path, filename: str) -> Path:
    base = Path(filename).stem
    d = session_dir / "versions" / base
    d.mkdir(parents=True, exist_ok=True)
    return d


def _meta_path(session_dir: Path, filename: str) -> Path:
    return _versions_dir(session_dir, filename) / "versions.json"


def load_versions(session_dir: Path, filename: str) -> list[dict]:
    mp = _meta_path(session_dir, filename)
    if not mp.exists():
        return []
    with mp.open("r", encoding="utf-8") as f:
        return json.load(f)


def _save_versions(session_dir: Path, filename: str,
                   versions: list[dict]) -> None:
    mp = _meta_path(session_dir, filename)
    with mp.open("w", encoding="utf-8") as f:
        json.dump(versions, f, indent=2, ensure_ascii=False)


def ensure_v1(session_dir: Path, filename: str) -> list[dict]:
    """Zorg dat er altijd een v1 (originele upload) geregistreerd staat."""
    versions = load_versions(session_dir, filename)
    if versions:
        return versions
    # Kopieer huidige data-file naar versions/<base>/v1.<ext>
    src = session_dir / "data" / filename
    if not src.exists():
        return []
    ext = Path(filename).suffix
    vdir = _versions_dir(session_dir, filename)
    dest = vdir / f"v1{ext}"
    dest.write_bytes(src.read_bytes())
    entry = {
        "version": 1,
        "file": str(dest.relative_to(session_dir)),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "action": "upload",
        "description": "Oorspronkelijke upload",
        "applied_finding": None,
    }
    versions = [entry]
    _save_versions(session_dir, filename, versions)
    return versions


def add_fix_version(session_dir: Path, filename: str,
                    patched_bytes: bytes,
                    description: str,
                    applied_finding: dict | None = None) -> dict:
    """Sla nieuwe versie op na een fix. Retourneert de nieuwe VersionEntry dict."""
    ensure_v1(session_dir, filename)
    versions = load_versions(session_dir, filename)
    next_v = (max(v["version"] for v in versions) + 1) if versions else 1
    ext = Path(filename).suffix
    vdir = _versions_dir(session_dir, filename)
    dest = vdir / f"v{next_v}{ext}"
    dest.write_bytes(patched_bytes)
    entry = {
        "version": next_v,
        "file": str(dest.relative_to(session_dir)),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "action": "fix",
        "description": description,
        "applied_finding": applied_finding,
    }
    versions.append(entry)
    _save_versions(session_dir, filename, versions)

    # Update "current" data-file naar de nieuwste versie
    active = session_dir / "data" / filename
    active.write_bytes(patched_bytes)
    return entry


def add_upload_version(session_dir: Path, filename: str,
                       bytes_: bytes,
                       description: str = "Nieuwe upload") -> dict:
    """Voor wanneer de gebruiker een nieuwe versie van hetzelfde .bpmn
    upload. Sla hem op als volgende versie en maak 'm actief."""
    ensure_v1(session_dir, filename)
    versions = load_versions(session_dir, filename)
    next_v = (max(v["version"] for v in versions) + 1) if versions else 1
    ext = Path(filename).suffix
    vdir = _versions_dir(session_dir, filename)
    dest = vdir / f"v{next_v}{ext}"
    dest.write_bytes(bytes_)
    entry = {
        "version": next_v,
        "file": str(dest.relative_to(session_dir)),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "action": "upload",
        "description": description,
    }
    versions.append(entry)
    _save_versions(session_dir, filename, versions)

    active = session_dir / "data" / filename
    active.write_bytes(bytes_)
    return entry


def activate_version(session_dir: Path, filename: str, version: int) -> bool:
    """Maak een specifieke versie actief (kopieer naar data/<filename>)."""
    versions = load_versions(session_dir, filename)
    target = next((v for v in versions if v["version"] == version), None)
    if not target:
        return False
    src = session_dir / target["file"]
    if not src.exists():
        return False
    (session_dir / "data" / filename).write_bytes(src.read_bytes())
    return True
