"""Live, in-RAM measurement state shared between the collector and the UI.

The collector thread writes; the Tk main thread calls :meth:`LiveState.snapshot`
twice a second and gets back plain lists and floats. Everything the UI touches
is copied under one lock, so the UI never holds a reference to a deque that is
being mutated underneath it.
"""

from __future__ import annotations

import socket
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

from .util import mean, percentile

# A sample is (ts, value); value is None when the measurement failed.
# Optional, not "float | None": this line runs at import, and Bullseye is Python 3.9.
Sample = tuple[float, Optional[float]]


class Series:
    """Bounded time series of ``(ts, value)`` with window helpers."""

    __slots__ = ("_q", "maxlen")

    def __init__(self, maxlen: int = 1800):
        self.maxlen = maxlen
        self._q: deque[Sample] = deque(maxlen=maxlen)

    def __len__(self) -> int:
        return len(self._q)

    def add(self, ts: float, value: float | None) -> None:
        self._q.append((ts, value))

    def last(self) -> float | None:
        return self._q[-1][1] if self._q else None

    def last_ts(self) -> float | None:
        return self._q[-1][0] if self._q else None

    def window(self, seconds: float, now: float | None = None) -> list[Sample]:
        now = now if now is not None else time.time()
        cut = now - seconds
        # Samples are appended in time order, so walk back from the newest.
        out: list[Sample] = []
        for item in reversed(self._q):
            if item[0] < cut:
                break
            out.append(item)
        out.reverse()
        return out


def series_stats(samples: list[Sample]) -> dict:
    """min / avg / max / p95 / p99 over the non-missing values of a window."""
    vals = [v for _, v in samples if v is not None]
    if not vals:
        return {"n": len(samples), "ok": 0, "min": None, "avg": None,
                "max": None, "p95": None, "p99": None, "last": None}
    return {
        "n": len(samples),
        "ok": len(vals),
        "min": min(vals),
        "avg": sum(vals) / len(vals),
        "max": max(vals),
        "p95": percentile(vals, 95),
        "p99": percentile(vals, 99),
        "last": samples[-1][1],
    }


# --------------------------------------------------------------------------- #
# Per-target latency / loss / jitter
# --------------------------------------------------------------------------- #


@dataclass
class PingRound:
    ts: float
    rtt_ms: float | None     # mean of the echoes that came back
    sent: int
    lost: int
    jitter_ms: float | None  # |rtt - previous rtt|


class TargetState:
    """Everything measured about one ping target."""

    def __init__(self, name: str, host: str, primary: bool, maxlen: int,
                 loss_window: int = 60, outage_after: int = 3):
        self.name = name
        self.host = host              # may be "auto" until resolved
        self.resolved_host = host
        self.primary = primary
        self.loss_window = max(2, loss_window)
        self.outage_after = max(1, outage_after)

        self.rounds: deque[PingRound] = deque(maxlen=maxlen)
        self.avail_1h: float | None = None
        self.avail_24h: float | None = None
        self.avail_1h_n = 0
        self.avail_24h_n = 0

        self.last_ok_ts: float | None = None
        self.outage_since: float | None = None
        self.last_outage_ended: float | None = None
        self.last_outage_duration: float | None = None
        self.total_outages = 0          # this run only
        self.outages_24h: int | None = None   # from history; None without a database
        self.last_error: str | None = None

        self._prev_rtt: float | None = None
        self._consec_fail = 0
        self._first_fail_ts: float | None = None

    # -- recording ---------------------------------------------------------- #

    def record(self, ts: float, rtts: list[float], sent: int,
               error: str | None = None) -> dict:
        """Record one probe round. Returns ``{"round":..., "event": (kind, detail)|None}``."""
        sent = max(1, int(sent))
        received = len(rtts)
        lost = max(0, sent - received)
        rtt = (sum(rtts) / received) if received else None

        jitter = None
        if rtt is not None:
            if self._prev_rtt is not None:
                jitter = abs(rtt - self._prev_rtt)
            self._prev_rtt = rtt

        rnd = PingRound(ts=ts, rtt_ms=rtt, sent=sent, lost=lost, jitter_ms=jitter)
        self.rounds.append(rnd)
        self.last_error = error

        event = None
        if received:
            self.last_ok_ts = ts
            self._consec_fail = 0
            self._first_fail_ts = None
            if self.outage_since is not None:
                duration = ts - self.outage_since
                self.last_outage_duration = duration
                self.last_outage_ended = ts
                event = ("up", f"{self.name} recovered after {duration:.0f}s")
                self.outage_since = None
        else:
            self._consec_fail += 1
            if self._first_fail_ts is None:
                self._first_fail_ts = ts
            if self.outage_since is None and self._consec_fail >= self.outage_after:
                self.outage_since = self._first_fail_ts
                self.total_outages += 1
                event = ("down", f"{self.name} unreachable ({error or 'timeout'})")

        return {"round": rnd, "event": event}

    # -- derived figures ---------------------------------------------------- #

    def _recent(self, count: int) -> list[PingRound]:
        if count >= len(self.rounds):
            return list(self.rounds)
        return list(self.rounds)[-count:]

    def loss_pct(self, count: int | None = None) -> float | None:
        rounds = self._recent(count or self.loss_window)
        sent = sum(r.sent for r in rounds)
        if sent <= 0:
            return None
        return 100.0 * sum(r.lost for r in rounds) / sent

    def window(self, seconds: float, now: float | None = None) -> list[PingRound]:
        now = now if now is not None else time.time()
        cut = now - seconds
        out: list[PingRound] = []
        for r in reversed(self.rounds):
            if r.ts < cut:
                break
            out.append(r)
        out.reverse()
        return out

    def is_alive(self) -> bool:
        return bool(self.rounds) and self.rounds[-1].lost < self.rounds[-1].sent

    def snapshot(self, window_s: float, now: float) -> dict:
        win = self.window(window_s, now)
        rtts = [r.rtt_ms for r in win if r.rtt_ms is not None]
        jitters = [r.jitter_ms for r in win if r.jitter_ms is not None]
        sent = sum(r.sent for r in win)
        lost = sum(r.lost for r in win)
        last = self.rounds[-1] if self.rounds else None

        return {
            "name": self.name,
            "host": self.resolved_host or "no route",
            "no_route": self.resolved_host is None,
            "is_gateway": self.host == "auto",
            "primary": self.primary,
            "alive": self.is_alive(),
            "has_data": bool(self.rounds),
            "rtt": last.rtt_ms if last else None,
            "rtt_min": min(rtts) if rtts else None,
            "rtt_avg": (sum(rtts) / len(rtts)) if rtts else None,
            "rtt_max": max(rtts) if rtts else None,
            "rtt_p95": percentile(rtts, 95) if rtts else None,
            "jitter": mean(jitters),
            "jitter_max": max(jitters) if jitters else None,
            "loss_pct": self.loss_pct(),
            "loss_window_pct": (100.0 * lost / sent) if sent else None,
            "loss_window_lost": lost,
            "loss_window_sent": sent,
            "avail_1h": self.avail_1h,
            "avail_24h": self.avail_24h,
            "avail_24h_n": self.avail_24h_n,
            "last_ok_ts": self.last_ok_ts,
            "outage_since": self.outage_since,
            "last_outage_duration": self.last_outage_duration,
            "outages": (self.total_outages if self.outages_24h is None
                        else self.outages_24h),
            "last_error": self.last_error,
            # Parallel lists keep the Tk canvas code simple.
            "ts": [r.ts for r in win],
            "rtt_series": [r.rtt_ms for r in win],
            "lost_series": [(r.lost / r.sent if r.sent else 0.0) for r in win],
            "jitter_series": [r.jitter_ms for r in win],
        }


# --------------------------------------------------------------------------- #
# Interface throughput and errors
# --------------------------------------------------------------------------- #


class IfaceState:
    def __init__(self, maxlen: int):
        self.name: str | None = None
        self.ip: str | None = None             # IPv4 address, None until assigned
        self.link_up: bool | None = None       # None = the kernel does not say
        self.signal_dbm: float | None = None   # Wi-Fi only
        self.speed_mbps: float | None = None   # link speed reported by the driver
        self.rx = Series(maxlen)               # Mbps
        self.tx = Series(maxlen)
        self.err_rate = Series(maxlen)         # (errors + drops) per second
        self.errors_total = 0
        self.drops_total = 0
        self.last_error: str | None = None

    def snapshot(self, window_s: float, now: float) -> dict:
        rx_win = self.rx.window(window_s, now)
        tx_win = self.tx.window(window_s, now)
        err_win = self.err_rate.window(window_s, now)
        rx_stats = series_stats(rx_win)
        tx_stats = series_stats(tx_win)
        return {
            "name": self.name,
            "ip": self.ip,
            "link_up": self.link_up,
            "signal_dbm": self.signal_dbm,
            "speed_mbps": self.speed_mbps,
            "rx": self.rx.last(),
            "tx": self.tx.last(),
            "rx_avg": rx_stats["avg"],
            "rx_max": rx_stats["max"],
            "tx_avg": tx_stats["avg"],
            "tx_max": tx_stats["max"],
            "rx_p95": rx_stats["p95"],
            "tx_p95": tx_stats["p95"],
            "err_rate": self.err_rate.last(),
            "err_rate_max": series_stats(err_win)["max"],
            "errors_total": self.errors_total,
            "drops_total": self.drops_total,
            "last_error": self.last_error,
            "ts": [t for t, _ in rx_win],
            "rx_series": [v for _, v in rx_win],
            "tx_series": [v for _, v in tx_win],
            "err_ts": [t for t, _ in err_win],
            "err_series": [v for _, v in err_win],
        }


# --------------------------------------------------------------------------- #
# TCP handshake health
# --------------------------------------------------------------------------- #


class TcpState:
    def __init__(self, maxlen: int):
        self.host: str = ""
        self.port: int = 0
        self.connect = Series(max(60, maxlen // 10))   # ms, sampled slowly
        self.retrans_pct: float | None = None
        self.fails = 0
        self.attempts = 0
        self.last_error: str | None = None

    def snapshot(self, window_s: float, now: float) -> dict:
        win = self.connect.window(max(window_s, 600), now)
        stats = series_stats(win)
        return {
            "host": self.host,
            "port": self.port,
            "connect_ms": self.connect.last(),
            "connect_avg": stats["avg"],
            "connect_max": stats["max"],
            "ok": self.connect.last() is not None,
            "retrans_pct": self.retrans_pct,
            "attempts": self.attempts,
            "fails": self.fails,
            "fail_pct": (100.0 * self.fails / self.attempts) if self.attempts else None,
            "last_error": self.last_error,
            "ts": [t for t, _ in win],
            "connect_series": [v for _, v in win],
        }


# --------------------------------------------------------------------------- #
# DNS resolution
# --------------------------------------------------------------------------- #


class DnsState:
    FAILING_AFTER = 2       # one slow answer is not a DNS fault

    def __init__(self, maxlen: int):
        self.host: str = ""
        self.enabled = False
        self.lookup = Series(max(60, maxlen // 10))   # ms, sampled slowly
        self.attempts = 0
        self.fails = 0
        self.consec_fail = 0
        self.last_error: str | None = None

    def snapshot(self, window_s: float, now: float) -> dict:
        win = self.lookup.window(max(window_s, 600), now)
        return {
            "host": self.host,
            "enabled": self.enabled,
            "ms": self.lookup.last(),
            "attempts": self.attempts,
            "fails": self.fails,
            "failing": self.consec_fail >= self.FAILING_AFTER,
            "last_error": self.last_error,
            "ts": [t for t, _ in win],
            "series": [v for _, v in win],
        }


# --------------------------------------------------------------------------- #
# The Pi itself
# --------------------------------------------------------------------------- #


class SystemState:
    def __init__(self):
        self.temp_c: float | None = None
        self.cpu_pct: float | None = None
        self.ram_pct: float | None = None
        self.sd_used_pct: float | None = None
        self.sd_free_bytes: int | None = None
        self.sd_written_bytes: int | None = None    # lifetime, from ext4
        self.sd_written_per_day: float | None = None    # bytes, averaged since boot
        self.sd_wear_pct: float | None = None       # an estimate: see probes/system.py
        self.sd_errors: int | None = None
        self.sd_readonly: bool = False
        self.uptime_s: float | None = None
        self.power_level: str | None = None     # None off a Pi / without vcgencmd
        self.power_text: str = ""

    def snapshot(self) -> dict:
        return {
            "temp_c": self.temp_c,
            "cpu_pct": self.cpu_pct,
            "ram_pct": self.ram_pct,
            "sd_used_pct": self.sd_used_pct,
            "sd_free_bytes": self.sd_free_bytes,
            "sd_written_bytes": self.sd_written_bytes,
            "sd_written_per_day": self.sd_written_per_day,
            "sd_wear_pct": self.sd_wear_pct,
            "sd_errors": self.sd_errors,
            "sd_readonly": self.sd_readonly,
            "uptime_s": self.uptime_s,
            "power_level": self.power_level,
            "power_text": self.power_text,
        }


# --------------------------------------------------------------------------- #
# Capacity ("bandwidth")
# --------------------------------------------------------------------------- #


class BandwidthState:
    def __init__(self):
        self.down_mbps: float | None = None
        self.up_mbps: float | None = None
        self.latency_ms: float | None = None
        self.ts: float | None = None
        self.method: str = "none"
        self.server: str = ""
        self.ok: bool = False
        self.note: str = ""
        self.running: bool = False
        self.started_ts: float | None = None    # while running
        self.spans: deque[tuple[float, float]] = deque(maxlen=8)  # finished tests
        self.history: list[dict] = []   # last few results, newest last

    def record(self, result: dict) -> None:
        self.ts = result.get("ts", time.time())
        self.method = result.get("method", self.method)
        self.ok = bool(result.get("ok"))
        self.note = result.get("note") or ""
        self.server = result.get("server") or ""
        if self.ok:
            self.down_mbps = result.get("down_mbps")
            self.up_mbps = result.get("up_mbps")
            self.latency_ms = result.get("latency_ms")
            self.history.append(dict(result))
            del self.history[:-12]

    def snapshot(self, now: float) -> dict:
        return {
            "down_mbps": self.down_mbps,
            "up_mbps": self.up_mbps,
            "latency_ms": self.latency_ms,
            "ts": self.ts,
            "age_s": (now - self.ts) if self.ts else None,
            "method": self.method,
            "server": self.server,
            "ok": self.ok,
            "note": self.note,
            "running": self.running,
            # When the link was deliberately saturated, so a latency spike during
            # a test is not read as a network problem.
            "spans": list(self.spans) + ([(self.started_ts, now)]
                                         if self.running and self.started_ts else []),
            "history": list(self.history),
        }


# --------------------------------------------------------------------------- #
# The shared root object
# --------------------------------------------------------------------------- #


class LiveState:
    """Thread-safe container for everything the dashboard reads."""

    def __init__(self, cfg):
        maxlen = cfg.general.history_points
        self._lock = threading.RLock()
        self.cfg = cfg
        self.hostname = socket.gethostname()
        self.started_at = time.time()
        self.targets: dict[str, TargetState] = {}
        for t in cfg.targets:
            self.targets[t.name] = TargetState(
                name=t.name, host=t.host, primary=t.primary, maxlen=maxlen,
                loss_window=cfg.probes.ping.loss_window,
                outage_after=cfg.probes.ping.outage_after,
            )
        self.iface = IfaceState(maxlen)
        self.tcp = TcpState(maxlen)
        self.dns = DnsState(maxlen)
        self.system = SystemState()
        self.bandwidth = BandwidthState()
        self.collector_ts: float = 0.0     # heartbeat: last completed probe round
        self.probe_errors: deque[str] = deque(maxlen=12)
        self.events: list[dict] = []
        self.db_bytes: int = 0
        self.db_error: str | None = None

    # -- access ------------------------------------------------------------- #

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def primary(self) -> TargetState:
        for t in self.targets.values():
            if t.primary:
                return t
        return next(iter(self.targets.values()))

    def note_error(self, text: str) -> None:
        with self._lock:
            stamped = f"{time.strftime('%H:%M:%S')} {text}"
            if not self.probe_errors or self.probe_errors[-1][9:] != text:
                self.probe_errors.append(stamped)

    def note_event(self, ts: float, kind: str, target: str, detail: str) -> None:
        with self._lock:
            self.events.append({"ts": ts, "kind": kind, "target": target,
                                "detail": detail})
            del self.events[:-30]

    def beat(self, ts: float | None = None) -> None:
        with self._lock:
            self.collector_ts = ts if ts is not None else time.time()

    def snapshot(self, window_s: float, stale_after: float = 5.0) -> dict:
        """A consistent, copied view of the whole state."""
        now = time.time()
        with self._lock:
            targets = [t.snapshot(window_s, now) for t in self.targets.values()]
            primary_name = self.primary().name
            snap = {
                "now": now,
                "window_s": window_s,
                "hostname": self.hostname,
                "uptime_s": now - self.started_at,
                "collector_ts": self.collector_ts,
                "stale": (now - self.collector_ts) > stale_after if self.collector_ts else True,
                "targets": targets,
                "primary": next((t for t in targets if t["name"] == primary_name), None),
                "iface": self.iface.snapshot(window_s, now),
                "tcp": self.tcp.snapshot(window_s, now),
                "dns": self.dns.snapshot(window_s, now),
                "system": self.system.snapshot(),
                "bandwidth": self.bandwidth.snapshot(now),
                "errors": list(self.probe_errors),
                "events": list(self.events),
                "db_bytes": self.db_bytes,
                "db_error": self.db_error,
            }
        return snap
