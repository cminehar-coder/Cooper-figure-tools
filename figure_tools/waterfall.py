"""
Waterfall charts: walk a baseline to a total through named contributions.

You declare what the chart claims and what the parts are; the module decides
colour, spacing, stroke and type. Two profiles ("paper" and "deck"), one export
set (PDF, SVG, PNG), and a refusal to ship a chart that looks finished and is
not (an unchecked total, an unescaped "$", a missing glyph).

    from figure_tools import Waterfall

    w = Waterfall(profile="paper")
    w.title("Material costs explain most of the increase")
    w.baseline("2023 cost", 120.0)
    w.step("Materials", +9.5)
    w.step("Labour", -2.1)
    w.total("2024 cost", expect=127.4)
    w.ylabel("Unit cost (USD/unit)")
    w.note("Illustrative numbers.")
    w.save("out/bridge")

Sign convention: a NEGATIVE step is drawn in the "down" colour and a POSITIVE
step in the "up" colour. That is the arithmetic direction, not a judgement that
down is good, and the axis label should say what the quantity is.
"""
from __future__ import annotations

import warnings
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

from . import style as S

MM_PER_IN = 25.4


class Waterfall:
    def __init__(self, profile: str = "paper", height_mm: float | None = None,
                 width_mm: float | None = None, zoom: bool = False):
        """zoom=True crops the value axis to the bars instead of including zero.

        A waterfall whose steps are a few percent of the baseline is
        unreadable on a zero-anchored axis -- the steps render as hairlines.
        Cropping is defensible here ONLY because every bar carries its own
        value label, so the reader takes the number from the label and uses
        the bar for direction and relative size. Do not crop a figure whose
        bars are unlabelled.
        """
        self.style = S.deck() if profile == "deck" else S.paper()
        st = self.style
        self.w_mm = width_mm or st.width_mm
        self.h_mm = height_mm or (self.w_mm * 0.46)
        if st.max_depth_mm and self.h_mm > st.max_depth_mm:
            raise ValueError("height %.1f mm exceeds the %s depth limit %.1f mm"
                             % (self.h_mm, st.name, st.max_depth_mm))
        self.deck = (profile == "deck")
        self.zoom = zoom
        self.c_up   = S.OKABE_ITO["vermilion"] if self.deck else st.accent2   # increase
        self.c_down = S.OKABE_ITO["green"] if self.deck else st.accent        # decrease
        self.c_anchor = "#4A4A50"
        self._title = None
        self._note = None
        self._ylabel = None
        self._rows = []          # (kind, label, value)  kind in base|step|total

    # ---------------- declaration ----------------
    @classmethod
    def from_table(cls, table, sheet=None, profile: str = "paper", title: str | None = None,
                   ylabel: str | None = None, **kw) -> "Waterfall":
        """Build a waterfall from a table.

        table: a DataFrame, or a path to a CSV or Excel file (sheet= picks the
        Excel sheet). Columns: label, value, kind, and optionally note.
        kind is base (first row), step, or total. On a total row, value is the
        total you measured independently; the steps must add up to it.
        """
        from .table import read_rows, require
        rows = read_rows(table, sheet, "waterfall")
        require(rows, ["label", "value", "kind"], "waterfall")
        w = cls(profile=profile, **kw)
        if title:
            w.title(title)
        if ylabel:
            w.ylabel(ylabel)
        notes = []
        for i, r in enumerate(rows, start=2):       # row 1 is the header
            kind = str(r.get("kind") or "").strip().lower()
            where = "waterfall row %d (%s)" % (i, r.get("label"))
            if r.get("label") is None or r.get("value") is None:
                raise ValueError("%s needs a label and a value" % where)
            if kind not in ("base", "step", "total"):
                raise ValueError("%s: kind is %r; use base, step or total" % (where, r.get("kind")))
            try:
                value = float(r["value"])
            except (TypeError, ValueError):
                raise ValueError("%s: value %r is not a number" % (where, r["value"])) from None
            label = str(r["label"])
            try:
                if kind == "base":
                    w.baseline(label, value)
                elif kind == "step":
                    w.step(label, value)
                else:
                    w.total(label, expect=value)
            except ValueError as e:
                raise ValueError("%s: %s" % (where, e)) from None
            if r.get("note"):
                notes.append(str(r["note"]))
        if not any(k == "total" for k, _, _ in w._rows):
            raise ValueError("waterfall table has no total row; add one with kind = total "
                             "and the independently measured total as its value")
        if notes:
            w.note(" ".join(dict.fromkeys(notes)))
        return w

    def title(self, text: str) -> "Waterfall":
        self._title = self._check_dollars(text, "title()"); return self

    @staticmethod
    def _check_dollars(text: str, where: str) -> str:
        """matplotlib reads $...$ as mathtext.

        A caption saying "$/pack ... $37.5M" has two unescaped dollars, so
        everything between them is silently italicised as maths and the figure
        looks finished while reading as gibberish. Escape them as
        a backslash plus dollar sign.
        """
        import re
        bare = len(re.findall(r"(?<!\\)\$", text))
        if bare:
            raise ValueError(
                "%s contains %d unescaped '$'. matplotlib will parse them as "
                "mathtext delimiters and italicise the span between them. "
                "Write '\\$' for a literal dollar sign." % (where, bare))
        return text

    def note(self, text: str) -> "Waterfall":
        self._note = self._check_dollars(text, "note()"); return self

    def baseline(self, label: str, value: float) -> "Waterfall":
        if self._rows:
            raise ValueError("baseline must come first")
        self._rows.append(("base", self._check_dollars(label, "baseline()"),
                           float(value))); return self

    def step(self, label: str, delta: float) -> "Waterfall":
        if not self._rows:
            raise ValueError("call baseline() before step()")
        self._rows.append(("step", self._check_dollars(label, "step()"),
                           float(delta))); return self

    def ylabel(self, text: str) -> "Waterfall":
        """Axis label. Without one the reader gets a bare column of numbers and
        has to hunt the units out of the note."""
        self._ylabel = self._check_dollars(text, "ylabel()"); return self

    def total(self, label: str, expect: float = None, tol: float = 0.005) -> "Waterfall":
        """Close the waterfall.

        `expect` is the point of the method. The walk is computed FROM the steps,
        so a total with no expectation can never disagree with them and a dropped
        step goes undetected. Pass the independently-measured total and a missing
        step becomes an error."""
        if not self._rows:
            raise ValueError("call baseline() before total()")
        if any(k == "total" for k, _, _ in self._rows):
            raise ValueError("waterfall already has a total()")
        if expect is not None:
            run = self._rows[0][2] + sum(v for k, _, v in self._rows[1:] if k == "step")
            if abs(run - float(expect)) > tol:
                raise ValueError(
                    "steps walk to %.4f but the measured total is %.4f "
                    "(difference %.4f). A step is missing or wrong."
                    % (run, float(expect), run - float(expect)))
        self._rows.append(("total", self._check_dollars(label, "total()"),
                           None)); return self

    # ---------------- geometry ----------------
    def _walk(self):
        """Return [(kind,label,bottom,top,value)] and the running total."""
        out, run = [], 0.0
        for kind, lab, v in self._rows:
            if kind == "base":
                run = v; out.append((kind, lab, 0.0, v, v))
            elif kind == "step":
                nxt = run + v
                out.append((kind, lab, min(run, nxt), max(run, nxt), v))
                run = nxt
            else:
                out.append((kind, lab, 0.0, run, run))
        return out, run

    @staticmethod
    def _decimals(bars, limit=4, tol=0.01):
        """Fewest decimals that satisfy BOTH legibility conditions.

        1. The printed numbers add up. At 0 dp a waterfall can
           print 10,097 - 325 + 11 = 9,783 beside a total of 9,782, and a reader
           who checks the arithmetic on the slide concludes the model is wrong.
        2. The SMALLEST step is still legible. Condition 1 alone is satisfied at
           0 dp by a waterfall reading "90, -1, -1, +0, 88", where the
           arithmetic closes and the figure says nothing. So the smallest non-zero
           step must also survive rounding to within `tol` relative error."""
        base = next((v for k, _, _, _, v in bars if k == "base"), 0.0)
        steps = [v for k, _, _, _, v in bars if k == "step"]
        total = next((v for k, _, _, _, v in bars if k == "total"), None)
        if total is None:
            return 0
        small = min((abs(v) for v in steps if v), default=None)
        for d in range(limit + 1):
            r = lambda x: round(x, d)
            closes = abs((r(base) + sum(r(s) for s in steps)) - r(total)) < 10.0 ** (-d) / 2
            legible = (small is None or
                       (round(small, d) != 0 and abs(round(small, d) - small) / small <= tol))
            if closes and legible:
                return d
        return limit

    # ---------------- render ----------------
    def render(self):
        if not self._rows:
            raise ValueError("nothing declared")
        if self._rows[-1][0] != "total":
            raise ValueError("a waterfall must close with total(); otherwise the "
                             "reader cannot tell whether steps are exhaustive")
        st = self.style
        bars, _ = self._walk()
        self._dp = self._decimals(bars)
        # Must be the library's own rcParams, not a hand-rolled pair. Rolling
        # our own silently drops pdf.fonttype 42 and svg.fonttype "none", so
        # every export embeds Type 3 fonts and converts text to paths --
        # the PDF would embed Type 3 fonts and an editor could not fix the type.
        plt.rcParams.update(S.rcparams(st))
        fig = plt.figure(figsize=(self.w_mm / MM_PER_IN, self.h_mm / MM_PER_IN),
                         dpi=st.dpi, facecolor=st.bg)
        # margins as fractions of the canvas. A y-axis label needs room of its
        # own: at the bare 0.055 the rotated text is drawn off the canvas edge
        # and silently clipped.
        left = 0.085 if self._ylabel else 0.055
        top_pad = 0.16 if self._title else 0.06
        bot_pad = 0.30 if self._note else 0.24
        ax = fig.add_axes([left, bot_pad, 1 - left - 0.03, 1 - bot_pad - top_pad])
        ax.set_facecolor(st.bg)

        lo = min(b[2] for b in bars); hi = max(b[3] for b in bars)
        if self.zoom:
            # Only the WALK levels matter. Including an anchor bar's bottom
            # (which sits at zero) is what silently defeats the crop.
            edges = []
            for kind, lab, b0, b1, v in bars:
                edges.append(b1)
                if kind == "step":
                    edges.append(b0)
            lo, hi = min(edges), max(edges)
            span = (hi - lo) or abs(hi) * 0.02 or 1.0
            lo -= span * 0.60
            span = hi - lo
        else:
            span = hi - lo or 1.0
        ax.set_ylim(lo - span * 0.06, hi + span * 0.17)
        ax.set_xlim(-0.7, len(bars) - 0.3)

        for i, (kind, lab, b0, b1, v) in enumerate(bars):
            if kind in ("base", "total"):
                col, alpha = self.c_anchor, 1.0
            else:
                # a step of exactly zero is neither an increase nor a saving
                col = self.c_up if v > 0 else (self.c_down if v < 0 else self.c_anchor)
                alpha = 1.0
            # min/abs, not b0 and (b1-b0): an anchor whose value is negative has
            # b1 < b0 = 0, which draws a zero-height sliver at the axis instead of
            # the bar. Silent, and wrong only for deltas and net-negative totals.
            ax.add_patch(Rectangle((i - 0.34, min(b0, b1)), 0.68,
                                   max(abs(b1 - b0), span * 0.002),
                                   facecolor=col, edgecolor="none", alpha=alpha, zorder=3))
            # connector to the next bar
            if i < len(bars) - 1:
                y = b1 if kind in ("base", "total") else (b1 if v > 0 else b0)
                ax.plot([i + 0.34, i + 1 - 0.34], [y, y], color=st.rule,
                        lw=st.lw_connector, ls=(0, (2.2, 1.8)), zorder=2)
            # value label
            txt = (format(v, "+,.%df" % self._dp) if kind == "step"
                   else format(v, ",.%df" % self._dp))
            ax.text(i, b1 + span * 0.028, txt, ha="center", va="bottom",
                    fontsize=max(st.size_annot, st.min_font_pt),
                    fontweight="bold", color=col, zorder=4)

        ax.set_xticks(range(len(bars)))
        ax.set_xticklabels([b[1] for b in bars],
                           fontsize=max(st.size_body - 1.5, st.min_font_pt))
        ax.tick_params(axis="x", length=0, pad=6)
        ax.tick_params(axis="y", labelsize=max(st.size_annot - 1, st.min_font_pt),
                       colors=st.muted, length=2, width=st.lw_rule)
        if self._ylabel:
            ax.set_ylabel(self._ylabel, fontsize=max(st.size_annot, st.min_font_pt),
                          color=st.muted, labelpad=6)
        ax.grid(axis="y", color=st.rule, lw=st.lw_rule * 0.7, alpha=0.30, zorder=0)
        ax.set_axisbelow(True)
        for sp in ("top", "right", "left"):
            ax.spines[sp].set_visible(False)
        ax.spines["bottom"].set_color(st.rule)
        ax.spines["bottom"].set_linewidth(st.lw_rule)

        if self._title:
            fig.text(left, 1 - top_pad * 0.52, self._title,
                     fontsize=st.size_title, fontweight="bold", color=st.ink,
                     ha="left", va="center")
        if self._note:
            fig.text(left, 0.045, self._note, fontsize=st.size_note,
                     color=st.muted, ha="left", va="bottom", wrap=True)
        self.fig = fig
        return self

    def save(self, stem: str, formats=None) -> list[str]:
        """Write every format at final size. A missing glyph is an error, not a
        warning -- matplotlib's default is to draw nothing and carry on."""
        import os
        if not hasattr(self, "fig"):
            self.render()
        st = self.style
        d = os.path.dirname(stem)
        if d:
            os.makedirs(d, exist_ok=True)
        out = []
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            for ext in (formats or st.formats):
                p = "%s.%s" % (stem, ext)
                self.fig.savefig(p, dpi=st.dpi, facecolor=st.bg, pad_inches=0)
                out.append(p)
        missing = sorted({str(w.message) for w in caught
                          if "missing from font" in str(w.message)})
        if missing:
            raise RuntimeError("glyphs missing from %s: %s" % (st.family, missing))
        plt.close(self.fig)
        return out
