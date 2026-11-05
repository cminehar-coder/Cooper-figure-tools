"""The three-line route: a table in, figures out.

Run:  python examples/table_examples.py
Writes examples/out/waterfall_table.{pdf,svg,png} and sankey_table.{pdf,svg,png,html}.
The tables are examples/tables/*.csv and *.xlsx (same numbers).
"""
from pathlib import Path

from figure_tools import Sankey, Waterfall

here = Path(__file__).parent
out = here / "out"

Waterfall.from_table(here / "tables" / "waterfall.csv", profile="paper",
                     title="Materials explain most of the 2024 cost increase",
                     ylabel="Unit cost (USD per unit)").save(str(out / "waterfall_table"))

s = Sankey.from_table(here / "tables" / "sankey.xlsx", title="Where a 100 k USD grant goes")
s.save(str(out / "sankey_table"))
s.save_html(str(out / "sankey_table.html"))
print("wrote", out)
