"""Palette, fonts and status logic for the dashboard.

The colours are a validated dark-mode set: a fixed eight-slot categorical order
(assigned by series identity, never cycled or re-ranked), a separate reserved
status palette, and recessive chrome one step off the surface. Status colour is
always paired with a text label on screen, so nothing is encoded by colour
alone - which matters more than usual here, because a 5" TFT at an angle
flattens hue badly.
"""

from __future__ import annotations

import math
import tkinter.font as tkfont

# -- surfaces and ink ------------------------------------------------------- #

PAGE = "#0d0d0d"        # behind the panels
PANEL = "#1a1a19"       # chart surface
PANEL_ALT = "#202020"   # table stripe
INK = "#f0efec"         # primary
INK2 = "#c3c2b7"        # secondary
MUTED = "#898781"       # axis labels, units
GRID = "#2c2c2a"        # hairline gridlines
AXIS = "#383835"        # baseline

# -- categorical series slots, in fixed order ------------------------------- #

SERIES = {
    "blue": "#3987e5",      # slot 1 - latency, download/rx
    "orange": "#d95926",    # slot 2 - upload/tx
    "aqua": "#199e70",      # slot 3 - jitter
    "yellow": "#c98500",    # slot 4
    "magenta": "#d55181",   # slot 5
    "green": "#008300",     # slot 6
    "violet": "#9085e9",    # slot 7 - TCP connect
    "red": "#e66767",       # slot 8 - interface errors
}

# -- reserved status palette (never used for a series) ---------------------- #

STATUS = {
    "ok": "#0ca30c",
    "warn": "#fab219",
    "serious": "#ec835a",
    "crit": "#d03b3b",
    "none": "#383835",
}
STATUS_TEXT = {
    "ok": "OK",
    "warn": "WARN",
    "serious": "DEGRADED",
    "crit": "CRIT",
    "none": "NO DATA",
}
STATUS_ORDER = ["none", "ok", "warn", "serious", "crit"]


# --------------------------------------------------------------------------- #
# Colour maths
# --------------------------------------------------------------------------- #


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def blend(fg: str, bg: str, t: float) -> str:
    """Mix ``fg`` into ``bg``; ``t=0`` is all background. Tk has no alpha channel,
    so translucency has to be baked into the colour."""
    t = max(0.0, min(1.0, t))
    f, b = _hex_to_rgb(fg), _hex_to_rgb(bg)
    out = tuple(round(b[i] + (f[i] - b[i]) * t) for i in range(3))
    return "#%02x%02x%02x" % out


def band_color(series_color: str) -> str:
    """The min/max range band behind a mean line: the same hue, pushed back."""
    return blend(series_color, PANEL, 0.32)


# --------------------------------------------------------------------------- #
# Status levels
# --------------------------------------------------------------------------- #


def level(value: float | None, warn: float, crit: float,
          lower_is_better: bool = True) -> str:
    """Map a measurement onto ``none``/``ok``/``warn``/``crit``."""
    if value is None:
        return "none"
    try:
        if math.isnan(value):
            return "none"
    except TypeError:
        return "none"
    if lower_is_better:
        if value >= crit:
            return "crit"
        if value >= warn:
            return "warn"
    else:
        if value <= crit:
            return "crit"
        if value <= warn:
            return "warn"
    return "ok"


def worst(*levels: str) -> str:
    """The most severe of several levels, ignoring missing data."""
    best = "none"
    for lv in levels:
        if lv not in STATUS_ORDER:
            continue
        if STATUS_ORDER.index(lv) > STATUS_ORDER.index(best):
            best = lv
    return best


def status_color(lv: str) -> str:
    return STATUS.get(lv, STATUS["none"])


def status_text(lv: str) -> str:
    return STATUS_TEXT.get(lv, "--")


# --------------------------------------------------------------------------- #
# Fonts
# --------------------------------------------------------------------------- #

_SANS_CANDIDATES = ("DejaVu Sans", "Liberation Sans", "Noto Sans", "FreeSans",
                    "Segoe UI", "Arial", "Helvetica")
_MONO_CANDIDATES = ("DejaVu Sans Mono", "Liberation Mono", "Noto Sans Mono",
                    "FreeMono", "Consolas", "Courier New", "Courier")


class Theme:
    """Fonts scaled to the panel. Build it after the Tk root exists."""

    def __init__(self, root, height: int = 480):
        scale = max(0.7, min(2.0, height / 480.0))
        try:
            available = set(tkfont.families(root))
        except Exception:      # pragma: no cover - headless Tk oddities
            available = set()

        # "Helvetica" and "Courier" are aliases Tk always resolves, so they are
        # safe last resorts; a named font like TkDefaultFont is not a family and
        # renders unpredictably inside a font tuple.
        sans = next((f for f in _SANS_CANDIDATES if f in available), "Helvetica")
        mono = next((f for f in _MONO_CANDIDATES if f in available), "Courier")
        self.sans_family = sans
        self.mono_family = mono

        def px(size: float) -> int:
            return -max(7, int(round(size * scale)))

        # Negative sizes are pixels in Tk, which keeps the layout predictable
        # across the Pi's DPI reporting.
        self.hero = (sans, px(36), "bold")
        self.hero_sm = (sans, px(23), "bold")
        self.hero_xs = (sans, px(18), "bold")
        self.unit = (sans, px(12), "normal")
        self.title = (sans, px(11), "bold")
        self.label = (sans, px(10), "normal")
        self.sub = (sans, px(11), "normal")
        self.chip = (sans, px(10), "bold")
        self.tick = (mono, px(9), "normal")
        self.table = (mono, px(11), "normal")
        self.table_head = (mono, px(10), "bold")
        self.clock = (mono, px(14), "bold")
        self.brand = (sans, px(15), "bold")
