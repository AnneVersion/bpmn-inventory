"""
Render a static PNG preview of the drawio file by reading the
mxGeometry positions and drawing boxes with matplotlib. Not a real
draw.io render — just a layout sanity check / overview image.
"""

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import matplotlib.patches as patches
import matplotlib.pyplot as plt


COLOUR_MAP = {
    # match the drawio fill colours roughly
    "#dae8fc": "#dae8fc",   # actor intern
    "#f8cecc": "#f8cecc",   # actor extern
    "#fff2cc": "#fff2cc",   # task
    "#d5e8d4": "#d5e8d4",   # data object
    "#fa6800": "#fa6800",   # anchor
    "#e1d5e7": "#e1d5e7",   # data store
    "#f5f5f5": "#f5f5f5",   # event
    "#f8f8f8": "#f8f8f8",   # annotation
}


def _extract_fill(style: str) -> str:
    for part in style.split(";"):
        if part.startswith("fillColor="):
            return part.split("=", 1)[1]
    return "#ffffff"


def render_page(diagram, ax, title):
    rects = []
    for cell in diagram.iter("mxCell"):
        style = cell.get("style", "")
        if "edge=\"1\"" in str(cell.attrib) or cell.get("edge") == "1":
            continue
        geom = cell.find("mxGeometry")
        if geom is None or geom.get("x") is None:
            continue
        x = float(geom.get("x", 0))
        y = float(geom.get("y", 0))
        w = float(geom.get("width", 0))
        h = float(geom.get("height", 0))
        if w == 0 or h == 0:
            continue
        fill = _extract_fill(style)
        # Find label - either on mxCell value or parent UserObject
        label = cell.get("value", "")
        if not label:
            parent = cell.getparent() if hasattr(cell, "getparent") else None
        rects.append((x, y, w, h, fill, label))

    # Get UserObject labels too (for cells without value)
    labels_by_cell_pos: dict = {}
    for uo in diagram.iter("UserObject"):
        label = uo.get("label", "")
        for cell in uo.iter("mxCell"):
            geom = cell.find("mxGeometry")
            if geom is not None and geom.get("x") is not None:
                key = (float(geom.get("x")), float(geom.get("y")))
                labels_by_cell_pos[key] = label

    if not rects:
        return

    xs = [r[0] for r in rects] + [r[0] + r[2] for r in rects]
    ys = [r[1] for r in rects] + [r[1] + r[3] for r in rects]

    for x, y, w, h, fill, label in rects:
        # In matplotlib y is inverted vs draw.io. We'll flip.
        rect = patches.FancyBboxPatch(
            (x, -y - h), w, h,
            boxstyle="round,pad=2",
            linewidth=0.5, edgecolor="#666",
            facecolor=fill if fill.startswith("#") else "#ffffff",
            alpha=0.85,
        )
        ax.add_patch(rect)
        # label
        full_label = labels_by_cell_pos.get((x, y), label) or ""
        # strip html
        import re
        clean = re.sub(r"<[^>]+>", " ", full_label).strip()
        if clean:
            fontcolor = "white" if fill == "#fa6800" else "black"
            ax.text(x + w / 2, -y - h / 2, clean[:60],
                    ha="center", va="center",
                    fontsize=5, color=fontcolor, wrap=True)

    ax.set_xlim(min(xs) - 50, max(xs) + 50)
    ax.set_ylim(-(max(ys) + 50), -(min(ys) - 50))
    ax.set_aspect("equal")
    ax.set_title(title, fontsize=14, fontweight="bold")
    ax.axis("off")


def main():
    drawio = Path(sys.argv[1])
    out_png = Path(sys.argv[2])
    tree = ET.parse(drawio)
    diagrams = tree.getroot().findall("diagram")

    fig, axes = plt.subplots(len(diagrams), 1,
                             figsize=(20, 8 * len(diagrams)))
    if len(diagrams) == 1:
        axes = [axes]
    for ax, diag in zip(axes, diagrams):
        render_page(diag, ax, diag.get("name", ""))

    fig.suptitle("Layout-preview (statisch — open .drawio voor "
                 "interactieve versie met tooltips)",
                 fontsize=10, color="#666", y=0.99)
    plt.tight_layout()
    plt.savefig(out_png, dpi=130, bbox_inches="tight",
                facecolor="white")
    print(f"Saved preview to {out_png}")


if __name__ == "__main__":
    main()
