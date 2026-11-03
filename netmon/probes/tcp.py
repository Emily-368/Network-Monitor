"""TCP health: handshake time and retransmission rate.

Ping tells you about the ICMP path; it does not tell you whether TCP actually
works across it. Two different things are measured here:

  * **connect time** - a real SYN/SYN-ACK/ACK handshake to a host:port, timed.
    Catches middleboxes, DPI and port blocking that ICMP sails straight past.
  * **retransmission rate** - ``RetransSegs`` as a share of ``OutSegs`` from
    /proc/net/snmp, sampled as a delta. This is the kernel's own view of loss,
    and it reflects every TCP connection on the Pi, not just the probe.
"""

from __future__ import annotations

import asyncio
import time

from ..util import read_tcp_snmp


class TcpProbe:
    name = "tcp"

    def __init__(self, cfg, state, storage):
        self.cfg = cfg
        self.tcfg = cfg.probes.tcp
        self.state = state
        self.storage = storage
        self.interval = self.tcfg.interval
        self._prev_snmp: dict[str, int] | None = None

        with state.lock:
            state.tcp.host = self.tcfg.host
            state.tcp.port = int(self.tcfg.port)

    # -- handshake ---------------------------------------------------------- #

    async def _handshake(self) -> tuple[float | None, str | None]:
        """Time a full TCP connect, in ms. Returns ``(ms, error)``."""
        writer = None
        t0 = time.perf_counter()
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(self.tcfg.host, int(self.tcfg.port)),
                timeout=self.tcfg.timeout,
            )
            elapsed = (time.perf_counter() - t0) * 1000.0
            return (elapsed, None)
        except asyncio.TimeoutError:
            return (None, "timeout")
        except (ConnectionRefusedError, ConnectionResetError):
            return (None, "refused")
        except OSError as exc:
            return (None, f"oserror:{exc.errno}")
        finally:
            if writer is not None:
                try:
                    writer.close()
                    await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
                except (OSError, asyncio.TimeoutError):
                    pass

    # -- kernel counters ---------------------------------------------------- #

    def _retrans_pct(self) -> float | None:
        snmp = read_tcp_snmp()
        if not snmp:
            return None
        prev, self._prev_snmp = self._prev_snmp, snmp
        if not prev:
            return None
        out = snmp.get("OutSegs", 0) - prev.get("OutSegs", 0)
        retrans = snmp.get("RetransSegs", 0) - prev.get("RetransSegs", 0)
        if out <= 0 or retrans < 0:
            return None
        return 100.0 * retrans / out

    # -- round -------------------------------------------------------------- #

    async def tick(self) -> None:
        connect_ms, err = await self._handshake()
        retrans = self._retrans_pct()
        now = time.time()

        with self.state.lock:
            st = self.state.tcp
            st.connect.add(now, connect_ms)
            st.retrans_pct = retrans
            st.attempts += 1
            st.last_error = err
            if connect_ms is None:
                st.fails += 1

        self.storage.add_tcp(now, self.tcfg.host, int(self.tcfg.port), connect_ms,
                             connect_ms is not None, retrans)
        if err:
            self.state.note_error(f"tcp {self.tcfg.host}:{self.tcfg.port}: {err}")
