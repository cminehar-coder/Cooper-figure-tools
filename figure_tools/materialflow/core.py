"""MaterialFlow: supply-chain data read into the same Sankey object as the rest.

    from figure_tools import MaterialFlow

    m = MaterialFlow.from_table("mache.xlsx", vehicle="Mustang Mach-E")   # or a folder of CSVs
    m = MaterialFlow.from_yaml("ford.yaml", vehicle="Mustang Mach-E")     # or the YAML files
    m.save("out/mache")                  # PDF, SVG, PNG, same as Sankey
    m.save_html("out/mache.html")        # interactive D3, every trim, with sources

Both loaders fill one Dataset (materialflow/tables.py), so the same data gives
the same figure from YAML, Excel or CSV. The pipeline in render_sankey.py then
filters to disclosed suppliers, scales to the pack, splits by country and
attaches companies and references. The mass panel goes to Sankey.flow(); from
there layout, profiles and save() are Sankey's. save_html() writes every panel
(mass and energy/water, all trims) with hover text and reference numbers, and a
bibliography next to it.
"""
from __future__ import annotations

import re
import warnings
from pathlib import Path

from ..sankey import Sankey

_BR = re.compile(r"<br\s*/?>|\n")
_TAGS = re.compile(r"<[^>]+>")


def _plain(label: str) -> str:
    return _TAGS.sub("", _BR.sub(" ", label)).strip()


class MaterialFlow(Sankey):
    def __init__(self, profile: str = "paper", width_mm: float | None = None,
                 height_mm: float | None = None):
        super().__init__(profile=profile, width_mm=width_mm, height_mm=height_mm)
        if height_mm is None:
            # ~11 process columns and dozens of nodes: draw it wide
            self.w_mm = width_mm or self.style.width_mm * 1.8
            self.h_mm = self.w_mm * (0.55 if profile == "deck" else 0.6)
        self._right = 0.30
        self._dataset = None
        self._subtitle = ""
        self._opts = None
        self._panel_specs: list = []
        self._heading = ""
        self._note = None

    # ---------------- input adapters ----------------
    @classmethod
    def from_yaml(cls, supply_chain, vehicle: str, **kw) -> "MaterialFlow":
        """Read a supply-chain YAML for one vehicle.

        schema/materials_registry.yaml and sources/references.yaml are read
        from the project folder (the nearest parent holding schema/ and
        sources/), as in examples/materials/.
        """
        from .tables import Dataset
        return cls.from_dataset(Dataset.from_yaml(supply_chain), vehicle=vehicle,
                                source=str(Path(supply_chain).name), **kw)

    @classmethod
    def from_table(cls, source=None, vehicle: str | None = None, *, flows=None,  # type: ignore[override]
                   materials=None, references=None, nodes=None, products=None,
                   relationships=None, **kw) -> "MaterialFlow":
        """Read the tables: one Excel workbook, a folder of CSVs, or separate
        tables (DataFrames or paths) passed as flows=, materials=, references=.

        With a products table, pass vehicle= to draw one vehicle at pack level.
        Without one, the flows are drawn per kWh.
        """
        from .tables import Dataset
        ds = Dataset.from_tables(source, flows=flows, materials=materials,
                                 references=references, nodes=nodes,
                                 products=products, relationships=relationships)
        name = Path(source).name if isinstance(source, (str, Path)) else "tables"
        return cls.from_dataset(ds, vehicle=vehicle, source=name, **kw)

    @classmethod
    def from_dataset(cls, ds, vehicle: str | None = None, profile: str = "paper",
                     panel: int = 0, log_scale: bool = False,
                     no_region_split: bool = False, width_mm: float | None = None,
                     height_mm: float | None = None, source: str = "") -> "MaterialFlow":
        """`panel` picks which mass panel (trim) save() draws; the HTML has all of them."""
        from .render_sankey import Options, build_vehicle_panels, validate, route_from_raw, \
            index_materials, index_references
        self = cls(profile=profile, width_mm=width_mm, height_mm=height_mm)
        opts = Options(route_path=None, out_path=Path("."), log_scale=log_scale,
                       no_region_split=no_region_split, vehicle=vehicle,
                       supply_chain_path=Path(source) if source else None,
                       renderer="d3", dataset=ds)
        problems = []
        for key, raw in ds.routes.items():
            problems += ["route %s: %s" % (key, w) for w in
                         validate(route_from_raw(raw), index_materials(ds.materials),
                                  index_references(ds.references))]
        if problems:
            warnings.warn("%d data warning(s), e.g. %s" % (len(problems), problems[0]),
                          stacklevel=3)
        if vehicle:
            products, specs = build_vehicle_panels(opts)
            heading = "%s - %s" % (products[0].get("company"), vehicle)
            note = ("Per-pack absolute mass; regions filtered to disclosed suppliers.")
        else:
            if ds.supply_chain.get("products"):
                raise ValueError("the tables have a products sheet; pass vehicle= "
                                 "(one of: %s)" % ", ".join(
                                     str(p.get("product")) for p in ds.supply_chain["products"]))
            specs, heading = _route_panels(ds, opts)
            note = "Per kWh of cell capacity."
        self._dataset, self._opts, self._panel_specs = ds, opts, specs
        self._heading, self._subtitle = heading, note
        mass = [s for s in specs if "Mass" in s[0] or "mass" in s[0]]
        if not mass:
            raise ValueError("no mass flows to draw%s" % (" for %r" % vehicle if vehicle else ""))
        title, trace, _ = mass[panel]
        self._load_trace(trace)
        self.title(_plain(title).replace("—", "-"))
        self.unit(trace.unit)
        return self

    def _load_trace(self, trace) -> None:
        from .render_sankey import _country_name, _process_rank
        names, seen = [], set()
        for nid, lab in zip(trace.nodes, trace.node_labels):
            lines = [_plain(x) for x in re.split(r"<br\s*/?>", lab)]
            code = nid.rsplit("_", 1)[-1]
            country = _country_name(code) if code.isupper() and len(code) == 2 else None
            # static labels stop at the country; company lists stay in the HTML
            keep = lines[:lines.index(country) + 1] if country in lines else lines[:1]
            name = ", ".join(keep) or nid
            if name in seen:
                name = "%s [%s]" % (name, nid)
            seen.add(name); names.append(name)
            self.column(name, _process_rank(nid))
        for i, (s, t) in enumerate(zip(trace.sources, trace.targets)):
            v = float(trace.raw_values[i]) if i < len(trace.raw_values) else float(trace.values[i])
            if v > 0 and s != t:
                self.flow(names[s], names[t], v)
        for i, c in enumerate(trace.node_colors or []):
            if c.startswith("#") and len(c) == 7:
                self.color(names[i], c)

    # ---------------- outputs ----------------
    def save_html(self, path: str, subtitle: str = "", height: int | None = None,
                  width: int | None = None) -> str:
        """Interactive D3 page with every panel, plus <stem>_bibliography.md/.csv."""
        from .d3_renderer import render_html, trace_to_panel
        from .render_sankey import _process_rank, index_references, write_bibliography
        columns = {0: "Mining", 1: "Beneficiation", 2: "Chemical Processing",
                   3: "Precursor Mfg", 4: "CAM / Anode Mfg", 5: "Electrode Mfg",
                   6: "Cell Mfg", 7: "Module Assembly", 8: "Pack Mfg",
                   9: "Application", 10: "EoL / Waste"}
        sub = subtitle or self._subtitle
        panels = [trace_to_panel(t, title=ttl, height=h, process_rank=_process_rank,
                                 subtitle=sub)
                  for ttl, t, h in self._panel_specs]
        page = render_html(self._heading, sub, panels, columns)
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(page, encoding="utf-8")
        assert self._dataset is not None
        write_bibliography(p, index_references(self._dataset.references))
        return str(p)


def _route_panels(ds, opts):
    """Panels for tables without a products sheet: every route, per kWh."""
    from .render_sankey import (_classify_traces, _filter_flows, _reset_ref_numbers,
                                build_trace, index_materials, index_references,
                                route_from_raw)
    _reset_ref_numbers()
    materials, refs = index_materials(ds.materials), index_references(ds.references)
    specs, heading = [], ""
    for key, raw in ds.routes.items():
        route = route_from_raw(raw)
        meta = route.meta
        label = " ".join(str(x) for x in (meta.get("chemistry"), meta.get("route")) if x) or key
        heading = heading or label
        mass, ew = _classify_traces(_filter_flows(route.flows, opts, materials))
        for flows, name, unit, h in ((mass, "Mass", "kg/kWh", 1100),
                                     (ew, "Energy and water", "MJ or L /kWh", 220)):
            if flows:
                tr = build_trace(flows, materials, refs, label="%s - %s" % (label, name),
                                 unit_label=unit, log_scale=opts.log_scale,
                                 no_region_split=opts.no_region_split)
                specs.append(("%s - %s (%s)" % (label, name, unit), tr, h))
    return specs, heading
