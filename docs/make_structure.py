"""The material-flow tables and how they link by ID (docs/img/materialflow_structure.svg/.png).

Every value is read from examples/materials/tables/mache.xlsx.
Run:  python docs/make_structure.py
"""
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, Rectangle

from figure_tools.materialflow.render_sankey import index_materials, index_references
from figure_tools.materialflow.tables import Dataset

ROOT = Path(__file__).resolve().parent.parent
RH = 2.2          # row height
RED, INK, GRID = "#C0392B", "#1b1b1b", "#c5ccd3"


class Box:
    """A small table: dark title bar, header row, data rows. Top-left at (x, y)."""

    def __init__(self, ax, x, y, title, cols, rows, widths, keys=()):
        self.x, self.top, self.w = x, y, sum(widths)
        self.bottom = y - RH * (len(rows) + 2)
        self.colx = {}
        ax.add_patch(Rectangle((x, y - RH), self.w, RH, facecolor="#33404d", edgecolor="#33404d"))
        ax.text(x + 0.6, y - RH / 2, title, color="white", fontsize=8.6, fontweight="bold",
                va="center")
        ax.add_patch(Rectangle((x, y - 2 * RH), self.w, RH, facecolor="#E8EEF4", edgecolor=GRID, lw=0.6))
        cx = x
        for c, w in zip(cols, widths):
            ax.text(cx + 0.6, y - 1.5 * RH, c, fontsize=7.6, fontweight="bold", va="center",
                    color=RED if c in keys else INK)
            self.colx[c] = cx + min(w, 12) / 2
            cx += w
        for i, r in enumerate(rows):
            ry = y - RH * (i + 3)
            ax.add_patch(Rectangle((x, ry), self.w, RH, facecolor="white", edgecolor=GRID, lw=0.6))
            cx = x
            for v, w in zip(r, widths):
                s = "" if v is None else str(v)
                lim = int(w * 1.7)
                ax.text(cx + 0.6, ry + RH / 2, s if len(s) <= lim else s[:lim - 1] + "…",
                        fontsize=7.4, va="center", color=INK)
                cx += w


def structure(out_stem: Path):
    plt.rcParams["svg.fonttype"] = "none"
    ds = Dataset.from_tables(ROOT / "examples" / "materials" / "tables" / "mache.xlsx")
    mats, refs = index_materials(ds.materials), index_references(ds.references)
    sh = ds.to_sheets()
    f1, f2 = [f for f in sh["flows"]
              if f.get("material_id") in mats and f.get("source_id")
              and all(s.strip() in refs for s in f["source_id"].split(";"))
              and str(f["route"]).startswith("data/lfp")][:2]
    nodes = {n["id"]: n for n in sh["nodes"] if n["route"] == f1["route"]}
    prod = sh["products"][0]
    rel = next(r for r in sh["relationships"]
               if r.get("source_id") and all(s.strip() in refs for s in r["source_id"].split(";")))

    fig = plt.figure(figsize=(13, 7.2), dpi=170)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 130)
    ax.set_ylim(0, 72)
    ax.axis("off")

    P = Box(ax, 2, 69, "products   what you draw", ["id", "product", "pack_kwh", "route"],
            [[prod["id"], prod["product"], prod["pack_kwh"], prod["route"]]],
            [14, 31, 10, 22], keys={"route"})
    fcols = ["route", "id", "source", "target", "material_id", "value_per_kwh", "unit", "source_id"]
    F = Box(ax, 2, 54, "flows   one row per flow", fcols,
            [[f.get(c) for c in fcols] for f in (f1, f2)],
            [16, 5, 11, 13, 15, 12, 9, 45], keys={"route", "source", "target", "material_id", "source_id"})
    N = Box(ax, 2, 35, "nodes   process steps", ["route", "id", "label"],
            [[nodes[i]["route"], i, nodes[i].get("label")]
             for i in dict.fromkeys([f1["source"], f1["target"]]) if i in nodes],
            [15, 13, 20], keys={"route", "id"})
    m = mats[f1["material_id"]]
    Mt = Box(ax, 50, 35, "materials   registry", ["id", "name"],
             [[m["id"], m.get("name")]], [15, 20], keys={"id"})
    ids = [s.strip() for s in f1["source_id"].split(";")][:2]
    R = Box(ax, 68, 16, "references   library", ["id", "type", "title", "year"],
            [[i, refs[i].get("type"), refs[i].get("title"), refs[i].get("year")] for i in ids],
            [19, 8, 22, 5], keys={"id"})
    Q = Box(ax, 2, 16, "relationships   who supplies whom", ["id", "tier", "source_id"],
            [[rel["id"], rel.get("tier"), rel["source_id"]]], [13, 5, 26], keys={"source_id"})

    def arrow(p, q, rad=0.0):
        ax.add_patch(FancyArrowPatch(p, q, arrowstyle="-|>", mutation_scale=9, lw=1.2, color=RED,
                                     connectionstyle="arc3,rad=%g" % rad, shrinkA=0, shrinkB=1))

    def label(x, y, s, ha="left"):
        ax.text(x, y, s, fontsize=7.2, color=RED, ha=ha, va="center",
                bbox=dict(facecolor="white", edgecolor="none", pad=0.6))

    out = F.bottom
    arrow((F.colx["route"], P.bottom), (F.colx["route"], F.top))
    label(F.colx["route"] + 1.5, (P.bottom + F.top) / 2, "products.route = flows.route")
    arrow((F.colx["source"], out), (N.colx["id"] - 1, N.top))
    arrow((F.colx["target"], out), (N.colx["id"] + 4, N.top))
    label(14, (out + N.top) / 2, "flows.source, flows.target = nodes.id")
    arrow((F.colx["material_id"], out), (Mt.colx["id"], Mt.top))
    label(Mt.colx["id"] + 2, (out + Mt.top) / 2 + 2.6, "flows.material_id = materials.id")
    arrow((F.colx["source_id"] - 20, out), (R.colx["id"] + 4, R.top))
    label(R.colx["id"] + 6, (out + R.top) / 2 - 1, "flows.source_id = references.id")
    arrow((Q.x + Q.w, Q.top - RH * 2.5), (R.x, R.top - RH * 2.5))
    label((Q.x + Q.w + R.x) / 2, Q.top - RH * 2.5 + 1.6, "relationships.source_id = references.id",
          ha="center")
    ax.text(65, 70.8, "Each arrow is a lookup by ID. Red column names are the keys.",
            fontsize=8.6, ha="center", color="#555")
    out_stem = Path(out_stem)
    for ext in ("svg", "png"):
        fig.savefig(str(out_stem) + "." + ext, dpi=170)
        print("wrote", str(out_stem) + "." + ext)
    plt.close(fig)


if __name__ == "__main__":
    structure(ROOT / "docs" / "img" / "materialflow_structure")
