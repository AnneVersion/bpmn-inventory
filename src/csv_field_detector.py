"""Intelligente CSV-veldnaam-herkenner voor proceslijsten.

Probleem: elke klant heeft andere kolomnamen (NL/EN/mengsels, duplicaten,
lege kolommen, verschillende delimiters, encodings). Deze tool:

1. Detecteert automatisch:
   - Encoding (utf-8, cp1252, latin-1)
   - Delimiter (; , \\t | )
   - Header-row (1e rij met >= 3 niet-lege kolommen)
   - Welke kolom welk canoniek veld is (via synoniem-matching)
2. Levert een genormaliseerde records-lijst (list[dict]) met canonieke keys.

Gebruik:
    from csv_field_detector import detect_and_parse
    records, schema = detect_and_parse("Processenlijst.csv")
    # records = [{"code": "1.1.27", "naam": "Inschrijven", ...}, ...]
    # schema  = {"code": 0, "naam": 8, ...}  (welke kolom-idx)

CLI:
    python csv_field_detector.py Processenlijst.csv
    python csv_field_detector.py Processenlijst.csv --json  # schema dump
"""
from __future__ import annotations
import csv, re, sys, json, argparse
from pathlib import Path
from difflib import SequenceMatcher

# Canonieke velden + synoniem-lijst (breed over NL/EN varianten)
SCHEMA: dict[str, list[str]] = {
    "code": [
        "proces nummer", "procesnummer", "process number", "code", "id",
        "proces id", "nummer proces", "process id", "procescode",
    ],
    "kern": [
        "kern", "kernproces", "kerncategorie", "core", "category",
    ],
    "hoofdproces": [
        "hoofdproces", "main process", "l1", "hoofd", "domein", "domain",
        "hoofdcategorie", "business domain",
    ],
    "subproces": [
        "subproces", "sub-process", "sub process", "l2", "sub",
        "subcategorie", "sub category",
    ],
    "deelproces": [
        "deelproces", "deel-proces", "l3", "deel", "activity",
        "task group", "processtap",
    ],
    "naam": [
        "naam", "name", "titel", "title", "deelprocesnaam",
        "process name", "omschrijving kort", "short description",
    ],
    "doel": [
        "doel", "goal", "purpose", "objective", "doelstelling",
        "beschrijving", "description", "omschrijving", "summary",
    ],
    "link": [
        "link", "url", "link naar volledige beschrijving", "documentatie",
        "documentation", "reference", "referentie", "bron", "source",
        "pdf", "link naar documentatie",
    ],
    "eigenaar": [
        "proceseigenaar", "owner", "process owner", "eigenaar",
        "verantwoordelijke", "responsible",
    ],
    "sme": [
        "sme", "subject matter expert", "expert", "inhoudelijke expert",
        "domein expert",
    ],
    "betrokken": [
        "betrokken", "betrokken bij totstandkoming", "stakeholders",
        "participants", "deelnemers", "involved",
    ],
    "opmerkingen": [
        "opmerkingen", "opmerking", "notes", "remarks", "comments",
        "toelichting",
    ],
    "kennishouders": [
        "kennishouders", "kennishouder", "knowledge holders",
        "kennis houder",
    ],
    "status": [
        "status", "state", "voortgang", "progress", "planning",
    ],
    "prioriteit": [
        "prioriteit", "priority", "priority level",
    ],
    "trigger": [
        "trigger", "start event", "aanleiding", "initiator",
    ],
    "resultaat": [
        "resultaat", "result", "outcome", "uitkomst", "output",
    ],
    "actoren": [
        "actoren", "actoren/rollen", "rollen", "rol", "roles", "actors",
        "deelnemende rollen",
    ],
    "systemen": [
        "systemen", "systems", "applicaties", "applications", "tools",
        "ondersteunende systemen",
    ],
    "kanalen": [
        "kanalen", "channels", "communication channels",
        "gebruikte kanalen",
    ],
}


# Velden waarvan duplicate kolommen mogen voorkomen (bv. meerdere "Nummering")
FLEX_DUP = {"nummering"}

# Stoplist voor kolomnamen die we negeren
STOP = {"", "nummering"}


def _norm(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9/ -]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _fuzzy(a: str, b: str) -> float:
    return SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def match_field(col: str, used_fields: set[str]) -> tuple[str | None, float]:
    """Return (canonical_field, score) or (None, 0) if no match."""
    col_n = _norm(col)
    if col_n in STOP:
        return (None, 0.0)
    best = (None, 0.0)
    for canon, synonyms in SCHEMA.items():
        if canon in used_fields:
            continue
        # Exacte match op een synoniem = hoogste score
        for syn in synonyms:
            if _norm(syn) == col_n:
                return (canon, 1.0)
        # Substring match
        for syn in synonyms:
            if _norm(syn) in col_n or col_n in _norm(syn):
                sc = 0.85
                if sc > best[1]:
                    best = (canon, sc)
        # Fuzzy match
        for syn in synonyms:
            sc = _fuzzy(syn, col)
            if sc > best[1] and sc > 0.75:
                best = (canon, sc)
    return best


def detect_encoding(path: str) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            with open(path, encoding=enc) as f:
                f.read(4096)
            return enc
        except UnicodeDecodeError:
            continue
    return "latin-1"


def detect_delimiter(sample: str) -> str:
    scores = {}
    for d in [";", ",", "\t", "|"]:
        counts = [line.count(d) for line in sample.splitlines() if line.strip()]
        if not counts:
            continue
        # beste = meest consistent + frequent
        avg = sum(counts) / len(counts)
        var = sum((c - avg) ** 2 for c in counts) / len(counts)
        if avg >= 2:
            scores[d] = (avg, -var)
    if not scores:
        return ","
    # kies delim met hoogste gemiddelde, bij gelijk lagere variantie
    return max(scores, key=lambda d: (scores[d][0], scores[d][1]))


def detect_header_row(rows: list[list[str]]) -> int:
    """Vind eerste rij met minstens 3 niet-lege kolommen zonder veel cijfers."""
    for i, r in enumerate(rows[:10]):
        non_empty = [c for c in r if c.strip()]
        if len(non_empty) < 3:
            continue
        # Header = vooral tekst, weinig cijfers
        total_chars = sum(len(c) for c in non_empty)
        digit_chars = sum(sum(1 for ch in c if ch.isdigit()) for c in non_empty)
        if total_chars and digit_chars / total_chars < 0.3:
            return i
    return 0


def detect_and_parse(path: str, verbose: bool = False) -> tuple[list[dict], dict[str, int], dict]:
    """Hoofdfunctie. Retourneer (records, schema, info).

    records: list[dict] met canonieke keys
    schema:  {"code": col_idx, "naam": col_idx, ...}
    info:    {"encoding", "delimiter", "header_row", "n_rows", "unmapped_columns"}
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(path)

    enc = detect_encoding(str(p))
    if verbose:
        print(f"[detect] encoding = {enc}")

    with open(p, encoding=enc) as f:
        raw = f.read()

    delim = detect_delimiter(raw[:4000])
    if verbose:
        print(f"[detect] delimiter = '{delim}'")

    rows = list(csv.reader(raw.splitlines(), delimiter=delim))
    hr = detect_header_row(rows)
    if verbose:
        print(f"[detect] header_row = {hr}")

    header = rows[hr]
    data_rows = rows[hr + 1:]

    # Map elke kolom aan canoniek veld
    schema: dict[str, int] = {}
    col_mapping: list[tuple[int, str, str | None, float]] = []
    used = set()
    for i, col in enumerate(header):
        canon, score = match_field(col, used)
        col_mapping.append((i, col, canon, score))
        if canon and canon not in used:
            schema[canon] = i
            used.add(canon)

    if verbose:
        print("[mapping]")
        for i, col, canon, score in col_mapping:
            mark = "OK" if canon else "  "
            print(f"  {mark} [{i:2}] {col!r:40} -> {canon or '(ongebruikt)':15} score={score:.2f}")

    # Bouw records
    records = []
    for r in data_rows:
        if not any(c.strip() for c in r):
            continue
        rec = {}
        for canon, idx in schema.items():
            if idx < len(r):
                rec[canon] = r[idx].strip()
            else:
                rec[canon] = ""
        # Skip records zonder code of naam
        if not rec.get("code", "") and not rec.get("naam", "") and not rec.get("subproces", ""):
            continue
        records.append(rec)

    info = {
        "encoding": enc,
        "delimiter": delim,
        "header_row": hr,
        "n_rows": len(records),
        "unmapped_columns": [c for _, c, canon, _ in col_mapping if not canon and c.strip()],
        "mapping": {col: canon for _, col, canon, _ in col_mapping if canon},
    }
    return records, schema, info


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_path")
    ap.add_argument("--json", action="store_true", help="Dump full schema + records as JSON")
    ap.add_argument("--preview", type=int, default=5, help="Aantal records om te tonen")
    args = ap.parse_args()

    records, schema, info = detect_and_parse(args.csv_path, verbose=not args.json)

    if args.json:
        print(json.dumps({"info": info, "schema": schema, "records": records},
                        ensure_ascii=False, indent=2))
        return

    print("\n=== Schema (canoniek veld -> kolom-index) ===")
    for k, v in schema.items():
        print(f"  {k:20} -> kolom {v}")

    print(f"\n=== {len(records)} records (preview {args.preview}) ===")
    for rec in records[:args.preview]:
        print(f"--- {rec.get('code','?')} ---")
        for k, v in rec.items():
            if v:
                vs = v[:80] + "..." if len(v) > 80 else v
                print(f"  {k:15} = {vs}")

    if info["unmapped_columns"]:
        print(f"\n=== Niet-herkende kolommen ===")
        for c in info["unmapped_columns"]:
            print(f"  - {c!r}")
        print("\nTip: voeg ontbrekende kolomnamen toe als synoniem in SCHEMA.")


if __name__ == "__main__":
    main()
