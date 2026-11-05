"""A made-up cost bridge: how a unit cost moves from one year to the next.

Run:  python examples/waterfall_example.py
Writes examples/out/waterfall_example.{pdf,svg,png}
"""
from pathlib import Path

from figure_tools import Waterfall

out = Path(__file__).parent / "out" / "waterfall_example"

w = Waterfall(profile="paper")
w.title("Materials explain most of the 2024 cost increase")
w.baseline("2023 unit cost", 120.0)
w.step("Materials", +9.5)
w.step("Labour", -2.1)
w.step("Energy", +1.8)
w.step("Scrap rate", -3.4)
w.step("Overhead", +1.6)
# expect= checks the walk against an independently known total
w.total("2024 unit cost", expect=127.4)
w.ylabel("Unit cost (USD per unit)")
w.note("Illustrative numbers, not data. Negative steps lower the cost.")
w.save(str(out))
print("wrote", out)
