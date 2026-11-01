"""The dashboard itself.

Layout, for an 800x480 DSI panel (it scales to any size, but that is the target):

    +----------------------------------------------------------------+
    | NETMON  host/iface   | PRIMARY TARGET + STATE |  window  clock |
    +------------------+------------------+--------------------------+
    | LATENCY          | PACKET LOSS      | BANDWIDTH vs THROUGHPUT  |
    +------------------+------------------+--------------------------+
    | JITTER | TCP | ERRORS | AVAILABILITY | UPTIME                  |
    +----------------------------------------------------------------+
    | per-target table + footer                                      |
    +----------------------------------------------------------------+

The three real-time-sensitive metrics get the top row and the full chart width;
the rest are compact readouts. Every charted value is also in the table, so the
screen is readable when the TFT's viewing angle washes the colours out.

Bandwidth vs throughput sits in one tile on purpose. Both are Mbps, so they
share a single axis - throughput is the line, capacity is the dashed reference,
and the headroom between them is the thing you actually want to see.
"""

from __future__ import annotations

import logging
import time
import tkinter as tk

from ..util import (fmt_avail, fmt_clock, fmt_duration, fmt_mbps, fmt_ms,
                    fmt_pct, fmt_rate)
from . import theme as T
from .theme import Theme
from .widgets import (BarChart, CapacityMeter, LineChart, Panel, StatRow,
                      TargetTable, Tile)

log = logging.getLogger(__name__)


def _fmt_window(seconds: int) -> str:
    if seconds >= 3600:
        return f"{seconds // 3600}h"
    if seconds >= 60:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _human_bytes(n: int) -> str:
    if n <= 0:
        return "0B"
    for unit in ("B", "K", "M", "G"):
        if n < 1024 or unit == "G":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}G"


class Dashboard:
    """Builds the widget tree once, then only pushes values into it."""

    def __init__(self, root: tk.Tk, cfg, state, collector=None):
        self.root = root
        self.cfg = cfg
        self.state = state
        self.collector = collector
        self.th = cfg.thresholds
        self.windows = list(cfg.ui.windows)
        self.window_s = cfg.ui.window_seconds
        self.theme = Theme(root, cfg.ui.height)
        self._after_id = None
        self._closing = False
        # How long a line may bridge missing columns before it breaks. Tied to
        # each probe's cadence, so a real gap in the data still reads as a gap.
        self.gap_ping = cfg.probes.ping.interval * 3.0
        self.gap_iface = cfg.probes.iface.interval * 3.0
        self.gap_tcp = cfg.probes.tcp.interval * 2.5

        root.configure(bg=T.PAGE)
        root.title("netmon")
        self._build()
        self._bind_keys()

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #

    def _build(self) -> None:
        th = self.theme
        outer = tk.Frame(self.root, bg=T.PAGE)
        outer.pack(fill="both", expand=True, padx=4, pady=4)
        outer.grid_rowconfigure(0, minsize=28, weight=0)
        outer.grid_rowconfigure(1, weight=5, minsize=150)
        outer.grid_rowconfigure(2, minsize=84, weight=0)
        outer.grid_rowconfigure(3, weight=4, minsize=126)
        outer.grid_columnconfigure(0, weight=1)

        # -- header ----------------------------------------------------- #
        head = tk.Frame(outer, bg=T.PAGE)
        head.grid(row=0, column=0, sticky="nsew", pady=(0, 4))
        head.grid_columnconfigure(0, weight=1, uniform="h")
        head.grid_columnconfigure(1, weight=2, uniform="h")
        head.grid_columnconfigure(2, weight=1, uniform="h")

        left = tk.Frame(head, bg=T.PAGE)
        left.grid(row=0, column=0, sticky="w")
        tk.Label(left, text="NETMON", bg=T.PAGE, fg=T.INK,
                 font=th.brand).pack(side="left")
        self.hdr_host = tk.Label(left, text="", bg=T.PAGE, fg=T.MUTED, font=th.label)
        self.hdr_host.pack(side="left", padx=(8, 0))

        self.hdr_state = tk.Label(head, text="", bg=T.PAGE, fg=T.INK2, font=th.title)
        self.hdr_state.grid(row=0, column=1, sticky="n")

        right = tk.Frame(head, bg=T.PAGE)
        right.grid(row=0, column=2, sticky="e")
        self.hdr_window = tk.Label(right, text="", bg=T.PAGE, fg=T.MUTED, font=th.label)
        self.hdr_window.pack(side="left", padx=(0, 8))
        self.hdr_clock = tk.Label(right, text="", bg=T.PAGE, fg=T.INK, font=th.clock)
        self.hdr_clock.pack(side="left")

        # -- primary row ------------------------------------------------ #
        prime = tk.Frame(outer, bg=T.PAGE)
        prime.grid(row=1, column=0, sticky="nsew")
        prime.grid_rowconfigure(0, weight=1)
        for col in range(3):
            prime.grid_columnconfigure(col, weight=1, uniform="p")

        self.t_latency = Tile(prime, th, "Latency", unit="ms")
        self.t_latency.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        self.c_latency = LineChart(self.t_latency.body, th,
                                   [("rtt", T.SERIES["blue"])], height=96)
        self.c_latency.pack(fill="both", expand=True)

        self.t_loss = Tile(prime, th, "Packet loss", unit="%")
        self.t_loss.grid(row=0, column=1, sticky="nsew", padx=4)
        self.c_loss = BarChart(self.t_loss.body, th, T.SERIES["blue"], height=96)
        self.c_loss.pack(fill="both", expand=True)

        self.t_bw = Tile(prime, th, "Bandwidth vs throughput", unit="Mbps in")
        self.t_bw.grid(row=0, column=2, sticky="nsew", padx=(4, 0))
        # The meter is packed first so it keeps its height when space is tight;
        # it carries the series identity for the chart above it.
        self.m_bw = CapacityMeter(
            self.t_bw.body, th,
            [("in", T.SERIES["blue"]), ("out", T.SERIES["orange"])],
            warn_pct=self.th.util_warn_pct)
        self.m_bw.pack(fill="x", side="bottom", pady=(2, 0))
        self.c_bw = LineChart(self.t_bw.body, th,
                              [("rx", T.SERIES["blue"]), ("tx", T.SERIES["orange"])],
                              height=80)
        self.c_bw.pack(fill="both", expand=True)

        # -- compact row ------------------------------------------------ #
        row2 = tk.Frame(outer, bg=T.PAGE)
        row2.grid(row=2, column=0, sticky="nsew", pady=4)
        row2.grid_rowconfigure(0, weight=1)
        for col in range(5):
            row2.grid_columnconfigure(col, weight=1, uniform="s")

        def compact(col: int, title: str, unit: str, chart_color: str | None):
            tile = Tile(row2, th, title, unit=unit, compact=True,
                        hero_font=th.hero_xs)
            tile.grid(row=0, column=col, sticky="nsew",
                      padx=(0 if col == 0 else 4, 0 if col == 4 else 0))
            chart = None
            if chart_color:
                chart = LineChart(tile.body, th, [(title, chart_color)],
                                  height=34, show_axis=False)
                chart.pack(fill="both", expand=True)
            return tile, chart

        self.t_jitter, self.c_jitter = compact(0, "Jitter", "ms", T.SERIES["aqua"])
        self.t_tcp, self.c_tcp = compact(1, "TCP connect", "ms", T.SERIES["violet"])
        self.t_err, self.c_err = compact(2, "Errors", "/s", T.SERIES["red"])
        self.t_avail, _ = compact(3, "Availability", "% 24h", None)
        self.t_up, _ = compact(4, "Uptime", "", None)

        self.r_avail_1h = StatRow(self.t_avail.body, th, "1h")
        self.r_avail_1h.pack(fill="x")
        self.r_avail_n = StatRow(self.t_avail.body, th, "echoes")
        self.r_avail_n.pack(fill="x")
        self.r_up_outages = StatRow(self.t_up.body, th, "outages")
        self.r_up_outages.pack(fill="x")
        self.r_up_last = StatRow(self.t_up.body, th, "last")
        self.r_up_last.pack(fill="x")

        # -- table ------------------------------------------------------ #
        bottom = Panel(outer, th, "Targets")
        bottom.grid(row=3, column=0, sticky="nsew")
        # Footer first, so a short panel eats into the table rather than
        # clipping the status line off the bottom of the screen.
        self.footer = tk.Label(bottom.body, text="", bg=T.PANEL, fg=T.MUTED,
                               font=th.label, anchor="w", justify="left")
        self.footer.pack(fill="x", side="bottom", pady=(2, 0))
        self.table = TargetTable(bottom.body, th, rows=len(self.cfg.targets))
        self.table.pack(fill="x", side="top", expand=False)

    # ------------------------------------------------------------------ #
    # Input
    # ------------------------------------------------------------------ #

    def _bind_keys(self) -> None:
        r = self.root
        r.bind("<Escape>", lambda e: self.quit())
        r.bind("<q>", lambda e: self.quit())
        r.bind("<f>", lambda e: self.toggle_fullscreen())
        r.bind("<b>", lambda e: self.run_bandwidth_now())
        r.bind("<Button-1>", lambda e: self.cycle_window())
        r.bind("<Right>", lambda e: self.cycle_window(1))
        r.bind("<Left>", lambda e: self.cycle_window(-1))
        for i, seconds in enumerate(self.windows[:9], start=1):
            r.bind(str(i), lambda e, s=seconds: self.set_window(s))

    def set_window(self, seconds: int) -> None:
        self.window_s = seconds
        self.refresh()

    def cycle_window(self, step: int = 1) -> None:
        try:
            idx = self.windows.index(self.window_s)
        except ValueError:
            idx = 0
        self.set_window(self.windows[(idx + step) % len(self.windows)])

    def toggle_fullscreen(self) -> None:
        current = bool(self.root.attributes("-fullscreen"))
        self.root.attributes("-fullscreen", not current)
        self.root.configure(cursor="none" if (not current and self.cfg.ui.hide_cursor)
                            else "")

    def run_bandwidth_now(self) -> None:
        if self.collector is None:
            return
        if self.collector.request_bandwidth():
            self.footer.configure(text="bandwidth test requested - throughput will "
                                       "spike to capacity while it runs")

    def quit(self) -> None:
        self._closing = True
        if self._after_id is not None:
            try:
                self.root.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None
        self.root.quit()

    # ------------------------------------------------------------------ #
    # The refresh loop
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        self.refresh()

    def refresh(self) -> None:
        if self._closing:
            return
        try:
            snap = self.state.snapshot(self.window_s)
            self._render(snap)
        except tk.TclError:
            return                      # window went away mid-update
        except Exception:               # noqa: BLE001 - a UI bug must not kill the Pi
            log.exception("dashboard refresh failed")
        if not self._closing:
            self._after_id = self.root.after(self.cfg.ui.refresh_ms, self.refresh)

    # -- rendering ------------------------------------------------------ #

    def _render(self, snap: dict) -> None:
        now = snap["now"]
        t1 = now
        t0 = now - self.window_s
        stale = snap["stale"]
        primary = snap["primary"] or {}
        iface = snap["iface"]
        tcp = snap["tcp"]
        bw = snap["bandwidth"]

        self._render_header(snap, primary, stale)
        self._render_latency(primary, t0, t1, stale)
        self._render_loss(primary, snap, t0, t1, stale)
        self._render_bandwidth(iface, bw, t0, t1, stale)
        self._render_compact(primary, iface, tcp, snap, t0, t1, stale)
        self._render_table(snap)
        self._render_footer(snap, iface, tcp, bw)

    def _render_header(self, snap, primary, stale) -> None:
        iface_name = snap["iface"]["name"] or "no iface"
        self.hdr_host.configure(text=f"{snap['hostname']}  ·  {iface_name}")
        self.hdr_clock.configure(text=fmt_clock(snap["now"]))
        self.hdr_window.configure(text=f"window {_fmt_window(self.window_s)}")

        if stale:
            self.hdr_state.configure(text="COLLECTOR NOT RESPONDING",
                                     fg=T.STATUS["crit"])
            return
        name = primary.get("name", "?")
        host = primary.get("host", "?")
        if primary.get("outage_since"):
            down_for = fmt_duration(snap["now"] - primary["outage_since"])
            self.hdr_state.configure(
                text=f"● OUTAGE  {name} ({host}) unreachable for {down_for}",
                fg=T.STATUS["crit"])
        elif primary.get("alive"):
            self.hdr_state.configure(
                text=f"● ONLINE  {name} ({host})", fg=T.STATUS["ok"])
        elif not primary.get("has_data"):
            self.hdr_state.configure(
                text=f"● STARTING  waiting for the first reply from {name}",
                fg=T.MUTED)
        else:
            self.hdr_state.configure(
                text=f"● NO REPLY  {name} ({host})", fg=T.STATUS["warn"])

    def _render_latency(self, p, t0, t1, stale) -> None:
        th = self.th
        tile = self.t_latency
        if stale or not p:
            tile.set_level("none")
            tile.set_value("--", "collector not reporting")
            return

        lv = T.level(p.get("rtt_avg"), th.latency_warn_ms, th.latency_crit_ms)
        lv = T.worst(lv, T.level(p.get("rtt_p95"), th.latency_crit_ms,
                                 th.latency_crit_ms * 2))
        if p.get("has_data") and not p.get("alive"):
            lv = "crit"
        tile.set_level(lv)
        tile.set_value(
            fmt_ms(p.get("rtt")),
            sub=(f"avg {fmt_ms(p.get('rtt_avg'))}   p95 {fmt_ms(p.get('rtt_p95'))}   "
                 f"min {fmt_ms(p.get('rtt_min'))}   max {fmt_ms(p.get('rtt_max'))} ms"),
            aux=f"{p.get('name', '')}",
        )
        lost_ts = [t for t, l in zip(p["ts"], p["lost_series"]) if l > 0]
        self.c_latency.draw(
            [p["ts"]], [p["rtt_series"]], t0, t1,
            y_floor=max(10.0, th.latency_warn_ms / 2),
            fmt=lambda v: f"{v:g}", lost_ts=lost_ts, max_gap_s=self.gap_ping,
        )

    def _render_loss(self, p, snap, t0, t1, stale) -> None:
        th = self.th
        tile = self.t_loss
        if stale or not p:
            tile.set_level("none")
            tile.set_value("--", "collector not reporting")
            return

        loss = p.get("loss_pct")
        lv = T.level(loss, th.loss_warn_pct, th.loss_crit_pct)
        tile.set_level(lv)
        window_pct = p.get("loss_window_pct")
        tile.set_value(
            fmt_pct(loss),
            sub=(f"{p.get('loss_window_lost', 0)} of {p.get('loss_window_sent', 0)} "
                 f"echoes lost in {_fmt_window(self.window_s)} "
                 f"({fmt_pct(window_pct)}%)   outages {p.get('total_outages', 0)}"),
            aux="rolling 60s",
        )
        values = [l * 100.0 for l in p["lost_series"]]
        self.c_loss.draw(p["ts"], values, t0, t1, y_floor=max(1.0, th.loss_warn_pct),
                         fmt=lambda v: f"{v:g}%")

    def _render_bandwidth(self, iface, bw, t0, t1, stale) -> None:
        """Throughput (live, 1 Hz) against measured capacity (hourly)."""
        th = self.th
        tile = self.t_bw
        if stale:
            tile.set_level("none")
            tile.set_value("--", "collector not reporting")
            return

        rx, tx = iface.get("rx"), iface.get("tx")
        cap_down = bw.get("down_mbps")
        cap_up = bw.get("up_mbps")
        fallback = iface.get("speed_mbps") or self.cfg.ui.link_capacity_mbps
        cap_ref = cap_down or fallback

        util = None
        if cap_ref and rx is not None:
            shares = [rx / cap_ref * 100.0]
            if tx is not None and cap_up:
                shares.append(tx / cap_up * 100.0)
            util = max(shares)

        tile.set_level(T.level(util, th.util_warn_pct, th.util_crit_pct))

        if bw.get("running"):
            cap_text = "capacity test running now..."
        elif cap_down:
            age = fmt_duration(bw.get("age_s")) if bw.get("age_s") else "just now"
            cap_text = (f"capacity {fmt_mbps(cap_down)}↓ {fmt_mbps(cap_up)}↑ Mbps"
                        f"   measured {age} ago")
        else:
            cap_text = f"capacity not measured yet - assuming {fmt_mbps(fallback)} Mbps"

        tile.set_value(fmt_mbps(rx), sub=cap_text, aux=f"↑ {fmt_mbps(tx)}")
        self.c_bw.draw(
            [iface["ts"], iface["ts"]],
            [iface["rx_series"], iface["tx_series"]],
            t0, t1,
            y_floor=1.0,
            fmt=fmt_mbps,
            reference=cap_ref,
            reference_label=f"cap {fmt_mbps(cap_ref)}" if cap_ref else "",
            max_gap_s=self.gap_iface,
        )

        # The meter carries the "vs": throughput as a share of capacity, which
        # stays legible even when the chart is scaled to a near-idle link.
        def share(value, capacity):
            if value is None or not capacity:
                return None, "--"
            pct = value / capacity * 100.0
            return pct, f"{fmt_pct(pct, 1)}%"

        in_pct, in_txt = share(rx, cap_ref)
        out_pct, out_txt = share(tx, cap_up or cap_ref)
        self.m_bw.draw([in_pct, out_pct], [in_txt, out_txt])

    def _render_compact(self, p, iface, tcp, snap, t0, t1, stale) -> None:
        th = self.th

        # Jitter
        if stale or not p:
            self.t_jitter.set_level("none")
            self.t_jitter.set_value("--")
        else:
            lv = T.level(p.get("jitter"), th.jitter_warn_ms, th.jitter_crit_ms)
            self.t_jitter.set_level(lv, show_text=False)
            self.t_jitter.set_value(fmt_ms(p.get("jitter")),
                                    sub=f"peak {fmt_ms(p.get('jitter_max'))} ms")
            self.c_jitter.draw([p["ts"]], [p["jitter_series"]], t0, t1,
                               y_floor=max(2.0, th.jitter_warn_ms / 2),
                               max_gap_s=self.gap_ping)

        # TCP handshake
        if stale:
            self.t_tcp.set_level("none")
            self.t_tcp.set_value("--")
        else:
            lv = T.level(tcp.get("connect_ms"), th.tcp_warn_ms, th.tcp_crit_ms)
            if tcp.get("connect_ms") is None and tcp.get("attempts"):
                lv = "crit"
            self.t_tcp.set_level(lv, show_text=False)
            retrans = tcp.get("retrans_pct")
            est = tcp.get("established")
            self.t_tcp.set_value(
                fmt_ms(tcp.get("connect_ms")),
                sub=(f":{tcp.get('port', '')}  retrans {fmt_pct(retrans)}%"
                     f"  est {est if est is not None else '--'}"))
            self.c_tcp.draw([tcp["ts"]], [tcp["connect_series"]], t0, t1,
                            y_floor=50.0, max_gap_s=self.gap_tcp)

        # Interface errors
        if stale:
            self.t_err.set_level("none")
            self.t_err.set_value("--")
        else:
            lv = T.level(iface.get("err_rate"), th.err_warn_rate, th.err_crit_rate)
            self.t_err.set_level(lv, show_text=False)
            total = iface.get("errors_total", 0) + iface.get("drops_total", 0)
            self.t_err.set_value(
                fmt_rate(iface.get("err_rate")),
                sub=f"{total} since start  {fmt_pct(iface.get('err_pct'), 3)}% of pkts")
            self.c_err.draw([iface["err_ts"]], [iface["err_series"]], t0, t1,
                            y_floor=max(1.0, th.err_warn_rate),
                            max_gap_s=self.gap_iface)

        # Availability
        avail = (p or {}).get("avail_24h")
        lv = T.level(avail, th.avail_warn_pct, th.avail_crit_pct, lower_is_better=False)
        self.t_avail.set_level(lv, show_text=False)
        self.t_avail.set_value(fmt_avail(avail))
        self.r_avail_1h.set(f"{fmt_avail((p or {}).get('avail_1h'))}%"
                            if (p or {}).get("avail_1h") is not None else "--")
        n24 = (p or {}).get("avail_24h_n") or 0
        self.r_avail_n.set(f"{n24:,}" if n24 else "collecting")

        # Monitor uptime / outage state
        self.t_up.set_level("ok" if not stale else "none", show_text=False)
        self.t_up.set_value(fmt_duration(snap.get("uptime_s")))
        self.r_up_outages.set(str((p or {}).get("total_outages", 0)))
        last = (p or {}).get("last_outage_duration")
        self.r_up_last.set(fmt_duration(last) if last else "none")

    def _render_table(self, snap) -> None:
        th = self.th
        for i, t in enumerate(snap["targets"]):
            lv = T.worst(
                T.level(t.get("rtt_avg"), th.latency_warn_ms, th.latency_crit_ms),
                T.level(t.get("loss_pct"), th.loss_warn_pct, th.loss_crit_pct),
                T.level(t.get("jitter"), th.jitter_warn_ms, th.jitter_crit_ms),
            )
            if t.get("outage_since"):
                lv = "crit"
                state = "DOWN " + fmt_duration(snap["now"] - t["outage_since"])
            elif snap["stale"]:
                lv = "none"
                state = "--"
            elif not t.get("has_data"):
                lv = "none"
                state = "WAITING"
            elif not t.get("alive"):
                lv = T.worst(lv, "warn")
                state = "NO REPLY"
            else:
                state = T.status_text(lv)
            self.table.set_row(i, [
                t.get("name", "?"),
                t.get("host", "?"),
                fmt_ms(t.get("rtt")),
                fmt_ms(t.get("rtt_avg")),
                fmt_ms(t.get("rtt_p95")),
                fmt_ms(t.get("jitter")),
                fmt_pct(t.get("loss_pct")),
                fmt_avail(t.get("avail_1h")),
                fmt_avail(t.get("avail_24h")),
                state,
            ], lv, primary=bool(t.get("primary")))

    def _render_footer(self, snap, iface, tcp, bw) -> None:
        bits = []
        if snap["stale"]:
            age = snap["now"] - (snap["collector_ts"] or snap["now"])
            bits.append(f"collector silent for {age:.0f}s")
        if snap["db_error"]:
            bits.append(f"db: {snap['db_error'][:48]}")
        if snap["errors"]:
            bits.append(snap["errors"][-1][:64])
        if snap["events"]:
            ev = snap["events"][-1]
            bits.append(f"{time.strftime('%H:%M', time.localtime(ev['ts']))} "
                        f"{ev['detail'][:48]}")
        if not bits:
            bits.append(f"history {_human_bytes(snap['db_bytes'])}"
                        f"   tcp {tcp.get('host')}:{tcp.get('port')}"
                        f"   fails {tcp.get('fails', 0)}/{tcp.get('attempts', 0)}")
        bits.append("[tap] window   [b] bandwidth test   [f] fullscreen   [q] quit")
        self.footer.configure(text="     ".join(bits))


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def run_dashboard(cfg, state, collector=None) -> None:
    """Create the Tk root, run the dashboard, and return when the window closes."""
    root = tk.Tk()
    root.geometry(f"{cfg.ui.width}x{cfg.ui.height}")
    root.minsize(640, 400)
    if cfg.ui.fullscreen:
        try:
            root.attributes("-fullscreen", True)
        except tk.TclError:
            log.warning("fullscreen not supported by this window manager")
    if cfg.ui.hide_cursor:
        root.configure(cursor="none")

    dash = Dashboard(root, cfg, state, collector)
    root.protocol("WM_DELETE_WINDOW", dash.quit)
    dash.start()
    try:
        root.mainloop()
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass
