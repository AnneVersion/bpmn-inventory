"""
BPMN Data-Inventarisatie Tool — orchestrator.

Usage:
    python main.py                     # uses ./data and writes to ./output
    python main.py --data DIR --out DIR

Input  : alle .bpmn-bestanden in --data
Output : in --out
    - data-inventarisatie.xlsx        (template ingevuld vanuit BPMN)
    - bpmn-en-erd.drawio              (twee pagina's: BPMN + ERD)
    - rapport.docx                    (procesoverzicht + methodiek)
    - inventory.json                  (machine-readable dump)
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

# Make sibling modules importable when called as `python src/main.py`
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bpmn_parser import parse_all
from merger import merge
from xlsx_export import write_xlsx
from drawio_export import write_drawio
from docx_export import write_docx


def _serialise_inventory(model) -> list[dict]:
    return [asdict(r) for r in model.inventory]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    here = Path(__file__).resolve().parent.parent
    ap.add_argument("--data", type=Path, default=here / "data",
                    help="Map met .bpmn-bestanden")
    ap.add_argument("--out", type=Path, default=here / "output",
                    help="Map waar outputs worden weggeschreven")
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)

    print(f"[1/5] BPMN's lezen uit {args.data}")
    bpmns = parse_all(args.data)
    if not bpmns:
        print("  GEEN .bpmn-bestanden gevonden. Stop.")
        return 1
    for b in bpmns:
        print(f"   - {b.source_file}: "
              f"{len(b.tasks)} tasks, {len(b.data_objects)} dataobjects, "
              f"{len(b.lanes)} lanes")

    print("[2/5] Samenvoegen + classificeren")
    model = merge(bpmns)
    print(f"   actoren:        {len(model.actors)}")
    print(f"   ankerobjecten:  {len(model.anchor_objects())}")
    print(f"   inventory rows: {len(model.inventory)}")

    print("[3/5] Excel-template invullen")
    xlsx_path = args.out / "data-inventarisatie.xlsx"
    write_xlsx(model, str(xlsx_path))
    print(f"   -> {xlsx_path}")

    print("[4/5] draw.io-bestand schrijven (BPMN + ERD)")
    drawio_path = args.out / "bpmn-en-erd.drawio"
    write_drawio(model, str(drawio_path))
    print(f"   -> {drawio_path}")

    print("[5/5] Word-rapport genereren")
    docx_path = args.out / "rapport.docx"
    write_docx(model, str(docx_path))
    print(f"   -> {docx_path}")

    # bonus: machine-readable
    json_path = args.out / "inventory.json"
    with json_path.open("w", encoding="utf-8") as f:
        json.dump({
            "files": [b.source_file for b in bpmns],
            "actors": [{"name": a.name, "type": a.subtype,
                        "appears_in": a.evidence.get("appears_in", [])}
                       for a in model.actors],
            "anchors": model.anchor_objects(),
            "inventory": _serialise_inventory(model),
        }, f, indent=2, ensure_ascii=False)
    print(f"   -> {json_path}")

    print("\nKlaar.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
