"""Render the Ford Mustang Mach-E battery supply chain as a D3 Sankey.

This is one example of the material-flow renderer (figure_tools.materialflow),
which reads a supply-chain YAML plus a materials registry and a reference
library, and writes an interactive HTML Sankey with a bibliography.

Run:  python examples/materials/run_mache.py
Writes examples/materials/out/Mustang_Mach_E_d3.html (+ bibliography .md/.csv)
Needs a network connection to open the HTML (d3 is loaded from a CDN).
"""
import sys
from pathlib import Path

from figure_tools.materialflow.render_sankey import main

here = Path(__file__).parent
sys.exit(main([
    "--vehicle", "Mustang Mach-E",
    "--supply-chain", str(here / "data" / "supply_chains" / "ford.yaml"),
    "--renderer", "d3",
    "--out", str(here / "out" / "Mustang_Mach_E_d3.html"),
]))
