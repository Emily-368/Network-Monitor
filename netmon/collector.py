"""The collector: an asyncio scheduler living in a background thread.

Each probe gets its own task with its own cadence, so the 1 Hz latency round is
never held up by a 30-second capacity test. A separate maintenance task does the
SQLite housekeeping (flush, rollup, purge) and recomputes the availability
figures, which are the only numbers that come from the database rather than RAM.
That work runs in a worker thread: an SD card can stall for seconds, and doing it
on the event loop would stall the 1 Hz probes with it.

A tick that overruns its interval is logged and the next one starts
immediately - the schedule drifts rather than piling up tasks, which is the
right trade on a Pi: a backlog of ping rounds would be worse than a gap.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time

from .probes import (BandwidthProbe, DnsProbe, IfaceProbe, PingProbe,
                     SystemProbe, TcpProbe)
from .state import LiveState
from .storage import Storage
from .util import default_route

log = logging.getLogger(__name__)

FLUSH_INTERVAL = 5.0        # seconds between SQLite commits
MAINTENANCE_INTERVAL = 30.0  # availability recompute + rollup/purge cadence
ROUTE_INTERVAL = 5.0        # how often "host: auto" follows the default route


class Collector(threading.Thread):
    """Runs every probe. Start it, read :attr:`state`, then :meth:`stop` it."""

    def __init__(self, cfg, state: LiveState | None = None,
                 storage: Storage | None = None):
        super().__init__(name="netmon-collector", daemon=True)
        self.cfg = cfg
        self.state = state or LiveState(cfg)
        self.storage = storage or Storage(
            cfg.db_file(),
            raw_retention_hours=cfg.general.raw_retention_hours,
            retention_days=cfg.general.retention_days,
        )
        self.probes: list = []
        self.ready = threading.Event()      # set once probes are constructed
        self.failed: BaseException | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_async: asyncio.Event | None = None
        self._stopping = threading.Event()
        self._last_rollup = 0.0

    # -- target resolution -------------------------------------------------- #

    def _resolve_targets(self) -> None:
        """Turn ``host: auto`` into the current default gateway.

        Called repeatedly, because the dashboard can start before the network is
        up and a DHCP renewal can move the gateway. With no route the target has
        no host at all and reads as failing, which is the truth of the situation.
        """
        _, gateway = default_route()
        for target in self.state.targets.values():
            if target.host != "auto":
                target.resolved_host = target.host
            elif gateway != target.resolved_host:
                target.resolved_host = gateway
                if gateway:
                    log.info("target %r -> default gateway %s", target.name, gateway)
                else:
                    log.warning("target %r is 'auto' but there is no default route",
                                target.name)

    # -- thread body -------------------------------------------------------- #

    def run(self) -> None:
        try:
            asyncio.run(self._main())
        except BaseException as exc:    # noqa: BLE001 - surfaced to the UI
            self.failed = exc
            log.exception("collector stopped: %s", exc)
            self.ready.set()

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_async = asyncio.Event()

        self.storage.open()
        with self.state.lock:
            self.state.db_error = self.storage.last_error
        self._resolve_targets()

        self.probes = [
            PingProbe(self.cfg, self.state, self.storage),
            IfaceProbe(self.cfg, self.state, self.storage),
            TcpProbe(self.cfg, self.state, self.storage),
            DnsProbe(self.cfg, self.state, self.storage),
            SystemProbe(self.cfg, self.state, self.storage),
            BandwidthProbe(self.cfg, self.state, self.storage),
        ]
        self._restore_last_bandwidth()
        self._restore_outages()
        self.ready.set()
        log.info("collector started: %s", ", ".join(
            f"{p.name}@{p.interval:g}s" for p in self.probes))

        tasks = [asyncio.create_task(self._periodic(p), name=f"probe-{p.name}")
                 for p in self.probes]
        tasks.append(asyncio.create_task(self._maintenance(), name="maintenance"))

        await self._stop_async.wait()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        self.storage.flush()
        self.storage.rollup()
        self.storage.close()
        log.info("collector stopped cleanly")

    def _restore_last_bandwidth(self) -> None:
        """Show the previous capacity result straight away instead of '--'."""
        last = self.storage.last_bandwidth()
        if not last:
            return
        with self.state.lock:
            self.state.bandwidth.record({**last, "ok": True})
        log.info("restored last bandwidth result from %s",
                 time.strftime("%Y-%m-%d %H:%M", time.localtime(last["ts"])))

    def _restore_outages(self) -> None:
        """Bring back recent events and the last outage, which live on disk."""
        events = self.storage.recent_events(30)
        durations = {t.name: self.storage.last_outage_seconds(t.name)
                     for t in self.state.targets.values()}
        with self.state.lock:
            self.state.events = list(reversed(events))
            for target in self.state.targets.values():
                target.last_outage_duration = durations[target.name]

    # -- scheduling --------------------------------------------------------- #

    async def _periodic(self, probe) -> None:
        """Run ``probe.tick()`` forever, holding its interval as a floor."""
        interval = max(0.1, float(probe.interval))
        loop = asyncio.get_running_loop()
        consecutive_errors = 0

        while not self._stop_async.is_set():
            started = loop.time()
            try:
                await probe.tick()
                consecutive_errors = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad probe must not kill the rest
                consecutive_errors += 1
                level = logging.WARNING if consecutive_errors < 4 else logging.DEBUG
                log.log(level, "probe %s failed: %s", probe.name, exc,
                        exc_info=consecutive_errors == 1)
                self.state.note_error(f"{probe.name}: {type(exc).__name__}")

            elapsed = loop.time() - started
            if elapsed > interval * 1.5 and interval >= 1.0:
                log.debug("probe %s took %.2fs (interval %.2fs)",
                          probe.name, elapsed, interval)
            delay = interval - elapsed
            if delay <= 0:
                await asyncio.sleep(0)      # yield, then go straight round again
                continue
            try:
                await asyncio.wait_for(self._stop_async.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def _maintenance(self) -> None:
        """SQLite housekeeping and the availability figures."""
        loop = asyncio.get_running_loop()
        next_flush = loop.time() + FLUSH_INTERVAL
        next_maint = loop.time() + 5.0
        next_route = loop.time() + ROUTE_INTERVAL

        while not self._stop_async.is_set():
            try:
                await asyncio.wait_for(self._stop_async.wait(), timeout=1.0)
                return
            except asyncio.TimeoutError:
                pass

            now_mono = loop.time()
            if now_mono >= next_route:
                next_route = now_mono + ROUTE_INTERVAL
                self._resolve_targets()

            if now_mono >= next_flush:
                next_flush = now_mono + FLUSH_INTERVAL
                try:
                    await asyncio.to_thread(self.storage.flush)
                except Exception as exc:  # noqa: BLE001
                    log.warning("flush failed: %s", exc)

            if now_mono >= next_maint:
                next_maint = now_mono + MAINTENANCE_INTERVAL
                try:
                    await asyncio.to_thread(self._housekeeping)
                except Exception as exc:  # noqa: BLE001
                    log.warning("housekeeping failed: %s", exc)

    def _housekeeping(self) -> None:
        """Worker thread. Everything here may block on the SD card."""
        self._update_availability()
        now = time.time()
        if now - self._last_rollup >= 60.0:
            self._last_rollup = now
            self.storage.rollup(now)
            self.storage.purge(now)
            size = self.storage.db_size_bytes()
            with self.state.lock:
                self.state.db_bytes = size
                self.state.db_error = self.storage.last_error

    def _update_availability(self) -> None:
        """Network availability = answered echoes / sent echoes, over 1 h and 24 h."""
        now = time.time()
        for target in list(self.state.targets.values()):
            a1, n1 = self.storage.availability(target.name, 3600, now)
            a24, n24 = self.storage.availability(target.name, 86400, now)
            outages = self.storage.outage_count(target.name, 86400, now)
            with self.state.lock:
                target.avail_1h, target.avail_1h_n = a1, n1
                target.avail_24h, target.avail_24h_n = a24, n24
                target.outages_24h = outages

    # -- shutdown ----------------------------------------------------------- #

    def stop(self, timeout: float = 5.0) -> None:
        """Ask the loop to finish and wait for the thread to exit."""
        if self._stopping.is_set():
            return
        self._stopping.set()
        loop, stop_event = self._loop, self._stop_async
        if loop is not None and stop_event is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(stop_event.set)
            except RuntimeError:
                pass
        if self.is_alive():
            self.join(timeout=timeout)
            if self.is_alive():
                log.warning("collector thread did not exit within %.1fs", timeout)

    # -- on demand, from the UI thread -------------------------------------- #

    def request_bandwidth(self) -> bool:
        """Kick off a capacity test now. Safe to call from the Tk thread."""
        loop = self._loop
        probe = next((p for p in self.probes if p.name == "bandwidth"), None)
        if loop is None or loop.is_closed() or probe is None or not probe.enabled:
            return False
        with self.state.lock:
            if self.state.bandwidth.running:
                return False

        def _launch() -> None:
            probe.skip_first = False
            asyncio.create_task(probe.tick(), name="bandwidth-on-demand")

        try:
            loop.call_soon_threadsafe(_launch)
        except RuntimeError:
            return False
        return True

    # -- one-shot mode, for --selftest -------------------------------------- #

    async def run_once(self) -> dict:
        """Construct the probes, tick each once, and return the snapshot."""
        self.storage.open()
        self._resolve_targets()
        self.probes = [
            PingProbe(self.cfg, self.state, self.storage),
            IfaceProbe(self.cfg, self.state, self.storage),
            TcpProbe(self.cfg, self.state, self.storage),
            DnsProbe(self.cfg, self.state, self.storage),
            SystemProbe(self.cfg, self.state, self.storage),
        ]
        results = {}
        for probe in self.probes:
            t0 = time.perf_counter()
            try:
                # The interface probe needs two reads to produce a delta.
                await probe.tick()
                if probe.name == "iface":
                    await asyncio.sleep(1.0)
                    await probe.tick()
                results[probe.name] = {"ok": True,
                                       "ms": (time.perf_counter() - t0) * 1000}
            except Exception as exc:  # noqa: BLE001
                results[probe.name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        self.storage.flush()
        self.storage.close()
        return results
