"""DNS: can this machine actually turn a name into an address?

"The internet is up but nothing loads" is very often DNS, and pinging 8.8.8.8
cannot see it - an echo to an IP address never touches a resolver. This goes
through the system resolver, the same path every application on the Pi uses, so
a broken or slow resolver shows up here even while every ping stays green.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time

log = logging.getLogger(__name__)


class DnsProbe:
    name = "dns"

    def __init__(self, cfg, state, storage):
        self.dcfg = cfg.probes.dns
        self.state = state
        self.storage = storage
        self.interval = self.dcfg.interval
        with state.lock:
            state.dns.host = self.dcfg.host
            state.dns.enabled = bool(self.dcfg.enabled)

    async def _resolve(self) -> tuple[float | None, str | None]:
        """Time one lookup, in ms. Returns ``(ms, error)``."""
        loop = asyncio.get_running_loop()
        t0 = time.perf_counter()
        try:
            await asyncio.wait_for(
                loop.getaddrinfo(self.dcfg.host, None, family=socket.AF_INET,
                                 type=socket.SOCK_STREAM),
                timeout=self.dcfg.timeout,
            )
        except asyncio.TimeoutError:
            return (None, "timeout")
        except OSError as exc:      # socket.gaierror is an OSError
            return (None, f"no answer ({exc.errno})")
        return ((time.perf_counter() - t0) * 1000.0, None)

    async def tick(self) -> None:
        if not self.dcfg.enabled:
            return
        ms, err = await self._resolve()
        now = time.time()

        event = None
        with self.state.lock:
            st = self.state.dns
            st.lookup.add(now, ms)
            st.attempts += 1
            st.last_error = err
            if ms is None:
                st.fails += 1
                st.consec_fail += 1
                if st.consec_fail == st.FAILING_AFTER:
                    event = ("down", f"DNS failing: {self.dcfg.host} ({err})")
            else:
                if st.consec_fail >= st.FAILING_AFTER:
                    event = ("up", "DNS recovered")
                st.consec_fail = 0

        if event:
            kind, detail = event
            self.state.note_event(now, kind, "DNS", detail)
            self.storage.add_event(now, kind, "DNS", detail)
            log.info("%s", detail)
