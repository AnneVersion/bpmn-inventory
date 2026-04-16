"""
Generate the methodology + overview report (Word).

Sections:
  1. Doel & samenvatting
  2. Methodiek (hoe extraheren we wat uit BPMN)
  3. Overzicht ingelezen BPMN-bestanden
  4. Actoren (intern + extern)
  5. Ankerobjecten
  6. Procesbeschrijving per BPMN
  7. Verantwoording classificatie-keuzes
"""

from __future__ import annotations

from collections import defaultdict

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.shared import Cm, Pt, RGBColor

from merger import MergedModel


def _styled_paragraph(doc, text: str, bold: bool = False,
                      size: int = 11, color: tuple[int, int, int] | None = None):
    p = doc.add_paragraph()
    run = p.add_run(text)
    run.bold = bold
    run.font.size = Pt(size)
    if color:
        run.font.color.rgb = RGBColor(*color)
    return p


def _table(doc, headers: list[str], rows: list[list[str]]):
    table = doc.add_table(rows=1, cols=len(headers))
    table.style = "Light Grid Accent 1"
    table.alignment = WD_TABLE_ALIGNMENT.LEFT
    hdr = table.rows[0].cells
    for i, h in enumerate(headers):
        hdr[i].text = ""
        run = hdr[i].paragraphs[0].add_run(h)
        run.bold = True
        run.font.size = Pt(10)
    for r in rows:
        cells = table.add_row().cells
        for i, v in enumerate(r):
            cells[i].text = str(v) if v is not None else ""
            for para in cells[i].paragraphs:
                for run in para.runs:
                    run.font.size = Pt(9)
    return table


def write_docx(model: MergedModel, out_path: str) -> None:
    doc = Document()

    # Page setup: A4 portrait, 2cm margins
    for section in doc.sections:
        section.page_width = Cm(21.0)
        section.page_height = Cm(29.7)
        section.left_margin = Cm(2.0)
        section.right_margin = Cm(2.0)
        section.top_margin = Cm(2.0)
        section.bottom_margin = Cm(2.0)

    # Default font
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)

    # --- Title ---
    title = doc.add_heading("Data-inventarisatie op basis van BPMN", level=0)
    for run in title.runs:
        run.font.color.rgb = RGBColor(0x1F, 0x3A, 0x5F)
    _styled_paragraph(
        doc,
        "Automatisch gegenereerd overzicht van data-objecten, actoren en "
        "ankerobjecten uit de aangeleverde BPMN-bestanden.",
        size=10, color=(0x55, 0x55, 0x55),
    )

    # --- 1. Samenvatting ---
    doc.add_heading("1. Samenvatting", level=1)
    n_bpmn = len(model.bpmns)
    n_tasks = sum(len(b.tasks) for b in model.bpmns)
    n_data = sum(len(b.data_objects) for b in model.bpmns)
    n_actors = len(model.actors)
    n_anchors = len(model.anchor_objects())
    n_rows = len(model.inventory)
    summary = (
        f"Verwerkt: {n_bpmn} BPMN-bestand(en) met in totaal {n_tasks} "
        f"processtappen, {n_data} expliciete data-objecten en {n_actors} unieke "
        f"actoren (intern + extern). De data-inventarisatie telt {n_rows} regels. "
        f"{n_anchors} dataobject-naam/-namen komen in meerdere BPMN's voor en "
        f"zijn gemarkeerd als ankerobject."
    )
    _styled_paragraph(doc, summary)

    # --- 2. Methodiek ---
    doc.add_heading("2. Methodiek — hoe extraheren we wat uit BPMN?", level=1)
    _styled_paragraph(
        doc,
        "Iedere BPMN 2.0 file is een XML-structuur. De parser leest de "
        "elementen uit en mapt ze als volgt op de inventarisatie-categorieën:",
    )
    method_rows = [
        ["<bpmn:participant>", "Pool / organisatie",
         "Indien zonder processRef → externe Actor; "
         "indien met processRef → eigen organisatie (geen losse actor)."],
        ["<bpmn:lane>", "Interne Actor (rol)",
         "Naam van de lane = rolnaam (bv. Frontoffice, Teamleider). "
         "flowNodeRef-elementen koppelen taken aan deze lane."],
        ["<bpmn:task>, <bpmn:userTask>, <bpmn:serviceTask>, …",
         "Processtap",
         "Iedere subtype valt onder 'Activity' in BPMN 2.0 spec."],
        ["<bpmn:dataObject> + <bpmn:dataObjectReference>",
         "Entiteit / Dataobject",
         "Definitie en verwijzing samen geclusterd op naam."],
        ["<bpmn:dataStore(Reference)>", "Bron (master)",
         "Persistente opslag — gemapt op kolom 'Bron (master)'."],
        ["<bpmn:dataInputAssociation> / <…OutputAssociation>",
         "Koppeling Processtap ↔ Dataobject",
         "Directe link tussen taak en data-element."],
        ["<bpmn:exclusiveGateway>, <…parallelGateway>, …",
         "Procesattribuut (routering)",
         "Conditions, default flows; geen persoonsgegeven."],
        ["<bpmn:intermediateThrowEvent> + <messageEventDefinition>",
         "Procesevent (data-uitwisseling)",
         "Vooral relevant als communicatie naar externe actor plaatsvindt."],
        ["<bpmn:textAnnotation> + <bpmn:association>",
         "Opmerking / business rule",
         "Vrije tekst die naast een activiteit hoort."],
        ["<bpmn:messageFlow>", "Communicatie tussen actoren",
         "Pijl tussen pools — duidt data-overdracht."],
    ]
    _table(doc, ["BPMN-element", "Inventarisatie-categorie", "Onderbouwing"],
           method_rows)

    _styled_paragraph(doc, "")
    _styled_paragraph(
        doc,
        "Sensitiviteits-classificatie volgt de FNV-legenda (Openbaar / "
        "Intern / Vertrouwelijk / Bijzonder persoonsgegeven). De heuristiek "
        "kijkt naar trefwoorden in de naam:",
    )
    _table(doc, ["Trefwoord(en) in naam", "Classificatie", "Reden"], [
        ["vakbond, lidmaatschap, gezondheid, etnisch, religie, politiek, "
         "biometr, seksueel", "Bijzonder persoonsgegeven", "AVG art. 9"],
        ["iban, salaris, geboorte, bsn, machtiging, incasso, betaal, loon, "
         "bedrag, aandrager, bewijs", "Vertrouwelijk", "Financieel of gevoelig"],
        ["(geen match)", "Intern", "Standaard — geen PII-signaal"],
    ])

    # --- 3. Overzicht bestanden ---
    doc.add_heading("3. Verwerkte BPMN-bestanden", level=1)
    rows = []
    for parsed in model.bpmns:
        rows.append([
            parsed.source_file, parsed.process_name,
            len(parsed.lanes), len(parsed.tasks), len(parsed.data_objects),
            len(parsed.gateways), len(parsed.events),
        ])
    _table(doc, ["Bestand", "Proces", "Lanes", "Tasks", "DataObj",
                 "Gateways", "Events"], rows)

    # --- 4. Actoren ---
    doc.add_heading("4. Actoren", level=1)
    _styled_paragraph(
        doc, "Interne actoren komen uit <bpmn:lane>; externe actoren uit "
             "<bpmn:participant> zonder processRef."
    )
    rows = [[a.name, a.subtype,
             ", ".join(a.evidence.get("appears_in", [])),
             a.evidence.get("classification_reason", "")]
            for a in model.actors]
    _table(doc, ["Actor", "Type", "Voorkomt in", "Onderbouwing"], rows)

    # --- 5. Ankerobjecten ---
    doc.add_heading("5. Ankerobjecten (≥ 2 processen)", level=1)
    if not model.anchor_objects():
        _styled_paragraph(
            doc,
            "Geen ankerobjecten gevonden — alle expliciete dataObjects "
            "komen maar in één BPMN voor. Dat hoeft geen probleem te zijn, "
            "maar is wel een aandachtspunt: gedeelde entiteiten zoals 'Lid', "
            "'Lidmaatschap', 'Contributie' uit de Excel-template komen in de "
            "BPMN's niet als <bpmn:dataObject> terug. Aanbeveling: voeg "
            "expliciete dataObjects toe (zie ★-correcties in template).",
        )
    else:
        rows = []
        for name in model.anchor_objects():
            files = sorted({sf for sf, _ in model.data_object_index[name.lower()]})
            rows.append([name, len(files), ", ".join(files)])
        _table(doc, ["Ankerobject", "Aantal BPMN's", "Voorkomt in"], rows)

    # --- 6. Procesbeschrijvingen ---
    doc.add_heading("6. Proces per BPMN", level=1)
    for parsed in model.bpmns:
        doc.add_heading(parsed.process_name, level=2)
        _styled_paragraph(doc, f"Bron: {parsed.source_file}",
                          size=9, color=(0x55, 0x55, 0x55))

        # Actors in this file
        if parsed.lanes:
            _styled_paragraph(doc, "Interne actoren:", bold=True)
            for lane in parsed.lanes:
                doc.add_paragraph(f"• {lane.name}", style="List Bullet")

        ext_actors = [p for p in parsed.participants
                      if not p.attributes.get("processRef")]
        if ext_actors:
            _styled_paragraph(doc, "Externe actoren / pools:", bold=True)
            for p in ext_actors:
                doc.add_paragraph(f"• {p.name}", style="List Bullet")

        # Process steps in order
        if parsed.tasks:
            _styled_paragraph(doc, "Processtappen:", bold=True)
            rows = [[f"A{i+1}", t.name, t.subtype, t.lane_id or "—"]
                    for i, t in enumerate(parsed.tasks)]
            _table(doc, ["Stap-ID", "Taak", "Subtype", "Lane-id"], rows)

        if parsed.data_objects:
            _styled_paragraph(doc, "Data-objecten:", bold=True)
            rows = [[d.name, d.subtype, d.id] for d in parsed.data_objects]
            _table(doc, ["Naam", "Subtype", "BPMN-id"], rows)

        if parsed.annotations:
            _styled_paragraph(doc, "Opmerkingen / business rules:", bold=True)
            for a in parsed.annotations:
                doc.add_paragraph(f"• {a.attributes.get('text','')}",
                                  style="List Bullet")

    doc.save(out_path)
