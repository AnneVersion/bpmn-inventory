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


# Veld-namen die we in SOLL-documentatie herkennen (case-insensitive)
FIELD_ALIASES: dict[str, list[str]] = {
    "trigger":     ["trigger", "trigger van het proces", "start-trigger", "aanleiding"],
    "doel":        ["doel", "doelstelling", "scope"],
    "resultaat":   ["resultaat", "eindresultaat", "output"],
    "actoren":     ["actoren", "actorenrollen", "actoren/rollen", "rollen",
                    "betrokken organisaties", "betrokken partijen",
                    "stakeholders"],
    "stappen":     ["stappen", "processtappen", "werkwijze", "procesbeschrijving",
                    "procesbeschijving", "procesverloop"],
    "data":        ["gebruikte data", "gegevens", "dataobjecten", "entiteiten",
                    "informatie"],
    "systemen":    ["gebruikte systemen", "systemen", "gebruikte kanalen",
                    "ict-middelen", "applicaties", "tools"],
    "beslispunten":["beslispunten", "beslissingen", "keuzes", "gateways"],
    "regels":      ["business rules", "regels", "voorwaarden", "randvoorwaarden"],
    "risicos":     ["risicos", "risico's", "risk"],
    "kpis":        ["kpis", "kpi's", "kpi", "stuurinformatie", "metrics"],
}


# Trefwoorden die een heading als ECHTE proces-beschrijving markeren
PROCESS_HEADING_HINTS = [
    "proces", "procedure", "workflow", "werkwijze", "stappen",
    "afhandeling", "behandeling", "procesbeschrijving",
    "procesbeschijving",          # veel voorkomende typo
    "stappenplan", "uitvoering",
]

# Subproces-markers: 'P1', 'P02', 'L2 P01', '3 Sub proces P01',
# '3 Subproces P01 - Inschrijven lid', etc.
PROCESS_NUMBER_RE = re.compile(
    r"""
    ^\s*
    (?:\d+(?:\.\d+)?\s+)?          # Optionele hoofdnummer (3 / 3.1)
    (?:L\d+\s+)?                   # Optioneel niveau (L2)
    (?:sub[\s-]*)?                 # Optioneel 'sub' prefix
    (?:proces[s]?\s*)?             # Optioneel 'proces' of 'process'
    [Pp]\d+\b                      # Vereiste P-nummer
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Ook: 'Subproces' of 'Hoofdproces' zonder nummer kan een proces aanduiden
SUBPROC_WORD_RE = re.compile(
    r"^\s*(?:hoofd|sub)\s*proces[s]?\b",
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
class ProcessFields:
    """Per-sectie gestructureerde velden, afgeleid uit SOLL-documentatie."""
    trigger: str = ""
    doel: str = ""
    resultaat: str = ""
    actoren: list[str] = field(default_factory=list)
    stappen: list[str] = field(default_factory=list)         # imperatieve zinnen
    data_items: list[str] = field(default_factory=list)      # entity-namen
    systemen: list[str] = field(default_factory=list)        # systeemnamen
    beslispunten: list[str] = field(default_factory=list)    # "condition : outcome1 / outcome2"
    regels: list[str] = field(default_factory=list)
    raw_fields: dict = field(default_factory=dict)


def _match_field_alias(text: str) -> str | None:
    """Match 'Trigger van het proces' -> 'trigger', 'Actoren/Rollen' ->
    'actoren'. Return canonical field-key of None."""
    low = text.strip().lower().rstrip(":").strip()
    for canonical, aliases in FIELD_ALIASES.items():
        for alias in aliases:
            if low == alias or low.startswith(alias + " "):
                return canonical
    return None


def _extract_bullets_from_table(paragraphs: list[str],
                                start_idx: int) -> tuple[list[str], int]:
    """Verzamel aaneensluitende bullet-items/regels tot een volgend veld begint.

    Retourneert (lines, end_idx)."""
    lines: list[str] = []
    i = start_idx
    while i < len(paragraphs):
        p = paragraphs[i].strip()
        if not p:
            i += 1
            continue
        # Als dit alleen een veldnaam is, stop
        if _match_field_alias(p):
            break
        # Als dit 'Veld: waarde' begint, stop ook (anders eten we het volgende
        # veld op dat op dezelfde regel lijkt)
        m = re.match(r"^([A-Za-z][\w\s/'-]{1,40}?)\s*[:.]\s+.+$", p)
        if m and _match_field_alias(m.group(1)):
            break
        # Strip gemeenschappelijke bullet-prefixes
        clean = re.sub(r"^[-*\u2022\u25cb\u25cf\u25aa\u25ab]\s*", "", p)
        clean = re.sub(r"^\d+[.)]\s+", "", clean)   # 1. of 1)
        if clean:
            lines.append(clean)
        i += 1
    return lines, i


def extract_process_fields(section: "DocSection") -> ProcessFields:
    """Herken SOLL-velden in een sectie en groepeer paragrafen per veld.

    Werkwijze:
    - Behandel titel van subsectie als kandidaat veld-naam (bv H2 'Trigger')
    - Behandel eerste zin van een paragraaf met ':' als kandidaat
      ('Trigger: een nieuw persoon wil lid worden')
    - Verzamel subsequente bullets/regels tot aan de volgende veldmarkering
    """
    fields = ProcessFields()
    raw: dict[str, list[str]] = {}

    # 1) Subsecties met veld-titel
    for child in section.children:
        canonical = _match_field_alias(child.title)
        if canonical:
            raw.setdefault(canonical, []).extend(
                p.strip() for p in child.paragraphs if p.strip()
            )

    # 2) Inline in eigen paragrafen: 'Veld: waarde'
    paragraphs = list(section.paragraphs)
    i = 0
    while i < len(paragraphs):
        p = paragraphs[i].strip()
        if not p:
            i += 1
            continue
        m = re.match(r"^(?P<field>[A-Za-z][\w\s/'-]{1,40}?)\s*[:.]\s+(?P<rest>.+)$", p)
        if m:
            canonical = _match_field_alias(m.group("field"))
            if canonical:
                rest = m.group("rest").strip()
                if rest:
                    raw.setdefault(canonical, []).append(rest)
                # Verzamel volgende bullets tot volgend veld
                extra, i = _extract_bullets_from_table(paragraphs, i + 1)
                raw.setdefault(canonical, []).extend(extra)
                continue
        # Of alleen veldnaam op eigen regel, daarna bullets
        canonical = _match_field_alias(p)
        if canonical:
            extra, i = _extract_bullets_from_table(paragraphs, i + 1)
            raw.setdefault(canonical, []).extend(extra)
            continue
        i += 1

    fields.raw_fields = raw

    # Normaliseer velden naar de dataclass
    fields.trigger = " ".join(raw.get("trigger", []))[:200]
    fields.doel = " ".join(raw.get("doel", []))[:400]
    fields.resultaat = " ".join(raw.get("resultaat", []))[:200]
    fields.actoren = [a for a in raw.get("actoren", []) if a]
    fields.stappen = [s for s in raw.get("stappen", []) if s]
    fields.data_items = [d for d in raw.get("data", []) if d]
    fields.systemen = [s for s in raw.get("systemen", []) if s]
    fields.beslispunten = [b for b in raw.get("beslispunten", []) if b]
    fields.regels = [r for r in raw.get("regels", []) if r]
    return fields


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
    """Bouw Camunda-style .bpmn XML uit één sectie met SOLL-veld-extractie.

    Gebruikt extract_process_fields() om:
    - Trigger -> naam van startEvent
    - Resultaat -> naam van endEvent
    - Actoren/Rollen -> lanes in de pool (elke taak wordt toegewezen)
    - Stappen -> tasks in volgorde (bulletlijst als source)
    - Data-items + entities in tekst -> dataObjects
    - Systemen -> dataStoreReferences met association
    - Beslispunten -> exclusiveGateways met named outgoing flows
    """
    _register_namespaces()
    fields = extract_process_fields(section)

    coll_id = _safe_id("Collaboration", "Collab")
    part_id = _safe_id("Participant", "P")
    proc_id = _safe_id("Process", "Proc")
    proc_name = section.title[:120]

    # Bepaal stappen: voorkeur voor expliciete 'Stappen'-bullets, anders
    # detecteer zinnen met werkwoord zoals voorheen.
    stap_sources = fields.stappen if fields.stappen else []
    if not stap_sources:
        sentences = []
        for p in section.paragraphs:
            sentences.extend(_split_sentences(p))
        for s in sentences:
            if _find_task_verb(s):
                stap_sources.append(s)
            if len(stap_sources) >= 20:
                break
    if not stap_sources:
        stap_sources = [section.title]

    task_items = []
    for src in stap_sources[:25]:
        verb = _find_task_verb(src) or ""
        if verb:
            name = _extract_task_name(src, verb)
        else:
            name = _capitalize(src)[:100]
        task_items.append({"verb": verb, "source": src, "name": name})

    # Entities: combineer auto-discovery + expliciete data-items uit veld
    ex = extract_entities(ParsedDoc(
        source_file="(generated)", kind="docx", sections=[section]
    ))
    entity_names = set(ex.entities.keys())
    for item in fields.data_items:
        # item kan zijn "Lidmaatschap" of "Lid (met naam, bsn, adres)"
        m = re.match(r"^([A-Z][\w\s-]*?)(?:\s*\(|\s*$)", item)
        if m:
            name = m.group(1).strip().capitalize()
            if len(name) >= 3:
                entity_names.add(name)
                # Probeer attributen uit haakjes te halen
                attr_match = re.search(r"\(([^)]+)\)", item)
                if attr_match:
                    attrs = [a.strip().lower() for a in
                             re.split(r"[,;]", attr_match.group(1))
                             if a.strip()]
                    existing = ex.entities.setdefault(name, set())
                    for a in attrs:
                        if len(a) >= 2 and len(a) <= 30:
                            existing.add(a)
    entity_names = sorted(entity_names)

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

    # --- Lanes uit actoren
    lane_ids: list[tuple[str, str]] = []   # [(lane_id, name)]
    if fields.actoren:
        laneset = ET.SubElement(proc, _qname(BPMN_NS, "laneSet"),
                                {"id": _safe_id("LaneSet", "LS")})
        for actor in fields.actoren[:6]:  # cap bij 6 lanes
            lane_id = f"Lane_{uuid.uuid4().hex[:7]}"
            lane_el = ET.SubElement(laneset, _qname(BPMN_NS, "lane"),
                                    {"id": lane_id, "name": actor[:80]})
            lane_ids.append((lane_id, actor, lane_el))

    # --- Bouw flow-graph
    # node_chain: start -> task1 -> task2 -> ... [optionele gateway] -> end
    start_id = "StartEvent_1"
    end_id = _safe_id("EndEvent", "EndEvent")
    task_ids = [f"Activity_{uuid.uuid4().hex[:7]}" for _ in task_items]

    # Gateways uit beslispunten: plak er 1 gateway tussen laatste task en end
    gateway_id = None
    gateway_name = ""
    gateway_outcomes: list[str] = []  # extra naamlabels voor flows
    if fields.beslispunten:
        first_bp = fields.beslispunten[0]
        # Patroon: "Conditie : ja / nee" of "Conditie -> ja / nee"
        m = re.match(r"^(?P<cond>[^:?]+)\??\s*[:\-\u2192]\s*(?P<opts>.+)$", first_bp)
        if m:
            gateway_name = m.group("cond").strip()[:80]
            gateway_outcomes = [o.strip()[:40] for o in
                                re.split(r"\s*[/,]\s*", m.group("opts"))
                                if o.strip()][:3]
        else:
            gateway_name = first_bp[:80]
            gateway_outcomes = ["ja", "nee"]
        gateway_id = f"Gateway_{uuid.uuid4().hex[:7]}"

    # node_chain en flow_ids bepalen afhankelijk van gateway
    if gateway_id:
        node_chain = [start_id] + task_ids + [gateway_id, end_id]
    else:
        node_chain = [start_id] + task_ids + [end_id]
    flow_ids = [f"Flow_{uuid.uuid4().hex[:7]}"
                for _ in range(len(node_chain) - 1)]

    # DataObjects
    data_obj_ids: list[tuple[str, str]] = []
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

    # DataStores uit systemen
    data_store_ids: list[tuple[str, str]] = []
    for sysname in fields.systemen[:5]:
        clean = re.split(r"[(,:]", sysname, 1)[0].strip()[:40]
        if not clean:
            continue
        ds_id = f"DataStore_{uuid.uuid4().hex[:7]}"
        dsr_id = f"DataStoreReference_{uuid.uuid4().hex[:7]}"
        ET.SubElement(defs, _qname(BPMN_NS, "dataStore"),
                      {"id": ds_id, "name": clean})
        ET.SubElement(proc, _qname(BPMN_NS, "dataStoreReference"),
                      {"id": dsr_id, "name": clean, "dataStoreRef": ds_id})
        data_store_ids.append((clean, dsr_id))

    # --- StartEvent (trigger-naam als beschikbaar)
    se_name = fields.trigger or "Start"
    se = ET.SubElement(proc, _qname(BPMN_NS, "startEvent"),
                       {"id": start_id, "name": se_name[:80]})
    ET.SubElement(se, _qname(BPMN_NS, "outgoing")).text = flow_ids[0]

    # --- Tasks
    tasks_meta = []
    lane_assign: dict[str, str] = {}
    for i, t in enumerate(task_items):
        tid = task_ids[i]
        task_el = ET.SubElement(proc, _qname(BPMN_NS, "serviceTask"),
                                {"id": tid, "name": t["name"][:100]})
        d = ET.SubElement(task_el, _qname(BPMN_NS, "documentation"))
        d.text = "SOURCE_SENTENCE:" + t["source"][:500]
        ET.SubElement(task_el, _qname(BPMN_NS, "incoming")).text = flow_ids[i]
        ET.SubElement(task_el, _qname(BPMN_NS, "outgoing")).text = flow_ids[i + 1]

        # Koppel dataObjects (als er zijn)
        for ent, dor_id in data_obj_ids:
            if ent.lower() in t["source"].lower() or ent.lower() in t["name"].lower():
                ia = ET.SubElement(task_el, _qname(BPMN_NS, "dataInputAssociation"),
                                   {"id": f"DataInputAssociation_{uuid.uuid4().hex[:6]}"})
                ET.SubElement(ia, _qname(BPMN_NS, "sourceRef")).text = dor_id

        # Koppel dataStores als systeem in taaknaam/bron staat
        for sysname, dsr_id in data_store_ids:
            if sysname.lower() in t["source"].lower() or sysname.lower() in t["name"].lower():
                ia = ET.SubElement(task_el, _qname(BPMN_NS, "dataInputAssociation"),
                                   {"id": f"DataInputAssociation_{uuid.uuid4().hex[:6]}"})
                ET.SubElement(ia, _qname(BPMN_NS, "sourceRef")).text = dsr_id

        # Wijs toe aan een lane (round-robin over actoren)
        if lane_ids:
            lane_id, actor_name, lane_el = lane_ids[i % len(lane_ids)]
            ET.SubElement(lane_el, _qname(BPMN_NS, "flowNodeRef")).text = tid
            lane_assign[tid] = lane_id

        tasks_meta.append({"id": tid, "name": t["name"], "source": t["source"]})

    # --- Gateway (optioneel)
    if gateway_id:
        gw = ET.SubElement(proc, _qname(BPMN_NS, "exclusiveGateway"),
                           {"id": gateway_id, "name": gateway_name})
        # Incoming van laatste task
        ET.SubElement(gw, _qname(BPMN_NS, "incoming")).text = flow_ids[-2]
        ET.SubElement(gw, _qname(BPMN_NS, "outgoing")).text = flow_ids[-1]
        if lane_ids:
            first_lane_id, _, first_lane_el = lane_ids[0]
            ET.SubElement(first_lane_el, _qname(BPMN_NS, "flowNodeRef")).text = gateway_id

    # --- EndEvent
    end_name = fields.resultaat or "Einde"
    ee = ET.SubElement(proc, _qname(BPMN_NS, "endEvent"),
                       {"id": end_id, "name": end_name[:80]})
    ET.SubElement(ee, _qname(BPMN_NS, "incoming")).text = flow_ids[-1]

    # Voeg start/end ook aan eerste lane toe
    if lane_ids:
        first_lane_id, _, first_lane_el = lane_ids[0]
        ET.SubElement(first_lane_el, _qname(BPMN_NS, "flowNodeRef")).text = start_id
        ET.SubElement(first_lane_el, _qname(BPMN_NS, "flowNodeRef")).text = end_id

    # --- Sequence flows (gateway-uitkomst als label indien mogelijk)
    for i, fid in enumerate(flow_ids):
        src = node_chain[i]
        tgt = node_chain[i + 1]
        attrs = {"id": fid, "sourceRef": src, "targetRef": tgt}
        # Flow uit gateway kan een label (uitkomst) krijgen
        if gateway_id and src == gateway_id and gateway_outcomes:
            attrs["name"] = gateway_outcomes[0]
        ET.SubElement(proc, _qname(BPMN_NS, "sequenceFlow"), attrs)

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

    # Bereken afmetingen
    task_count = len(task_items)
    gw_extra = 80 if gateway_id else 0
    content_width = 36 + (task_count * 160) + gw_extra + 36 + 80
    pool_width = max(300 + content_width, 700)

    num_lanes = max(1, len(lane_ids))
    lane_height = 100
    pool_height = num_lanes * lane_height + 20
    pool_x = 160
    pool_y = 80
    # Pool-shape
    add_shape(part_id, pool_x, pool_y, pool_width, pool_height,
              is_horizontal=True)

    # Lane-shapes (kolommen naast elkaar = rijen in horizontale pool)
    lane_y_by_id: dict[str, int] = {}
    if lane_ids:
        for idx, (lid, _name, _el) in enumerate(lane_ids):
            ly = pool_y + 10 + idx * lane_height
            add_shape(lid, pool_x + 30, ly, pool_width - 30, lane_height,
                      is_horizontal=True)
            lane_y_by_id[lid] = ly + lane_height // 2 - 18

    x0 = pool_x + 90
    # StartEvent: in de eerste lane (of midden van pool zonder lanes)
    if lane_ids:
        first_lane_id = lane_ids[0][0]
        y_start = lane_y_by_id[first_lane_id]
    else:
        y_start = pool_y + pool_height // 2 - 18
    add_shape(start_id, x0, y_start, 36, 36)
    x = x0 + 36 + 54

    # Tasks (in lane-y bepaald door hun assignment)
    task_positions = []  # per task: (x_left, y_top_of_shape)
    for i, tid in enumerate(task_ids):
        if lane_ids and tid in lane_assign:
            y_t = lane_y_by_id[lane_assign[tid]] - 22
        else:
            y_t = pool_y + pool_height // 2 - 40
        add_shape(tid, x, y_t, 100, 80)
        task_positions.append((x, y_t))
        x += 100 + 60

    # Gateway (in eerste lane of middle)
    gw_pos = None
    if gateway_id:
        y_gw = (lane_y_by_id[lane_ids[0][0]] - 5) if lane_ids \
               else (pool_y + pool_height // 2 - 25)
        add_shape(gateway_id, x, y_gw, 50, 50, is_marker=True)
        gw_pos = (x, y_gw)
        x += 50 + 60

    # EndEvent
    y_end = lane_y_by_id[lane_ids[0][0]] if lane_ids \
            else pool_y + pool_height // 2 - 18
    x_end = x
    add_shape(end_id, x_end, y_end, 36, 36)

    # Edges
    def center_right(xp, yp, w, h): return (xp + w, yp + h // 2)
    def center_left(xp, yp, w, h):  return (xp, yp + h // 2)

    # Start (36x36) -> task0 (of gateway/end)
    start_right = (x0 + 36, y_start + 18)
    prev_right = start_right
    for i, (tx, ty) in enumerate(task_positions):
        add_edge(flow_ids[i], [prev_right, (tx, ty + 40)])
        prev_right = (tx + 100, ty + 40)

    if gateway_id:
        gx, gy = gw_pos
        add_edge(flow_ids[-2], [prev_right, (gx, gy + 25)])
        add_edge(flow_ids[-1], [(gx + 50, gy + 25), (x_end, y_end + 18)])
    else:
        add_edge(flow_ids[-1], [prev_right, (x_end, y_end + 18)])

    # DataObjects onder de pool
    if data_obj_ids:
        dy = pool_y + pool_height + 40
        dx = pool_x + 20
        for ent, dor_id in data_obj_ids:
            add_shape(dor_id, dx, dy, 36, 50)
            dx += 120

    # DataStores onder dataObjects
    if data_store_ids:
        dy = pool_y + pool_height + (120 if data_obj_ids else 40)
        dx = pool_x + 20
        for sysname, dsr_id in data_store_ids:
            add_shape(dsr_id, dx, dy, 50, 50)
            dx += 140

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

    if SUBPROC_WORD_RE.match(section.title):
        return ("process",
                f"Titel begint met 'Subproces' / 'Hoofdproces' ({section.title!r}).")

    # SOLL-veldnamen zijn GEEN eigen proces — ze zijn velden binnen een
    # parent-proces (Trigger/Doel/Actoren/Stappen/Gebruikte data/...).
    field_match = _match_field_alias(section.title)
    if field_match:
        return ("field",
                f"Titel is een SOLL-veldnaam ({field_match!r}) — hoort bij "
                "parent-proces, geen eigen BPMN.")

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


def umbrella_to_l1_bpmn(section: DocSection,
                        subprocess_names: list[tuple[str, str]]) -> bytes:
    """Bouw een L1-overzichts-BPMN uit een umbrella-sectie.

    `subprocess_names` = [(subproc_title, subproc_bpmn_filename), ...] in de
    volgorde zoals ze in het document staan.

    Output: 1 BPMN met <bpmn:callActivity>'s (één per subproc), sequentieel
    verbonden met start → call1 → call2 → ... → end. De callActivity krijgt
    calledElement verwijzend naar de subprocess-id conventie (de bpmn-js
    viewer toont hem als een task met "plus"-icon).
    """
    _register_namespaces()
    coll_id = _safe_id("Collaboration", "Collab")
    part_id = _safe_id("Participant", "P")
    proc_id = _safe_id("L1", "Proc")
    proc_name = section.title[:120]

    defs = ET.Element(_qname(BPMN_NS, "definitions"), {
        "id": _safe_id("Definitions", "D"),
        "targetNamespace": "http://bpmn.io/schema/bpmn",
        "exporter": "BPMN Inventory doc-generator (L1-overview)",
    })
    collab = ET.SubElement(defs, _qname(BPMN_NS, "collaboration"),
                           {"id": coll_id})
    ET.SubElement(collab, _qname(BPMN_NS, "participant"), {
        "id": part_id, "name": proc_name + " [L1 overzicht]",
        "processRef": proc_id,
    })
    proc = ET.SubElement(defs, _qname(BPMN_NS, "process"), {
        "id": proc_id, "name": proc_name, "isExecutable": "false",
    })
    doc_el = ET.SubElement(proc, _qname(BPMN_NS, "documentation"))
    doc_el.text = ("L1_OVERVIEW:" + section.title +
                   "\nSubprocessen:\n" +
                   "\n".join(f"- {t} ({f})" for t, f in subprocess_names))

    start_id = "StartEvent_1"
    end_id = _safe_id("EndEvent", "EndEvent")
    call_ids = [f"CallActivity_{uuid.uuid4().hex[:7]}"
                for _ in subprocess_names]
    flow_ids = [f"Flow_{uuid.uuid4().hex[:7]}"
                for _ in range(len(call_ids) + 1)]
    node_chain = [start_id] + call_ids + [end_id]

    se = ET.SubElement(proc, _qname(BPMN_NS, "startEvent"),
                       {"id": start_id, "name": "Start L1"})
    ET.SubElement(se, _qname(BPMN_NS, "outgoing")).text = flow_ids[0]

    for i, (title, filename) in enumerate(subprocess_names):
        cid = call_ids[i]
        # calledElement-id-conventie: 1 bpmn per proces; we gebruiken de
        # subproc-naam als calledElement-stub (viewer toont 'im still a task')
        calledRef = _safe_id(title, "CalledProc")
        ca = ET.SubElement(proc, _qname(BPMN_NS, "callActivity"), {
            "id": cid, "name": title[:100], "calledElement": calledRef,
        })
        d = ET.SubElement(ca, _qname(BPMN_NS, "documentation"))
        d.text = f"SUBPROCESS_REF_FILE:{filename}"
        ET.SubElement(ca, _qname(BPMN_NS, "incoming")).text = flow_ids[i]
        ET.SubElement(ca, _qname(BPMN_NS, "outgoing")).text = flow_ids[i + 1]

    ee = ET.SubElement(proc, _qname(BPMN_NS, "endEvent"),
                       {"id": end_id, "name": "Einde L1"})
    ET.SubElement(ee, _qname(BPMN_NS, "incoming")).text = flow_ids[-1]

    for i, fid in enumerate(flow_ids):
        ET.SubElement(proc, _qname(BPMN_NS, "sequenceFlow"),
                      {"id": fid,
                       "sourceRef": node_chain[i],
                       "targetRef": node_chain[i + 1]})

    # DI-layout
    di_root = ET.SubElement(defs, _qname(DI_NS, "BPMNDiagram"),
                            {"id": "BPMNDiagram_1"})
    plane = ET.SubElement(di_root, _qname(DI_NS, "BPMNPlane"),
                          {"id": "BPMNPlane_1", "bpmnElement": coll_id})
    pool_w = 300 + len(call_ids) * 180 + 100
    pool_h = 180
    pool_x, pool_y = 160, 80
    # Pool
    sh = ET.SubElement(plane, _qname(DI_NS, "BPMNShape"),
                       {"id": f"{part_id}_di", "bpmnElement": part_id,
                        "isHorizontal": "true"})
    ET.SubElement(sh, _qname(DC_NS, "Bounds"),
                  {"x": str(pool_x), "y": str(pool_y),
                   "width": str(pool_w), "height": str(pool_h)})
    ET.SubElement(sh, _qname(DI_NS, "BPMNLabel"))

    y_mid = pool_y + pool_h // 2 - 18
    x = pool_x + 90
    # Start
    sh = ET.SubElement(plane, _qname(DI_NS, "BPMNShape"),
                       {"id": f"{start_id}_di", "bpmnElement": start_id})
    ET.SubElement(sh, _qname(DC_NS, "Bounds"),
                  {"x": str(x), "y": str(y_mid), "width": "36", "height": "36"})
    ET.SubElement(sh, _qname(DI_NS, "BPMNLabel"))
    prev_right = (x + 36, y_mid + 18)
    x += 90
    # CallActivities
    for cid in call_ids:
        sh = ET.SubElement(plane, _qname(DI_NS, "BPMNShape"),
                           {"id": f"{cid}_di", "bpmnElement": cid})
        ET.SubElement(sh, _qname(DC_NS, "Bounds"),
                      {"x": str(x), "y": str(y_mid - 22),
                       "width": "120", "height": "80"})
        ET.SubElement(sh, _qname(DI_NS, "BPMNLabel"))
        x += 180
    # End
    sh = ET.SubElement(plane, _qname(DI_NS, "BPMNShape"),
                       {"id": f"{end_id}_di", "bpmnElement": end_id})
    ET.SubElement(sh, _qname(DC_NS, "Bounds"),
                  {"x": str(x), "y": str(y_mid), "width": "36", "height": "36"})
    ET.SubElement(sh, _qname(DI_NS, "BPMNLabel"))

    # Edges
    x_iter = pool_x + 90 + 36
    prev = (x_iter, y_mid + 18)
    for i, cid in enumerate(call_ids):
        cx = pool_x + 90 + 36 + 54 + i * 180
        # edge to this call
        e = ET.SubElement(plane, _qname(DI_NS, "BPMNEdge"),
                          {"id": f"{flow_ids[i]}_di",
                           "bpmnElement": flow_ids[i]})
        ET.SubElement(e, _qname(DIAG_NS, "waypoint"),
                      {"x": str(prev[0]), "y": str(prev[1])})
        ET.SubElement(e, _qname(DIAG_NS, "waypoint"),
                      {"x": str(cx), "y": str(y_mid + 18)})
        prev = (cx + 120, y_mid + 18)
    # Edge to end
    e = ET.SubElement(plane, _qname(DI_NS, "BPMNEdge"),
                      {"id": f"{flow_ids[-1]}_di",
                       "bpmnElement": flow_ids[-1]})
    ET.SubElement(e, _qname(DIAG_NS, "waypoint"),
                  {"x": str(prev[0]), "y": str(prev[1])})
    ET.SubElement(e, _qname(DIAG_NS, "waypoint"),
                  {"x": str(x), "y": str(y_mid + 18)})

    buf = io.BytesIO()
    ET.ElementTree(defs).write(buf, xml_declaration=True, encoding="UTF-8")
    return buf.getvalue()


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

    # Bepaal welke secties paraplu's zijn voor subprocessen. Een sectie
    # is een 'umbrella' als ze zelf 'process' is EN er minstens 1 directe
    # subsectie is die OOK 'process' is (niet een 'field'-subsectie).
    # Als alle subsecties 'field' zijn, is de parent een gewoon proces
    # met SOLL-velden als subsecties.
    umbrella_indices: set[int] = set()
    for i, (depth, sec) in enumerate(all_sections):
        if classified[i]["classification"] != "process":
            continue
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
