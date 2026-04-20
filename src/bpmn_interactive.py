"""
Interactieve tekst-verwerker voor .docx / .pptx documenten.

Workflow:
  1. Upload een document → `build_chunks_file()` parseert secties/paragrafen
     naar een JSON-lijst met chunks + auto-detectie (entities, verbs, systems).
  2. De review-GUI toont chunk-voor-chunk het fragment met voorgestelde
     classificaties. Gebruiker bevestigt of slaat over.
  3. Beslissingen worden per chunk weggeschreven in hetzelfde JSON-bestand
     zodat de voortgang gepersisteerd blijft tussen requests.

Doel: gebruiker niet overstelpen met een 50-pagina batch-import maar stap
voor stap het domein leren kennen, met audit-trail per interpretatie.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from bpmn_docs import extract_docx, extract_pptx, DocSection, ParsedDoc
from bpmn_erd import (CANONICAL_MAP, SYSTEM_TERMS, EXCLUDE_TERMS,
                      canonicalize, load_project_decisions)
from bpmn_review import VERB_ACTIONS, DATA_NOUNS


CHUNKS_FILENAME = "interactive_chunks.json"


# ---------------------------------------------------------------------------
# Chunk-builder
# ---------------------------------------------------------------------------

def _flatten_sections(sections: list[DocSection],
                      acc: list[dict] | None = None,
                      breadcrumb: list[str] | None = None) -> list[dict]:
    """Vertaal een boom van DocSections naar een platte lijst chunks.

    Elke paragraaf binnen een sectie wordt zijn eigen chunk — kort genoeg
    om in één review-stap te behandelen. Lege paragrafen worden
    overgeslagen. Sectie-titels gaan mee als breadcrumb zodat de reviewer
    context heeft.
    """
    if acc is None:
        acc = []
    breadcrumb = breadcrumb or []
    for sec in sections:
        path = breadcrumb + [sec.title] if sec.title and sec.title != "(root)" else list(breadcrumb)
        # Heading zelf als chunk als die inhoudelijk iets zegt (>3 chars)
        if sec.title and sec.title.strip() and sec.title != "(root)":
            acc.append({
                "kind": "heading",
                "level": sec.level,
                "text": sec.title.strip(),
                "breadcrumb": path,
            })
        for p in sec.paragraphs:
            p = (p or "").strip()
            if len(p) < 3:
                continue
            acc.append({
                "kind": "paragraph",
                "level": sec.level,
                "text": p,
                "breadcrumb": path,
            })
        _flatten_sections(sec.children, acc, path)
    return acc


def _detect_candidates(text: str) -> dict:
    """Heuristische kandidaten-detectie op een stuk tekst.

    Returned structuur (alles lijsten van dicts met `match`, `canonical`):
      entities      — termen die naar een bekende entity canonicaliseren
      systems       — termen die in SYSTEM_TERMS staan
      verbs         — data-werkwoord-matches uit VERB_ACTIONS
      processsteps  — verb+noun-paren (suggestie: taakstap)
      excluded      — termen die expliciet op de exclude-lijst staan
                      (transparant: gebruiker ziet wat weggefilterd is)
    """
    low = text.lower()
    out: dict[str, list[dict]] = {
        "entities": [], "systems": [], "verbs": [],
        "processsteps": [], "excluded": [],
    }
    seen_ent: set[str] = set()
    seen_sys: set[str] = set()

    # Entities & excluded
    for raw in CANONICAL_MAP.keys():
        if len(raw) < 3:
            continue
        if not re.search(r"\b" + re.escape(raw) + r"\w{0,3}\b", low):
            continue
        canon = canonicalize(raw)
        if not canon:
            if raw in EXCLUDE_TERMS and raw not in seen_ent:
                out["excluded"].append({
                    "match": raw, "reason": "EXCLUDE_TERMS",
                })
                seen_ent.add(raw)
            continue
        if canon in seen_ent:
            continue
        seen_ent.add(canon)
        out["entities"].append({"match": raw, "canonical": canon})

    # Systems
    for sys in SYSTEM_TERMS:
        if len(sys) < 3:
            continue
        if re.search(r"\b" + re.escape(sys) + r"\w{0,3}\b", low):
            if sys not in seen_sys:
                seen_sys.add(sys)
                out["systems"].append({"match": sys})

    # Verbs
    verbs_found = []
    for verb, action in VERB_ACTIONS.items():
        if verb in low:
            verbs_found.append({"verb": verb, "action": action})
    out["verbs"] = verbs_found

    # Process-steps = verb + noun samen
    nouns_found = [n for n in DATA_NOUNS if n in low]
    if verbs_found and nouns_found:
        out["processsteps"].append({
            "verbs": [v["verb"] for v in verbs_found][:3],
            "nouns": nouns_found[:3],
            "action": verbs_found[0]["action"],
            "suggested_task_name":
                f"{verbs_found[0]['verb'].capitalize()} {nouns_found[0]}",
        })
    return out


# ---------------------------------------------------------------------------
# Files & state
# ---------------------------------------------------------------------------

def doc_subdir(project_dir: Path, doc_id: str) -> Path:
    sub = project_dir / "documents" / doc_id
    sub.mkdir(parents=True, exist_ok=True)
    return sub


def chunks_file(project_dir: Path, doc_id: str) -> Path:
    return doc_subdir(project_dir, doc_id) / CHUNKS_FILENAME


def build_chunks_file(project_dir: Path, source_file: Path,
                      original_name: str,
                      project_root: Path | None = None) -> tuple[str, Path]:
    """Parse het document één keer en schrijf de chunks + detectie naar
    een JSON-bestand. Retourneer (doc_id, path naar chunks.json).

    `source_file` is het bestand op disk; `original_name` de originele
    filename (voor audit).
    """
    # Zorg dat CANONICAL_MAP / SYSTEM_TERMS up-to-date zijn
    if project_root is not None:
        try:
            load_project_decisions(project_root)
        except Exception:
            pass

    ext = source_file.suffix.lower()
    if ext == ".docx":
        parsed = extract_docx(source_file)
    elif ext == ".pptx":
        parsed = extract_pptx(source_file)
    else:
        raise ValueError(f"Onbekend doctype: {ext}")

    doc_id = uuid.uuid4().hex[:12]
    sub = doc_subdir(project_dir, doc_id)
    # Bewaar het origineel in de doc-subdir zodat het download-bar
    # dezelfde structuur volgt als het bestaande /doc/<doc_id>/raw endpoint.
    dest_orig = sub / f"original{ext}"
    if not dest_orig.exists():
        dest_orig.write_bytes(source_file.read_bytes())

    chunks = _flatten_sections(parsed.sections)
    for i, ch in enumerate(chunks):
        ch["idx"] = i
        ch["candidates"] = _detect_candidates(ch["text"])
        ch["decision"] = None     # None | 'processed' | 'skipped'
        ch["decided_at"] = None
        ch["applied"] = []        # lijst dicts: wat is toegepast

    data = {
        "doc_id": doc_id,
        "source_file": original_name,
        "kind": parsed.kind,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "total_chunks": len(chunks),
        "chunks": chunks,
    }
    path = chunks_file(project_dir, doc_id)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return doc_id, path


def load_chunks(project_dir: Path, doc_id: str) -> dict | None:
    p = chunks_file(project_dir, doc_id)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def save_chunks(project_dir: Path, doc_id: str, data: dict) -> None:
    p = chunks_file(project_dir, doc_id)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                 encoding="utf-8")


def list_interactive_docs(project_dir: Path) -> list[dict]:
    """Alle doc_id-folders die een chunks.json bevatten."""
    base = project_dir / "documents"
    if not base.exists():
        return []
    out = []
    for sub in sorted(base.iterdir()):
        if not sub.is_dir():
            continue
        p = sub / CHUNKS_FILENAME
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        total = data.get("total_chunks", 0)
        chunks = data.get("chunks", [])
        processed = sum(1 for c in chunks
                        if c.get("decision") == "processed")
        skipped = sum(1 for c in chunks
                      if c.get("decision") == "skipped")
        out.append({
            "doc_id": data.get("doc_id", sub.name),
            "source_file": data.get("source_file", ""),
            "kind": data.get("kind", ""),
            "created_at": data.get("created_at", ""),
            "total": total,
            "processed": processed,
            "skipped": skipped,
            "remaining": total - processed - skipped,
        })
    return out


def apply_chunk_decision(project_dir: Path, doc_id: str,
                         chunk_idx: int, action: str,
                         payload: dict | None = None) -> dict:
    """Registreer een beslissing op een chunk.

    action = 'process' | 'skip'
    payload (voor process):
      {"entities": [{"canonical":"CAO","match":"cao"}, ...],
       "systems":  [{"match":"crm"}, ...],
       "processsteps": [{"suggested_task_name":"..."}, ...]}
    """
    data = load_chunks(project_dir, doc_id)
    if data is None:
        raise ValueError(f"Onbekend doc_id {doc_id}")
    chunks = data["chunks"]
    if chunk_idx < 0 or chunk_idx >= len(chunks):
        raise ValueError(f"chunk_idx {chunk_idx} buiten bereik")
    ch = chunks[chunk_idx]
    if action == "skip":
        ch["decision"] = "skipped"
    elif action == "process":
        ch["decision"] = "processed"
        ch["applied"] = payload or {}
    else:
        raise ValueError(f"Onbekende action: {action}")
    ch["decided_at"] = datetime.now().isoformat(timespec="seconds")
    save_chunks(project_dir, doc_id, data)
    # Zoek volgende onbesliste chunk
    next_idx = None
    for i in range(chunk_idx + 1, len(chunks)):
        if not chunks[i].get("decision"):
            next_idx = i
            break
    return {"ok": True, "next_idx": next_idx, "chunk": ch}
