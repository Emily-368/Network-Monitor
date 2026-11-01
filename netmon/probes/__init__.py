"""Measurement probes.

Each probe is an object with ``name``, ``interval`` and an ``async def tick()``.
The collector schedules them independently, so a slow probe (a capacity test)
never delays a fast one (the 1 Hz ping round).
"""

from .ping import PingProbe
from .iface import IfaceProbe
from .tcp import TcpProbe
from .bandwidth import BandwidthProbe

__all__ = ["PingProbe", "IfaceProbe", "TcpProbe", "BandwidthProbe"]
