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
import bpmn_interactive                                  # noqa: E402
import bpmn_anchors                                      # noqa: E402
import bpmn_process_map                                  # noqa: E402
import csv_field_detector                                # noqa: E402


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
    entities, rels = bpmn_erd.build_erd(model, user_defs=user_defs, project_root=ROOT)
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


def _compute_process_matches(pid: str, meta: dict) -> dict:
    """Match elke CSV-procesregister-rij aan een BPMN-bestand en retourneer
    rijen + orphan uploads + totalen.

    Gebruikt door /project/<pid> én /project/<pid>/processes. Code op één
    plek zodat beide views altijd dezelfde match-score gebruiken."""
    origins = meta.get("bpmn_origins", {})
    records = meta.get("proceslijst_records", [])
    data_dir = bpmn_project.project_data_dir(ROOT, pid)
    disk_bpmns = sorted([p.name for p in data_dir.glob("*.bpmn")]) \
                 if data_dir.exists() else []

    STOPW = {"l1", "l2", "l3", "l4", "soll", "ist", "v1", "v2", "v3", "v4",
             "de", "het", "een", "en", "in", "van", "bij", "voor", "op",
             "proces", "subproces", "bpmn", "concept"}
    # 'final' en 'p01'..'p09' bewust NIET in STOPW: gebruikers hernoemen
    # uploads met P-nummers die betekenisvol zijn.

    def tok(s: str) -> set[str]:
        return {t for t in re.split(r"[^a-zA-Z0-9]+", s.lower())
                if t and len(t) > 2 and t not in STOPW}

    def code_variants(code: str) -> list[str]:
        return [code, code.replace(".", "_"),
                code.replace(".", "-"), code.replace(".", "")]

    def _fuzzy_overlap(a: set[str], b: set[str]) -> set[str]:
        """Match op exact én op substring in beide richtingen, zodat
        'contributie' ~ 'contributies', 'werverspremie' ~ 'werverspremie',
        'organisatie' ~ 'organisatiegegevens'. Minimum 4 tekens om valse
        hits op korte woordjes te voorkomen."""
        hits = a & b
        for ta in a:
            if len(ta) < 4:
                continue
            for tb in b:
                if len(tb) < 4 or (ta, tb) in hits:
                    continue
                if ta in tb or tb in ta:
                    hits.add(ta)
                    break
        return hits

    matched_files: set[str] = set()
    rows = []
    for rec in records:
        code = rec.get("code", "").strip()
        naam = (rec.get("naam") or rec.get("deelproces")
                or rec.get("subproces") or "").strip()
        proc_tokens = tok(naam + " " + rec.get("subproces", "")
                          + " " + rec.get("deelproces", ""))
        best = None
        best_score = 0
        best_why = ""
        for fname in disk_bpmns:
            score = 0
            why = []
            fn_low = fname.lower()
            if code:
                for cv in code_variants(code):
                    if cv and cv in fn_low:
                        score += 10
                        why.append(f"code {cv}")
                        break
            file_tokens = tok(fname.replace("-", " ").replace("_", " "))
            overlap = _fuzzy_overlap(proc_tokens, file_tokens)
            if overlap:
                score += len(overlap) * 2
                why.append("woorden: " + ", ".join(sorted(overlap)))
            if score > best_score:
                best_score = score
                best = fname
                best_why = "; ".join(why)
        origin = origins.get(best or "", {}) if best else {}
        rows.append({
            "code": code,
            "naam": naam,
            "doel": rec.get("doel", ""),
            "eigenaar": rec.get("eigenaar", ""),
            "sme": rec.get("sme", ""),
            "best_match": best,
            "match_score": best_score,
            "match_why": best_why,
            "origin_kind": origin.get("kind", ""),
            "origin_label": origin.get("kind_label", ""),
            "matched": best_score >= 8,
        })
        if best and best_score >= 8:
            matched_files.add(best)

    orphan_uploads = []
    for fname in disk_bpmns:
        o = origins.get(fname, {})
        if o.get("kind") != "upload":
            continue
        if fname in matched_files:
            continue
        orphan_uploads.append({
            "file": fname,
            "source": o.get("source", ""),
            "created_at": o.get("created_at", ""),
        })

    totals = {
        "records": len(records),
        "disk_bpmns": len(disk_bpmns),
        "uploads": sum(1 for o in origins.values() if o.get("kind") == "upload"),
        "skeletons": sum(1 for o in origins.values() if o.get("kind") == "skeleton"),
        "matched_records": sum(1 for r in rows if r["matched"]),
        "unmatched_records": sum(1 for r in rows if not r["matched"]),
        "orphan_uploads": len(orphan_uploads),
    }
    return {"rows": rows, "orphan_uploads": orphan_uploads, "totals": totals}


@app.route("/project/<pid>")
def project_detail(pid: str):
    meta = bpmn_project.load(ROOT, pid)
    if meta is None:
        abort(404)
    match = _compute_process_matches(pid, meta)
    return render_template("project.html", project=meta,
                           max_mb=MAX_FILE_MB,
                           process_match=match)


@app.route("/project/<pid>/interactive", methods=["GET"])
def project_interactive_index(pid: str):
    """Overzicht van interactief te reviewen documenten + upload-formulier."""
    if not bpmn_project.is_valid_pid(pid):
        abort(404)
    meta = bpmn_project.load(ROOT, pid)
    if meta is None:
        abort(404)
    pdir = bpmn_project.project_root_dir(ROOT, pid)
    docs = bpmn_interactive.list_interactive_docs(pdir)
    return render_template("interactive_index.html",
                           project=meta, docs=docs, max_mb=MAX_FILE_MB)


@app.route("/project/<pid>/interactive/upload", methods=["POST"])
def project_interactive_upload(pid: str):
    """Upload een .docx/.pptx en bouw chunks zonder auto-generatie."""
    if not bpmn_project.is_valid_pid(pid):
        abort(404)
    meta = bpmn_project.load(ROOT, pid)
    if meta is None:
        abort(404)
    f = request.files.get("doc_file")
    if not f or not f.filename:
        return "Geen bestand geüpload.", 400
    ext = Path(f.filename).suffix.lower()
    if ext not in (".docx", ".pptx"):
        return "Alleen .docx of .pptx.", 400
    pdir = bpmn_project.project_root_dir(ROOT, pid)
    # Tijdelijk opslaan om aan bpmn_interactive te geven
    tmp = pdir / f"_tmp_interactive{ext}"
    f.save(str(tmp))
    try:
        doc_id, _ = bpmn_interactive.build_chunks_file(
            pdir, tmp, f.filename, project_root=ROOT,
        )
    finally:
        if tmp.exists():
            tmp.unlink()
    return redirect(url_for("project_interactive_review",
                            pid=pid, doc_id=doc_id, chunk=0))


@app.route("/project/<pid>/interactive/<doc_id>/review")
def project_interactive_review(pid: str, doc_id: str):
    if not bpmn_project.is_valid_pid(pid):
        abort(404)
    meta = bpmn_project.load(ROOT, pid)
    if meta is None:
        abort(404)
    pdir = bpmn_project.project_root_dir(ROOT, pid)
    data = bpmn_interactive.load_chunks(pdir, doc_id)
    if data is None:
        abort(404)
    try:
        chunk_idx = int(request.args.get("chunk", 0))
    except (TypeError, ValueError):
        chunk_idx = 0
    chunks = data["chunks"]
    chunk_idx = max(0, min(chunk_idx, len(chunks) - 1))
    processed = sum(1 for c in chunks if c.get("decision") == "processed")
    skipped = sum(1 for c in chunks if c.get("decision") == "skipped")
    remaining = len(chunks) - processed - skipped
    return render_template("interactive_review.html",
                           project=meta, doc=data,
                           chunk=chunks[chunk_idx], chunk_idx=chunk_idx,
                           total=len(chunks),
                           processed=processed, skipped=skipped,
                           remaining=remaining)


@app.route("/project/<pid>/interactive/<doc_id>/apply", methods=["POST"])
def project_interactive_apply(pid: str, doc_id: str):
    if not bpmn_project.is_valid_pid(pid):
        return jsonify({"error": "Ongeldig project"}), 404
    pdir = bpmn_project.project_root_dir(ROOT, pid)
    payload = request.get_json(silent=True) or {}
    try:
        chunk_idx = int(payload.get("chunk_idx", -1))
    except (TypeError, ValueError):
        return jsonify({"error": "chunk_idx verplicht"}), 400
    action = (payload.get("action") or "").strip().lower()
    if action not in ("process", "skip"):
        return jsonify({"error": "action moet 'process' of 'skip' zijn"}), 400
    try:
        res = bpmn_interactive.apply_chunk_decision(
            pdir, doc_id, chunk_idx, action,
            payload.get("payload"),
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(res)


@app.route("/project/<pid>/upload-for-process", methods=["POST"])
def project_upload_for_process(pid: str):
    """Upload een .bpmn/.xml voor een specifiek proces uit het register.

    Form-data:
      code           — processcode uit CSV (bv. '1.1.10')
      naam           — processnaam
      replace_file   — (optioneel) bestaande filename die vervangen moet
                       worden (typisch een skeleton)
      bpmn_file      — het echte BPMN-bestand

    Workflow:
      - Als replace_file meegegeven is én bestaat: overschrijf het (en
        zet de origin om van 'skeleton' naar 'upload').
      - Anders: sla op onder een nieuwe, slug-gebaseerde filename die
        de code + naam bevat. Als de filename al bestaat, voeg `_u1`,
        `_u2`... toe.
      - Registreer nieuwe kind_label = 'Door gebruiker geupload (voor
        proces X.Y)'.
    """
    if not bpmn_project.is_valid_pid(pid):
        abort(404)
    meta = bpmn_project.load(ROOT, pid)
    if meta is None:
        abort(404)
    code = (request.form.get("code") or "").strip()
    naam = (request.form.get("naam") or "").strip()
    replace_file = secure_filename(request.form.get("replace_file") or "")
    f = request.files.get("bpmn_file")
    if not f or not f.filename:
        return "Geen BPMN-bestand gekozen.", 400
    if Path(f.filename).suffix.lower() not in ALLOWED_EXT:
        return "Alleen .bpmn of .xml toegestaan.", 400

    pdir = bpmn_project.project_root_dir(ROOT, pid)
    data_dir = bpmn_project.project_data_dir(ROOT, pid)
    data_dir.mkdir(parents=True, exist_ok=True)

    def slugify(s: str) -> str:
        s = re.sub(r"[^a-zA-Z0-9]+", "_", s.lower()).strip("_")
        return s or "proces"

    # Bepaal doel-filename
    origins = meta.setdefault("bpmn_origins", {})
    if replace_file and (data_dir / replace_file).exists():
        target_name = replace_file
    else:
        base = f"{slugify(code)}_{slugify(naam)}".strip("_")[:80] or "proces"
        target_name = f"{base}.bpmn"
        n = 1
        while (data_dir / target_name).exists():
            target_name = f"{base}_u{n}.bpmn"
            n += 1

    target_path = data_dir / target_name
    # Oude v1 behouden als er een bestaand bestand is
    if target_path.exists():
        bpmn_apply.ensure_v1(pdir, target_name)
    target_path.write_bytes(f.read())
    # Nieuwe upload als versie registreren
    try:
        bpmn_apply.add_upload_version(
            pdir, target_name,
            bytes_=target_path.read_bytes(),
            description=(f"Handmatig geüpload voor proces {code} {naam} "
                         f"(origineel: {f.filename})"),
        )
    except Exception:
        pass  # versie-log is hulpdata

    from datetime import datetime as _dt
    origins[target_name] = {
        "kind": "upload",
        "kind_label": f"Door gebruiker geüpload voor proces {code} {naam}".strip(),
        "source": f.filename,
        "source_row": f"{code} {naam}".strip(),
        "created_at": _dt.now().isoformat(timespec="seconds"),
    }
    if target_name not in meta.get("bpmn_files", []):
        meta.setdefault("bpmn_files", []).append(target_name)
    order = list(meta.get("bpmn_order", []))
    if target_name not in order:
        order.append(target_name)
        meta["bpmn_order"] = order
    bpmn_project.save(ROOT, meta)

    return redirect(request.referrer or url_for("project_detail", pid=pid))


@app.route("/project/<pid>/processes")
def project_processes(pid: str):
    """Toon CSV-procesregister + match naar BPMNs + herkomst-badge
    (uploaded vs skeleton)."""
    if not bpmn_project.is_valid_pid(pid):
        abort(404)
    meta = bpmn_project.load(ROOT, pid)
    if meta is None:
        abort(404)
    match = _compute_process_matches(pid, meta)
    return render_template("processes.html",
                           project=meta, rows=match["rows"],
                           orphan_uploads=match["orphan_uploads"],
                           totals=match["totals"])


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

    from datetime import datetime as _dt
    meta.setdefault("bpmn_origins", {})
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
        meta["bpmn_origins"][safe] = {
            "kind": "upload",
            "kind_label": "Door gebruiker geupload",
            "source": f.filename,
            "created_at": _dt.now().isoformat(timespec="seconds"),
        }

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

    # GLOBALE ankerobjecten-repository bijwerken: zorg dat de tool
    # cross-project weet welke entities centraal zijn in de organisatie.
    process_map_mermaid = ""
    process_relations: list[dict] = []
    try:
        entities_obj, _rels = bpmn_erd.build_erd(model, user_defs=user_defs, project_root=ROOT)
        bpmn_anchors.ingest_from_model(ROOT, entities_obj, project_id=pid)
        # Bouw ook de proces-relatie-kaart: welke processen hangen samen
        # via gedeelde entities?
        process_map_mermaid, process_relations = \
            bpmn_process_map.build_process_map(entities_obj)
    except Exception:
        pass  # anchors-store is hulpdata, mag nooit de analyse blokkeren

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
        "bpmn_parents": meta.get("bpmn_parents", {}),
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
        "process_map_mermaid": locals().get("process_map_mermaid", ""),
        "process_relations": locals().get("process_relations", []),
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
    # Forceer source_file op de display-filename, zodat preview-findings
    # dezelfde source_file hebben als de live findings in summary.json.
    parsed.source_file = safe
    from merger import merge as _merge
    model = _merge([parsed])
    findings = review(model, user_defs=user_defs)

    try:
        xml_bytes, _changes = bpmn_apply.build_improved_preview(
            source, findings, user_defs, display_filename=safe
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
    parsed.source_file = safe
    from merger import merge as _merge
    model = _merge([parsed])
    findings = review(model, user_defs=user_defs)
    try:
        _xml, changes = bpmn_apply.build_improved_preview(
            source, findings, user_defs, display_filename=safe
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
    from datetime import datetime as _dt
    meta.setdefault("bpmn_origins", {})
    doc_kind = "word" if ext == ".docx" else "powerpoint"
    doc_kind_label = "Auto-gegenereerd uit Word-document" if ext == ".docx" else "Auto-gegenereerd uit PowerPoint"
    for filename, xml_bytes, tasks_meta, proc_name in bpmns:
        dest_bpmn = data_dir / filename
        dest_bpmn.write_bytes(xml_bytes)
        bpmn_apply.ensure_v1(pdir, filename)
        meta["bpmn_origins"][filename] = {
            "kind": doc_kind,
            "kind_label": doc_kind_label,
            "source": f.filename,
            "source_proces": proc_name,
            "source_tasks": [t.get("name", "") if isinstance(t, dict) else getattr(t, "name", "") for t in (tasks_meta or [])][:10],
            "doc_id": doc_id,
            "created_at": _dt.now().isoformat(timespec="seconds"),
        }

    # Update bpmn_order: voeg nieuwe files onderaan toe
    order = list(meta.get("bpmn_order", []))
    for filename, _, _, _ in bpmns:
        if filename not in order:
            order.append(filename)
    meta["bpmn_order"] = order

    # Subproces-hiërarchie: map filename -> parent_process (uit doc-classificatie)
    parents = dict(meta.get("bpmn_parents", {}))
    for s in result.section_classification:
        fn = s.get("generated_filename")
        if fn:
            parents[fn] = s.get("parent_process", "")
    meta["bpmn_parents"] = parents
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
    entities, _rels = bpmn_erd.build_erd(model, user_defs=user_defs, project_root=ROOT)
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


@app.route("/projects/bulk-delete", methods=["POST"])
def projects_bulk_delete():
    """Verwijder een lijst van projecten in 1 keer."""
    pids = request.form.getlist("pids")
    count = 0
    for pid in pids:
        if bpmn_project.is_valid_pid(pid):
            if bpmn_project.delete(ROOT, pid):
                count += 1
    return redirect(url_for("index"))


@app.route("/project/<pid>/register", methods=["GET", "POST"])
def project_register(pid: str):
    """Procesregister: lijst van verwachte processen voor dit project.

    GET  -> pagina met bestaande lijst + editor
    POST -> sla complete lijst op (1 naam per regel in textarea)
    """
    if not bpmn_project.is_valid_pid(pid):
        abort(404)
    meta = bpmn_project.load(ROOT, pid)
    if meta is None:
        abort(404)

    if request.method == "POST":
        raw = request.form.get("expected_processes", "")
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        meta["expected_processes"] = lines
        bpmn_project.save(ROOT, meta)
        return redirect(url_for("project_register", pid=pid))

    expected = meta.get("expected_processes", [])
    bpmn_files = meta.get("bpmn_files", [])

    # Match expected processes tegen aanwezige BPMN-filenames
    # Simpele fuzzy-match: case-insensitive substring beide kanten
    def _slug_for_match(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", s.lower())

    by_slug_expected = {_slug_for_match(e): e for e in expected}
    bpmn_slugs = {_slug_for_match(Path(f).stem): f for f in bpmn_files}

    matched: list[dict] = []
    for exp in expected:
        sl = _slug_for_match(exp)
        found_file = None
        for bpmn_sl, filename in bpmn_slugs.items():
            if sl and (sl in bpmn_sl or bpmn_sl in sl) and len(sl) >= 4:
                found_file = filename
                break
        matched.append({
            "expected": exp,
            "bpmn_file": found_file,
            "status": "present" if found_file else "missing",
        })

    # Ook: welke BPMN-bestanden hebben geen match in de verwachtingslijst?
    unexpected = []
    for filename in bpmn_files:
        sl = _slug_for_match(Path(filename).stem)
        is_expected = any(
            (_slug_for_match(e) and
             (sl in _slug_for_match(e) or _slug_for_match(e) in sl)
             and len(_slug_for_match(e)) >= 4)
            for e in expected
        )
        if not is_expected:
            unexpected.append(filename)

    return render_template("register.html",
                           project=meta, expected=expected,
                           matched=matched, unexpected=unexpected)


@app.route("/project/<pid>/upload-csv", methods=["POST"])
def project_upload_csv(pid: str):
    """Upload een proceslijst-CSV en laat de veld-detector de kolommen herkennen.

    POST multipart:
        file=<csv>
        apply=<"true"|"false">  # indien "true": zet expected_processes meteen

    Returns JSON:
        {
          "info":    {encoding, delimiter, header_row, n_rows, mapping, unmapped_columns},
          "schema":  {code: idx, naam: idx, ...},
          "records": [{code, naam, hoofdproces, ...}, ...],
          "applied": <bool>,
          "expected_count": <int>
        }
    """
    if not bpmn_project.is_valid_pid(pid):
        abort(404)
    meta = bpmn_project.load(ROOT, pid)
    if meta is None:
        abort(404)

    if "file" not in request.files:
        return jsonify({"error": "geen bestand"}), 400
    f = request.files["file"]
    fname = secure_filename(f.filename or "upload.csv")
    if not fname.lower().endswith((".csv", ".tsv", ".txt")):
        return jsonify({"error": "alleen .csv/.tsv/.txt toegestaan"}), 400

    # Bewaar tijdelijk in de project-folder
    pdir = bpmn_project.project_root_dir(ROOT, pid)
    pdir.mkdir(parents=True, exist_ok=True)
    tmp_path = pdir / "procesregister_upload.csv"
    f.save(str(tmp_path))

    try:
        records, schema, info = csv_field_detector.detect_and_parse(str(tmp_path))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"kon CSV niet parsen: {exc}"}), 400

    applied = False
    apply_flag = request.form.get("apply", "false").lower() in ("true", "1", "yes")
    if apply_flag and records:
        # Converteer records naar een leesbare lijst voor expected_processes.
        # Format: "<code> <naam> - <hoofdproces> / <subproces>"
        lines = []
        for r in records:
            code = r.get("code", "").strip()
            naam = (r.get("naam") or r.get("deelproces") or r.get("subproces") or "").strip()
            hp   = r.get("hoofdproces", "").strip()
            sp   = r.get("subproces", "").strip()
            parts = []
            if code: parts.append(code)
            if naam: parts.append(naam)
            context = " / ".join(x for x in (hp, sp) if x)
            label = " ".join(parts)
            if context and label:
                label = f"{label} - {context}"
            elif context:
                label = context
            if label:
                lines.append(label)
        meta["expected_processes"] = lines
        # Bewaar ook de ruwe records als metadata voor latere matching/analyse
        meta["proceslijst_records"]   = records
        meta["proceslijst_schema"]    = schema
        meta["proceslijst_csv_info"]  = info
        bpmn_project.save(ROOT, meta)
        applied = True

    return jsonify({
        "info": info,
        "schema": schema,
        "records": records,
        "applied": applied,
        "expected_count": len(records) if applied else 0,
        "sample": records[:5],
    })


@app.route("/project/<pid>/entity-sources")
def project_entity_sources(pid: str):
    """Per entiteit: welke processen/BPMNs hem 'bijgedragen' hebben.

    Antwoordt op de vraag 'waar haal je entiteit X vandaan?'.
    """
    if not bpmn_project.is_valid_pid(pid):
        abort(404)
    meta = bpmn_project.load(ROOT, pid)
    if meta is None:
        abort(404)
    summary_path = bpmn_project.project_output_dir(ROOT, pid) / "summary.json"
    if not summary_path.exists():
        return jsonify({"entities": [], "error": "nog geen summary; eerst /analyze draaien"}), 200
    try:
        s = json.loads(summary_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"entities": [], "error": f"kon summary niet lezen: {exc}"}), 500
    out = []
    for e in s.get("erd", {}).get("entities", []):
        if isinstance(e, str):
            out.append({"name": e})
            continue
        out.append({
            "name": e.get("name"),
            "aliases": sorted(set(e.get("aliases", []))),
            "source_processes": e.get("source_processes", []),
            "n_processes": len(e.get("source_processes", [])),
            "is_anchor": e.get("is_anchor", False),
            "is_master": e.get("is_master", False),
            "bpmn_sources": e.get("source_bpmn_ids", [])[:30],
            "attributes": [a if isinstance(a, str) else a.get("name") for a in e.get("attributes", [])][:15],
        })
    return jsonify({"entities": out})


@app.route("/project/<pid>/apply-csv-mapping", methods=["POST"])
def project_apply_csv_mapping(pid: str):
    """Pas handmatige mapping-overrides toe op het laatst geuploade register.csv.

    POST JSON:
        {"overrides": {"CSV-kolom": "canoniek_veld", ...}}

    De tool leest opnieuw `procesregister_upload.csv`, past overrides toe
    bovenop de auto-mapping, en schrijft expected_processes + records weer weg.
    """
    if not bpmn_project.is_valid_pid(pid):
        abort(404)
    meta = bpmn_project.load(ROOT, pid)
    if meta is None:
        abort(404)
    data = request.get_json(silent=True) or {}
    overrides = data.get("overrides", {})
    pdir = bpmn_project.project_root_dir(ROOT, pid)
    csv_path = pdir / "procesregister_upload.csv"
    if not csv_path.exists():
        return jsonify({"error": "geen eerder geuploade CSV gevonden"}), 400

    try:
        records, schema, info = csv_field_detector.detect_and_parse(str(csv_path))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"kon CSV niet opnieuw parsen: {exc}"}), 400

    # Pas overrides toe: vind voor elke override-kolom de kolom-index en zet
    # 'm in het schema onder de gekozen canonieke naam (overschrijf eventueel
    # een auto-mapping).
    import csv as _csv
    with open(csv_path, encoding=info["encoding"]) as f:
        rows = list(_csv.reader(f, delimiter=info["delimiter"]))
    header = rows[info["header_row"]] if rows else []
    col_idx = {c: i for i, c in enumerate(header)}

    new_schema = {}
    # eerst auto-schema
    for k, v in schema.items():
        new_schema[k] = v
    # overrides
    for csv_col, canon in overrides.items():
        if canon in (None, "", "(negeren)"):
            continue
        if csv_col in col_idx:
            # verwijder eventuele andere kolom die dezelfde canon claimde
            for k in [k for k, v in new_schema.items() if k == canon]:
                del new_schema[k]
            new_schema[canon] = col_idx[csv_col]

    # Bouw records opnieuw
    data_rows = rows[info["header_row"] + 1:]
    new_records: list[dict] = []
    for r in data_rows:
        if not any(c.strip() for c in r):
            continue
        rec = {}
        for canon, idx in new_schema.items():
            rec[canon] = r[idx].strip() if idx < len(r) else ""
        if not rec.get("code") and not rec.get("naam") and not rec.get("subproces"):
            continue
        new_records.append(rec)

    # Zet expected_processes
    lines = []
    for rec in new_records:
        code = rec.get("code", "").strip()
        naam = (rec.get("naam") or rec.get("deelproces") or rec.get("subproces") or "").strip()
        hp = rec.get("hoofdproces", "").strip()
        sp = rec.get("subproces", "").strip()
        parts = [x for x in (code, naam) if x]
        context = " / ".join(x for x in (hp, sp) if x)
        label = " ".join(parts)
        if context and label:
            label = f"{label} - {context}"
        elif context:
            label = context
        if label:
            lines.append(label)

    meta["expected_processes"] = lines
    meta["proceslijst_records"] = new_records
    meta["proceslijst_schema"] = new_schema
    meta["proceslijst_csv_info"] = {**info, "mapping": {col: c for c, idx in new_schema.items() for col, ix in col_idx.items() if ix == idx}}
    bpmn_project.save(ROOT, meta)
    return jsonify({"expected_count": len(lines), "records": new_records[:5], "schema": new_schema})


@app.route("/project/<pid>/match-bpmns", methods=["POST"])
def project_match_bpmns(pid: str):
    """Match elk verwacht proces tegen de aanwezige BPMN's van het project.

    Score-systeem:
      +10 code-match in filename
      +2  per overlappend woord (na stop-word filter)
    """
    if not bpmn_project.is_valid_pid(pid):
        abort(404)
    meta = bpmn_project.load(ROOT, pid)
    if meta is None:
        abort(404)
    expected = meta.get("expected_processes", [])
    bpmn_files = meta.get("bpmn_files", [])
    records = meta.get("proceslijst_records", [])

    STOPW = {"l1", "l2", "l3", "l4", "soll", "ist", "v1", "v2", "v3", "v4",
             "de", "het", "een", "en", "in", "van", "bij", "voor", "op",
             "proces", "subproces", "bpmn", "concept"}

    def tok(s: str) -> set[str]:
        return {t for t in re.split(r"[^a-zA-Z0-9]+", s.lower())
                if t and len(t) > 2 and t not in STOPW}

    def code_variants(code: str) -> list[str]:
        return [code, code.replace(".", "_"), code.replace(".", "-"), code.replace(".", "")]

    results = []
    # Als we ruwe records hebben gebruiken we die voor betere match (code + naam apart)
    if records:
        for rec in records:
            code = rec.get("code", "")
            naam = rec.get("naam") or rec.get("deelproces") or rec.get("subproces") or ""
            proc_tokens = tok(naam + " " + rec.get("subproces", "") + " " + rec.get("deelproces", ""))
            matches = []
            for fname in bpmn_files:
                score = 0
                why = []
                fn_low = fname.lower()
                if code:
                    for cv in code_variants(code):
                        if cv and cv in fn_low:
                            score += 10
                            why.append(f"code {cv}")
                            break
                file_tokens = tok(fname.replace("-", " ").replace("_", " "))
                overlap = proc_tokens & file_tokens
                if overlap:
                    score += len(overlap) * 2
                    why.append("woorden: " + ", ".join(sorted(overlap)))
                if score > 0:
                    matches.append({"file": fname, "score": score, "why": "; ".join(why)})
            matches.sort(key=lambda m: -m["score"])
            results.append({
                "expected": f"{code} {naam}".strip(),
                "code": code,
                "matches": matches[:5],
                "best_match": matches[0] if matches and matches[0]["score"] >= 8 else None,
            })
    else:
        # Fallback op enkel expected_processes strings
        for exp in expected:
            proc_tokens = tok(exp)
            matches = []
            for fname in bpmn_files:
                score = 0
                why = []
                # Zoek proces-nummer in expected-string
                m = re.search(r"(\d+\.\d+(?:\.\d+)?)", exp)
                if m:
                    for cv in code_variants(m.group(1)):
                        if cv in fname.lower():
                            score += 10
                            why.append(f"code {cv}")
                            break
                file_tokens = tok(fname.replace("-", " ").replace("_", " "))
                overlap = proc_tokens & file_tokens
                if overlap:
                    score += len(overlap) * 2
                    why.append("woorden: " + ", ".join(sorted(overlap)))
                if score > 0:
                    matches.append({"file": fname, "score": score, "why": "; ".join(why)})
            matches.sort(key=lambda m: -m["score"])
            results.append({
                "expected": exp,
                "code": None,
                "matches": matches[:5],
                "best_match": matches[0] if matches and matches[0]["score"] >= 8 else None,
            })

    return jsonify({"matches": results, "total_bpmns": len(bpmn_files)})


@app.route("/project/<pid>/generate-skeletons", methods=["POST"])
def project_generate_skeletons(pid: str):
    """Genereer een minimaal-geldig skeleton-BPMN voor elk verwacht proces
    dat nog geen bijbehorende BPMN heeft.
    """
    if not bpmn_project.is_valid_pid(pid):
        abort(404)
    meta = bpmn_project.load(ROOT, pid)
    if meta is None:
        abort(404)
    records = meta.get("proceslijst_records", [])
    bpmn_files = meta.get("bpmn_files", [])
    data_dir = bpmn_project.project_data_dir(ROOT, pid)
    data_dir.mkdir(parents=True, exist_ok=True)

    def slugify(s: str) -> str:
        s = re.sub(r"[^a-zA-Z0-9]+", "_", s.lower()).strip("_")
        return s or "proces"

    def safe_xml(s: str) -> str:
        return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                 .replace('"', "&quot;").replace("'", "&apos;"))

    TMPL = """<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
                  xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI"
                  xmlns:dc="http://www.omg.org/spec/DD/20100524/DC"
                  xmlns:di="http://www.omg.org/spec/DD/20100524/DI"
                  id="Definitions_{ID}" targetNamespace="http://bpmn.io/schema/bpmn">
  <bpmn:collaboration id="Collab_{ID}">
    <bpmn:participant id="Part_{ID}" name="{POOL}" processRef="Proc_{ID}" />
    <bpmn:textAnnotation id="TA_{ID}"><bpmn:text>{META}</bpmn:text></bpmn:textAnnotation>
    <bpmn:association id="Assoc_{ID}" associationDirection="None" sourceRef="Task_{ID}" targetRef="TA_{ID}" />
  </bpmn:collaboration>
  <bpmn:process id="Proc_{ID}" isExecutable="false">
    <bpmn:laneSet id="LS_{ID}">
      <bpmn:lane id="Lane_{ID}" name="Onbekend">
        <bpmn:flowNodeRef>Start_{ID}</bpmn:flowNodeRef>
        <bpmn:flowNodeRef>Task_{ID}</bpmn:flowNodeRef>
        <bpmn:flowNodeRef>End_{ID}</bpmn:flowNodeRef>
      </bpmn:lane>
    </bpmn:laneSet>
    <bpmn:startEvent id="Start_{ID}" name="Start {CODE}"><bpmn:outgoing>F_st_{ID}</bpmn:outgoing></bpmn:startEvent>
    <bpmn:task id="Task_{ID}" name="{TASK}">
      <bpmn:incoming>F_st_{ID}</bpmn:incoming>
      <bpmn:outgoing>F_te_{ID}</bpmn:outgoing>
    </bpmn:task>
    <bpmn:endEvent id="End_{ID}" name="Afgerond {CODE}"><bpmn:incoming>F_te_{ID}</bpmn:incoming></bpmn:endEvent>
    <bpmn:sequenceFlow id="F_st_{ID}" sourceRef="Start_{ID}" targetRef="Task_{ID}" />
    <bpmn:sequenceFlow id="F_te_{ID}" sourceRef="Task_{ID}" targetRef="End_{ID}" />
  </bpmn:process>
  <bpmndi:BPMNDiagram id="Diag_{ID}">
    <bpmndi:BPMNPlane id="Plane_{ID}" bpmnElement="Collab_{ID}">
      <bpmndi:BPMNShape id="Part_{ID}_di" bpmnElement="Part_{ID}" isHorizontal="true"><dc:Bounds x="160" y="80" width="720" height="200" /></bpmndi:BPMNShape>
      <bpmndi:BPMNShape id="Lane_{ID}_di" bpmnElement="Lane_{ID}" isHorizontal="true"><dc:Bounds x="190" y="80" width="690" height="200" /></bpmndi:BPMNShape>
      <bpmndi:BPMNShape id="Start_{ID}_di" bpmnElement="Start_{ID}"><dc:Bounds x="240" y="162" width="36" height="36" /></bpmndi:BPMNShape>
      <bpmndi:BPMNShape id="Task_{ID}_di" bpmnElement="Task_{ID}"><dc:Bounds x="340" y="140" width="240" height="80" /></bpmndi:BPMNShape>
      <bpmndi:BPMNShape id="End_{ID}_di" bpmnElement="End_{ID}"><dc:Bounds x="660" y="162" width="36" height="36" /></bpmndi:BPMNShape>
      <bpmndi:BPMNShape id="TA_{ID}_di" bpmnElement="TA_{ID}"><dc:Bounds x="320" y="300" width="440" height="80" /></bpmndi:BPMNShape>
      <bpmndi:BPMNEdge id="F_st_{ID}_di" bpmnElement="F_st_{ID}"><di:waypoint x="276" y="180" /><di:waypoint x="340" y="180" /></bpmndi:BPMNEdge>
      <bpmndi:BPMNEdge id="F_te_{ID}_di" bpmnElement="F_te_{ID}"><di:waypoint x="580" y="180" /><di:waypoint x="660" y="180" /></bpmndi:BPMNEdge>
    </bpmndi:BPMNPlane>
  </bpmndi:BPMNDiagram>
</bpmn:definitions>
"""
    existing_slugs = {slugify(Path(f).stem) for f in bpmn_files}
    created = []
    skipped = []
    from datetime import datetime as _dt
    meta.setdefault("bpmn_origins", {})
    for rec in records:
        code = rec.get("code", "").strip()
        naam = (rec.get("naam") or rec.get("deelproces") or rec.get("subproces") or "").strip()
        if not code and not naam:
            continue
        base_slug = slugify(f"{code}_{naam}")
        if any(base_slug in es or es in base_slug for es in existing_slugs):
            skipped.append(base_slug)
            continue
        fid = slugify(code) or slugify(naam)
        pool = f"[{code}] {naam}"[:100]
        task = f"Uitvoeren {naam[:1].lower()}{naam[1:]}" if naam else "Uitvoeren proces"
        meta_txt = []
        if rec.get("doel"): meta_txt.append(f"Doel: {rec['doel'][:300]}")
        if rec.get("eigenaar"): meta_txt.append(f"Eigenaar: {rec['eigenaar']}")
        if rec.get("sme"): meta_txt.append(f"SME: {rec['sme']}")
        meta_str = " | ".join(meta_txt)[:800]
        xml = TMPL.format(ID=fid, CODE=safe_xml(code), POOL=safe_xml(pool),
                          TASK=safe_xml(task), META=safe_xml(meta_str))
        out = data_dir / f"{base_slug}.bpmn"
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(xml)
        created.append(base_slug + ".bpmn")
        if out.name not in meta.get("bpmn_files", []):
            meta.setdefault("bpmn_files", []).append(out.name)
        meta["bpmn_origins"][out.name] = {
            "kind": "skeleton",
            "kind_label": "Auto-gegenereerd skeleton uit CSV",
            "source": "procesregister_upload.csv",
            "source_row": f"{code} {naam}".strip(),
            "source_doel": rec.get("doel", ""),
            "source_eigenaar": rec.get("eigenaar", ""),
            "source_sme": rec.get("sme", ""),
            "created_at": _dt.now().isoformat(timespec="seconds"),
        }

    bpmn_project.save(ROOT, meta)
    return jsonify({"created": len(created), "files": created, "skipped": skipped})


@app.route("/project/<pid>/bpmn-info/<path:filename>")
def project_bpmn_info(pid: str, filename: str):
    """Retourneer herkomst + anchor-info + gerelateerde BPMN-varianten.

    Response JSON:
        {
          "filename":       "...",
          "origin":         { "kind":..., "kind_label":..., "source":..., "source_row":..., ... }
          "anchor_entities": [ {"name":"Lid", "reason":"komt voor in 5 processen: ..."} ],
          "variants":       [ {"filename":"...", "kind":"origineel|v1|v2|regelconform", "url":"..."} ],
          "raw_url":        "/project/<pid>/bpmn/<filename>",
          "summary_url":    "/project/<pid>/results#bpmn:..."
        }
    """
    if not bpmn_project.is_valid_pid(pid):
        abort(404)
    meta = bpmn_project.load(ROOT, pid)
    if meta is None:
        abort(404)

    origins = meta.get("bpmn_origins", {})
    # Als geen origin bekend is → default: upload
    if filename in origins:
        origin = origins[filename]
    else:
        origin = {
            "kind": "upload",
            "kind_label": "Door gebruiker geupload",
            "source": filename,
        }

    # Varianten zoeken (versies/ folder)
    variants: list[dict] = []
    project_root = bpmn_project.project_root_dir(ROOT, pid)
    versions_base = project_root / "versions"
    base_stem = Path(filename).stem
    if versions_base.exists():
        for sub in versions_base.iterdir():
            if sub.is_dir() and (base_stem.lower() in sub.name.lower() or sub.name.lower() in base_stem.lower()):
                for vf in sorted(sub.glob("*.bpmn")):
                    variants.append({
                        "filename": vf.name,
                        "kind": "versie",
                        "url": f"/project/{pid}/bpmn-original/{vf.relative_to(project_root).as_posix()}",
                    })
    # Actieve BPMN
    data_dir = bpmn_project.project_data_dir(ROOT, pid)
    if (data_dir / filename).exists():
        variants.append({
            "filename": filename,
            "kind": "actief",
            "url": f"/project/{pid}/bpmn/{filename}",
        })

    # Anchor-info: welke entiteiten uit de globale anchors komen vaak voor
    anchors = []
    try:
        all_anchors = bpmn_anchors.get_anchors(ROOT, min_processes=2)
        for a in all_anchors[:50]:
            n_proc = len(a.get("processes", []))
            n_proj = len(a.get("projects", []))
            reason_parts = [f"komt voor in {n_proc} processen"]
            procs = a.get("processes", [])[:5]
            if procs:
                reason_parts.append("bv. " + ", ".join(procs))
            if n_proj > 1:
                reason_parts.append(f"in {n_proj} projecten")
            if a.get("is_master"):
                reason_parts.append("gemarkeerd als master-entiteit")
            anchors.append({
                "name": a["name"],
                "n_processes": n_proc,
                "processes": procs,
                "reason": "; ".join(reason_parts),
                "is_master": a.get("is_master", False),
            })
    except Exception:
        pass

    return jsonify({
        "filename": filename,
        "origin": origin,
        "anchor_entities": anchors,
        "variants": variants,
        "raw_url": f"/project/{pid}/bpmn/{filename}",
        "summary_url": f"/project/{pid}/results#bpmn:{filename}",
    })


def _find_source_trace(code: str) -> dict | None:
    """Zoek in handmade/projects/*/source_trace.json naar een entry voor deze code."""
    base = ROOT / "output" / "handmade" / "projects"
    if not base.exists():
        return None
    for proj_dir in base.iterdir():
        st = proj_dir / "source_trace.json"
        if not st.exists():
            continue
        try:
            data = json.loads(st.read_text(encoding="utf-8"))
        except Exception:
            continue
        # Probeer code varianten
        for variant in (code, code.replace("_", "."), code.replace("-", ".")):
            if variant in data:
                entry = dict(data[variant])
                entry["_source_trace_file"] = str(st.relative_to(ROOT)).replace("\\", "/")
                entry["_project_slug"] = proj_dir.name
                return entry
    return None


def _extract_code_from_filename(fname: str) -> str | None:
    """Haal proces-code uit filename. Bv. '1_1_27_xxx.bpmn' -> '1.1.27'."""
    m = re.search(r"(\d+)[_.\-](\d+)(?:[_.\-](\d+))?(?:[_.\-](\d+))?", fname)
    if not m:
        return None
    parts = [g for g in m.groups() if g is not None]
    return ".".join(parts[:3]) if len(parts) >= 2 else None


@app.route("/project/<pid>/bpmn-sources/<path:filename>")
def project_bpmn_sources(pid: str, filename: str):
    """Retourneer welke Word/PPT bronnen voor deze BPMN zijn geraadpleegd en
    welke wijzigingen zijn doorgevoerd met bron-citaat.
    """
    if not bpmn_project.is_valid_pid(pid):
        abort(404)
    meta = bpmn_project.load(ROOT, pid)
    if meta is None:
        abort(404)

    # Probeer eerst via code uit filename
    code = _extract_code_from_filename(filename)
    trace = _find_source_trace(code) if code else None

    # Als er geen trace is, ook proberen via source_row in origins
    if not trace:
        origin = meta.get("bpmn_origins", {}).get(filename, {})
        row = origin.get("source_row", "")
        m = re.match(r"(\d+\.\d+(?:\.\d+)?)", row)
        if m:
            trace = _find_source_trace(m.group(1))

    if not trace:
        return jsonify({
            "has_trace": False,
            "filename": filename,
            "detected_code": code,
            "message": "Geen bron-trace gevonden voor deze BPMN (nog niet regelconform uitgewerkt of geen source_trace.json-entry).",
        })

    # Voor elke bron: lees bronnen-tekst (eerste N regels) en voeg link toe
    for src in trace.get("sources", []):
        ep = src.get("extract_path", "")
        if ep:
            abs_path = ROOT / ep
            if abs_path.exists():
                try:
                    lines = abs_path.read_text(encoding="utf-8").splitlines()
                    lr = src.get("line_range")
                    if lr and len(lr) == 2:
                        # toon alleen de relevante section
                        src["preview"] = "\n".join(lines[lr[0]-1:lr[1]])
                    else:
                        src["preview"] = "\n".join(lines[:40])
                    src["total_lines"] = len(lines)
                except Exception as e:
                    src["preview"] = f"(kon bestand niet lezen: {e})"
                src["raw_url"] = f"/project/{pid}/source-raw?path=" + ep

    # Review.md ook ophalen indien beschikbaar
    review_md = None
    rp = trace.get("review_path")
    if rp:
        rpath = ROOT / rp
        if rpath.exists():
            try:
                review_md = rpath.read_text(encoding="utf-8")
            except Exception:
                pass

    return jsonify({
        "has_trace": True,
        "filename": filename,
        "detected_code": code,
        "trace": trace,
        "review_md": review_md,
    })


@app.route("/project/<pid>/source-raw")
def project_source_raw(pid: str):
    """Serveer een bron-tekstbestand (bronnen/word_*.txt, ppt_*.txt) in plain text.

    Query-param: ?path=output/handmade/projects/.../bronnen/word_foo.txt
    Veiligheid: pad moet beginnen met 'output/handmade/'.
    """
    if not bpmn_project.is_valid_pid(pid):
        abort(404)
    rel = request.args.get("path", "").replace("\\", "/")
    if not rel.startswith("output/handmade/") or ".." in rel:
        abort(400)
    p = ROOT / rel
    if not p.exists() or not p.is_file():
        abort(404)
    return send_from_directory(p.parent, p.name, mimetype="text/plain")


@app.route("/project/<pid>/bulk-remove-bpmns", methods=["POST"])
def project_bulk_remove_bpmns(pid: str):
    """Verwijder meerdere BPMNs tegelijk uit een project."""
    if not bpmn_project.is_valid_pid(pid):
        abort(404)
    meta = bpmn_project.load(ROOT, pid)
    if meta is None:
        abort(404)
    import shutil
    targets = request.form.getlist("targets")
    data_dir = bpmn_project.project_data_dir(ROOT, pid)
    pdir = bpmn_project.project_root_dir(ROOT, pid)
    for t in targets:
        safe = secure_filename(t)
        if not safe:
            continue
        p = data_dir / safe
        if p.exists():
            p.unlink()
        base = Path(safe).stem
        versions_dir = pdir / "versions" / base
        if versions_dir.exists():
            shutil.rmtree(versions_dir, ignore_errors=True)
    meta["bpmn_order"] = [n for n in meta.get("bpmn_order", [])
                          if n not in {secure_filename(t) for t in targets}]
    bpmn_project.save(ROOT, meta)
    return redirect(url_for("project_detail", pid=pid))


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
        "process_map_mermaid": locals().get("process_map_mermaid", ""),
        "process_relations": locals().get("process_relations", []),
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

    # Proces-relatie-kaart
    process_map_mermaid = ""
    process_relations: list[dict] = []
    try:
        entities_obj, _rels = bpmn_erd.build_erd(model, user_defs=user_defs, project_root=ROOT)
        process_map_mermaid, process_relations = \
            bpmn_process_map.build_process_map(entities_obj)
    except Exception:
        pass

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
        "process_map_mermaid": locals().get("process_map_mermaid", ""),
        "process_relations": locals().get("process_relations", []),
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

@app.route("/anchors")
def anchors_page():
    """Overzicht van alle entities die over alle projecten heen zijn
    waargenomen. Zo ziet de tool (en de gebruiker) wat de gedeelde
    core van het bedrijfsdatamodel is."""
    anchors = bpmn_anchors.get_anchors(ROOT, min_processes=2)
    all_known = bpmn_anchors.get_all_known(ROOT)
    single_use = [e for e in all_known if e["process_count"] < 2]
    projects = bpmn_project.list_projects(ROOT)
    project_names = {p["id"]: p["name"] for p in projects}
    return render_template("anchors.html",
                           anchors=anchors,
                           single_use=single_use,
                           total_known=len(all_known),
                           project_names=project_names)


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


@app.route("/definitions/entity-info/<name>", methods=["GET"])
def definitions_entity_info(name: str):
    """Geef per-entity de definitie + attributen + hun definities terug."""
    data = bpmn_defs.load(ROOT)
    attrs = data.get("objects", {}).get(name, [])
    norm_attrs = []
    for a in attrs:
        if isinstance(a, dict):
            norm_attrs.append({
                "name": a.get("name", ""),
                "type": a.get("type", "string"),
                "required": bool(a.get("required", False)),
                "unique": bool(a.get("unique", False)),
                "pk": bool(a.get("pk", False)),
                "fk": bool(a.get("fk", False)),
                "fk_to": a.get("fk_to", ""),
                "definition": a.get("definition", ""),
            })
    return jsonify({
        "name": name,
        "definition": data.get("object_definitions", {}).get(name, ""),
        "attributes": norm_attrs,
        "discovered": data.get("discovered_objects", {}).get(name, {}),
    })


@app.route("/definitions/entity-definition", methods=["POST"])
def definitions_set_entity_definition():
    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    definition = payload.get("definition", "")
    if not name:
        return jsonify({"error": "Geef name"}), 400
    bpmn_defs.set_object_definition(ROOT, name, definition)
    return jsonify({"ok": True})


@app.route("/definitions/attribute-definition", methods=["POST"])
def definitions_set_attribute_definition():
    payload = request.get_json(silent=True) or {}
    entity = (payload.get("entity") or "").strip()
    attr = (payload.get("attribute") or "").strip()
    definition = payload.get("definition", "")
    if not entity or not attr:
        return jsonify({"error": "Geef entity + attribute"}), 400
    bpmn_defs.set_attribute_definition(ROOT, entity, attr, definition)
    return jsonify({"ok": True})


@app.route("/definitions/relation", methods=["GET"])
def definitions_get_relation():
    a = (request.args.get("a") or "").strip()
    b = (request.args.get("b") or "").strip()
    if not (a and b):
        return jsonify({"error": "Geef a en b"}), 400
    info = bpmn_defs.get_relation_definition(ROOT, a, b)
    return jsonify(info)


@app.route("/definitions/relation", methods=["POST"])
def definitions_set_relation():
    payload = request.get_json(silent=True) or {}
    a = (payload.get("a") or "").strip()
    b = (payload.get("b") or "").strip()
    definition = payload.get("definition", "")
    cardinality = payload.get("cardinality", "")
    if not (a and b):
        return jsonify({"error": "Geef a en b"}), 400
    bpmn_defs.set_relation_definition(ROOT, a, b, definition, cardinality)
    return jsonify({"ok": True})


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

    source_file wordt op de DISPLAY-naam gezet (niet v1.bpmn), zodat
    frontend-matching tussen preview-changes en actieve findings werkt.
    """
    v1 = _resolve_source_file(sdir, safe_filename)
    if v1 is None:
        return []
    import bpmn_parser as _bp
    parsed = _bp.parse_bpmn(v1)
    parsed.source_file = safe_filename   # display-naam, niet v1.bpmn
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
            source, findings, user_defs, display_filename=safe
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
            source, findings, user_defs, display_filename=safe
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
