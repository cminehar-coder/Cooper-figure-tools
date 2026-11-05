# figure-tools

Waterfall and Sankey figures from plain Python data. You give it numbers; it
handles layout, colour, type and export (PDF, SVG, PNG, and an interactive HTML
Sankey). Two styles: `paper` (hairlines, 8 pt type floor, colour-blind-safe
palette) and `deck` (big type, heavy strokes, for slides).

![waterfall](docs/img/waterfall.png)
![sankey](docs/img/sankey.png)

## 60-second quickstart

Python 3.10 or newer.

```bash
git clone https://github.com/cminehar-coder/figure-tools.git
cd figure-tools
uv venv && uv pip install -e .          # or: python -m venv .venv && .venv/bin/pip install -e .
python examples/waterfall_example.py     # writes examples/out/waterfall_example.{pdf,svg,png}
python examples/sankey_example.py        # writes examples/out/sankey_example.{pdf,svg,png,html}
```

Waterfall:

```python
from figure_tools import Waterfall

w = Waterfall(profile="paper")            # or "deck"
w.title("Materials explain most of the 2024 cost increase")
w.baseline("2023 unit cost", 120.0)
w.step("Materials", +9.5)
w.step("Labour", -2.1)
w.total("2024 unit cost", expect=127.4)   # errors if the steps do not add up to 127.4
w.ylabel("Unit cost (USD per unit)")
w.save("out/bridge")                      # out/bridge.pdf, .svg, .png
```

Sankey:

```python
from figure_tools import Sankey

s = Sankey(profile="paper")
s.title("Where a 100 k USD grant goes")
s.unit("k USD")
s.flow("Grant", "People", 62)
s.flow("Grant", "Equipment", 21)
s.flow("People", "Students", 40)
s.flow("People", "Postdoc", 22)
s.save("out/grant")                       # PDF, SVG, PNG
s.save_html("out/grant.html")             # interactive, hover for values
```

Columns come from the longest path through your flows. Cycles and non-positive
flows raise an error.

## What it checks for you

- **Waterfall totals.** `total(expect=...)` raises if the steps do not walk to the
  total you measured independently, so a dropped step cannot slip through.
- **Printed numbers add up.** Decimals are chosen so the labels on the figure sum
  to the printed total and the smallest step is still readable.
- **Dollar signs.** matplotlib treats `$...$` as maths and silently italicises the
  text in between. A bare `$` in a title or label raises an error; write `\$`.
- **Fonts.** PDFs embed real TrueType text (not Type 3) and SVG keeps live text,
  so a copy-editor can fix a typo without redrawing. A missing glyph is an error.

## Material-flow Sankey (interactive, with bibliography)

`figure_tools.materialflow` is a more specialised renderer. It reads a
supply-chain YAML, a materials registry and a reference library, then writes an
interactive D3 Sankey of mass flows by process step and country, plus a
bibliography (`.md` and `.csv`) of every source cited. The shipped example is the
Ford Mustang Mach-E battery supply chain, built from public sources only.

```bash
python examples/materials/run_mache.py
# or: figure-tools-materialflow --vehicle "Mustang Mach-E" \
#       --supply-chain examples/materials/data/supply_chains/ford.yaml --renderer d3 --out out.html
```

![Mach-E supply chain](docs/img/mache_d3.png)

The input format is documented in `examples/materials/schema/schema_definition.yaml`.
The project folder must contain `schema/` and `sources/` next to the data
(see `examples/materials/`). The HTML loads d3 from a CDN, so opening it needs a
network connection.

## Limits

- The data in `examples/` is illustrative or, for Mach-E, taken from the public
  sources listed in its bibliography. Numbers are estimates; check them against
  the cited sources before reuse.
- Fonts are not bundled. The styles ask for Arial, Helvetica or Liberation Sans
  and fall back to DejaVu Sans, which ships with matplotlib (free licence).
- The static Sankey puts labels over the flows with a white halo; very dense
  graphs (dozens of nodes per column) are better served by the HTML version.

## Tests

```bash
uv venv && uv pip install -e ".[test]" && .venv/bin/pytest
```

Every example is rendered from scratch and its outputs are checked.

## Cite

GitHub's "Cite this repository" button reads `CITATION.cff`. Once a release is
archived on Zenodo, cite the DOI it is given.

## Licence

MIT, copyright Cooper Minehart. See `LICENSE`.
