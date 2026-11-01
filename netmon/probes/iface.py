"""Throughput and interface error rates.

This is the *throughput* half of the "bandwidth vs throughput" question: how
much traffic is actually crossing the NIC right now, read from the kernel's
byte counters. It costs nothing to measure and can be sampled at 1 Hz, unlike
capacity (see :mod:`netmon.probes.bandwidth`), which has to saturate the link.

Error rate comes from the same counters: ``errin/errout`` (frames the driver
rejected) plus ``dropin/dropout`` (frames discarded for lack of buffer), as a
per-second rate and as a share of all packets seen.
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)

try:
    import psutil
    HAVE_PSUTIL = True
except ImportError:  # pragma: no cover
    HAVE_PSUTIL = False

from ..util import pick_interface

BITS_PER_BYTE = 8
MEGA = 1_000_000.0


class IfaceProbe:
    name = "iface"

    def __init__(self, cfg, state, storage):
        self.cfg = cfg
        self.state = state
        self.storage = storage
        self.interval = cfg.probes.iface.interval
        self.iface = pick_interface(cfg.general.interface)
        self._prev = None          # (ts, counters)
        self._base = None          # counters at startup, for the session totals
        self._warned = False

        if self.iface is None:
            log.warning("could not determine an interface to measure; "
                        "set general.interface in config.yaml")
        with state.lock:
            state.iface.name = self.iface
        self._read_link_speed()

    def _read_link_speed(self) -> None:
        """Link speed in Mbps, as reported by the driver (``ethtool`` style)."""
        if not (HAVE_PSUTIL and self.iface):
            return
        try:
            st = psutil.net_if_stats().get(self.iface)
        except OSError:
            return
        if st and st.speed:
            with self.state.lock:
                self.state.iface.speed_mbps = float(st.speed)

    def _counters(self):
        if not HAVE_PSUTIL:
            return None
        try:
            return psutil.net_io_counters(pernic=True).get(self.iface)
        except (OSError, RuntimeError) as exc:
            if not self._warned:
                log.warning("cannot read interface counters: %s", exc)
                self._warned = True
            return None

    async def tick(self) -> None:
        if not self.iface:
            return
        now = time.time()
        cur = self._counters()
        if cur is None:
            with self.state.lock:
                self.state.iface.last_error = "counters unavailable"
            return

        if self._base is None:
            self._base = cur
        if self._prev is None:
            self._prev = (now, cur)
            return

        prev_ts, prev = self._prev
        dt = now - prev_ts
        self._prev = (now, cur)
        if dt <= 0.05:
            return

        # Counters are 64-bit on a 64-bit kernel, but a NIC reset can still
        # make them go backwards; treat any negative delta as a restart.
        def delta(attr: str) -> int:
            d = getattr(cur, attr) - getattr(prev, attr)
            return d if d >= 0 else 0

        rx_mbps = delta("bytes_recv") * BITS_PER_BYTE / MEGA / dt
        tx_mbps = delta("bytes_sent") * BITS_PER_BYTE / MEGA / dt
        err_in, err_out = delta("errin"), delta("errout")
        drop_in, drop_out = delta("dropin"), delta("dropout")
        pkt_in, pkt_out = delta("packets_recv"), delta("packets_sent")
        err_rate = (err_in + err_out + drop_in + drop_out) / dt

        with self.state.lock:
            st = self.state.iface
            st.rx.add(now, rx_mbps)
            st.tx.add(now, tx_mbps)
            st.err_rate.add(now, err_rate)
            st.errors_total += err_in + err_out
            st.drops_total += drop_in + drop_out
            st.packets_total += pkt_in + pkt_out
            st.bytes_rx_total = max(0, cur.bytes_recv - self._base.bytes_recv)
            st.bytes_tx_total = max(0, cur.bytes_sent - self._base.bytes_sent)
            st.last_error = None

        self.storage.add_iface(now, self.iface, rx_mbps, tx_mbps, err_in, err_out,
                               drop_in, drop_out, pkt_in, pkt_out)

        if err_rate > 0:
            log.debug("%s: %.1f errors+drops/s", self.iface, err_rate)
