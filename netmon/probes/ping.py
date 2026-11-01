"""Latency, packet loss and jitter.

ICMP echo via :mod:`icmplib`, which can use unprivileged datagram sockets, so
netmon does not need root (see the sysctl note in the README). If icmplib is
missing or the socket is refused, it falls back to the system ``ping`` binary -
slower and one process per round, but it works everywhere.

Definitions used here, because these three terms get muddled:

  * **latency** - round-trip time of one echo, in ms.
  * **packet loss** - echoes sent minus echoes answered, over a rolling window,
    as a percentage. Measured per round so a partially answered burst counts
    correctly.
  * **jitter** - mean absolute difference between consecutive round-trip times
    (the "packet delay variation" figure that matters for voice and video), not
    the standard deviation.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import time

log = logging.getLogger(__name__)

try:  # icmplib is pure Python, but it may not be installed yet.
    from icmplib import async_ping as _icmplib_async_ping
    from icmplib.exceptions import ICMPLibError, NameLookupError, SocketPermissionError
    HAVE_ICMPLIB = True
except ImportError:  # pragma: no cover - exercised on hosts without icmplib
    HAVE_ICMPLIB = False

    class ICMPLibError(Exception):
        pass

    class NameLookupError(ICMPLibError):
        pass

    class SocketPermissionError(ICMPLibError):
        pass


_PING_BIN = shutil.which("ping")
_TIME_RE = re.compile(r"time[=<]\s*([0-9.]+)\s*ms")


class PingProbe:
    """One probe round pings every target concurrently."""

    name = "ping"

    def __init__(self, cfg, state, storage):
        self.cfg = cfg
        self.pcfg = cfg.probes.ping
        self.state = state
        self.storage = storage
        self.interval = self.pcfg.interval
        self.backend = self._choose_backend()
        self._warned_fallback = False

    def _choose_backend(self) -> str:
        want = (self.pcfg.backend or "auto").lower()
        if want == "icmplib":
            if not HAVE_ICMPLIB:
                log.warning("backend=icmplib but icmplib is not installed; using system ping")
                return "system"
            return "icmplib"
        if want == "system":
            return "system"
        if HAVE_ICMPLIB:
            return "icmplib"
        if _PING_BIN:
            log.warning("icmplib not installed - falling back to the system ping binary")
            return "system"
        raise RuntimeError(
            "no way to send ICMP echoes: install icmplib (pip install icmplib) "
            "or make sure the 'ping' command is on PATH"
        )

    # -- backends ----------------------------------------------------------- #

    async def _echo_icmplib(self, host: str) -> tuple[list[float], int, str | None]:
        try:
            result = await _icmplib_async_ping(
                host,
                count=self.pcfg.count,
                interval=self.pcfg.echo_interval,
                timeout=self.pcfg.timeout,
                privileged=self.pcfg.privileged,
            )
        except NameLookupError:
            return ([], self.pcfg.count, "dns")
        except SocketPermissionError as exc:
            # Unprivileged ICMP is not permitted - switch over for good.
            if not self._warned_fallback and _PING_BIN:
                log.warning("ICMP socket refused (%s) - switching to the system ping binary. "
                            "See the README for the ping_group_range sysctl.", exc)
                self._warned_fallback = True
                self.backend = "system"
            return ([], self.pcfg.count, "permission")
        except ICMPLibError as exc:
            return ([], self.pcfg.count, type(exc).__name__)
        except OSError as exc:
            return ([], self.pcfg.count, f"oserror:{exc.errno}")

        # icmplib reports round-trip times in milliseconds already.
        sent = getattr(result, "packets_sent", self.pcfg.count) or self.pcfg.count
        return (list(result.rtts), sent, None)

    async def _echo_system(self, host: str) -> tuple[list[float], int, str | None]:
        if not _PING_BIN:
            return ([], self.pcfg.count, "no-ping-binary")
        count = self.pcfg.count
        # -W is seconds on Linux/BSD ping and must be a whole number.
        deadline = max(1, int(round(self.pcfg.timeout)))
        cmd = [_PING_BIN, "-n", "-q", "-c", str(count), "-W", str(deadline)]
        if count > 1:
            cmd += ["-i", str(max(0.2, self.pcfg.echo_interval))]
        cmd.append(host)

        budget = self.pcfg.timeout * count + self.pcfg.echo_interval * count + 2.0
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=budget)
        except asyncio.TimeoutError:
            if proc is not None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            return ([], count, "timeout")
        except OSError as exc:
            return ([], count, f"oserror:{exc.errno}")

        text = out.decode("utf-8", "replace")
        rtts = [float(m) for m in _TIME_RE.findall(text)]
        if not rtts:
            # -q suppresses per-echo lines, so read the summary instead.
            m = re.search(r"=\s*[0-9.]+/([0-9.]+)/", text)
            recv = re.search(r"(\d+)\s+received", text)
            n_recv = int(recv.group(1)) if recv else 0
            if m and n_recv:
                rtts = [float(m.group(1))] * n_recv
        return (rtts, count, None if rtts else "timeout")

    async def _echo(self, host: str) -> tuple[list[float], int, str | None]:
        if self.backend == "icmplib":
            return await self._echo_icmplib(host)
        return await self._echo_system(host)

    # -- the probe round ---------------------------------------------------- #

    async def tick(self) -> None:
        targets = list(self.state.targets.values())
        results = await asyncio.gather(
            *(self._echo(t.resolved_host) for t in targets),
            return_exceptions=True,
        )
        ts = time.time()

        with self.state.lock:
            for target, res in zip(targets, results):
                if isinstance(res, BaseException):
                    log.debug("ping %s raised %r", target.name, res)
                    rtts, sent, err = [], self.pcfg.count, type(res).__name__
                else:
                    rtts, sent, err = res

                outcome = target.record(ts, rtts, sent, err)
                rnd = outcome["round"]
                self.storage.add_ping(ts, target.name, rnd.rtt_ms, rnd.sent,
                                      rnd.lost, rnd.jitter_ms)
                if outcome["event"]:
                    kind, detail = outcome["event"]
                    self.storage.add_event(ts, kind, target.name, detail)
                    self.state.events.append({"ts": ts, "kind": kind,
                                              "target": target.name, "detail": detail})
                    del self.state.events[:-30]
                    log.info("%s", detail)
                if err and err not in ("timeout",):
                    self.state.note_error(f"ping {target.name}: {err}")

        self.state.beat(ts)
