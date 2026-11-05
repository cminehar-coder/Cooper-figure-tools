"""Build the README images in docs/img/ from the example data.

    python docs/make_images.py

Needs pandas, openpyxl, matplotlib and LibreOffice (soffice) + pdftoppm for the
spreadsheet screenshots. Every value in the pictures is read from the example
tables, nothing is typed in by hand.
"""
import subprocess
import tempfile
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from PIL import Image, ImageChops

from figure_tools.materialflow.tables import Dataset

ROOT = Path(__file__).resolve().parent.parent
IMG = ROOT / "docs" / "img"
EX = ROOT / "examples"
IMG.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------- spreadsheet screenshots
def style_sheet(ws, widths):
    thin = Side(style="thin", color="BBBBBB")
    for c in ws[1]:
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="E8EEF4")
    for row in ws.iter_rows():
        for c in row:
            c.border = Border(left=thin, right=thin, top=thin, bottom=thin)
            c.alignment = Alignment(vertical="center")
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[ws.cell(1, i).column_letter].width = w
    ws.page_setup.orientation = "landscape"
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.fitToHeight = 0


def screenshot(xlsx: Path, out: Path, dpi=170):
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", d, str(xlsx)],
                       check=True, capture_output=True)
        pdf = Path(d) / (xlsx.stem + ".pdf")
        subprocess.run(["pdftoppm", "-png", "-r", str(dpi), "-f", "1", "-l", "1", str(pdf),
                        str(Path(d) / "p")], check=True)
        png = next(Path(d).glob("p*.png"))
        im = Image.open(png).convert("RGB")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        box = ImageChops.difference(im, bg).getbbox()
        pad = 14
        box = (max(box[0] - pad, 0), max(box[1] - pad, 0),
               min(box[2] + pad, im.width), min(box[3] + pad, im.height))
        im.crop(box).save(out)
    print("wrote", out)


def sheet_png(rows, header, widths, out, sheet="Sheet1"):
    with tempfile.TemporaryDirectory() as d:
        wb = Workbook()
        ws = wb.active
        ws.title = sheet
        ws.append(header)
        for r in rows:
            ws.append(list(r))
        style_sheet(ws, widths)
        p = Path(d) / "shot.xlsx"
        wb.save(p)
        screenshot(p, out)


def from_xlsx(name, widths, out, nrows=None):
    ws = load_workbook(EX / "tables" / name).active
    vals = [[c.value for c in r] for r in ws.iter_rows()]
    if nrows:
        vals = vals[:nrows]
    sheet_png(vals[1:], vals[0], widths, out)


def mache_flow_sheet(out):
    ds = Dataset.from_tables(EX / "materials" / "tables" / "mache.xlsx")
    flows = ds.to_sheets()["flows"]
    cols = ["route", "id", "source", "target", "material", "value_per_kwh", "unit", "low", "high",
            "source_id", "verified"]
    rows = [[f.get(c) for c in cols] for f in flows[:6]]
    sheet_png(rows, cols, [25, 6, 14, 16, 20, 15, 14, 6, 6, 52, 9], out)


if __name__ == "__main__":
    from_xlsx("waterfall.xlsx", [18, 8, 8, 56], IMG / "table_waterfall.png")
    from_xlsx("sankey.xlsx", [12, 12, 8, 8, 13], IMG / "table_sankey.png")
    mache_flow_sheet(IMG / "table_materialflow_flows.png")
    from make_structure import structure
    structure(IMG / "materialflow_structure")
