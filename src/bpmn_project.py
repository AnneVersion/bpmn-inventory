"""
Project-container: groepeer BPMNs van eenzelfde bedrijf/domein en laat
ze gezamenlijk analyseren.

Opgeslagen in `output/projects/<project_id>/`:
  project.json       { id, name, created_at, bpmn_order: [...] }
  data/              uploaded .bpmn files (meerdere processen)
  versions/<base>/   per-BPMN versiehistorie (v1.bpmn, v2.bpmn, ...)
  output/            gegenereerde artifacts (xlsx, drawio, docx, summary)

Een project overleeft webapp-herstarts (op disk), in tegenstelling tot
sessies die bedoeld zijn voor ad-hoc checks. Sessie-functionaliteit
blijft naast projecten bestaan.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from pathlib import Path


PROJECT_ID_RE = re.compile(r"^[0-9a-f]{12}$")


def projects_dir(root: Path) -> Path:
    d = root / "output" / "projects"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _project_dir(root: Path, pid: str) -> Path:
    return projects_dir(root) / pid


def _meta_path(root: Path, pid: str) -> Path:
    return _project_dir(root, pid) / "project.json"


def is_valid_pid(pid: str) -> bool:
    return bool(pid) and bool(PROJECT_ID_RE.match(pid))


def list_projects(root: Path) -> list[dict]:
    """Alle projecten als lijst dicts (sorted by created_at desc)."""
    out = []
    for pdir in projects_dir(root).iterdir():
        if not pdir.is_dir() or not is_valid_pid(pdir.name):
            continue
        meta_p = pdir / "project.json"
        if not meta_p.exists():
            continue
        try:
            with meta_p.open("r", encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        # Tel BPMNs op disk + splits op herkomst (upload vs skeleton).
        # Zo ziet de gebruiker op de projectkaart in één oogopslag hoeveel
        # hij zelf heeft aangeleverd versus hoeveel de tool uit het CSV-
        # register heeft gegenereerd.
        data_dir = pdir / "data"
        disk_names: list[str] = []
        if data_dir.exists():
            disk_names = [p.name for p in data_dir.iterdir()
                          if p.suffix.lower() in (".bpmn", ".xml")]
        origins = meta.get("bpmn_origins", {}) or {}
        upload_count = sum(
            1 for n in disk_names if origins.get(n, {}).get("kind") == "upload"
        )
        skeleton_count = sum(
            1 for n in disk_names if origins.get(n, {}).get("kind") == "skeleton"
        )
        other_count = len(disk_names) - upload_count - skeleton_count
        meta["bpmn_count"] = len(disk_names)
        meta["bpmn_upload_count"] = upload_count
        meta["bpmn_skeleton_count"] = skeleton_count
        meta["bpmn_other_count"] = other_count
        out.append(meta)
    out.sort(key=lambda m: m.get("created_at", ""), reverse=True)
    return out


def load(root: Path, pid: str) -> dict | None:
    if not is_valid_pid(pid):
        return None
    p = _meta_path(root, pid)
    if not p.exists():
        return None
    with p.open("r", encoding="utf-8") as f:
        meta = json.load(f)
    meta["bpmn_files"] = list_bpmns(root, pid)
    return meta


def save(root: Path, meta: dict) -> None:
    pid = meta["id"]
    mp = _meta_path(root, pid)
    mp.parent.mkdir(parents=True, exist_ok=True)
    # Strip compute-only velden
    persisted = {k: v for k, v in meta.items() if k != "bpmn_files"}
    with mp.open("w", encoding="utf-8") as f:
        json.dump(persisted, f, indent=2, ensure_ascii=False)


def create(root: Path, name: str) -> dict:
    pid = uuid.uuid4().hex[:12]
    pdir = _project_dir(root, pid)
    (pdir / "data").mkdir(parents=True, exist_ok=True)
    (pdir / "output").mkdir(parents=True, exist_ok=True)
    (pdir / "versions").mkdir(parents=True, exist_ok=True)
    meta = {
        "id": pid,
        "name": name.strip() or "(zonder naam)",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "bpmn_order": [],
    }
    save(root, meta)
    return meta


def delete(root: Path, pid: str) -> bool:
    """Verwijder een project (en alle bestanden) van disk."""
    import shutil
    if not is_valid_pid(pid):
        return False
    pdir = _project_dir(root, pid)
    if not pdir.exists():
        return False
    shutil.rmtree(pdir, ignore_errors=True)
    return True


def list_bpmns(root: Path, pid: str) -> list[str]:
    """Bestandsnamen in `data/` van een project."""
    if not is_valid_pid(pid):
        return []
    data_dir = _project_dir(root, pid) / "data"
    if not data_dir.exists():
        return []
    return sorted([p.name for p in data_dir.iterdir()
                   if p.suffix.lower() in (".bpmn", ".xml")])


def project_data_dir(root: Path, pid: str) -> Path:
    return _project_dir(root, pid) / "data"


def project_output_dir(root: Path, pid: str) -> Path:
    d = _project_dir(root, pid) / "output"
    d.mkdir(parents=True, exist_ok=True)
    return d


def project_root_dir(root: Path, pid: str) -> Path:
    return _project_dir(root, pid)


def rename(root: Path, pid: str, new_name: str) -> dict | None:
    meta = load(root, pid)
    if meta is None:
        return None
    meta["name"] = new_name.strip() or meta["name"]
    save(root, meta)
    return meta


# ---------------------------------------------------------------------------
# Dependency-sort: lifecycle-based topologische volgorde
# ---------------------------------------------------------------------------

def compute_dependency_order(entities: list, source_file_by_process: dict
                              ) -> tuple[list[str], list[dict]]:
    """Bereken een topologische volgorde van BPMN-bestanden op basis
    van entity-lifecycle. Returns (ordered_filenames, reasons).

    Regel: als entity X wordt aangemaakt door proces A (staat als
    creator) en gelezen door proces B (staat als reader of updater),
    dan moet A vóór B komen. Updaters komen na creators; alleen-readers
    komen na creators en updaters.

    `entities` = lijst Entity-objecten uit bpmn_erd.build_erd.
    `source_file_by_process` = {process_name: source_file} mapping.

    `reasons` per file: waarom de file op die positie staat
    (bv. "Moet na P01 omdat dat Lidmaatschap creëert").
    """
    from collections import defaultdict

    # Verzamel alle files (inclusief die zonder data-interactie)
    all_files = set(source_file_by_process.values())

    # Bouw dependency-graph: file -> set(files die hier voor moeten komen)
    must_come_after: dict[str, set[str]] = defaultdict(set)
    reasons_for: dict[str, list[str]] = defaultdict(list)

    for e in entities:
        creators = [p for p in e.creators]
        updaters = [p for p in e.updaters]
        readers  = [p for p in e.readers]

        creator_files = {source_file_by_process.get(p) for p in creators}
        creator_files.discard(None)
        updater_files = {source_file_by_process.get(p) for p in updaters}
        updater_files.discard(None)
        reader_files  = {source_file_by_process.get(p) for p in readers}
        reader_files.discard(None)

        # Updaters komen na creators
        for u in updater_files:
            for c in creator_files:
                if u != c:
                    must_come_after[u].add(c)
                    reasons_for[u].append(
                        f"Na {c!r} omdat dat {e.name!r} aanmaakt "
                        f"(hier wijzigen)."
                    )
        # Readers komen na creators en updaters
        for r in reader_files:
            for c in creator_files:
                if r != c:
                    must_come_after[r].add(c)
                    reasons_for[r].append(
                        f"Na {c!r} omdat dat {e.name!r} aanmaakt."
                    )
            for u in updater_files:
                if r != u and r not in creator_files:
                    must_come_after[r].add(u)
                    reasons_for[r].append(
                        f"Na {u!r} omdat dat {e.name!r} laatst wijzigt."
                    )

    # Kahn's algoritme voor topologische sort
    # Eerst in-degree tellen
    in_degree: dict[str, int] = {f: 0 for f in all_files}
    edges_out: dict[str, set[str]] = defaultdict(set)
    for target, sources in must_come_after.items():
        for src in sources:
            if src not in all_files:
                continue
            edges_out[src].add(target)
            in_degree[target] = in_degree.get(target, 0) + 1

    # Seed: files zonder inkomende edges, alfabetisch gesorteerd voor stabiele output
    ready = sorted([f for f, deg in in_degree.items() if deg == 0])
    ordered: list[str] = []
    while ready:
        f = ready.pop(0)
        ordered.append(f)
        for t in sorted(edges_out.get(f, set())):
            in_degree[t] -= 1
            if in_degree[t] == 0:
                ready.append(t)
        ready.sort()  # Stabiel alfabetisch

    # Als er cyclus is, voeg overgebleven files aan het einde toe
    missing = [f for f in all_files if f not in ordered]
    ordered.extend(sorted(missing))

    # Bouw reasons: per file een korte uitleg
    reasons: list[dict] = []
    for f in ordered:
        r_list = list(dict.fromkeys(reasons_for.get(f, [])))  # uniek, behoud order
        if not r_list:
            # Geen dependencies: leg uit waarom het vrij staat
            if f in {src for src_set in must_come_after.values() for src in src_set}:
                # Anderen hangen van mij af
                r_list = ["Vrij (geen voorgangers) — andere processen hangen van dit af."]
            else:
                r_list = ["Geen lifecycle-afhankelijkheden met andere processen."]
        reasons.append({"file": f, "reasons": r_list[:3]})  # max 3 om te voorkomen dat het te lang wordt

    return ordered, reasons
