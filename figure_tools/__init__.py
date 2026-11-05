"""figure-tools: waterfall and Sankey figures from plain Python data.

    from figure_tools import Waterfall, Sankey

See README.md for a 60-second quickstart.
"""
from .waterfall import Waterfall
from .sankey import Sankey
from .style import OKABE_ITO, Style, deck, paper

__version__ = "0.1.0"
__all__ = ["Waterfall", "Sankey", "Style", "paper", "deck", "OKABE_ITO"]
