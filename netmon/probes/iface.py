"""Throughput and interface error rates.

This is the *throughput* half of the "bandwidth vs throughput" question: how
much traffic is crossing this NIC right now, read from the kernel's byte counters.
It costs nothing to measure and can be sampled at 1 Hz, unlike capacity (see
:mod:`netmon.probes.bandwidth`), which has to saturate the link. These are the
Pi's own counters, so other devices' traffic never appears in them.

Error rate comes from the same counters: ``errin/errout`` (frames the driver
rejected) plus ``dropin/dropout`` (frames discarded for lack of buffer), as a
per-second rate.

The probe also owns the facts about the link itself - address, carrier, speed,
Wi-Fi signal - and, with ``interface: auto``, follows the default route, because
the dashboard can start before the network is up.
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

from ..util import default_route, iface_ipv4, link_is_up, pick_interface, wifi_signal_dbm

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
        self._auto = cfg.general.interface in ("", "auto", None)
        self._prev = None          # (ts, counters)
        self._link_up = None
        self._speed = None
        self._warned = False

        if self.iface is None:
            log.warning("could not determine an interface to measure; "
                        "set general.interface in config.yaml")
        with state.lock:
            state.iface.name = self.iface

    def _follow_route(self) -> None:
        """Keep ``interface: auto`` on the interface the traffic actually uses."""
        if not self._auto:
            return
        route_iface, _ = default_route()
        if route_iface:
            name = route_iface
        elif self.iface and iface_ipv4(self.iface):
            return      # the route is gone but this NIC is still configured: stay
        else:
            name = pick_interface("auto")
        if not name or name == self.iface:
            return
        log.info("measuring on %s (was %s)", name, self.iface)
        self.iface = name
        self._prev = None       # another NIC's counters: a delta would be nonsense
        self._link_up = self._speed = None
        with self.state.lock:
            self.state.iface.name = name

    def _link_speed(self) -> float | None:
        """Link speed in Mbps, as reported by the driver (``ethtool`` style)."""
        if not HAVE_PSUTIL:
            return None
        try:
            st = psutil.net_if_stats().get(self.iface)
        except OSError:
            return None
        return float(st.speed) if st and st.speed else None

    def _read_link(self) -> None:
        # Read every tick, not once: DHCP can hand out a new lease at any time,
        # and the address on screen is what you type into ssh.
        ip = iface_ipv4(self.iface)
        up = link_is_up(self.iface)
        signal = wifi_signal_dbm(self.iface)
        speed = self._link_speed()
        with self.state.lock:
            st = self.state.iface
            st.ip, st.link_up, st.signal_dbm, st.speed_mbps = ip, up, signal, speed

        detail = None
        if self._link_up is not None and up is not None and up != self._link_up:
            detail = f"{self.iface} link {'up' if up else 'down'}"
        elif self._speed and speed and speed != self._speed:
            detail = f"{self.iface} link {self._speed:.0f} -> {speed:.0f} Mbps"
        if detail:
            now = time.time()
            self.state.note_event(now, "link", self.iface, detail)
            self.storage.add_event(now, "link", self.iface, detail)
            log.info("%s", detail)
        if up is not None:
            self._link_up = up
        if speed:
            self._speed = speed

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
        self._follow_route()
        if not self.iface:
            return
        self._read_link()

        now = time.time()
        cur = self._counters()
        if cur is None:
            with self.state.lock:
                self.state.iface.last_error = "counters unavailable"
            return

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
            st.last_error = None

        self.storage.add_iface(now, self.iface, rx_mbps, tx_mbps, err_in, err_out,
                               drop_in, drop_out, pkt_in, pkt_out)

        if err_rate > 0:
            log.debug("%s: %.1f errors+drops/s", self.iface, err_rate)
