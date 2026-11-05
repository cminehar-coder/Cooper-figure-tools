"""Read a table from a DataFrame, a CSV path or an Excel path.

Every from_table() in figure-tools goes through read_rows(), so the three
inputs give the same rows. CSV needs nothing beyond the standard library.
DataFrames and Excel files need pandas (and openpyxl for .xlsx):

    pip install "figure-tools[tables]"
"""
from __future__ import annotations

import csv
import json
import math
import os
from pathlib import Path


def _norm_col(c) -> str:
    return str(c).strip().lower().replace(" ", "_")


def coerce(v):
    """One cell to one Python value, the same for CSV, Excel and pandas.

    Empty and NaN become None. Whole floats become ints. "true"/"false" become
    booleans. A cell that starts with "json:" is parsed as JSON (nested
    fields in the material-flow tables). Everything else stays text.
    """
    if v is None:
        return None
    if hasattr(v, "item") and not isinstance(v, (str, bytes)):
        try:
            v = v.item()          # numpy scalar -> python
        except (ValueError, AttributeError):
            pass
    if isinstance(v, float):
        if math.isnan(v):
            return None
        return int(v) if v.is_integer() else v
    if isinstance(v, bool) or isinstance(v, int):
        return v
    if hasattr(v, "isoformat"):
        return v.isoformat()
    s = str(v).strip()
    if s == "" or s.lower() in ("nan", "none", "null"):
        return None
    if s.startswith("json:"):
        return json.loads(s[5:])
    low = s.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(s)
    except ValueError:
        pass
    try:
        f = float(s)
        if math.isnan(f) or math.isinf(f):
            return s
        return int(f) if f.is_integer() else f
    except ValueError:
        return s


def _need_pandas(what: str):
    try:
        import pandas as pd
    except ImportError as e:
        raise ImportError("reading %s needs pandas: pip install "
                          "\"figure-tools[tables]\" (CSV works without it)" % what) from e
    return pd


def read_rows(x, sheet=None, what: str = "table", keep_case: bool = False) -> list[dict]:
    """Return the table as a list of {column: value} dicts.

    x: a pandas DataFrame, a path to .csv / .xlsx / .xls, or a list of dicts.
    sheet: sheet name or index for Excel files (default: the first sheet).
    Column names are lower-cased and spaces become underscores, unless
    keep_case is set (the material-flow tables carry YAML keys such as pack_kWh_usable).
    """
    if isinstance(x, (str, os.PathLike)):
        p = Path(x)
        if not p.exists():
            raise FileNotFoundError("%s: no such file: %s" % (what, p))
        ext = p.suffix.lower()
        if ext in (".csv", ".txt"):
            with p.open(newline="", encoding="utf-8-sig") as f:
                raw = list(csv.DictReader(f))
        elif ext in (".xlsx", ".xlsm", ".xls"):
            pd = _need_pandas("Excel files")
            try:
                df = pd.read_excel(p, sheet_name=0 if sheet is None else sheet,
                                   dtype=object)
            except ValueError as e:
                raise ValueError("%s: cannot read sheet %r from %s (%s)"
                                 % (what, sheet, p.name, e)) from e
            raw = df.to_dict("records")
        else:
            raise ValueError("%s: expected .csv or .xlsx, got %s" % (what, p.name))
    elif hasattr(x, "to_dict") and hasattr(x, "columns"):
        raw = x.astype(object).to_dict("records")
    elif isinstance(x, list):
        raw = x
    else:
        raise TypeError("%s: pass a DataFrame, a CSV or Excel path, or a list "
                        "of dicts, not %s" % (what, type(x).__name__))
    rows = []
    for r in raw:
        row = {(str(k).strip() if keep_case else _norm_col(k)): coerce(v)
               for k, v in r.items() if k is not None}
        if any(v is not None for v in row.values()):
            rows.append(row)
    return rows


def require(rows: list[dict], columns: list[str], what: str,
            columns_present: list[str] | None = None) -> None:
    """Raise a clear error if a required column is missing."""
    have = set(columns_present) if columns_present is not None else (
        set().union(*[r.keys() for r in rows]) if rows else set())
    missing = [c for c in columns if c not in have]
    if missing:
        raise ValueError("%s table is missing column(s) %s. It has: %s"
                         % (what, ", ".join(missing), ", ".join(sorted(have)) or "(nothing)"))
    if not rows:
        raise ValueError("%s table has no rows" % what)


def read_sheets(x) -> dict[str, list[dict]]:
    """A whole workbook or folder of CSVs as {sheet name: rows}."""
    p = Path(x)
    if p.is_dir():
        return {f.stem.lower(): read_rows(f, what=f.name, keep_case=True)
                for f in sorted(p.glob("*.csv"))}
    if p.suffix.lower() in (".xlsx", ".xlsm", ".xls"):
        pd = _need_pandas("Excel files")
        book = pd.read_excel(p, sheet_name=None, dtype=object)
        return {str(k).strip().lower(): read_rows(v, what="sheet " + str(k), keep_case=True)
                for k, v in book.items()}
    raise ValueError("expected an .xlsx workbook or a folder of CSVs, got %s" % p)
