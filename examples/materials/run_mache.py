"""Render the Ford Mustang Mach-E battery supply chain from its tables.

Run:  python examples/materials/run_mache.py
Writes examples/materials/out/Mustang_Mach_E.{pdf,svg,png}, Mustang_Mach_E_d3.html
and the bibliography (.md, .csv) next to the HTML.
The HTML needs a network connection to open (d3 loads from a CDN).

The tables came from the YAML in data/, schema/ and sources/ (see
examples/make_tables.py). Either input gives the same figure:

    MaterialFlow.from_table("tables/mache.xlsx", vehicle=...)   # or tables/mache_csv/
    MaterialFlow.from_yaml("data/supply_chains/ford.yaml", vehicle=...)
"""
from pathlib import Path

from figure_tools import MaterialFlow

here = Path(__file__).parent
out = here / "out"

m = MaterialFlow.from_table(here / "tables" / "mache.xlsx", vehicle="Mustang Mach-E")
m.save(str(out / "Mustang_Mach_E"))
m.save_html(str(out / "Mustang_Mach_E_d3.html"))
print("wrote", out)
