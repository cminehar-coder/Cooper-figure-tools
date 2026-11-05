"""A made-up budget Sankey: money in, categories, line items.

Run:  python examples/sankey_example.py
Writes examples/out/sankey_example.{pdf,svg,png} and sankey_example.html
"""
from pathlib import Path

from figure_tools import Sankey

out = Path(__file__).parent / "out" / "sankey_example"

s = Sankey(profile="paper")
s.title("Where a 100 k USD grant goes")
s.unit("k USD")
s.flow("Grant", "People", 62)
s.flow("Grant", "Equipment", 21)
s.flow("Grant", "Travel", 9)
s.flow("Grant", "Overhead", 8)
s.flow("People", "Students", 40)
s.flow("People", "Postdoc", 22)
s.flow("Equipment", "Cell tester", 14)
s.flow("Equipment", "Computing", 7)
s.flow("Travel", "Conferences", 9)
s.note("Illustrative numbers, not data.")
s.save(str(out))
s.save_html(str(out) + ".html", subtitle="Illustrative numbers, not data.")
print("wrote", out)
