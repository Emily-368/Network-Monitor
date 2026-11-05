"""Stdlib-only helpers: default-route discovery, formatting, light statistics."""

from __future__ import annotations

import getpass
import glob
import os
import socket
import struct
import time

# --------------------------------------------------------------------------- #
# Network facts straight from the kernel
# --------------------------------------------------------------------------- #

_ROUTE_FILE = "/proc/net/route"
_MOUNTS_FILE = "/proc/mounts"
_EXT4_DIR = "/sys/fs/ext4"


def default_route() -> tuple[str | None, str | None]:
    """Return ``(interface, gateway_ip)`` for the lowest-metric default route.

    Returns ``(None, None)`` off Linux or when there is no default route.
    """
    try:
        with open(_ROUTE_FILE, "r", encoding="ascii") as fh:
            lines = fh.read().splitlines()[1:]
    except OSError:
        return (None, None)

    best: tuple[int, str, str] | None = None
    for line in lines:
        f = line.split()
        # iface dest gateway flags refcnt use metric mask mtu window irtt
        if len(f) < 8 or f[1] != "00000000":
            continue
        try:
            metric = int(f[6])
            gw = socket.inet_ntoa(struct.pack("<L", int(f[2], 16)))
        except (ValueError, struct.error, OSError):
            continue
        if gw == "0.0.0.0":
            continue
        if best is None or metric < best[0]:
            best = (metric, f[0], gw)
    return (best[1], best[2]) if best else (None, None)


def pick_interface(preferred: str = "auto") -> str | None:
    """Resolve the interface to measure. Falls back to the first up, non-loopback NIC."""
    if preferred and preferred != "auto":
        return preferred

    iface, _ = default_route()
    if iface:
        return iface

    try:
        import psutil
    except ImportError:
        return None
    try:
        stats = psutil.net_if_stats()
    except OSError:
        return None
    # No route yet (early boot, say). A NIC that already has an address is a far
    # better guess than one that is merely up.
    up = [name for name, st in stats.items()
          if st.isup and not name.startswith(("lo", "docker", "veth", "br-"))]
    return next((name for name in up if iface_ipv4(name)), up[0] if up else None)


def iface_ipv4(name: str | None) -> str | None:
    """First IPv4 address on ``name``, or ``None`` if it has none (yet)."""
    if not name:
        return None
    try:
        import psutil
    except ImportError:
        return None
    try:
        addrs = psutil.net_if_addrs().get(name, [])
    except OSError:
        return None
    return next((a.address for a in addrs if a.family == socket.AF_INET), None)


def connection_type(name: str | None) -> str:
    """A plain name for the kind of link an interface is."""
    if not name:
        return "not connected"
    if os.path.isdir(f"/sys/class/net/{name}/wireless") or name.startswith("wl"):
        return "Wi-Fi"
    if name.startswith(("eth", "en")):
        return "Ethernet"
    return name


def local_user() -> str:
    """The login name netmon runs as: the user to ssh in with."""
    try:
        return getpass.getuser()
    except (KeyError, OSError):
        return "?"


def link_is_up(name: str) -> bool | None:
    """Carrier state from sysfs; ``None`` when the kernel does not know."""
    try:
        with open(f"/sys/class/net/{name}/operstate", "r", encoding="ascii") as fh:
            state = fh.read().strip()
    except OSError:
        return None
    if state == "up":
        return True
    return None if state == "unknown" else False


def wifi_signal_dbm(name: str) -> float | None:
    """Signal level from /proc/net/wireless; ``None`` for a wired interface."""
    try:
        with open("/proc/net/wireless", "r", encoding="ascii") as fh:
            lines = fh.read().splitlines()[2:]
    except OSError:
        return None
    for line in lines:
        # iface: status link level noise ...
        f = line.split()
        if len(f) >= 4 and f[0].rstrip(":") == name:
            try:
                level = float(f[3].rstrip("."))
            except ValueError:
                return None
            return level if level < 0 else None
    return None


def read_cpu_temp() -> float | None:
    """SoC temperature in Celsius, or ``None`` where sysfs does not expose it."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r", encoding="ascii") as fh:
            return int(fh.read().strip()) / 1000.0
    except (OSError, ValueError):
        return None


def list_framebuffers() -> list[str]:
    """Framebuffers the kernel knows about, as ``fb0 vc4drmfb 800x480``."""
    out = []
    for path in sorted(glob.glob("/sys/class/graphics/fb[0-9]*")):
        try:
            with open(f"{path}/name", "r", encoding="ascii") as fh:
                name = fh.read().strip()
            with open(f"{path}/virtual_size", "r", encoding="ascii") as fh:
                size = fh.read().strip().replace(",", "x")
        except OSError:
            continue
        out.append(f"{os.path.basename(path)} {name} {size}")
    return out


def root_filesystem() -> tuple[str | None, bool]:
    """Return ``(device name, read-only?)`` for /, such as ``("mmcblk0p2", False)``.

    A card that is failing often gets remounted read-only by the kernel.
    """
    try:
        with open(_MOUNTS_FILE, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return (None, False)
    # The last entry for / is the one in effect.
    for line in reversed(lines):
        f = line.split()
        if len(f) >= 4 and f[1] == "/" and f[0] != "rootfs":
            return (os.path.basename(f[0]), "ro" in f[3].split(","))
    return (None, False)


def ext4_stats(device: str | None) -> dict | None:
    """Write totals and error count that ext4 keeps for a filesystem.

    ``lifetime_kb`` is stored on disk, so it survives reboots and counts every
    write since the filesystem was created. ``None`` if / is not ext4.
    """
    names = [device] if device else []
    try:
        # The mount table can say /dev/root; with a single filesystem, that is it.
        found = [n for n in os.listdir(_EXT4_DIR) if n != "features"]
    except OSError:
        return None
    if not (device and device in found):
        names = found if len(found) == 1 else []
    for name in names:
        try:
            out = {}
            for key, filename in (("lifetime_kb", "lifetime_write_kbytes"),
                                  ("session_kb", "session_write_kbytes"),
                                  ("errors", "errors_count")):
                with open(f"{_EXT4_DIR}/{name}/{filename}", "r", encoding="ascii") as fh:
                    out[key] = int(fh.read().strip())
            return out
        except (OSError, ValueError):
            return None
    return None


def host_uptime() -> float | None:
    """Seconds since boot, or ``None`` where /proc is not available."""
    try:
        with open("/proc/uptime", "r", encoding="ascii") as fh:
            return float(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def read_tcp_snmp() -> dict[str, int]:
    """Kernel-wide TCP counters from /proc/net/snmp (``RetransSegs``, ``OutSegs``, ...)."""
    try:
        with open("/proc/net/snmp", "r", encoding="ascii") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return {}

    for i in range(0, len(lines) - 1, 2):
        head, body = lines[i], lines[i + 1]
        if not head.startswith("Tcp:"):
            continue
        keys = head.split()[1:]
        vals = body.split()[1:]
        out = {}
        for k, v in zip(keys, vals):
            try:
                out[k] = int(v)
            except ValueError:
                pass
        return out
    return {}


# --------------------------------------------------------------------------- #
# Formatting - the dashboard is 800x480, so every string is kept short
# --------------------------------------------------------------------------- #


def fmt_ms(value: float | None, digits: int = 1) -> str:
    if value is None:
        return "--"
    if value >= 1000:
        return f"{value / 1000:.2f}s"
    if value >= 100:
        digits = 0
    return f"{value:.{digits}f}"


def fmt_mbps(value: float | None) -> str:
    if value is None:
        return "--"
    if value >= 100:
        return f"{value:.0f}"
    if value >= 10:
        return f"{value:.1f}"
    if value >= 1:
        return f"{value:.2f}"
    if value <= 0:
        return "0"
    return f"{value:.3f}"


def fmt_pct(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "--"
    if value == 0:
        return "0"
    return f"{value:.{digits}f}"


def fmt_avail(value: float | None) -> str:
    """Availability needs its decimals: 99.974 % is four outage-minutes a day,
    and rounding it to 100 % hides exactly the thing being watched."""
    if value is None:
        return "--"
    if value >= 99.9995:
        return "100"
    if value < 90:
        return f"{value:.1f}"
    return f"{value:.3f}"


def fmt_duration(seconds: float | None, short: bool = True) -> str:
    """``3d 4h`` / ``4h 12m`` / ``12m 05s`` / ``45s``."""
    if seconds is None:
        return "--"
    s = int(max(0, seconds))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s" if not short else f"{m}m{s:02d}"
    return f"{s}s"


def fmt_rate(value: float | None) -> str:
    if value is None:
        return "--"
    if value == 0:
        return "0"
    if value < 1:
        return f"{value:.2f}"
    return f"{value:.1f}"


def fmt_clock(ts: float | None = None) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts if ts is not None else time.time()))


# --------------------------------------------------------------------------- #
# Statistics without numpy
# --------------------------------------------------------------------------- #


def percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile. ``pct`` is 0-100."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    k = (len(vals) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(vals) - 1)
    frac = k - lo
    return vals[lo] * (1 - frac) + vals[hi] * frac


def mean(values: list[float]) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def clamp(value: float, low: float, high: float) -> float:
    return low if value < low else (high if value > high else value)
