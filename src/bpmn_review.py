"""
BPMN kwaliteitscheck.

Controleert iedere ParsedBpmn op BPMN 2.0 modelleringsregels en op
semantische issues (taak-namen die data-interactie impliceren terwijl
er geen dataObject / dataAssociation is).

Iedere check produceert Findings met severity (error | warning | info),
een ruleId, het betrokken element en een suggestie.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from bpmn_parser import ParsedBpmn, BpmnElement
from merger import MergedModel

# Hergebruik canonical-map + attribute-hints uit bpmn_erd
try:
    from bpmn_erd import canonicalize as _erd_canonicalize, \
                         ATTRIBUTE_HINTS as _ERD_HINTS, \
                         CANONICAL_MAP as _ERD_MAP
except Exception:  # pragma: no cover
    _erd_canonicalize = None
    _ERD_HINTS, _ERD_MAP = {}, {}


# ---------------------------------------------------------------------------
# Rule catalog
# ---------------------------------------------------------------------------

RULES: dict[str, dict] = {
    # --- Structureel ---
    "R001": {"title": "Taak zonder lane", "severity": "warning",
             "why": "Een taak hoort in een lane die de uitvoerende rol aangeeft."},
    "R002": {"title": "Taak zonder inkomende sequence flow", "severity": "error",
             "why": "Alleen start-events mogen geen inkomende flow hebben."},
    "R003": {"title": "Taak zonder uitgaande sequence flow", "severity": "error",
             "why": "Alleen end-events mogen geen uitgaande flow hebben."},
    "R004": {"title": "Proces zonder start-event", "severity": "warning",
             "why": "Elk proces hoort expliciet te beginnen met een <bpmn:startEvent>."},
    "R005": {"title": "Proces zonder end-event", "severity": "warning",
             "why": "Elk proces hoort expliciet te eindigen met een <bpmn:endEvent>."},
    "R006": {"title": "DataObject zonder koppeling", "severity": "info",
             "why": "Een dataObject zonder data(Input|Output)Association is niet"
                    " aan een processtap gekoppeld en blijft 'los hangen'."},
    "R007": {"title": "Gateway met slechts 1 uitgaande flow", "severity": "warning",
             "why": "Een gateway splitst of merget stromen. 1 uitgaande flow"
                    " betekent dat de gateway overbodig is."},
    "R008": {"title": "Exclusive gateway zonder default flow", "severity": "info",
             "why": "Best practice: een <bpmn:exclusiveGateway> krijgt een"
                    " default-flow zodat er altijd een vervolg is."},
    "R009": {"title": "Element zonder naam", "severity": "warning",
             "why": "Zonder naam is onduidelijk wat het element representeert."},
    "R010": {"title": "Generieke taaknaam", "severity": "info",
             "why": "Namen als 'Task', 'Activity', 'Taak' zijn betekenisloos."},
    "R011": {"title": "MessageFlow binnen zelfde pool", "severity": "error",
             "why": "MessageFlows mogen alleen tussen verschillende pools lopen."},
    "R012": {"title": "Externe pool zonder messageFlow", "severity": "warning",
             "why": "Een externe actor (pool zonder processRef) die geen"
                    " messageFlow heeft, is visueel ornament zonder betekenis."},

    # --- Semantisch ---
    "R101": {"title": "Taak impliceert data-interactie zonder dataObject",
             "severity": "warning",
             "why": "De taaknaam bevat sleutelwoorden die data-interactie"
                    " impliceren, maar er is geen data(Input|Output)Association."},
    "R102": {"title": "Taak noemt systeem / register zonder dataStore",
             "severity": "info",
             "why": "De taak verwijst naar een systeem (CRM, SAP, register) maar"
                    " er is geen <bpmn:dataStore> als bron gedefinieerd."},
    "R103": {"title": "DataObject zonder naam", "severity": "error",
             "why": "Een naamloos dataObject kan niet in de inventarisatie."},

    # --- Cross-BPMN (ERD-afleiding) ---
    "X001": {"title": "Entity wordt gelezen maar nooit aangemaakt",
             "severity": "warning",
             "why": "Ergens wordt de entity geconsumeerd terwijl geen "
                    "aangeleverd proces hem aanmaakt — mogelijk ontbreekt "
                    "een CREATE-proces of moet de entity een dataStore zijn."},
    "X002": {"title": "Entity wordt aangemaakt maar nooit gelezen",
             "severity": "info",
             "why": "Dead data: de entity wordt opgevoerd maar door geen "
                    "aangeleverd proces gebruikt."},
    "X003": {"title": "Zelfde entiteit onder verschillende namen",
             "severity": "info",
             "why": "De tool heeft meerdere aliases samengevoegd tot één "
                    "entiteit; harmoniseer de naamgeving."},
    "X004": {"title": "Entity alleen impliciet gedetecteerd (geen dataObject)",
             "severity": "warning",
             "why": "De entity is uitsluitend uit taaknamen/annotaties "
                    "afgeleid, niet via een <bpmn:dataObject> of "
                    "<bpmn:dataStore>. Verbeterpunt: voeg een expliciete "
                    "dataObject-koppeling toe voor traceerbaarheid."},
}


# Sleutelwoorden in taaknaam -> impliciete data-interactie, met actietype:
#   READ   = dataInputAssociation (taak leest object)
#   WRITE  = dataOutputAssociation (taak maakt / schrijft object)
#   UPDATE = beide (taak leest + wijzigt)
VERB_ACTIONS: dict[str, str] = {
    # WRITE = nieuw aanmaken / invoeren / vastleggen
    "toevoeg": "WRITE", "registreer": "WRITE", "invoer": "WRITE",
    "invul": "WRITE", "aanmaak": "WRITE", "aanmak": "WRITE",
    "creeer": "WRITE", "opvoer": "WRITE", "vastleg": "WRITE",
    "opslaan": "WRITE", "opsla": "WRITE", "bewaar": "WRITE",
    "verzend": "WRITE", "verstuur": "WRITE",
    # UPDATE = bestaand wijzigen / uitvoeren van een bewerking
    "muteer": "UPDATE", "wijzig": "UPDATE", "pas aan": "UPDATE",
    "aanpas": "UPDATE", "update": "UPDATE", "bijwerk": "UPDATE",
    "verwerk": "UPDATE", "uitvoer": "UPDATE", "afsluit": "UPDATE",
    # READ = opzoeken / raadplegen / controleren
    "zoek op": "READ", "opzoek": "READ", "raadpleeg": "READ",
    "raadple": "READ", "bekijk": "READ",
    "ontvang": "READ",
    "goedkeur": "READ", "beoordeel": "READ", "controleer": "READ",
    "valideer": "READ", "afkeur": "READ", "afwij": "READ",
}
# Backwards-compat: platte lijst verbs voor keyword-matching
DATA_VERBS = list(VERB_ACTIONS.keys())

# Zelfstandige-naamwoord-achtige hints die op een dataobject wijzen.
# Hier staan zowel samengestelde ('organisatiegegeven') als de los-staande
# entiteiten ('organisatie') zodat R101 in beide vormen treft.
DATA_NOUNS = [
    # Generieke data-substantieven
    "gegeven", "gegevens", "dossier", "formulier", "aanvraag",
    "aanvragen", "contract", "factuur", "machtiging", "bestand",
    "document", "melding", "brief", "notitie", "record",
    "verzoek", "mandaat", "incasso", "betaling",
    "profiel", "account",
    # Samengestelde concepten
    "organisatiegegeven", "persoonsgegeven", "bedrijfsgegeven",
    "lidmaatschap", "inschrijv", "opzegging", "wijziging",
    "jaaropgave", "tariefgroep", "werverspremie",
    # Losstaande entiteiten (komen vaak los in taaknamen voor)
    "organisatie", "bedrijf", "werkgever", "werknemer",
    "persoon", "klant", "lid", "leden",
    "adres", "naam", "iban", "bsn",
    "cao",
]

# Systeem / bron-namen die vaak als (ontbrekende) dataStore voorkomen
SYSTEMS = [
    "crm", "sap", "salesforce", "afas", "exact", "odoo",
    "ledenadministratie", "systeem", "register", "portaal", "portal",
    "database", "kvk-register", "handelsregister",
    "excel", "sharepoint",
]

# Namen die als "leeg" gelden
GENERIC_NAMES = {"task", "activity", "taak", "activiteit", "stap", "step", ""}


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class Finding:
    rule: str
    severity: str
    source_file: str
    element_id: str
    element_name: str
    element_kind: str
    message: str
    suggestion: str = ""
    matched_keywords: list[str] = field(default_factory=list)
    # Verrijking voor R101 (data-interactie) -- maakt auto-fix mogelijk:
    action_type: str = ""           # "READ" | "WRITE" | "UPDATE" | ""
    suggested_object: str = ""      # canonical bv. 'Organisatie'
    suggested_attributes: list[dict] = field(default_factory=list)  # [{name, type}]
    fixable: bool = False           # of een 'Toepassen'-flow mogelijk is
    # Verrijking voor R008 (gateway zonder default flow):
    outgoing_flows: list[dict] = field(default_factory=list)  # [{id, name, target_id, target_name}]
    # --- Evidence / redenering: transparant hoe we tot deze conclusie kwamen
    evidence: dict = field(default_factory=dict)
    # Typische keys:
    #   checked    = 'wat hebben we onderzocht'
    #   observed   = 'wat zagen we'
    #   expected   = 'wat hoort er te staan'
    #   conclusion = 'waarom is dit een probleem'
    #   xml_refs   = ['<bpmn:flowNodeRef>T1 (niet gevonden)', ...]
    # Fix-type per regel (bepaalt dialog in UI)
    fix_type: str = ""              # "auto" | "text" | "pick_lane" | "pick_task" | ...
    fix_params: dict = field(default_factory=dict)  # context-data voor dialog

    def to_dict(self) -> dict:
        return {
            "rule": self.rule,
            "rule_title": RULES.get(self.rule, {}).get("title", self.rule),
            "severity": self.severity,
            "source_file": self.source_file,
            "element_id": self.element_id,
            "element_name": self.element_name,
            "element_kind": self.element_kind,
            "message": self.message,
            "suggestion": self.suggestion,
            "matched_keywords": self.matched_keywords,
            "action_type": self.action_type,
            "suggested_object": self.suggested_object,
            "suggested_attributes": self.suggested_attributes,
            "fixable": self.fixable,
            "outgoing_flows": self.outgoing_flows,
            "evidence": self.evidence,
            "fix_type": self.fix_type,
            "fix_params": self.fix_params,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_keywords(text: str, keywords: list[str]) -> list[str]:
    low = text.lower()
    return [k for k in keywords if k in low]


def _suggest_data_object(task_name: str, nouns: list[str]) -> str:
    """Gok een dataobject-naam uit de gevonden noun-keywords.

    Stappen:
    1. Extract het originele woord dat de noun bevat uit de taaknaam.
    2. Stuur dat door `canonicalize()` (indien beschikbaar) om
       'Organisatiegegevens' -> 'Organisatie' te krijgen.
    3. Val terug op Title-case van de noun zelf.
    """
    if not nouns:
        return ""
    noun = nouns[0]
    raw = ""
    for word in re.findall(r"[A-Za-z]{3,}", task_name):
        if noun.lower() in word.lower():
            raw = word
            break
    if not raw:
        raw = noun
    if _erd_canonicalize:
        canon = _erd_canonicalize(raw)
        if canon:
            return canon
    return raw[0].upper() + raw[1:]


def _classify_action(verbs: list[str]) -> str:
    """Map gevonden werkwoorden naar een actietype (READ/WRITE/UPDATE)."""
    actions = {VERB_ACTIONS.get(v) for v in verbs if v in VERB_ACTIONS}
    actions.discard(None)
    if not actions:
        return ""
    # Prioriteit: UPDATE > WRITE > READ  (UPDATE impliceert lezen + schrijven)
    if "UPDATE" in actions:
        return "UPDATE"
    if "WRITE" in actions and "READ" in actions:
        return "UPDATE"
    if "WRITE" in actions:
        return "WRITE"
    return "READ"


def _lookup_attributes(canonical_object: str,
                       user_defs: dict | None = None) -> list[dict]:
    """Haal attribuut-suggesties op uit user-defs (priority) of ATTRIBUTE_HINTS.

    `user_defs` is de user-dictionary: `{"objects": {"Organisatie": [...]}}`.
    """
    if not canonical_object:
        return []
    key_exact = canonical_object
    key_low = canonical_object.lower()

    # 1. User-dictionary heeft voorrang (exacte naam, case-insensitive)
    if user_defs and isinstance(user_defs, dict):
        objs = user_defs.get("objects", {})
        for k, attrs in objs.items():
            if k.lower() == key_low:
                return _normalize_attrs(attrs)

    # 2. Built-in ATTRIBUTE_HINTS (substring match op key)
    for hint_key, specs in _ERD_HINTS.items():
        if hint_key in key_low and len(hint_key) > 3:
            return [_spec_to_dict(s) for s in specs]
    if key_low in _ERD_HINTS:
        return [_spec_to_dict(s) for s in _ERD_HINTS[key_low]]
    return []


def _spec_to_dict(spec: str) -> dict:
    """'iban:string:uniek' -> {name, type, required, unique}"""
    parts = spec.split(":")
    name = parts[0]
    typ = parts[1] if len(parts) > 1 else "string"
    flags = parts[2:] if len(parts) > 2 else []
    return {
        "name": name,
        "type": typ,
        "required": "required" in flags,
        "unique": "uniek" in flags or "unique" in flags,
    }


def _normalize_attrs(attrs: list) -> list[dict]:
    """User-defs kunnen list[str] (spec) of list[dict] zijn."""
    out = []
    for a in attrs:
        if isinstance(a, str):
            out.append(_spec_to_dict(a))
        elif isinstance(a, dict):
            out.append({
                "name": a.get("name", ""),
                "type": a.get("type", "string"),
                "required": bool(a.get("required", False)),
                "unique": bool(a.get("unique", False)),
            })
    return out


def _flows_source_target(parsed: ParsedBpmn) -> tuple[set[str], set[str]]:
    """Verzamel node-ids die ergens source of target zijn."""
    sources, targets = set(), set()
    for f in parsed.sequence_flows:
        s = f.attributes.get("source", "")
        t = f.attributes.get("target", "")
        if s: sources.add(s)
        if t: targets.add(t)
    return sources, targets


# ---------------------------------------------------------------------------
# Per-BPMN review
# ---------------------------------------------------------------------------

def _review_structural(parsed: ParsedBpmn) -> list[Finding]:
    findings: list[Finding] = []
    src = parsed.source_file
    has_outgoing, has_incoming = _flows_source_target(parsed)

    # Task-niveau
    lane_options = [{"id": l.id, "name": l.name} for l in parsed.lanes]
    task_options = [{"id": tt.id, "name": tt.name or tt.id}
                    for tt in parsed.tasks]

    for t in parsed.tasks:
        name = (t.name or "").strip()

        # R009: taak zonder naam
        if not name:
            findings.append(Finding(
                rule="R009", severity=RULES["R009"]["severity"],
                source_file=src, element_id=t.id, element_name="(geen naam)",
                element_kind=t.subtype,
                message="Taak heeft geen name-attribuut.",
                suggestion="Geef elke taak een actiegerichte naam "
                           "(werkwoord + object).",
                evidence={
                    "checked": f"Attribute `name` op <bpmn:{t.subtype} id=\"{t.id}\">",
                    "observed": "name-attribute ontbreekt of is leeg",
                    "expected": "Een beschrijvende naam (bv. 'Registreer lid')",
                    "conclusion": "Zonder naam is de taak onleesbaar voor auditors.",
                },
                fixable=True, fix_type="text",
                fix_params={"field": "name", "label": "Nieuwe taaknaam",
                            "placeholder": "Werkwoord + object, bv. 'Verwerk aanvraag'"},
            ))

        # R010: generieke taaknaam
        if name.lower() in GENERIC_NAMES:
            findings.append(Finding(
                rule="R010", severity=RULES["R010"]["severity"],
                source_file=src, element_id=t.id, element_name=name,
                element_kind=t.subtype,
                message=f"Taaknaam '{name}' is generiek en zegt niets over de"
                        " handeling.",
                suggestion="Hernoem naar iets als 'Beoordeel aanvraag' of"
                           " 'Registreer lidmaatschap'.",
                evidence={
                    "checked": f"Taaknaam '{name}' vergeleken met generieke-lijst",
                    "observed": f"'{name.lower()}' staat in GENERIC_NAMES "
                                f"{sorted(GENERIC_NAMES)}",
                    "expected": "Een specifieke naam met werkwoord + object",
                    "conclusion": "Generieke namen maken het proces onbegrijpelijk.",
                },
                fixable=True, fix_type="text",
                fix_params={"field": "name", "label": "Nieuwe taaknaam",
                            "placeholder": "Werkwoord + object, bv. 'Beoordeel aanvraag'",
                            "current": name},
            ))

        # R001: taak zonder lane
        if not t.lane_id:
            findings.append(Finding(
                rule="R001", severity=RULES["R001"]["severity"],
                source_file=src, element_id=t.id, element_name=name,
                element_kind=t.subtype,
                message=f"Taak '{name}' zit niet in een lane.",
                suggestion="Plaats de taak in een <bpmn:lane> om de"
                           " uitvoerende rol/afdeling expliciet te maken.",
                evidence={
                    "checked": (f"<bpmn:flowNodeRef>{t.id}</bpmn:flowNodeRef> "
                                f"in alle <bpmn:lane>-elementen + "
                                "geometrische DI-fallback op <bpmndi:BPMNShape>-bounds"),
                    "observed": (f"Taak-id {t.id} niet gevonden als flowNodeRef "
                                 f"in lanes {[l.name for l in parsed.lanes]} én "
                                 "shape-center valt buiten alle lane-bounds"),
                    "expected": "Task.lane_id verwijst naar een lane.id",
                    "conclusion": "Zonder lane is de uitvoerende rol onbekend "
                                  "— vereist voor RACI-analyse en autorisatie.",
                },
                fixable=bool(lane_options),
                fix_type="pick_lane" if lane_options else "",
                fix_params={"lanes": lane_options} if lane_options else {},
            ))

        # R002 / R003: hangende taken
        if t.id not in has_incoming:
            findings.append(Finding(
                rule="R002", severity=RULES["R002"]["severity"],
                source_file=src, element_id=t.id, element_name=name,
                element_kind=t.subtype,
                message=f"Taak '{name}' heeft geen inkomende sequence flow.",
                suggestion="Verbind een voorgaande taak of start-event.",
                evidence={
                    "checked": "Zoekt naar <bpmn:sequenceFlow targetRef=\"" + t.id + "\">",
                    "observed": f"0 sequence flows wijzen naar {t.id}",
                    "expected": ">= 1 inkomende sequenceFlow (behalve start-events)",
                    "conclusion": "Taak kan nooit worden gestart vanuit het "
                                  "proces; is een hangende activiteit.",
                },
            ))
        if t.id not in has_outgoing:
            findings.append(Finding(
                rule="R003", severity=RULES["R003"]["severity"],
                source_file=src, element_id=t.id, element_name=name,
                element_kind=t.subtype,
                message=f"Taak '{name}' heeft geen uitgaande sequence flow.",
                suggestion="Verbind met een volgende taak of end-event.",
                evidence={
                    "checked": "Zoekt naar <bpmn:sequenceFlow sourceRef=\"" + t.id + "\">",
                    "observed": f"0 sequence flows vertrekken vanaf {t.id}",
                    "expected": ">= 1 uitgaande sequenceFlow (behalve end-events)",
                    "conclusion": "Proces komt tot stilstand; geen vervolg gedefinieerd.",
                },
            ))

    # Proces-niveau
    start_events = [e for e in parsed.events if e.subtype == "startEvent"]
    end_events = [e for e in parsed.events if e.subtype == "endEvent"]
    if parsed.tasks and not start_events:
        findings.append(Finding(
            rule="R004", severity=RULES["R004"]["severity"],
            source_file=src, element_id=parsed.process_name or "(proces)",
            element_name=parsed.process_name or src, element_kind="process",
            message="Proces heeft geen <bpmn:startEvent>.",
            suggestion="Voeg een start-event toe vooraan het proces.",
            evidence={
                "checked": "Aantal <bpmn:startEvent>-elementen in het process",
                "observed": "0 start events gevonden",
                "expected": ">= 1 start event (BPMN 2.0 best practice)",
                "conclusion": "Proces heeft geen duidelijk startpunt; "
                              "uitvoeringsengine kan niet weten waar te beginnen.",
            },
        ))
    if parsed.tasks and not end_events:
        findings.append(Finding(
            rule="R005", severity=RULES["R005"]["severity"],
            source_file=src, element_id=parsed.process_name or "(proces)",
            element_name=parsed.process_name or src, element_kind="process",
            message="Proces heeft geen <bpmn:endEvent>.",
            suggestion="Voeg een end-event toe aan het einde van het proces.",
            evidence={
                "checked": "Aantal <bpmn:endEvent>-elementen in het process",
                "observed": "0 end events gevonden",
                "expected": ">= 1 end event",
                "conclusion": "Proces heeft geen duidelijk eindpunt.",
            },
        ))

    # Gateways
    # Tel uitgaande flows per gateway
    out_count: dict[str, int] = defaultdict(int)
    for f in parsed.sequence_flows:
        s = f.attributes.get("source", "")
        if s:
            out_count[s] += 1
    for gw in parsed.gateways:
        n_out = out_count.get(gw.id, 0)
        if n_out == 1:
            findings.append(Finding(
                rule="R007", severity=RULES["R007"]["severity"],
                source_file=src, element_id=gw.id, element_name=gw.name,
                element_kind=gw.subtype,
                message=f"Gateway '{gw.name or gw.id}' heeft slechts 1 uitgaande"
                        " flow.",
                suggestion="Verwijder de gateway (flow wordt direct gemaakt) "
                           "of voeg een tweede uitgaande flow met conditie toe.",
                evidence={
                    "checked": (f"Aantal <bpmn:sequenceFlow sourceRef=\"{gw.id}\">-"
                                "elementen"),
                    "observed": f"{n_out} uitgaande flow",
                    "expected": ">= 2 (gateway splitst of merget anders niet)",
                    "conclusion": "Met 1 uitgaande flow voegt de gateway niets "
                                  "toe; het diagram is verwarrend.",
                },
                fixable=True, fix_type="auto_confirm",
                fix_params={"operation": "remove_degenerate_gateway",
                            "gateway_id": gw.id},
            ))
        # R008: exclusive zonder default -> lijst uitgaande flows bijvoegen
        if gw.subtype == "exclusiveGateway" and not gw.attributes.get("default"):
            outgoing = []
            task_by_id = {t.id: t for t in parsed.tasks}
            event_by_id = {e.id: e for e in parsed.events}
            gw_by_id = {g.id: g for g in parsed.gateways}
            for f in parsed.sequence_flows:
                if f.attributes.get("source") != gw.id:
                    continue
                tgt_id = f.attributes.get("target", "")
                tgt = (task_by_id.get(tgt_id)
                       or event_by_id.get(tgt_id)
                       or gw_by_id.get(tgt_id))
                outgoing.append({
                    "id": f.id,
                    "name": f.name or "",
                    "target_id": tgt_id,
                    "target_name": tgt.name if tgt else tgt_id,
                    "target_kind": tgt.subtype if tgt else "",
                })
            findings.append(Finding(
                rule="R008", severity=RULES["R008"]["severity"],
                source_file=src, element_id=gw.id, element_name=gw.name,
                element_kind=gw.subtype,
                message=f"ExclusiveGateway '{gw.name or gw.id}' heeft geen"
                        " default-flow.",
                suggestion=("Markeer een van de uitgaande flows als default. "
                            "Klik 'Toepassen' om te kiezen."),
                outgoing_flows=outgoing,
                fixable=len(outgoing) >= 2,
                fix_type="r008_pick_flow",
                evidence={
                    "checked": (f"`default`-attribuut op <bpmn:exclusiveGateway "
                                f"id=\"{gw.id}\">"),
                    "observed": "`default`-attribuut ontbreekt",
                    "expected": "default-flow is best practice zodat er altijd "
                                "een fallback-pad bestaat",
                    "conclusion": ("Zonder default blijft de flow hangen als "
                                   "geen van de condities matcht."),
                },
            ))

    # DataObjects zonder naam
    for d in parsed.data_objects:
        if not d.name or d.name.startswith("(naamloos"):
            findings.append(Finding(
                rule="R103", severity=RULES["R103"]["severity"],
                source_file=src, element_id=d.id, element_name="(naamloos)",
                element_kind=d.subtype,
                message="DataObject heeft geen name-attribuut.",
                suggestion="Geef het dataObject een betekenisvolle naam"
                           " (bv. 'Lidmaatschap', 'Factuur').",
                evidence={
                    "checked": f"Attribute `name` op <bpmn:{d.subtype} id=\"{d.id}\">",
                    "observed": f"name = '{d.name}'",
                    "expected": "Een betekenisvolle entity-naam",
                    "conclusion": "Naamloze dataObjects kunnen niet in de "
                                  "inventarisatie en niet in het ERD.",
                },
                fixable=True, fix_type="text",
                fix_params={"field": "name", "label": "DataObject-naam",
                            "placeholder": "bv. 'Lidmaatschap'"},
            ))

    # R006: dataObject zonder link
    linked_ids: set[str] = set()
    for a in parsed.data_associations:
        s = a.attributes.get("source", "")
        t = a.attributes.get("target", "")
        if s: linked_ids.add(s)
        if t: linked_ids.add(t)
    for d in parsed.data_objects:
        if d.id not in linked_ids:
            findings.append(Finding(
                rule="R006", severity=RULES["R006"]["severity"],
                source_file=src, element_id=d.id, element_name=d.name,
                element_kind=d.subtype,
                message=f"DataObject '{d.name}' is niet via een"
                        " data(Input|Output)Association aan een taak gekoppeld.",
                suggestion="Verbind het dataObject met de taak die het leest of"
                           " schrijft.",
                evidence={
                    "checked": (f"Of {d.id} voorkomt als sourceRef/targetRef "
                                "in een <bpmn:data(In|Out)putAssociation>"),
                    "observed": f"{d.id} nergens gevonden in data-associations",
                    "expected": "Minstens 1 taak die dit object leest of schrijft",
                    "conclusion": "Los dataObject dat niet wordt gebruikt in "
                                  "het proces.",
                },
                fixable=bool(task_options),
                fix_type="r006_link_task" if task_options else "",
                fix_params={"tasks": task_options,
                            "dataobject_id": d.id,
                            "dataobject_name": d.name} if task_options else {},
            ))

    return findings


def _review_semantic(parsed: ParsedBpmn,
                     user_defs: dict | None = None) -> list[Finding]:
    """Taaknamen met 'toevoegen organisatiegegevens in CRM'-patroon."""
    findings: list[Finding] = []
    src = parsed.source_file

    # Welke task-ids hebben een dataAssociation?
    task_ids = {t.id for t in parsed.tasks}
    tasks_with_data: set[str] = set()
    for a in parsed.data_associations:
        s = a.attributes.get("source", "")
        t = a.attributes.get("target", "")
        for tid in (s, t):
            if tid in task_ids:
                tasks_with_data.add(tid)

    for t in parsed.tasks:
        name = (t.name or "").strip()
        if not name:
            continue

        verbs = _find_keywords(name, DATA_VERBS)
        nouns = _find_keywords(name, DATA_NOUNS)
        systems = _find_keywords(name, SYSTEMS)

        # R101: taak doet data-interactie maar heeft geen dataAssociation
        if (verbs and nouns) and t.id not in tasks_with_data:
            suggested = _suggest_data_object(name, nouns)
            action = _classify_action(verbs)
            attrs = _lookup_attributes(suggested, user_defs)

            # Friendlier suggestion tekst
            action_nl = {"READ": "opzoeken / raadplegen",
                         "WRITE": "aanmaken / schrijven",
                         "UPDATE": "wijzigen (lezen + schrijven)"
                        }.get(action, "benaderen")
            attr_names = ", ".join(a["name"] for a in attrs) if attrs else ""
            attr_hint = (f" Verwachte attributen: {attr_names}."
                         if attr_names else
                         " (Geen attribuut-suggesties; voeg ze toe in Definities.)")
            suggestion = (
                f"Vermoedelijk object: '{suggested}' — actie: {action_nl}."
                + attr_hint
                + " Klik 'Toepassen' om een <bpmn:dataObject>"
                + (" + <bpmn:dataInputAssociation>" if action == "READ"
                   else " + <bpmn:dataOutputAssociation>" if action == "WRITE"
                   else " + data(Input|Output)Association")
                + " toe te voegen aan deze taak."
            ) if suggested else (
                "Voeg een expliciet dataObject toe en koppel het via een"
                " data(Input|Output)Association."
            )

            findings.append(Finding(
                rule="R101", severity=RULES["R101"]["severity"],
                source_file=src, element_id=t.id, element_name=name,
                element_kind=t.subtype,
                message=(f"Taak '{name}' bevat data-werkwoord "
                         f"{verbs!r} en data-zelfstandig naamwoord "
                         f"{nouns!r}, maar is niet gekoppeld aan een"
                         f" <bpmn:dataObject>."),
                suggestion=suggestion,
                matched_keywords=verbs + nouns,
                action_type=action,
                suggested_object=suggested,
                suggested_attributes=attrs,
                fixable=bool(suggested and action),
                fix_type="r101_add_dataobject" if suggested and action else "",
                evidence={
                    "checked": (f"Taaknaam '{name}' tegen DATA_VERBS en DATA_NOUNS, "
                                "en of {t.id} voorkomt in data-associations"),
                    "observed": (f"Werkwoorden matched: {verbs}; znwn matched: {nouns}; "
                                 "data-associations op deze taak: 0"),
                    "expected": ("Als een taak data bewerkt, moet er een "
                                 "<bpmn:data(In|Out)putAssociation> naar een "
                                 "<bpmn:dataObject> zijn."),
                    "conclusion": (f"Taak impliceert {action}-actie op "
                                   f"'{suggested}' maar is niet aan een "
                                   "dataObject gekoppeld."),
                },
            ))

        # R102: systeem genoemd zonder dataStore in proces
        if systems:
            has_datastore = any(
                (ds.name or "").lower().find(systems[0]) >= 0
                for ds in parsed.data_stores
            )
            if not has_datastore:
                findings.append(Finding(
                    rule="R102", severity=RULES["R102"]["severity"],
                    source_file=src, element_id=t.id, element_name=name,
                    element_kind=t.subtype,
                    message=(f"Taak '{name}' noemt systeem(en) "
                             f"{systems!r}, maar er is geen overeenkomstig"
                             f" <bpmn:dataStore> in het proces."),
                    suggestion=(f"Voeg een <bpmn:dataStoreReference>"
                                f" '{systems[0].upper()}' toe als master-bron."),
                    matched_keywords=systems,
                    evidence={
                        "checked": (f"Taaknaam '{name}' tegen SYSTEMS-lijst + "
                                    "aanwezige <bpmn:dataStore>-elementen"),
                        "observed": (f"Systeem-match: {systems}; geen "
                                     "overeenkomstige <bpmn:dataStore> in proces"),
                        "expected": ("Ieder gerefereerd master-systeem verdient "
                                     "een <bpmn:dataStoreReference>."),
                        "conclusion": ("Systeem wordt gebruikt maar niet als "
                                       "data-bron vastgelegd — lineage ontbreekt."),
                    },
                    fixable=True,
                    fix_type="r102_add_datastore",
                    fix_params={"system_name": systems[0].upper(), "task_id": t.id,
                                "action_type": _classify_action(verbs) or "READ"},
                ))

    return findings


def review(model: MergedModel, user_defs: dict | None = None) -> list[dict]:
    """Draai alle checks en geef dicts terug (JSON-ready).

    `user_defs` = inhoud van de user-dictionary (definities.json) met
    {"objects": {"<Naam>": [attribuut-specs...]}}. Heeft voorrang boven
    de ingebouwde ATTRIBUTE_HINTS bij R101-suggesties.
    """
    findings: list[Finding] = []
    for parsed in model.bpmns:
        findings.extend(_review_structural(parsed))
        findings.extend(_review_semantic(parsed, user_defs))

    # Sorteer: error > warning > info, dan per bestand
    order = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: (order.get(f.severity, 9),
                                 f.source_file, f.rule, f.element_name))
    return [f.to_dict() for f in findings]


def summarize(findings: list[dict]) -> dict:
    by_severity = defaultdict(int)
    by_rule = defaultdict(int)
    for f in findings:
        by_severity[f["severity"]] += 1
        by_rule[f["rule"]] += 1
    return {
        "total": len(findings),
        "by_severity": dict(by_severity),
        "by_rule": dict(by_rule),
        "rules_catalog": [
            {"id": rid, **meta} for rid, meta in RULES.items()
        ],
    }
