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
import bpmn_erd                                          # noqa: E402
import bpmn_project                                      # noqa: E402
import bpmn_docs                                         # noqa: E402


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


def _build_smart_erd(model, user_defs) -> dict:
    """Bouw het volledige datamodel via bpmn_erd en retourneer dict met
    mermaid, summary, entities, relationships en cross-BPMN findings."""
    entities, rels = bpmn_erd.build_erd(model, user_defs=user_defs)
    mermaid = bpmn_erd.to_mermaid(entities, rels)
    erd_summary = bpmn_erd.summarize(entities, rels)
    x_findings = bpmn_erd.cross_bpmn_findings(entities)
    return {
        "mermaid": mermaid,
        "summary": erd_summary,
        "cross_findings": x_findings,
    }


def build_erd_mermaid(model) -> str:
    """[DEPRECATED] Oude naïeve Mermaid-generator, bewaard voor backwards compat."""
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
    # Toont beide: projecten (persistent) + ad-hoc sessie upload
    projects = bpmn_project.list_projects(ROOT)
    return render_template("index.html", max_mb=MAX_FILE_MB,
                           projects=projects)


# ---------------------------------------------------------------------------
# Projecten (stap 1: create + list + detail + add-bpmn)
# ---------------------------------------------------------------------------

@app.route("/projects", methods=["GET"])
def projects_list():
    return jsonify(bpmn_project.list_projects(ROOT))


@app.route("/projects", methods=["POST"])
def projects_create():
    name = (request.form.get("name") or request.get_json(silent=True, force=False) or {}).get("name", "") if request.is_json else request.form.get("name", "")
    if request.is_json:
        name = (request.get_json(silent=True) or {}).get("name", "")
    if not name or not name.strip():
        return "Geef een projectnaam op.", 400
    meta = bpmn_project.create(ROOT, name)
    return redirect(url_for("project_detail", pid=meta["id"]))


@app.route("/project/<pid>")
def project_detail(pid: str):
    meta = bpmn_project.load(ROOT, pid)
    if meta is None:
        abort(404)
    return render_template("project.html", project=meta, max_mb=MAX_FILE_MB)


@app.route("/project/<pid>/add-bpmn", methods=["POST"])
def project_add_bpmn(pid: str):
    if not bpmn_project.is_valid_pid(pid):
        abort(404)
    meta = bpmn_project.load(ROOT, pid)
    if meta is None:
        abort(404)

    files = request.files.getlist("bpmn_files")
    files = [f for f in files if f and f.filename]
    if not files:
        return "Geen bestanden geüpload.", 400

    for f in files:
        ext = Path(f.filename).suffix.lower()
        if ext not in ALLOWED_EXT:
            return f"'{f.filename}' heeft geen .bpmn/.xml extensie.", 400

    data_dir = bpmn_project.project_data_dir(ROOT, pid)
    data_dir.mkdir(parents=True, exist_ok=True)

    for f in files:
        safe = secure_filename(f.filename) or f"upload_{uuid.uuid4().hex[:6]}.bpmn"
        dest = data_dir / safe
        # Als een file met deze naam al bestaat: nieuwe versie registreren
        # (het project_dir is zelf een sessie-achtige struct voor versioning)
        if dest.exists():
            # Vervang + registreer nieuwe upload-versie
            bpmn_apply.ensure_v1(bpmn_project.project_root_dir(ROOT, pid), safe)
            bpmn_apply.add_upload_version(
                bpmn_project.project_root_dir(ROOT, pid),
                safe, f.read(),
                description=f"Nieuwe upload ({f.filename})",
            )
        else:
            f.save(str(dest))
            # Registreer v1
            bpmn_apply.ensure_v1(bpmn_project.project_root_dir(ROOT, pid), safe)

    # Update bpmn_order zodat nieuwe files onderaan komen
    order = list(meta.get("bpmn_order", []))
    for name in bpmn_project.list_bpmns(ROOT, pid):
        if name not in order:
            order.append(name)
    meta["bpmn_order"] = order
    bpmn_project.save(ROOT, meta)

    return redirect(url_for("project_detail", pid=pid))


def _regenerate_project_summary(pid: str) -> dict:
    """Draai full pipeline op alle BPMNs in een project + schrijf summary.json.

    Spiegel van `_regenerate_session_summary` maar met projects als
    scope. url_base wordt /project/<pid> zodat results.html dezelfde
    apply/improved/bpmn-routes kan vinden.
    """
    meta = bpmn_project.load(ROOT, pid)
    if meta is None:
        raise ValueError("Project niet gevonden")
    pdir = bpmn_project.project_root_dir(ROOT, pid)
    data_dir = bpmn_project.project_data_dir(ROOT, pid)
    out_dir = bpmn_project.project_output_dir(ROOT, pid)

    bpmns = parse_all(data_dir)
    if not bpmns:
        raise ValueError("Geen BPMNs in dit project")

    model = merge(bpmns)
    bpmn_defs.auto_discover(ROOT, model, session_id=pid)
    user_defs = bpmn_defs.load(ROOT)
    findings = review(model, user_defs=user_defs)
    erd = _build_smart_erd(model, user_defs)
    findings = findings + erd["cross_findings"]
    findings_summary = summarize(findings)

    saved_files = [p.name for p in sorted(data_dir.glob("*.bpmn"))] + \
                  [p.name for p in sorted(data_dir.glob("*.xml"))]

    # Artifacts
    write_xlsx(model, str(out_dir / "data-inventarisatie.xlsx"))
    write_drawio(model, str(out_dir / "bpmn-en-erd.drawio"))
    write_docx(model, str(out_dir / "rapport.docx"))
    with (out_dir / "inventory.json").open("w", encoding="utf-8") as fh:
        json.dump({"files": [b.source_file for b in bpmns],
                   "inventory": [asdict(r) for r in model.inventory]},
                  fh, indent=2, ensure_ascii=False)

    # Zorg dat elke BPMN v1 heeft
    for f in saved_files:
        bpmn_apply.ensure_v1(pdir, f)
    versions_by_file = {f: bpmn_apply.load_versions(pdir, f)
                        for f in saved_files}

    summary = {
        "sid": pid,                     # results.html gebruikt 'sid' voor IDs
        "project_id": pid,
        "project_name": meta.get("name", ""),
        "url_base": f"/project/{pid}",
        "is_project": True,
        "bpmn_files": saved_files,
        "versions": versions_by_file,
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
        "mermaid_erd": erd["mermaid"],
        "erd": erd["summary"],
        "findings": findings,
        "findings_summary": findings_summary,
        "report_per_bpmn": [{
            "name": b.source_file, "process": b.process_name,
            "lanes": [l.name for l in b.lanes],
            "external_actors": [p.name for p in b.participants
                                if not p.attributes.get("processRef")],
            "documentation": getattr(b, "process_documentation", ""),
            "tasks": [{"id": f"A{i+1}", "name": t.name,
                       "subtype": t.subtype, "lane": t.lane_id or "",
                       "documentation": t.attributes.get("documentation", "")}
                      for i, t in enumerate(b.tasks)],
            "data_objects": [{"name": d.name, "subtype": d.subtype, "id": d.id}
                             for d in b.data_objects],
            "annotations": [a.attributes.get("text", "") for a in b.annotations],
        } for b in model.bpmns],
    }
    with (out_dir / "summary.json").open("w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, ensure_ascii=False)
    return summary


@app.route("/project/<pid>/analyze", methods=["POST"])
def project_analyze(pid: str):
    if not bpmn_project.is_valid_pid(pid):
        abort(404)
    meta = bpmn_project.load(ROOT, pid)
    if meta is None:
        abort(404)
    try:
        _regenerate_project_summary(pid)
    except ValueError as e:
        return str(e), 400
    except Exception as e:
        return f"Analyse mislukt: {e}", 500
    return redirect(url_for("project_results", pid=pid))


@app.route("/project/<pid>/results")
def project_results(pid: str):
    if not bpmn_project.is_valid_pid(pid):
        abort(404)
    summary_path = bpmn_project.project_output_dir(ROOT, pid) / "summary.json"
    if not summary_path.exists():
        # Analyse is nog niet gedraaid; stuur terug naar detail met hint
        return redirect(url_for("project_detail", pid=pid) + "?need_analyze=1")
    with summary_path.open("r", encoding="utf-8") as fh:
        summary = json.load(fh)
    # Zelfde defaults als session_view
    for k in ("bpmn_files", "mermaid_erd", "report_per_bpmn", "findings",
              "actors", "anchors", "inventory", "files"):
        summary.setdefault(k, [] if k != "mermaid_erd" else "")
    summary.setdefault("erd", {"entities": [], "relationships": []})
    summary.setdefault("totals", {})
    for k in ("files", "actors", "anchors", "rows", "tasks", "data_objects"):
        summary["totals"].setdefault(k, 0)
    summary.setdefault("findings_summary", {"total": 0, "by_severity": {},
                                             "by_rule": {}, "rules_catalog": []})
    summary.setdefault("url_base", f"/project/{pid}")
    summary.setdefault("versions", {})
    if summary["anchors"] and isinstance(summary["anchors"][0], str):
        summary["anchors"] = [{"name": n, "processes": []}
                              for n in summary["anchors"]]

    classification_counts: dict[str, int] = {}
    for row in summary["inventory"]:
        classification_counts[row["classification"]] = \
            classification_counts.get(row["classification"], 0) + 1

    return render_template("results.html",
                           summary=summary,
                           classification_counts=classification_counts)


# --- Project-scoped per-file routes (mirroring session-routes) ---

@app.route("/project/<pid>/bpmn/<path:filename>")
def project_bpmn_raw(pid: str, filename: str):
    if not bpmn_project.is_valid_pid(pid):
        abort(404)
    data_dir = bpmn_project.project_data_dir(ROOT, pid)
    safe = secure_filename(filename)
    if not safe or not (data_dir / safe).exists():
        abort(404)
    return send_from_directory(str(data_dir), safe,
                               mimetype="application/xml")


@app.route("/project/<pid>/bpmn-original/<path:filename>")
def project_bpmn_original(pid: str, filename: str):
    if not bpmn_project.is_valid_pid(pid):
        abort(404)
    pdir = bpmn_project.project_root_dir(ROOT, pid)
    safe = secure_filename(filename)
    base = Path(safe).stem
    ext = Path(safe).suffix
    v1 = pdir / "versions" / base / f"v1{ext}"
    if v1.exists():
        return send_from_directory(str(v1.parent), v1.name,
                                   mimetype="application/xml")
    data_file = bpmn_project.project_data_dir(ROOT, pid) / safe
    if data_file.exists():
        return send_from_directory(str(data_file.parent), safe,
                                   mimetype="application/xml")
    abort(404)


@app.route("/project/<pid>/improved/<path:filename>")
def project_improved_bpmn(pid: str, filename: str):
    if not bpmn_project.is_valid_pid(pid):
        abort(404)
    pdir = bpmn_project.project_root_dir(ROOT, pid)
    safe = secure_filename(filename)
    base = Path(safe).stem
    ext = Path(safe).suffix
    source = pdir / "versions" / base / f"v1{ext}"
    if not source.exists():
        source = bpmn_project.project_data_dir(ROOT, pid) / safe
    if not source.exists():
        abort(404)

    # Findings opnieuw voor v1 van deze BPMN
    user_defs = bpmn_defs.load(ROOT)
    import bpmn_parser as _bp
    parsed = _bp.parse_bpmn(source)
    from merger import merge as _merge
    model = _merge([parsed])
    findings = review(model, user_defs=user_defs)

    try:
        xml_bytes, _changes = bpmn_apply.build_improved_preview(
            source, findings, user_defs
        )
    except Exception as e:
        return f"Preview-fout: {e}", 500
    from flask import Response
    return Response(xml_bytes, mimetype="application/xml")


@app.route("/project/<pid>/improved-summary/<path:filename>")
def project_improved_summary(pid: str, filename: str):
    if not bpmn_project.is_valid_pid(pid):
        abort(404)
    pdir = bpmn_project.project_root_dir(ROOT, pid)
    safe = secure_filename(filename)
    base = Path(safe).stem
    ext = Path(safe).suffix
    source = pdir / "versions" / base / f"v1{ext}"
    if not source.exists():
        source = bpmn_project.project_data_dir(ROOT, pid) / safe
    if not source.exists():
        abort(404)
    user_defs = bpmn_defs.load(ROOT)
    import bpmn_parser as _bp
    parsed = _bp.parse_bpmn(source)
    from merger import merge as _merge
    model = _merge([parsed])
    findings = review(model, user_defs=user_defs)
    try:
        _xml, changes = bpmn_apply.build_improved_preview(
            source, findings, user_defs
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"changes": changes})


@app.route("/project/<pid>/apply-fix", methods=["POST"])
def project_apply_fix(pid: str):
    if not bpmn_project.is_valid_pid(pid):
        return jsonify({"error": "Ongeldig project"}), 404
    pdir = bpmn_project.project_root_dir(ROOT, pid)
    payload = request.get_json(silent=True) or {}
    rule = (payload.get("rule") or "").strip()
    file_name = secure_filename(payload.get("file", ""))
    params = payload.get("params") or {}
    if not (rule and file_name):
        return jsonify({"error": "rule en file verplicht"}), 400
    data_file = bpmn_project.project_data_dir(ROOT, pid) / file_name
    if not data_file.exists():
        return jsonify({"error": f"Bestand '{file_name}' niet gevonden"}), 404
    ok, description = bpmn_apply.apply_fix(data_file, rule, params)
    if not ok:
        return jsonify({"error": description}), 400
    bpmn_apply.ensure_v1(pdir, file_name)
    entry = bpmn_apply.add_fix_version(
        pdir, file_name,
        patched_bytes=data_file.read_bytes(),
        description=f"[{rule}] {description}",
        applied_finding=payload,
    )
    try:
        _regenerate_project_summary(pid)
    except Exception as e:
        return jsonify({"error": f"Regeneratie mislukt: {e}"}), 500
    return jsonify({"ok": True, "new_version": entry,
                    "description": description})


@app.route("/project/<pid>/apply-default-flow", methods=["POST"])
def project_apply_default_flow(pid: str):
    if not bpmn_project.is_valid_pid(pid):
        return jsonify({"error": "Ongeldig project"}), 404
    pdir = bpmn_project.project_root_dir(ROOT, pid)
    payload = request.get_json(silent=True) or {}
    file_name = secure_filename(payload.get("file", ""))
    gateway_id = payload.get("gateway_id", "")
    default_flow_id = payload.get("default_flow_id") or None
    guess = bool(payload.get("guess_conditions", True))
    if not (file_name and gateway_id):
        return jsonify({"error": "Geef file + gateway_id"}), 400
    data_file = bpmn_project.project_data_dir(ROOT, pid) / file_name
    if not data_file.exists():
        return jsonify({"error": f"Bestand '{file_name}' niet gevonden"}), 404
    try:
        bpmn_apply.apply_set_default_flow(
            data_file, gateway_id=gateway_id,
            default_flow_id=default_flow_id,
            guess_conditions=guess,
        )
    except Exception as e:
        return jsonify({"error": f"Fix mislukte: {e}"}), 500
    bpmn_apply.ensure_v1(pdir, file_name)
    entry = bpmn_apply.add_fix_version(
        pdir, file_name,
        patched_bytes=data_file.read_bytes(),
        description=(f"Default-flow op {default_flow_id}"
                     if default_flow_id else "Conditie-stubs toegevoegd"),
        applied_finding=payload,
    )
    try:
        _regenerate_project_summary(pid)
    except Exception as e:
        return jsonify({"error": f"Regeneratie mislukt: {e}"}), 500
    return jsonify({"ok": True, "new_version": entry})


@app.route("/project/<pid>/upload-version", methods=["POST"])
def project_upload_version(pid: str):
    if not bpmn_project.is_valid_pid(pid):
        abort(404)
    pdir = bpmn_project.project_root_dir(ROOT, pid)
    target = secure_filename(request.form.get("target", ""))
    f = request.files.get("bpmn_file")
    if not target or not f:
        return "Geef target en bpmn_file mee.", 400
    if not (bpmn_project.project_data_dir(ROOT, pid) / target).exists():
        return f"Onbekend target-bestand: {target}", 404
    if Path(f.filename).suffix.lower() not in ALLOWED_EXT:
        return "Alleen .bpmn of .xml toegestaan.", 400
    bpmn_apply.ensure_v1(pdir, target)
    bpmn_apply.add_upload_version(
        pdir, target, bytes_=f.read(),
        description=f"Nieuwe upload ({f.filename})",
    )
    try:
        _regenerate_project_summary(pid)
    except Exception as e:
        return f"Regeneratie mislukt: {e}", 500
    return redirect(url_for("project_results", pid=pid))


@app.route("/project/<pid>/activate", methods=["POST"])
def project_activate_version(pid: str):
    if not bpmn_project.is_valid_pid(pid):
        return jsonify({"error": "Ongeldig project"}), 404
    pdir = bpmn_project.project_root_dir(ROOT, pid)
    payload = request.get_json(silent=True) or {}
    file_name = secure_filename(payload.get("file", ""))
    version = int(payload.get("version", 0) or 0)
    if not (file_name and version):
        return jsonify({"error": "Geef file + version"}), 400
    ok = bpmn_apply.activate_version(pdir, file_name, version)
    if not ok:
        return jsonify({"error": "Versie niet gevonden"}), 404
    try:
        _regenerate_project_summary(pid)
    except Exception as e:
        return jsonify({"error": f"Regeneratie mislukt: {e}"}), 500
    return jsonify({"ok": True})


@app.route("/project/<pid>/download/<path:filename>")
def project_download(pid: str, filename: str):
    if not bpmn_project.is_valid_pid(pid):
        abort(404)
    out_dir = bpmn_project.project_output_dir(ROOT, pid)
    if not out_dir.exists():
        abort(404)
    return send_from_directory(str(out_dir), filename, as_attachment=True)


@app.route("/project/<pid>/upload-doc", methods=["POST"])
def project_upload_doc(pid: str):
    """Upload .docx of .pptx met procesbeschrijving. Parseer de tekst,
    genereer BPMNs per sectie, extracteer entities en sla een audit-
    entry op."""
    if not bpmn_project.is_valid_pid(pid):
        abort(404)
    meta = bpmn_project.load(ROOT, pid)
    if meta is None:
        abort(404)

    f = request.files.get("doc_file")
    if not f or not f.filename:
        return "Geen bestand geupload.", 400
    ext = Path(f.filename).suffix.lower()
    if ext not in (".docx", ".pptx"):
        return "Alleen .docx of .pptx worden ondersteund.", 400

    pdir = bpmn_project.project_root_dir(ROOT, pid)
    doc_id = uuid.uuid4().hex[:12]
    sub = bpmn_docs.doc_subdir(pdir, doc_id)
    dest = sub / f"original{ext}"
    f.save(str(dest))

    try:
        parsed, entities, bpmns, result = bpmn_docs.process_document(
            dest, doc_id
        )
    except Exception as e:
        return f"Verwerking mislukt: {e}", 500

    # Genereerde BPMN's opslaan in project/data/ en per-bestand v1 registreren
    data_dir = bpmn_project.project_data_dir(ROOT, pid)
    for filename, xml_bytes, tasks_meta, _proc_name in bpmns:
        dest_bpmn = data_dir / filename
        dest_bpmn.write_bytes(xml_bytes)
        bpmn_apply.ensure_v1(pdir, filename)

    # Update bpmn_order: voeg nieuwe files onderaan toe
    order = list(meta.get("bpmn_order", []))
    for filename, _, _, _ in bpmns:
        if filename not in order:
            order.append(filename)
    meta["bpmn_order"] = order
    bpmn_project.save(ROOT, meta)

    # Entities als auto-discovered in global definitions
    # (werkt via bpmn_defs — we doen een pseudo-model-discovery)
    defs_data = bpmn_defs.load(ROOT)
    defs_data.setdefault("objects", {})
    defs_data.setdefault("discovered_objects", {})
    for ent_name, attrs in entities.entities.items():
        attr_list = sorted(attrs)
        # Alleen toevoegen als object nog niet bestaat; anders behoud user-attrs
        if ent_name not in defs_data["objects"]:
            defs_data["objects"][ent_name] = []
        # Discovered-metadata bijwerken
        entry = defs_data["discovered_objects"].get(ent_name, {
            "aliases": [], "processes": [], "source_files": [],
            "discovered": True,
        })
        entry["discovered"] = True
        entry["source_files"] = sorted(set(entry.get("source_files", []))
                                        | {f.filename})
        entry["from_docs"] = sorted(set(entry.get("from_docs", []))
                                     | {f.filename})
        entry["suggested_attributes"] = sorted(
            set(entry.get("suggested_attributes", [])) | set(attr_list)
        )
        defs_data["discovered_objects"][ent_name] = entry
    bpmn_defs.save(ROOT, defs_data)

    # Audit-log
    bpmn_docs.append_docs_log(pdir, result.to_dict())

    # Redirect naar project detail met hint
    return redirect(url_for("project_detail", pid=pid) + "?doc_processed=1")


@app.route("/project/<pid>/docs")
def project_docs(pid: str):
    if not bpmn_project.is_valid_pid(pid):
        abort(404)
    meta = bpmn_project.load(ROOT, pid)
    if meta is None:
        abort(404)
    pdir = bpmn_project.project_root_dir(ROOT, pid)
    log = bpmn_docs.load_docs_log(pdir)
    return render_template("docs.html", project=meta, docs=log)


@app.route("/project/<pid>/doc/<doc_id>/raw")
def project_doc_raw(pid: str, doc_id: str):
    """Download het originele document."""
    if not bpmn_project.is_valid_pid(pid):
        abort(404)
    pdir = bpmn_project.project_root_dir(ROOT, pid)
    sub = pdir / "documents" / doc_id
    if not sub.exists():
        abort(404)
    # Zoek original.*
    for p in sub.iterdir():
        if p.name.startswith("original"):
            return send_from_directory(str(sub), p.name, as_attachment=True)
    abort(404)


@app.route("/project/<pid>/auto-order", methods=["POST"])
def project_auto_order(pid: str):
    """Bereken lifecycle-gebaseerde volgorde en sla op in bpmn_order."""
    if not bpmn_project.is_valid_pid(pid):
        abort(404)
    meta = bpmn_project.load(ROOT, pid)
    if meta is None:
        abort(404)
    data_dir = bpmn_project.project_data_dir(ROOT, pid)
    bpmns = parse_all(data_dir)
    if not bpmns:
        return "Geen BPMNs in dit project", 400

    model = merge(bpmns)
    user_defs = bpmn_defs.load(ROOT)
    entities, _rels = bpmn_erd.build_erd(model, user_defs=user_defs)
    source_file_by_process = {
        (b.process_name or b.source_file): b.source_file for b in bpmns
    }
    ordered, reasons = bpmn_project.compute_dependency_order(
        entities, source_file_by_process
    )
    meta["bpmn_order"] = ordered
    meta["order_reasons"] = reasons
    meta["order_mode"] = "auto"
    bpmn_project.save(ROOT, meta)
    return redirect(url_for("project_detail", pid=pid))


@app.route("/project/<pid>/reorder", methods=["POST"])
def project_reorder(pid: str):
    """Verplaats één bestand omhoog of omlaag in bpmn_order."""
    if not bpmn_project.is_valid_pid(pid):
        abort(404)
    meta = bpmn_project.load(ROOT, pid)
    if meta is None:
        abort(404)
    target = secure_filename(request.form.get("target", ""))
    direction = request.form.get("direction", "up")  # 'up' | 'down'
    order = list(meta.get("bpmn_order", []))
    if target not in order:
        return "Onbekend bestand", 400
    idx = order.index(target)
    if direction == "up" and idx > 0:
        order[idx], order[idx - 1] = order[idx - 1], order[idx]
    elif direction == "down" and idx < len(order) - 1:
        order[idx], order[idx + 1] = order[idx + 1], order[idx]
    meta["bpmn_order"] = order
    meta["order_mode"] = "manual"
    bpmn_project.save(ROOT, meta)
    return redirect(url_for("project_detail", pid=pid))


@app.route("/project/<pid>/delete", methods=["POST"])
def project_delete(pid: str):
    if not bpmn_project.is_valid_pid(pid):
        abort(404)
    bpmn_project.delete(ROOT, pid)
    return redirect(url_for("index"))


@app.route("/project/<pid>/remove-bpmn", methods=["POST"])
def project_remove_bpmn(pid: str):
    if not bpmn_project.is_valid_pid(pid):
        abort(404)
    meta = bpmn_project.load(ROOT, pid)
    if meta is None:
        abort(404)
    target = secure_filename((request.form.get("target") or ""))
    if not target:
        return "Geef target mee.", 400
    data_file = bpmn_project.project_data_dir(ROOT, pid) / target
    if data_file.exists():
        data_file.unlink()
    # Ook versions opruimen
    base = Path(target).stem
    versions_dir = bpmn_project.project_root_dir(ROOT, pid) / "versions" / base
    if versions_dir.exists():
        import shutil
        shutil.rmtree(versions_dir, ignore_errors=True)
    # Update order
    meta["bpmn_order"] = [n for n in meta.get("bpmn_order", []) if n != target]
    bpmn_project.save(ROOT, meta)
    return redirect(url_for("project_detail", pid=pid))


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
    # Auto-discover: actoren + entity-namen uit dit model in user-defs opnemen
    # (zodat ze meteen in /definities verschijnen voor verdere verrijking).
    bpmn_defs.auto_discover(ROOT, model, session_id=sid)
    user_defs = bpmn_defs.load(ROOT)
    findings = review(model, user_defs=user_defs)
    # Verrijk findings met cross-BPMN analyse uit het datamodel
    erd = _build_smart_erd(model, user_defs)
    findings = findings + erd["cross_findings"]
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
        "mermaid_erd": erd["mermaid"],
        "erd": erd["summary"],
        "findings": findings,
        "findings_summary": findings_summary,
        # Rapport-secties per BPMN (voor inline HTML rapport)
        "report_per_bpmn": [{
            "name": b.source_file,
            "process": b.process_name,
            "lanes": [l.name for l in b.lanes],
            "external_actors": [p.name for p in b.participants
                                if not p.attributes.get("processRef")],
            "documentation": getattr(b, "process_documentation", ""),
            "tasks": [{"id": f"A{i+1}", "name": t.name,
                       "subtype": t.subtype, "lane": t.lane_id or "",
                       "documentation": t.attributes.get("documentation", "")}
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
    summary.setdefault("erd", {"entities": [], "relationships": []})
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
    bpmn_defs.auto_discover(ROOT, model, session_id=sid)
    user_defs = bpmn_defs.load(ROOT)
    findings = review(model, user_defs=user_defs)
    erd = _build_smart_erd(model, user_defs)
    findings = findings + erd["cross_findings"]
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
        "mermaid_erd": erd["mermaid"],
        "erd": erd["summary"],
        "findings": findings,
        "findings_summary": findings_summary,
        "report_per_bpmn": [{
            "name": b.source_file, "process": b.process_name,
            "lanes": [l.name for l in b.lanes],
            "external_actors": [p.name for p in b.participants
                                if not p.attributes.get("processRef")],
            "documentation": getattr(b, "process_documentation", ""),
            "tasks": [{"id": f"A{i+1}", "name": t.name,
                       "subtype": t.subtype, "lane": t.lane_id or "",
                       "documentation": t.attributes.get("documentation", "")}
                      for i, t in enumerate(b.tasks)],
            "data_objects": [{"name": d.name, "subtype": d.subtype, "id": d.id}
                             for d in b.data_objects],
            "annotations": [a.attributes.get("text", "") for a in b.annotations],
        } for b in model.bpmns],
    }

    # mermaid_erd en erd zijn al toegewezen via _build_smart_erd hierboven

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


@app.route("/session/<sid>/apply-fix", methods=["POST"])
def session_apply_fix(sid: str):
    """Generieke apply-endpoint: dispatcht op `rule` naar de juiste fix.

    Body: { rule, file, params: {...} }
    """
    if not _is_valid_sid(sid):
        return jsonify({"error": "Ongeldige sessie"}), 404
    sdir = SESSIONS_DIR / sid
    if not sdir.exists():
        return jsonify({"error": "Sessie niet gevonden"}), 404

    payload = request.get_json(silent=True) or {}
    rule = (payload.get("rule") or "").strip()
    file_name = secure_filename(payload.get("file", ""))
    params = payload.get("params") or {}

    if not (rule and file_name):
        return jsonify({"error": "rule en file verplicht"}), 400

    data_file = sdir / "data" / file_name
    if not data_file.exists():
        return jsonify({"error": f"Bestand '{file_name}' niet gevonden"}), 404

    ok, description = bpmn_apply.apply_fix(data_file, rule, params)
    if not ok:
        return jsonify({"error": description}), 400

    bpmn_apply.ensure_v1(sdir, file_name)
    entry = bpmn_apply.add_fix_version(
        sdir, file_name,
        patched_bytes=data_file.read_bytes(),
        description=f"[{rule}] {description}",
        applied_finding=payload,
    )
    try:
        _regenerate_session_summary(sid)
    except Exception as e:
        return jsonify({"error": f"Regeneratie mislukt: {e}"}), 500
    return jsonify({
        "ok": True,
        "new_version": entry,
        "description": description,
    })


@app.route("/session/<sid>/apply-default-flow", methods=["POST"])
def session_apply_default_flow(sid: str):
    """R008 fix: zet default-flow op gateway + optioneel conditie-stubs.

    Body: { file, gateway_id, default_flow_id (of null), guess_conditions }
    """
    if not _is_valid_sid(sid):
        abort(404)
    sdir = SESSIONS_DIR / sid
    if not sdir.exists():
        abort(404)

    payload = request.get_json(silent=True) or {}
    file_name = secure_filename(payload.get("file", ""))
    gateway_id = payload.get("gateway_id", "")
    default_flow_id = payload.get("default_flow_id") or None
    guess = bool(payload.get("guess_conditions", True))

    if not (file_name and gateway_id):
        return jsonify({"error": "Geef file + gateway_id"}), 400

    data_file = sdir / "data" / file_name
    if not data_file.exists():
        return jsonify({"error": f"Bestand {file_name} niet gevonden"}), 404

    try:
        bpmn_apply.apply_set_default_flow(
            data_file, gateway_id=gateway_id,
            default_flow_id=default_flow_id,
            guess_conditions=guess,
        )
    except Exception as e:
        return jsonify({"error": f"Fix mislukte: {e}"}), 500

    bpmn_apply.ensure_v1(sdir, file_name)
    entry = bpmn_apply.add_fix_version(
        sdir, file_name,
        patched_bytes=data_file.read_bytes(),
        description=(f"Default-flow ingesteld op {default_flow_id}"
                     if default_flow_id else
                     "Conditie-stubs toegevoegd aan uitgaande flows"),
        applied_finding=payload,
    )
    try:
        _regenerate_session_summary(sid)
    except Exception as e:
        return jsonify({"error": f"Regeneratie mislukt: {e}"}), 500
    return jsonify({"ok": True, "new_version": entry})


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


def _resolve_source_file(sdir: Path, safe_filename: str) -> Path | None:
    """Geef het pad naar v1 als aanwezig, anders de actieve data-file.

    Zo worden preview + original altijd gebaseerd op dezelfde originele
    upload, onafhankelijk van hoeveel fixes er al zijn toegepast.
    """
    base = Path(safe_filename).stem
    ext = Path(safe_filename).suffix
    v1 = sdir / "versions" / base / f"v1{ext}"
    if v1.exists():
        return v1
    data_file = sdir / "data" / safe_filename
    return data_file if data_file.exists() else None


def _findings_for_original(sdir: Path, safe_filename: str,
                           user_defs: dict) -> list[dict]:
    """Bereken findings opnieuw tegen v1 (niet tegen de actieve versie).

    Zo blijft de preview consistent: hij toont altijd wat er zou zijn
    als je vanaf v1 alle safe auto-fixes toepast, niet 'wat er nog
    moet nadat je al een paar fixes hebt gedaan'.
    """
    v1 = _resolve_source_file(sdir, safe_filename)
    if v1 is None:
        return []
    # Parse alleen deze ene file via parse_bpmn
    import bpmn_parser as _bp
    parsed = _bp.parse_bpmn(v1)
    # Minimal model-stub zodat cross-BPMN findings geen zin hebben (enkel 1 file)
    from merger import merge
    model = merge([parsed])
    findings = review(model, user_defs=user_defs)
    return findings


@app.route("/session/<sid>/improved/<path:filename>")
def session_improved_bpmn(sid: str, filename: str):
    """Preview-XML: v1 + alle safe auto-fixes toegepast."""
    if not _is_valid_sid(sid):
        abort(404)
    sdir = SESSIONS_DIR / sid
    safe = secure_filename(filename)
    source = _resolve_source_file(sdir, safe)
    if source is None:
        abort(404)

    user_defs = bpmn_defs.load(ROOT)
    findings = _findings_for_original(sdir, safe, user_defs)
    try:
        xml_bytes, _changes = bpmn_apply.build_improved_preview(
            source, findings, user_defs
        )
    except Exception as e:
        return f"Preview-fout: {e}", 500

    from flask import Response
    return Response(xml_bytes, mimetype="application/xml")


@app.route("/session/<sid>/improved-summary/<path:filename>")
def session_improved_summary(sid: str, filename: str):
    """Lijst van wijzigingen die in de v1-based preview zijn doorgevoerd."""
    if not _is_valid_sid(sid):
        abort(404)
    sdir = SESSIONS_DIR / sid
    safe = secure_filename(filename)
    source = _resolve_source_file(sdir, safe)
    if source is None:
        abort(404)
    user_defs = bpmn_defs.load(ROOT)
    findings = _findings_for_original(sdir, safe, user_defs)
    try:
        _xml, changes = bpmn_apply.build_improved_preview(
            source, findings, user_defs
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"changes": changes})


@app.route("/session/<sid>/bpmn-original/<path:filename>")
def session_bpmn_original(sid: str, filename: str):
    """Serveer altijd de v1-versie (de originele upload), onafhankelijk van
    hoeveel fixes er inmiddels zijn toegepast."""
    if not _is_valid_sid(sid):
        abort(404)
    sdir = SESSIONS_DIR / sid
    safe = secure_filename(filename)
    # Zoek v1.<ext> in versions/<base>/
    base = Path(safe).stem
    ext = Path(safe).suffix
    v1 = sdir / "versions" / base / f"v1{ext}"
    if v1.exists():
        return send_from_directory(
            str(v1.parent), v1.name, mimetype="application/xml"
        )
    # Fallback: actieve file (als versies nog niet zijn aangemaakt)
    data_file = sdir / "data" / safe
    if data_file.exists():
        return send_from_directory(
            str(data_file.parent), safe, mimetype="application/xml"
        )
    abort(404)


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
