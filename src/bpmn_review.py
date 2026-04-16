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
}


# Sleutelwoorden die in een taaknaam data-interactie impliceren
DATA_VERBS = [
    "toevoeg", "registreer", "invoer", "invul",
    "muteer", "wijzig", "pas aan", "aanpas",
    "verwerk", "aanmaak", "aanmak", "creeer",
    "opvoer", "update", "bijwerk",
    "opslaan", "opsla", "bewaar", "vastleg",
    "zoek op", "opzoek", "raadpleeg", "raadple",
    "verzend", "verstuur", "ontvang",
    "goedkeur", "beoordeel", "controleer", "valideer",
    "afkeur", "afwij",
]

# Zelfstandige-naamwoord-achtige hints die op een dataobject wijzen
DATA_NOUNS = [
    "gegeven", "gegevens", "dossier", "formulier", "aanvraag",
    "aanvragen", "contract", "factuur", "machtiging", "bestand",
    "document", "melding", "brief", "notitie", "record",
    "lidmaatschap", "inschrijv", "opzegging", "wijziging",
    "verzoek", "mandaat", "incasso", "betaling",
    "profiel", "account", "lid",
    "organisatiegegeven", "persoonsgegeven", "bedrijfsgegeven",
    "adres", "naam", "iban", "bsn",
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
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_keywords(text: str, keywords: list[str]) -> list[str]:
    low = text.lower()
    return [k for k in keywords if k in low]


def _suggest_data_object(task_name: str, nouns: list[str]) -> str:
    """Gok een dataobject-naam uit de gevonden noun-keywords."""
    if not nouns:
        return ""
    noun = nouns[0]
    # Extract een woord uit de originele naam dat deze substring bevat
    for word in re.findall(r"[A-Za-z]{3,}", task_name):
        if noun.lower() in word.lower():
            # Capitalize
            return word[0].upper() + word[1:]
    return noun.capitalize()


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
            ))

        # R002 / R003: hangende taken
        if t.id not in has_incoming:
            findings.append(Finding(
                rule="R002", severity=RULES["R002"]["severity"],
                source_file=src, element_id=t.id, element_name=name,
                element_kind=t.subtype,
                message=f"Taak '{name}' heeft geen inkomende sequence flow.",
                suggestion="Verbind een voorgaande taak of start-event.",
            ))
        if t.id not in has_outgoing:
            findings.append(Finding(
                rule="R003", severity=RULES["R003"]["severity"],
                source_file=src, element_id=t.id, element_name=name,
                element_kind=t.subtype,
                message=f"Taak '{name}' heeft geen uitgaande sequence flow.",
                suggestion="Verbind met een volgende taak of end-event.",
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
        ))
    if parsed.tasks and not end_events:
        findings.append(Finding(
            rule="R005", severity=RULES["R005"]["severity"],
            source_file=src, element_id=parsed.process_name or "(proces)",
            element_name=parsed.process_name or src, element_kind="process",
            message="Proces heeft geen <bpmn:endEvent>.",
            suggestion="Voeg een end-event toe aan het einde van het proces.",
        ))

    # Gateways
    # Tel uitgaande flows per gateway
    out_count: dict[str, int] = defaultdict(int)
    for f in parsed.sequence_flows:
        s = f.attributes.get("source", "")
        if s:
            out_count[s] += 1
    for gw in parsed.gateways:
        if out_count.get(gw.id, 0) == 1:
            findings.append(Finding(
                rule="R007", severity=RULES["R007"]["severity"],
                source_file=src, element_id=gw.id, element_name=gw.name,
                element_kind=gw.subtype,
                message=f"Gateway '{gw.name or gw.id}' heeft slechts 1 uitgaande"
                        " flow.",
                suggestion="Verwijder de gateway of voeg een tweede uitgaande"
                           " flow met conditie toe.",
            ))
        # R008: exclusive zonder default
        if gw.subtype == "exclusiveGateway" and not gw.attributes.get("default"):
            findings.append(Finding(
                rule="R008", severity=RULES["R008"]["severity"],
                source_file=src, element_id=gw.id, element_name=gw.name,
                element_kind=gw.subtype,
                message=f"ExclusiveGateway '{gw.name or gw.id}' heeft geen"
                        " default-flow.",
                suggestion="Markeer een van de uitgaande flows als default via"
                           " het default-attribuut.",
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
            ))

    return findings


def _review_semantic(parsed: ParsedBpmn) -> list[Finding]:
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
            findings.append(Finding(
                rule="R101", severity=RULES["R101"]["severity"],
                source_file=src, element_id=t.id, element_name=name,
                element_kind=t.subtype,
                message=(f"Taak '{name}' bevat data-werkwoord "
                         f"{verbs!r} en data-zelfstandig naamwoord "
                         f"{nouns!r}, maar is niet gekoppeld aan een"
                         f" <bpmn:dataObject>."),
                suggestion=(f"Voeg een <bpmn:dataObject> '{suggested}' toe en"
                            " koppel met <bpmn:dataInputAssociation> of"
                            " <bpmn:dataOutputAssociation>."
                            if suggested else
                            "Voeg een expliciet dataObject toe en koppel het"
                            " met een data(Input|Output)Association."),
                matched_keywords=verbs + nouns,
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
                ))

    return findings


def review(model: MergedModel) -> list[dict]:
    """Draai alle checks en geef dicts terug (JSON-ready)."""
    findings: list[Finding] = []
    for parsed in model.bpmns:
        findings.extend(_review_structural(parsed))
        findings.extend(_review_semantic(parsed))

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
