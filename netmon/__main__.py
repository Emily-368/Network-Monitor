"""Command line entry point.

    python -m netmon                  collector + dashboard (the kiosk default)
    python -m netmon --windowed       same, but in a window - handy over VNC
    python -m netmon --headless       collector only, no display needed
    python -m netmon --selftest       probe everything once and print the result
    python -m netmon --report         summarise the stored history and exit
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import threading
import time

from . import __version__
from .collector import Collector
from .config import load_config
from .state import LiveState
from .storage import Storage
from .util import (default_route, fmt_duration, fmt_mbps, fmt_ms, fmt_pct,
                   iface_ipv4, pick_interface)

log = logging.getLogger("netmon")


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="netmon",
        description="Real-time network monitor for a Raspberry Pi with a TFT panel.",
    )
    p.add_argument("-c", "--config", help="path to config.yaml")
    p.add_argument("--headless", action="store_true",
                   help="run the collector without a display")
    p.add_argument("--windowed", action="store_true",
                   help="do not go fullscreen")
    p.add_argument("--selftest", action="store_true",
                   help="run one round of every probe, print findings, exit")
    p.add_argument("--report", action="store_true",
                   help="print a summary of the stored history and exit")
    p.add_argument("--log-level", default=None,
                   help="DEBUG, INFO, WARNING, ERROR")
    p.add_argument("--version", action="version", version=f"netmon {__version__}")
    return p.parse_args(argv)


# --------------------------------------------------------------------------- #
# Modes
# --------------------------------------------------------------------------- #


def _selftest(cfg) -> int:
    print(f"netmon {__version__} self-test")
    print(f"  config      : {cfg.source_path or '(built-in defaults)'}")

    iface, gateway = default_route()
    print(f"  default route: iface={iface or '?'} gateway={gateway or '?'}")
    measured = pick_interface(cfg.general.interface)
    print(f"  measuring on : {measured or '(none found)'}"
          f" address={iface_ipv4(measured) or 'none'}")
    print(f"  database     : {cfg.db_file()}")

    # Report which optional pieces are present before anything is attempted.
    for module, why in (("icmplib", "ICMP echo without root"),
                        ("psutil", "interface + socket counters"),
                        ("yaml", "config file"),
                        ("speedtest", "capacity test (method: speedtest)")):
        try:
            __import__(module)
            print(f"  [ok]   {module:<10} {why}")
        except ImportError:
            print(f"  [MISS] {module:<10} {why}")
    try:
        import tkinter                                      # noqa: F401
        print(f"  [ok]   {'tkinter':<10} dashboard")
    except ImportError:
        print(f"  [MISS] {'tkinter':<10} dashboard (sudo apt install python3-tk)")

    state = LiveState(cfg)
    collector = Collector(cfg, state=state)
    print("\n  probing (this takes a couple of seconds)...")
    results = asyncio.run(collector.run_once())
    for name, res in results.items():
        if res.get("ok"):
            print(f"  [ok]   {name:<10} {res['ms']:.0f} ms")
        else:
            print(f"  [FAIL] {name:<10} {res.get('error')}")

    snap = state.snapshot(60)
    print("\n  measurements")
    for t in snap["targets"]:
        print(f"    {t['name']:<12} {t['host']:<16} "
              f"rtt {fmt_ms(t['rtt']):>8} ms   "
              f"loss {fmt_pct(t['loss_pct']):>5} %   "
              f"{'alive' if t['alive'] else 'NO REPLY'}")
    i = snap["iface"]
    print(f"    throughput   in {fmt_mbps(i['rx'])} / out {fmt_mbps(i['tx'])} Mbps"
          f"   link {fmt_mbps(i['speed_mbps'])} Mbps")
    print(f"    errors       {i['err_rate'] if i['err_rate'] is not None else '--'}/s")
    c = snap["tcp"]
    print(f"    tcp connect  {fmt_ms(c['connect_ms'])} ms to {c['host']}:{c['port']}"
          f"   retrans {fmt_pct(c['retrans_pct'])} %")
    d = snap["dns"]
    print(f"    dns lookup   {fmt_ms(d['ms'])} ms for {d['host']}"
          f"{'' if d['ms'] is not None else '   ' + str(d['last_error'])}")
    s = snap["system"]
    print(f"    the Pi       temp {s['temp_c'] if s['temp_c'] is not None else '--'} C"
          f"   {s['power_text'] or 'power unknown (no vcgencmd)'}")

    failed = [n for n, r in results.items() if not r.get("ok")]
    unreachable = [t["name"] for t in snap["targets"] if not t["alive"]]
    if failed:
        print(f"\n  FAILED probes: {', '.join(failed)}")
    if unreachable:
        print(f"  no reply from: {', '.join(unreachable)}")
        print("  if everything is unreachable, see 'Unprivileged ICMP' in the README")
    if not failed and not unreachable:
        print("\n  all good - run 'python -m netmon' to start the dashboard")
    return 1 if failed else 0


def _report(cfg) -> int:
    storage = Storage(cfg.db_file(), cfg.general.raw_retention_hours,
                      cfg.general.retention_days)
    if not storage.open():
        print(f"cannot read {cfg.db_file()}")
        return 1
    print(f"netmon history: {cfg.db_file()} "
          f"({storage.db_size_bytes() / 1024:.0f} KiB)\n")
    print(f"{'target':<14}{'1h avail':>10}{'24h avail':>11}{'7d avail':>10}")
    for t in cfg.targets:
        a1, _ = storage.availability(t.name, 3600)
        a24, _ = storage.availability(t.name, 86400)
        a7, _ = storage.availability(t.name, 7 * 86400)
        print(f"{t.name:<14}{fmt_pct(a1, 3):>9}%{fmt_pct(a24, 3):>10}%"
              f"{fmt_pct(a7, 3):>9}%")

    bw = storage.last_bandwidth()
    if bw:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(bw["ts"]))
        print(f"\nlast capacity test ({when}, {bw['method']}): "
              f"{fmt_mbps(bw['down_mbps'])} down / {fmt_mbps(bw['up_mbps'])} up Mbps")
    else:
        print("\nno capacity test recorded yet")

    events = storage.recent_events(12)
    if events:
        print("\nrecent events")
        for ev in events:
            print(f"  {time.strftime('%m-%d %H:%M:%S', time.localtime(ev['ts']))} "
                  f"{ev['kind']:<5} {ev['detail']}")
    storage.close()
    return 0


def _headless(cfg) -> int:
    state = LiveState(cfg)
    collector = Collector(cfg, state=state)
    stop = threading.Event()

    def _signal(signum, _frame):
        log.info("signal %s - shutting down", signum)
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _signal)
        except (ValueError, AttributeError, OSError):
            pass

    collector.start()
    collector.ready.wait(timeout=10)
    if collector.failed:
        log.error("collector failed to start: %s", collector.failed)
        return 1

    log.info("running headless; Ctrl-C to stop")
    while not stop.wait(60.0):
        if collector.failed:
            log.error("collector died: %s", collector.failed)
            return 1
        snap = state.snapshot(60)
        p = snap["primary"] or {}
        log.info("%s rtt %s ms  jitter %s  loss %s%%  avail24h %s%%  "
                 "thru %s/%s Mbps  up %s",
                 p.get("name", "?"), fmt_ms(p.get("rtt_avg")), fmt_ms(p.get("jitter")),
                 fmt_pct(p.get("loss_pct")), fmt_pct(p.get("avail_24h"), 3),
                 fmt_mbps(snap["iface"].get("rx")), fmt_mbps(snap["iface"].get("tx")),
                 fmt_duration(snap["uptime_s"]))
    collector.stop()
    return 0


def _dashboard(cfg) -> int:
    try:
        import tkinter                                      # noqa: F401
    except ImportError:
        print("tkinter is not installed. Either:\n"
              "  sudo apt install python3-tk      (for the dashboard)\n"
              "  python -m netmon --headless      (to run without a display)",
              file=sys.stderr)
        return 1

    from .ui import run_dashboard

    state = LiveState(cfg)
    collector = Collector(cfg, state=state)
    collector.start()
    collector.ready.wait(timeout=10)
    if collector.failed:
        log.error("collector failed to start: %s", collector.failed)
        return 1
    try:
        run_dashboard(cfg, state, collector)
    finally:
        collector.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        cfg = load_config(args.config)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 2
    _setup_logging(args.log_level or cfg.general.log_level)
    if cfg.source_path:
        log.debug("loaded config from %s", cfg.source_path)

    if args.windowed:
        cfg.ui.fullscreen = False
        cfg.ui.hide_cursor = False

    if args.selftest:
        return _selftest(cfg)
    if args.report:
        return _report(cfg)
    if args.headless:
        return _headless(cfg)
    return _dashboard(cfg)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
