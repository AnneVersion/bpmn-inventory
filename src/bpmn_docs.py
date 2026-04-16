"""
Document-ingestie: .docx / .pptx parsing + BPMN-generatie uit beschrijvende tekst.

Workflow:
1. Upload: /project/<pid>/upload-doc slaat de raw file op in
   projects/<pid>/documents/<doc_id>/original.docx (of .pptx).
2. Parse: extract_docx/extract_pptx leest headings + paragraphs en
   retourneert een gestructureerde `ParsedDoc` met secties.
3. BPMN-genereren: `generate_bpmn_from_doc()` maakt per sectie (sub)-
   proces een .bpmn met heuristische taakherkenning en dataObjects.
4. Entity-extractie: scan alle noun-phrases en kandidaat-attributen;
   upsert in definitions (/definities krijgt ze als auto-discovered).
5. Audit: elke doc krijgt een docs.json-entry met processed_at,
   generated_bpmns, extracted_entities, extracted_attributes.

De BPMN-generator is expliciet HEURISTISCH. De output wordt toegevoegd
als reguliere BPMN aan het project, zodat review/apply/... erop werken.
"""

from __future__ import annotations

import io
import json
import re
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

BPMN_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL"
DI_NS = "http://www.omg.org/spec/BPMN/20100524/DI"
DC_NS = "http://www.omg.org/spec/DD/20100524/DC"
DIAG_NS = "http://www.omg.org/spec/DD/20100524/DI"


# Trefwoorden die een "proces-start"-zin markeren
PROCESS_HEADING_HINTS = [
    "proces", "procedure", "workflow", "werkwijze", "stappen",
    "afhandeling", "behandeling",
]

# Werkwoorden die sterke indicatie zijn dat een zin een task beschrijft
TASK_VERBS = [
    "registreer", "registreren", "opzoek", "opzoeken", "raadpleeg", "raadplegen",
    "controleer", "controleren", "valideer", "valideren", "beoordeel", "beoordelen",
    "stuur", "sturen", "verzend", "verzenden", "verstuur", "ontvang", "ontvangen",
    "voer in", "invoeren", "invullen", "vul in", "toevoeg", "toevoegen",
    "wijzig", "wijzigen", "muteer", "muteren", "pas aan", "aanpassen",
    "verwerk", "verwerken", "maak aan", "aanmaken", "cre\u00eber", "creeer",
    "opsla", "opslaan", "bewaar", "bewaren", "vastleg", "vastleggen",
    "incasseer", "incasseren", "betaal", "betalen", "factureer", "factureren",
    "goedkeur", "goedkeuren", "afwijs", "afwijzen", "uitschrijv", "uitschrijven",
]


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class DocSection:
    """Eén hoofd- of sub-sectie uit een document."""
    level: int                   # 1=h1, 2=h2, ...
    title: str
    paragraphs: list[str] = field(default_factory=list)
    children: list["DocSection"] = field(default_factory=list)

    def full_text(self) -> str:
        lines = [self.title] + self.paragraphs
        for c in self.children:
            lines.append(c.full_text())
        return "\n".join(lines)


@dataclass
class ParsedDoc:
    source_file: str
    kind: str                    # 'docx' | 'pptx'
    sections: list[DocSection] = field(default_factory=list)
    raw_text: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_id(raw: str, prefix: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9]+", "_", raw).strip("_") or "X"
    return f"{prefix}_{clean}_{uuid.uuid4().hex[:6]}"


def _slugify(text: str, max_len: int = 40) -> str:
    s = re.sub(r"[^A-Za-z0-9\-_ ]+", "", text).strip()
    s = re.sub(r"\s+", "_", s)
    return s[:max_len] or "doc"


# ---------------------------------------------------------------------------
# DOCX parsing
# ---------------------------------------------------------------------------

def extract_docx(path: Path) -> ParsedDoc:
    """Lees een .docx in en bouw een hiërarchische sectiestructuur
    op basis van Heading-styles (Heading 1, Heading 2, ...).

    Als er geen headings zijn wordt alles onder één fictieve sectie
    "Document" geplaatst.
    """
    from docx import Document
    doc = Document(path)
    root = DocSection(level=0, title="(root)")
    stack: list[DocSection] = [root]
    raw_lines: list[str] = []

    for para in doc.paragraphs:
        text = (para.text or "").strip()
        if not text:
            continue
        raw_lines.append(text)
        style_name = (para.style.name if para.style else "") or ""
        lvl = None
        m = re.match(r"Heading\s+(\d+)", style_name, re.IGNORECASE)
        if m:
            try:
                lvl = int(m.group(1))
            except ValueError:
                lvl = None
        if lvl is None and style_name.lower() == "title":
            lvl = 1

        if lvl is not None:
            # Nieuwe sectie op diepte lvl
            while stack and stack[-1].level >= lvl:
                stack.pop()
            section = DocSection(level=lvl, title=text)
            if not stack:
                stack = [root]
            stack[-1].children.append(section)
            stack.append(section)
        else:
            # Paragraaf hoort bij top-van-stack, of bij root als leeg
            target = stack[-1] if stack else root
            target.paragraphs.append(text)

    # Als helemaal geen headings: 1 fallback-sectie
    if not root.children and root.paragraphs:
        fallback = DocSection(level=1, title=path.stem,
                              paragraphs=root.paragraphs)
        root.children.append(fallback)
        root.paragraphs = []

    return ParsedDoc(
        source_file=path.name, kind="docx",
        sections=root.children,
        raw_text="\n".join(raw_lines),
    )


# ---------------------------------------------------------------------------
# PPTX parsing
# ---------------------------------------------------------------------------

def extract_pptx(path: Path) -> ParsedDoc:
    """Lees een .pptx: elke slide = 1 sectie (level=1).

    Titel van de slide = section.title, overige tekst in placeholders
    en textbox = paragraphs.
    """
    from pptx import Presentation
    pres = Presentation(str(path))
    sections: list[DocSection] = []
    raw_lines: list[str] = []

    for i, slide in enumerate(pres.slides, start=1):
        title = f"Slide {i}"
        paragraphs: list[str] = []
        for shape in slide.shapes:
            if not shape.has_text_frame:
                continue
            tf = shape.text_frame
            is_title = False
            try:
                is_title = bool(shape.is_placeholder
                                and shape.placeholder_format
                                and shape.placeholder_format.idx == 0)
            except Exception:
                is_title = False
            for para in tf.paragraphs:
                text = "".join(run.text or "" for run in para.runs).strip()
                if not text:
                    continue
                raw_lines.append(text)
                if is_title and title == f"Slide {i}":
                    title = text
                    is_title = False  # volgende paragraphs zijn geen titel
                else:
                    paragraphs.append(text)
        sections.append(DocSection(level=1, title=title, paragraphs=paragraphs))

    return ParsedDoc(
        source_file=path.name, kind="pptx",
        sections=sections,
        raw_text="\n".join(raw_lines),
    )


# ---------------------------------------------------------------------------
# Task-herkenning: van zin -> (verb, rest)
# ---------------------------------------------------------------------------

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _split_sentences(text: str) -> list[str]:
    out = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        # Bullets strip
        line = re.sub(r"^[-*\u2022]\s*", "", line)
        for s in _SENT_SPLIT.split(line):
            s = s.strip()
            if s:
                out.append(s)
    return out


def _find_task_verb(sentence: str) -> str | None:
    low = sentence.lower()
    for v in TASK_VERBS:
        if re.search(r"\b" + re.escape(v), low):
            return v
    return None


def _capitalize(text: str) -> str:
    return text[0].upper() + text[1:] if text else text


def _extract_task_name(sentence: str, verb: str) -> str:
    """Bouw een BPMN-achtige taaknaam: imperative verb + object.

    Simpel maar effectief: neem de matchende verb-stam + daarna
    max 6 woorden.
    """
    low = sentence.lower()
    m = re.search(r"\b" + re.escape(verb) + r"\w*\s+(.{3,80})", low)
    if not m:
        return _capitalize(sentence)[:80]
    rest = m.group(1)
    # Stop bij komma of punt
    rest = re.split(r"[,.;]", rest)[0].strip()
    # Beperk tot ~6 woorden
    words = rest.split()[:6]
    return _capitalize(verb + " " + " ".join(words)).strip()


# ---------------------------------------------------------------------------
# Entity + attribuut extractie
# ---------------------------------------------------------------------------

# Hints dat een woord/frase een entity is
ENTITY_HINTS = [
    "lid", "lidmaatschap", "persoon", "organisatie", "bedrijf", "werkgever",
    "klant", "contract", "aanvraag", "verzoek", "dossier", "factuur",
    "betaling", "machtiging", "incasso", "account", "profiel", "melding",
    "cao", "formulier", "mandaat", "werknemer", "contributie", "jaaropgave",
    "tariefgroep", "verzekering", "polis", "declaratie",
]

# Hints voor attributen (die horen bij entities)
ATTR_HINTS = [
    "naam", "emailadres", "email", "telefoonnummer", "telefoon",
    "adres", "straat", "huisnummer", "postcode", "woonplaats",
    "geboortedatum", "datum", "bsn", "iban", "kvknummer", "kvk",
    "lidnummer", "klantnummer", "factuurnummer", "status", "bedrag",
    "startdatum", "einddatum", "geslacht", "rechtsvorm",
]


@dataclass
class ExtractedEntities:
    entities: dict[str, set[str]] = field(default_factory=dict)  # name -> attributes
    mentions_by_section: dict[str, set[str]] = field(default_factory=dict)  # section_title -> entities


def extract_entities(doc: ParsedDoc) -> ExtractedEntities:
    """Scan alle tekst op bekende entity-woorden + omliggende attribute-woorden.

    Eenvoudig: als in één paragraaf zowel entity E als attribuut A voorkomen,
    dan geldt A als attribuut van E. Dit is heuristisch — je moet in
    /definities handmatig verfijnen.
    """
    ex = ExtractedEntities()

    def walk(sec: DocSection):
        full = " ".join([sec.title] + sec.paragraphs).lower()
        seen_ents = set()
        for ent in ENTITY_HINTS:
            if re.search(r"\b" + ent + r"\w*\b", full):
                # Canonical-case naam (Title case)
                nm = ent.capitalize()
                seen_ents.add(nm)
                ex.entities.setdefault(nm, set())
        for attr in ATTR_HINTS:
            if re.search(r"\b" + attr + r"\w*\b", full) and seen_ents:
                # Ken attribute toe aan alle entities in dezelfde paragraaf
                for nm in seen_ents:
                    ex.entities.setdefault(nm, set()).add(attr)
        if seen_ents:
            ex.mentions_by_section.setdefault(sec.title, set()).update(seen_ents)
        for child in sec.children:
            walk(child)

    for s in doc.sections:
        walk(s)
    return ex


# ---------------------------------------------------------------------------
# BPMN-generatie
# ---------------------------------------------------------------------------

def _register_namespaces():
    ET.register_namespace("bpmn", BPMN_NS)
    ET.register_namespace("bpmndi", DI_NS)
    ET.register_namespace("dc", DC_NS)
    ET.register_namespace("di", DIAG_NS)


def _qname(ns: str, tag: str) -> str:
    return f"{{{ns}}}{tag}"


def section_to_bpmn(section: DocSection) -> tuple[bytes, list[dict]]:
    """Bouw een .bpmn XML-bytes uit één sectie.

    - Sectietitel = proces-naam
    - Elke paragraaf/zin met een task-verb = userTask
    - Start- en end-event automatisch
    - Entities in de sectie = dataObjects
    - Basic DI-waypoints zodat bpmn-js kan renderen

    Returns (xml_bytes, tasks_meta) waar tasks_meta per task de
    brontekst bevat (voor "origin"-linking later).
    """
    _register_namespaces()

    proc_id = _safe_id(section.title, "Proc")
    proc_name = section.title[:120]

    # Verzamel taak-zinnen
    sentences = []
    for p in section.paragraphs:
        sentences.extend(_split_sentences(p))
    # Taken: alleen zinnen met een verb-match, max 20
    task_items = []
    for s in sentences:
        v = _find_task_verb(s)
        if v:
            nm = _extract_task_name(s, v)
            task_items.append({"verb": v, "source": s, "name": nm})
        if len(task_items) >= 20:
            break

    # Als geen task-zinnen gevonden, maak 1 dummy task van de titel
    if not task_items:
        task_items = [{"verb": "", "source": section.title,
                       "name": _capitalize(section.title)[:80]}]

    # Entities in de sectie
    ex = extract_entities(ParsedDoc(
        source_file="(generated)", kind="docx", sections=[section]
    ))
    entity_names = sorted(ex.entities.keys())

    # Bouw XML
    defs = ET.Element(_qname(BPMN_NS, "definitions"), {
        "id": _safe_id("Defs", "D"),
        "targetNamespace": "http://bpmn.io/generated",
    })
    proc = ET.SubElement(defs, _qname(BPMN_NS, "process"), {
        "id": proc_id, "name": proc_name,
    })
    # Documentation met brontekst
    doc_txt = ET.SubElement(proc, _qname(BPMN_NS, "documentation"))
    doc_txt.text = "SOURCE_DOC_SECTION_TEXT:\n" + section.full_text()[:4000]

    # DataObjects
    data_obj_ids = []
    for ent in entity_names:
        do_id = _safe_id(ent, "DO")
        dor_id = _safe_id(ent, "DOR")
        ET.SubElement(proc, _qname(BPMN_NS, "dataObject"),
                      {"id": do_id, "name": ent})
        ET.SubElement(proc, _qname(BPMN_NS, "dataObjectReference"),
                      {"id": dor_id, "name": ent, "dataObjectRef": do_id})
        # Attribuut-annotatie als JSON in documentation
        attrs = sorted(ex.entities.get(ent, set()))
        if attrs:
            doc_el = ET.SubElement(proc, _qname(BPMN_NS, "documentation"))
            doc_el.text = (f"ENTITY_ATTRIBUTES[{ent}]:"
                           + json.dumps(attrs, ensure_ascii=False))
        data_obj_ids.append((ent, dor_id))

    # Start event
    start_id = _safe_id("start", "SE")
    ET.SubElement(proc, _qname(BPMN_NS, "startEvent"),
                  {"id": start_id, "name": "Start"})

    # Tasks + sequence flows
    prev_id = start_id
    tasks_meta = []
    for i, t in enumerate(task_items, start=1):
        tid = _safe_id(f"t{i}", "Task")
        task_el = ET.SubElement(proc, _qname(BPMN_NS, "userTask"),
                                {"id": tid, "name": t["name"][:100]})
        # Brontekst in documentation
        d = ET.SubElement(task_el, _qname(BPMN_NS, "documentation"))
        d.text = "SOURCE_SENTENCE:" + t["source"][:500]
        # Koppel alle dataobjects als dataInputAssociation
        for ent, dor_id in data_obj_ids:
            ia = ET.SubElement(task_el, _qname(BPMN_NS, "dataInputAssociation"),
                               {"id": _safe_id(ent, "IA")})
            ET.SubElement(ia, _qname(BPMN_NS, "sourceRef")).text = dor_id
        # Sequence flow van vorige naar deze task
        sf_id = _safe_id(f"sf{i}", "SF")
        ET.SubElement(proc, _qname(BPMN_NS, "sequenceFlow"),
                      {"id": sf_id, "sourceRef": prev_id, "targetRef": tid})
        prev_id = tid
        tasks_meta.append({"id": tid, "name": t["name"], "source": t["source"]})

    # End event + final sf
    end_id = _safe_id("end", "EE")
    ET.SubElement(proc, _qname(BPMN_NS, "endEvent"),
                  {"id": end_id, "name": "Einde"})
    ET.SubElement(proc, _qname(BPMN_NS, "sequenceFlow"),
                  {"id": _safe_id("sfEnd", "SF"),
                   "sourceRef": prev_id, "targetRef": end_id})

    # DI-layout: horizontaal, 160px per node
    plane_id = _safe_id("plane", "Pl")
    di_root = ET.SubElement(defs, _qname(DI_NS, "BPMNDiagram"),
                            {"id": _safe_id("diag", "Di")})
    plane = ET.SubElement(di_root, _qname(DI_NS, "BPMNPlane"),
                          {"id": plane_id, "bpmnElement": proc_id})

    def add_shape(ref, x, y, w, h, is_marker=False):
        sh = ET.SubElement(plane, _qname(DI_NS, "BPMNShape"),
                           {"id": _safe_id("s", "Shape"), "bpmnElement": ref})
        if is_marker:
            sh.set("isMarkerVisible", "true")
        b = ET.SubElement(sh, _qname(DC_NS, "Bounds"),
                          {"x": str(x), "y": str(y),
                           "width": str(w), "height": str(h)})
        return sh

    def add_edge(ref, x1, y1, x2, y2):
        attrs = {"id": _safe_id("e", "Edge")}
        if ref:
            attrs["bpmnElement"] = ref
        e = ET.SubElement(plane, _qname(DI_NS, "BPMNEdge"), attrs)
        ET.SubElement(e, _qname(DIAG_NS, "waypoint"),
                      {"x": str(x1), "y": str(y1)})
        ET.SubElement(e, _qname(DIAG_NS, "waypoint"),
                      {"x": str(x2), "y": str(y2)})

    x = 80
    y = 200
    add_shape(start_id, x, y, 36, 36)
    prev_center = (x + 18, y + 18)
    x += 90
    for t in tasks_meta:
        add_shape(t["id"], x, y - 12, 140, 60)
        task_center_left = (x, y + 18)
        add_edge(None, prev_center[0], prev_center[1],
                 task_center_left[0], task_center_left[1])
        prev_center = (x + 140, y + 18)
        x += 180
    add_shape(end_id, x, y, 36, 36)
    add_edge(None, prev_center[0], prev_center[1], x, y + 18)

    # DataObjects onderaan
    dx = 80
    for ent, dor_id in data_obj_ids:
        add_shape(dor_id, dx, y + 100, 36, 50)
        dx += 120

    # Serialize
    tree = ET.ElementTree(defs)
    buf = io.BytesIO()
    tree.write(buf, xml_declaration=True, encoding="UTF-8")
    return buf.getvalue(), tasks_meta


# ---------------------------------------------------------------------------
# Hoge-niveau orchestratie: document -> BPMN(s) + entity-extractie
# ---------------------------------------------------------------------------

@dataclass
class DocProcessingResult:
    doc_id: str
    source_file: str
    kind: str
    processed_at: str
    generated_bpmns: list[dict] = field(default_factory=list)  # [{filename, process_name, tasks}]
    extracted_entities: dict[str, list[str]] = field(default_factory=dict)
    section_texts: dict[str, str] = field(default_factory=dict)  # process_name -> full_text

    def to_dict(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "source_file": self.source_file,
            "kind": self.kind,
            "processed_at": self.processed_at,
            "generated_bpmns": self.generated_bpmns,
            "extracted_entities": self.extracted_entities,
            "section_texts": self.section_texts,
        }


def process_document(
    source_path: Path, doc_id: str
) -> tuple[ParsedDoc, ExtractedEntities, list[tuple[str, bytes, list[dict], str]], DocProcessingResult]:
    """Parse + extract + genereer BPMNs voor alle top-level secties.

    Returns:
        parsed: ParsedDoc
        entities: ExtractedEntities
        bpmns: list of (bpmn_filename, xml_bytes, tasks_meta, process_name)
        result: DocProcessingResult met audit-metadata
    """
    ext = source_path.suffix.lower()
    if ext == ".docx":
        parsed = extract_docx(source_path)
    elif ext == ".pptx":
        parsed = extract_pptx(source_path)
    else:
        raise ValueError(f"Onbekende extensie: {ext}")

    entities = extract_entities(parsed)

    bpmns: list[tuple[str, bytes, list[dict], str]] = []
    section_texts: dict[str, str] = {}

    # Per top-level sectie genereer een .bpmn
    def gen_for(sec: DocSection, depth: int):
        if depth >= 2:
            return
        # Skip lege secties
        has_content = bool(sec.paragraphs or sec.children)
        if not has_content:
            return
        xml, tasks_meta = section_to_bpmn(sec)
        slug = _slugify(sec.title)
        filename = f"{slug}_{doc_id[:6]}.bpmn"
        bpmns.append((filename, xml, tasks_meta, sec.title))
        section_texts[sec.title] = sec.full_text()
        for child in sec.children:
            gen_for(child, depth + 1)

    for sec in parsed.sections:
        gen_for(sec, depth=0)

    result = DocProcessingResult(
        doc_id=doc_id,
        source_file=source_path.name,
        kind=ext.lstrip("."),
        processed_at=datetime.now().isoformat(timespec="seconds"),
        generated_bpmns=[{
            "filename": fn, "process_name": pn, "task_count": len(tm)
        } for fn, _, tm, pn in bpmns],
        extracted_entities={k: sorted(v) for k, v in entities.entities.items()},
        section_texts=section_texts,
    )
    return parsed, entities, bpmns, result


# ---------------------------------------------------------------------------
# Audit-log opslag
# ---------------------------------------------------------------------------

def docs_dir(project_dir: Path) -> Path:
    d = project_dir / "documents"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_docs_log(project_dir: Path) -> list[dict]:
    log_p = docs_dir(project_dir) / "docs.json"
    if not log_p.exists():
        return []
    try:
        with log_p.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return []


def append_docs_log(project_dir: Path, entry: dict) -> None:
    log = load_docs_log(project_dir)
    log.append(entry)
    log_p = docs_dir(project_dir) / "docs.json"
    with log_p.open("w", encoding="utf-8") as f:
        json.dump(log, f, indent=2, ensure_ascii=False)


def doc_subdir(project_dir: Path, doc_id: str) -> Path:
    d = docs_dir(project_dir) / doc_id
    d.mkdir(parents=True, exist_ok=True)
    return d
