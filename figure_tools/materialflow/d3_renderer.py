"""Emit a self-contained HTML Sankey using d3-sankey with strict rank-pinned
column placement. Solves the Plotly Sankey column-collapse problem by using
d3-sankey's `nodeAlign` callback to pin every node to its assigned column.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

# Imports from sibling render_sankey module.
# This file is consumed by render_sankey.py via from-imports; keep the
# surface tiny and stable.

D3_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>__TITLE__</title>
<style>
  body { margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif; color:#222; background:#fff; }
  .header { padding:14px 24px 4px; }
  .header h1 { margin:0; font-size:18px; font-weight:600; }
  .header .sub { margin-top:4px; font-size:11px; color:#555; }
  .panel { padding:18px 24px; border-bottom:1px solid #eee; }
  .panel-title { font-size:14px; font-weight:600; margin:0 0 6px 0; }
  .panel-sub { font-size:11px; color:#666; margin:0 0 8px 0; }
  .col-axis text { font-size:10.5px; fill:#666; }
  .col-axis line { stroke:#e5e5e5; stroke-dasharray:2,3; }
  .node rect { stroke:rgba(0,0,0,0.35); stroke-width:0.5; }
  .node-label { font-size:11px; pointer-events:none; }
  .node-label tspan { dominant-baseline:middle; }
  .link { fill:none; stroke-opacity:0.45; }
  .link:hover { stroke-opacity:0.8; }
  #tip { position:absolute; pointer-events:none; padding:8px 10px; background:rgba(20,20,20,0.92); color:#fff; font-size:11px; line-height:1.4; border-radius:4px; max-width:340px; opacity:0; transition:opacity 0.08s; box-shadow:0 4px 12px rgba(0,0,0,0.18); white-space:normal; }
</style>
</head>
<body>
<div class="header">
  <h1>__TITLE__</h1>
  <div class="sub">__SUBTITLE__</div>
</div>
<div id="panels"></div>
<div id="tip"></div>

<script src="https://cdn.jsdelivr.net/npm/d3@7"></script>
<script src="https://cdn.jsdelivr.net/npm/d3-sankey@0.12"></script>
<script>
const PANELS = __PANELS_JSON__;
const COLUMN_LABELS = __COLUMN_LABELS_JSON__;

const tip = d3.select("#tip");
function showTip(html, evt) {
  tip.html(html)
     .style("left", (evt.pageX + 14) + "px")
     .style("top", (evt.pageY + 14) + "px")
     .style("opacity", 1);
}
function hideTip() { tip.style("opacity", 0); }

function renderPanel(panel, container) {
  if (!panel.links || !panel.links.length) return;   // nothing to draw
  const W = panel.width || 2600;
  // PAD_T raised from 36 → 56 so the column-axis labels ("Mining",
  // "Beneficiation", ...) get breathing room above the node rectangles
  // and don't crowd against the panel subtitle.
  const PAD_L = 30, PAD_R = panel.width ? 220 : 320, PAD_T = 56, PAD_B = 64;
  const H = panel.height || 1100;

  const root = d3.select(container).append("div").attr("class", "panel");
  root.append("p").attr("class", "panel-title").text(panel.title);
  if (panel.subtitle) root.append("p").attr("class", "panel-sub").text(panel.subtitle);
  const svg = root.append("svg").attr("width", W).attr("height", H);

  const innerW = W - PAD_L - PAD_R;
  const innerH = H - PAD_T - PAD_B;
  const g = svg.append("g").attr("transform", `translate(${PAD_L},${PAD_T})`);

  // Build d3-sankey graph
  const nodes = panel.nodes.map(n => ({...n}));
  const links = panel.links.map(l => ({...l}));

  const sankey = d3.sankey()
    .nodeId(d => d.id)
    .nodeWidth(18)
    .nodePadding(18)
    .nodeAlign(d => d.rank)        // <<< pin every node to its assigned column
    .iterations(32)
    .extent([[0, 0], [innerW, innerH]]);

  // Augment links with source/target object refs (d3-sankey wants ids OR objects)
  const graph = sankey({nodes: nodes, links: links});

  // Sort nodes within each column: cathode-track top, anode-track bottom,
  // others in middle. Within a track, sort by REGION (stable global order)
  // so that the same geography (e.g. United States) sits at the same
  // vertical band across every column — this stops US/PL/CN swapping
  // places between Mining → Beneficiation → Chem Processing → ... and
  // eliminates the snake-like crossing of links.
  // Region rank: lower = higher up. Western Hemisphere first, then EU,
  // then Asia, then commodity-mining regions (AU/BR/CL/AR/etc.).
  const REGION_RANK = {
    "US": 0, "CA": 1, "MX": 2,
    "GB": 5, "IE": 6, "NO": 7, "SE": 8, "FI": 9, "DK": 10,
    "DE": 12, "FR": 13, "BE": 14, "NL": 15, "LU": 16, "AT": 17,
    "ES": 18, "PT": 19, "IT": 20,
    "PL": 22, "CZ": 23, "SK": 24, "HU": 25, "RO": 26, "BG": 27,
    "HR": 28, "SI": 29, "EE": 30, "LV": 31, "LT": 32, "GR": 33,
    "EU": 35,
    "TR": 38, "RU": 40, "IL": 41, "JO": 42, "MA": 43, "ZA": 44,
    "GA": 45, "GH": 46, "MZ": 47, "MG": 48, "TZ": 49, "CD": 50,
    "KR": 60, "JP": 61, "TW": 62, "VN": 63, "IN": 64, "ID": 65, "PH": 66,
    "CN": 70,
    "AU": 80, "NZ": 81,
    "BR": 90, "CL": 91, "AR": 92, "PE": 93, "ZW": 94,
    "OTHER": 999, "ROW": 999, "OTH": 999,
  };
  function regionFromId(nid) {
    // Composite IDs end with "_<REGION>" (e.g. "electrode_mfg_US",
    // "chem_processing_current_collector_PL"). Anode track has form
    // "anode_mfg_anode_CN" so strip a leading "anode_" if present.
    const parts = String(nid || "").split("_");
    if (parts.length < 2) return "";
    let last = parts[parts.length - 1].toUpperCase();
    // App nodes (app_xxx) don't carry regions — keep them out of the rank
    if (String(nid).startsWith("app_")) return "";
    // Region tokens are 2-3 letter uppercase
    if (last.length >= 2 && last.length <= 3 && /^[A-Z]+$/.test(last)) return last;
    return "";
  }
  function regionRank(nid) {
    const r = regionFromId(nid);
    if (!r) return 1000;  // unattributed nodes (no region) at bottom
    return (r in REGION_RANK) ? REGION_RANK[r] : 500;  // unknown region: middle
  }

  // Material category rank: lower = higher up. Same-material nodes cluster
  // together vertically across columns so a flow like
  // "iron-phosphorus mining → iron-phosphorus chem processing → iron-
  // phosphorus precursor" doesn't have to arc up and down between columns.
  // Without this, the same-region-different-material nodes get sorted by
  // mass per column and end up at very different y-positions, producing
  // long looping ribbons that misread as data anomalies.
  const PROCESS_STEPS = [
    "chem_processing", "precursor_mfg", "anode_mfg", "cam_mfg",
    "module_assembly", "pack_mfg", "electrode_mfg", "cell_mfg",
    "mining", "beneficiation", "application", "eol_recycling", "waste",
  ];
  const MATERIAL_RANK = {
    "lithium": 0,
    "iron_phosphorus": 1,
    "phosphorus": 2,
    "nickel": 3,
    "cobalt": 4,
    "manganese": 5,
    "precursor": 6,
    "cam": 7,
    "current_collector": 8,
    "electrolyte": 9,
    "separator": 10,
    "anode": 11,
    "loss": 90,
    "energy": 91,
    "water": 92,
    "other": 99,
  };
  function categoryFromId(nid) {
    let s = String(nid || "");
    for (const step of PROCESS_STEPS) {
      if (s.startsWith(step + "_")) {
        s = s.slice(step.length + 1);
        break;
      }
    }
    // s is now like "iron_phosphorus_CN" or "lithium_AU" or "anode_CN"
    // Strip a trailing 2-3-letter uppercase region token.
    const parts = s.split("_");
    if (parts.length > 1) {
      const last = parts[parts.length - 1];
      if (last.length >= 2 && last.length <= 3 && /^[A-Z]+$/.test(last)) {
        parts.pop();
      }
    }
    return parts.join("_");
  }
  function materialRank(nid) {
    const c = categoryFromId(nid);
    return (c in MATERIAL_RANK) ? MATERIAL_RANK[c] : 50;
  }

  const byCol = d3.group(graph.nodes, d => d.rank);
  byCol.forEach((arr, rank) => {
    arr.forEach(n => {
      const inSum = (n.targetLinks || []).reduce((s, l) => s + l.value, 0);
      const outSum = (n.sourceLinks || []).reduce((s, l) => s + l.value, 0);
      n._mass = Math.max(inSum, outSum);
      n._regionRank = regionRank(n.id);
      n._materialRank = materialRank(n.id);
    });
    arr.sort((a, b) => {
      const tA = a.track || "mid", tB = b.track || "mid";
      const trackOrder = {cathode: 0, mid: 1, anode: 2};
      if (trackOrder[tA] !== trackOrder[tB]) return trackOrder[tA] - trackOrder[tB];
      // Sort by MATERIAL CATEGORY first within a track, so same-material
      // nodes (e.g. iron_phosphorus across mining/beneficiation/chem_processing)
      // sit at the same vertical band across columns.
      if (a._materialRank !== b._materialRank) return a._materialRank - b._materialRank;
      // Within a material group, sort by region (US top, CN bottom).
      if (a._regionRank !== b._regionRank) return a._regionRank - b._regionRank;
      // Tiebreak by mass.
      return b._mass - a._mass;
    });
    // Re-assign y0/y1 based on the new sort order, packing into the column
    const total = arr.reduce((s, n) => s + (n.y1 - n.y0), 0);
    const slack = innerH - total;
    const gap = arr.length > 1 ? slack / (arr.length - 1) : 0;
    let y = 0;
    for (const n of arr) {
      const h = n.y1 - n.y0;
      n.y0 = y;
      n.y1 = y + h;
      y += h + gap;
    }
    // Re-route source/target endpoints
    arr.forEach(n => {
      if (n.sourceLinks) n.sourceLinks.forEach(l => l.y0 = (l.source.y0 + l.source.y1) / 2);
      if (n.targetLinks) n.targetLinks.forEach(l => l.y1 = (l.target.y0 + l.target.y1) / 2);
    });
  });
  // Recompute link y endpoints from updated node positions
  graph.nodes.forEach(n => {
    let y = n.y0;
    (n.sourceLinks || []).sort((a, b) => a.target.y0 - b.target.y0).forEach(l => {
      l.y0 = y + l.width / 2;
      y += l.width;
    });
    y = n.y0;
    (n.targetLinks || []).sort((a, b) => a.source.y0 - b.source.y0).forEach(l => {
      l.y1 = y + l.width / 2;
      y += l.width;
    });
  });

  // Column axis — draw light vertical guides + step labels at the top
  const colAxis = g.append("g").attr("class", "col-axis");
  const ranks = Array.from(byCol.keys()).sort((a, b) => a - b);
  const xByRank = new Map();
  ranks.forEach(r => {
    const sample = byCol.get(r)[0];
    xByRank.set(r, (sample.x0 + sample.x1) / 2);
  });
  ranks.forEach(r => {
    const x = xByRank.get(r);
    colAxis.append("line").attr("x1", x).attr("x2", x).attr("y1", -8).attr("y2", innerH);
    colAxis.append("text")
      .attr("x", x).attr("y", -14).attr("text-anchor", "middle")
      .text(COLUMN_LABELS[r] || "");
  });

  // Links
  g.append("g").attr("fill", "none")
    .selectAll("path").data(graph.links).join("path")
    .attr("class", "link")
    .attr("d", d3.sankeyLinkHorizontal())
    .attr("stroke", d => d.color || "#888")
    .attr("stroke-width", d => Math.max(1, d.width))
    .on("mousemove", function(evt, d) { showTip(d.hover || (d.value.toFixed(2) + " " + (panel.unit || "")), evt); })
    .on("mouseout", hideTip);

  // Nodes
  const nodeG = g.append("g").selectAll("g").data(graph.nodes).join("g")
    .attr("class", "node");
  nodeG.append("rect")
    .attr("x", d => d.x0).attr("y", d => d.y0)
    .attr("height", d => Math.max(2, d.y1 - d.y0))
    .attr("width", d => d.x1 - d.x0)
    .attr("fill", d => d.color || "#555")
    .on("mousemove", function(evt, d) { showTip(d.hover || d.label, evt); })
    .on("mouseout", hideTip);
  // Label placement. Labels are placed next to their node, then a global
  // pass moves overlapping labels apart. The pass is global (not per column)
  // because a long label can run into the next column's labels, and it clamps
  // labels inside the plot so none can sit on top of the column headers.
  // Long lines are cut with an ellipsis; the full text stays in the tooltip.
  const LH = 12;                       // line-height in px
  const MIN_GAP = 6;                   // min vertical clearance between labels
  const CHAR_W = 6.2;                  // approx px per character at 11 px
  const MAX_CH = 40;                   // longest label line before the ellipsis
  const lastColRank = Math.max(...graph.nodes.map(n => n.rank));
  const labelData = graph.nodes.map(d => {
    const lines = (d.label || "").split("\\n")
      .map(s => s.length > MAX_CH ? s.slice(0, MAX_CH - 1) + "\u2026" : s);
    const onLeftOfCol = d.x0 < innerW / 2;
    // Rightmost-column label goes to the RIGHT of the rect (PAD_R is reserved
    // for it). Other right-half labels go left of their rect.
    const isLast = (d.rank === lastColRank);
    const xAnchor = isLast ? d.x1 + 8
                           : onLeftOfCol ? d.x1 + 6 : d.x0 - 6;
    const textAnchor = isLast ? "start"
                              : onLeftOfCol ? "start" : "end";
    const yIdeal = (d.y0 + d.y1) / 2;
    const totalH = lines.length * LH;
    const w = Math.max(...lines.map(s => s.length)) * CHAR_W;
    const xa = textAnchor === "start" ? xAnchor : xAnchor - w;
    return {d, lines, xAnchor, textAnchor, yIdeal, totalH, yPlaced: yIdeal,
            xa, xb: xa + w};
  });
  const clampY = lab => {
    const lo = lab.totalH / 2 + 2, hi = innerH - lab.totalH / 2;
    lab.yPlaced = Math.min(Math.max(lab.yPlaced, lo), Math.max(lo, hi));
  };
  const collide = (A, B) =>
    A.xa < B.xb && B.xa < A.xb &&
    Math.abs(A.yPlaced - B.yPlaced) < (A.totalH + B.totalH) / 2 + MIN_GAP;
  labelData.forEach(clampY);
  for (let iter = 0; iter < 40; iter++) {
    let moved = false;
    const sorted = labelData.slice().sort((a, b) => a.yPlaced - b.yPlaced);
    // forward: push the lower label of a colliding pair down
    for (let i = 0; i < sorted.length; i++) {
      for (let j = i + 1; j < sorted.length; j++) {
        const A = sorted[i], B = sorted[j];
        if (collide(A, B)) {
          B.yPlaced = A.yPlaced + (A.totalH + B.totalH) / 2 + MIN_GAP;
          moved = true;
        }
      }
    }
    labelData.forEach(clampY);
    // backward: anything pushed past the bottom edge pushes its neighbours up
    const rev = labelData.slice().sort((a, b) => b.yPlaced - a.yPlaced);
    for (let i = 0; i < rev.length; i++) {
      for (let j = i + 1; j < rev.length; j++) {
        const A = rev[i], B = rev[j];
        if (collide(A, B)) {
          B.yPlaced = A.yPlaced - (A.totalH + B.totalH) / 2 - MIN_GAP;
          moved = true;
        }
      }
    }
    labelData.forEach(clampY);
    if (!moved) break;
  }

  // Draw leader lines for labels that moved away from their node
  const leadersG = g.append("g").attr("class", "leaders").attr("fill", "none")
    .attr("stroke", "#bbb").attr("stroke-width", 0.6);
  labelData.forEach(lab => {
    if (Math.abs(lab.yPlaced - lab.yIdeal) > 4) {
      leadersG.append("line")
        .attr("x1", (lab.d.x0 + lab.d.x1) / 2)
        .attr("y1", lab.yIdeal)
        .attr("x2", lab.xAnchor + (lab.textAnchor === "start" ? -2 : 2))
        .attr("y2", lab.yPlaced);
    }
  });

  // Multi-line labels via tspans, placed at yPlaced
  nodeG.append("text")
    .attr("class", "node-label")
    .each(function(d) {
      const lab = labelData.find(L => L.d === d);
      const sel = d3.select(this);
      const startY = lab.yPlaced - ((lab.lines.length - 1) * LH) / 2;
      lab.lines.forEach((ln, i) => {
        sel.append("tspan")
          .attr("x", lab.xAnchor)
          .attr("y", startY + i * LH)
          .attr("text-anchor", lab.textAnchor)
          .text(ln);
      });
    });
}

PANELS.forEach(p => renderPanel(p, "#panels"));
</script>
</body>
</html>
"""


def _node_to_d3(idx: int, trace, refs_lookup) -> dict:
    """Build a d3-friendly node dict from the SankeyTrace at index idx."""
    nid = trace.nodes[idx]
    label = trace.node_labels[idx]
    hover = trace.node_hover[idx]
    rank = refs_lookup["process_rank"](nid)
    color = (trace.node_colors[idx]
             if trace.node_colors and idx < len(trace.node_colors)
             else "#555")
    # Track membership for in-column sort: cathode top, anode bottom.
    is_anode = (nid.startswith("anode_mfg") or "_anode_" in nid or nid.endswith("_anode"))
    is_cathode = (nid.startswith("cam_mfg") or nid == "cam_mfg"
                  or nid.startswith("precursor_mfg") or nid == "precursor_mfg")
    track = "anode" if is_anode else ("cathode" if is_cathode else "mid")
    return {
        "id": nid,
        "label": label.replace("<br>", "\n"),
        "hover": hover,
        "rank": int(rank),
        "color": color,
        "track": track,
    }


def _link_to_d3(i: int, trace) -> dict:
    return {
        "source": trace.nodes[trace.sources[i]],
        "target": trace.nodes[trace.targets[i]],
        "value": float(trace.values[i]),
        "raw_value": float(trace.raw_values[i]) if i < len(trace.raw_values) else float(trace.values[i]),
        "color": trace.link_colors[i] if i < len(trace.link_colors) else "#888",
        "hover": trace.link_hover[i] if i < len(trace.link_hover) else "",
    }


def trace_to_panel(trace, title: str, height: int, process_rank, subtitle: str = "") -> dict:
    """Convert one SankeyTrace into the JSON shape consumed by the d3 frontend."""
    refs_lookup = {"process_rank": process_rank}
    nodes = [_node_to_d3(i, trace, refs_lookup) for i in range(len(trace.nodes))]
    links = [_link_to_d3(i, trace) for i in range(len(trace.values))]
    return {
        "title": title,
        "subtitle": subtitle,
        "unit": trace.unit,
        "height": height,
        "nodes": nodes,
        "links": links,
    }


def render_html(title: str, subtitle: str, panels: list[dict], column_labels: dict[int, str]) -> str:
    """Render the final self-contained HTML."""
    return (D3_HTML_TEMPLATE
            .replace("__TITLE__", title)
            .replace("__SUBTITLE__", subtitle)
            .replace("__PANELS_JSON__", json.dumps(panels))
            .replace("__COLUMN_LABELS_JSON__", json.dumps({str(k): v for k, v in column_labels.items()})))
