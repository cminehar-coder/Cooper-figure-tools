"""Tables are the standard input: CSV, Excel and DataFrame give the same figure."""
from pathlib import Path

import pytest

pd = pytest.importorskip("pandas")
pytest.importorskip("openpyxl")

from figure_tools import MaterialFlow, Sankey, Waterfall  # noqa: E402
from figure_tools.materialflow.tables import Dataset, convert  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
T = ROOT / "examples" / "tables"
M = ROOT / "examples" / "materials"
VEHICLE = "Mustang Mach-E"


def png_bytes(chart, tmp_path, name):
    (path,) = chart.save(str(tmp_path / name), formats=["png"])
    return Path(path).read_bytes()


def test_waterfall_csv_excel_dataframe_identical(tmp_path):
    inputs = {"csv": T / "waterfall.csv", "xlsx": T / "waterfall.xlsx",
              "df": pd.read_csv(T / "waterfall.csv")}
    charts = {k: Waterfall.from_table(v, profile="paper", title="t", ylabel="y")
              for k, v in inputs.items()}
    assert charts["csv"]._rows == charts["xlsx"]._rows == charts["df"]._rows
    pngs = {k: png_bytes(c, tmp_path, "w_" + k) for k, c in charts.items()}
    assert pngs["csv"] == pngs["xlsx"] == pngs["df"]


def test_waterfall_table_matches_python_api(tmp_path):
    w = Waterfall(profile="paper").title("t").baseline("2023 unit cost", 120.0)
    for lab, v in (("Materials", 9.5), ("Labour", -2.1), ("Energy", 1.8),
                   ("Scrap rate", -3.4), ("Overhead", 1.6)):
        w.step(lab, v)
    w.total("2024 unit cost", expect=127.4).ylabel("y")
    w.note("Illustrative numbers, not data. Negative steps lower the cost.")
    t = Waterfall.from_table(T / "waterfall.csv", profile="paper", title="t", ylabel="y")
    assert png_bytes(w, tmp_path, "api") == png_bytes(t, tmp_path, "tbl")


def test_sankey_csv_excel_dataframe_identical(tmp_path):
    inputs = {"csv": T / "sankey.csv", "xlsx": T / "sankey.xlsx",
              "df": pd.read_csv(T / "sankey.csv")}
    charts = {k: Sankey.from_table(v, title="t") for k, v in inputs.items()}
    assert charts["csv"]._flows == charts["xlsx"]._flows == charts["df"]._flows
    assert charts["csv"]._unit == "k USD"
    pngs = {k: png_bytes(c, tmp_path, "s_" + k) for k, c in charts.items()}
    assert pngs["csv"] == pngs["xlsx"] == pngs["df"]
    htmls = {}
    for k, c in charts.items():
        c.save_html(str(tmp_path / (k + ".html")))
        htmls[k] = (tmp_path / (k + ".html")).read_text()
    assert htmls["csv"] == htmls["xlsx"] == htmls["df"]


def test_waterfall_errors():
    with pytest.raises(ValueError, match="missing column.*kind"):
        Waterfall.from_table(pd.DataFrame({"label": ["a"], "value": [1]}))
    bad_total = pd.DataFrame({"label": ["a", "b", "c"], "value": [10, 2, 99],
                              "kind": ["base", "step", "total"]})
    with pytest.raises(ValueError, match=r"row 4 \(c\).*step is missing or wrong"):
        Waterfall.from_table(bad_total)
    with pytest.raises(ValueError, match="use base, step or total"):
        Waterfall.from_table(pd.DataFrame({"label": ["a"], "value": [1], "kind": ["bar"]}))
    with pytest.raises(ValueError, match="no total row"):
        Waterfall.from_table(pd.DataFrame({"label": ["a", "b"], "value": [1, 2],
                                           "kind": ["base", "step"]}))


def test_sankey_errors():
    with pytest.raises(ValueError, match="missing column.*target"):
        Sankey.from_table(pd.DataFrame({"source": ["a"], "value": [1]}))
    with pytest.raises(ValueError, match="mixes units"):
        Sankey.from_table(pd.DataFrame({"source": ["a", "b"], "target": ["b", "c"],
                                        "value": [1, 1], "unit": ["kg", "t"]}))
    with pytest.raises(ValueError, match="not a number"):
        Sankey.from_table(pd.DataFrame({"source": ["a"], "target": ["b"], "value": ["lots"]}))


def test_missing_pandas_or_file_is_clear(tmp_path):
    with pytest.raises(FileNotFoundError, match="no such file"):
        Sankey.from_table(tmp_path / "nope.csv")


def mache(kind):
    if kind == "yaml":
        return MaterialFlow.from_yaml(M / "data" / "supply_chains" / "ford.yaml", vehicle=VEHICLE)
    if kind == "xlsx":
        return MaterialFlow.from_table(M / "tables" / "mache.xlsx", vehicle=VEHICLE)
    if kind == "csv":
        return MaterialFlow.from_table(M / "tables" / "mache_csv", vehicle=VEHICLE)
    d = M / "tables" / "mache_csv"
    read = lambda n: pd.read_csv(d / (n + ".csv"))  # noqa: E731
    return MaterialFlow.from_table(flows=read("flows"), nodes=read("nodes"),
                                   materials=read("materials"), references=read("references"),
                                   products=read("products"), relationships=read("relationships"),
                                   vehicle=VEHICLE)


def test_mache_renders_through_shared_path_from_every_input(tmp_path):
    out = {}
    for kind in ("yaml", "xlsx", "csv", "df"):
        m = mache(kind)
        assert isinstance(m, Sankey)            # same object as Sankey, same save()
        files = m.save(str(tmp_path / kind), formats=["png"])
        assert Path(files[0]).stat().st_size > 50_000
        html = m.save_html(str(tmp_path / (kind + ".html")))
        out[kind] = (m._flows, m._columns, Path(html).read_text(),
                     Path(files[0]).read_bytes())
    for kind in ("xlsx", "csv", "df"):
        assert out[kind] == out["yaml"], kind
    assert "const PANELS" in out["yaml"][2]


def test_yaml_tables_yaml_round_trip(tmp_path):
    convert(M / "tables" / "mache.xlsx", tmp_path / "yaml_out")
    a = Dataset.from_yaml(M / "data" / "supply_chains" / "ford.yaml")
    b = Dataset.from_yaml(tmp_path / "yaml_out" / "data" / "supply_chains" / "supply_chain.yaml")

    def clean(ds):    # an empty cell and an absent key mean the same thing; edge whitespace is trimmed
        norm = lambda v: v.strip() if isinstance(v, str) else v  # noqa: E731
        return {k: [{c: norm(v) for c, v in r.items() if v is not None} for r in rows]
                for k, rows in ds.to_sheets().items()}
    assert clean(a).keys() == clean(b).keys()
    assert clean(a) == clean(b)


def test_materialflow_table_errors():
    with pytest.raises(ValueError, match="missing column.*value_per_kwh"):
        MaterialFlow.from_table(flows=pd.DataFrame({"source": ["a"], "target": ["b"]}))
    with pytest.raises(ValueError, match="flows table is missing column"):
        MaterialFlow.from_table(M / "tables" / "mache_csv" / "materials.csv")


def test_flows_only_table_draws_per_kwh(tmp_path):
    flows = pd.DataFrame({
        "source": ["mining", "cell_mfg"], "target": ["cell_mfg", "application"],
        "material": ["ore", "cell"], "value_per_kwh": [3.0, 1.0], "unit": ["kg_per_kWh"] * 2})
    m = MaterialFlow.from_table(flows=flows)
    assert m.save(str(tmp_path / "x"), formats=["png"])
    assert "const PANELS" in Path(m.save_html(str(tmp_path / "x.html"))).read_text()
