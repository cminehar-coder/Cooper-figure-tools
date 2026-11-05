#!/usr/bin/env python3
"""
render_sankey.py — v1 Sankey renderer for the battery material-flows project.

Reads a route YAML (e.g. a route YAML next to schema/ and sources/) plus the shared
materials registry and references library, validates the data, and emits an
interactive HTML Sankey diagram. Mass flows (kg/kWh) and energy/water flows
(MJ/kWh, L/kWh) are rendered as paired Plotly Sankey subplots so they share
node layout but are not visually conflated by unit.

Strategy (Option 1, region-split nodes):
  * The standard process steps (mining, beneficiation, ..., waste) define a
    rough column ordering.
  * Each flow's region_breakdown is unrolled into composite nodes
    "<step>_<material-category>_<region>". F090's application_breakdown is
    instead unrolled into "app_<category>" nodes.
  * Material category drives flow color (looked up from materials_registry).

CLI:
    python -m figure_tools.materialflow.render_sankey --route <route.yaml> \\
        --out out.html [--log-scale] [--material lithium] \\
        [--no-region-split] [--energy-water-only] [--validate]

Pure stdlib + pyyaml + plotly. No pandas, no networkx.
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


# --------------------------------------------------------------------------- #
# Hard-fail import guards: print a clear message instead of a stack trace.
# --------------------------------------------------------------------------- #
try:
    import yaml  # type: ignore[import-untyped]
except ImportError:
    sys.stderr.write(
        "ERROR: PyYAML is not installed. Install it with:\n"
        "    python3 -m pip install pyyaml\n"
    )
    sys.exit(2)

try:
    import plotly.graph_objects as go  # type: ignore[import-untyped]
    import plotly.io as pio  # type: ignore[import-untyped]
    from plotly.subplots import make_subplots  # type: ignore[import-untyped]
except ImportError:
    sys.stderr.write(
        "ERROR: plotly is not installed. Install it with:\n"
        "    python3 -m pip install plotly\n"
    )
    sys.exit(2)


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

# Process-step ordering used as the row dimension (column position in Sankey).
PROCESS_ORDER: list[str] = [
    "mining",
    "beneficiation",
    "chem_processing",
    "precursor_mfg",
    "cam_mfg",
    "anode_mfg",
    "electrode_mfg",
    "cell_mfg",
    "module_assembly",
    "pack_mfg",
    "application",
    "eol_recycling",
    "waste",
]

# Material-category color palette (rgba so we can vary opacity per flow).
CATEGORY_COLORS: dict[str, str] = {
    "lithium":           "#F5C300",  # yellow
    "nickel":            "#4CAF50",  # green
    "cobalt":            "#E91E63",  # pink
    "manganese":         "#9C27B0",  # purple
    "iron_phosphorus":   "#E53935",  # red
    "phosphorus":        "#FB8C00",  # orange (back-compat alias)
    "anode":             "#424242",  # dark gray (graphite)
    "electrolyte":       "#1E88E5",  # blue
    "current_collector": "#795548",  # brown (Al)
    "separator":         "#81D4FA",  # light blue
    "precursor":         "#7E57C2",  # mid-purple (PCAM intermediates)
    "cam":               "#FF7043",  # warm orange (CAM intermediates)
    "recycled":          "#A5D6A7",  # light green
    "loss":              "#212121",  # near-black for waste/loss
    "energy":            "#FFB300",  # amber
    "water":             "#26C6DA",  # cyan
    "other":             "#90A4AE",  # blue-gray fallback
}

# Special override: Cu foil should render copper, not generic brown.
CU_FOIL_COLOR = "#B87333"

# Tolerances
SHARE_TOLERANCE_PCT = 0.5

# Energy / water units recognised on flows.
ENERGY_UNITS = {"MJ_per_kWh", "MJ_per_pack"}
WATER_UNITS = {"L_per_kWh", "L_per_pack"}

# Pretty-label overrides for application nodes (populated during build).
# Keyed by composite node id (e.g. "app_passenger_ev_premium_long_range").
_APP_LABEL_OVERRIDES: dict[str, str] = {}


# --------------------------------------------------------------------------- #
# Dataclasses
# --------------------------------------------------------------------------- #

@dataclass
class Flow:
    """One flow record from a route YAML, with raw fields preserved."""
    id: str
    from_node: str
    to_node: str
    material: str
    material_id: Optional[str]
    value: Optional[float]
    unit: str
    region_breakdown: Optional[list[dict[str, Any]]]
    application_breakdown: Optional[list[dict[str, Any]]]
    companies: Optional[list[str]]
    source_ids: list[str]
    discrepancy_flags: Optional[list[dict[str, Any]]]
    is_loss: bool
    is_recycled: bool
    verified: bool
    notes: Optional[str]


@dataclass
class RouteData:
    """A loaded route YAML with normalised flows."""
    meta: dict[str, Any]
    nodes: list[dict[str, Any]]
    flows: list[Flow]
    raw: dict[str, Any]


@dataclass
class Options:
    """CLI options bag, passed down through the pipeline."""
    route_path: Optional[Path]
    out_path: Path
    log_scale: bool = False
    material_filter: Optional[str] = None
    no_region_split: bool = False
    energy_water_only: bool = False
    validate: bool = False
    # Vehicle mode (optional): renders one or more trims of a named vehicle
    # at pack-level kg, filtering route flows to regions actually in the chain.
    vehicle: Optional[str] = None
    supply_chain_path: Optional[Path] = None
    renderer: str = "plotly"  # "plotly" | "d3"


@dataclass
class SankeyTrace:
    """Resolved Sankey data for a single Plotly trace."""
    label: str               # e.g. "Mass flows (kg/kWh)"
    unit: str                # e.g. "kg/kWh"
    nodes: list[str]         # composite node IDs
    node_labels: list[str]   # display labels
    node_hover: list[str]    # per-node hover html
    node_x: list[float]      # explicit x-coordinates (process column position)
    node_y: list[float]      # explicit y-coordinates (vertical placement)
    sources: list[int]       # link source-index list
    targets: list[int]       # link target-index list
    values: list[float]      # link plotted values (after log if requested)
    raw_values: list[float]  # link raw values (for hover)
    link_colors: list[str]
    link_hover: list[str]
    node_colors: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #

def load_yaml(path: Path) -> dict[str, Any]:
    """Load a single YAML file and return the parsed dict."""
    if not path.exists():
        sys.stderr.write(f"ERROR: file not found: {path}\n")
        sys.exit(2)
    with path.open("r") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        sys.stderr.write(f"ERROR: {path} did not parse as a YAML mapping.\n")
        sys.exit(2)
    return data


def _coerce_float(x: Any) -> Optional[float]:
    """Return float(x) or None if x is None / unparseable."""
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def parse_flow(raw: dict[str, Any]) -> Flow:
    """Convert a raw flow dict into a Flow dataclass with safe defaults."""
    return Flow(
        id=str(raw.get("id", "")),
        from_node=str(raw.get("from", "")),
        to_node=str(raw.get("to", "")),
        material=str(raw.get("material", "") or ""),
        material_id=raw.get("material_id"),
        value=_coerce_float(raw.get("value")),
        unit=str(raw.get("unit", "kg_per_kWh")),
        region_breakdown=raw.get("region_breakdown"),
        application_breakdown=raw.get("application_breakdown"),
        companies=raw.get("companies"),
        source_ids=list(raw.get("source_ids") or []),
        discrepancy_flags=raw.get("discrepancy_flags"),
        is_loss=bool(raw.get("is_loss", False)),
        is_recycled=bool(raw.get("is_recycled", False)),
        verified=bool(raw.get("verified", False)),
        notes=raw.get("notes"),
    )


def load_route(path: Path) -> RouteData:
    """Load a route YAML and parse all flows."""
    raw = load_yaml(path)
    flows = [parse_flow(f) for f in (raw.get("flows") or [])]
    return RouteData(
        meta=raw.get("meta") or {},
        nodes=list(raw.get("nodes") or []),
        flows=flows,
        raw=raw,
    )


def load_materials_registry(path: Path) -> dict[str, dict[str, Any]]:
    """Index materials registry by id."""
    raw = load_yaml(path)
    out: dict[str, dict[str, Any]] = {}
    for m in (raw.get("materials") or []):
        mid = m.get("id")
        if mid:
            out[str(mid)] = m
    return out


def load_references(path: Path) -> dict[str, dict[str, Any]]:
    """Index references by id."""
    raw = load_yaml(path)
    out: dict[str, dict[str, Any]] = {}
    for s in (raw.get("sources") or []):
        sid = s.get("id")
        if sid:
            out[str(sid)] = s
    return out


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

def validate(
    route: RouteData,
    materials: dict[str, dict[str, Any]],
    refs: dict[str, dict[str, Any]],
) -> list[str]:
    """Pre-flight check the route. Returns list of warning messages."""
    warnings: list[str] = []
    defined_nodes = {n.get("id") for n in route.nodes}

    for flow in route.flows:
        prefix = f"[{flow.id}]"

        # 1. Region share sum
        if flow.region_breakdown:
            total = sum(_coerce_float(r.get("share_pct")) or 0.0
                        for r in flow.region_breakdown)
            if abs(total - 100.0) > SHARE_TOLERANCE_PCT:
                warnings.append(
                    f"{prefix} region_breakdown share_pct sums to "
                    f"{total:.2f} (expected 100 ± {SHARE_TOLERANCE_PCT})"
                )

        # 2. Application share sum
        if flow.application_breakdown:
            total = sum(_coerce_float(r.get("share_pct")) or 0.0
                        for r in flow.application_breakdown)
            if abs(total - 100.0) > SHARE_TOLERANCE_PCT:
                warnings.append(
                    f"{prefix} application_breakdown share_pct sums to "
                    f"{total:.2f} (expected 100 ± {SHARE_TOLERANCE_PCT})"
                )

        # 3. source_ids resolve
        for sid in flow.source_ids:
            if sid not in refs:
                warnings.append(f"{prefix} unknown source_id: {sid}")
        if flow.region_breakdown:
            for r in flow.region_breakdown:
                for sid in (r.get("source_ids") or []):
                    if sid not in refs:
                        warnings.append(
                            f"{prefix} region {r.get('region')} unknown "
                            f"source_id: {sid}"
                        )

        # 4. Orphan node refs
        if flow.from_node and flow.from_node not in defined_nodes:
            warnings.append(
                f"{prefix} from-node '{flow.from_node}' not defined in nodes:"
            )
        if flow.to_node and flow.to_node not in defined_nodes:
            warnings.append(
                f"{prefix} to-node '{flow.to_node}' not defined in nodes:"
            )

        # 5. value: null + verified: true conflict
        if flow.value is None and flow.verified:
            warnings.append(f"{prefix} value is null but verified=true")

        # 6. material_id resolves (if set)
        if flow.material_id and flow.material_id not in materials:
            # Some flows use ad-hoc material_ids (ELECTRICITY, WATER,
            # PROCESS_WATER, CO_CUORE, MN_ELEMENTAL, GRAPHITE_ORE) that aren't
            # in the registry yet. Surface as a warning, not an error.
            warnings.append(
                f"{prefix} material_id '{flow.material_id}' not in "
                f"materials registry"
            )

    return warnings


# --------------------------------------------------------------------------- #
# Material-category and color resolution
# --------------------------------------------------------------------------- #

def _category_for_flow(
    flow: Flow,
    materials: dict[str, dict[str, Any]],
) -> str:
    """Return a category key suitable for CATEGORY_COLORS lookup."""
    # Energy / water by unit
    if flow.unit in ENERGY_UNITS:
        return "energy"
    if flow.unit in WATER_UNITS:
        return "water"
    # Loss flows render dark
    if flow.is_loss:
        return "loss"
    # Look up category from registry
    if flow.material_id and flow.material_id in materials:
        cat = materials[flow.material_id].get("category")
        if cat:
            return str(cat)
    # Fallbacks based on free-text material name
    name = (flow.material or "").lower()
    if "graphite" in name or "anode" in name:
        return "anode"
    if "lithium" in name or "lioh" in name or "li2co3" in name:
        return "lithium"
    if "nickel" in name or "niso4" in name:
        return "nickel"
    if "cobalt" in name or "coso4" in name:
        return "cobalt"
    if "manganese" in name or "mnso4" in name:
        return "manganese"
    if "phosphate" in name or "phosphoric" in name or "lfp" in name:
        return "iron_phosphorus"
    if "electrolyte" in name or "lipf6" in name:
        return "electrolyte"
    if "separator" in name:
        return "separator"
    if "foil" in name or "current collector" in name:
        return "current_collector"
    return "other"


def _flow_color(category: str, flow: Flow) -> str:
    """Resolve final flow color, applying recycled / Cu-foil overrides."""
    if flow.is_recycled:
        return CATEGORY_COLORS["recycled"]
    if category == "current_collector" and "cu" in (flow.material or "").lower():
        return CU_FOIL_COLOR
    return CATEGORY_COLORS.get(category, CATEGORY_COLORS["other"])


def _hex_to_rgba(hex_color: str, alpha: float) -> str:
    """Convert '#RRGGBB' to 'rgba(r,g,b,a)' for translucent links."""
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return hex_color
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{alpha:.2f})"


# --------------------------------------------------------------------------- #
# Hover-text helpers
# --------------------------------------------------------------------------- #

def _short_cite(sid: str, refs: dict[str, dict[str, Any]]) -> str:
    """Render a short citation label like 'IEA CMR 2023'."""
    if sid not in refs:
        return sid
    ref = refs[sid]
    author = str(ref.get("author") or "").split(",")[0].strip() or sid
    year = ref.get("year") or ""
    # Trim long org names
    author = re.sub(r"\(.*?\)", "", author).strip()
    return f"{author} {year}".strip()


# --------------------------------------------------------------------------- #
# Reference numbering — assigns a stable integer to each cited ref id so that
# numerical values in the rendered Sankey can carry superscript citations
# (e.g. "0.681 kg/kWh<sup>3,7</sup>"). The bibliography artefact emitted next
# to the HTML lists each number with its full citation.
# Reset by render_html() at the start of every render so numbering starts at 1.
# --------------------------------------------------------------------------- #

_REF_NUMBER_MAP: dict[str, int] = {}


def _reset_ref_numbers() -> None:
    """Clear the global ref-number map. Call once per render."""
    _REF_NUMBER_MAP.clear()


def _ref_number(sid: str) -> int:
    """Return the integer assigned to this ref id, registering on first use."""
    if sid not in _REF_NUMBER_MAP:
        _REF_NUMBER_MAP[sid] = len(_REF_NUMBER_MAP) + 1
    return _REF_NUMBER_MAP[sid]


def _ref_superscript(source_ids: Optional[list[str]]) -> str:
    """Build "<sup>3,7</sup>" for a list of ref ids; empty string if none."""
    if not source_ids:
        return ""
    nums: list[str] = []
    seen: set[int] = set()
    for sid in source_ids:
        n = _ref_number(sid)
        if n not in seen:
            nums.append(str(n))
            seen.add(n)
    return f"<sup>{','.join(nums)}</sup>" if nums else ""


def _value_with_refs(value: Optional[float], unit_str: str,
                     source_ids: Optional[list[str]]) -> str:
    """Format a numeric value followed by a superscript citation."""
    if value is None:
        return ""
    return f"{value:g} {unit_str}{_ref_superscript(source_ids)}"


def write_bibliography(out_html_path: Path,
                       refs: dict[str, dict[str, Any]]) -> tuple[Path, Path]:
    """Emit <stem>_bibliography.md and <stem>_bibliography.csv next to the HTML.

    Lists every ref id that received a number during the render, in numeric
    order. Returns the (md_path, csv_path) pair for the caller to log.
    """
    if not _REF_NUMBER_MAP:
        return (out_html_path, out_html_path)
    items = sorted(_REF_NUMBER_MAP.items(), key=lambda kv: kv[1])

    md_lines: list[str] = []
    md_lines.append(f"# Bibliography — {out_html_path.stem}")
    md_lines.append("")
    md_lines.append(f"References cited in `{out_html_path.name}`. "
                    f"Superscript numbers in the Sankey hover tooltips refer "
                    f"to the entries below.")
    md_lines.append("")

    def _csv_escape(s: str) -> str:
        s = s.replace('"', '""')
        return f'"{s}"' if any(c in s for c in [',', '"', '\n']) else s

    csv_lines: list[str] = [
        "ref_number,ref_id,type,author,year,title,doi,url,pages,notes",
    ]

    for sid, num in items:
        ref = refs.get(sid, {})
        author = str(ref.get("author") or "Unknown").strip()
        year = ref.get("year") or ""
        title = str(ref.get("title") or sid).strip()
        doi = str(ref.get("doi") or "").strip()
        url = str(ref.get("url") or "").strip()
        rtype = str(ref.get("type") or "").strip()
        pages = str(ref.get("pages") or "").strip()
        notes_raw = str(ref.get("notes") or "").strip()
        # Single-line notes for Markdown
        notes = " ".join(notes_raw.split())

        # Markdown entry
        cite = f"{num}. **{author}** ({year}). *{title}*."
        extras: list[str] = []
        if doi:
            extras.append(f"DOI: [{doi}](https://doi.org/{doi})")
        if url:
            extras.append(f"URL: <{url}>")
        if pages:
            extras.append(f"pp. {pages}")
        if extras:
            cite += " " + " · ".join(extras)
        cite += f"  \n   _id: `{sid}`"
        if notes:
            # Truncate long notes for readability
            nshort = notes if len(notes) <= 280 else notes[:277] + "..."
            cite += f"  \n   _Note: {nshort}_"
        md_lines.append(cite)
        md_lines.append("")

        csv_lines.append(",".join([
            str(num),
            _csv_escape(sid),
            _csv_escape(rtype),
            _csv_escape(author),
            str(year),
            _csv_escape(title),
            _csv_escape(doi),
            _csv_escape(url),
            _csv_escape(pages),
            _csv_escape(notes),
        ]))

    md_path = out_html_path.with_name(out_html_path.stem + "_bibliography.md")
    csv_path = out_html_path.with_name(out_html_path.stem + "_bibliography.csv")
    md_path.write_text("\n".join(md_lines), encoding="utf-8")
    csv_path.write_text("\n".join(csv_lines), encoding="utf-8")
    return (md_path, csv_path)


def _flow_hover(
    flow: Flow,
    region: Optional[str],
    region_share: Optional[float],
    region_companies: Optional[list[str]],
    region_value: Optional[float],
    refs: dict[str, dict[str, Any]],
    region_source_ids: Optional[list[str]] = None,
) -> str:
    """Build the hover-template HTML for a single Sankey link.

    Numerical values carry inline superscript citations (e.g. "0.681
    kg/kWh^3,7^") that key into the bibliography file emitted alongside the
    HTML. Region-specific values use region.source_ids if present so that the
    superscript reflects the source supporting that particular regional share.
    """
    unit_str = flow.unit.replace("_per_", "/")
    lines: list[str] = []
    lines.append(f"<b>{flow.id} — {flow.material}</b>")
    if flow.value is not None:
        lines.append(
            f"Total: {_value_with_refs(flow.value, unit_str, flow.source_ids)}"
        )
    if region:
        share_str = ""
        if region_share is not None:
            share_str = (f" ({region_share:.1f}%"
                         f"{_ref_superscript(region_source_ids)})")
        val_str = ""
        if region_value is not None:
            # Prefer region.source_ids for the regional value when present;
            # fall back to flow-level sources so the superscript is never empty.
            rsids = region_source_ids or flow.source_ids
            val_str = f" — {_value_with_refs(region_value, unit_str, rsids)}"
        lines.append(f"Region: <b>{region}</b>{share_str}{val_str}")
    companies = region_companies or flow.companies or []
    if companies:
        top = ", ".join(str(c) for c in companies[:5])
        more = "" if len(companies) <= 5 else f" (+{len(companies)-5} more)"
        lines.append(f"Companies: {top}{more}")
    if flow.source_ids:
        # Show short cites + the assigned reference numbers so the user can
        # cross-reference the bibliography entries.
        parts = []
        for s in flow.source_ids[:6]:
            parts.append(f"[{_ref_number(s)}] {_short_cite(s, refs)}")
        lines.append("Sources: " + "; ".join(parts))
    if flow.discrepancy_flags:
        for d in flow.discrepancy_flags:
            t = d.get("type", "flag")
            status = d.get("status", "")
            lines.append(f"⚠ {t} ({status})")
    return "<br>".join(lines)


def _application_hover(cat_entry: dict[str, Any]) -> str:
    """Hover for an application_breakdown destination node."""
    lines = [f"<b>{cat_entry.get('category_label') or cat_entry.get('category')}</b>"]
    share = cat_entry.get("share_pct")
    if share is not None:
        lines.append(f"Share of pack: {share}%")
    examples = cat_entry.get("example_products") or []
    for ex in examples[:8]:
        oem = ex.get("oem", "?")
        model = ex.get("model", "?")
        year = ex.get("year", "")
        kwh = ex.get("pack_kWh", "")
        sw = ex.get("share_within_cat_pct", "")
        sw_str = f" — {sw}%" if sw != "" else ""
        lines.append(f"  • {oem} {model} ({year}, {kwh} kWh){sw_str}")
    if len(examples) > 8:
        lines.append(f"  …and {len(examples)-8} more")
    return "<br>".join(lines)


# --------------------------------------------------------------------------- #
# Sankey graph construction
# --------------------------------------------------------------------------- #

# Column rank override: cathode and anode tracks are PARALLEL (both feed
# electrode_mfg). Both should sit in the same column, not sequentially.
# This is a logical column index — cam_mfg and anode_mfg share column 5,
# both upstream of electrode_mfg at column 6.
COLUMN_RANK_OVERRIDE: dict[str, int] = {
    "mining":           0,
    "beneficiation":    1,
    "chem_processing":  2,
    "precursor_mfg":    3,
    "cam_mfg":          4,
    "anode_mfg":        4,   # parallel to cam_mfg
    "electrode_mfg":    5,
    "cell_mfg":         6,
    "module_assembly":  7,
    "pack_mfg":         8,
    "application":      9,
    "eol_recycling":   10,
    "waste":           10,
}


def _process_rank(node_id: str) -> int:
    """Return column rank of a process step (0=leftmost)."""
    # Try whole-id first, then strip suffix to find base process step.
    if node_id in COLUMN_RANK_OVERRIDE:
        return COLUMN_RANK_OVERRIDE[node_id]
    if node_id.startswith("app_"):
        return COLUMN_RANK_OVERRIDE["application"]
    # composite: <step>_<...>; pick the longest matching prefix.
    best_idx = -1
    best_len = -1
    for step, rank in COLUMN_RANK_OVERRIDE.items():
        if node_id.startswith(step + "_") and len(step) > best_len:
            best_idx = rank
            best_len = len(step)
    if best_idx >= 0:
        return best_idx
    return max(COLUMN_RANK_OVERRIDE.values()) + 1  # unknown → far right


def _slug(s: str) -> str:
    """Filesystem-safe slug for composite node IDs."""
    return re.sub(r"[^A-Za-z0-9]+", "_", str(s)).strip("_") or "x"


def _composite_node_id(
    step: str,
    flow: Flow,
    region: Optional[str],
    materials: dict[str, dict[str, Any]],
) -> str:
    """Build a composite node id like 'mining_lithium_AU' or 'cell_mfg_CN'."""
    cat = _category_for_flow(flow, materials)
    parts = [step]
    # Material category as a sub-discriminator: only at stages that handle
    # ONE material at a time (mining, beneficiation, chem_processing,
    # anode_mfg). At precursor_mfg and cam_mfg, multiple materials converge
    # into a single physical product (PCAM, CAM).
    material_specific_steps = {
        "mining", "beneficiation", "chem_processing", "anode_mfg",
    }
    # Region-pinned stages: stages whose node ID includes a per-region split.
    # Integration stages (precursor_mfg, cam_mfg) are NOT region-pinned —
    # they collapse to a single node per chemistry so multi-region upstream
    # flows can converge cleanly without producing orphan dead-ends.
    region_pinned_steps = {
        "mining", "beneficiation", "chem_processing", "anode_mfg",
        "electrode_mfg", "cell_mfg", "module_assembly", "pack_mfg",
        "application", "eol_recycling", "waste",
    }
    if step in material_specific_steps and cat not in {"loss", "energy", "water"}:
        parts.append(cat)
    if step in region_pinned_steps and region:
        parts.append(_slug(region))
    return "_".join(parts)


def _node_label(node_id: str, region: Optional[str] = None) -> str:
    """Compact label for a composite node id.

    Layout convention: process step is implicit by column position, so we
    keep labels short — material + region only (or step alone if no suffix).
    Examples:
      mining_lithium_AU       → "Li · AU"
      chem_processing_co_CN   → "Co · CN"
      cell_mfg_KR             → "Cell — KR"
      pack_mfg                → "Pack"
    """
    if node_id in _APP_LABEL_OVERRIDES:
        return _APP_LABEL_OVERRIDES[node_id]
    if node_id.startswith("app_"):
        rest = node_id[len("app_"):]
        return rest.replace("_", " ").title()

    # Match longest-prefix process step
    step = None
    for s in sorted(PROCESS_ORDER, key=len, reverse=True):
        if node_id == s or node_id.startswith(s + "_"):
            step = s
            break
    if step is None:
        return node_id
    suffix = node_id[len(step):].lstrip("_")

    # Full-name step labels (no abbreviations)
    STEP_FULL = {
        "mining": "Mining",
        "beneficiation": "Beneficiation",
        "chem_processing": "Chemical Processing",
        "precursor_mfg": "Precursor Manufacturing",
        "cam_mfg": "Cathode Active Material",
        "anode_mfg": "Anode Manufacturing",
        "electrode_mfg": "Electrode Manufacturing",
        "cell_mfg": "Cell Manufacturing",
        "module_assembly": "Module Assembly",
        "pack_mfg": "Pack Manufacturing",
        "application": "Application",
        "eol_recycling": "End-of-Life Recycling",
        "waste": "Waste",
    }
    # Full-name material labels (no abbreviations)
    MAT_FULL = {
        "lithium": "Lithium",
        "nickel": "Nickel",
        "cobalt": "Cobalt",
        "manganese": "Manganese",
        "anode": "Graphite Anode",
        "graphite": "Graphite",
        "iron_phosphorus": "Iron + Phosphorus",
        "iron": "Iron",
        "phosphorus": "Phosphorus",
        "electrolyte": "Electrolyte",
        "current_collector": "Current Collector",
        "separator": "Separator",
        "precursor": "Precursor",
        "cam": "Cathode Active Material",
        "energy": "Process Electricity",
        "water": "Process Water",
        "loss": "Process Loss",
        "other": "Other Material",
    }
    step_label = STEP_FULL.get(step, step.replace("_", " ").title())

    if not suffix:
        return step_label
    # Suffix is "<material>_<REGION>" or "<REGION>" or "<material>"
    bits = suffix.split("_")
    # Try to identify a region (uppercase 2–3 letter code at end)
    region_code = None
    if bits and len(bits[-1]) <= 3 and bits[-1].isupper():
        region_code = bits[-1]
        bits = bits[:-1]
    elif bits and bits[-1].lower() in {"other", "row"}:
        region_code = "Other"
        bits = bits[:-1]
    material = "_".join(bits) if bits else None
    mat_label = None
    if material:
        mat_label = MAT_FULL.get(material.lower(),
                                 material.replace("_", " ").title())

    # Multi-line label: process step on top, then material, then region.
    # Plotly Sankey accepts <br> for line breaks within node labels.
    parts = [step_label]
    if mat_label and mat_label.lower() != step_label.lower():
        parts.append(mat_label)
    if region_code:
        try:
            parts.append(_country_name(region_code))
        except NameError:
            parts.append(region_code)
    return "<br>".join(parts)


def _classify_traces(flows: list[Flow]) -> tuple[list[Flow], list[Flow]]:
    """Split flows into (mass, energy_water) buckets by unit."""
    mass: list[Flow] = []
    ew: list[Flow] = []
    for f in flows:
        if f.unit in ENERGY_UNITS or f.unit in WATER_UNITS:
            ew.append(f)
        else:
            mass.append(f)
    return mass, ew


def _filter_flows(flows: list[Flow], opts: Options,
                  materials: dict[str, dict[str, Any]]) -> list[Flow]:
    """Apply CLI filters (--material) and drop value-null flows."""
    out: list[Flow] = []
    needle = (opts.material_filter or "").strip().lower() or None
    for f in flows:
        if f.value is None or f.value <= 0:
            continue
        if needle:
            cat = _category_for_flow(f, materials)
            text = f"{cat} {f.material} {f.material_id or ''}".lower()
            if needle not in text:
                continue
        out.append(f)
    return out


def build_trace(
    flows: list[Flow],
    materials: dict[str, dict[str, Any]],
    refs: dict[str, dict[str, Any]],
    *,
    label: str,
    unit_label: str,
    log_scale: bool,
    no_region_split: bool,
) -> Optional[SankeyTrace]:
    """Resolve a list of flows into a single Sankey trace."""
    if not flows:
        return None

    node_ids: list[str] = []
    node_index: dict[str, int] = {}
    node_hover: dict[str, list[str]] = {}
    node_step: dict[str, str] = {}

    sources: list[int] = []
    targets: list[int] = []
    values: list[float] = []
    raw_values: list[float] = []
    link_colors: list[str] = []
    link_hover: list[str] = []

    def get_or_add_node(nid: str, step: str, hover_seed: str = "") -> int:
        if nid not in node_index:
            node_index[nid] = len(node_ids)
            node_ids.append(nid)
            node_step[nid] = step
            node_hover.setdefault(nid, [])
            if hover_seed:
                node_hover[nid].append(hover_seed)
        return node_index[nid]

    for flow in flows:
        category = _category_for_flow(flow, materials)
        base_color = _flow_color(category, flow)
        # Translucent links so dense areas don't go to mud.
        link_color = _hex_to_rgba(base_color, 0.55 if not flow.is_loss else 0.35)

        # Special: F090 with application_breakdown → application sub-nodes
        is_application_split = (
            flow.to_node == "application"
            and flow.application_breakdown
            and not no_region_split
        )

        # Region splitting for the source side of the link
        region_entries: list[dict[str, Any]]
        if flow.region_breakdown and not no_region_split:
            # Aggregate regions with share_pct < MIN_REGION_PCT into "Other"
            MIN_REGION_PCT = 8.0
            big_regions: list[dict[str, Any]] = []
            small_regions: list[dict[str, Any]] = []
            for r in flow.region_breakdown:
                share = _coerce_float(r.get("share_pct")) or 0
                rname = (r.get("region") or "").lower()
                if rname in {"other", "row"} or share < MIN_REGION_PCT:
                    small_regions.append(r)
                else:
                    big_regions.append(r)
            if small_regions:
                # Build a single "Other" entry that absorbs all small/ROW regions
                merged_share = 0.0
                merged_value = 0.0
                merged_companies: list[str] = []
                merged_sources: list[str] = list(flow.source_ids)
                for s in small_regions:
                    merged_share += _coerce_float(s.get("share_pct")) or 0
                    sv = _coerce_float(s.get("value"))
                    if sv is None and flow.value is not None:
                        sv = flow.value * (_coerce_float(s.get("share_pct")) or 0) / 100.0
                    merged_value += sv or 0
                    for c in (s.get("companies") or []):
                        if c not in merged_companies:
                            merged_companies.append(c)
                    for sid in (s.get("source_ids") or []):
                        if sid not in merged_sources:
                            merged_sources.append(sid)
                if merged_share > 0 or merged_value > 0:
                    big_regions.append({
                        "region": "Other",
                        "share_pct": merged_share,
                        "value": merged_value,
                        "companies": merged_companies,
                        "source_ids": merged_sources,
                    })
            region_entries = big_regions
        else:
            # Single aggregate edge: synthesize one entry
            region_entries = [{
                "region": None,
                "share_pct": 100.0,
                "value": flow.value,
                "companies": flow.companies,
                "source_ids": flow.source_ids,
            }]

        for r in region_entries:
            region = r.get("region")
            share = _coerce_float(r.get("share_pct"))
            r_value = _coerce_float(r.get("value"))
            if r_value is None and share is not None and flow.value is not None:
                r_value = flow.value * share / 100.0
            if r_value is None or r_value <= 0:
                continue
            r_companies = r.get("companies") or flow.companies
            r_source_ids = r.get("source_ids") or None

            # Source node id (region-split if requested & present)
            from_region = region if not no_region_split else None
            from_id = _composite_node_id(
                flow.from_node, flow, from_region, materials,
            )
            from_idx = get_or_add_node(from_id, flow.from_node)

            # Target node id
            if is_application_split:
                # F090 split into per-application nodes; ignore upstream region
                # on the destination side.
                for cat_entry in (flow.application_breakdown or []):
                    cat_share = _coerce_float(cat_entry.get("share_pct")) or 0
                    cat_value = _coerce_float(cat_entry.get("value"))
                    if cat_value is None and flow.value is not None:
                        cat_value = flow.value * cat_share / 100.0
                    if cat_value is None or cat_value <= 0:
                        continue
                    # Multiply by region share to attribute that slice
                    contrib = cat_value * (share or 100.0) / 100.0
                    if contrib <= 0:
                        continue
                    cat_slug = _slug(cat_entry.get("category") or "uncat")
                    to_id = f"app_{cat_slug}"
                    to_idx = get_or_add_node(to_id, "application")
                    node_hover[to_id] = [_application_hover(cat_entry)]
                    # Use the category_label as the display label if present
                    pretty = cat_entry.get("category_label")
                    if pretty:
                        _APP_LABEL_OVERRIDES[to_id] = str(pretty)

                    sources.append(from_idx)
                    targets.append(to_idx)
                    raw_values.append(contrib)
                    plotted = _maybe_log(contrib, log_scale)
                    values.append(plotted)
                    link_colors.append(link_color)
                    link_hover.append(_flow_hover(
                        flow, region, share, r_companies, contrib, refs,
                        region_source_ids=r_source_ids,
                    ))
                # Done with this region entry
                continue

            # Per-entry to_region override (used in vehicle mode for asymmetric
            # cross-region flows: e.g., Anode·China → Electrode·USA where the
            # cathode/anode plant is in a different country from the cell maker).
            to_region = region if not no_region_split else None
            to_region_override = r.get("to_region")
            if to_region_override and not no_region_split:
                to_region = to_region_override
            to_id = _composite_node_id(
                flow.to_node, flow, to_region, materials,
            )
            to_idx = get_or_add_node(to_id, flow.to_node)

            sources.append(from_idx)
            targets.append(to_idx)
            raw_values.append(r_value)
            values.append(_maybe_log(r_value, log_scale))
            link_colors.append(link_color)
            link_hover.append(_flow_hover(
                flow, region, share, r_companies, r_value, refs,
                region_source_ids=r_source_ids,
            ))

    # Build node hover strings: aggregate companies / mass through node
    in_total: dict[str, float] = {n: 0.0 for n in node_ids}
    out_total: dict[str, float] = {n: 0.0 for n in node_ids}
    for src, tgt, v in zip(sources, targets, raw_values):
        out_total[node_ids[src]] += v
        in_total[node_ids[tgt]] += v

    node_hover_strs: list[str] = []
    for nid in node_ids:
        seeded = node_hover.get(nid) or []
        lines = []
        if seeded:
            lines.extend(seeded)
        else:
            lines.append(f"<b>{_node_label(nid)}</b>")
        thru = max(in_total[nid], out_total[nid])
        if thru > 0:
            lines.append(f"Total through node: {thru:g} {unit_label}")
        node_hover_strs.append("<br>".join(lines))

    # Compute explicit x-coordinates: pin nodes to columns by process step.
    # Distribute ranks across [0.02, 0.98] so first/last column don't clip.
    n_steps = len(PROCESS_ORDER)
    # +1 because some flows go to 'waste' which is at PROCESS_ORDER index for waste
    ranks = [_process_rank(n) for n in node_ids]
    max_rank = max(ranks) if ranks else 1
    min_rank = min(ranks) if ranks else 0
    span = max(max_rank - min_rank, 1)
    x_left, x_right = 0.02, 0.98
    node_x = [
        x_left + (r - min_rank) / span * (x_right - x_left)
        for r in ranks
    ]
    # Group nodes by column rank. Within each column, sort by mass (heaviest
    # at top) so the diagram reads "biggest material flows first" vertically.
    # Special-case: in the anode/cathode parallel column, force cathode-side
    # nodes to upper half and anode-side nodes to lower half so the chains
    # are visually distinct (cathode chain converges from above, anode from
    # below into electrode_mfg).
    node_y_map: dict[int, float] = {}
    by_rank: dict[int, list[int]] = {}
    for idx, r in enumerate(ranks):
        by_rank.setdefault(r, []).append(idx)
    node_mass: dict[int, float] = {}
    for src, tgt, v in zip(sources, targets, raw_values):
        node_mass[src] = node_mass.get(src, 0.0) + v
        node_mass[tgt] = node_mass.get(tgt, 0.0) + v

    def _is_anode_track(node_id: str) -> bool:
        return (node_id.startswith("anode_mfg")
                or "_anode_" in node_id
                or node_id.endswith("_anode"))

    def _is_cathode_track(node_id: str) -> bool:
        return (node_id.startswith("cam_mfg") or node_id == "cam_mfg"
                or node_id.startswith("precursor_mfg") or node_id == "precursor_mfg")

    for r, idxs in by_rank.items():
        # Identify track membership for the parallel column
        anode_idxs = [i for i in idxs if _is_anode_track(node_ids[i])]
        cathode_idxs = [i for i in idxs if _is_cathode_track(node_ids[i])
                        and i not in anode_idxs]
        other_idxs = [i for i in idxs if i not in anode_idxs and i not in cathode_idxs]

        # Sort each group by mass (heaviest first within the group)
        cathode_idxs.sort(key=lambda i: -node_mass.get(i, 0))
        anode_idxs.sort(key=lambda i: -node_mass.get(i, 0))
        other_idxs.sort(key=lambda i: -node_mass.get(i, 0))

        # Layout: cathode at top, others in middle, anode at bottom.
        # If only one track present, distribute the whole group across the column.
        n_cat = len(cathode_idxs)
        n_oth = len(other_idxs)
        n_an = len(anode_idxs)
        n_total = n_cat + n_oth + n_an
        if n_total == 0:
            continue
        if n_total == 1:
            # Single node — center it
            for i in idxs:
                node_y_map[i] = 0.5
            continue
        # Allocate y-bands proportional to group sizes (cathode top, anode bot)
        Y_TOP = 0.05
        Y_BOT = 0.95
        span = Y_BOT - Y_TOP
        # Slot height per node
        slot = span / n_total
        cursor = Y_TOP + slot / 2
        for i in cathode_idxs + other_idxs + anode_idxs:
            node_y_map[i] = cursor
            cursor += slot
    node_y = [node_y_map.get(i, 0.5) for i in range(len(node_ids))]

    return SankeyTrace(
        label=label,
        unit=unit_label,
        nodes=node_ids,
        node_labels=[_node_label(n) for n in node_ids],
        node_hover=node_hover_strs,
        node_x=node_x,
        node_y=node_y,
        sources=sources,
        targets=targets,
        values=values,
        raw_values=raw_values,
        link_colors=link_colors,
        link_hover=link_hover,
        node_colors=["rgba(80,80,80,0.85)"] * len(node_ids),
    )


def _maybe_log(v: float, log_scale: bool) -> float:
    """Apply log1p transform if requested. Keeps Sankey readable."""
    if not log_scale:
        return v
    return math.log10(v + 1.0) if v > 0 else 0.0


# --------------------------------------------------------------------------- #
# Layout helpers
# --------------------------------------------------------------------------- #

def _node_xy(node_ids: list[str]) -> tuple[list[float], list[float]]:
    """Compute (x,y) positions per node — x by process rank, y by appearance."""
    n_steps = len(PROCESS_ORDER)
    # x in (0,1); avoid 0/1 endpoints to not collide with Plotly margins.
    x_for_rank = [0.02 + (0.96 * i / max(1, n_steps - 1)) for i in range(n_steps + 1)]

    by_rank: dict[int, list[str]] = {}
    for nid in node_ids:
        rank = _process_rank(nid)
        by_rank.setdefault(rank, []).append(nid)

    xs: list[float] = []
    ys: list[float] = []
    pos: dict[str, tuple[float, float]] = {}
    for rank, group in by_rank.items():
        n = len(group)
        for j, nid in enumerate(group):
            x = x_for_rank[min(rank, len(x_for_rank) - 1)]
            y = (j + 1) / (n + 1)
            pos[nid] = (x, y)
    for nid in node_ids:
        x, y = pos[nid]
        xs.append(x)
        ys.append(y)
    return xs, ys


def _make_sankey(trace: SankeyTrace, title: str) -> go.Sankey:
    """Build a Plotly Sankey trace from a resolved SankeyTrace.

    Uses arrangement='fixed' to honour explicit x-positions. To prevent
    Plotly's renderer from collapsing single-node columns, the trace is
    expected to have invisible spacer nodes (top + bottom) at every column
    via _inject_column_spacers, with transparent spine links between them.
    """
    node_colors = trace.node_colors or ["rgba(80,80,80,0.85)"] * len(trace.nodes)
    # Per-node line: transparent on spacers so they truly disappear.
    node_line_colors = [
        "rgba(0,0,0,0)" if c == "rgba(0,0,0,0)" else "rgba(0,0,0,0.4)"
        for c in node_colors
    ]
    return go.Sankey(
        arrangement="snap",
        node=dict(
            pad=34,           # more vertical room for multi-line labels
            thickness=20,
            line=dict(color=node_line_colors, width=0.5),
            label=trace.node_labels,
            customdata=trace.node_hover,
            hovertemplate="%{customdata}<extra></extra>",
            x=trace.node_x,
            y=trace.node_y,
            color=node_colors,
        ),
        link=dict(
            source=trace.sources,
            target=trace.targets,
            value=trace.values,
            color=trace.link_colors,
            customdata=trace.link_hover,
            hovertemplate="%{customdata}<extra></extra>",
        ),
        name=title,
    )


# --------------------------------------------------------------------------- #
# Title / footer construction
# --------------------------------------------------------------------------- #

def _build_title(meta: dict[str, Any]) -> str:
    """Compose the chart title with chemistry / route / conventions."""
    chem = meta.get("chemistry", "?")
    route = meta.get("route", "?")
    year = meta.get("year", "?")
    region = meta.get("region", "?")
    convention_bits: list[str] = []
    alloc = meta.get("allocation_convention") or {}
    if alloc.get("method"):
        convention_bits.append(f"alloc={alloc['method']}")
    extras = []
    for key in ("graphite_eol_recovery", "convention", "policy"):
        if key in meta:
            extras.append(f"{key}={meta[key]}")
    parts = [
        f"<b>{chem} — {route}</b>",
        f"FU=1 kWh · year={year} · region={region}",
    ]
    if convention_bits:
        parts.append(" · ".join(convention_bits))
    if extras:
        parts.append(" · ".join(extras))
    return "<br>".join(parts)


def _build_footnote(route: RouteData) -> str:
    """Footer summarising any accepted_uncertainty discrepancy_flags."""
    bits: list[str] = []
    for flow in route.flows:
        for d in (flow.discrepancy_flags or []):
            if str(d.get("status")) == "accepted_uncertainty":
                desc = str(d.get("description") or "").strip().split("\n")[0]
                bits.append(f"{flow.id}: {desc[:140]}")
    if not bits:
        return ""
    head = "<b>Accepted-uncertainty notes:</b>"
    return head + "<br>" + "<br>".join(f"• {b}" for b in bits[:8])


# --------------------------------------------------------------------------- #
# Main rendering entry point
# --------------------------------------------------------------------------- #

def render_html(
    route: RouteData,
    materials: dict[str, dict[str, Any]],
    refs: dict[str, dict[str, Any]],
    opts: Options,
) -> str:
    """Render the route to a self-contained HTML string."""
    # Reset the ref-number map so each render starts numbering at [1].
    _reset_ref_numbers()
    flows = _filter_flows(route.flows, opts, materials)
    mass_flows, ew_flows = _classify_traces(flows)
    if opts.energy_water_only:
        mass_flows = []

    mass_trace = build_trace(
        mass_flows, materials, refs,
        label="Mass flows", unit_label="kg/kWh",
        log_scale=opts.log_scale, no_region_split=opts.no_region_split,
    )
    ew_trace = build_trace(
        ew_flows, materials, refs,
        label="Energy / Water", unit_label="MJ·or·L /kWh",
        log_scale=opts.log_scale, no_region_split=opts.no_region_split,
    )

    title = _build_title(route.meta)
    footnote = _build_footnote(route)

    # Subplot layout: mass on top (taller), energy/water below.
    rows = (1 if mass_trace else 0) + (1 if ew_trace else 0)
    if rows == 0:
        sys.stderr.write("ERROR: no renderable flows after filtering.\n")
        sys.exit(1)

    if rows == 2:
        fig = make_subplots(
            rows=2, cols=1,
            specs=[[{"type": "sankey"}], [{"type": "sankey"}]],
            row_heights=[0.85, 0.15],
            vertical_spacing=0.05,
            subplot_titles=("Mass flows (kg/kWh)",
                            "Energy & water (MJ/kWh, L/kWh)"),
        )
        fig.add_trace(_make_sankey(mass_trace, "mass"), row=1, col=1)
        fig.add_trace(_make_sankey(ew_trace, "ew"), row=2, col=1)
    else:
        fig = go.Figure()
        the_trace = mass_trace or ew_trace
        assert the_trace is not None
        sub_title = "Mass flows (kg/kWh)" if mass_trace else \
                    "Energy & water (MJ/kWh, L/kWh)"
        fig.add_trace(_make_sankey(the_trace, sub_title))

    annotations = []
    if footnote:
        annotations.append(dict(
            text=footnote,
            xref="paper", yref="paper",
            x=0.0, y=-0.08,
            showarrow=False,
            align="left",
            font=dict(size=10, color="#444"),
        ))
    fig.update_layout(
        title=dict(text=title, x=0.0, xanchor="left", font=dict(size=14)),
        font=dict(family="Helvetica, Arial, sans-serif", size=12),
        margin=dict(l=20, r=20, t=80, b=80 + (40 if footnote else 0)),
        width=3000,
        height=2200 if rows == 2 else 1700,
        annotations=annotations,
    )

    return pio.to_html(fig, include_plotlyjs="cdn", full_html=True)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _project_root_from_route(route_path: Path) -> Path:
    """Walk up from the route file to find the project root (data/ sibling)."""
    p = route_path.resolve().parent
    for _ in range(6):
        if (p / "schema").is_dir() and (p / "sources").is_dir():
            return p
        p = p.parent
    # Fallback: assume cwd
    return Path.cwd()


def _default_out_path(route_path: Path, root: Path) -> Path:
    """Synthesize outputs/<chem>_<route>_sankey.html if --out is omitted."""
    raw = load_yaml(route_path)
    meta = raw.get("meta") or {}
    chem = _slug(meta.get("chemistry") or route_path.stem)
    rname = _slug(meta.get("route") or "route")
    return root / "outputs" / f"{chem}_{rname}_sankey.html"


# --------------------------------------------------------------------------- #
# Vehicle mode — render a single named car/product at pack-level kg
# --------------------------------------------------------------------------- #

# Country code → full display name (used in node labels)
COUNTRY_NAMES: dict[str, str] = {
    "CN": "China", "US": "United States", "KR": "South Korea", "JP": "Japan",
    "AU": "Australia", "CA": "Canada", "CL": "Chile", "AR": "Argentina",
    "BR": "Brazil", "ZW": "Zimbabwe", "ID": "Indonesia", "PH": "Philippines",
    "RU": "Russia", "NC": "New Caledonia", "FI": "Finland", "BE": "Belgium",
    "ZA": "South Africa", "GA": "Gabon", "GH": "Ghana", "MZ": "Mozambique",
    "MG": "Madagascar", "MA": "Morocco", "CD": "Democratic Republic of Congo",
    "JO": "Jordan", "IL": "Israel", "PL": "Poland", "DE": "Germany",
    "FR": "France", "GB": "United Kingdom", "IT": "Italy", "NO": "Norway",
    "SE": "Sweden", "TZ": "Tanzania", "IE": "Ireland", "IN": "India",
    "MX": "Mexico", "VN": "Vietnam", "EU": "European Union",
}


def _country_name(code: str) -> str:
    """Return display name for a country code; pass-through if unknown."""
    if not code:
        return code
    c = code.upper()
    if c in COUNTRY_NAMES:
        return COUNTRY_NAMES[c]
    if c.lower() == "other":
        return "Other"
    return code


# Country codes used across route YAMLs (region_breakdown values)
KNOWN_COUNTRIES: set[str] = {
    "CN", "KR", "JP", "US", "EU", "AU", "CL", "AR", "BR", "ZW", "ID", "PH",
    "RU", "NC", "CA", "FI", "BE", "ZA", "GA", "GH", "MZ", "MG", "MA", "CD",
    "JO", "IL", "PL", "DE", "FR", "GB", "IT", "NO", "SE",
    "TZ", "IE", "IN", "MX", "VN",
    # NL excluded (subregion-to-Canada mapping). DRC excluded too — routed
    # to "CD" via SUBREGION_TO_COUNTRY so it matches route-YAML region_breakdown.
}
# Subregions and aliases that should resolve to the route-YAML country code.
SUBREGION_TO_COUNTRY: dict[str, str] = {
    "NL": "CA",   # Newfoundland & Labrador (Canadian mining context, e.g. Voisey's Bay)
    "ON": "CA",   # Ontario
    "BC": "CA", "QC": "CA", "AB": "CA",
    "WA": "AU",   # Western Australia (mining context)
    "NSW": "AU", "QLD": "AU", "SA": "AU", "VIC": "AU", "NT": "AU",
    # 3-letter aliases that map to ISO 2-letter codes used in route YAMLs:
    "DRC": "CD",  # Democratic Republic of Congo
    "USA": "US",
    "CHN": "CN",
}


def _extract_regions(record: dict[str, Any]) -> set[str]:
    """Return all country-code regions implied by a relationship endpoint.
    Looks at explicit `region`/`country` field and parses 2–3-letter codes
    from the `facility` string (handles formats like 'Liyang (CN)',
    'Sichuan (CN) + Kwinana (AU)', 'Changshu (Jiangsu, CN)').
    """
    out: set[str] = set()
    if not isinstance(record, dict):
        return out

    def _add(tok: str) -> None:
        t = tok.upper()
        # Reject all-3-letter codes unless they're known (e.g., DRC)
        if t in KNOWN_COUNTRIES:
            out.add(t)
        elif t in SUBREGION_TO_COUNTRY:
            out.add(SUBREGION_TO_COUNTRY[t])

    direct = record.get("region") or record.get("country")
    if direct:
        for tok in re.findall(r"[A-Za-z]{2,3}", str(direct)):
            _add(tok)
    for s in (record.get("facility"), record.get("plant"), record.get("location")):
        if s:
            # Take all 2–3-letter uppercase tokens
            for tok in re.findall(r"\b([A-Z]{2,3})\b", str(s)):
                _add(tok)
    return out


# Heuristic: extract material categories (lithium, nickel, cobalt, etc.) from a
# relationship's material string. Returns a set because some materials (e.g.,
# "nickel-cobalt sulfide concentrate") imply multiple categories.
def _material_categories(material: str) -> set[str]:
    m = (material or "").lower()
    cats: set[str] = set()
    if not m:
        return cats
    if any(k in m for k in ["lithium", "lioh", "li2co3", "spodumene"]):
        cats.add("lithium")
    if "ni-co" in m or "nickel-cobalt" in m or "cu-co" in m or "ni/co" in m:
        cats.update({"nickel", "cobalt"})
    if any(k in m for k in ["nickel", "niso4", "ni intermediate", "mhp", "ni sulf"]):
        cats.add("nickel")
    if any(k in m for k in ["cobalt", "coso4", "co hydroxide", "co metal", "co sulf"]):
        cats.add("cobalt")
    if any(k in m for k in ["manganese", "mnso4", "mn ore", "mn sulf"]):
        cats.add("manganese")
    if any(k in m for k in ["fepo4", "iron phosphate", "feso4", "ferrous", "iron sulf", "iron oxide"]):
        cats.add("iron_phosphorus")
    if any(k in m for k in ["phosphoric", "h3po4", "phosphate rock", "phosphate (battery"]):
        cats.add("iron_phosphorus")
    if any(k in m for k in ["graphite", "spng", "spheronized"]):
        cats.add("anode")
    return cats


# Heuristic mapping of relationship.material → process step.
# `tier` (1/2/3) is used to disambiguate when material name is generic.
def _material_to_step(material: str, tier: Optional[int] = None) -> set[str]:
    """Return the set of process step IDs that this relationship represents.

    Returns a set because some relationships (e.g., a mine producing
    'concentrate' for a refiner) span both mining and beneficiation.
    """
    m = (material or "").lower()
    steps: set[str] = set()

    # Tier 3 is by definition a mine; always include 'mining'. If the material
    # is described as concentrate or intermediate, also include 'beneficiation'.
    if tier == 3:
        steps.add("mining")
        if any(k in m for k in ["concentrate", "intermediate", "matte", "mhp", "ore"]):
            steps.add("beneficiation")
        return steps

    if not m:
        return steps

    # Anode-side FIRST (so "graphite anode for LFP cells" doesn't match 'cell')
    if "anode" in m or "spng" in m or "spheronized" in m:
        steps.add("anode_mfg")
        return steps
    if "graphite" in m:
        if tier == 2:
            steps.add("anode_mfg")
            return steps
        steps.add("mining")
        return steps

    # Specific-first: cells / modules / packs
    if "cell" in m:
        steps.add("cell_mfg")
        return steps
    if "module" in m:
        steps.add("module_assembly")
        return steps
    if "pack" in m and "package" not in m and "rock" not in m:
        steps.add("pack_mfg")
        return steps

    # Cathode-side
    if "pcam" in m or "fepo4" in m or "hydroxide precursor" in m or "ni(oh)" in m or "precursor" in m:
        steps.add("precursor_mfg")
        return steps
    if "cam" in m or "cathode active" in m or "lifepo4" in m or "nmc" in m or "nca" in m:
        steps.add("cam_mfg")
        return steps

    # Refining / chemicals
    if any(k in m for k in [
        "sulfate", "hydroxide", "carbonate", "phosphoric", "h3po4",
        "lioh", "li2co3", "niso4", "coso4", "mnso4", "feso4",
        "battery-grade",
    ]):
        steps.add("chem_processing")
        return steps

    # Concentrate (Tier 2-ish: between mine and refinery)
    if "concentrate" in m:
        steps.add("beneficiation")
        steps.add("mining")  # the concentrate originated at a mine
        return steps

    # Mining
    if any(k in m for k in ["ore", "rock", "spodumene", "brine"]):
        steps.add("mining")
        return steps

    return steps


def find_matching_products(sc_data: dict[str, Any], vehicle: str) -> list[dict[str, Any]]:
    """Return product entries whose product/company/id contains the search string."""
    needle = vehicle.lower().strip()
    products = sc_data.get("products") or []
    out = []
    for p in products:
        haystack = " ".join(str(p.get(k, "")).lower()
                            for k in ("id", "company", "product"))
        if needle in haystack:
            out.append(p)
    return out


@dataclass
class VisitedSets:
    """Per-vehicle visited regions, keyed for two filtering modes:
    - by_step:           {step → {regions}}        (downstream stages, no material)
    - by_category_step:  {(category, step) → {regions}}   (upstream, material-specific)
    - chain:             {company-name} discovered in the bilateral graph
    - companies_by_step: {(step, region_iso2) → [companies]}  for label attachment
    - region_weights:    {step → {region → weight}}  per-region share derived
                         from cell_makers[].share_pct_estimate. Used by the
                         vehicle filter to weight cross-region splits (e.g.
                         LGES Holland 75% / Wrocław 25%) instead of even split.
    """
    by_step: dict[str, set[str]]
    by_category_step: dict[tuple[str, str], set[str]]
    chain: set[str] = field(default_factory=set)
    companies_by_step: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    region_weights: dict[str, dict[str, float]] = field(default_factory=dict)


def derive_visited_regions(
    product: dict[str, Any],
    relationships: list[dict[str, Any]],
) -> VisitedSets:
    """Walk the bilateral graph from a product upstream and collect regions.

    Returns two parallel dictionaries: one for downstream stages (cell_mfg,
    module_assembly, pack_mfg, application) where region applies to the stage
    as a whole, and one for upstream stages (mining, beneficiation, chem_processing,
    precursor_mfg, cam_mfg, anode_mfg) where region applies per material category.
    """
    by_step: dict[str, set[str]] = {}
    by_cat_step: dict[tuple[str, str], set[str]] = {}

    def _add_step(step: str, regions: set[str]) -> bool:
        if not regions:
            return False
        cur = by_step.setdefault(step, set())
        before = len(cur)
        cur.update(regions)
        return len(cur) > before

    def _add_cat_step(cats: set[str], step: str, regions: set[str]) -> bool:
        if not regions:
            return False
        any_added = False
        if not cats:
            # Material has no specific category — record at step level
            if _add_step(step, regions):
                any_added = True
            return any_added
        for cat in cats:
            cur = by_cat_step.setdefault((cat, step), set())
            before = len(cur)
            cur.update(regions)
            if len(cur) > before:
                any_added = True
        return any_added

    # Seed cell_mfg regions from product.cell_makers
    cm_list = product.get("cell_makers") or []
    cm_regions: set[str] = set()
    # Per-region weights derived from share_pct_estimate. When a cell_makers
    # entry covers multiple regions (e.g. "US / PL" without per-facility
    # split), the entry's share_pct_estimate is divided evenly across them.
    # Entries with explicit per-facility shares (separate rows for each
    # region) carry their own share_pct_estimate weight.
    cm_region_weights: dict[str, float] = {}
    for cm in cm_list:
        regs = _extract_regions(cm)
        cm_regions |= regs
        sh = cm.get("share_pct_estimate")
        try:
            sh_f = float(sh) if sh is not None else None
        except (TypeError, ValueError):
            sh_f = None
        if sh_f and regs:
            per = sh_f / len(regs)
            for rgn in regs:
                cm_region_weights[rgn] = cm_region_weights.get(rgn, 0.0) + per
    # Normalize weights to sum to 1.0
    if cm_region_weights:
        tot = sum(cm_region_weights.values()) or 1.0
        cm_region_weights = {k: v / tot for k, v in cm_region_weights.items()}
    _add_step("cell_mfg", cm_regions)
    _add_step("module_assembly", cm_regions)
    _add_step("pack_mfg", cm_regions)
    _add_step("application", cm_regions)
    # Electrode mfg is co-located with cell mfg in commercial practice
    _add_step("electrode_mfg", cm_regions)

    # Track companies known to be in the chain. Use a normalized-name index so
    # "Hunan Yuneng" matches "Hunan Yuneng New Energy Battery Material".
    def _norm(name: str) -> str:
        # Strip common corporate suffixes and excess whitespace; lowercase
        if not name:
            return ""
        s = name.lower().strip()
        for suf in [
            "new energy battery material", "new energy battery materials",
            "new material group", "new materials", "new material",
            "group", "company", "co. ltd", "co ltd", "co.", "ltd.", "ltd",
            "incorporated", "inc.", "inc", "corp.", "corp", "corporation",
            "(parent)",
        ]:
            if s.endswith(" " + suf):
                s = s[: -(len(suf) + 1)].strip()
        return s

    def _chain_match(query: str, chain_set: set[str]) -> bool:
        if not query:
            return False
        q = _norm(query)
        for c in chain_set:
            cn = _norm(c)
            if q == cn or q in cn or cn in q:
                return True
        return False

    chain: set[str] = {(cm.get("company") or "") for cm in cm_list}
    chain = {c for c in chain if c}
    pid = product.get("id")
    companies_by_step: dict[tuple[str, str], list[str]] = {}
    changed = True

    while changed:
        changed = False
        for rel in relationships:
            d = rel.get("downstream") or {}
            u = rel.get("upstream") or {}
            d_co = d.get("company") or ""
            d_pid = d.get("product_id")
            tier = rel.get("tier")
            if d_pid != pid and not _chain_match(d_co, chain):
                continue
            u_regions = _extract_regions(u)
            mat = rel.get("material") or ""
            steps = _material_to_step(mat, tier=tier)
            cats = _material_categories(mat)
            UPSTREAM = {"mining", "beneficiation", "chem_processing",
                        "precursor_mfg", "cam_mfg", "anode_mfg"}
            for step in steps:
                if step in UPSTREAM:
                    if _add_cat_step(cats, step, u_regions):
                        changed = True
                else:
                    if _add_step(step, u_regions):
                        changed = True
            u_co = u.get("company") or ""
            if u_co:
                # Record (step, region) → company for label attachment
                for step in steps:
                    for rgn in u_regions:
                        bucket = companies_by_step.setdefault((step, rgn), [])
                        if u_co not in bucket:
                            bucket.append(u_co)
                if u_co not in chain:
                    chain.add(u_co)
                    changed = True

    # ---------------------------------------------------------------------
    # Forward-propagation pass:
    # If a (category, step) has any visited regions, propagate them forward
    # along the upstream chain to fill empty downstream stages within the
    # same material category. This addresses the common gap where Ford's
    # disclosures end at a refiner or cathode maker but route-YAML flows
    # extend further (e.g., LiOH refined in CN flows into a CN precursor
    # plant; even if the precursor maker isn't disclosed by Ford, the
    # region is implied by the refining-stage geography).
    # ---------------------------------------------------------------------
    # Cathode-side metals follow this stage order:
    cathode_chain: list[str] = [
        "mining", "beneficiation", "chem_processing",
        "precursor_mfg", "cam_mfg",
    ]
    # Anode side has its own chain
    anode_chain: list[str] = [
        "mining", "beneficiation", "anode_mfg",
    ]
    cats_seen: set[str] = {c for (c, _) in by_cat_step.keys()}
    for cat in cats_seen:
        chain_steps = anode_chain if cat == "anode" else cathode_chain
        # Forward pass: explicit stage value propagates downstream.
        last_seen: set[str] = set()
        for step in chain_steps:
            key = (cat, step)
            if key in by_cat_step and by_cat_step[key]:
                last_seen = set(by_cat_step[key])
            elif last_seen:
                by_cat_step[key] = set(last_seen)
        # Backward pass: fill remaining empty UPSTREAM stages from the
        # nearest downstream populated stage. Critical for graphite (where
        # the mine is undisclosed but the spheronization plant region is
        # named). Also handles cases where Tier-3 mining is undisclosed
        # but Tier-2 cathode/anode is.
        last_seen = set()
        for step in reversed(chain_steps):
            key = (cat, step)
            if key in by_cat_step and by_cat_step[key]:
                last_seen = set(by_cat_step[key])
            elif last_seen:
                by_cat_step[key] = set(last_seen)

    # ---------------------------------------------------------------------
    # Populate by_step from union of all by_category_step entries.
    # Used as a fallback for flows whose category isn't a specific metal
    # (e.g., CAM, PCAM, electrolyte, separator, electricity, water, "other").
    # These don't fit per-metal filtering, so they pass through if any
    # metal chain is visited at the same process step.
    # ---------------------------------------------------------------------
    ALL_UPSTREAM = ["mining", "beneficiation", "chem_processing",
                    "precursor_mfg", "cam_mfg", "anode_mfg"]
    for step in ALL_UPSTREAM:
        union: set[str] = set()
        for (_cat, s), regs in by_cat_step.items():
            if s == step:
                union |= regs
        if union:
            by_step[step] = by_step.get(step, set()) | union

    # Electrode manufacturing is co-located with cell manufacturing in
    # commercial practice. Don't pool cam_mfg or anode_mfg into it — those
    # are separate facilities upstream. The cross-region transition handler
    # (CROSS_REGION_TRANSITIONS in _filter_route_for_vehicle) takes care of
    # routing cam→electrode and anode→electrode flows into the cell maker's
    # region without polluting electrode_mfg's filter set.
    by_step["electrode_mfg"] = (
        by_step.get("electrode_mfg", set())
        | by_step.get("cell_mfg", set())
    )

    # Region weights: only the cell_mfg-family stages carry per-facility
    # share weight from cell_makers. Other steps fall back to even split.
    region_weights: dict[str, dict[str, float]] = {}
    if cm_region_weights:
        for s in ("cell_mfg", "electrode_mfg", "module_assembly", "pack_mfg",
                  "application"):
            region_weights[s] = dict(cm_region_weights)

    return VisitedSets(
        by_step=by_step,
        by_category_step=by_cat_step,
        chain=chain,
        companies_by_step=companies_by_step,
        region_weights=region_weights,
    )


def _flow_category(flow: Flow, materials: dict[str, dict[str, Any]]) -> Optional[str]:
    """Look up a flow's material category via materials_registry; fall back to
    parsing the material name."""
    if flow.material_id:
        m_entry = materials.get(flow.material_id)
        if m_entry:
            cat = m_entry.get("category")
            if cat:
                return str(cat)
    cats = _material_categories(flow.material or "")
    if cats:
        return next(iter(cats))
    return None


def _filter_route_for_vehicle(
    route: RouteData,
    visited: VisitedSets,
    pack_kWh: float,
    materials: dict[str, dict[str, Any]],
    product_label: Optional[str] = None,
) -> RouteData:
    """Return a NEW RouteData where flows are scaled by pack_kWh and
    region_breakdown is filtered to ONLY the regions in this vehicle's chain.

    For each flow, the allowed regions depend on whether the from_node is an
    upstream stage (filter by (category, step)) or downstream (filter by step
    only). Non-visited regions are dropped entirely and the remaining visited
    regions are re-normalised so their share_pct sums to 100.
    """
    UPSTREAM = {"mining", "beneficiation", "chem_processing",
                "precursor_mfg", "cam_mfg", "anode_mfg"}
    EOL_NODES = {"eol_recycling", "waste", "application"}
    # Drop flows whose pack-level mass is below this threshold (visual clutter
    # from very minor regional shares). Keep at 0.5 kg/pack — anything smaller
    # is essentially invisible at the diagram's mass scale.
    MIN_PACK_MASS_KG = 0.5

    # Build a normalized chain-company set for filtering route-YAML company
    # lists. Route YAMLs ship "industry-typical" company lists (e.g. CATL,
    # BYD, Tesla on cell_mfg) that don't necessarily supply THIS vehicle.
    # We intersect those lists against the bilateral graph's discovered
    # chain so only Ford-relevant suppliers surface in the hover text.
    def _norm_co(s: str) -> str:
        if not s:
            return ""
        n = str(s).lower().strip()
        for suf in [
            "new energy battery material", "new energy battery materials",
            "new material group", "new materials", "new material",
            "group", "company", "co. ltd", "co ltd", "co.", "ltd.", "ltd",
            "incorporated", "inc.", "inc", "corp.", "corp", "corporation",
            "(parent)",
        ]:
            if n.endswith(" " + suf):
                n = n[: -(len(suf) + 1)].strip()
        return n
    chain_norm = {_norm_co(c) for c in (visited.chain or set()) if c}

    def _filter_companies(cs: Optional[list[str]]) -> Optional[list[str]]:
        if not cs:
            return cs
        if not chain_norm:
            return cs
        kept: list[str] = []
        for c in cs:
            cn = _norm_co(c)
            if not cn:
                continue
            # Match if the route-listed name is in the chain (substring either
            # direction handles "Hunan Yuneng" ↔ "Hunan Yuneng New Energy
            # Battery Material" pairs).
            for chn in chain_norm:
                if cn == chn or cn in chn or chn in cn:
                    kept.append(c)
                    break
        return kept

    new_flows: list[Flow] = []
    for f in route.flows:
        # Vehicle-mode pruning of flows that don't belong in a "what's in this
        # pack" story:
        # (1) Drop loss flows — they confuse the diagram with long arrows from
        #     upstream stages straight to a global "waste" sink.
        if f.is_loss:
            continue
        # (2) Drop EoL flows — application → eol → waste / recycled metals
        #     describe an event years after the pack is built; they don't
        #     belong on a pack-content diagram.
        if f.from_node in EOL_NODES or f.to_node in EOL_NODES:
            # Keep pack_mfg → application (the terminal arrow into the vehicle)
            if not (f.from_node == "pack_mfg" and f.to_node == "application"):
                continue
        new_value = (f.value or 0) * pack_kWh
        # Skip very small flows in mass units; keep all energy/water flows
        if (f.unit or "").startswith("kg") and (new_value or 0) < MIN_PACK_MASS_KG:
            continue
        new_rb: Optional[list[dict[str, Any]]] = None
        skip_flow = False

        # Stage-product helper: parse country codes embedded in company name
        # strings, e.g. "Granges (SE/CN)" → ["SE", "CN"]. Lets us synthesize
        # a region_breakdown for stage-product flows (foil/electrolyte/
        # separator) when the route YAML lists companies but no rb.
        STAGE_PRODUCT_FLOWS = {
            ("chem_processing", "electrode_mfg"),
            ("chem_processing", "cell_mfg"),
        }

        def _regions_from_companies(companies: Optional[list[str]]) -> dict[str, list[str]]:
            """{ISO2: [companies]} keyed by parenthetical country code."""
            out: dict[str, list[str]] = {}
            for c in (companies or []):
                m = re.search(r"\(([A-Z][A-Z](?:[/,\s][A-Z][A-Z])*)\)", str(c))
                if not m:
                    continue
                for code in re.split(r"[/,\s]+", m.group(1)):
                    code = code.strip().upper()[:3]
                    if code:
                        out.setdefault(code, []).append(c)
            return out

        # (5) Fix #2 — flow has no region_breakdown: synthesize one from
        #     visited regions so it doesn't render as an unattributed single
        #     node. Use the from_node's visited regions (or material category
        #     visited if it's a metal-specific upstream flow).
        if not f.region_breakdown:
            METAL_CATS = {"lithium", "nickel", "cobalt", "manganese",
                          "iron_phosphorus", "anode"}
            synth_regions: set[str] = set()
            # Stage-product flows (foil/electrolyte/separator from
            # chem_processing → electrode/cell): if the route YAML lists
            # companies with embedded country codes (e.g. "Granges (SE/CN)"),
            # use those codes to build a per-region rb that mirrors actual
            # global mfg geography. Otherwise fall back to the destination's
            # visited region so the link doesn't end up unattributed.
            if (f.from_node, f.to_node) in STAGE_PRODUCT_FLOWS:
                co_regions = _regions_from_companies(f.companies)
                if co_regions:
                    n = len(co_regions)
                    share = 100.0 / n
                    rb_synth: list[dict[str, Any]] = []
                    for rgn, cs in sorted(co_regions.items()):
                        rb_synth.append({
                            "region": rgn,
                            "share_pct": share,
                            "value": new_value * share / 100.0,
                            "companies": cs,
                            "source_ids": f.source_ids,
                            "notes": "Region parsed from company-name annotations.",
                        })
                    f = Flow(
                        id=f.id, from_node=f.from_node, to_node=f.to_node,
                        material=f.material, material_id=f.material_id,
                        value=f.value, unit=f.unit,
                        region_breakdown=rb_synth,
                        application_breakdown=f.application_breakdown,
                        companies=f.companies, source_ids=f.source_ids,
                        discrepancy_flags=f.discrepancy_flags,
                        is_loss=f.is_loss, is_recycled=f.is_recycled,
                        verified=f.verified, notes=f.notes,
                    )
                    synth_regions = set()  # rb already synthesized; skip re-synth
                else:
                    synth_regions = visited.by_step.get(f.to_node, set())
            elif f.from_node in UPSTREAM:
                cat = _flow_category(f, materials)
                if cat in METAL_CATS:
                    synth_regions = visited.by_category_step.get(
                        (cat, f.from_node), set()
                    )
                if not synth_regions:
                    synth_regions = visited.by_step.get(f.from_node, set())
            else:
                synth_regions = visited.by_step.get(f.from_node, set())
            if synth_regions:
                # Equal-weight synthetic split across visited regions
                n = len(synth_regions)
                share = 100.0 / n
                rb_synth: list[dict[str, Any]] = []
                for rgn in sorted(synth_regions):
                    rb_synth.append({
                        "region": rgn,
                        "share_pct": share,
                        "value": new_value * share / 100.0,
                        "companies": f.companies or [],
                        "source_ids": f.source_ids,
                        "notes": "Region synthesized from visited supply chain (no explicit region_breakdown in route YAML).",
                    })
                # Re-route through the same logic by giving the flow a temporary rb
                f = Flow(
                    id=f.id, from_node=f.from_node, to_node=f.to_node,
                    material=f.material, material_id=f.material_id,
                    value=f.value, unit=f.unit,
                    region_breakdown=rb_synth,
                    application_breakdown=f.application_breakdown,
                    companies=f.companies, source_ids=f.source_ids,
                    discrepancy_flags=f.discrepancy_flags,
                    is_loss=f.is_loss, is_recycled=f.is_recycled,
                    verified=f.verified, notes=f.notes,
                )
        if f.region_breakdown:
            # Determine allowed region set
            METAL_CATS = {"lithium", "nickel", "cobalt", "manganese",
                          "iron_phosphorus", "anode"}
            if (f.from_node, f.to_node) in STAGE_PRODUCT_FLOWS:
                # Stage-product flows: trust the rb we just built (either
                # from YAML or parsed from company-name country codes).
                # Don't filter against chem_processing's metals-only visited
                # set, which won't include CN/JP/KR foil-making regions.
                allowed = {(r.get("region") or "").upper()[:3]
                           for r in f.region_breakdown
                           if r.get("region")}
            elif f.from_node in UPSTREAM:
                cat = _flow_category(f, materials)
                if cat in METAL_CATS:
                    # Per-metal filtering (specific to this material chain)
                    allowed = visited.by_category_step.get((cat, f.from_node), set())
                else:
                    # Stage products (CAM, PCAM, energy/water, "other") fall
                    # back to union at this step.
                    allowed = visited.by_step.get(f.from_node, set())
            else:
                allowed = visited.by_step.get(f.from_node, set())

            kept_raw: list[tuple[dict[str, Any], float]] = []
            # Cross-region transitions: when a flow's from_node is at one
            # geography (e.g., POSCO CAM in Korea, BTR anode in China) but
            # the to_node is at a different geography (LGES electrode in
            # the US), we apply a `to_region` override that keeps from_region
            # at the actual upstream location and routes to the cell-maker's
            # region for the to-side of the link. This produces honest
            # cross-border flows like "Anode·China → Electrode·USA".
            CROSS_REGION_TRANSITIONS = {
                ("cam_mfg", "electrode_mfg"),
                ("precursor_mfg", "cam_mfg"),
                ("anode_mfg", "electrode_mfg"),
                # Stage-product commodities (foil, electrolyte, separator) are
                # made in CN/KR/JP and shipped to the cell-maker's plant in
                # US/EU/etc. Without this entry, F062/F063/F071/F072 land at
                # electrode_mfg_CN / cell_mfg_CN which are island nodes for any
                # non-China cell-maker (e.g. Mach-E ER → LGES Holland/Wroclaw).
                ("chem_processing", "electrode_mfg"),
                ("chem_processing", "cell_mfg"),
            }
            target_regions: set[str] = set()
            # Energy and water flows skip the cross-region routing — they
            # belong on a separate sub-Sankey track and shouldn't be
            # re-routed across borders.
            is_energy_water = (f.unit or "").startswith(("MJ_", "L_"))
            if not is_energy_water and (f.from_node, f.to_node) in CROSS_REGION_TRANSITIONS:
                target_regions = visited.by_step.get(f.to_node, set())
                # cam_mfg / precursor_mfg are region-less in node IDs, so for
                # those the "from region" filter doesn't matter; for anode_mfg
                # we still respect the from_region from route YAML.

            # Region-code equivalence: route YAMLs sometimes use coarse codes
            # like "EU" while supply-chain YAMLs use fine-grained ISO codes
            # like "PL" (LGES Wrocław), "DE" (CATL Erfurt), "HU" (Samsung SDI
            # Göd), etc. Without a mapping, an rb entry of region="EU" is
            # dropped because "EU" ∉ {US, PL}, leaving the to-side under-
            # represented and breaking mass balance at electrode_mfg / cell_mfg.
            EU_COUNTRY_CODES = {
                "PL", "DE", "FR", "IT", "ES", "PT", "HU", "CZ", "SK", "AT",
                "BE", "NL", "SE", "FI", "NO", "DK", "IE", "GR", "RO", "BG",
                "HR", "SI", "EE", "LV", "LT", "LU",
            }

            def _rgn_in_allowed(rgn_code: str, allowed_set: set[str]) -> bool:
                if rgn_code in allowed_set:
                    return True
                # "EU" matches any European country in allowed set
                if rgn_code == "EU" and (allowed_set & EU_COUNTRY_CODES):
                    return True
                # Any European country code matches "EU" if it's in allowed
                if rgn_code in EU_COUNTRY_CODES and "EU" in allowed_set:
                    return True
                return False

            # Per-region weight for the to_node (e.g. {US: 0.75, PL: 0.25})
            # — used when splitting cross-region target_regions and when
            # expanding a coarse "EU" rb across multiple visited European
            # countries. Falls back to even split when weights are absent.
            to_weights = visited.region_weights.get(f.to_node, {}) if target_regions else {}

            def _weighted(regions: list[str], weights: dict[str, float]) -> list[tuple[str, float]]:
                """Return [(region, fraction)] summing to 1.0. Even split if no weights."""
                if not regions:
                    return []
                pairs = [(r, weights.get(r, 0.0)) for r in regions]
                tot = sum(w for _, w in pairs)
                if tot <= 0:
                    even = 1.0 / len(regions)
                    return [(r, even) for r in regions]
                return [(r, w / tot) for r, w in pairs]

            for r in f.region_breakdown:
                rgn = (r.get("region") or "").upper()[:3]
                if rgn in ("OTHER", "ROW"):
                    continue
                if not _rgn_in_allowed(rgn, allowed):
                    continue
                share = _coerce_float(r.get("share_pct")) or 0
                # Remap coarse rb codes ("EU") to actual visited European
                # country code(s). If the to_node has region weights, weight
                # the EU expansion by them (so 75/25 LGES Holland/Wrocław is
                # preserved instead of forced to 50/50).
                expanded_with_weights: list[tuple[str, float]]
                if rgn == "EU":
                    eu_in_allowed = sorted(allowed & EU_COUNTRY_CODES)
                    if eu_in_allowed:
                        # Try from-side weights first (expanding the source);
                        # for stage-product / cross-region flows the EU rb is
                        # on the FROM side of the link, so weight by from_node
                        # weights when present.
                        from_weights = visited.region_weights.get(f.from_node, {})
                        expanded_with_weights = _weighted(eu_in_allowed, from_weights)
                    else:
                        expanded_with_weights = [(rgn, 1.0)]
                else:
                    expanded_with_weights = [(rgn, 1.0)]

                if target_regions:
                    target_pairs = _weighted(sorted(target_regions), to_weights)
                    for src_rgn, src_frac in expanded_with_weights:
                        for tgt, tgt_frac in target_pairs:
                            sub_share = share * src_frac * tgt_frac
                            kept_raw.append(({
                                **r,
                                "region": src_rgn,
                                "share_pct": sub_share,
                                "to_region": tgt,
                            }, sub_share))
                else:
                    for src_rgn, src_frac in expanded_with_weights:
                        sub_share = share * src_frac
                        kept_raw.append(({
                            **r,
                            "region": src_rgn,
                            "share_pct": sub_share,
                        }, sub_share))
            # For downstream-to-downstream flows (e.g. electrode_mfg →
            # cell_mfg), the route YAML's regional split represents a global
            # default and ignores per-vehicle facility weights. If the
            # from_node has explicit region_weights from the supply chain
            # (e.g. LGES Holland 75%, Wrocław 25%), re-weight the kept rb
            # entries so the flow respects the actual facility split.
            from_weights = visited.region_weights.get(f.from_node, {})
            if from_weights and not target_regions:
                # Only re-weight when there's at least one region present in
                # both kept_raw and from_weights; otherwise the YAML rb stays
                # as the best available signal.
                kept_regions = {(r.get("region") or "").upper()[:3]
                                for r, _ in kept_raw}
                if kept_regions & set(from_weights.keys()):
                    new_kept_raw: list[tuple[dict[str, Any], float]] = []
                    for r, _orig in kept_raw:
                        rgn_code = (r.get("region") or "").upper()[:3]
                        w = from_weights.get(rgn_code, 0.0)
                        if w > 0:
                            new_kept_raw.append((r, w))
                    if new_kept_raw:
                        kept_raw = new_kept_raw

            total_kept_share = sum(s for _, s in kept_raw)
            if total_kept_share > 0 and kept_raw:
                kept: list[dict[str, Any]] = []
                for r, original_share in kept_raw:
                    normalized_share = original_share / total_kept_share * 100.0
                    new_region_value = new_value * normalized_share / 100.0
                    kept.append({
                        **r,
                        "share_pct": normalized_share,
                        "value": new_region_value,
                    })
                new_rb = kept
            else:
                # No visited region matches this flow — drop it entirely so
                # the diagram doesn't render an unattributed single-node fragment.
                skip_flow = True
        if skip_flow:
            continue
        # In vehicle mode, replace global application_breakdown (e.g.,
        # "Standard Range Global", "Mainstream China") with a single node
        # named after the product, since this Sankey is for ONE vehicle.
        new_app_bd = f.application_breakdown
        if (product_label and f.to_node == "application"
                and f.application_breakdown):
            new_app_bd = [{
                "category": "vehicle",
                "category_label": product_label,
                "share_pct": 100.0,
                "value": new_value,
                "example_products": [],
                "source_ids": f.source_ids,
                "notes": "Vehicle mode: collapsed to single product terminus.",
            }]
        # Rewrite unit suffix _per_kWh → _per_pack so hovers display correctly
        new_unit = f.unit.replace("_per_kWh", "_per_pack") if f.unit else f.unit
        # OEM-contamination filter: route YAMLs ship "industry-typical" OEM
        # arrays (Tesla, BYD, GM…) only on the cell-maker-and-down stages
        # — those are the customer lists. Upstream commodity flows
        # (foil, electrolyte, separator, CAM, anode, refined metals) carry
        # actual supplier names that ARE relevant even if not in Ford's
        # bilateral chain. Only filter when the OEM list itself is the risk.
        # Only the OUTGOING flows from cell_mfg/module/pack carry the
        # customer-OEM list (Tesla/BYD as cell-maker customers, etc.).
        # Incoming flows TO those stages (foil, electrolyte, separator) carry
        # legitimate component-supplier names that should NOT be filtered.
        OEM_CONTAMINATION_FROM_NODES = {
            "cell_mfg", "module_assembly", "pack_mfg",
        }
        should_filter_companies = f.from_node in OEM_CONTAMINATION_FROM_NODES
        if should_filter_companies:
            filtered_flow_companies = _filter_companies(f.companies)
            if new_rb:
                filtered_rb = []
                for r in new_rb:
                    r2 = dict(r)
                    r2["companies"] = _filter_companies(r.get("companies"))
                    filtered_rb.append(r2)
            else:
                filtered_rb = new_rb
        else:
            filtered_flow_companies = f.companies
            filtered_rb = new_rb
        new_flows.append(Flow(
            id=f.id,
            from_node=f.from_node,
            to_node=f.to_node,
            material=f.material,
            material_id=f.material_id,
            value=new_value,
            unit=new_unit,
            region_breakdown=filtered_rb,
            application_breakdown=new_app_bd,
            companies=filtered_flow_companies,
            source_ids=f.source_ids,
            discrepancy_flags=f.discrepancy_flags,
            is_loss=f.is_loss,
            is_recycled=f.is_recycled,
            verified=f.verified,
            notes=f.notes,
        ))
    return RouteData(meta=route.meta, nodes=route.nodes, flows=new_flows, raw=route.raw)


def _collect_stage_product_companies(
    flows: list[Flow],
    materials: dict[str, dict[str, Any]],
) -> dict[str, list[str]]:
    """Walk flows and return {chem_processing_<stage>_<REG>: [companies...]}
    for stage-product flows (foil, electrolyte, separator). Used to surface
    real component-supplier names on those nodes' labels.

    Composite IDs match _composite_node_id: chem_processing_<material_slug>_<REG>.
    The slug is derived from the material name via _material_categories /
    materials_registry, the same way build_trace builds source nodes.
    """
    out: dict[str, list[str]] = {}
    STAGE_PRODUCT_FROM_TO = {
        ("chem_processing", "electrode_mfg"),
        ("chem_processing", "cell_mfg"),
    }
    for f in flows:
        if (f.from_node, f.to_node) not in STAGE_PRODUCT_FROM_TO:
            continue
        if not f.region_breakdown:
            continue
        for r in f.region_breakdown:
            reg = (r.get("region") or "").upper()[:3]
            if not reg or reg in ("OTHER", "ROW"):
                continue
            cs = r.get("companies") or f.companies or []
            if not cs:
                continue
            # Composite ID derived from the source flow node + material
            # (slugged) + region — this matches build_trace's composite
            # node IDs for chem_processing_<material>_<REG>.
            nid = _composite_node_id(f.from_node, f, reg, materials)
            slot = out.setdefault(nid, [])
            for c in cs:
                if c not in slot:
                    slot.append(c)
    return out


def _attach_company_labels(
    trace: SankeyTrace,
    product: dict[str, Any],
    stage_product_companies: Optional[dict[str, list[str]]] = None,
    visited_companies_by_step: Optional[dict[tuple[str, str], list[str]]] = None,
) -> SankeyTrace:
    """Append the maker company name to electrode/cell/module/pack node
    labels, matched by region from product.cell_makers (and friends).

    cell_makers entries look like:
      {company: CATL, facility: Changshu (CN), share_pct_estimate: 100,
       region: CN}
    or with mixed-region facility:
      {company: LG Energy Solution, facility: Holland MI (US) + Wroclaw (PL),
       region: "US / PL"}

    For multi-region entries we split on '/' and attach the same company to
    both region-suffixed nodes. Module assembly and pack mfg are co-located
    with cell mfg in commercial practice unless overridden by separate
    module_makers / pack_makers fields.
    """
    STAGES_TO_LABEL = ("electrode_mfg", "cell_mfg", "module_assembly", "pack_mfg")
    # Build region → [companies] from cell_makers (and override with
    # module_makers / pack_makers / electrode_makers if those keys exist
    # on the product, for OEMs that disclose them separately).
    def _collect(field: str) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for entry in (product.get(field) or []):
            co = entry.get("company")
            reg = entry.get("region") or ""
            if not co or not reg:
                continue
            for r in re.split(r"[\s/,]+", str(reg)):
                r = r.strip().upper()[:3]
                if not r:
                    continue
                out.setdefault(r, []).append(co)
        return out

    cell_co = _collect("cell_makers")
    elec_co = _collect("electrode_makers") or cell_co
    mod_co = _collect("module_makers") or cell_co
    pack_co = _collect("pack_makers") or cell_co
    co_by_stage: dict[str, dict[str, list[str]]] = {
        "electrode_mfg": elec_co,
        "cell_mfg": cell_co,
        "module_assembly": mod_co,
        "pack_mfg": pack_co,
    }

    UPSTREAM_STAGES = ("precursor_mfg", "cam_mfg", "anode_mfg")
    new_labels = list(trace.node_labels)
    new_hover = list(trace.node_hover)
    sp_co = stage_product_companies or {}
    vco = visited_companies_by_step or {}
    for i, nid in enumerate(trace.nodes):
        cos: list[str] = []
        # 1) Stage-product chem_processing_<material>_<REG> nodes: pull
        # supplier names that build_trace dropped when collapsing the link.
        if nid in sp_co:
            cos = list(sp_co[nid])
        else:
            # 2) Cell/electrode/module/pack stages: lookup from product YAML.
            step = None
            for s in STAGES_TO_LABEL:
                if nid == s or nid.startswith(s + "_"):
                    step = s
                    break
            if step is None:
                # 3) Upstream stages (precursor_mfg, cam_mfg, anode_mfg):
                # lookup from supply_relationships visited graph.
                for s in UPSTREAM_STAGES:
                    if nid == s or nid.startswith(s + "_"):
                        step = s
                        break
                if step is None:
                    continue
                suffix = nid[len(step):].lstrip("_")
                # anode_mfg has form anode_mfg_anode_<REG>; strip leading
                # "anode_" so the suffix is the region code only.
                if suffix.startswith("anode_"):
                    suffix = suffix[len("anode_"):]
                reg = suffix.upper()[:3] if suffix else ""
                # First try exact (step, region); if region is empty (cam_mfg
                # / precursor_mfg are region-less), union all regions seen
                # at this step in the chain.
                if reg:
                    cos = list(vco.get((step, reg), []))
                if not cos:
                    cos = []
                    for (s2, _r), names in vco.items():
                        if s2 == step:
                            for c in names:
                                if c not in cos:
                                    cos.append(c)
            else:
                suffix = nid[len(step):].lstrip("_").upper()[:3]
                cos = co_by_stage.get(step, {}).get(suffix, [])
        if not cos:
            continue
        # Append as a new line; keep label tight.
        co_str = " / ".join(cos[:2]) + (" +" + str(len(cos) - 2) if len(cos) > 2 else "")
        new_labels[i] = trace.node_labels[i] + "<br>" + co_str
        new_hover[i] = (trace.node_hover[i] or "") + (
            f"<br><b>Operator:</b> {co_str}" if trace.node_hover[i] else f"<b>Operator:</b> {co_str}"
        )

    return SankeyTrace(
        label=trace.label,
        unit=trace.unit,
        nodes=trace.nodes,
        node_labels=new_labels,
        node_hover=new_hover,
        node_x=trace.node_x,
        node_y=trace.node_y,
        sources=trace.sources,
        targets=trace.targets,
        values=trace.values,
        raw_values=trace.raw_values,
        link_colors=trace.link_colors,
        link_hover=trace.link_hover,
        node_colors=list(trace.node_colors) if trace.node_colors else [],
    )


def _prune_islands(trace: SankeyTrace) -> SankeyTrace:
    """Drop nodes that have no forward path to any terminal (app_*/vehicle).

    Vehicle-mode artifact: when a country mines a metal but processes it
    elsewhere (e.g. Australian lithium → Chinese refining), the mining
    chain at the source country can dead-end if downstream flow uses
    only the processing-country region. Such orphan chains clutter the
    diagram with no informational value — drop them.
    """
    n = len(trace.nodes)
    if n == 0:
        return trace
    # Build forward adjacency
    fwd: dict[int, list[int]] = {i: [] for i in range(n)}
    for s, t in zip(trace.sources, trace.targets):
        fwd[s].append(t)
    # Identify terminals: app_*, application, anything that's a sink
    # (no outgoing) is also terminal-eligible if it carries the product.
    TERMINAL_PREFIXES = ("app_", "application")
    terminals = {i for i, nid in enumerate(trace.nodes)
                 if any(nid.startswith(p) for p in TERMINAL_PREFIXES)}
    if not terminals:
        return trace  # nothing to anchor against
    # Reverse-BFS reachability set
    rev: dict[int, list[int]] = {i: [] for i in range(n)}
    for s, t in zip(trace.sources, trace.targets):
        rev[t].append(s)
    reachable: set[int] = set(terminals)
    queue = list(terminals)
    while queue:
        cur = queue.pop()
        for prev in rev[cur]:
            if prev not in reachable:
                reachable.add(prev)
                queue.append(prev)
    if len(reachable) == n:
        return trace  # no islands

    # Build old→new index map for nodes that survive
    old_to_new: dict[int, int] = {}
    new_nodes: list[str] = []
    new_labels: list[str] = []
    new_hover: list[str] = []
    new_x: list[float] = []
    new_y: list[float] = []
    new_node_colors: list[str] = []
    has_node_colors = bool(trace.node_colors) and len(trace.node_colors) == n
    for old in range(n):
        if old in reachable:
            old_to_new[old] = len(new_nodes)
            new_nodes.append(trace.nodes[old])
            new_labels.append(trace.node_labels[old])
            new_hover.append(trace.node_hover[old])
            new_x.append(trace.node_x[old])
            new_y.append(trace.node_y[old])
            if has_node_colors:
                new_node_colors.append(trace.node_colors[old])
    # Filter & remap links
    keep_idx = [k for k, (s, t) in enumerate(zip(trace.sources, trace.targets))
                if s in reachable and t in reachable]
    new_sources = [old_to_new[trace.sources[k]] for k in keep_idx]
    new_targets = [old_to_new[trace.targets[k]] for k in keep_idx]
    new_values = [trace.values[k] for k in keep_idx]
    new_raw = [trace.raw_values[k] for k in keep_idx]
    new_lcolors = [trace.link_colors[k] for k in keep_idx]
    new_lhover = [trace.link_hover[k] for k in keep_idx]
    return SankeyTrace(
        label=trace.label,
        unit=trace.unit,
        nodes=new_nodes,
        node_labels=new_labels,
        node_hover=new_hover,
        node_x=new_x,
        node_y=new_y,
        sources=new_sources,
        targets=new_targets,
        values=new_values,
        raw_values=new_raw,
        link_colors=new_lcolors,
        link_hover=new_lhover,
        node_colors=new_node_colors,
    )


def _prune_dead_ends(trace: SankeyTrace) -> SankeyTrace:
    """Iteratively remove orphan nodes that have inbound flows but no
    outbound flows (and are not legitimate terminal nodes).

    A node is terminal (allowed to have no outflow) if its ID indicates
    a vehicle product, application, or end of recycling. Otherwise an
    in-degree>0 / out-degree==0 node is a dead end produced by upstream
    region leakage, and we drop the inbound flows.
    """
    TERMINAL_PREFIXES = ("app_", "application", "waste", "eol")
    nodes = list(trace.nodes)
    sources = list(trace.sources)
    targets = list(trace.targets)
    values = list(trace.values)
    raw_values = list(trace.raw_values)
    link_colors = list(trace.link_colors)
    link_hover = list(trace.link_hover)

    def _is_terminal(node_id: str) -> bool:
        if any(node_id.startswith(p) for p in TERMINAL_PREFIXES):
            return True
        # Also terminal: pack_mfg's destination (the vehicle product)
        # which is captured via app_ prefix from the application split.
        return False

    changed = True
    iter_count = 0
    while changed and iter_count < 12:
        iter_count += 1
        changed = False
        outdeg = {i: 0 for i in range(len(nodes))}
        indeg = {i: 0 for i in range(len(nodes))}
        for s, t in zip(sources, targets):
            outdeg[s] = outdeg.get(s, 0) + 1
            indeg[t] = indeg.get(t, 0) + 1
        # Find dead ends: nodes with in>0, out==0, not terminal
        dead = set()
        for i, nid in enumerate(nodes):
            if indeg.get(i, 0) > 0 and outdeg.get(i, 0) == 0 and not _is_terminal(nid):
                dead.add(i)
        if not dead:
            break
        # Drop links pointing into dead-end nodes
        keep_idx = [k for k, t in enumerate(targets) if t not in dead]
        if len(keep_idx) == len(sources):
            break  # nothing to drop
        sources = [sources[k] for k in keep_idx]
        targets = [targets[k] for k in keep_idx]
        values = [values[k] for k in keep_idx]
        raw_values = [raw_values[k] for k in keep_idx]
        link_colors = [link_colors[k] for k in keep_idx]
        link_hover = [link_hover[k] for k in keep_idx]
        changed = True

    return SankeyTrace(
        label=trace.label,
        unit=trace.unit,
        nodes=trace.nodes,
        node_labels=trace.node_labels,
        node_hover=trace.node_hover,
        node_x=trace.node_x,
        node_y=trace.node_y,
        sources=sources,
        targets=targets,
        values=values,
        raw_values=raw_values,
        link_colors=link_colors,
        link_hover=link_hover,
        node_colors=list(trace.node_colors) if trace.node_colors else ["rgba(80,80,80,0.85)"] * len(trace.nodes),
    )


def _inject_column_spacers(trace: SankeyTrace) -> SankeyTrace:
    """Force Plotly to honour fixed-x layout by adding 2 invisible spacer
    nodes to every column (top + bottom), wired by transparent spine links.

    Plotly's Sankey collapses columns that contain only one node when using
    arrangement='fixed' — single-node columns get merged into neighbours.
    By guaranteeing every column has ≥3 nodes (1 real + 2 spacers, or just
    spacers in fully empty columns), and stitching them via near-zero links,
    we keep all columns logically distinct so x positions are honoured.
    """
    if not trace.node_x:
        return trace

    # Discover every column rank we want represented. Use the full PROCESS_ORDER
    # range plus +1 for waste, so even columns where no real node lives still
    # appear visually distinct (and so adjacent column distances are uniform).
    real_ranks = sorted(set(_process_rank(n) for n in trace.nodes))
    if not real_ranks:
        return trace
    min_rank, max_rank = real_ranks[0], real_ranks[-1]
    all_ranks = list(range(min_rank, max_rank + 1))
    span = max(max_rank - min_rank, 1)
    x_left, x_right = 0.02, 0.98

    nodes = list(trace.nodes)
    node_labels = list(trace.node_labels)
    node_hover = list(trace.node_hover)
    node_x = list(trace.node_x)
    node_y = list(trace.node_y)
    node_colors = list(trace.node_colors) if trace.node_colors else \
        ["rgba(80,80,80,0.85)"] * len(nodes)
    sources = list(trace.sources)
    targets = list(trace.targets)
    values = list(trace.values)
    raw_values = list(trace.raw_values)
    link_colors = list(trace.link_colors)
    link_hover = list(trace.link_hover)

    transparent = "rgba(0,0,0,0)"
    top_idx_by_rank: dict[int, int] = {}
    bot_idx_by_rank: dict[int, int] = {}
    for r in all_ranks:
        x = x_left + (r - min_rank) / span * (x_right - x_left)
        # Top spacer
        nodes.append(f"_spacer_top_{r}")
        node_labels.append("")
        node_hover.append("")
        node_x.append(x)
        node_y.append(0.001)
        node_colors.append(transparent)
        top_idx_by_rank[r] = len(nodes) - 1
        # Bottom spacer
        nodes.append(f"_spacer_bot_{r}")
        node_labels.append("")
        node_hover.append("")
        node_x.append(x)
        node_y.append(0.999)
        node_colors.append(transparent)
        bot_idx_by_rank[r] = len(nodes) - 1

    # Wire a transparent spine: top[r] -> top[r+1], bot[r] -> bot[r+1].
    # Tiny non-zero value so Plotly doesn't drop the link.
    eps = 1e-6
    for a, b in zip(all_ranks, all_ranks[1:]):
        sources.append(top_idx_by_rank[a]); targets.append(top_idx_by_rank[b])
        values.append(eps); raw_values.append(0.0)
        link_colors.append(transparent); link_hover.append("")
        sources.append(bot_idx_by_rank[a]); targets.append(bot_idx_by_rank[b])
        values.append(eps); raw_values.append(0.0)
        link_colors.append(transparent); link_hover.append("")

    # Peg each real node's topological depth to its assigned rank: add an
    # invisible incoming edge from spacer_top_{rank-1}. Plotly's Sankey
    # column-assignment uses longest-path-from-source, so this forces
    # depth(node) >= rank, putting it in the right column even when its
    # natural upstream chain is short or missing in vehicle mode.
    n_real = len(trace.nodes)
    for i in range(n_real):
        nid = trace.nodes[i]
        r = _process_rank(nid)
        if r - 1 in top_idx_by_rank:
            sources.append(top_idx_by_rank[r - 1])
            targets.append(i)
            values.append(eps); raw_values.append(0.0)
            link_colors.append(transparent); link_hover.append("")

    return SankeyTrace(
        label=trace.label,
        unit=trace.unit,
        nodes=nodes,
        node_labels=node_labels,
        node_hover=node_hover,
        node_x=node_x,
        node_y=node_y,
        sources=sources,
        targets=targets,
        values=values,
        raw_values=raw_values,
        link_colors=link_colors,
        link_hover=link_hover,
        node_colors=node_colors,
    )


def render_vehicle_html(opts: Options) -> str:
    """Render a multi-trim vehicle Sankey at pack-level kg."""
    # Reset the ref-number map so each render starts numbering at [1].
    _reset_ref_numbers()
    assert opts.supply_chain_path is not None
    assert opts.vehicle is not None
    sc_data = load_yaml(opts.supply_chain_path)
    products = find_matching_products(sc_data, opts.vehicle)
    if not products:
        sys.stderr.write(f"ERROR: no products match '{opts.vehicle}' in {opts.supply_chain_path}\n")
        sys.exit(1)
    relationships = sc_data.get("supply_relationships") or []
    root = _project_root_from_route(opts.supply_chain_path)
    materials_path = root / "schema" / "materials_registry.yaml"
    refs_path = root / "sources" / "references.yaml"
    materials = load_materials_registry(materials_path)
    refs = load_references(refs_path)

    # Build one (label, mass_trace, ew_trace) per product
    panels: list[tuple[str, dict[str, Any], Optional[SankeyTrace], Optional[SankeyTrace]]] = []
    for product in products:
        rf = product.get("route_file")
        pack_kWh_raw = product.get("pack_kWh")
        # pack_kWh may be a number or a range string like "60-78" — pick midpoint
        if isinstance(pack_kWh_raw, (int, float)):
            pack_kWh = float(pack_kWh_raw)
        elif isinstance(pack_kWh_raw, str) and "-" in pack_kWh_raw:
            lo, hi = pack_kWh_raw.split("-", 1)
            pack_kWh = (float(lo) + float(hi)) / 2
        else:
            sys.stderr.write(f"WARN: skipping product {product.get('id')}: no pack_kWh\n")
            continue
        if not rf:
            sys.stderr.write(f"WARN: skipping product {product.get('id')}: no route_file\n")
            continue
        route_path = (root / rf).resolve()
        route = load_route(route_path)
        visited = derive_visited_regions(product, relationships)
        product_label = f"{product.get('product')} ({product.get('pack_chemistry')})"
        scaled = _filter_route_for_vehicle(
            route, visited, pack_kWh, materials,
            product_label=product_label,
        )
        # Apply opts filters then split mass / energy_water
        filtered = _filter_flows(scaled.flows, opts, materials)
        mass_flows, ew_flows = _classify_traces(filtered)
        mass_trace = build_trace(
            mass_flows, materials, refs,
            label=f"{product.get('product')} — mass",
            unit_label="kg/pack",
            log_scale=opts.log_scale,
            no_region_split=opts.no_region_split,
        ) if mass_flows else None
        ew_trace = build_trace(
            ew_flows, materials, refs,
            label=f"{product.get('product')} — energy/water",
            unit_label="MJ or L /pack",
            log_scale=opts.log_scale,
            no_region_split=opts.no_region_split,
        ) if ew_flows else None
        # (4) Iteratively drop dead-end nodes (no outgoing flow that's not
        # a terminal). Keeps re-running until stable. Removes orphan
        # "Cell · China" / "PCAM · S. Korea" type artefacts.
        sp_companies = _collect_stage_product_companies(filtered, materials)
        vco = visited.companies_by_step
        if mass_trace:
            mass_trace = _prune_dead_ends(mass_trace)
            mass_trace = _prune_islands(mass_trace)
            mass_trace = _attach_company_labels(mass_trace, product, sp_companies, vco)
        if ew_trace:
            ew_trace = _prune_dead_ends(ew_trace)
            ew_trace = _attach_company_labels(ew_trace, product, sp_companies, vco)
            # Skip island-prune on EW: energy/water flows terminate at a
            # global sink that doesn't connect to app_vehicle. Every node
            # would look like an "island" by the mass-panel definition.
        label = f"{product.get('product')} ({product.get('pack_chemistry')}, {pack_kWh:g} kWh)"
        panels.append((label, product, mass_trace, ew_trace))

    if not panels:
        sys.stderr.write("ERROR: no renderable panels.\n")
        sys.exit(1)

    # One row per panel; if a panel has both mass and EW, give it two rows.
    rows_per_panel: list[int] = []
    for _label, _product, mass_trace, ew_trace in panels:
        rows_per_panel.append((1 if mass_trace else 0) + (1 if ew_trace else 0))
    total_rows = sum(rows_per_panel)
    # Build one Plotly Figure per panel and concatenate the HTML <div>s.
    # Plotly.js's multi-subplot Sankey ignores fixed x coordinates after the
    # first sub-panel, collapsing columns visually. Single-figure Sankeys
    # respect arrangement='fixed' correctly.
    title = (
        f"<b>{products[0].get('company')} — {opts.vehicle}</b><br>"
        f"<span style='font-size:11px;color:#555'>"
        f"Per-pack absolute mass; chains derived from {opts.supply_chain_path.name}; "
        f"regions filtered to disclosed suppliers."
        f"</span>"
    )
    panel_specs: list[tuple[str, SankeyTrace, int]] = []  # (title, trace, height)
    for (label, _product, mass_trace, ew_trace) in panels:
        if mass_trace:
            panel_specs.append((f"{label} — Mass (kg/pack)", mass_trace, 1100))
        if ew_trace:
            panel_specs.append((f"{label} — Energy & water (MJ or L /pack)", ew_trace, 220))

    # D3 renderer: emit a self-contained HTML using d3-sankey with strict
    # rank-pinned column placement (no Plotly column collapse).
    if opts.renderer == "d3":
        from .d3_renderer import trace_to_panel, render_html as render_d3_html
        column_labels = {
            0: "Mining", 1: "Beneficiation", 2: "Chemical Processing",
            3: "Precursor Mfg", 4: "CAM / Anode Mfg", 5: "Electrode Mfg",
            6: "Cell Mfg", 7: "Module Assembly", 8: "Pack Mfg",
            9: "Application", 10: "EoL / Waste",
        }
        d3_panels = [
            trace_to_panel(t, title=ttl, height=h, process_rank=_process_rank,
                           subtitle="Per-pack absolute mass; chains derived from "
                                    f"{opts.supply_chain_path.name}; "
                                    "regions filtered to disclosed suppliers.")
            for (ttl, t, h) in panel_specs
        ]
        d3_title = f"{products[0].get('company')} — {opts.vehicle}"
        d3_subtitle = (f"Per-pack absolute mass; chains from "
                       f"{opts.supply_chain_path.name}; "
                       "regions filtered to disclosed suppliers.")
        return render_d3_html(d3_title, d3_subtitle, d3_panels, column_labels)

    panel_htmls: list[str] = []
    for i, (panel_title, panel_trace, panel_height) in enumerate(panel_specs):
        sankey = _make_sankey(panel_trace, panel_title)
        f = go.Figure([sankey])
        layout_kwargs = dict(
            title=dict(text=panel_title, x=0.02, xanchor="left",
                       font=dict(size=13)),
            font=dict(family="Helvetica, Arial, sans-serif", size=11),
            margin=dict(l=20, r=20, t=50, b=20),
            width=2400,
            height=panel_height,
        )
        f.update_layout(**layout_kwargs)
        # First panel includes plotly.js script tag; later panels don't.
        include_js = "cdn" if i == 0 else False
        panel_html = pio.to_html(
            f, include_plotlyjs=include_js, full_html=False,
        )
        panel_htmls.append(panel_html)

    # Wrap with a top-level title and the panels stacked vertically.
    full_html = (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<title>{products[0].get('company')} — {opts.vehicle}</title>"
        "<style>body{font-family:Helvetica,Arial,sans-serif;margin:20px;}"
        "h1{font-size:16px;margin:0 0 4px 0;}"
        ".subtitle{font-size:11px;color:#555;margin-bottom:12px;}"
        ".panel{margin-bottom:8px;}</style></head><body>"
        f"<h1>{products[0].get('company')} — {opts.vehicle}</h1>"
        f"<div class='subtitle'>Per-pack absolute mass; chains derived from "
        f"{opts.supply_chain_path.name}; regions filtered to disclosed suppliers.</div>"
        + "".join(f"<div class='panel'>{h}</div>" for h in panel_htmls)
        + "</body></html>"
    )
    return full_html


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_args(argv: list[str]) -> Options:
    """Parse argv into an Options object."""
    p = argparse.ArgumentParser(
        description="Render a battery route YAML as an interactive Sankey HTML."
    )
    p.add_argument("--route", required=False, type=Path, default=None,
                   help="Path to route YAML (e.g. a route YAML next to schema/ and sources/). "
                        "Required unless --vehicle is set.")
    p.add_argument("--vehicle", type=str, default=None,
                   help="Render a specific vehicle/product at pack-level kg "
                        "(reads --supply-chain). Example: --vehicle 'Mustang Mach-E'")
    p.add_argument("--supply-chain", type=Path, default=None,
                   help="Path to supply-chain YAML (e.g. examples/materials/data/ford.yaml). "
                        "Required when --vehicle is set.")
    p.add_argument("--out", type=Path, default=None,
                   help="Output HTML path (default: outputs/<chem>_<route>_sankey.html "
                        "or outputs/<vehicle>_pack_sankey.html in vehicle mode)")
    p.add_argument("--log-scale", action="store_true",
                   help="Log10(value+1) the link widths for visual readability")
    p.add_argument("--material", type=str, default=None,
                   help="Filter to flows whose category/name contains this string")
    p.add_argument("--no-region-split", action="store_true",
                   help="Collapse region_breakdown into single process-step nodes")
    p.add_argument("--energy-water-only", action="store_true",
                   help="Render only the energy/water sub-Sankey")
    p.add_argument("--validate", action="store_true",
                   help="Strict: exit non-zero if validation produces warnings")
    p.add_argument("--renderer", choices=["plotly", "d3"], default="plotly",
                   help="Output renderer (default plotly). 'd3' emits a "
                        "self-contained HTML using d3-sankey with strict "
                        "rank-based column placement.")
    args = p.parse_args(argv)
    if args.vehicle and not args.supply_chain:
        p.error("--vehicle requires --supply-chain")
    if not args.vehicle and not args.route:
        p.error("either --route or --vehicle is required")
    if args.vehicle:
        # Vehicle mode: derive default out path from vehicle name
        slug = re.sub(r"[^A-Za-z0-9]+", "_", args.vehicle).strip("_") or "vehicle"
        root = (args.supply_chain.resolve().parent.parent.parent
                if args.supply_chain else Path.cwd())
        out = args.out or (root / "outputs" / f"{slug}_pack_sankey.html")
    else:
        root = _project_root_from_route(args.route)
        out = args.out or _default_out_path(args.route, root)
    return Options(
        route_path=args.route,
        out_path=out,
        log_scale=args.log_scale,
        material_filter=args.material,
        no_region_split=args.no_region_split,
        energy_water_only=args.energy_water_only,
        validate=args.validate,
        vehicle=args.vehicle,
        supply_chain_path=args.supply_chain,
        renderer=args.renderer,
    )


def main(argv: Optional[list[str]] = None) -> int:
    """Orchestrate load → validate → render → write."""
    opts = parse_args(argv if argv is not None else sys.argv[1:])

    # Vehicle mode dispatches to a separate pipeline
    if opts.vehicle:
        html = render_vehicle_html(opts)
        opts.out_path.parent.mkdir(parents=True, exist_ok=True)
        opts.out_path.write_text(html, encoding="utf-8")
        sys.stderr.write(f"Wrote {opts.out_path}\n")
        # Bibliography uses the global ref-number map populated during render.
        # Reload references for citation metadata.
        assert opts.supply_chain_path is not None
        veh_root = _project_root_from_route(opts.supply_chain_path)
        veh_refs = load_references(veh_root / "sources" / "references.yaml")
        md_path, csv_path = write_bibliography(opts.out_path, veh_refs)
        if md_path != opts.out_path:
            sys.stderr.write(f"Wrote {md_path}\n")
            sys.stderr.write(f"Wrote {csv_path}\n")
        return 0

    assert opts.route_path is not None
    root = _project_root_from_route(opts.route_path)
    materials_path = root / "schema" / "materials_registry.yaml"
    refs_path = root / "sources" / "references.yaml"

    route = load_route(opts.route_path)
    materials = load_materials_registry(materials_path)
    refs = load_references(refs_path)

    warnings = validate(route, materials, refs)
    if warnings:
        sys.stderr.write(f"Validation: {len(warnings)} warning(s)\n")
        for w in warnings:
            sys.stderr.write(f"  - {w}\n")
    else:
        sys.stderr.write("Validation: 0 warnings\n")

    if opts.validate and warnings:
        sys.stderr.write(
            "ERROR: --validate set; refusing to render with warnings.\n"
        )
        return 1

    html = render_html(route, materials, refs, opts)
    opts.out_path.parent.mkdir(parents=True, exist_ok=True)
    opts.out_path.write_text(html, encoding="utf-8")
    sys.stderr.write(f"Wrote {opts.out_path}\n")
    md_path, csv_path = write_bibliography(opts.out_path, refs)
    if md_path != opts.out_path:
        sys.stderr.write(f"Wrote {md_path}\n")
        sys.stderr.write(f"Wrote {csv_path}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
