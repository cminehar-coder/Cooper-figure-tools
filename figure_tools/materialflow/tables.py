"""The material-flow data as tables, and the converter to and from YAML.

A Dataset holds what the renderer needs: the supply chain (products and
supply relationships), one or more routes (nodes and flows per kWh), the
materials registry and the reference library. It can be read from the YAML
files, from one Excel workbook, or from a folder of CSVs, and written back to
any of them. The renderer only ever sees a Dataset, so all three inputs
draw the same figure.

Sheets (one Excel sheet or one CSV per name):

    flows          one row per flow: route, id, source, target, material,
                   material_id, value_per_kwh, unit, low, high, source_id,
                   verified, is_loss, is_recycled, companies, notes, ...
    nodes          route, id, label, description
    route_meta     route, key, value
    materials      id, name, formula, category, ...        (the registry)
    references     id, type, title, author, year, doi, url, pages, ...
    products       id, company, product, pack_chemistry, pack_kwh, route, ...
    relationships  id, tier, material, confidence, source_id, ...
    supply_meta    key, value         (notes, gaps and checks from the file)

Only flows is required. Without products the table is one route and the
figure is per kWh. Fields that are lists or mappings in the YAML (region
breakdowns, cell makers, upstream and downstream) sit in one cell as JSON,
marked with a leading "json:".
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from ..table import coerce, read_sheets, require

_SKIP = object()
# YAML flow key -> table column
_FLOW_COLS = {"from": "source", "to": "target", "value": "value_per_kwh"}
_ROUTE_DEFAULT = "route"
_KNOWN = {"route", "id", "source", "target", "material", "material_id", "value_per_kwh",
          "unit", "low", "high", "source_id", "verified", "is_loss", "is_recycled",
          "companies", "notes", "label", "description", "key", "value", "pack_kwh"}


def _lower_known(row: dict) -> dict:
    """Match the documented column names without regard to case or spaces;
    leave any other column as the YAML key it came from."""
    out = {}
    for k, v in row.items():
        n = str(k).strip().lower().replace(" ", "_")
        out[n if n in _KNOWN else str(k).strip()] = v
    return out


def _enc(v):
    """A YAML value to a cell value."""
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return "json:" + json.dumps(v, ensure_ascii=False, default=str)
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


def _dec(v):
    return coerce(v)


def _rows_from_dicts(items: list[dict], lead: dict | None = None) -> list[dict]:
    rows = []
    for it in items:
        row = dict(lead or {})
        for k, v in it.items():
            row[str(k)] = _enc(v)
        rows.append(row)
    return rows


def _dicts_from_rows(rows: list[dict], drop=()) -> list[dict]:
    out = []
    for r in rows:
        out.append({k: v for k, v in r.items() if v is not None and k not in drop})
    return out


def _flow_to_row(route: str, f: dict) -> dict:
    row: dict[str, Any] = {"route": route}
    unc = f.get("uncertainty") or {}
    for k, v in f.items():
        if k == "uncertainty":
            continue
        if k == "source_ids":
            row["source_id"] = "; ".join(str(x) for x in (v or []))
        elif k == "companies" and isinstance(v, list) and all(isinstance(x, str) for x in v):
            row["companies"] = "; ".join(v)
        else:
            row[_FLOW_COLS.get(k, k)] = _enc(v)
    if unc:
        row["low"] = unc.get("low")
        row["high"] = unc.get("high")
        if unc.get("method") is not None:
            row["uncertainty_method"] = unc.get("method")
        for k, v in unc.items():
            if k not in ("low", "high", "method"):
                row["uncertainty_" + k] = _enc(v)
    return row


def _split(v) -> list[str]:
    if v is None:
        return []
    return [x.strip() for x in str(v).split(";") if x.strip()]


def _row_to_flow(r: dict) -> dict:
    f: dict[str, Any] = {}
    unc: dict[str, Any] = {}
    inv = {v: k for k, v in _FLOW_COLS.items()}
    for k, v in r.items():
        if v is None or k == "route":
            continue
        if k == "source_id":
            f["source_ids"] = _split(v)
        elif k == "companies" and isinstance(v, str):
            f["companies"] = _split(v)
        elif k in ("low", "high"):
            unc[k] = v
        elif k.startswith("uncertainty_"):
            unc[k[len("uncertainty_"):]] = v
        else:
            f[inv.get(k, k)] = v
    if unc:
        f["uncertainty"] = unc
    if "id" in f:
        f["id"] = str(f["id"])
    for k in ("from", "to"):
        if k in f:
            f[k] = str(f[k])
    return f


class Dataset:
    def __init__(self, supply_chain: dict | None = None, routes: dict | None = None,
                 materials: dict | None = None, references: dict | None = None):
        self.supply_chain = supply_chain or {}
        self.routes = routes or {}
        self.materials = materials or {"materials": []}
        self.references = references or {"sources": []}

    # ---------------- YAML ----------------
    @classmethod
    def from_yaml(cls, supply_chain) -> "Dataset":
        """Read a supply-chain YAML and the files it points at.

        Route files (products[].route_file) are resolved from the project
        folder, the nearest parent that holds schema/ and sources/.
        """
        from .render_sankey import _project_root_from_route, load_yaml
        sc = Path(supply_chain)
        root = _project_root_from_route(sc)
        data = load_yaml(sc)
        routes = {}
        for p in data.get("products") or []:
            rf = p.get("route_file")
            if rf and rf not in routes:
                routes[rf] = load_yaml((root / rf).resolve())
        return cls(data, routes,
                   load_yaml(root / "schema" / "materials_registry.yaml"),
                   load_yaml(root / "sources" / "references.yaml"))

    @classmethod
    def from_route_yaml(cls, route, materials=None, references=None) -> "Dataset":
        from .render_sankey import _project_root_from_route, load_yaml
        rp = Path(route)
        root = _project_root_from_route(rp)
        return cls({}, {_ROUTE_DEFAULT: load_yaml(rp)},
                   load_yaml(Path(materials) if materials else root / "schema" / "materials_registry.yaml"),
                   load_yaml(Path(references) if references else root / "sources" / "references.yaml"))

    def to_yaml(self, folder) -> list[str]:
        """Write the YAML layout of examples/materials/ under `folder`."""
        import yaml
        root = Path(folder)
        out = []

        def dump(path: Path, data):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True,
                                           width=100), encoding="utf-8")
            out.append(str(path))
        if self.supply_chain:
            dump(root / "data" / "supply_chains" / "supply_chain.yaml", self.supply_chain)
        for key, raw in self.routes.items():
            dump(root / (key if key.endswith(".yaml") else "data/%s.yaml" % key), raw)
        dump(root / "schema" / "materials_registry.yaml", self.materials)
        dump(root / "sources" / "references.yaml", self.references)
        return out

    # ---------------- tables ----------------
    def to_sheets(self) -> dict[str, list[dict]]:
        s: dict[str, list[dict]] = {}
        s["flows"] = [_flow_to_row(k, f) for k, r in self.routes.items()
                      for f in (r.get("flows") or [])]
        s["nodes"] = [row for k, r in self.routes.items()
                      for row in _rows_from_dicts(r.get("nodes") or [], {"route": k})]
        s["route_meta"] = [{"route": k, "key": mk, "value": _enc(mv)}
                           for k, r in self.routes.items()
                           for mk, mv in (r.get("meta") or {}).items()]
        for k, r in self.routes.items():
            extra = set(r) - {"meta", "nodes", "flows"}
            if extra:
                s.setdefault("route_meta", []).extend(
                    {"route": k, "key": "@" + e, "value": _enc(r[e])} for e in sorted(extra))
        s["materials"] = _rows_from_dicts(self.materials.get("materials") or [])
        s["references"] = _rows_from_dicts(self.references.get("sources") or [])
        extra_m = {k: v for k, v in self.materials.items() if k != "materials"}
        extra_r = {k: v for k, v in self.references.items() if k != "sources"}
        sc = self.supply_chain
        if sc:
            prods = []
            for p in sc.get("products") or []:
                row = _rows_from_dicts([p])[0]
                if "route_file" in row:
                    row["route"] = row.pop("route_file")
                if "pack_kWh" in row:
                    row["pack_kwh"] = row.pop("pack_kWh")
                prods.append(row)
            s["products"] = prods
            rels = []
            for r in sc.get("supply_relationships") or []:
                row = _rows_from_dicts([r])[0]
                if isinstance(r.get("source_ids"), list):
                    row["source_id"] = "; ".join(str(x) for x in r["source_ids"])
                    row.pop("source_ids", None)
                rels.append(row)
            s["relationships"] = rels
            meta_rows = [{"key": k, "value": _enc(v)} for k, v in sc.items()
                         if k not in ("products", "supply_relationships")]
            s["supply_meta"] = meta_rows
        extras = [{"key": "@materials." + k, "value": _enc(v)} for k, v in extra_m.items()]
        extras += [{"key": "@references." + k, "value": _enc(v)} for k, v in extra_r.items()]
        if extras:
            s["supply_meta"] = s.get("supply_meta", []) + extras
        return {k: v for k, v in s.items() if v}

    @classmethod
    def from_sheets(cls, sheets: dict[str, list[dict]]) -> "Dataset":
        sheets = {k.strip().lower(): [_lower_known(r) for r in v] for k, v in sheets.items()}
        flows = sheets.get("flows")
        if not flows:
            raise ValueError("no flows: the workbook needs a sheet (or CSV) called flows; "
                             "it has: %s" % (", ".join(sorted(sheets)) or "nothing"))
        require(flows, ["source", "target", "value_per_kwh"], "flows")
        routes: dict[str, dict] = {}
        for r in flows:
            key = str(r.get("route") or _ROUTE_DEFAULT)
            routes.setdefault(key, {"flows": []})["flows"].append(_row_to_flow(r))
        for r in sheets.get("nodes") or []:
            key = str(r.get("route") or _ROUTE_DEFAULT)
            if key in routes:
                routes[key].setdefault("nodes", []).append(
                    {k: v for k, v in r.items() if v is not None and k != "route"})
        for r in sheets.get("route_meta") or []:
            key = str(r.get("route") or _ROUTE_DEFAULT)
            if key not in routes or r.get("key") is None:
                continue
            k = str(r["key"])
            if k.startswith("@"):
                routes[key][k[1:]] = r.get("value")
            else:
                routes[key].setdefault("meta", {})[k] = r.get("value")
        for key, raw in routes.items():
            if "nodes" not in raw:       # no nodes sheet: make one from the flows
                ids = list(dict.fromkeys(str(x) for f in raw["flows"]
                                         for x in (f.get("from"), f.get("to")) if x))
                raw["nodes"] = [{"id": i, "label": i} for i in ids]
            raw.setdefault("meta", {})
        mats = {"materials": _dicts_from_rows(sheets.get("materials") or [])}
        refs = {"sources": _dicts_from_rows(sheets.get("references") or [])}
        sc: dict[str, Any] = {}
        if sheets.get("products"):
            prods = []
            for r in sheets["products"]:
                p = {k: v for k, v in r.items() if v is not None}
                if "route" in p:
                    p["route_file"] = p.pop("route")
                if "pack_kwh" in p:
                    p["pack_kWh"] = p.pop("pack_kwh")
                prods.append(p)
            sc["products"] = prods
            rels = []
            for r in sheets.get("relationships") or []:
                p = {k: v for k, v in r.items() if v is not None}
                if "source_id" in p:
                    p["source_ids"] = _split(p.pop("source_id"))
                rels.append(p)
            sc["supply_relationships"] = rels
        for r in sheets.get("supply_meta") or []:
            k = str(r.get("key") or "")
            if k.startswith("@materials."):
                mats[k[len("@materials."):]] = r.get("value")
            elif k.startswith("@references."):
                refs[k[len("@references."):]] = r.get("value")
            elif k:
                sc[k] = r.get("value")
        return cls(sc, routes, mats, refs)

    @classmethod
    def from_tables(cls, source=None, *, flows=None, materials=None, references=None,
                    nodes=None, products=None, relationships=None) -> "Dataset":
        """From a workbook path, a folder of CSVs, or separate tables
        (each a DataFrame or a CSV or Excel path)."""
        from ..table import read_rows
        sheets: dict[str, list[dict]] = {}
        if source is not None:
            if Path(source).suffix.lower() in (".csv", ".txt"):
                flows = source if flows is None else flows     # a lone CSV is the flows table
            else:
                sheets.update(read_sheets(source))
        for name, t in (("flows", flows), ("materials", materials),
                        ("references", references), ("nodes", nodes),
                        ("products", products), ("relationships", relationships)):
            if t is not None:
                sheets[name] = read_rows(t, what=name, keep_case=True)
        return cls.from_sheets(sheets)

    def write(self, target) -> list[str]:
        """Write the tables: .xlsx for one workbook (needs openpyxl), or any
        other path for a folder of CSVs."""
        sheets = self.to_sheets()
        p = Path(target)
        if p.suffix.lower() == ".xlsx":
            try:
                from openpyxl import Workbook
                from openpyxl.styles import Font
            except ImportError as e:
                raise ImportError("writing .xlsx needs openpyxl: pip install "
                                  "\"figure-tools[tables]\"") from e
            wb = Workbook()
            wb.remove(wb.active)
            for name, rows in sheets.items():
                ws = wb.create_sheet(name[:31])
                cols = list(dict.fromkeys(c for r in rows for c in r))
                ws.append(cols)
                for c in ws[1]:
                    c.font = Font(bold=True)
                for r in rows:
                    ws.append([_cell(r.get(c)) for c in cols])
                ws.freeze_panes = "A2"
                for i, c in enumerate(cols, start=1):
                    width = max([len(str(c))] + [min(len(str(r.get(c) or "")), 40) for r in rows[:200]])
                    ws.column_dimensions[ws.cell(1, i).column_letter].width = min(width + 2, 42)
            p.parent.mkdir(parents=True, exist_ok=True)
            wb.save(p)
            return [str(p)]
        p.mkdir(parents=True, exist_ok=True)
        out = []
        for name, rows in sheets.items():
            cols = list(dict.fromkeys(c for r in rows for c in r))
            f = p / (name + ".csv")
            with f.open("w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(cols)
                for r in rows:
                    w.writerow([_cell(r.get(c)) for c in cols])
            out.append(str(f))
        return out


def _cell(v):
    if v is None:
        return None
    if isinstance(v, str) and len(v) > 32000:
        raise ValueError("a cell is %d characters; Excel allows 32767" % len(v))
    return v


def convert(src, dst) -> list[str]:
    """YAML to tables or tables to YAML.

    convert("ford.yaml", "mache.xlsx")        supply-chain YAML -> workbook
    convert("ford.yaml", "mache_csv/")         supply-chain YAML -> folder of CSVs
    convert("mache.xlsx", "mache_yaml/")       workbook -> YAML files
    """
    s = Path(src)
    if s.suffix.lower() in (".yaml", ".yml"):
        return Dataset.from_yaml(s).write(dst)
    return Dataset.from_tables(s).to_yaml(dst)
