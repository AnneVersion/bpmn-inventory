"""
Tkinter GUI voor de BPMN Data-Inventarisatie tool.

Toont de samengevoegde BPMN en de afgeleide ERD interactief op
Canvas-elementen, met hover-tooltips die voor elk element uitleggen
welk BPMN-XML-element de bron was en hoe de classificatie tot stand
kwam.

Start met:   python src/gui.py
Of via:      run_gui.bat   (dubbelklik in Windows Verkenner)
"""

from __future__ import annotations

import os
import subprocess
import sys
import tkinter as tk
from collections import defaultdict
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

# Importeer de bestaande pipeline modules
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bpmn_parser import parse_all                        # noqa: E402
from merger import merge                                 # noqa: E402
from xlsx_export import write_xlsx                       # noqa: E402
from drawio_export import write_drawio                   # noqa: E402
from docx_export import write_docx                       # noqa: E402


# ---------------------------------------------------------------------------
# Tooltip helper

class Tooltip:
    """Floating tooltip-window dat de canvas-shape onder de muis beschrijft."""

    def __init__(self, widget):
        self.widget = widget
        self.tip = None

    def show(self, x_root, y_root, text):
        self.hide()
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x_root + 18}+{y_root + 12}")
        label = tk.Label(
            self.tip, text=text, bg="#ffffe0", relief="solid",
            borderwidth=1, justify="left",
            font=("Calibri", 9), padx=8, pady=6, wraplength=420,
        )
        label.pack()

    def hide(self):
        if self.tip:
            self.tip.destroy()
            self.tip = None


# ---------------------------------------------------------------------------
# Canvas voor de samengevoegde BPMN

class BpmnCanvas(tk.Canvas):
    """Tekent alle ingelezen BPMN's als kolommen naast elkaar."""

    def __init__(self, parent, model):
        super().__init__(parent, bg="white", highlightthickness=0)
        self.model = model
        self.tooltip = Tooltip(self)
        self.item_tip = {}              # canvas_id -> tooltip-tekst
        self._render()
        self.bind("<Motion>", self._on_motion)
        self.bind("<Leave>", lambda e: self.tooltip.hide())

    def _tip_for(self, el, category):
        return (
            f"BPMN-element: <{el.evidence.get('xml_tag', el.kind)}>\n"
            f"Naam: {el.name}\n"
            f"BPMN-id: {el.id}\n"
            f"Categorie inventarisatie: {category}\n\n"
            f"Hoe geclassificeerd:\n"
            f"{el.evidence.get('reason', '(geen onderbouwing)')}"
        )

    def _draw_rect(self, x, y, w, h, fill, outline, label, tip,
                   font=("Calibri", 9), fg="black"):
        rid = self.create_rectangle(x, y, x + w, y + h,
                                    fill=fill, outline=outline, width=1)
        tid = self.create_text(x + w / 2, y + h / 2, text=label,
                               width=w - 10, font=font, fill=fg)
        self.item_tip[rid] = tip
        self.item_tip[tid] = tip

    def _draw_note(self, x, y, w, h, fill, outline, label, fg, tip):
        fold = 8
        # Note-shape met gevouwen hoek rechtsboven
        body = [x, y, x + w - fold, y, x + w, y + fold,
                x + w, y + h, x, y + h]
        pid = self.create_polygon(body, fill=fill, outline=outline, width=1)
        # Vouw-driehoek
        self.create_polygon([x + w - fold, y, x + w - fold, y + fold,
                             x + w, y + fold],
                            fill="white", outline=outline)
        tid = self.create_text(x + w / 2, y + h / 2, text=label,
                               width=w - 15, font=("Calibri", 9), fill=fg)
        self.item_tip[pid] = tip
        self.item_tip[tid] = tip

    def _draw_diamond(self, x, y, w, h, fill, outline, label, tip):
        cx, cy = x + w / 2, y + h / 2
        points = [cx, y, x + w, cy, cx, y + h, x, cy]
        pid = self.create_polygon(points, fill=fill, outline=outline, width=1)
        tid = self.create_text(cx, cy, text=label, font=("Calibri", 8))
        self.item_tip[pid] = tip
        self.item_tip[tid] = tip

    def _render(self):
        anchors = {a.lower() for a in self.model.anchor_objects()}
        x_off = 30
        col_w = 360

        for parsed in self.model.bpmns:
            # Header
            hdr = self.create_text(x_off, 15, text=parsed.process_name,
                                   font=("Calibri", 11, "bold"),
                                   anchor="nw", fill="#1F3A5F", width=col_w)
            sub = self.create_text(x_off, 35, text=parsed.source_file,
                                   font=("Calibri", 8, "italic"),
                                   anchor="nw", fill="#777", width=col_w)
            tip_hdr = (
                f"Bestand: {parsed.source_file}\n"
                f"Proces-id: {parsed.process_name}\n"
                f"Pools: {len(parsed.participants)} | "
                f"lanes: {len(parsed.lanes)} | "
                f"tasks: {len(parsed.tasks)} | "
                f"dataObjects: {len(parsed.data_objects)} | "
                f"gateways: {len(parsed.gateways)} | "
                f"events: {len(parsed.events)}"
            )
            self.item_tip[hdr] = tip_hdr
            self.item_tip[sub] = tip_hdr

            y = 65
            # Lanes (intern)
            for lane in parsed.lanes:
                self._draw_rect(x_off, y, col_w, 26,
                                "#dae8fc", "#6c8ebf",
                                f"[lane] {lane.name}",
                                self._tip_for(lane, "Actor (intern)"))
                y += 30
            # Tasks
            for task in parsed.tasks:
                self._draw_rect(x_off + 12, y, col_w - 24, 32,
                                "#fff2cc", "#d6b656",
                                task.name or task.id,
                                self._tip_for(task, "Processtap"))
                y += 36

            # Data objects (rechter sub-kolom)
            data_y = 65
            for d in parsed.data_objects:
                is_anchor = d.name.lower() in anchors
                if is_anchor:
                    fill, outline, fg = "#fa6800", "#a04000", "white"
                    label = f"[ANKER] {d.name}"
                else:
                    fill, outline, fg = "#d5e8d4", "#82b366", "black"
                    label = d.name
                self._draw_note(x_off + col_w + 20, data_y,
                                col_w - 60, 32,
                                fill, outline, label, fg,
                                self._tip_for(d, "Data-object / entiteit"
                                              + (" — ANKEROBJECT" if is_anchor else "")))
                data_y += 36

            # Gateways onder tasks
            gw_y = max(y, data_y) + 15
            for gw in parsed.gateways:
                self._draw_diamond(x_off + 12, gw_y, col_w - 24, 28,
                                   "white", "black",
                                   f"<> {gw.name or gw.short_id()}",
                                   self._tip_for(gw, "Procesattribuut (gateway)"))
                gw_y += 32

            # Annotaties (telling)
            if parsed.annotations:
                ann_text = "\n".join(
                    a.attributes.get("text", "")[:120]
                    for a in parsed.annotations[:3]
                )
                tip = (f"{len(parsed.annotations)} <bpmn:textAnnotation> "
                       f"elementen.\n\nVoorbeelden:\n{ann_text}")
                self._draw_rect(x_off + 12, max(gw_y, data_y) + 20,
                                col_w - 24, 50,
                                "#f8f8f8", "#999",
                                f"{len(parsed.annotations)} text annotation(s)",
                                tip, font=("Calibri", 8, "italic"), fg="#555")

            x_off += col_w * 2 + 30

        self.update_idletasks()
        bbox = self.bbox("all")
        if bbox:
            self.config(scrollregion=(bbox[0] - 30, bbox[1] - 30,
                                      bbox[2] + 30, bbox[3] + 30))

    def _on_motion(self, event):
        x, y = self.canvasx(event.x), self.canvasy(event.y)
        items = self.find_overlapping(x, y, x, y)
        for item in reversed(items):
            if item in self.item_tip:
                self.tooltip.show(self.winfo_rootx() + event.x,
                                  self.winfo_rooty() + event.y,
                                  self.item_tip[item])
                return
        self.tooltip.hide()


# ---------------------------------------------------------------------------
# ERD-canvas

class ErdCanvas(tk.Canvas):
    def __init__(self, parent, model):
        super().__init__(parent, bg="white", highlightthickness=0)
        self.model = model
        self.tooltip = Tooltip(self)
        self.item_tip = {}
        self._render()
        self.bind("<Motion>", self._on_motion)
        self.bind("<Leave>", lambda e: self.tooltip.hide())

    def _render(self):
        files_by_name = defaultdict(set)
        attrs_by_name = defaultdict(set)
        for parsed in self.model.bpmns:
            task_by_id = {t.id: t.name for t in parsed.tasks}
            for d in parsed.data_objects:
                if d.name and not d.name.startswith("(naamloos"):
                    files_by_name[d.name.lower()].add(parsed.source_file)
            for assoc in parsed.data_associations:
                src = assoc.attributes.get("source", "")
                tgt = assoc.attributes.get("target", "")
                data_id = src if assoc.subtype == "dataInputAssociation" else tgt
                task_id = tgt if assoc.subtype == "dataInputAssociation" else src
                obj = next((d for d in parsed.data_objects if d.id == data_id), None)
                if obj and task_id in task_by_id:
                    attrs_by_name[obj.name.lower()].add(task_by_id[task_id])

        anchors = {a.lower() for a in self.model.anchor_objects()}
        names = sorted(files_by_name.keys())

        if not names:
            self.create_text(40, 40,
                             text=("Geen <bpmn:dataObject>-elementen gevonden in de "
                                   "ingelezen BPMN's.\n\n"
                                   "Tip: voeg expliciete dataObjects toe in de "
                                   "modelleer-tool, dan vult de ERD zich automatisch."),
                             anchor="nw", font=("Calibri", 11), fill="#666",
                             width=600)
            self.config(scrollregion=(0, 0, 700, 200))
            return

        cols = 3
        bw, bh = 280, 150
        gap_x, gap_y = 50, 60
        for i, name in enumerate(names):
            row, col = i // cols, i % cols
            x = 30 + col * (bw + gap_x)
            y = 30 + row * (bh + gap_y)
            original = next(
                (d.name for p in self.model.bpmns for d in p.data_objects
                 if d.name.lower() == name),
                name,
            )
            is_anchor = name in anchors
            fill = "#fa6800" if is_anchor else "#dae8fc"
            outline = "#a04000" if is_anchor else "#6c8ebf"
            fg = "white" if is_anchor else "black"

            attrs = sorted(attrs_by_name.get(name, []))
            attr_text = "\n".join(f"- {a[:35]}" for a in attrs[:5]) \
                or "(geen taak-koppelingen)"
            label = ("[ANKER] " if is_anchor else "") + original

            tip = (
                f"Entiteit: {original}\n"
                f"Voorkomt in: {len(files_by_name[name])} BPMN(s)\n"
                f"Bestanden: {', '.join(sorted(files_by_name[name]))}\n"
                f"Status: {'ANKEROBJECT' if is_anchor else 'lokaal object'}\n\n"
                f"Hoe afgeleid:\n"
                f"<bpmn:dataObject> of <bpmn:dataObjectReference> wordt "
                f"geprojecteerd op een entiteit. Attribuut-kandidaten = "
                f"taken die dit object via een data-association raken."
            )

            rid = self.create_rectangle(x, y, x + bw, y + bh,
                                        fill=fill, outline=outline, width=2)
            tid_h = self.create_text(x + bw / 2, y + 18, text=label,
                                     font=("Calibri", 11, "bold"), fill=fg,
                                     width=bw - 10)
            sep = self.create_line(x + 5, y + 36, x + bw - 5, y + 36,
                                   fill=outline)
            tid_b = self.create_text(x + 12, y + 44, text=attr_text,
                                     anchor="nw", font=("Calibri", 9),
                                     fill=fg, width=bw - 24)
            for item in (rid, tid_h, sep, tid_b):
                self.item_tip[item] = tip

        self.update_idletasks()
        bbox = self.bbox("all")
        if bbox:
            self.config(scrollregion=(bbox[0] - 30, bbox[1] - 30,
                                      bbox[2] + 30, bbox[3] + 30))

    def _on_motion(self, event):
        x, y = self.canvasx(event.x), self.canvasy(event.y)
        items = self.find_overlapping(x, y, x, y)
        for item in reversed(items):
            if item in self.item_tip:
                self.tooltip.show(self.winfo_rootx() + event.x,
                                  self.winfo_rooty() + event.y,
                                  self.item_tip[item])
                return
        self.tooltip.hide()


# ---------------------------------------------------------------------------
# Helper: scrollbare canvas-frame

def make_scrollable(parent, canvas_class, model):
    frame = ttk.Frame(parent)
    canvas = canvas_class(frame, model)
    hbar = ttk.Scrollbar(frame, orient="horizontal", command=canvas.xview)
    vbar = ttk.Scrollbar(frame, orient="vertical", command=canvas.yview)
    canvas.configure(xscrollcommand=hbar.set, yscrollcommand=vbar.set)
    canvas.grid(row=0, column=0, sticky="nsew")
    vbar.grid(row=0, column=1, sticky="ns")
    hbar.grid(row=1, column=0, sticky="ew")
    frame.grid_rowconfigure(0, weight=1)
    frame.grid_columnconfigure(0, weight=1)
    # Mouse wheel scroll
    def _wheel(e):
        canvas.yview_scroll(-1 * (e.delta // 120), "units")
    canvas.bind("<MouseWheel>", _wheel)
    return frame


# ---------------------------------------------------------------------------
# Hoofdvenster

class App:
    def __init__(self, root):
        self.root = root
        root.title("BPMN Data-Inventarisatie")
        root.geometry("1500x900")
        self.model = None
        base = Path(__file__).resolve().parent.parent
        self.data_dir = base / "data"
        self.out_dir = base / "output"
        self._build_ui()

    def _build_ui(self):
        top = ttk.Frame(self.root, padding=8)
        top.pack(fill="x")

        ttk.Label(top, text="BPMN-map:").pack(side="left")
        self.data_label = ttk.Label(top, text=str(self.data_dir),
                                    foreground="#444")
        self.data_label.pack(side="left", padx=6)
        ttk.Button(top, text="Bladeren...",
                   command=self._pick_data).pack(side="left")

        ttk.Separator(top, orient="vertical").pack(side="left", fill="y", padx=10)

        ttk.Button(top, text="Inlezen + analyseren",
                   command=self._run_analyse).pack(side="left", padx=2)
        ttk.Button(top, text="Genereer outputs (xlsx + drawio + docx)",
                   command=self._run_export).pack(side="left", padx=2)
        ttk.Button(top, text="Open output-map",
                   command=self._open_output).pack(side="left", padx=2)

        self.status = ttk.Label(
            self.root, text="Klik 'Inlezen + analyseren' om te starten.",
            anchor="w", padding=(10, 5), background="#f0f0f0",
        )
        self.status.pack(fill="x")

        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=8, pady=6)

        placeholder = ttk.Frame(self.nb, padding=40)
        ttk.Label(
            placeholder,
            text=("Selecteer hierboven een map met .bpmn-bestanden en klik "
                  "'Inlezen + analyseren'.\n\n"
                  "Standaard wordt de map 'data' naast deze applicatie gebruikt."),
            font=("Calibri", 11),
        ).pack()
        self.nb.add(placeholder, text="(nog geen analyse)")

    def _pick_data(self):
        d = filedialog.askdirectory(initialdir=str(self.data_dir),
                                    title="Selecteer map met .bpmn-bestanden")
        if d:
            self.data_dir = Path(d)
            self.data_label.config(text=str(self.data_dir))

    def _set_status(self, msg, color="#000"):
        self.status.config(text=msg, foreground=color)
        self.root.update_idletasks()

    def _run_analyse(self):
        self._set_status("Bezig met inlezen en analyseren...", "#0066cc")
        try:
            bpmns = parse_all(self.data_dir)
            if not bpmns:
                messagebox.showerror(
                    "Geen bestanden",
                    f"Geen .bpmn-bestanden gevonden in:\n{self.data_dir}",
                )
                self._set_status("Geen BPMN-bestanden gevonden.", "#cc0000")
                return
            self.model = merge(bpmns)
            self._build_tabs()
            self._set_status(
                f"OK - {len(bpmns)} BPMN's verwerkt; "
                f"{len(self.model.actors)} actoren, "
                f"{len(self.model.anchor_objects())} ankerobjecten, "
                f"{len(self.model.inventory)} inventarisatie-regels.",
                "#008800",
            )
        except Exception as e:
            messagebox.showerror("Fout bij analyseren", f"{type(e).__name__}: {e}")
            self._set_status(f"Fout: {e}", "#cc0000")

    def _build_tabs(self):
        for tab_id in self.nb.tabs():
            self.nb.forget(tab_id)

        # Overzicht
        ov = ttk.Frame(self.nb, padding=15)
        text = tk.Text(ov, wrap="word", font=("Consolas", 10), height=20)
        text.pack(fill="both", expand=True)
        text.insert("end", self._overview_text())
        text.config(state="disabled")
        self.nb.add(ov, text="Overzicht")

        # BPMN samengevoegd
        self.nb.add(make_scrollable(self.nb, BpmnCanvas, self.model),
                    text="BPMN samengevoegd")

        # ERD
        self.nb.add(make_scrollable(self.nb, ErdCanvas, self.model), text="ERD")

        # Inventarisatie tabel
        inv = ttk.Frame(self.nb, padding=8)
        cols = ("process", "step", "data_object", "attribute",
                "classification", "remarks")
        labels = ["Proces", "Stap-ID", "Dataobject", "Attribuut",
                  "Classificatie", "Opmerking"]
        widths = [180, 70, 180, 180, 130, 220]
        tv = ttk.Treeview(inv, columns=cols, show="headings", height=25)
        for c, lbl, w in zip(cols, labels, widths):
            tv.heading(c, text=lbl)
            tv.column(c, width=w, anchor="w")
        for r in self.model.inventory:
            tv.insert("", "end", values=(
                r.process[:30], r.step_id, r.data_object[:30],
                r.attribute[:30], r.classification, r.remarks[:50],
            ))
        sb = ttk.Scrollbar(inv, command=tv.yview)
        tv.config(yscrollcommand=sb.set)
        tv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.nb.add(inv, text=f"Inventarisatie ({len(self.model.inventory)})")

        # Actoren
        ac = ttk.Frame(self.nb, padding=8)
        cols = ("name", "type", "files", "reason")
        labels = ["Actor", "Type", "Voorkomt in", "Onderbouwing"]
        widths = [200, 80, 350, 350]
        tv2 = ttk.Treeview(ac, columns=cols, show="headings", height=20)
        for c, lbl, w in zip(cols, labels, widths):
            tv2.heading(c, text=lbl)
            tv2.column(c, width=w, anchor="w")
        for a in self.model.actors:
            tv2.insert("", "end", values=(
                a.name, a.subtype,
                ", ".join(a.evidence.get("appears_in", [])),
                a.evidence.get("classification_reason", ""),
            ))
        tv2.pack(fill="both", expand=True)
        self.nb.add(ac, text=f"Actoren ({len(self.model.actors)})")

        # Ankerobjecten
        an = ttk.Frame(self.nb, padding=15)
        if self.model.anchor_objects():
            tk.Label(an, text="Ankerobjecten (komen in 2+ BPMN's voor):",
                     font=("Calibri", 11, "bold")).pack(anchor="w")
            for name in self.model.anchor_objects():
                files = sorted({sf for sf, _ in
                                self.model.data_object_index[name.lower()]})
                tk.Label(an, text=f"• {name}  —  {len(files)} bestand(en): "
                                  f"{', '.join(files)}",
                         font=("Calibri", 10), anchor="w",
                         justify="left", wraplength=1200).pack(anchor="w", pady=2)
        else:
            tk.Label(
                an,
                text=("Geen ankerobjecten gevonden.\n\n"
                      "Geen enkele dataObject-naam komt in twee of meer "
                      "BPMN-bestanden voor. Dit is geen fout — het betekent "
                      "dat de BPMN's geen gedeelde data-objecten expliciet "
                      "modelleren. Aanbeveling: voeg in de modelleer-tool "
                      "expliciete <bpmn:dataObject>-elementen toe voor "
                      "gedeelde entiteiten zoals Lid, Lidmaatschap, "
                      "Contributie."),
                font=("Calibri", 11), justify="left", wraplength=1000,
            ).pack(anchor="w", pady=10)
        self.nb.add(an, text="Ankerobjecten")

    def _overview_text(self):
        m = self.model
        intern = ", ".join(a.name for a in m.actors if a.subtype == "intern") or "(geen)"
        extern = ", ".join(a.name for a in m.actors if a.subtype == "extern") or "(geen)"
        anchors = ", ".join(m.anchor_objects()) or "(geen)"
        files_block = "\n".join(
            f"  - {b.source_file}\n"
            f"      tasks={len(b.tasks)}, dataObjects={len(b.data_objects)}, "
            f"lanes={len(b.lanes)}, gateways={len(b.gateways)}, "
            f"events={len(b.events)}"
            for b in m.bpmns
        )
        return (
            f"BPMN-bestanden verwerkt: {len(m.bpmns)}\n{files_block}\n\n"
            f"Actoren totaal: {len(m.actors)}\n"
            f"  Intern (lanes):  {intern}\n"
            f"  Extern (pools):  {extern}\n\n"
            f"Ankerobjecten: {len(m.anchor_objects())}\n"
            f"  {anchors}\n\n"
            f"Inventarisatie-regels: {len(m.inventory)}\n\n"
            f"Klik 'Genereer outputs' om xlsx/drawio/docx te schrijven naar:\n"
            f"  {self.out_dir}\n"
        )

    def _run_export(self):
        if not self.model:
            messagebox.showinfo("Eerst analyseren",
                                "Klik eerst op 'Inlezen + analyseren'.")
            return
        self._set_status("Bezig met genereren xlsx + drawio + docx...", "#0066cc")
        try:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            write_xlsx(self.model, str(self.out_dir / "data-inventarisatie.xlsx"))
            write_drawio(self.model, str(self.out_dir / "bpmn-en-erd.drawio"))
            write_docx(self.model, str(self.out_dir / "rapport.docx"))
            self._set_status(f"OK - bestanden geschreven naar {self.out_dir}",
                             "#008800")
            messagebox.showinfo(
                "Klaar",
                f"Drie bestanden geschreven naar:\n{self.out_dir}\n\n"
                f"- data-inventarisatie.xlsx\n"
                f"- bpmn-en-erd.drawio\n"
                f"- rapport.docx",
            )
        except Exception as e:
            messagebox.showerror("Fout bij genereren",
                                 f"{type(e).__name__}: {e}")
            self._set_status(f"Fout: {e}", "#cc0000")

    def _open_output(self):
        if not self.out_dir.exists():
            messagebox.showinfo("Geen output",
                                f"Map bestaat nog niet:\n{self.out_dir}\n\n"
                                "Klik eerst op 'Genereer outputs'.")
            return
        if sys.platform == "win32":
            os.startfile(str(self.out_dir))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(self.out_dir)])
        else:
            subprocess.Popen(["xdg-open", str(self.out_dir)])


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
