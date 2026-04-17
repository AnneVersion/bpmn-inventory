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


# Trefwoorden die een heading als ECHTE proces-beschrijving markeren
PROCESS_HEADING_HINTS = [
    "proces", "procedure", "workflow", "werkwijze", "stappen",
    "afhandeling", "behandeling", "procesbeschrijving",
    "procesbeschijving",          # veel voorkomende typo
    "stappenplan", "uitvoering",
]

# Subproces-markers: P1, P2, P3 / L2 P01 / Stap 1 / etc.
PROCESS_NUMBER_RE = re.compile(
    r"^\s*(?:L\d+\s+)?(?:sub)?(?:proces\s*)?p\d+\b",
    re.IGNORECASE,
)
STAP_RE = re.compile(r"^\s*stap\s*\d+\b", re.IGNORECASE)

# Headings die nooit een proces zijn (metadata)
METADATA_HEADING_HINTS = [
    "doel", "doelstelling", "trigger", "resultaat", "scope",
    "stakeholder", "actor", "rollen", "betrokken", "kpi",
    "stuurinformatie", "risico", "aandachtspunt", "achtergrond",
    "instructie", "type proces", "gebruikte data",
    "gebruikte kanalen", "gebruikte systemen", "gebruikte systeem",
    "inleiding", "samenvatting", "bijlage", "referentie",
    "versiehistorie", "document", "review", "audit",
    "afkortingen", "begrippen", "termen", "definities",
    "wijzigingshistorie", "revisions", "samenvattend",
    "resultaten",
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
    """Bouw Camunda-style .bpmn XML uit één sectie.

    Output-struktuur (compatible met bpmn.io / Camunda Modeler):
    - <bpmn:collaboration> met <bpmn:participant name="..." processRef=...>
      zodat het diagram een proces-pool toont met de proces-naam erop
    - <bpmn:process isExecutable="true"> met:
        * startEvent + serviceTask(s) + endEvent
        * Elke task heeft <bpmn:incoming>/<bpmn:outgoing> refs (Camunda-convention)
        * sequenceFlows met Flow_-ids
    - BPMN-DI plane verwijst naar de collaboration
    - Elke shape krijgt <bpmndi:BPMNLabel /> voor correcte label-positionering
    - Task-subtype serviceTask geeft het tandwiel-icon (matcht Camunda-stijl);
      fallback naar userTask als verb 'raadpleeg'/'controleer'/etc. (niet
      geautomatiseerd)
    """
    _register_namespaces()

    coll_id = _safe_id("Collaboration", "Collab")
    part_id = _safe_id("Participant", "P")
    proc_id = _safe_id("Process", "Proc")
    proc_name = section.title[:120]

    # Verzamel task-zinnen (max 20)
    sentences = []
    for p in section.paragraphs:
        sentences.extend(_split_sentences(p))
    task_items = []
    for s in sentences:
        v = _find_task_verb(s)
        if v:
            nm = _extract_task_name(s, v)
            task_items.append({"verb": v, "source": s, "name": nm})
        if len(task_items) >= 20:
            break
    if not task_items:
        task_items = [{"verb": "", "source": section.title,
                       "name": _capitalize(section.title)[:80]}]

    # Entities
    ex = extract_entities(ParsedDoc(
        source_file="(generated)", kind="docx", sections=[section]
    ))
    entity_names = sorted(ex.entities.keys())

    # --- XML root + collaboration (pool wrapper)
    defs = ET.Element(_qname(BPMN_NS, "definitions"), {
        "id": _safe_id("Definitions", "D"),
        "targetNamespace": "http://bpmn.io/schema/bpmn",
        "exporter": "BPMN Inventory doc-generator",
    })
    collab = ET.SubElement(defs, _qname(BPMN_NS, "collaboration"),
                           {"id": coll_id})
    ET.SubElement(collab, _qname(BPMN_NS, "participant"), {
        "id": part_id,
        "name": proc_name,
        "processRef": proc_id,
    })

    proc = ET.SubElement(defs, _qname(BPMN_NS, "process"), {
        "id": proc_id, "name": proc_name, "isExecutable": "true",
    })
    doc_txt = ET.SubElement(proc, _qname(BPMN_NS, "documentation"))
    doc_txt.text = "SOURCE_DOC_SECTION_TEXT:\n" + section.full_text()[:4000]

    # --- Bouw flow-graph: start -> task1 -> task2 -> ... -> end
    # We bepalen eerst alle ids en flows, zodat we incoming/outgoing
    # kunnen toevoegen per element.
    start_id = "StartEvent_1"
    end_id = _safe_id("EndEvent", "EndEvent")
    task_ids = [f"Activity_{uuid.uuid4().hex[:7]}" for _ in task_items]
    flow_ids = [f"Flow_{uuid.uuid4().hex[:7]}"
                for _ in range(len(task_ids) + 1)]
    # flow_ids[i] = flow van node i naar node i+1 in [start, task0, task1, ..., end]

    node_chain = [start_id] + task_ids + [end_id]

    # DataObjects
    data_obj_ids = []
    for ent in entity_names:
        do_id = f"DataObject_{uuid.uuid4().hex[:7]}"
        dor_id = f"DataObjectReference_{uuid.uuid4().hex[:7]}"
        ET.SubElement(proc, _qname(BPMN_NS, "dataObject"),
                      {"id": do_id, "name": ent})
        ET.SubElement(proc, _qname(BPMN_NS, "dataObjectReference"),
                      {"id": dor_id, "name": ent, "dataObjectRef": do_id})
        attrs = sorted(ex.entities.get(ent, set()))
        if attrs:
            doc_el = ET.SubElement(proc, _qname(BPMN_NS, "documentation"))
            doc_el.text = (f"ENTITY_ATTRIBUTES[{ent}]:"
                           + json.dumps(attrs, ensure_ascii=False))
        data_obj_ids.append((ent, dor_id))

    # --- StartEvent (met outgoing ref)
    se = ET.SubElement(proc, _qname(BPMN_NS, "startEvent"),
                       {"id": start_id})
    ET.SubElement(se, _qname(BPMN_NS, "outgoing")).text = flow_ids[0]

    # --- Tasks (serviceTask met incoming/outgoing refs)
    tasks_meta = []
    for i, t in enumerate(task_items):
        tid = task_ids[i]
        task_el = ET.SubElement(proc, _qname(BPMN_NS, "serviceTask"),
                                {"id": tid, "name": t["name"][:100]})
        d = ET.SubElement(task_el, _qname(BPMN_NS, "documentation"))
        d.text = "SOURCE_SENTENCE:" + t["source"][:500]
        ET.SubElement(task_el, _qname(BPMN_NS, "incoming")).text = flow_ids[i]
        ET.SubElement(task_el, _qname(BPMN_NS, "outgoing")).text = flow_ids[i + 1]
        # DataInput-associations naar alle entities
        for ent, dor_id in data_obj_ids:
            ia = ET.SubElement(task_el, _qname(BPMN_NS, "dataInputAssociation"),
                               {"id": f"DataInputAssociation_{uuid.uuid4().hex[:6]}"})
            ET.SubElement(ia, _qname(BPMN_NS, "sourceRef")).text = dor_id
        tasks_meta.append({"id": tid, "name": t["name"], "source": t["source"]})

    # --- EndEvent (met incoming ref)
    ee = ET.SubElement(proc, _qname(BPMN_NS, "endEvent"), {"id": end_id})
    ET.SubElement(ee, _qname(BPMN_NS, "incoming")).text = flow_ids[-1]

    # --- Sequence flows
    for i, fid in enumerate(flow_ids):
        ET.SubElement(proc, _qname(BPMN_NS, "sequenceFlow"), {
            "id": fid,
            "sourceRef": node_chain[i],
            "targetRef": node_chain[i + 1],
        })

    # --- DI-layout: pool rondom het hele proces, horizontaal
    di_root = ET.SubElement(defs, _qname(DI_NS, "BPMNDiagram"),
                            {"id": "BPMNDiagram_1"})
    plane = ET.SubElement(di_root, _qname(DI_NS, "BPMNPlane"),
                          {"id": "BPMNPlane_1", "bpmnElement": coll_id})

    def add_shape(ref, x, y, w, h, is_marker=False,
                  is_horizontal=False, with_label=True):
        attrs = {"id": f"{ref}_di", "bpmnElement": ref}
        if is_horizontal:
            attrs["isHorizontal"] = "true"
        if is_marker:
            attrs["isMarkerVisible"] = "true"
        sh = ET.SubElement(plane, _qname(DI_NS, "BPMNShape"), attrs)
        ET.SubElement(sh, _qname(DC_NS, "Bounds"), {
            "x": str(x), "y": str(y), "width": str(w), "height": str(h)
        })
        if with_label:
            ET.SubElement(sh, _qname(DI_NS, "BPMNLabel"))
        return sh

    def add_edge(ref, waypoints):
        e = ET.SubElement(plane, _qname(DI_NS, "BPMNEdge"),
                          {"id": f"{ref}_di", "bpmnElement": ref})
        for (wx, wy) in waypoints:
            ET.SubElement(e, _qname(DIAG_NS, "waypoint"),
                          {"x": str(wx), "y": str(wy)})

    # Bereken afmetingen voor de pool
    pool_margin_x = 160
    step = 160
    content_width = 36 + (len(task_items) * step) + 36 + 40  # start + tasks + end
    pool_width = max(pool_margin_x + content_width + 60, 600)
    pool_height = 252

    pool_x = 160
    pool_y = 80
    # Pool-shape
    add_shape(part_id, pool_x, pool_y, pool_width, pool_height,
              is_horizontal=True)

    # StartEvent
    y_mid = pool_y + pool_height // 2 - 18
    x = pool_x + 90
    add_shape(start_id, x, y_mid, 36, 36, with_label=True)
    start_right = (x + 36, y_mid + 18)
    x += 36 + 54  # gap tussen start en eerste task

    # Tasks: 100x80
    task_centers = []  # (left_center, right_center)
    for tid in task_ids:
        add_shape(tid, x, y_mid - 22, 100, 80)
        task_centers.append(((x, y_mid + 18), (x + 100, y_mid + 18)))
        x += 100 + 60  # gap tussen tasks

    # EndEvent
    x_end = x - 24  # compensate last gap
    add_shape(end_id, x_end, y_mid, 36, 36)
    end_left = (x_end, y_mid + 18)

    # Edges
    prev_right = start_right
    for i, (lc, rc) in enumerate(task_centers):
        add_edge(flow_ids[i], [prev_right, lc])
        prev_right = rc
    add_edge(flow_ids[-1], [prev_right, end_left])

    # DataObjects onder de pool
    if data_obj_ids:
        dy = pool_y + pool_height + 30
        dx = pool_x + 20
        for ent, dor_id in data_obj_ids:
            add_shape(dor_id, dx, dy, 36, 50)
            dx += 120

    buf = io.BytesIO()
    ET.ElementTree(defs).write(buf, xml_declaration=True, encoding="UTF-8")
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
    # Nieuw: alle top-level secties met hun classificatie (process | metadata | empty)
    section_classification: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "source_file": self.source_file,
            "kind": self.kind,
            "processed_at": self.processed_at,
            "generated_bpmns": self.generated_bpmns,
            "extracted_entities": self.extracted_entities,
            "section_texts": self.section_texts,
            "section_classification": self.section_classification,
        }


# ---------------------------------------------------------------------------
# Sectie-classificatie: is dit een proces of metadata?
# ---------------------------------------------------------------------------

def _count_task_verbs(paragraphs: list[str]) -> int:
    count = 0
    for p in paragraphs:
        for s in _split_sentences(p):
            if _find_task_verb(s):
                count += 1
    return count


def _all_paragraphs(section: "DocSection") -> list[str]:
    """Verzamel alle paragraphs van sectie + recursief subsecties."""
    out = list(section.paragraphs)
    for c in section.children:
        out.extend(_all_paragraphs(c))
    return out


def classify_section(section: "DocSection") -> tuple[str, str]:
    """Classificeer een sectie als 'process' | 'metadata' | 'empty'.

    Retourneert (classificatie, reden).

    Regels (in volgorde):
    1. Titel matcht 'P1', 'P2', 'Stap 1' -> process
    2. Titel bevat een PROCESS_HEADING_HINT woord -> process
    3. Titel bevat een METADATA_HEADING_HINT woord -> metadata
    4. Telt 2+ task-werkwoorden in paragrafen -> process
    5. Anders:
       - Heeft geen content -> empty
       - Anders -> metadata (veilige default, voorkomt onzin-BPMNs)
    """
    title_low = section.title.lower().strip()
    if not title_low:
        return ("empty", "Geen titel.")

    if PROCESS_NUMBER_RE.match(section.title) or STAP_RE.match(section.title):
        return ("process", f"Titel matcht proces-nummer-patroon ({section.title!r}).")

    # Metadata-hints krijgen VOORRANG boven proces-hints, want zinnen als
    # 'Trigger van het proces' matchen beide: 'trigger' is specifieker dan
    # 'proces'. Dit voorkomt false-positives op rubrieken met proces in de naam.
    for hint in METADATA_HEADING_HINTS:
        if re.search(r"\b" + re.escape(hint) + r"\w*\b", title_low):
            return ("metadata",
                    f"Titel bevat metadata-trefwoord {hint!r} — waarschijnlijk "
                    "geen procesbeschrijving.")

    for hint in PROCESS_HEADING_HINTS:
        if re.search(r"\b" + re.escape(hint) + r"\w*\b", title_low):
            return ("process", f"Titel bevat proces-trefwoord {hint!r}.")

    all_pars = _all_paragraphs(section)
    verb_count = _count_task_verbs(all_pars)
    if verb_count >= 2:
        return ("process",
                f"{verb_count} task-werkwoorden gevonden in paragrafen "
                "(Registreer, Wijzig, Controleer, Valideer, ...).")

    if not all_pars:
        return ("empty", "Geen paragrafen onder deze sectie.")

    return ("metadata",
            "Geen proces-werkwoorden en geen proces-trefwoord in titel; "
            "lijkt op metadata (achtergrond, beschrijving, toelichting).")


def _walk_all_sections(sections: list[DocSection]) -> list[tuple[int, DocSection]]:
    """Lineariseer de sectie-boom naar (depth, section) tuples."""
    out: list[tuple[int, DocSection]] = []
    def walk(sec: DocSection, depth: int):
        out.append((depth, sec))
        for c in sec.children:
            walk(c, depth + 1)
    for s in sections:
        walk(s, 0)
    return out


def process_document(
    source_path: Path, doc_id: str
) -> tuple[ParsedDoc, ExtractedEntities, list[tuple[str, bytes, list[dict], str]], DocProcessingResult]:
    """Parse + extract + genereer BPMNs UIT SECTIES die als 'process' worden
    geclassificeerd (niet uit metadata-secties).

    Ook subsecties (L2, L3) worden meegenomen als ze een proces zijn —
    zodat een document met "P1 / P2 / P3" onder een hoofdproces
    correct 3 subprocessen oplevert.

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

    # Classificeer ELKE sectie (ook subsecties) zodat subprocessen
    # zichtbaar zijn in de audit-log.
    classified: list[dict] = []
    all_sections = _walk_all_sections(parsed.sections)
    for depth, sec in all_sections:
        cls, reason = classify_section(sec)
        classified.append({
            "title": sec.title,
            "level": sec.level,
            "depth": depth,
            "classification": cls,
            "reason": reason,
            "paragraph_count": len(sec.paragraphs),
            "child_count": len(sec.children),
        })

    # Bepaal welke secties paraplu's zijn voor subprocessen: als een
    # sectie depth=0 een 'process' is EN er zit minstens 1 'process'
    # subsectie onder, dan skippen we de paraplu (om dubbele BPMNs
    # te voorkomen). Subsecties blijven wel eigen BPMN.
    umbrella_indices: set[int] = set()
    for i, (depth, sec) in enumerate(all_sections):
        if classified[i]["classification"] != "process":
            continue
        # Kijk naar alle volgende secties die dieper genest zijn
        has_process_subsection = False
        j = i + 1
        while j < len(all_sections) and all_sections[j][0] > depth:
            if classified[j]["classification"] == "process":
                has_process_subsection = True
                break
            j += 1
        if has_process_subsection:
            umbrella_indices.add(i)
            classified[i]["reason"] += \
                " (paraplu overgeslagen: subsecties zijn al proces)"
            classified[i]["classification"] = "umbrella"

    # Bepaal welke secties we als BPMN genereren:
    # - classificatie == 'process'
    # - max depth 2 (anders worden BPMNs te gedetailleerd en nested)
    already_processed: set[int] = set()
    for i, (depth, sec) in enumerate(all_sections):
        info = classified[i]
        if info["classification"] != "process":
            continue
        if depth > 2:
            info["reason"] += " (overgeslagen: te diep genest)"
            info["classification"] = "skipped_nested"
            continue

        # Vind de parent-proces titel (zoekt achteruit naar depth < mijn depth
        # die ook een 'process' of 'umbrella' is)
        parent_title = ""
        for j in range(i - 1, -1, -1):
            pd, ps = all_sections[j]
            if pd < depth:
                parent_title = ps.title
                break
        info["parent_process"] = parent_title

        xml, tasks_meta = section_to_bpmn(sec)
        slug = _slugify(sec.title)
        filename = f"{slug}_{doc_id[:6]}.bpmn"
        # voorkom dubbele filenames (bv. meerdere "P1" secties)
        suffix = 1
        orig_filename = filename
        while any(b[0] == filename for b in bpmns):
            suffix += 1
            filename = orig_filename.replace(f"_{doc_id[:6]}",
                                             f"_{suffix}_{doc_id[:6]}")
        bpmns.append((filename, xml, tasks_meta, sec.title))
        section_texts[sec.title] = sec.full_text()
        already_processed.add(i)
        # Sla filename -> parent mapping apart op in info zodat webapp het
        # kan opslaan in project.json.bpmn_parents
        info["generated_filename"] = filename

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
        section_classification=classified,
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
