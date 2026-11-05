"""Render every example and check the outputs exist and look sane.

Run from a clean venv:  uv venv && uv pip install -e ".[test]" && pytest
"""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EX = ROOT / "examples"


def run(script):
    r = subprocess.run([sys.executable, str(EX / script)], cwd=ROOT,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_waterfall_example():
    run("waterfall_example.py")
    for ext in ("pdf", "svg", "png"):
        assert (EX / "out" / f"waterfall_example.{ext}").stat().st_size > 5_000


def test_sankey_example():
    run("sankey_example.py")
    for ext in ("pdf", "svg", "png", "html"):
        assert (EX / "out" / f"sankey_example.{ext}").stat().st_size > 5_000


def test_mache_example():
    run("materials/run_mache.py")
    html = (EX / "materials" / "out" / "Mustang_Mach_E_d3.html").read_text()
    assert "const PANELS" in html and len(html) > 20_000
    assert (EX / "materials" / "out" / "Mustang_Mach_E_d3_bibliography.csv").exists()


def test_html_labels_are_clamped():
    # the label-overlap fix: global de-overlap pass and clamp are in the page
    html = (EX / "materials" / "out" / "Mustang_Mach_E_d3.html")
    if not html.exists():
        run("materials/run_mache.py")
    text = html.read_text()
    assert "clampY" in text and "collide" in text


def test_waterfall_total_is_checked():
    from figure_tools import Waterfall
    w = Waterfall().baseline("a", 10).step("b", 1).step("c", 2)
    with pytest.raises(ValueError, match="step is missing or wrong"):
        w.total("end", expect=99)


def test_waterfall_refuses_bare_dollar():
    from figure_tools import Waterfall
    with pytest.raises(ValueError, match="unescaped"):
        Waterfall().title("costs $5 to $6")


def test_sankey_rejects_cycle():
    from figure_tools import Sankey
    s = Sankey().flow("a", "b", 1).flow("b", "a", 1)
    with pytest.raises(ValueError, match="cycle"):
        s.render()


def test_sankey_rejects_nonpositive():
    from figure_tools import Sankey
    with pytest.raises(ValueError):
        Sankey().flow("a", "b", 0)
