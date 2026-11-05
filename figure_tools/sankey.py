"""
Sankey diagrams from a plain list of flows.

    from figure_tools import Sankey

    s = Sankey(profile="paper")
    s.title("Where the budget goes")
    s.flow("Revenue", "Salaries", 60)
    s.flow("Revenue", "Equipment", 25)
    s.flow("Revenue", "Travel", 15)
    s.unit("k USD")
    s.save("out/budget")                 # PDF, SVG, PNG (matplotlib)
    s.save_html("out/budget.html")       # interactive d3 version

Each flow is (source, target, value). Columns are assigned automatically by
the longest path from a source, node heights are max(inflow, outflow), and
ordering inside a column is improved by a few barycentre sweeps to reduce
crossings. Cycles are rejected. Flows into a node and out of it need not match;
the node is drawn as tall as the larger side.
"""
from __future__ import annotations

import html as _html
import json
import os
import warnings
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import PathPatch, Rectangle
from matplotlib.path import Path
import matplotlib.patheffects as pe

from . import style as S

MM_PER_IN = 25.4
_CYCLE = ["blue", "vermilion", "green", "orange", "purple", "sky"]


class Sankey:
    def __init__(self, profile: str = "paper", width_mm: float | None = None,
                 height_mm: float | None = None):
        self.style = S.deck() if profile == "deck" else S.paper()
        self.w_mm = width_mm or self.style.width_mm
        self.h_mm = height_mm or self.w_mm * (0.55 if profile == "deck" else 0.6)
        self._flows: list[tuple[str, str, float, str | None]] = []
        self._title = None
        self._note = None
        self._unit = ""
        self._colors: dict[str, str] = {}

    # ---------------- declaration ----------------
    def title(self, text: str) -> "Sankey":
        self._title = _no_bare_dollar(text); return self

    def note(self, text: str) -> "Sankey":
        self._note = _no_bare_dollar(text); return self

    def unit(self, text: str) -> "Sankey":
        self._unit = text; return self

    def color(self, node: str, hex_color: str) -> "Sankey":
        """Override the colour of a node (and of the flows leaving it)."""
        self._colors[node] = hex_color; return self

    def flow(self, source: str, target: str, value: float,
             color: str | None = None) -> "Sankey":
        value = float(value)
        if value <= 0:
            raise ValueError("flow %s -> %s has value %g; flows must be positive"
                             % (source, target, value))
        if source == target:
            raise ValueError("flow from %r to itself" % source)
        self._flows.append((str(source), str(target), value, color)); return self

    # ---------------- layout ----------------
    def _layers(self):
        nodes = []
        for s, t, _, _ in self._flows:
            for n in (s, t):
                if n not in nodes:
                    nodes.append(n)
        out = defaultdict(list)
        indeg = defaultdict(int)
        for s, t, _, _ in self._flows:
            out[s].append(t); indeg[t] += 1
        # Kahn's algorithm with longest-path layering; leftover nodes = cycle.
        layer = {n: 0 for n in nodes}
        queue = [n for n in nodes if indeg[n] == 0]
        seen = 0
        deg = dict(indeg)
        while queue:
            n = queue.pop(0); seen += 1
            for m in out[n]:
                layer[m] = max(layer[m], layer[n] + 1)
                deg[m] -= 1
                if deg[m] == 0:
                    queue.append(m)
        if seen != len(nodes):
            raise ValueError("the flows contain a cycle; a Sankey needs a "
                             "directed acyclic graph")
        return nodes, layer

    def _order(self, nodes, layer):
        cols = defaultdict(list)
        for n in nodes:
            cols[layer[n]].append(n)
        pos = {n: i for c in cols.values() for i, n in enumerate(c)}
        nb_in = defaultdict(list); nb_out = defaultdict(list)
        for s, t, v, _ in self._flows:
            nb_out[s].append((t, v)); nb_in[t].append((s, v))

        def bary(n, nbrs):
            w = sum(v for _, v in nbrs)
            return sum(pos[m] * v for m, v in nbrs) / w if w else pos[n]
        for _ in range(6):
            for k in sorted(cols)[1:]:
                cols[k].sort(key=lambda n: bary(n, nb_in[n]))
                for i, n in enumerate(cols[k]): pos[n] = i
            for k in sorted(cols)[::-1][1:]:
                cols[k].sort(key=lambda n: bary(n, nb_out[n]))
                for i, n in enumerate(cols[k]): pos[n] = i
        return cols

    # ---------------- render ----------------
    def render(self):
        if not self._flows:
            raise ValueError("nothing declared; call flow() first")
        st = self.style
        nodes, layer = self._layers()
        cols = self._order(nodes, layer)
        inflow = defaultdict(float); outflow = defaultdict(float)
        for s, t, v, _ in self._flows:
            outflow[s] += v; inflow[t] += v
        size = {n: max(inflow[n], outflow[n]) for n in nodes}
        ncol = max(cols) + 1

        plt.rcParams.update(S.rcparams(st))
        fig = plt.figure(figsize=(self.w_mm / MM_PER_IN, self.h_mm / MM_PER_IN),
                         dpi=st.dpi, facecolor=st.bg)
        left, right = 0.03, 0.18 if ncol > 1 else 0.03
        top = 0.14 if self._title else 0.04
        bot = 0.12 if self._note else 0.04
        ax = fig.add_axes([left, bot, 1 - left - right, 1 - bot - top])
        ax.axis("off")
        ax.set_xlim(0, 1); ax.set_ylim(1, 0)

        # vertical scale: the fullest column fits with 6% gaps between nodes
        gap_frac = 0.06
        def col_need(c):
            return sum(size[n] for n in c) / max(1e-9, 1 - gap_frac * (len(c) - 1))
        scale_den = max(col_need(c) for c in cols.values())
        k = 1.0 / scale_den
        node_w = 0.022
        xs = {c: (c / (ncol - 1) * (1 - node_w) if ncol > 1 else 0.0) for c in cols}
        y0 = {}
        for c, members in cols.items():
            total = sum(size[n] for n in members) * k + gap_frac * (len(members) - 1)
            y = (1 - total) / 2
            for n in members:
                y0[n] = y; y += size[n] * k + gap_frac

        palette = [S.OKABE_ITO[c] for c in _CYCLE]
        color = {}
        order_for_color = [n for c in sorted(cols) for n in cols[c]]
        for i, n in enumerate(order_for_color):
            color[n] = self._colors.get(n, palette[i % len(palette)])

        # link slots: stack outgoing flows on the source, incoming on the target
        out_off = defaultdict(float); in_off = defaultdict(float)
        tgt_order = {n: i for c in cols.values() for i, n in enumerate(c)}
        for s, t, v, col in sorted(self._flows,
                                   key=lambda f: (tgt_order[f[1]], tgt_order[f[0]])):
            ya = y0[s] + out_off[s]; out_off[s] += v * k
            yb = y0[t] + in_off[t]; in_off[t] += v * k
            xa = xs[layer[s]] + node_w; xb = xs[layer[t]]
            xm = (xa + xb) / 2
            h = v * k
            verts = [(xa, ya), (xm, ya), (xm, yb), (xb, yb),
                     (xb, yb + h), (xm, yb + h), (xm, ya + h), (xa, ya + h), (xa, ya)]
            codes = [Path.MOVETO, Path.CURVE4, Path.CURVE4, Path.CURVE4,
                     Path.LINETO, Path.CURVE4, Path.CURVE4, Path.CURVE4, Path.CLOSEPOLY]
            ax.add_patch(PathPatch(Path(verts, codes), facecolor=col or color[s],
                                   edgecolor="none", alpha=0.38, zorder=1))
        fs = max(st.size_annot, st.min_font_pt)
        halo = [pe.withStroke(linewidth=2.2, foreground=st.bg)]
        for n in nodes:
            x = xs[layer[n]]
            ax.add_patch(Rectangle((x, y0[n]), node_w, size[n] * k,
                                   facecolor=color[n], edgecolor="none", zorder=3))
            val = format(size[n], ",.4g") + ((" " + self._unit) if self._unit else "")
            ax.text(x + node_w + 0.008, y0[n] + size[n] * k / 2,
                    _no_bare_dollar(n) + "\n" + val, ha="left", va="center",
                    fontsize=fs, color=st.ink, zorder=4, path_effects=halo,
                    linespacing=1.15)
        if self._title:
            fig.text(left, 1 - top * 0.5, self._title, fontsize=st.size_title,
                     fontweight="bold", color=st.ink, ha="left", va="center")
        if self._note:
            fig.text(left, 0.02, self._note, fontsize=st.size_note, color=st.muted,
                     ha="left", va="bottom", wrap=True)
        self.fig = fig
        return self

    def save(self, stem: str, formats=None) -> list[str]:
        if not hasattr(self, "fig"):
            self.render()
        d = os.path.dirname(stem)
        if d:
            os.makedirs(d, exist_ok=True)
        out = []
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            for ext in (formats or self.style.formats):
                p = "%s.%s" % (stem, ext)
                self.fig.savefig(p, dpi=self.style.dpi, facecolor=self.style.bg)
                out.append(p)
        missing = sorted({str(w.message) for w in caught
                          if "missing from font" in str(w.message)})
        if missing:
            raise RuntimeError("glyphs missing from %s: %s"
                               % (self.style.family, missing))
        plt.close(self.fig)
        del self.fig
        return out

    # ---------------- interactive HTML ----------------
    def save_html(self, path: str, subtitle: str = "", height: int = 700,
                  width: int = 1200) -> str:
        """Interactive version (d3-sankey, loaded from a CDN, so it needs a
        network connection when opened). Hover for values."""
        from .materialflow.d3_renderer import render_html
        nodes, layer = self._layers()
        inflow = defaultdict(float); outflow = defaultdict(float)
        for s, t, v, _ in self._flows:
            outflow[s] += v; inflow[t] += v
        palette = [S.OKABE_ITO[c] for c in _CYCLE]
        color = {n: self._colors.get(n, palette[i % len(palette)])
                 for i, n in enumerate(nodes)}
        unit = self._unit
        panel = {
            "title": "", "subtitle": "", "unit": unit,
            "height": height, "width": width,
            "nodes": [{"id": n, "label": n + "\n" + _fmt(max(inflow[n], outflow[n]), unit), "rank": layer[n], "color": color[n],
                       "track": "mid",
                       "hover": _html.escape("%s: %.4g %s" % (n, max(inflow[n], outflow[n]), unit))}
                      for n in nodes],
            "links": [{"source": s, "target": t, "value": v, "raw_value": v,
                       "color": _rgba(c or color[s], 0.35),
                       "hover": _html.escape("%s to %s: %.4g %s" % (s, t, v, unit))}
                      for s, t, v, c in self._flows],
        }
        page = render_html(_html.escape(self._title or "Sankey"),
                           _html.escape(subtitle), [panel], {})
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(page)
        return path


def _fmt(v, unit):
    return format(v, ",.4g") + ((" " + unit) if unit else "")


def _rgba(hex_color: str, a: float) -> str:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return "rgba(%d,%d,%d,%g)" % (r, g, b, a)


def _no_bare_dollar(text: str) -> str:
    import re
    if re.search(r"(?<!\\)\$", text):
        raise ValueError("text contains an unescaped '$' which matplotlib reads "
                         "as mathtext; write '\\$' for a literal dollar sign")
    return text
