"""The health of the Pi itself, so a sick Pi is not mistaken for a sick network.

The Pi 3B's Ethernet hangs off its USB bus, so a weak power supply can look exactly
like a flaky link: dropped frames, loss, resets. Under-voltage is reported by the
firmware, read here through ``vcgencmd get_throttled``.

The SD card is watched too, since it is what wears out first on a Pi. Consumer
cards keep their wear-levelling data inside their own controller and do not report
it to Linux, so real wear cannot be read. What is reported instead:

  * **written** - the total ext4 has written to the filesystem in its lifetime.
    It is real, and it is kept on disk, so it survives reboots.
  * **wear estimate** - that total, as a share of ``card size x sd_rated_cycles``.
    An estimate only: it cannot see what the controller actually does.
  * **errors and read-only** - ext4's error count, and whether the kernel has
    remounted the card read-only. These are the signs of a card actually failing.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time

log = logging.getLogger(__name__)

try:
    import psutil
    HAVE_PSUTIL = True
except ImportError:  # pragma: no cover
    HAVE_PSUTIL = False

from ..util import ext4_stats, host_uptime, read_cpu_temp, root_filesystem

_VCGENCMD = shutil.which("vcgencmd")

# Bits of the get_throttled value.
UNDERVOLT_NOW = 1 << 0
FREQ_CAPPED_NOW = 1 << 1
THROTTLED_NOW = 1 << 2
TEMP_LIMIT_NOW = 1 << 3
UNDERVOLT_SINCE_BOOT = 1 << 16
THROTTLED_SINCE_BOOT = 1 << 18


def describe_throttled(bits: int) -> tuple[str, str]:
    """Map the firmware's flags onto ``(level, text)``, most severe first."""
    if bits & UNDERVOLT_NOW:
        return ("crit", "UNDER-VOLTAGE")
    if bits & (THROTTLED_NOW | FREQ_CAPPED_NOW | TEMP_LIMIT_NOW):
        return ("warn", "THROTTLED")
    if bits & UNDERVOLT_SINCE_BOOT:
        return ("warn", "power dipped")
    if bits & THROTTLED_SINCE_BOOT:
        return ("warn", "was throttled")
    return ("ok", "power OK")


class SystemProbe:
    name = "system"

    def __init__(self, cfg, state, storage):
        self.state = state
        self.storage = storage
        self.interval = cfg.probes.system.interval
        self.rated_cycles = cfg.probes.system.sd_rated_cycles
        self._undervolt = False
        self._readonly = False

    async def _read_throttled(self) -> int | None:
        if not _VCGENCMD:
            return None
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                _VCGENCMD, "get_throttled",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
            # throttled=0x50000
            return int(out.decode("ascii", "replace").strip().split("=")[-1], 16)
        except asyncio.TimeoutError:
            if proc is not None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            return None
        except (OSError, ValueError):
            return None

    def _read_sd(self) -> dict:
        try:
            usage = shutil.disk_usage("/")
        except OSError:
            usage = None
        device, readonly = root_filesystem()
        stats = ext4_stats(device)
        uptime = host_uptime()

        out = {"used_pct": None, "free": None, "written": None, "per_day": None,
               "wear": None, "errors": None, "readonly": readonly}
        if usage and usage.total:
            out["used_pct"] = 100.0 * (usage.total - usage.free) / usage.total
            out["free"] = usage.free
        if stats:
            out["written"] = stats["lifetime_kb"] * 1024
            out["errors"] = stats["errors"]
            if uptime:
                out["per_day"] = stats["session_kb"] * 1024 / uptime * 86400
            if usage and usage.total:
                out["wear"] = 100.0 * out["written"] / (usage.total * self.rated_cycles)
        return out

    async def tick(self) -> None:
        bits = await self._read_throttled()
        cpu = psutil.cpu_percent(interval=None) if HAVE_PSUTIL else None
        ram = psutil.virtual_memory().percent if HAVE_PSUTIL else None
        sd = self._read_sd()
        level, text = describe_throttled(bits) if bits is not None else (None, "")

        with self.state.lock:
            st = self.state.system
            st.temp_c = read_cpu_temp()
            st.cpu_pct = cpu
            st.ram_pct = ram
            st.sd_used_pct = sd["used_pct"]
            st.sd_free_bytes = sd["free"]
            st.sd_written_bytes = sd["written"]
            st.sd_written_per_day = sd["per_day"]
            st.sd_wear_pct = sd["wear"]
            st.sd_errors = sd["errors"]
            st.sd_readonly = sd["readonly"]
            st.uptime_s = host_uptime()
            st.power_level = level
            st.power_text = text

        # Only the edge is an event; the flag stays set for as long as it lasts.
        undervolt = bool(bits and bits & UNDERVOLT_NOW)
        if undervolt and not self._undervolt:
            now = time.time()
            detail = "Pi under-voltage: check the power supply"
            self.state.note_event(now, "power", "Pi", detail)
            self.storage.add_event(now, "power", "Pi", detail)
            log.warning("%s", detail)
        self._undervolt = undervolt

        if sd["readonly"] and not self._readonly:
            now = time.time()
            detail = "SD card is read-only: it may be failing"
            self.state.note_event(now, "disk", "Pi", detail)
            self.storage.add_event(now, "disk", "Pi", detail)
            log.warning("%s", detail)
        self._readonly = sd["readonly"]
