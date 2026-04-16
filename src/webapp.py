"""
Flask web-frontend voor de BPMN Data-Inventarisatie tool.

Start met:  python src/webapp.py           (poort 8095)
Of via:     run_web.bat                    (dubbelklik in Verkenner)

Endpoints:
    GET  /                          upload-pagina
    POST /run                       verwerk geuploade .bpmn's
    GET  /session/<sid>             resultaten-pagina
    GET  /session/<sid>/bpmn/<f>    raw .bpmn XML (voor bpmn-js)
    GET  /session/<sid>/download/<f>
"""

from __future__ import annotations

import json
import re
import sys
import uuid
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path

from flask import (Flask, abort, jsonify, redirect, render_template, request,
                   send_from_directory, url_for)
from werkzeug.utils import secure_filename

# Zorg dat sibling-modules importeerbaar zijn
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bpmn_parser import parse_all                        # noqa: E402
from merger import merge                                 # noqa: E402
from xlsx_export import write_xlsx                       # noqa: E402
from drawio_export import write_drawio                   # noqa: E402
from docx_export import write_docx                       # noqa: E402
from bpmn_review import review, summarize                # noqa: E402
import bpmn_apply                                        # noqa: E402
import bpmn_defs                                         # noqa: E402


ROOT = Path(__file__).resolve().parent.parent
SESSIONS_DIR = ROOT / "output" / "sessions"
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_EXT = {".bpmn", ".xml"}
MAX_FILE_MB = 25

app = Flask(
    __name__,
    template_folder=str(ROOT / "templates"),
    static_folder=str(ROOT / "static"),
)
app.config["MAX_CONTENT_LENGTH"] = MAX_FILE_MB * 1024 * 1024 * 20  # 20 bestanden


# ---------------------------------------------------------------------------
# Mermaid ERD builder
# ---------------------------------------------------------------------------

_ID_CLEAN = re.compile(r"[^A-Za-z0-9]+")


def _mermaid_id(name: str) -> str:
    """Maak een veilige mermaid-entity-naam (geen spaties/leestekens)."""
    clean = _ID_CLEAN.sub("_", name).strip("_") or "ENTITY"
    if clean[0].isdigit():
        clean = "E_" + clean
    return clean[:40]


def build_erd_mermaid(model) -> str:
    """Genereer een mermaid erDiagram uit de merged model.

    Entiteiten = unieke dataobject-namen.
    Relaties   = elke taak die twee dataobjects gebruikt (via
                 dataInput/OutputAssociation) creeert een relatie tussen
                 die twee entiteiten.
    """
    # Collect unique entities by display name
    entity_by_name: dict[str, dict] = {}
    anchors = {a.lower() for a in model.anchor_objects()}

    for parsed in model.bpmns:
        for d in parsed.data_objects:
            nm = (d.name or "").strip()
            if not nm or nm.startswith("(naamloos"):
                continue
            key = nm.lower()
            if key not in entity_by_name:
                entity_by_name[key] = {
                    "name": nm,
                    "id": _mermaid_id(nm),
                    "processes": set(),
                    "is_anchor": key in anchors,
                }
            entity_by_name[key]["processes"].add(parsed.process_name or parsed.source_file)

    # Collect co-occurrences per task (input/output of same task)
    relations: set[tuple[str, str]] = set()
    for parsed in model.bpmns:
        by_id = {d.id: d for d in parsed.data_objects}
        task_to_objs: dict[str, list[str]] = defaultdict(list)
        for assoc in parsed.data_associations:
            src = assoc.attributes.get("source", "")
            tgt = assoc.attributes.get("target", "")
            if assoc.subtype == "dataInputAssociation":
                data_el = by_id.get(src); task_id = tgt
            else:
                data_el = by_id.get(tgt) or by_id.get(src); task_id = src
            if data_el and data_el.name and task_id:
                task_to_objs[task_id].append(data_el.name.lower())

        for objs in task_to_objs.values():
            uniq = sorted(set(objs))
            for i in range(len(uniq)):
                for j in range(i + 1, len(uniq)):
                    relations.add((uniq[i], uniq[j]))

    if not entity_by_name:
        return ""

    # Build mermaid string
    lines = ["erDiagram"]
    for entity in sorted(entity_by_name.values(), key=lambda e: e["name"]):
        anchor_mark = "  anchor" if entity["is_anchor"] else ""
        processes_str = ", ".join(sorted(entity["processes"]))[:60]
        lines.append(f"    {entity['id']} {{")
        lines.append(f"        string naam \"{entity['name']}\"")
        if entity["is_anchor"]:
            lines.append(f"        string type \"Ankerobject\"")
        if processes_str:
            # escape dubbele quotes
            safe = processes_str.replace('"', "'")
            lines.append(f"        string proces \"{safe}\"")
        lines.append("    }")

    for a, b in sorted(relations):
        ea = entity_by_name.get(a)
        eb = entity_by_name.get(b)
        if ea and eb:
            lines.append(f'    {ea["id"]} }}o--o{{ {eb["id"]} : "gedeelde taak"')

    # Als er geen relaties zijn, voeg dan ankers-centrale links toe
    if not relations and len(entity_by_name) > 1:
        anchor_entities = [e for e in entity_by_name.values() if e["is_anchor"]]
        if anchor_entities:
            center = anchor_entities[0]
            for e in entity_by_name.values():
                if e is center:
                    continue
                lines.append(f'    {center["id"]} }}o--|| {e["id"]} : "co-proces"')

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", max_mb=MAX_FILE_MB)


@app.route("/run", methods=["POST"])
def run_pipeline():
    files = request.files.getlist("bpmn_files")
    files = [f for f in files if f and f.filename]
    if not files:
        return render_template("index.html",
                               error="Geen bestanden geupload.",
                               max_mb=MAX_FILE_MB), 400

    for f in files:
        ext = Path(f.filename).suffix.lower()
        if ext not in ALLOWED_EXT:
            return render_template(
                "index.html",
                error=f"Bestand '{f.filename}' heeft geen .bpmn/.xml extensie.",
                max_mb=MAX_FILE_MB,
            ), 400

    sid = uuid.uuid4().hex[:12]
    sdir = SESSIONS_DIR / sid
    data_dir = sdir / "data"
    out_dir = sdir / "output"
    data_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    saved_files: list[str] = []
    for f in files:
        safe_name = secure_filename(f.filename)
        # secure_filename can return '' for pathologic names; fallback
        if not safe_name:
            safe_name = f"upload_{len(saved_files)+1}.bpmn"
        dest = data_dir / safe_name
        f.save(str(dest))
        saved_files.append(safe_name)

    bpmns = parse_all(data_dir)
    if not bpmns:
        return render_template(
            "index.html",
            error="Geen geldige BPMN 2.0 bestanden herkend.",
            max_mb=MAX_FILE_MB,
        ), 400

    model = merge(bpmns)
    user_defs = bpmn_defs.load(ROOT)
    findings = review(model, user_defs=user_defs)
    findings_summary = summarize(findings)

    # Zorg dat iedere BPMN een v1 (origineel) heeft in versions/
    for f in saved_files:
        bpmn_apply.ensure_v1(sdir, f)

    xlsx_path = out_dir / "data-inventarisatie.xlsx"
    drawio_path = out_dir / "bpmn-en-erd.drawio"
    docx_path = out_dir / "rapport.docx"
    json_path = out_dir / "inventory.json"

    write_xlsx(model, str(xlsx_path))
    write_drawio(model, str(drawio_path))
    write_docx(model, str(docx_path))

    with json_path.open("w", encoding="utf-8") as fh:
        json.dump({
            "files": [b.source_file for b in bpmns],
            "actors": [{"name": a.name, "type": a.subtype,
                        "appears_in": a.evidence.get("appears_in", [])}
                       for a in model.actors],
            "anchors": model.anchor_objects(),
            "inventory": [asdict(r) for r in model.inventory],
        }, fh, indent=2, ensure_ascii=False)

    # Versies per bestand
    versions_by_file = {f: bpmn_apply.load_versions(sdir, f) for f in saved_files}

    # Summary voor resultaten-pagina (inclusief rapport-data)
    summary = {
        "sid": sid,
        "bpmn_files": saved_files,
        "versions": versions_by_file,
        "url_base": f"/session/{sid}",
        "files": [{
            "name": b.source_file,
            "process": b.process_name,
            "tasks": len(b.tasks),
            "data_objects": len(b.data_objects),
            "lanes": len(b.lanes),
            "gateways": len(b.gateways),
            "events": len(b.events),
        } for b in bpmns],
        "totals": {
            "files": len(bpmns),
            "actors": len(model.actors),
            "anchors": len(model.anchor_objects()),
            "rows": len(model.inventory),
            "tasks": sum(len(b.tasks) for b in bpmns),
            "data_objects": sum(len(b.data_objects) for b in bpmns),
        },
        "actors": [{
            "name": a.name,
            "type": "Extern" if a.subtype == "extern" else "Intern",
            "appears_in": a.evidence.get("appears_in", []),
            "reason": a.evidence.get("classification_reason", ""),
        } for a in model.actors],
        "anchors": [
            {"name": name,
             "processes": sorted({sf for sf, _ in model.data_object_index[name.lower()]})}
            for name in model.anchor_objects()
        ],
        "inventory": [asdict(r) for r in model.inventory],
        "mermaid_erd": build_erd_mermaid(model),
        "findings": findings,
        "findings_summary": findings_summary,
        # Rapport-secties per BPMN (voor inline HTML rapport)
        "report_per_bpmn": [{
            "name": b.source_file,
            "process": b.process_name,
            "lanes": [l.name for l in b.lanes],
            "external_actors": [p.name for p in b.participants
                                if not p.attributes.get("processRef")],
            "tasks": [{"id": f"A{i+1}", "name": t.name,
                       "subtype": t.subtype, "lane": t.lane_id or ""}
                      for i, t in enumerate(b.tasks)],
            "data_objects": [{"name": d.name, "subtype": d.subtype, "id": d.id}
                             for d in b.data_objects],
            "annotations": [a.attributes.get("text", "") for a in b.annotations],
        } for b in model.bpmns],
    }
    with (out_dir / "summary.json").open("w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, ensure_ascii=False)

    return redirect(url_for("session_view", sid=sid))


@app.route("/session/<sid>")
def session_view(sid: str):
    if not _is_valid_sid(sid):
        abort(404)
    summary_path = SESSIONS_DIR / sid / "output" / "summary.json"
    if not summary_path.exists():
        abort(404)
    with summary_path.open("r", encoding="utf-8") as fh:
        summary = json.load(fh)

    # Defensieve defaults voor sessies die nog zijn gegenereerd met een
    # oudere versie van de pipeline (en dus sommige velden missen).
    summary.setdefault("bpmn_files", [])
    summary.setdefault("mermaid_erd", "")
    summary.setdefault("report_per_bpmn", [])
    summary.setdefault("findings", [])
    summary.setdefault("findings_summary", {
        "total": 0,
        "by_severity": {},
        "by_rule": {},
        "rules_catalog": [],
    })
    summary.setdefault("actors", [])
    summary.setdefault("anchors", [])
    summary.setdefault("inventory", [])
    summary.setdefault("files", [])
    summary.setdefault("totals", {})
    summary.setdefault("url_base", f"/session/{sid}")
    summary.setdefault("versions", {})
    for k in ("files", "actors", "anchors", "rows", "tasks", "data_objects"):
        summary["totals"].setdefault(k, 0)
    # 'anchors' kan in oude summaries een list[str] zijn; normaliseer naar dicts
    if summary["anchors"] and isinstance(summary["anchors"][0], str):
        summary["anchors"] = [{"name": n, "processes": []}
                              for n in summary["anchors"]]

    classification_counts: dict[str, int] = {}
    for row in summary["inventory"]:
        classification_counts[row["classification"]] = \
            classification_counts.get(row["classification"], 0) + 1

    return render_template(
        "results.html",
        summary=summary,
        classification_counts=classification_counts,
    )


def _regenerate_session_summary(sid: str) -> dict:
    """Herbouw summary.json voor een sessie (na fix / nieuwe versie).

    Leest alle .bpmn uit data/, draait pipeline + review en overschrijft
    summary.json. Returns de nieuwe summary dict.
    """
    sdir = SESSIONS_DIR / sid
    data_dir = sdir / "data"
    out_dir = sdir / "output"
    out_dir.mkdir(parents=True, exist_ok=True)

    bpmns = parse_all(data_dir)
    if not bpmns:
        raise ValueError("Geen BPMNs meer in deze sessie")

    model = merge(bpmns)
    user_defs = bpmn_defs.load(ROOT)
    findings = review(model, user_defs=user_defs)
    findings_summary = summarize(findings)

    saved_files = [p.name for p in sorted(data_dir.glob("*.bpmn"))] + \
                  [p.name for p in sorted(data_dir.glob("*.xml"))]

    # Regenereer de belangrijkste artifacts
    write_xlsx(model, str(out_dir / "data-inventarisatie.xlsx"))
    write_drawio(model, str(out_dir / "bpmn-en-erd.drawio"))
    write_docx(model, str(out_dir / "rapport.docx"))
    with (out_dir / "inventory.json").open("w", encoding="utf-8") as fh:
        json.dump({"files": [b.source_file for b in bpmns],
                   "inventory": [asdict(r) for r in model.inventory]},
                  fh, indent=2, ensure_ascii=False)

    versions_by_file = {f: bpmn_apply.load_versions(sdir, f)
                        for f in saved_files}

    summary = {
        "sid": sid,
        "bpmn_files": saved_files,
        "versions": versions_by_file,
        "url_base": f"/session/{sid}",
        "files": [{"name": b.source_file, "process": b.process_name,
                   "tasks": len(b.tasks),
                   "data_objects": len(b.data_objects),
                   "lanes": len(b.lanes),
                   "gateways": len(b.gateways),
                   "events": len(b.events)} for b in bpmns],
        "totals": {"files": len(bpmns),
                   "actors": len(model.actors),
                   "anchors": len(model.anchor_objects()),
                   "rows": len(model.inventory),
                   "tasks": sum(len(b.tasks) for b in bpmns),
                   "data_objects": sum(len(b.data_objects) for b in bpmns)},
        "actors": [{"name": a.name,
                    "type": "Extern" if a.subtype == "extern" else "Intern",
                    "appears_in": a.evidence.get("appears_in", []),
                    "reason": a.evidence.get("classification_reason", "")}
                   for a in model.actors],
        "anchors": [{"name": n,
                     "processes": sorted({sf for sf, _ in model.data_object_index[n.lower()]})}
                    for n in model.anchor_objects()],
        "inventory": [asdict(r) for r in model.inventory],
        "mermaid_erd": "",
        "findings": findings,
        "findings_summary": findings_summary,
        "report_per_bpmn": [{
            "name": b.source_file, "process": b.process_name,
            "lanes": [l.name for l in b.lanes],
            "external_actors": [p.name for p in b.participants
                                if not p.attributes.get("processRef")],
            "tasks": [{"id": f"A{i+1}", "name": t.name,
                       "subtype": t.subtype, "lane": t.lane_id or ""}
                      for i, t in enumerate(b.tasks)],
            "data_objects": [{"name": d.name, "subtype": d.subtype, "id": d.id}
                             for d in b.data_objects],
            "annotations": [a.attributes.get("text", "") for a in b.annotations],
        } for b in model.bpmns],
    }

    # Voeg mermaid ERD terug (oude webapp had een build_erd_mermaid)
    try:
        summary["mermaid_erd"] = build_erd_mermaid(model)
    except Exception:
        summary["mermaid_erd"] = ""

    with (out_dir / "summary.json").open("w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, ensure_ascii=False)
    return summary


@app.route("/session/<sid>/apply", methods=["POST"])
def session_apply(sid: str):
    """Pas een fix toe op een .bpmn. Body: JSON met
    `file`, `task_id`, `object`, `action`, `attributes` (optioneel)."""
    if not _is_valid_sid(sid):
        abort(404)
    sdir = SESSIONS_DIR / sid
    if not sdir.exists():
        abort(404)

    payload = request.get_json(silent=True) or {}
    file_name = secure_filename(payload.get("file", ""))
    task_id   = payload.get("task_id", "")
    obj       = payload.get("object", "")
    action    = (payload.get("action") or "WRITE").upper()
    attrs     = payload.get("attributes") or []

    if not (file_name and task_id and obj and action in ("READ", "WRITE", "UPDATE")):
        return jsonify({"error": "Onvolledige payload. "
                        "Verwacht file, task_id, object, action (READ/WRITE/UPDATE)."}), 400

    data_file = sdir / "data" / file_name
    if not data_file.exists():
        return jsonify({"error": f"Bestand {file_name} niet gevonden"}), 404

    # Pas fix toe op actieve bestand (overschrijft het)
    try:
        bpmn_apply.apply_add_dataobject(
            data_file, task_id=task_id,
            object_name=obj, action_type=action, attributes=attrs,
        )
    except Exception as e:
        return jsonify({"error": f"Fix mislukte: {e}"}), 500

    # Nieuwe versie registreren
    bpmn_apply.ensure_v1(sdir, file_name)
    entry = bpmn_apply.add_fix_version(
        sdir, file_name,
        patched_bytes=data_file.read_bytes(),
        description=f"Toegevoegd dataObject '{obj}' ({action}) aan task {task_id}",
        applied_finding=payload,
    )

    # Summary opnieuw opbouwen
    try:
        summary = _regenerate_session_summary(sid)
    except Exception as e:
        return jsonify({"error": f"Summary-regeneratie mislukte: {e}"}), 500

    return jsonify({
        "ok": True,
        "new_version": entry,
        "findings_total": summary["findings_summary"]["total"],
        "redirect": f"/session/{sid}#panel=bpmn",
    })


@app.route("/session/<sid>/upload-version", methods=["POST"])
def session_upload_version(sid: str):
    """Upload een nieuwe versie van een bestaand .bpmn-bestand."""
    if not _is_valid_sid(sid):
        abort(404)
    sdir = SESSIONS_DIR / sid
    if not sdir.exists():
        abort(404)

    target = request.form.get("target")
    f = request.files.get("bpmn_file")
    if not target or not f:
        return "Geef target en bpmn_file mee.", 400

    target = secure_filename(target)
    if not (sdir / "data" / target).exists():
        return f"Onbekend target-bestand: {target}", 404

    if Path(f.filename).suffix.lower() not in ALLOWED_EXT:
        return "Alleen .bpmn of .xml toegestaan.", 400

    bpmn_apply.ensure_v1(sdir, target)
    bpmn_apply.add_upload_version(
        sdir, target,
        bytes_=f.read(),
        description=f"Nieuwe upload ({f.filename})",
    )
    try:
        _regenerate_session_summary(sid)
    except Exception as e:
        return f"Regeneratie mislukt: {e}", 500
    return redirect(url_for("session_view", sid=sid))


@app.route("/session/<sid>/activate", methods=["POST"])
def session_activate_version(sid: str):
    if not _is_valid_sid(sid):
        abort(404)
    sdir = SESSIONS_DIR / sid
    payload = request.get_json(silent=True) or {}
    file_name = secure_filename(payload.get("file", ""))
    version = int(payload.get("version", 0) or 0)
    if not (file_name and version):
        return jsonify({"error": "Geef file + version"}), 400
    ok = bpmn_apply.activate_version(sdir, file_name, version)
    if not ok:
        return jsonify({"error": "Versie niet gevonden"}), 404
    try:
        _regenerate_session_summary(sid)
    except Exception as e:
        return jsonify({"error": f"Regeneratie mislukt: {e}"}), 500
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Definities (user-dictionary)
# ---------------------------------------------------------------------------

@app.route("/definitions", methods=["GET"])
def definitions_view():
    data = bpmn_defs.load(ROOT)
    return render_template("definitions.html", defs=data)


@app.route("/definitions/api", methods=["GET"])
def definitions_api():
    return jsonify(bpmn_defs.load(ROOT))


@app.route("/definitions/object", methods=["POST"])
def definitions_upsert_object():
    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    attrs = payload.get("attributes") or []
    if not name:
        return jsonify({"error": "Geef een naam"}), 400
    try:
        bpmn_defs.upsert_object(ROOT, name, attrs)
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "data": bpmn_defs.load(ROOT)})


@app.route("/definitions/object/<name>", methods=["DELETE"])
def definitions_delete_object(name: str):
    bpmn_defs.delete_object(ROOT, name)
    return jsonify({"ok": True, "data": bpmn_defs.load(ROOT)})


@app.route("/session/<sid>/bpmn/<path:filename>")
def session_bpmn_raw(sid: str, filename: str):
    """Serveer een geuploade .bpmn file als XML voor bpmn-js."""
    if not _is_valid_sid(sid):
        abort(404)
    data_dir = SESSIONS_DIR / sid / "data"
    safe = secure_filename(filename)
    if not safe or not (data_dir / safe).exists():
        abort(404)
    return send_from_directory(
        str(data_dir), safe, mimetype="application/xml"
    )


@app.route("/session/<sid>/download/<path:filename>")
def session_download(sid: str, filename: str):
    if not _is_valid_sid(sid):
        abort(404)
    out_dir = SESSIONS_DIR / sid / "output"
    if not out_dir.exists():
        abort(404)
    return send_from_directory(str(out_dir), filename, as_attachment=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_valid_sid(sid: str) -> bool:
    return bool(sid) and all(c in "0123456789abcdef" for c in sid) and len(sid) == 12


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = 8095
    print(f"BPMN Inventory webapp draait op http://localhost:{port}")
    app.run(host="127.0.0.1", port=port, debug=False)
