"""
Export the merged model to a single .drawio file with two pages:

1. "BPMN samengevoegd" - all process steps grouped per source BPMN, with
   data objects and actors highlighted. Anchor objects (data objects
   appearing in 2+ BPMNs) get a distinctive colour.

2. "ERD" - entities derived from data objects, with relationships
   inferred from co-occurrence in the same task.

Every shape is wrapped in a <UserObject> with a `tooltip` attribute
that explains:
  * which BPMN element it came from (XML tag),
  * how it was classified (entity, actor, gateway, ...),
  * the rule that determined the classification,
  * for anchors: in which BPMN files it appears.

draw.io renders the tooltip on hover, so the diagram becomes
self-documenting.
"""

from __future__ import annotations

import html
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass

from merger import MergedModel
from bpmn_parser import BpmnElement, ParsedBpmn


# Style strings - draw.io style format. Kept minimal and readable.
STYLES = {
    "actor_intern":   "rounded=1;fillColor=#dae8fc;strokeColor=#6c8ebf;fontSize=12;",
    "actor_extern":   "rounded=1;fillColor=#f8cecc;strokeColor=#b85450;fontSize=12;",
    "task":           "rounded=1;whiteSpace=wrap;html=1;fillColor=#fff2cc;strokeColor=#d6b656;fontSize=11;",
    "data_object":    "shape=note;whiteSpace=wrap;html=1;fillColor=#d5e8d4;strokeColor=#82b366;fontSize=10;",
    "anchor_object":  "shape=note;whiteSpace=wrap;html=1;fillColor=#fa6800;strokeColor=#a04000;fontColor=#ffffff;fontStyle=1;fontSize=11;",
    "data_store":     "shape=cylinder3;whiteSpace=wrap;html=1;fillColor=#e1d5e7;strokeColor=#9673a6;fontSize=10;",
    "gateway":        "rhombus;whiteSpace=wrap;html=1;fillColor=#fff;strokeColor=#000;fontSize=10;",
    "event":          "ellipse;whiteSpace=wrap;html=1;fillColor=#f5f5f5;strokeColor=#666;fontSize=10;",
    "annotation":     "shape=note;whiteSpace=wrap;html=1;fillColor=#f8f8f8;strokeColor=#999;fontSize=9;align=left;",
    "header":         "text;html=1;strokeColor=none;fillColor=none;fontSize=14;fontStyle=1;align=left;",
    "entity":         "rounded=0;whiteSpace=wrap;html=1;fillColor=#dae8fc;strokeColor=#6c8ebf;fontSize=11;verticalAlign=top;",
    "anchor_entity":  "rounded=0;whiteSpace=wrap;html=1;fillColor=#fa6800;strokeColor=#a04000;fontColor=#ffffff;fontSize=11;verticalAlign=top;fontStyle=1;",
    "edge":           "endArrow=classic;html=1;",
}


# --- Tooltip building ------------------------------------------------------

def _tooltip(lines: list[str]) -> str:
    """draw.io expects HTML in the tooltip attr. Keep it readable."""
    body = "<br/>".join(html.escape(l) for l in lines if l)
    return body


def tooltip_for_element(el: BpmnElement, extra: list[str] | None = None) -> str:
    lines = [
        f"BPMN-element: <{el.evidence.get('xml_tag', el.kind)}>",
        f"Naam: {el.name}",
        f"BPMN-id: {el.id}",
    ]
    if el.subtype and el.subtype != el.kind:
        lines.append(f"Subtype: {el.subtype}")
    reason = el.evidence.get("reason")
    if reason:
        lines.append("")
        lines.append("Hoe geclassificeerd:")
        lines.append(reason)
    extras = el.evidence.get("classification_reason")
    if extras:
        lines.append(extras)
    if extra:
        lines.append("")
        lines.extend(extra)
    return _tooltip(lines)


# --- mxGraph XML emission --------------------------------------------------

@dataclass
class _Cell:
    cell_id: str
    label: str
    style: str
    x: float
    y: float
    w: float
    h: float
    tooltip: str = ""
    parent: str = "1"


def _emit_cell(parent: ET.Element, cell: _Cell) -> None:
    """Emit a vertex cell, wrapped in <UserObject> when there is a tooltip."""
    if cell.tooltip:
        user_obj = ET.SubElement(parent, "UserObject", {
            "label": cell.label,
            "tooltip": cell.tooltip,
            "id": cell.cell_id,
        })
        mxcell = ET.SubElement(user_obj, "mxCell", {
            "style": cell.style,
            "vertex": "1",
            "parent": cell.parent,
        })
    else:
        mxcell = ET.SubElement(parent, "mxCell", {
            "id": cell.cell_id,
            "value": cell.label,
            "style": cell.style,
            "vertex": "1",
            "parent": cell.parent,
        })
    ET.SubElement(mxcell, "mxGeometry", {
        "x": str(cell.x), "y": str(cell.y),
        "width": str(cell.w), "height": str(cell.h),
        "as": "geometry",
    })


def _emit_edge(parent: ET.Element, edge_id: str, src: str, tgt: str,
               label: str = "", tooltip: str = "") -> None:
    style = STYLES["edge"]
    if tooltip:
        user_obj = ET.SubElement(parent, "UserObject", {
            "label": label, "tooltip": tooltip, "id": edge_id,
        })
        ET.SubElement(user_obj, "mxCell", {
            "style": style, "edge": "1", "parent": "1",
            "source": src, "target": tgt,
        }).append(_edge_geom())
    else:
        cell = ET.SubElement(parent, "mxCell", {
            "id": edge_id, "value": label, "style": style,
            "edge": "1", "parent": "1", "source": src, "target": tgt,
        })
        cell.append(_edge_geom())


def _edge_geom() -> ET.Element:
    geom = ET.Element("mxGeometry", {"relative": "1", "as": "geometry"})
    return geom


# --- Page builders ---------------------------------------------------------

def _build_bpmn_page(model: MergedModel) -> ET.Element:
    """One page showing each BPMN as a vertical strip side by side."""
    diagram = ET.Element("diagram", {"id": "page-bpmn", "name": "BPMN samengevoegd"})
    graph = ET.SubElement(diagram, "mxGraphModel", {
        "dx": "1422", "dy": "754", "grid": "1", "gridSize": "10",
        "guides": "1", "tooltips": "1", "connect": "1", "arrows": "1",
        "fold": "1", "page": "1", "pageScale": "1", "pageWidth": "5500",
        "pageHeight": "2000", "math": "0", "shadow": "0",
    })
    root = ET.SubElement(graph, "root")
    ET.SubElement(root, "mxCell", {"id": "0"})
    ET.SubElement(root, "mxCell", {"id": "1", "parent": "0"})

    anchors_lower = {a.lower() for a in model.anchor_objects()}
    cell_counter = [10]

    def next_id(prefix: str) -> str:
        cell_counter[0] += 1
        return f"{prefix}_{cell_counter[0]}"

    x_offset = 50
    column_w = 380
    for parsed in model.bpmns:
        # Header per process
        header = _Cell(
            cell_id=next_id("hdr"),
            label=f"<b>{html.escape(parsed.process_name)}</b><br/>"
                  f"<i>{html.escape(parsed.source_file)}</i>",
            style=STYLES["header"],
            x=x_offset, y=20, w=column_w - 20, h=50,
            tooltip=_tooltip([
                f"Bron: {parsed.source_file}",
                f"Process-id: {parsed.process_name}",
                f"Pools: {len(parsed.participants)}, "
                f"lanes: {len(parsed.lanes)}, "
                f"tasks: {len(parsed.tasks)}, "
                f"dataObjects: {len(parsed.data_objects)}, "
                f"gateways: {len(parsed.gateways)}, "
                f"events: {len(parsed.events)}",
            ]),
        )
        _emit_cell(root, header)

        y = 90
        # Lanes (actors)
        for lane in parsed.lanes:
            cell = _Cell(
                cell_id=next_id("lane"),
                label=f"👤 {html.escape(lane.name)}",
                style=STYLES["actor_intern"],
                x=x_offset, y=y, w=column_w - 20, h=30,
                tooltip=tooltip_for_element(lane, extra=[
                    "Type in inventarisatie: Actor (intern)",
                ]),
            )
            _emit_cell(root, cell)
            y += 40

        # Tasks
        task_cells: dict[str, str] = {}
        for task in parsed.tasks:
            cid = next_id("task")
            task_cells[task.id] = cid
            cell = _Cell(
                cell_id=cid,
                label=html.escape(task.name),
                style=STYLES["task"],
                x=x_offset + 20, y=y, w=column_w - 60, h=40,
                tooltip=tooltip_for_element(task, extra=[
                    f"Lane: {task.lane_id or '(geen lane)'}",
                    f"Data inputs: {len(task.attributes.get('data_inputs', []))}",
                    f"Data outputs: {len(task.attributes.get('data_outputs', []))}",
                    "Type in inventarisatie: Processtap",
                ]),
            )
            _emit_cell(root, cell)
            y += 50

        # Data objects
        for d in parsed.data_objects:
            is_anchor = d.name.lower() in anchors_lower
            cid = next_id("data")
            label = ("⚓ " if is_anchor else "") + html.escape(d.name)
            tip_extra = [
                "Type in inventarisatie: Entiteit / dataObject",
            ]
            if is_anchor:
                files = sorted({sf for sf, _ in
                                model.data_object_index[d.name.lower()]})
                tip_extra.append(
                    f"⚓ ANKEROBJECT — komt voor in {len(files)} BPMN's: "
                    + ", ".join(files)
                )
            cell = _Cell(
                cell_id=cid,
                label=label,
                style=STYLES["anchor_object"] if is_anchor else STYLES["data_object"],
                x=x_offset + column_w + 20, y=y - 50, w=column_w - 80, h=40,
                tooltip=tooltip_for_element(d, extra=tip_extra),
            )
            _emit_cell(root, cell)
            y += 50

        # Gateways (compact list under tasks)
        for gw in parsed.gateways:
            cid = next_id("gw")
            cell = _Cell(
                cell_id=cid,
                label=f"◆ {html.escape(gw.name or gw.short_id())}",
                style=STYLES["gateway"],
                x=x_offset + 20, y=y, w=column_w - 60, h=35,
                tooltip=tooltip_for_element(gw, extra=[
                    "Type in inventarisatie: Procesattribuut",
                ]),
            )
            _emit_cell(root, cell)
            y += 45

        # Annotations (only count, not full content - too noisy)
        if parsed.annotations:
            cid = next_id("notes")
            text = "\n".join(a.attributes.get("text", "")
                             for a in parsed.annotations[:3])
            cell = _Cell(
                cell_id=cid,
                label=f"📝 {len(parsed.annotations)} text annotation(s)",
                style=STYLES["annotation"],
                x=x_offset + 20, y=y, w=column_w - 60, h=50,
                tooltip=_tooltip([
                    f"{len(parsed.annotations)} <bpmn:textAnnotation> "
                    f"elementen in dit bestand.",
                    "Voorbeeld(en):",
                    text[:300],
                    "",
                    "Type in inventarisatie: Opmerking / business rule",
                ]),
            )
            _emit_cell(root, cell)

        x_offset += column_w * 2 + 40

    return diagram


def _build_erd_page(model: MergedModel) -> ET.Element:
    """ERD page: one entity per data object, edges for co-occurrence."""
    diagram = ET.Element("diagram", {"id": "page-erd", "name": "ERD"})
    graph = ET.SubElement(diagram, "mxGraphModel", {
        "dx": "1422", "dy": "754", "grid": "1", "gridSize": "10",
        "guides": "1", "tooltips": "1", "connect": "1", "arrows": "1",
        "fold": "1", "page": "1", "pageScale": "1", "pageWidth": "1700",
        "pageHeight": "1100", "math": "0", "shadow": "0",
    })
    root = ET.SubElement(graph, "root")
    ET.SubElement(root, "mxCell", {"id": "0"})
    ET.SubElement(root, "mxCell", {"id": "1", "parent": "0"})

    # Header
    _emit_cell(root, _Cell(
        cell_id="erd_hdr",
        label="<b>ERD afgeleid uit BPMN data-objecten</b><br/>"
              "<i>Oranje = ankerobject (komt in ≥2 processen voor)</i>",
        style=STYLES["header"], x=40, y=20, w=900, h=40,
        tooltip=_tooltip([
            "Methodiek:",
            "1. Verzamel alle <bpmn:dataObject> en <bpmn:dataObjectReference>.",
            "2. Cluster op naam (case-insensitive) over alle BPMN-bestanden.",
            "3. Markeer een naam als 'anker' als hij in ≥2 verschillende "
            "BPMN's voorkomt.",
            "4. Attributen: voor elk dataObject worden alle taken die het via "
            "dataInput/Output-association raken als 'gebruik door' relatie "
            "geregistreerd.",
        ]),
    ))

    # Collect attributes (= tasks that touch each data object) per name
    attrs_by_name: dict[str, set[str]] = defaultdict(set)
    files_by_name: dict[str, set[str]] = defaultdict(set)
    for parsed in model.bpmns:
        # Build local task lookup
        task_by_id = {t.id: t.name for t in parsed.tasks}
        for assoc in parsed.data_associations:
            src = assoc.attributes.get("source", "")
            tgt = assoc.attributes.get("target", "")
            data_id = src if assoc.subtype == "dataInputAssociation" else tgt
            task_id = tgt if assoc.subtype == "dataInputAssociation" else src
            data_obj = next((d for d in parsed.data_objects if d.id == data_id), None)
            task_name = task_by_id.get(task_id, "")
            if data_obj and task_name:
                attrs_by_name[data_obj.name.lower()].add(task_name)
        for d in parsed.data_objects:
            if d.name and not d.name.startswith("(naamloos"):
                files_by_name[d.name.lower()].add(parsed.source_file)

    # Emit one rectangle per unique data object
    anchors = {a.lower() for a in model.anchor_objects()}
    cols = 4
    box_w, box_h = 280, 140
    pad_x, pad_y = 40, 80
    seen_names = sorted(files_by_name.keys())
    name_to_id: dict[str, str] = {}

    for i, name_lower in enumerate(seen_names):
        # Original casing from first occurrence
        original = next(
            (d.name for parsed in model.bpmns for d in parsed.data_objects
             if d.name.lower() == name_lower),
            name_lower,
        )
        is_anchor = name_lower in anchors
        col = i % cols
        row = i // cols
        x = 40 + col * (box_w + pad_x)
        y = 90 + row * (box_h + pad_y)

        attr_list = sorted(attrs_by_name.get(name_lower, []))
        attr_text = "<br/>".join(f"• {html.escape(a)}" for a in attr_list[:6])
        label = (f"<b>{'⚓ ' if is_anchor else ''}{html.escape(original)}</b>"
                 f"<hr/>{attr_text or '<i>(geen taak-koppelingen)</i>'}")

        cell_id = f"ent_{i}"
        name_to_id[name_lower] = cell_id
        _emit_cell(root, _Cell(
            cell_id=cell_id, label=label,
            style=STYLES["anchor_entity"] if is_anchor else STYLES["entity"],
            x=x, y=y, w=box_w, h=box_h,
            tooltip=_tooltip([
                f"Entiteit afgeleid uit dataObject(Reference) '{original}'",
                f"Voorkomens: {len(files_by_name[name_lower])} BPMN-bestand(en)",
                "Bestanden: " + ", ".join(sorted(files_by_name[name_lower])),
                f"Status: {'ANKEROBJECT' if is_anchor else 'lokaal object'}",
                "",
                "Hoe afgeleid:",
                "Het BPMN-element <bpmn:dataObject> of "
                "<bpmn:dataObjectReference> wordt geprojecteerd op een "
                "entiteit in de ERD. Alle activiteiten die dit object via "
                "een dataInput/Output-association raken, worden als "
                "kandidaat-attributen / -relaties weergegeven.",
            ]),
        ))

    # Relationships: connect entities that share a task in any BPMN
    edges_drawn: set[tuple[str, str]] = set()
    edge_count = 0
    for parsed in model.bpmns:
        # task_id -> set(data_obj_name_lower)
        task_to_objs: dict[str, set[str]] = defaultdict(set)
        for assoc in parsed.data_associations:
            src = assoc.attributes.get("source", "")
            tgt = assoc.attributes.get("target", "")
            data_id = src if assoc.subtype == "dataInputAssociation" else tgt
            task_id = tgt if assoc.subtype == "dataInputAssociation" else src
            data_obj = next((d for d in parsed.data_objects if d.id == data_id), None)
            if data_obj and task_id:
                task_to_objs[task_id].add(data_obj.name.lower())
        for objs in task_to_objs.values():
            objs_list = sorted(objs)
            for i, a in enumerate(objs_list):
                for b in objs_list[i + 1:]:
                    pair = tuple(sorted((a, b)))
                    if pair in edges_drawn:
                        continue
                    edges_drawn.add(pair)
                    if a in name_to_id and b in name_to_id:
                        edge_count += 1
                        _emit_edge(
                            root, f"erd_edge_{edge_count}",
                            name_to_id[a], name_to_id[b],
                            tooltip=_tooltip([
                                f"Relatie: {a} ↔ {b}",
                                f"Afgeleid uit gedeelde taak in "
                                f"{parsed.source_file}",
                                "Methodiek: twee dataObjects die door dezelfde "
                                "activiteit worden gebruikt of geproduceerd "
                                "krijgen een ongerichte relatie.",
                            ]),
                        )
    return diagram


def write_drawio(model: MergedModel, out_path: str) -> None:
    mxfile = ET.Element("mxfile", {
        "host": "Electron",
        "agent": "bpmn_inventory_export",
        "type": "device",
    })
    mxfile.append(_build_bpmn_page(model))
    mxfile.append(_build_erd_page(model))
    tree = ET.ElementTree(mxfile)
    tree.write(out_path, encoding="utf-8", xml_declaration=True)
