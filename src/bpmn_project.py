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
        # Tel BPMNs
        data_dir = pdir / "data"
        bpmn_count = 0
        if data_dir.exists():
            bpmn_count = sum(1 for p in data_dir.iterdir()
                             if p.suffix.lower() in (".bpmn", ".xml"))
        meta["bpmn_count"] = bpmn_count
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
