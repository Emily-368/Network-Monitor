"""Capacity measurement - the "bandwidth" half of bandwidth vs throughput.

Bandwidth here means *how much the link can carry*, which can only be found by
filling it. That makes it the opposite of every other probe in netmon: it is
expensive, it perturbs what it measures, and it must run rarely (hourly by
default). Throughput - what the Pi itself is sending and receiving - comes from
:mod:`netmon.probes.iface` at 1 Hz and is free.

Two methods:

  * ``iperf3`` against a server on your own LAN. Strongly preferred on a Pi 3B:
    it measures your link rather than the internet, finishes in seconds, and
    costs almost no RAM. Needs ``apt install iperf3`` here and ``iperf3 -s``
    there.
  * ``speedtest`` via speedtest-cli. Measures the path to the internet, takes
    ~30 s and is heavier. ``threads`` is capped and upload pre-allocation is
    turned off, because the default allocates hundreds of MB and a 1 GB Pi
    cannot spare it.

Either way the test runs in a worker thread, so the 1 Hz probes keep ticking -
and you will see throughput spike to the capacity figure while it runs, which
is the clearest possible illustration of the difference between the two.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
import time

log = logging.getLogger(__name__)

MEGA = 1_000_000.0


def _run_iperf3(cfg) -> dict:
    """Blocking. Download then upload against an iperf3 server."""
    binary = shutil.which("iperf3")
    if not binary:
        return {"ok": False, "note": "iperf3 not installed (sudo apt install iperf3)"}
    if not cfg.iperf3_server:
        return {"ok": False, "note": "set probes.bandwidth.iperf3_server"}

    def one(reverse: bool) -> tuple[float | None, str | None]:
        cmd = [binary, "-c", cfg.iperf3_server, "-p", str(cfg.iperf3_port),
               "-t", str(cfg.iperf3_duration), "-J"]
        if reverse:
            cmd.append("-R")        # server sends -> measures our download
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=cfg.iperf3_duration + 20)
        except subprocess.TimeoutExpired:
            return (None, "iperf3 timed out")
        except OSError as exc:
            return (None, f"iperf3 failed: {exc}")
        try:
            data = json.loads(proc.stdout or "{}")
        except ValueError:
            return (None, (proc.stderr or "iperf3 returned no JSON").strip()[:120])
        if data.get("error"):
            return (None, str(data["error"])[:120])
        end = data.get("end", {})
        summary = end.get("sum_received") or end.get("sum_sent") or {}
        bps = summary.get("bits_per_second")
        if not bps:
            return (None, "iperf3 reported no throughput")
        return (bps / MEGA, None)

    down, err1 = one(reverse=True)
    up, err2 = one(reverse=False)
    note = err1 or err2 or ""
    return {
        "ok": down is not None or up is not None,
        "down_mbps": down,
        "up_mbps": up,
        "latency_ms": None,
        "server": f"{cfg.iperf3_server}:{cfg.iperf3_port}",
        "note": note,
    }


def _run_speedtest(cfg) -> dict:
    """Blocking. speedtest.net via speedtest-cli, tuned for 1 GB of RAM."""
    try:
        import speedtest
    except ImportError:
        return {"ok": False, "note": "speedtest-cli not installed (pip install speedtest-cli)"}

    threads = max(1, int(cfg.threads or 2))
    try:
        st = speedtest.Speedtest(secure=True)
        st.get_best_server()
        # pre_allocate=False keeps the upload payload off the heap; the default
        # tries to allocate the whole thing up front and OOMs a Pi 3B.
        down = st.download(threads=threads) / MEGA
        up = st.upload(threads=threads, pre_allocate=False) / MEGA
        res = st.results.dict()
        server = res.get("server", {}) or {}
        return {
            "ok": True,
            "down_mbps": down,
            "up_mbps": up,
            "latency_ms": res.get("ping"),
            "server": f"{server.get('sponsor', '?')} / {server.get('name', '?')}",
            "note": "",
        }
    except Exception as exc:  # speedtest-cli raises a wide zoo of its own errors
        return {"ok": False, "note": f"{type(exc).__name__}: {exc}"[:140]}


class BandwidthProbe:
    name = "bandwidth"

    def __init__(self, cfg, state, storage):
        self.cfg = cfg
        self.bcfg = cfg.probes.bandwidth
        self.state = state
        self.storage = storage
        self.interval = self.bcfg.interval
        self.enabled = bool(self.bcfg.enabled) and (self.bcfg.method or "none") != "none"
        # Skip the first firing unless asked, so a reboot does not immediately
        # saturate the link you are trying to watch.
        self.skip_first = not self.bcfg.run_at_start

        with state.lock:
            state.bandwidth.method = self.bcfg.method

    def _measure(self) -> dict:
        method = (self.bcfg.method or "none").lower()
        if method == "iperf3":
            return _run_iperf3(self.bcfg)
        if method == "speedtest":
            return _run_speedtest(self.bcfg)
        return {"ok": False, "note": f"unknown method {method!r}"}

    async def tick(self) -> None:
        if not self.enabled:
            return
        if self.skip_first:
            self.skip_first = False
            log.info("bandwidth: first test deferred (run_at_start is false); "
                     "next in %.0f min", self.interval / 60)
            return

        started = time.time()
        with self.state.lock:
            self.state.bandwidth.running = True
            self.state.bandwidth.started_ts = started
        log.info("bandwidth: running %s test", self.bcfg.method)

        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(None, self._measure)
        except Exception as exc:  # pragma: no cover - defensive
            result = {"ok": False, "note": f"{type(exc).__name__}: {exc}"[:140]}
        finally:
            with self.state.lock:
                self.state.bandwidth.running = False
                self.state.bandwidth.spans.append((started, time.time()))

        result.setdefault("down_mbps", None)
        result.setdefault("up_mbps", None)
        result.setdefault("latency_ms", None)
        result.setdefault("server", "")
        result["ts"] = time.time()
        result["method"] = self.bcfg.method

        with self.state.lock:
            self.state.bandwidth.record(result)

        self.storage.add_bandwidth(result["ts"], result["method"], result["down_mbps"],
                                   result["up_mbps"], result["latency_ms"],
                                   result["server"], result["ok"], result.get("note", ""))
        if result["ok"]:
            log.info("bandwidth: %.1f down / %.1f up Mbps via %s",
                     result["down_mbps"] or 0.0, result["up_mbps"] or 0.0,
                     result["server"] or self.bcfg.method)
        else:
            log.warning("bandwidth test failed: %s", result.get("note"))
            self.state.note_error(f"bandwidth: {result.get('note')}")
