"""
Flask web-frontend voor de BPMN Data-Inventarisatie tool.

Start met:  python src/webapp.py           (poort 8095)
Of via:     run_web.bat                    (dubbelklik in Verkenner)

Endpoints:
    GET  /                          upload-pagina
    POST /run                       verwerk geuploade .bpmn's
    GET  /session/<sid>             resultaten-pagina
    GET  /session/<sid>/download/<filename>
"""

from __future__ import annotations

import json
import sys
import uuid
from dataclasses import asdict
from pathlib import Path

from flask import (Flask, abort, redirect, render_template, request,
                   send_from_directory, url_for)
from werkzeug.utils import secure_filename

# Zorg dat sibling-modules importeerbaar zijn
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bpmn_parser import parse_all                        # noqa: E402
from merger import merge                                 # noqa: E402
from xlsx_export import write_xlsx                       # noqa: E402
from drawio_export import write_drawio                   # noqa: E402
from docx_export import write_docx                       # noqa: E402


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

    # Valideer extensies
    for f in files:
        ext = Path(f.filename).suffix.lower()
        if ext not in ALLOWED_EXT:
            return render_template(
                "index.html",
                error=f"Bestand '{f.filename}' heeft geen .bpmn/.xml extensie.",
                max_mb=MAX_FILE_MB,
            ), 400

    # Nieuwe sessie
    sid = uuid.uuid4().hex[:12]
    sdir = SESSIONS_DIR / sid
    data_dir = sdir / "data"
    out_dir = sdir / "output"
    data_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    for f in files:
        dest = data_dir / secure_filename(f.filename)
        f.save(str(dest))

    # Pipeline
    bpmns = parse_all(data_dir)
    if not bpmns:
        return render_template(
            "index.html",
            error="Geen geldige BPMN 2.0 bestanden herkend.",
            max_mb=MAX_FILE_MB,
        ), 400

    model = merge(bpmns)

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

    # Summary voor resultaten-pagina
    summary = {
        "sid": sid,
        "files": [{
            "name": b.source_file,
            "process": b.process_name,
            "tasks": len(b.tasks),
            "data_objects": len(b.data_objects),
            "lanes": len(b.lanes),
        } for b in bpmns],
        "totals": {
            "files": len(bpmns),
            "actors": len(model.actors),
            "anchors": len(model.anchor_objects()),
            "rows": len(model.inventory),
        },
        "actors": [{
            "name": a.name,
            "type": "Extern" if a.subtype == "extern" else "Intern",
            "appears_in": a.evidence.get("appears_in", []),
        } for a in model.actors],
        "anchors": model.anchor_objects(),
        "inventory": [asdict(r) for r in model.inventory],
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

    # Tel classificatie-verdeling voor de badges
    classification_counts: dict[str, int] = {}
    for row in summary["inventory"]:
        classification_counts[row["classification"]] = \
            classification_counts.get(row["classification"], 0) + 1

    return render_template(
        "results.html",
        summary=summary,
        classification_counts=classification_counts,
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
