"""The health of the Pi itself, so a sick Pi is not mistaken for a sick network.

The Pi 3B's Ethernet hangs off its USB bus, so a weak power supply can look exactly
like a flaky link: dropped frames, loss, resets. Under-voltage is reported by the
firmware, read here through ``vcgencmd get_throttled``.
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

from ..util import host_uptime, read_cpu_temp

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
        self._undervolt = False

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

    async def tick(self) -> None:
        bits = await self._read_throttled()
        cpu = psutil.cpu_percent(interval=None) if HAVE_PSUTIL else None
        level, text = describe_throttled(bits) if bits is not None else (None, "")

        with self.state.lock:
            st = self.state.system
            st.temp_c = read_cpu_temp()
            st.cpu_pct = cpu
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
