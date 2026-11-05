"""One plain-English answer to "what is wrong, and whose problem is it?".

Walks outward from the Pi - cable, address, route, gateway, upstream, DNS, TCP -
and reports the first thing broken, because everything past that point is broken
only as a consequence. Pure logic over a snapshot, so it needs no display.
"""

from __future__ import annotations

from .util import fmt_duration


def _down_for(snap: dict, target: dict) -> str:
    return fmt_duration(snap["now"] - target["outage_since"])


def diagnose(snap: dict) -> tuple[str, str, str]:
    """Return ``(level, headline, detail)`` for the header."""
    if snap["stale"]:
        return ("crit", "COLLECTOR NOT RESPONDING", "readings are out of date")

    targets = snap["targets"]
    primary = snap["primary"] or {}
    iface = snap["iface"]
    if not any(t["has_data"] for t in targets):
        return ("none", "STARTING", f"waiting for {primary.get('name', 'first reply')}")

    if iface["link_up"] is False:
        return ("crit", "LINK DOWN", f"{iface['name']}: check the cable or Wi-Fi")
    if iface["name"] and not iface["ip"]:
        return ("crit", "NO IP ADDRESS", f"{iface['name']} has no DHCP lease")

    gateways = [t for t in targets if t["is_gateway"]]
    upstream = [t for t in targets if not t["is_gateway"]]

    if any(t["no_route"] for t in gateways):
        return ("crit", "NO ROUTE", "no default gateway on this network")
    down = next((t for t in gateways if t["outage_since"]), None)
    if down:
        return ("crit", "LAN FAULT",
                f"router {down['host']} down {_down_for(snap, down)}")

    if upstream and all(t["outage_since"] for t in upstream):
        since = _down_for(snap, primary if primary in upstream else upstream[0])
        if gateways:
            return ("crit", "ISP / UPSTREAM FAULT", f"router OK, internet down {since}")
        return ("crit", "OUTAGE", f"all targets down {since}")
    if primary.get("outage_since"):
        return ("crit", "OUTAGE",
                f"{primary['name']} ({primary['host']}) down {_down_for(snap, primary)}")

    dns = snap["dns"]
    if dns["enabled"] and dns["failing"]:
        return ("crit", "DNS FAULT", f"pings OK, {dns['host']} won't resolve")

    tcp = snap["tcp"]
    recent = tcp["connect_series"][-2:]
    if len(recent) == 2 and recent[0] is None and recent[1] is None:
        return ("crit", "TCP FAULT", f"pings OK, {tcp['host']}:{tcp['port']} failing")

    if snap["bandwidth"]["running"]:
        return ("warn", "CAPACITY TEST RUNNING", "expect latency to spike")
    if primary.get("has_data") and not primary.get("alive"):
        return ("warn", "NO REPLY", f"{primary['name']} ({primary['host']})")
    return ("ok", "ONLINE", f"{primary.get('name', '?')} ({primary.get('host', '?')})")
