"""SQLite history.

SD cards die from small frequent writes, so this module does three things to
keep the write volume down:

  * inserts are buffered in RAM and committed in one transaction every few
    seconds (``flush``),
  * 1 Hz raw rows are folded into per-minute aggregates (``rollup``) and then
    deleted (``purge``), so the file reaches a steady size - and each rollup
    touches only the minutes that are new, never the whole raw table, and
  * WAL + ``synchronous=NORMAL``, which is the usual trade for a monitor: a
    power cut can cost the last few seconds of samples, nothing more.

Availability is answered from the per-minute table plus whatever raw rows are
newer than the last rollup, so the two never double-count.
"""

from __future__ import annotations

import functools
import logging
import re
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS ping_raw (
           ts        REAL    NOT NULL,
           target    TEXT    NOT NULL,
           rtt_ms    REAL,
           sent      INTEGER NOT NULL,
           lost      INTEGER NOT NULL,
           jitter_ms REAL)""",
    "CREATE INDEX IF NOT EXISTS ix_ping_raw ON ping_raw(target, ts)",
    "CREATE INDEX IF NOT EXISTS ix_ping_raw_ts ON ping_raw(ts)",
    """CREATE TABLE IF NOT EXISTS ping_minute (
           minute     INTEGER NOT NULL,
           target     TEXT    NOT NULL,
           rounds     INTEGER,
           sent       INTEGER,
           lost       INTEGER,
           rtt_avg    REAL,
           rtt_min    REAL,
           rtt_max    REAL,
           jitter_avg REAL,
           PRIMARY KEY (minute, target))""",
    """CREATE TABLE IF NOT EXISTS iface_raw (
           ts      REAL NOT NULL,
           iface   TEXT NOT NULL,
           rx_mbps REAL,
           tx_mbps REAL,
           err_in  INTEGER,
           err_out INTEGER,
           drop_in INTEGER,
           drop_out INTEGER,
           pkt_in  INTEGER,
           pkt_out INTEGER)""",
    # Rollup and purge select on time alone; nothing queries by interface.
    "DROP INDEX IF EXISTS ix_iface_raw",
    "CREATE INDEX IF NOT EXISTS ix_iface_raw_ts ON iface_raw(ts)",
    """CREATE TABLE IF NOT EXISTS iface_minute (
           minute  INTEGER NOT NULL,
           iface   TEXT    NOT NULL,
           rx_avg  REAL,
           rx_max  REAL,
           tx_avg  REAL,
           tx_max  REAL,
           err_in  INTEGER,
           err_out INTEGER,
           drop_in INTEGER,
           drop_out INTEGER,
           pkt_in  INTEGER,
           pkt_out INTEGER,
           PRIMARY KEY (minute, iface))""",
    """CREATE TABLE IF NOT EXISTS tcp_sample (
           ts          REAL NOT NULL,
           host        TEXT,
           port        INTEGER,
           connect_ms  REAL,
           ok          INTEGER,
           retrans_pct REAL,
           established INTEGER)""",
    "CREATE INDEX IF NOT EXISTS ix_tcp_sample ON tcp_sample(ts)",
    """CREATE TABLE IF NOT EXISTS bandwidth_sample (
           ts         REAL NOT NULL,
           method     TEXT,
           down_mbps  REAL,
           up_mbps    REAL,
           latency_ms REAL,
           server     TEXT,
           ok         INTEGER,
           note       TEXT)""",
    "CREATE INDEX IF NOT EXISTS ix_bandwidth_sample ON bandwidth_sample(ts)",
    """CREATE TABLE IF NOT EXISTS event (
           ts     REAL NOT NULL,
           kind   TEXT,
           target TEXT,
           detail TEXT)""",
    "CREATE INDEX IF NOT EXISTS ix_event ON event(ts)",
)

BANDWIDTH_RETENTION_DAYS = 365
# Rollup re-reads this many minutes behind its mark: a row stamped just before a
# minute boundary can still be sitting in the write buffer when that minute closes.
ROLLUP_OVERLAP_MIN = 2


def _locked(fn):
    """Serialise access to the connection. The lock is re-entrant, so methods
    that call each other (close -> flush) do not deadlock."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)
    return wrapper


class Storage:
    """Buffered SQLite writer.

    Probes append from the collector's event loop; flush, rollup and queries run
    in a worker thread. Those are slow on an SD card, so the buffers have their
    own short-held lock and appending never waits for the database.
    """

    def __init__(self, path: Path | str, raw_retention_hours: float = 6.0,
                 retention_days: float = 30.0):
        self.path = Path(path)
        self.raw_retention_s = max(1800.0, raw_retention_hours * 3600.0)
        self.retention_s = max(3600.0, retention_days * 86400.0)
        self._conn: sqlite3.Connection | None = None
        # One connection, shared between threads: the lock plus
        # check_same_thread=False serialises every use of it.
        self._lock = threading.RLock()
        self._buf_lock = threading.Lock()
        self._rolled_min: int | None = None     # newest minute already rolled up
        self._ping: list[tuple] = []
        self._iface: list[tuple] = []
        self._tcp: list[tuple] = []
        self._bw: list[tuple] = []
        self._events: list[tuple] = []
        self.enabled = True
        self.last_error: str | None = None

    # -- lifecycle ---------------------------------------------------------- #

    @_locked
    def open(self) -> bool:
        """Create the database and schema. Returns False if history is unavailable."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), timeout=10.0,
                                   isolation_level=None, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            for stmt in SCHEMA:
                conn.execute(stmt)
            self._conn = conn
            log.info("history database at %s", self.path)
            return True
        except (sqlite3.Error, OSError) as exc:
            self.enabled = False
            self.last_error = str(exc)
            log.error("cannot open %s (%s) - running without history", self.path, exc)
            return False

    @_locked
    def close(self) -> None:
        if self._conn is None:
            return
        try:
            self.flush()
            self._conn.close()
        except sqlite3.Error as exc:
            log.warning("error closing database: %s", exc)
        finally:
            self._conn = None

    # -- buffered writes ---------------------------------------------------- #

    def add_ping(self, ts, target, rtt_ms, sent, lost, jitter_ms) -> None:
        with self._buf_lock:
            self._ping.append((ts, target, rtt_ms, sent, lost, jitter_ms))

    def add_iface(self, ts, iface, rx, tx, err_in, err_out, drop_in, drop_out,
                  pkt_in, pkt_out) -> None:
        with self._buf_lock:
            self._iface.append((ts, iface, rx, tx, err_in, err_out, drop_in,
                                drop_out, pkt_in, pkt_out))

    def add_tcp(self, ts, host, port, connect_ms, ok, retrans_pct) -> None:
        with self._buf_lock:
            # NULL fills the retired "established" column in existing databases.
            self._tcp.append((ts, host, port, connect_ms, int(ok), retrans_pct, None))

    def add_bandwidth(self, ts, method, down, up, latency, server, ok, note) -> None:
        with self._buf_lock:
            self._bw.append((ts, method, down, up, latency, server, int(ok), note))

    def add_event(self, ts, kind, target, detail) -> None:
        with self._buf_lock:
            self._events.append((ts, kind, target, detail))

    @_locked
    def flush(self) -> None:
        """Commit everything buffered in one transaction."""
        with self._buf_lock:
            ping, self._ping = self._ping, []
            iface, self._iface = self._iface, []
            tcp, self._tcp = self._tcp, []
            bw, self._bw = self._bw, []
            events, self._events = self._events, []
        if self._conn is None or not any((ping, iface, tcp, bw, events)):
            return
        try:
            with self._conn:
                self._conn.execute("BEGIN")
                if ping:
                    self._conn.executemany(
                        "INSERT INTO ping_raw VALUES (?,?,?,?,?,?)", ping)
                if iface:
                    self._conn.executemany(
                        "INSERT INTO iface_raw VALUES (?,?,?,?,?,?,?,?,?,?)", iface)
                if tcp:
                    self._conn.executemany(
                        "INSERT INTO tcp_sample VALUES (?,?,?,?,?,?,?)", tcp)
                if bw:
                    self._conn.executemany(
                        "INSERT INTO bandwidth_sample VALUES (?,?,?,?,?,?,?,?)", bw)
                if events:
                    self._conn.executemany(
                        "INSERT INTO event VALUES (?,?,?,?)", events)
        except sqlite3.Error as exc:
            self.last_error = str(exc)
            log.warning("flush failed, dropping %d buffered rows: %s",
                        len(ping) + len(iface), exc)

    # -- maintenance -------------------------------------------------------- #

    @_locked
    def rollup(self, now: float | None = None) -> None:
        """Fold newly completed minutes of raw samples into the per-minute tables.

        Only minutes past the last rollup (less a small overlap) are read, so the
        cost stays flat however much raw data is being retained. Re-aggregating a
        minute that is already present just rewrites it, so the overlap is safe.
        """
        if self._conn is None:
            return
        now = now or time.time()
        cur_min = int(now // 60)
        try:
            if self._rolled_min is None:
                # Whichever table is further behind decides where to resume.
                self._rolled_min = min(self._conn.execute(
                    f"SELECT COALESCE(MAX(minute), -1) FROM {table}").fetchone()[0]
                    for table in ("ping_minute", "iface_minute"))
            bounds = ((self._rolled_min - ROLLUP_OVERLAP_MIN) * 60.0, cur_min * 60.0)
            with self._conn:
                self._conn.execute("BEGIN")
                self._conn.execute(
                    """INSERT OR REPLACE INTO ping_minute
                         SELECT CAST(ts / 60 AS INTEGER) AS minute, target,
                                COUNT(*), SUM(sent), SUM(lost),
                                AVG(rtt_ms), MIN(rtt_ms), MAX(rtt_ms), AVG(jitter_ms)
                           FROM ping_raw
                          WHERE ts >= ? AND ts < ?
                       GROUP BY minute, target""", bounds)
                self._conn.execute(
                    """INSERT OR REPLACE INTO iface_minute
                         SELECT CAST(ts / 60 AS INTEGER) AS minute, iface,
                                AVG(rx_mbps), MAX(rx_mbps), AVG(tx_mbps), MAX(tx_mbps),
                                SUM(err_in), SUM(err_out), SUM(drop_in), SUM(drop_out),
                                SUM(pkt_in), SUM(pkt_out)
                           FROM iface_raw
                          WHERE ts >= ? AND ts < ?
                       GROUP BY minute, iface""", bounds)
            self._rolled_min = cur_min - 1
        except sqlite3.Error as exc:
            self.last_error = str(exc)
            log.warning("rollup failed: %s", exc)

    @_locked
    def purge(self, now: float | None = None) -> None:
        """Delete raw samples already rolled up, and expired aggregates."""
        if self._conn is None:
            return
        now = now or time.time()
        raw_cut = now - self.raw_retention_s
        min_cut = int((now - self.retention_s) // 60)
        bw_cut = now - BANDWIDTH_RETENTION_DAYS * 86400
        try:
            with self._conn:
                self._conn.execute("BEGIN")
                # Only delete raw rows whose minute made it into the rollup.
                rolled = self._conn.execute(
                    "SELECT COALESCE(MAX(minute), -1) FROM ping_minute").fetchone()[0]
                self._conn.execute(
                    "DELETE FROM ping_raw WHERE ts < ? AND CAST(ts / 60 AS INTEGER) <= ?",
                    (raw_cut, rolled))
                rolled_if = self._conn.execute(
                    "SELECT COALESCE(MAX(minute), -1) FROM iface_minute").fetchone()[0]
                self._conn.execute(
                    "DELETE FROM iface_raw WHERE ts < ? AND CAST(ts / 60 AS INTEGER) <= ?",
                    (raw_cut, rolled_if))
                self._conn.execute("DELETE FROM ping_minute WHERE minute < ?", (min_cut,))
                self._conn.execute("DELETE FROM iface_minute WHERE minute < ?", (min_cut,))
                self._conn.execute("DELETE FROM tcp_sample WHERE ts < ?",
                                   (now - self.retention_s,))
                self._conn.execute("DELETE FROM event WHERE ts < ?",
                                   (now - self.retention_s,))
                self._conn.execute("DELETE FROM bandwidth_sample WHERE ts < ?", (bw_cut,))
        except sqlite3.Error as exc:
            self.last_error = str(exc)
            log.warning("purge failed: %s", exc)

    # -- queries ------------------------------------------------------------ #

    @_locked
    def availability(self, target: str, window_s: float,
                     now: float | None = None) -> tuple[float | None, int]:
        """Share of echoes answered over ``window_s``.

        Returns ``(percent_or_None, echoes_considered)``.
        """
        if self._conn is None:
            return (None, 0)
        now = now or time.time()
        since = now - window_s
        try:
            rolled = self._conn.execute(
                "SELECT COALESCE(MAX(minute), -1) FROM ping_minute WHERE target = ?",
                (target,)).fetchone()[0]
            sent = lost = 0

            row = self._conn.execute(
                """SELECT COALESCE(SUM(sent), 0), COALESCE(SUM(lost), 0)
                     FROM ping_minute WHERE target = ? AND minute >= ?""",
                (target, int(since // 60))).fetchone()
            sent += row[0]
            lost += row[1]

            # Raw rows newer than the newest rolled-up minute, so nothing is counted twice.
            raw_from = max(since, (rolled + 1) * 60.0)
            row = self._conn.execute(
                """SELECT COALESCE(SUM(sent), 0), COALESCE(SUM(lost), 0)
                     FROM ping_raw WHERE target = ? AND ts >= ?""",
                (target, raw_from)).fetchone()
            sent += row[0]
            lost += row[1]
        except sqlite3.Error as exc:
            self.last_error = str(exc)
            return (None, 0)

        if sent <= 0:
            return (None, 0)
        return (100.0 * (sent - lost) / sent, int(sent))

    @_locked
    def last_bandwidth(self) -> dict | None:
        """Most recent successful capacity measurement."""
        if self._conn is None:
            return None
        try:
            row = self._conn.execute(
                """SELECT ts, method, down_mbps, up_mbps, latency_ms, server, note
                     FROM bandwidth_sample WHERE ok = 1
                 ORDER BY ts DESC LIMIT 1""").fetchone()
        except sqlite3.Error:
            return None
        if not row:
            return None
        return {"ts": row[0], "method": row[1], "down_mbps": row[2], "up_mbps": row[3],
                "latency_ms": row[4], "server": row[5], "note": row[6]}

    @_locked
    def recent_events(self, limit: int = 20) -> list[dict]:
        if self._conn is None:
            return []
        try:
            rows = self._conn.execute(
                "SELECT ts, kind, target, detail FROM event ORDER BY ts DESC LIMIT ?",
                (limit,)).fetchall()
        except sqlite3.Error:
            return []
        return [{"ts": r[0], "kind": r[1], "target": r[2], "detail": r[3]} for r in rows]

    @_locked
    def outage_count(self, target: str, window_s: float,
                     now: float | None = None) -> int | None:
        """How many outages began on ``target`` within ``window_s``."""
        if self._conn is None:
            return None
        since = (now or time.time()) - window_s
        try:
            return self._conn.execute(
                "SELECT COUNT(*) FROM event WHERE kind = 'down' AND target = ? AND ts >= ?",
                (target, since)).fetchone()[0]
        except sqlite3.Error:
            return None

    @_locked
    def last_outage_seconds(self, target: str) -> float | None:
        """Duration of the most recent outage on ``target`` that has ended."""
        if self._conn is None:
            return None
        try:
            row = self._conn.execute(
                """SELECT detail FROM event WHERE kind = 'up' AND target = ?
                 ORDER BY ts DESC LIMIT 1""", (target,)).fetchone()
        except sqlite3.Error:
            return None
        match = re.search(r"after (\d+)s", row[0]) if row and row[0] else None
        return float(match.group(1)) if match else None

    def db_size_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                total += (self.path.parent / (self.path.name + suffix)).stat().st_size
            except OSError:
                pass
        return total
