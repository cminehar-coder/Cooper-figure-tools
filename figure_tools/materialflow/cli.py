"""Command line for the material-flow Sankey. A thin wrapper over MaterialFlow.

    figure-tools-materialflow --table mache.xlsx --vehicle "Mustang Mach-E" --out out/mache
    figure-tools-materialflow --supply-chain ford.yaml --vehicle "Mustang Mach-E" --out out/mache
    figure-tools-materialflow convert ford.yaml mache.xlsx     # YAML -> tables (or folder of CSVs)
    figure-tools-materialflow convert mache.xlsx yaml_out/     # tables -> YAML

Writes out/mache.{pdf,svg,png}, out/mache.html and the bibliography next to it.
A route YAML (`--route`) still goes to the older plotly renderer.
"""
from __future__ import annotations

import argparse
import sys


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["convert"]:
        from .tables import convert
        if len(argv) != 3:
            print("usage: figure-tools-materialflow convert SRC DST", file=sys.stderr)
            return 2
        for f in convert(argv[1], argv[2]):
            print("wrote", f)
        return 0
    if "--route" in argv:
        from .render_sankey import main as legacy
        return legacy(argv)
    p = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--table", help="Excel workbook or folder of CSVs")
    src.add_argument("--supply-chain", help="supply-chain YAML")
    p.add_argument("--vehicle", help="vehicle to draw (needed when there is a products sheet)")
    p.add_argument("--out", required=True, help="output path without extension")
    p.add_argument("--profile", choices=["paper", "deck"], default="paper")
    p.add_argument("--log-scale", action="store_true")
    p.add_argument("--no-region-split", action="store_true")
    p.add_argument("--no-static", action="store_true", help="skip PDF/SVG/PNG")
    a = p.parse_args(argv)
    from .core import MaterialFlow
    kw = dict(vehicle=a.vehicle, profile=a.profile, log_scale=a.log_scale,
              no_region_split=a.no_region_split)
    if a.table:
        m = MaterialFlow.from_table(a.table, **kw)
    else:
        if not a.vehicle:
            p.error("--supply-chain needs --vehicle")
        m = MaterialFlow.from_yaml(a.supply_chain, **kw)
    print("wrote", m.save_html(a.out + ".html"))
    if not a.no_static:
        for f in m.save(a.out):
            print("wrote", f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
