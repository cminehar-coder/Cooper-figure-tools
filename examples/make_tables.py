"""Regenerate the example tables (CSV and Excel) in examples/tables/ and the
Mach-E tables in examples/materials/tables/ from the YAML.

Run:  python examples/make_tables.py
Needs pandas and openpyxl:  pip install "figure-tools[tables]"
"""
from pathlib import Path

import pandas as pd

from figure_tools.materialflow.tables import convert

here = Path(__file__).parent
out = here / "tables"
out.mkdir(exist_ok=True)

waterfall = pd.DataFrame(
    [
        ("2023 unit cost", 120.0, "base", None),
        ("Materials", 9.5, "step", None),
        ("Labour", -2.1, "step", None),
        ("Energy", 1.8, "step", None),
        ("Scrap rate", -3.4, "step", None),
        ("Overhead", 1.6, "step", None),
        ("2024 unit cost", 127.4, "total", "Illustrative numbers, not data. Negative steps lower the cost."),
    ],
    columns=["label", "value", "kind", "note"],
)
sankey = pd.DataFrame(
    [
        ("Grant", "People", 62, "k USD", "labour"),
        ("Grant", "Equipment", 21, "k USD", "kit"),
        ("Grant", "Travel", 9, "k USD", "other"),
        ("Grant", "Overhead", 8, "k USD", "other"),
        ("People", "Students", 40, "k USD", "labour"),
        ("People", "Postdoc", 22, "k USD", "labour"),
        ("Equipment", "Cell tester", 14, "k USD", "kit"),
        ("Equipment", "Computing", 7, "k USD", "kit"),
        ("Travel", "Conferences", 9, "k USD", "other"),
    ],
    columns=["source", "target", "value", "unit", "color_group"],
)
for name, df in (("waterfall", waterfall), ("sankey", sankey)):
    df.to_csv(out / (name + ".csv"), index=False)
    df.to_excel(out / (name + ".xlsx"), index=False)
    print("wrote", out / (name + ".csv"), out / (name + ".xlsx"))

ford = here / "materials" / "data" / "supply_chains" / "ford.yaml"
for dst in ("tables/mache.xlsx", "tables/mache_csv"):
    for f in convert(ford, here / "materials" / dst):
        print("wrote", f)
