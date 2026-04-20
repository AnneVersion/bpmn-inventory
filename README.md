# BPMN Data-Inventarisatie Tool

Genereert een data-inventarisatie, draw.io-diagrammen (BPMN samengevoegd
+ ERD) en een Word-rapport op basis van één of meerdere BPMN 2.0
bestanden.

Gemaakt om de "data-inventarisatie"-template (kolommen: Processtap,
Stap-ID, Dataobject, Attribuut, Verplicht?, Doelbinding, Classificatie,
Autorisatie, Bewaartermijn, Bron, Opmerkingen) automatisch te vullen
vanuit BPMN, en transparant te maken hoe elk inventarisatie-element uit
de BPMN-XML is afgeleid.

## Installatie

Vereist: Python 3.10+

```bash
pip install -r requirements.txt
```

Dat is alles. De parser gebruikt alleen de standard library
(`xml.etree.ElementTree`).

### Draaien in GitHub Codespaces (geen lokale setup)

1. Open de repo op GitHub → knop **Code → Codespaces → Create codespace**.
2. De Codespace start vanaf `.devcontainer/devcontainer.json`: Python 3.12,
   `pip install -r requirements.txt` draait automatisch bij first-boot, en
   `python src/webapp.py` start na attach. Port 8095 wordt doorgestuurd en
   opent direct een preview-tab.
3. Projecten en output landen in `output/` binnen de Codespace. Die
   verdwijnen als de Codespace wordt opgeruimd — commit belangrijke
   handmade-assets (`output/handmade/`) naar Git als je ze wilt bewaren.

## Mappenstructuur

```
bpmn_inventory/
├── data/                      # leg hier je .bpmn-bestanden neer
├── output/                    # alle gegenereerde outputs landen hier
├── src/
│   ├── bpmn_parser.py         # leest een .bpmn -> ParsedBpmn dataclass
│   ├── merger.py              # combineert + classificeert + bouwt rows
│   ├── xlsx_export.py         # schrijft data-inventarisatie.xlsx
│   ├── drawio_export.py       # schrijft bpmn-en-erd.drawio (2 pagina's)
│   ├── docx_export.py         # schrijft rapport.docx
│   └── main.py                # orchestrator (CLI)
└── README.md
```

## Gebruik

### CLI

```bash
cd bpmn_inventory
python src/main.py
```

Of met expliciete paden:

```bash
python src/main.py --data E:\scripts\webscraper\bpmn\data ^
                   --out  E:\scripts\webscraper\bpmn\output
```

### GUI (Tkinter)

```bash
python src/gui.py
```

Of dubbelklik `run_gui.bat` in Windows Verkenner.

### Web-frontend (Flask)

```bash
python serve.py
```

Of dubbelklik `run_web.bat`. Daarna openen: [http://localhost:8095](http://localhost:8095).

Upload een of meerdere `.bpmn`-bestanden via de pagina; de tool draait de
volledige pipeline en toont KPI's, actoren, classificatie-verdeling en een
filterbare inventarisatie-tabel met downloadknoppen voor xlsx, drawio,
docx en json. Iedere upload krijgt een eigen sessie-map onder
`output/sessions/<sid>/`, zodat parallelle sessies elkaar niet
overschrijven.

### Outputs

| Bestand | Inhoud |
|---|---|
| `data-inventarisatie.xlsx` | Sheets: *Alle processen*, één per BPMN, *Ankerobjecten*, *Actoren*, *Legenda* |
| `bpmn-en-erd.drawio` | Open in draw.io / diagrams.net / Visio. Pagina 1 = samengevoegde BPMN met actoren, taken, dataobjecten, gateways. Pagina 2 = ERD afgeleid uit dataobjecten. **Iedere shape heeft een tooltip** waarin staat uit welk XML-element hij komt en waarom hij zo geclassificeerd is. |
| `rapport.docx` | Methodiek, overzicht per BPMN, actorenoverzicht, ankerobjecten |
| `inventory.json` | Volledige dump als JSON, handig voor verdere automatisering |

## Hoe wordt wat afgeleid?

Iedere BPMN-XML-tag wordt op een vaste manier op een
inventarisatie-categorie gemapt. Onderstaand schema staat ook in het
Word-rapport en in de Legenda-sheet van de Excel.

| BPMN-element                                           | Inventarisatie-categorie       |
|--------------------------------------------------------|--------------------------------|
| `<bpmn:participant>` zonder `processRef`               | Actor (extern)                 |
| `<bpmn:lane>`                                          | Actor (intern, rol)            |
| `<bpmn:task>`, `<bpmn:userTask>`, `<bpmn:serviceTask>` | Processtap                     |
| `<bpmn:dataObject>`, `<bpmn:dataObjectReference>`      | Entiteit / dataobject          |
| `<bpmn:dataStore(Reference)>`                          | Bron (master)                  |
| `<bpmn:dataInputAssociation>`/`<…OutputAssociation>`   | Koppeling taak ↔ dataobject    |
| `<bpmn:exclusiveGateway>` enzovoort                    | Procesattribuut (routering)    |
| `<bpmn:intermediateThrowEvent>` + messageEventDef      | Procesevent (data-uitwisseling)|
| `<bpmn:textAnnotation>` + `<bpmn:association>`         | Opmerking / business rule      |
| `<bpmn:messageFlow>`                                   | Communicatie tussen actoren    |

### Sensitiviteits-classificatie

Heuristiek op basis van trefwoorden in de naam (configureerbaar in
`merger.py`, lijsten `SPECIAL_CATEGORY_KEYWORDS` en
`CONFIDENTIAL_KEYWORDS`):

| Trefwoord                                                                               | Classificatie               |
|-----------------------------------------------------------------------------------------|-----------------------------|
| vakbond, lidmaatschap, gezondheid, etnisch, religie, politiek, biometr, seksueel        | Bijzonder persoonsgegeven   |
| iban, salaris, geboorte, bsn, machtiging, incasso, betaal, loon, bedrag, aandrager      | Vertrouwelijk               |
| (geen match)                                                                            | Intern                      |

### Ankerobjecten

Een dataobject heet "anker" als de **naam** in twee of meer
BPMN-bestanden voorkomt (case-insensitive). In de draw.io krijgen
ankers een afwijkende oranje kleur en een ⚓-marker.

## Tooltips in draw.io

Open `bpmn-en-erd.drawio` in [diagrams.net](https://app.diagrams.net/)
en hover over een shape. Voorbeeld-tooltip:

```
BPMN-element: <bpmn:lane>
Naam: Frontoffice
BPMN-id: Lane_0zu1njy

Hoe geclassificeerd:
Element <bpmn:lane> binnen <bpmn:laneSet>; een lane representeert
een rol/actor in het proces.

Type in inventarisatie: Actor (intern)
```

Tooltips zijn geïmplementeerd via `<UserObject>`-wrappers — de
draw.io-standaard voor metadata + hover-info.

## Uitbreiden

- **Andere BPMN-dialecten** (Camunda extensions, Signavio, …): de
  parser leest alleen de standaard `bpmn:`-namespace, dus dialect-
  specifieke data wordt genegeerd. Voeg in `bpmn_parser.py` extra
  extractors toe als je die ook wilt.
- **Andere classificatie-trefwoorden**: pas de twee lijsten boven in
  `merger.py` aan.
- **Andere koppeling tussen task en dataobject**: nu via
  `dataInputAssociation` / `dataOutputAssociation`. Wil je ook impliciete
  koppeling op basis van naam-matching? Voeg een tweede pass toe in
  `build_inventory()`.

## Bekende beperkingen

- BPMN's zonder expliciete `<bpmn:dataObject>`-elementen leveren een
  inventarisatie waarin alleen de processtappen voorkomen (zonder
  data-koppelingen). Dit komt overeen met "★ ONTBREKEND" in de
  voorbeeldtemplate van FNV — die items moeten handmatig of via een
  BPMN-correctie aangevuld worden.
- De ERD is **afgeleid** uit dataobject-naamgelijkheid en
  task-co-occurrence; het is geen formeel datamodel. Beschouw het als
  startpunt voor een gesprek met de domein-expert.
- Heuristische classificatie kijkt alleen naar de naam, niet naar
  context. Controleer altijd Bijzonder/Vertrouwelijk handmatig.
