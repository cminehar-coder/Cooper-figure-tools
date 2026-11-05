"""
style.py - the figure style. This is the layer that decides whether a figure looks
like a journal figure or like a slide someone made in a hurry.

Everything is in millimetres and points, because that is what publishers specify.
The canvas is set up so one data unit equals one millimetre exactly, and font
sizes and line widths stay in points, so a spec that says "no thinner than 0.5
point" is expressible without arithmetic.

TWO PROFILES, and the rule for choosing between them:

  paper   goes into a manuscript. Greyscale structure with one accent, hairline
          rules, 8 pt floor. Colour must never be the only thing carrying a
          distinction, because ACS says so and because referees print in mono.
  deck    goes onto a 16:9 slide. Saturated two-colour palette, heavier strokes,
          big type, because it is read from the back of a room.

Do not mix them: a heavy slide figure looks wrong in a manuscript and a
hairline paper figure disappears on a projector.

VERIFIED SPECS, from ACS's own author guidelines PDF for Environmental Science
& Technology (coden=esthag, retrieved 2026-08-26, "Appendix 2: Preparing
Graphics"), quoted:

  "Single-column graphics can be sized up to 240 points wide (3.33 in.) and
   double-column graphics must be sized between 300 and 504 points (4.167 in.
   and 7 in.). The maximum depth for all graphics is 660 points (9.167 in.)
   including the caption"
  "Lettering should be no smaller than 4.5 points in the final published format."
  "Helvetica or Arial fonts work well for lettering. Lines should be no thinner
   than 0.5 point."
  "Black and white line art, 1200 dpi / Grayscale art, 600 dpi / Color art, 300 dpi"
  "Avoid relying on color alone to represent information."
  "The Web Content Accessibility Guidelines (WCAG) define minimum contrast
   requirements of 4.5:1 for text and 3:1 for nontext elements"

Note the internal conflict in that document, which we resolve toward the
stricter number: Appendix 2 says lettering may go to 4.5 pt, while the
manuscript preparation checklist on page 1 says "Tables/Schemes/Figures: Text
should be clear and legible, with fonts no smaller than 8 pt." We use 8 pt as
the floor. A figure that passes the stricter rule passes both.

Cell Press widths (Joule, Cell Reports Physical Science, iScience) are recorded
as UNVERIFIED: cell.com returned 403 to two fetch attempts, so the numbers below
come from search excerpts of Cell Press pages rather than from the page itself.
Verify before submitting to a Cell Press venue. See PROVENANCE in this file.

"""
from __future__ import annotations

import warnings
from dataclasses import dataclass, field, replace

from matplotlib.font_manager import FontProperties, findfont

MM_PER_PT = 25.4 / 72.0
PT_PER_MM = 72.0 / 25.4


def mm(pt: float) -> float:
    """Points to millimetres."""
    return pt * MM_PER_PT


def pt(mm_value: float) -> float:
    """Millimetres to points."""
    return mm_value * PT_PER_MM


# --------------------------------------------------------------------------
# Palettes
# --------------------------------------------------------------------------

# The colourblind-safe eight.
#
# VERIFIED against the primary source: Wong, B. "Points of view: Color blindness."
# Nature Methods 8(6):441 (June 2011), Figure 2, "Colors optimized for color-blind
# individuals". Each hex below was converted from the RGB (1-255) triple printed in
# Wong's own table and checked. His colour names are given beside our short keys,
# because his differ from the ones in common circulation.
#
# On the name: this is universally called the Okabe-Ito palette, and the constant
# keeps that name because it is what anyone searching for it will type. But Wong's
# column does not cite Okabe and Ito anywhere; its four references are Wong 2010,
# Albrecht 2010, Deeb 2003 and Jones 2011. That attribution is therefore COMMON BUT
# UNVERIFIED here. Do not repeat it as sourced without reading their 2008 text.
OKABE_ITO = {
    "black":     "#000000",   # Wong: "Black",          RGB   0,   0,   0
    "orange":    "#E69F00",   # Wong: "Orange",         RGB 230, 159,   0
    "sky":       "#56B4E9",   # Wong: "Sky blue",       RGB  86, 180, 233
    "green":     "#009E73",   # Wong: "Bluish green",   RGB   0, 158, 115
    "yellow":    "#F0E442",   # Wong: "Yellow",         RGB 240, 228,  66
    "blue":      "#0072B2",   # Wong: "Blue",           RGB   0, 114, 178
    "vermilion": "#D55E00",   # Wong: "Vermillion",     RGB 213,  94,   0
    "purple":    "#CC79A7",   # Wong: "Reddish purple", RGB 204, 121, 167
}

# --------------------------------------------------------------------------
# Luminance
# --------------------------------------------------------------------------
# These live here, beside the palettes, because they are what makes a palette
# claim checkable. 

def _srgb_to_lin(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(hex_color: str) -> float:
    """WCAG 2.x relative luminance."""
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * _srgb_to_lin(r) + 0.7152 * _srgb_to_lin(g) + 0.0722 * _srgb_to_lin(b)


def contrast_ratio(a: str, b: str) -> float:
    la, lb = relative_luminance(a), relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def greyscale_ramp(palette: dict, threshold: float = 0.06) -> list[str]:
    """
    The subset of a palette that survives a greyscale print, in luminance order.

    Sort on relative luminance, then walk upward keeping any colour at least
    `threshold` brighter than the last one kept. 0.06 is the number check.py
    already uses to flag two palette entries as a hue-alone collision, so a ramp
    built here passes that check by construction.

    Computing this rather than writing the list out by hand is deliberate. The
    hand-written version of this ramp put Okabe-Ito vermilion next to bluish
    green, which are 0.035 apart and merge, under a docstring claiming they were
    0.06 apart. The property is worth more asserted in code than in prose.
    """
    ordered = sorted(palette.values(), key=relative_luminance)
    out: list[str] = []
    for c in ordered:
        if not out or relative_luminance(c) - relative_luminance(out[-1]) >= threshold:
            out.append(c)
    return out

PROVENANCE = {
    "acs_est": (
        "VERIFIED. ACS author guidelines PDF, coden=esthag, Appendix 2 "
        "'Preparing Graphics', retrieved 2026-08-26 from "
        "researcher-resources.acs.org/publish/author_guidelines/pdf?coden=esthag"
    ),
    "cell_press": (
        "UNVERIFIED. cell.com/joule/figureguidelines and /joule/authors both "
        "returned HTTP 403 on 2026-08-26. Widths below (85 / 114 / 174 mm two-column "
        "journals; 112 / 172 mm for Joule and Matter) come from search-result "
        "excerpts of Cell Press pages, not from the pages themselves. Confirm "
        "before submitting."
    ),
    "nature": (
        "UNVERIFIED. nature.com author pages are behind an IdP redirect. The "
        "89 / 120 / 183 mm figures in circulation are widely repeated but were "
        "not confirmed against Nature's own page in this session."
    ),
    "okabe_ito": (
        "VERIFIED, values. Every hex in OKABE_ITO was converted from the RGB "
        "(1-255) triples printed in Figure 2 of Wong, B. 'Points of view: Color "
        "blindness.' Nature Methods 8(6):441 (June 2011), read from the column "
        "itself on 2026-08-26, and all eight match. "
        "UNVERIFIED, attribution. The palette is universally called Okabe-Ito, but "
        "Wong's column does not cite Okabe and Ito; its references are Wong 2010, "
        "Albrecht 2010, Deeb 2003 and Jones 2011. Do not present the Okabe-Ito "
        "attribution as sourced without reading their 2008 text."
    ),
    "rougier": (
        "VERIFIED. Rougier NP, Droettboom M, Bourne PE. 'Ten Simple Rules for "
        "Better Figures.' PLoS Comput Biol 10(9):e1003833 (2014), "
        "doi:10.1371/journal.pcbi.1003833. Rule titles read from the article text "
        "at PMC4161295 on 2026-08-26."
    ),
}


# --------------------------------------------------------------------------
# Column widths
# --------------------------------------------------------------------------

# Named width -> millimetres. Tagged by the provenance keys above.
WIDTHS = {
    # ACS, verified. 240 pt and 504 pt converted exactly.
    "acs_single": 240 * MM_PER_PT,          # 84.67 mm
    "acs_double": 504 * MM_PER_PT,          # 177.80 mm
    "acs_double_min": 300 * MM_PER_PT,      # 105.83 mm, the floor for two-column
    # Cell Press, unverified.
    "cell_single": 85.0,
    "cell_1p5": 114.0,
    "cell_full": 174.0,
    "joule_single": 112.0,
    "joule_full": 172.0,
    # Nature, unverified.
    "nature_single": 89.0,
    "nature_1p5": 120.0,
    "nature_double": 183.0,
    # A 16:9 slide at 13.333 x 7.5 in.
    "slide": 13.3333 * 25.4,                # 338.67 mm
}

# ACS maximum depth including caption, verified: 660 pt.
ACS_MAX_DEPTH_MM = 660 * MM_PER_PT          # 232.83 mm


# --------------------------------------------------------------------------
# Font resolution, and the silent-bold trap
# --------------------------------------------------------------------------

class FontWeightError(RuntimeError):
    """Raised when a requested family cannot actually render the weights asked of it."""


def resolve_family(candidates: list[str], need_italic: bool = True) -> str:
    """
    Return the first candidate family that genuinely has distinct regular, bold
    and (optionally) italic faces registered with matplotlib.

    This exists because of a real trap on macOS. Helvetica Neue, Helvetica,
    Charter, Avenir Next and Palatino ship as .ttc collections, and matplotlib
    registers only face index 0 from a .ttc. Asking such a family for
    weight='bold' returns the regular face with no warning at all: the text
    renders, it is simply not bold, and the type hierarchy you designed silently
    does not exist. Measured on this machine 2026-08-26 with matplotlib 3.10.9:
    of every installed family, only Arial, Arial Narrow, Courier New, Georgia,
    Times New Roman, Trebuchet MS, Verdana, DejaVu Serif and the STIX faces have
    real bold and italic registered.

    So we check rather than hope, and we fail loudly rather than ship flat type.
    """
    tried = []
    for fam in candidates:
        try:
            regular = findfont(FontProperties(family=fam, weight="normal"),
                               fallback_to_default=False)
            bold = findfont(FontProperties(family=fam, weight="bold"),
                            fallback_to_default=False)
        except Exception:
            tried.append(f"{fam}: not installed")
            continue
        if regular == bold:
            tried.append(f"{fam}: bold collapses to regular ({regular.split('/')[-1]})")
            continue
        if need_italic:
            try:
                italic = findfont(FontProperties(family=fam, style="italic"),
                                  fallback_to_default=False)
            except Exception:
                tried.append(f"{fam}: no italic")
                continue
            if italic == regular:
                tried.append(f"{fam}: italic collapses to regular")
                continue
        return fam
    raise FontWeightError(
        "No candidate font family has real bold"
        + (" and italic" if need_italic else "")
        + " faces registered with matplotlib.\n  "
        + "\n  ".join(tried)
        + "\nInstall a family with separate face files, or add one to the candidate list."
    )


# --------------------------------------------------------------------------
# The style object
# --------------------------------------------------------------------------

@dataclass
class Style:
    """
    Every visual decision in one place. Change it here and the whole figure set
    moves together, which is the point: a set that shares a style reads as one
    argument, and a set that does not reads as four people's work.
    """
    name: str

    # canvas
    width_mm: float
    max_depth_mm: float | None = None

    # type, in points
    family: str = "Arial"
    size_title: float = 9.0        # the figure's one-line claim
    size_head: float = 8.5         # a panel's name
    size_body: float = 8.0         # a panel's content lines
    size_annot: float = 8.0        # arrow labels, source lines
    size_note: float = 8.0         # the footnote under the figure
    leading: float = 1.42          # line height as a multiple of size

    # ink
    ink: str = "#1A1A1A"           # primary text
    muted: str = "#6B6B70"         # secondary text, source lines, arrow labels
    rule: str = "#8E8E94"          # panel borders. Dark enough to clear
                                   # WCAG 3:1 for non-text, which ACS cites.
    tint: str = "#F4F4F6"          # panel header wash
    bg: str = "#FFFFFF"
    accent: str = OKABE_ITO["blue"]
    accent2: str = OKABE_ITO["vermilion"]

    # strokes, in points
    lw_rule: float = 0.6           # panel borders
    lw_connector: float = 0.7      # arrows
    lw_emphasis: float = 1.1       # the one thing you want seen

    # geometry, in millimetres
    unit: float = 2.0              # the spacing scale; every gap is a multiple
    pad_x: float = 3.2             # panel interior padding, horizontal
    pad_y: float = 2.6             # panel interior padding, vertical
    gap: float = 12.0              # between panels in a row
    radius: float = 0.6            # corner radius. Small on purpose.
    margin: float = 3.0            # canvas margin on all four sides
    label_gap: float = 1.6         # clearance between an arrow and its label
    head_len: float = 2.0          # arrowhead length
    head_w: float = 1.7            # arrowhead width

    # export
    dpi: int = 600
    formats: tuple = ("pdf", "svg", "png")

    # policy
    min_font_pt: float = 8.0       # the ACS checklist floor, the stricter reading
    abs_min_pt: float = 4.5        # ACS Appendix 2 absolute floor. Applies only
                                   # to mathtext sub and superscripts, which every
                                   # journal sets smaller than body type.
    min_line_pt: float = 0.5       # ACS Appendix 2
    palette: dict = field(default_factory=lambda: dict(OKABE_ITO))

    def line_h(self, size: float) -> float:
        """Line height in millimetres for a given point size."""
        return mm(size * self.leading)

    def scaled(self, **kw) -> "Style":
        return replace(self, **kw)


def paper(width: str = "acs_double") -> Style:
    """
    A manuscript figure. Structure carried by hairlines and type, not by heavy
    fills, because that is what reproduces well at 84 mm and in greyscale.
    """
    fam = resolve_family(["Arial", "Helvetica", "Liberation Sans", "DejaVu Sans"])
    return Style(
        name=f"paper:{width}",
        width_mm=WIDTHS[width],
        max_depth_mm=ACS_MAX_DEPTH_MM,
        family=fam,
    )


def deck() -> Style:
    """
    A slide figure. Bigger type, stronger strokes, saturated colour, because
    it is read at distance.
    """
    fam = resolve_family(["Arial", "Helvetica", "Liberation Sans", "DejaVu Sans"])
    return Style(
        name="deck",
        width_mm=WIDTHS["slide"],
        max_depth_mm=7.5 * 25.4,
        family=fam,
        size_title=18.0,
        size_head=15.5,
        size_body=13.5,
        size_annot=12.0,
        size_note=12.0,
        ink="#1A1A1A",
        muted="#5F6063",
        rule="#8E8E94",
        tint="#F3F1F1",
        accent=OKABE_ITO["vermilion"],
        accent2=OKABE_ITO["blue"],
        lw_rule=1.1,
        lw_connector=1.5,
        lw_emphasis=2.4,
        unit=4.0,
        pad_x=6.0,
        pad_y=5.2,
        gap=16.0,
        margin=4.0,
        radius=1.2,
        label_gap=3.0,
        head_len=4.2,
        head_w=3.4,
        dpi=300,
        min_font_pt=12.0,
        abs_min_pt=7.0,
        min_line_pt=0.75,
        palette=dict(OKABE_ITO),
    )


PROFILES = {"paper": paper, "deck": deck}


def get(profile: str = "paper", **kw) -> Style:
    if profile not in PROFILES:
        raise KeyError(f"unknown profile {profile!r}; have {sorted(PROFILES)}")
    return PROFILES[profile](**kw)


def rcparams(style: Style) -> dict:
    """
    matplotlib settings that must hold for any figure we export.

    fonttype 42 keeps PDF text as real TrueType rather than Type 3, which is
    what ACS wants and what lets a copy-editor select the text. svg.fonttype
    'none' keeps SVG text as <text> rather than outlines, so the file stays
    editable in Illustrator or Inkscape. Both of these are the difference
    between a figure someone can fix and a figure they have to redraw.
    """
    return {
        "font.family": "sans-serif",
        "font.sans-serif": [style.family, "Arial", "Helvetica", "DejaVu Sans"],
        "text.color": style.ink,
        "pdf.fonttype": 42,
        "ps.fonttype": 42,
        "svg.fonttype": "none",
        "figure.facecolor": style.bg,
        "savefig.facecolor": style.bg,
        "axes.linewidth": style.lw_rule,
        "mathtext.fontset": "custom",
        "mathtext.rm": style.family,
        "mathtext.it": f"{style.family}:italic",
        "mathtext.bf": f"{style.family}:bold",
        "mathtext.default": "it",
    }


def check_font_health(style: Style) -> list[str]:
    """Return a list of problems, empty if the style's font can do its job."""
    problems = []
    try:
        resolve_family([style.family])
    except FontWeightError as exc:
        problems.append(str(exc))
    smallest = min(style.size_annot, style.size_note, style.size_body)
    if smallest < style.min_font_pt:
        problems.append(
            f"smallest type is {smallest} pt, below the {style.min_font_pt} pt floor "
            f"for profile {style.name!r}"
        )
    thinnest = min(style.lw_rule, style.lw_connector)
    if thinnest < style.min_line_pt:
        problems.append(
            f"thinnest line is {thinnest} pt, below the {style.min_line_pt} pt floor"
        )
    return problems
