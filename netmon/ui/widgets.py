"""Hand-drawn Tk canvas charts and tiles.

No matplotlib: on a Pi 3B a matplotlib redraw costs tenths of a second and
tens of megabytes, and all that is needed here is a line, a band and some bars.
Canvas items are pooled and updated with ``coords()`` rather than deleted and
recreated, so refreshing does not churn the heap. Text only reconfigures when it
changes, because any reconfigure repaints, and that costs CPU on a Pi 3B.

Charts bucket their samples into one column per ~2 px and draw a min/max range
band behind a mean line, so a 30-minute window stays honest instead of
aliasing spikes away. Every y axis starts at zero.
"""

from __future__ import annotations

import math
import tkinter as tk

from . import theme as T

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def set_label(label: tk.Label, **options) -> None:
    """Configure only what differs, so an unchanged label is not repainted."""
    changed = {k: v for k, v in options.items() if str(label.cget(k)) != str(v)}
    if changed:
        label.configure(**changed)


def nice_ceil(value: float) -> float:
    """Round up to a readable axis maximum (1, 1.5, 2, 2.5, 3, 4, 5, 7.5, 10 x 10^n)."""
    if value is None or value <= 0:
        return 1.0
    exp = math.floor(math.log10(value))
    base = 10.0 ** exp
    for mult in (1, 1.5, 2, 2.5, 3, 4, 5, 7.5, 10):
        if value <= mult * base * 1.0000001:
            return mult * base
    return 10 * base


def bucket(ts: list[float], values: list[float | None], t0: float, t1: float,
           ncols: int) -> list[tuple[float, float, float, int] | None]:
    """Fold samples into ``ncols`` time columns of ``(min, mean, max, n)``."""
    cols: list[list[float]] = [[] for _ in range(ncols)]
    span = t1 - t0
    if span <= 0 or ncols <= 0:
        return [None] * max(0, ncols)
    for t, v in zip(ts, values):
        if v is None:
            continue
        idx = int((t - t0) / span * ncols)
        if idx == ncols:
            idx = ncols - 1
        if 0 <= idx < ncols:
            cols[idx].append(v)
    out: list[tuple[float, float, float, int] | None] = []
    for acc in cols:
        if acc:
            out.append((min(acc), sum(acc) / len(acc), max(acc), len(acc)))
        else:
            out.append(None)
    return out


def _runs(cols: list, max_gap_cols: int = 0) -> list[list[int]]:
    """Indices of non-empty columns, split where the gap is too long to bridge.

    ``max_gap_cols`` is how many empty columns may be spanned before the line
    breaks. A probe sampled every 10 s leaves most 2 px columns empty, and
    without this it would be drawn as a row of dots rather than a line; a real
    outage still has to show as a break, which is what the limit is for.
    """
    runs: list[list[int]] = []
    current: list[int] = []
    last: int | None = None
    for i, c in enumerate(cols):
        if c is None:
            continue
        if last is not None and (i - last - 1) > max_gap_cols:
            runs.append(current)
            current = []
        current.append(i)
        last = i
    if current:
        runs.append(current)
    return runs


class _Pool:
    """Reusable canvas items of one kind."""

    def __init__(self, canvas: tk.Canvas, factory):
        self.canvas = canvas
        self.factory = factory
        self.items: list[int] = []
        self._used = 0

    def begin(self) -> None:
        self._used = 0

    def take(self) -> int:
        if self._used >= len(self.items):
            self.items.append(self.factory())
        item = self.items[self._used]
        self._used += 1
        self.canvas.itemconfigure(item, state="normal")
        return item

    def end(self) -> None:
        for item in self.items[self._used:]:
            self.canvas.itemconfigure(item, state="hidden")


def _draw_spans(canvas: tk.Canvas, pool: _Pool, spans, t0: float, t1: float,
                x0: float, x1: float, y_top: float, y_bot: float) -> None:
    """Shade the capacity tests: the link was saturated on purpose just then."""
    pool.begin()
    if t1 > t0:
        scale = (x1 - x0) / (t1 - t0)
        for start, end in spans or ():
            if end < t0 or start > t1:
                continue
            canvas.coords(pool.take(), x0 + (max(start, t0) - t0) * scale, y_top,
                          x0 + (min(end, t1) - t0) * scale, y_bot)
    pool.end()


def _span_factory(canvas: tk.Canvas):
    """A rectangle behind everything else on the canvas."""
    def make() -> int:
        item = canvas.create_rectangle(0, 0, 1, 1, fill=T.GRID, outline="")
        canvas.tag_lower(item)
        return item
    return make


# --------------------------------------------------------------------------- #
# Charts
# --------------------------------------------------------------------------- #

PAD_L, PAD_R, PAD_T, PAD_B = 2, 34, 8, 10


class LineChart(tk.Canvas):
    """One or more series as a mean line over a min/max band, zero-based."""

    def __init__(self, master, theme: T.Theme, series: list[tuple[str, str]],
                 height: int = 86, show_axis: bool = True, **kw):
        super().__init__(master, bg=T.PANEL, highlightthickness=0, bd=0,
                         height=height, **kw)
        self.theme = theme
        self.series = series            # [(label, hex colour), ...] in slot order
        self.show_axis = show_axis
        self._cw = 0
        self._ch = height

        self._spans = _Pool(self, _span_factory(self))
        self._grid = _Pool(self, lambda: self.create_line(
            0, 0, 1, 0, fill=T.GRID, width=1))
        self._bands = [
            _Pool(self, lambda c=color: self.create_polygon(
                0, 0, 1, 0, 1, 1, fill=T.band_color(c), outline=""))
            for _, color in series
        ]
        self._lines = [
            _Pool(self, lambda c=color: self.create_line(
                0, 0, 1, 1, fill=c, width=2, capstyle="round", joinstyle="round"))
            for _, color in series
        ]
        self._marks = _Pool(self, lambda: self.create_rectangle(
            0, 0, 1, 1, fill=T.STATUS["crit"], outline=""))
        self._refline = self.create_line(0, 0, 1, 0, fill=T.MUTED, width=1,
                                         dash=(4, 3), state="hidden")
        self._reflabel = self.create_text(0, 0, text="", fill=T.MUTED,
                                          font=theme.tick, anchor="e", state="hidden")
        self._ymax_txt = self.create_text(0, 0, text="", fill=T.MUTED,
                                          font=theme.tick, anchor="e")
        self._ymid_txt = self.create_text(0, 0, text="", fill=T.MUTED,
                                          font=theme.tick, anchor="e")
        self._zero = self.create_line(0, 0, 1, 0, fill=T.AXIS, width=1)
        self._empty = self.create_text(0, 0, text="", fill=T.MUTED,
                                       font=theme.label, anchor="center", state="hidden")
        self.bind("<Configure>", self._on_configure)

    def _on_configure(self, event) -> None:
        self._cw, self._ch = event.width, event.height

    # -- drawing ------------------------------------------------------------ #

    def draw(self, ts_lists: list[list[float]], value_lists: list[list[float | None]],
             t0: float, t1: float, ymax: float | None = None, y_floor: float = 1.0,
             fmt=lambda v: f"{v:g}", reference: float | None = None,
             reference_label: str = "", lost_ts: list[float] | None = None,
             max_gap_s: float = 0.0, spans=None) -> None:
        w = self._cw or self.winfo_width()
        h = self._ch or self.winfo_height()
        if w < 24 or h < 24:
            return

        pad_r = PAD_R if self.show_axis else 2
        x0, x1 = PAD_L, max(PAD_L + 10, w - pad_r)
        y_top, y_bot = PAD_T, max(PAD_T + 10, h - PAD_B)
        plot_w = x1 - x0
        plot_h = y_bot - y_top
        ncols = max(4, int(plot_w // 2))

        all_cols = [bucket(ts, vals, t0, t1, ncols)
                    for ts, vals in zip(ts_lists, value_lists)]

        data_max = 0.0
        has_data = False
        for cols in all_cols:
            for c in cols:
                if c:
                    has_data = True
                    data_max = max(data_max, c[2])
        top = ymax if ymax is not None else nice_ceil(max(data_max, y_floor))
        if reference is not None and reference <= top * 1.05 and reference > 0:
            top = max(top, nice_ceil(reference))
        if top <= 0:
            top = 1.0

        def ypos(value: float) -> float:
            return y_bot - (min(value, top) / top) * plot_h

        def xpos(col: int) -> float:
            return x0 + (col + 0.5) / ncols * plot_w

        _draw_spans(self, self._spans, spans, t0, t1, x0, x1, y_top, y_bot)

        # Recessive solid hairlines at 1/3 and 2/3 of the scale.
        self._grid.begin()
        for frac in (1 / 3, 2 / 3):
            y = y_bot - frac * plot_h
            self.coords(self._grid.take(), x0, y, x1, y)
        self._grid.end()

        self.coords(self._zero, x0, y_bot, x1, y_bot)

        col_seconds = (t1 - t0) / ncols if t1 > t0 else 1.0
        max_gap_cols = int(max_gap_s / col_seconds) if col_seconds > 0 else 0

        for idx, cols in enumerate(all_cols):
            band, line = self._bands[idx], self._lines[idx]
            band.begin()
            line.begin()
            for run in _runs(cols, max_gap_cols):
                if len(run) < 2:
                    i = run[0]
                    c = cols[i]
                    x = xpos(i)
                    self.coords(line.take(), x - 1, ypos(c[1]), x + 1, ypos(c[1]))
                    continue
                # Band: forward along the maxima, back along the minima.
                upper = []
                lower = []
                spread = False
                for i in run:
                    c = cols[i]
                    upper += [xpos(i), ypos(c[2])]
                    lower = [xpos(i), ypos(c[0])] + lower
                    if c[2] - c[0] > 1e-9:
                        spread = True
                if spread:
                    self.coords(band.take(), *(upper + lower))
                pts = []
                for i in run:
                    pts += [xpos(i), ypos(cols[i][1])]
                self.coords(line.take(), *pts)
            band.end()
            line.end()

        # Lost samples: short bars on the baseline, in the reserved critical red.
        self._marks.begin()
        if lost_ts and t1 > t0:
            for t in lost_ts:
                frac = (t - t0) / (t1 - t0)
                if 0.0 <= frac <= 1.0:
                    x = x0 + frac * plot_w
                    self.coords(self._marks.take(), x - 1, y_bot - 5, x + 1, y_bot)
        self._marks.end()

        if reference is not None and 0 < reference <= top * 1.001:
            y = ypos(reference)
            self.coords(self._refline, x0, y, x1, y)
            self.itemconfigure(self._refline, state="normal")
            if reference_label:
                self.coords(self._reflabel, x1 - 2, max(y_top + 5, y - 7))
                self.itemconfigure(self._reflabel, text=reference_label, state="normal")
            else:
                self.itemconfigure(self._reflabel, state="hidden")
        else:
            self.itemconfigure(self._refline, state="hidden")
            self.itemconfigure(self._reflabel, state="hidden")

        if self.show_axis:
            self.coords(self._ymax_txt, w - 3, y_top + 4)
            self.itemconfigure(self._ymax_txt, text=fmt(top))
            # The two labels need room not to touch; on a short plot show only
            # the top one, since the baseline is always zero anyway.
            self.coords(self._ymid_txt, w - 3, y_bot - plot_h / 2)
            self.itemconfigure(self._ymid_txt,
                               text=fmt(top / 2) if plot_h >= 54 else "")
        else:
            self.itemconfigure(self._ymax_txt, text="")
            self.itemconfigure(self._ymid_txt, text="")

        if has_data:
            self.itemconfigure(self._empty, state="hidden")
        else:
            self.coords(self._empty, (x0 + x1) / 2, (y_top + y_bot) / 2)
            self.itemconfigure(self._empty, text="waiting for samples", state="normal")


class BarChart(tk.Canvas):
    """Magnitude over time as zero-anchored bars, with a 2 px gap between them."""

    def __init__(self, master, theme: T.Theme, color: str, height: int = 86,
                 show_axis: bool = True, **kw):
        super().__init__(master, bg=T.PANEL, highlightthickness=0, bd=0,
                         height=height, **kw)
        self.theme = theme
        self.color = color
        self.show_axis = show_axis
        self._cw = 0
        self._ch = height
        self._spans = _Pool(self, _span_factory(self))
        self._grid = _Pool(self, lambda: self.create_line(
            0, 0, 1, 0, fill=T.GRID, width=1))
        self._bars = _Pool(self, lambda: self.create_rectangle(
            0, 0, 1, 1, fill=color, outline=""))
        self._ymax_txt = self.create_text(0, 0, text="", fill=T.MUTED,
                                          font=theme.tick, anchor="e")
        self._zero = self.create_line(0, 0, 1, 0, fill=T.AXIS, width=1)
        self._empty = self.create_text(0, 0, text="", fill=T.MUTED,
                                       font=theme.label, anchor="center", state="hidden")
        self.bind("<Configure>", self._on_configure)

    def _on_configure(self, event) -> None:
        self._cw, self._ch = event.width, event.height

    def draw(self, ts: list[float], values: list[float | None], t0: float, t1: float,
             ymax: float | None = None, y_floor: float = 1.0,
             fmt=lambda v: f"{v:g}", bar_px: int = 6, spans=None) -> None:
        w = self._cw or self.winfo_width()
        h = self._ch or self.winfo_height()
        if w < 24 or h < 24:
            return

        pad_r = PAD_R if self.show_axis else 2
        x0, x1 = PAD_L, max(PAD_L + 10, w - pad_r)
        y_top, y_bot = PAD_T, max(PAD_T + 10, h - PAD_B)
        plot_w, plot_h = x1 - x0, y_bot - y_top
        ncols = max(4, int(plot_w // max(3, bar_px)))
        cols = bucket(ts, values, t0, t1, ncols)

        data_max = max((c[2] for c in cols if c), default=0.0)
        top = ymax if ymax is not None else nice_ceil(max(data_max, y_floor))
        if top <= 0:
            top = 1.0
        col_w = plot_w / ncols
        bar_w = max(1.0, col_w - 2.0)        # the 2 px gap

        _draw_spans(self, self._spans, spans, t0, t1, x0, x1, y_top, y_bot)

        self._grid.begin()
        for frac in (1 / 3, 2 / 3):
            y = y_bot - frac * plot_h
            self.coords(self._grid.take(), x0, y, x1, y)
        self._grid.end()
        self.coords(self._zero, x0, y_bot, x1, y_bot)

        any_bar = False
        self._bars.begin()
        for i, c in enumerate(cols):
            if not c:
                continue
            value = c[1]
            if value <= 0:
                continue
            any_bar = True
            left = x0 + i * col_w + (col_w - bar_w) / 2
            y = y_bot - (min(value, top) / top) * plot_h
            # Always leave a visible sliver, so a non-zero value never reads as zero.
            self.coords(self._bars.take(), left, min(y, y_bot - 1.5),
                        left + bar_w, y_bot)
        self._bars.end()

        if self.show_axis:
            self.coords(self._ymax_txt, w - 3, y_top + 4)
            self.itemconfigure(self._ymax_txt, text=fmt(top))

        if any_bar or any(cols):
            self.itemconfigure(self._empty, state="hidden")
        else:
            self.coords(self._empty, (x0 + x1) / 2, (y_top + y_bot) / 2)
            text = "no loss in window" if cols and any(c for c in cols) else "waiting"
            self.itemconfigure(self._empty, text=text, state="normal")


# --------------------------------------------------------------------------- #
# Tiles
# --------------------------------------------------------------------------- #


class Tile(tk.Frame):
    """A panel with a status border, a title row, a hero value and a body area."""

    def __init__(self, master, theme: T.Theme, title: str, unit: str = "",
                 hero_font=None, compact: bool = False):
        super().__init__(master, bg=T.STATUS["none"], bd=0, highlightthickness=0)
        self.theme = theme
        self._level = "none"

        self.inner = tk.Frame(self, bg=T.PANEL, bd=0, highlightthickness=0)
        self.inner.pack(fill="both", expand=True, padx=2, pady=2)

        head = tk.Frame(self.inner, bg=T.PANEL)
        head.pack(fill="x", padx=6, pady=(4, 0))
        self.head = head        # callers add a legend here
        self.title_lbl = tk.Label(head, text=title.upper(), bg=T.PANEL, fg=T.MUTED,
                                  font=theme.title, anchor="w")
        self.title_lbl.pack(side="left")
        self.chip = tk.Label(head, text="", bg=T.PANEL, fg=T.STATUS["none"],
                             font=theme.chip, anchor="e")
        self.chip.pack(side="right")

        value_row = tk.Frame(self.inner, bg=T.PANEL)
        value_row.pack(fill="x", padx=6, pady=(0, 0))
        self.value_lbl = tk.Label(value_row, text="--", bg=T.PANEL, fg=T.INK,
                                  font=hero_font or (theme.hero_sm if compact else theme.hero),
                                  anchor="w")
        self.value_lbl.pack(side="left")
        self.unit_lbl = tk.Label(value_row, text=unit, bg=T.PANEL, fg=T.MUTED,
                                 font=theme.unit, anchor="sw")
        self.unit_lbl.pack(side="left", padx=(3, 0), pady=(0, 4))
        self.aux_lbl = tk.Label(value_row, text="", bg=T.PANEL, fg=T.INK2,
                                font=theme.sub, anchor="se")
        self.aux_lbl.pack(side="right", pady=(0, 4))

        self.sub_lbl = tk.Label(self.inner, text="", bg=T.PANEL, fg=T.MUTED,
                                font=theme.label, anchor="w", justify="left")
        self.sub_lbl.pack(fill="x", padx=6, pady=(0, 2))

        self.body = tk.Frame(self.inner, bg=T.PANEL)
        self.body.pack(fill="both", expand=True, padx=4, pady=(0, 4))

    # -- updates ------------------------------------------------------------ #

    def set_level(self, lv: str, show_text: bool = True) -> None:
        if lv != self._level:
            self._level = lv
            self.configure(bg=T.status_color(lv))
            self.chip.configure(fg=T.status_color(lv))
        # Colour is never the only channel: the chip always spells it out.
        set_label(self.chip, text=f"● {T.status_text(lv)}" if show_text else "")

    def set_value(self, text: str, sub: str = "", aux: str = "") -> None:
        if self.value_lbl.cget("text") != text:
            self.value_lbl.configure(text=text)
        if self.sub_lbl.cget("text") != sub:
            self.sub_lbl.configure(text=sub)
        if self.aux_lbl.cget("text") != aux:
            self.aux_lbl.configure(text=aux)

    def set_unit(self, text: str) -> None:
        if self.unit_lbl.cget("text") != text:
            self.unit_lbl.configure(text=text)


class CapacityMeter(tk.Canvas):
    """Throughput as a share of measured capacity, one labelled bar per direction.

    The line chart above it is scaled to live throughput, so on an idle link a
    capacity reference line would sit far off the top of the plot and the "vs"
    in bandwidth vs throughput would be invisible. This says it on a 0-100 %
    scale instead - same axis for both bars, since both are a share of their
    own capacity - and doubles as the chart's legend: the colours here are the
    colours of the lines above.
    """

    ROW_H = 14
    LABEL_W = 24

    def __init__(self, master, theme: T.Theme, rows: list[tuple[str, str]],
                 warn_pct: float = 70.0, **kw):
        super().__init__(master, bg=T.PANEL, highlightthickness=0, bd=0,
                         height=self.ROW_H * len(rows), **kw)
        self.theme = theme
        self.rows = rows
        self.warn_pct = warn_pct
        self._cw = 0
        self._items = []
        for label, color in rows:
            self._items.append({
                "label": self.create_text(0, 0, text=label, fill=T.MUTED,
                                          font=theme.tick, anchor="w"),
                "track": self.create_rectangle(0, 0, 1, 1, fill=T.GRID, outline=""),
                "fill": self.create_rectangle(0, 0, 1, 1, fill=color, outline=""),
                "tick": self.create_line(0, 0, 1, 1, fill=T.AXIS, width=1),
                "value": self.create_text(0, 0, text="--", fill=T.INK2,
                                          font=theme.tick, anchor="e"),
            })
        self.bind("<Configure>", lambda e: setattr(self, "_cw", e.width))

    def draw(self, percents: list[float | None], texts: list[str]) -> None:
        w = self._cw or self.winfo_width()
        if w < 60:
            return
        bar_x0 = self.LABEL_W
        bar_x1 = max(bar_x0 + 20, w - 54)
        bar_w = bar_x1 - bar_x0

        for i, item in enumerate(self._items):
            y = i * self.ROW_H + self.ROW_H / 2
            top, bot = y - 3, y + 3
            self.coords(item["label"], 0, y)
            self.coords(item["track"], bar_x0, top, bar_x1, bot)
            pct = percents[i] if i < len(percents) else None
            frac = 0.0 if pct is None else max(0.0, min(1.0, pct / 100.0))
            # Keep a 1 px sliver so a live-but-tiny flow never reads as zero.
            end = bar_x0 + max(1.0 if pct else 0.0, frac * bar_w)
            self.coords(item["fill"], bar_x0, top, end, bot)
            tick_x = bar_x0 + bar_w * max(0.0, min(1.0, self.warn_pct / 100.0))
            self.coords(item["tick"], tick_x, top - 2, tick_x, bot + 2)
            self.coords(item["value"], w - 2, y)
            text = texts[i] if i < len(texts) else "--"
            if self.itemcget(item["value"], "text") != text:
                self.itemconfigure(item["value"], text=text)


class Panel(tk.Frame):
    """A titled panel with no hero value - used for the table."""

    def __init__(self, master, theme: T.Theme, title: str):
        super().__init__(master, bg=T.AXIS, bd=0, highlightthickness=0)
        self.inner = tk.Frame(self, bg=T.PANEL, bd=0, highlightthickness=0)
        self.inner.pack(fill="both", expand=True, padx=2, pady=2)
        self.head = tk.Frame(self.inner, bg=T.PANEL)
        self.head.pack(fill="x", padx=6, pady=(3, 1))
        self.title_lbl = tk.Label(self.head, text=title.upper(), bg=T.PANEL,
                                  fg=T.MUTED, font=theme.title, anchor="w",
                                  bd=0, pady=0)
        self.title_lbl.pack(side="left")
        self.body = tk.Frame(self.inner, bg=T.PANEL)
        self.body.pack(fill="both", expand=True, padx=6, pady=(0, 4))


class StatRow(tk.Frame):
    """A label/value pair for the compact bottom row of a tile."""

    def __init__(self, master, theme: T.Theme, label: str):
        super().__init__(master, bg=T.PANEL)
        tk.Label(self, text=label, bg=T.PANEL, fg=T.MUTED, font=theme.label,
                 anchor="w", bd=0, pady=0).pack(side="left")
        self.value = tk.Label(self, text="--", bg=T.PANEL, fg=T.INK2,
                              font=theme.label, anchor="e", bd=0, pady=0)
        self.value.pack(side="right")

    def set(self, text: str) -> None:
        if self.value.cget("text") != text:
            self.value.configure(text=text)


class TargetTable(tk.Frame):
    """The table view: every charted number also readable as text.

    This is what makes the dashboard usable when the panel's colours are washed
    out by viewing angle, and it is the only place per-target detail lives.
    """

    COLUMNS = (
        ("TARGET", 11, "w"),
        ("HOST", 15, "w"),
        ("NOW", 7, "e"),
        ("AVG", 7, "e"),
        ("P95", 7, "e"),
        ("JIT", 6, "e"),
        ("LOSS%", 7, "e"),
        ("1H", 7, "e"),
        ("24H", 7, "e"),
        ("STATE", 10, "w"),
    )

    def __init__(self, master, theme: T.Theme, rows: int):
        super().__init__(master, bg=T.PANEL)
        self.theme = theme
        for col, (name, width, anchor) in enumerate(self.COLUMNS):
            self.grid_columnconfigure(col, weight=1 if col < 2 else 0)
            tk.Label(self, text=name, bg=T.PANEL, fg=T.MUTED, font=theme.table_head,
                     anchor=anchor, width=width, bd=0, pady=0).grid(
                row=0, column=col, sticky="ew", padx=1, pady=(0, 1))
        self.cells: list[list[tk.Label]] = []
        for r in range(rows):
            row_cells = []
            bg = T.PANEL if r % 2 == 0 else T.PANEL_ALT
            for col, (_, width, anchor) in enumerate(self.COLUMNS):
                # No label padding: on this panel it costs the charts 16 px.
                lbl = tk.Label(self, text="", bg=bg, fg=T.INK2, font=theme.table,
                               anchor=anchor, width=width, bd=0, pady=0)
                lbl.grid(row=r + 1, column=col, sticky="ew", padx=1)
                row_cells.append(lbl)
            self.cells.append(row_cells)

    def set_row(self, index: int, values: list[str], level: str,
                primary: bool = False) -> None:
        if index >= len(self.cells):
            return
        row = self.cells[index]
        for cell, text in zip(row, values):
            if cell.cget("text") != text:
                cell.configure(text=text)
        # Identity in the first column, state in the last; the rest stays ink.
        set_label(row[0], fg=T.INK if primary else T.INK2)
        set_label(row[-1], fg=T.status_color(level))
