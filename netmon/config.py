"""Typed configuration, loaded from YAML over dataclass defaults.

Unknown keys are reported rather than ignored silently - a typo in a threshold
is otherwise invisible until you wonder why nothing ever turns amber.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_CONFIG_NAMES = ("config.yaml", "config.yml")


@dataclass
class TargetCfg:
    name: str = "target"
    host: str = "auto"      # "auto" -> default gateway
    primary: bool = False


@dataclass
class PingCfg:
    interval: float = 1.0
    count: int = 1
    echo_interval: float = 0.2
    timeout: float = 1.0
    loss_window: int = 60
    outage_after: int = 3
    backend: str = "auto"       # auto | icmplib | system
    privileged: bool = False


@dataclass
class IfaceCfg:
    interval: float = 1.0


@dataclass
class TcpCfg:
    interval: float = 10.0
    host: str = "1.1.1.1"
    port: int = 443
    timeout: float = 3.0


@dataclass
class DnsCfg:
    enabled: bool = True
    interval: float = 15.0
    host: str = "cloudflare.com"
    timeout: float = 3.0


@dataclass
class SystemCfg:
    interval: float = 10.0


@dataclass
class BandwidthCfg:
    enabled: bool = True
    method: str = "speedtest"   # speedtest | iperf3 | none
    interval: float = 86400.0
    run_at_start: bool = False
    threads: int = 2
    iperf3_server: str = ""
    iperf3_port: int = 5201
    iperf3_duration: int = 5


@dataclass
class ProbesCfg:
    ping: PingCfg = field(default_factory=PingCfg)
    iface: IfaceCfg = field(default_factory=IfaceCfg)
    tcp: TcpCfg = field(default_factory=TcpCfg)
    dns: DnsCfg = field(default_factory=DnsCfg)
    system: SystemCfg = field(default_factory=SystemCfg)
    bandwidth: BandwidthCfg = field(default_factory=BandwidthCfg)


@dataclass
class GeneralCfg:
    interface: str = "auto"
    db_path: str = "~/.local/share/netmon/netmon.db"
    raw_retention_hours: float = 6.0
    retention_days: float = 30.0
    history_points: int = 1800
    log_level: str = "INFO"


@dataclass
class UiCfg:
    fullscreen: bool = True
    width: int = 800
    height: int = 480
    refresh_ms: int = 1000
    hide_cursor: bool = True
    window_seconds: int = 180
    windows: list = field(default_factory=lambda: [60, 180, 600, 1800])
    link_capacity_mbps: float = 100.0


@dataclass
class Thresholds:
    latency_warn_ms: float = 40.0
    latency_crit_ms: float = 120.0
    jitter_warn_ms: float = 10.0
    jitter_crit_ms: float = 30.0
    loss_warn_pct: float = 2.0
    loss_crit_pct: float = 5.0
    tcp_warn_ms: float = 150.0
    tcp_crit_ms: float = 500.0
    err_warn_rate: float = 1.0
    err_crit_rate: float = 10.0
    util_warn_pct: float = 70.0
    util_crit_pct: float = 90.0
    avail_warn_pct: float = 99.5
    avail_crit_pct: float = 99.0
    signal_warn_dbm: float = -70.0
    signal_crit_dbm: float = -80.0
    temp_warn_c: float = 70.0
    temp_crit_c: float = 80.0


@dataclass
class Config:
    general: GeneralCfg = field(default_factory=GeneralCfg)
    targets: list = field(default_factory=lambda: [
        TargetCfg(name="Gateway", host="auto", primary=False),
        TargetCfg(name="Internet", host="1.1.1.1", primary=True),
    ])
    probes: ProbesCfg = field(default_factory=ProbesCfg)
    ui: UiCfg = field(default_factory=UiCfg)
    thresholds: Thresholds = field(default_factory=Thresholds)
    source_path: str = ""

    # -- derived helpers ---------------------------------------------------- #

    @property
    def primary_target(self) -> TargetCfg:
        for t in self.targets:
            if t.primary:
                return t
        return self.targets[0]

    def db_file(self) -> Path:
        return Path(os.path.expanduser(self.general.db_path)).resolve()


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


def _section(cls, data: Any, path: str):
    """Build a dataclass from a mapping, warning about keys it does not know."""
    if data is None:
        return cls()
    if not isinstance(data, dict):
        log.warning("config: %s should be a mapping, got %s - using defaults",
                    path.rstrip("."), type(data).__name__)
        return cls()

    known = {f.name for f in fields(cls)}
    kwargs = {}
    for key, value in data.items():
        if key in known:
            kwargs[key] = value
        else:
            log.warning("config: ignoring unknown key %s%s", path, key)
    try:
        return cls(**kwargs)
    except TypeError as exc:
        log.warning("config: %s - using defaults (%s)", path.rstrip("."), exc)
        return cls()


def _targets(data: Any) -> list:
    if not data:
        return Config().targets
    out = []
    for i, item in enumerate(data):
        if isinstance(item, str):
            out.append(TargetCfg(name=item, host=item))
        else:
            out.append(_section(TargetCfg, item, f"targets[{i}]."))
    if not out:
        return Config().targets
    if not any(t.primary for t in out):
        out[-1].primary = True
    else:
        # Exactly one primary; later ones win the flag, earlier ones lose it.
        seen = False
        for t in reversed(out):
            if t.primary and seen:
                t.primary = False
            elif t.primary:
                seen = True
    return out


def find_config(explicit: str | None = None) -> Path | None:
    """Locate a config file: explicit path, then ./, then ~/.config/netmon/."""
    if explicit:
        p = Path(os.path.expanduser(explicit))
        if not p.is_file():
            raise FileNotFoundError(f"config file not found: {p}")
        return p

    here = Path(__file__).resolve().parent.parent
    candidates = []
    for name in DEFAULT_CONFIG_NAMES:
        candidates += [
            Path.cwd() / name,
            here / name,
            Path(os.path.expanduser("~/.config/netmon")) / name,
        ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def load_config(explicit: str | None = None) -> Config:
    """Load configuration, falling back to built-in defaults when no file exists."""
    path = find_config(explicit)
    if path is None:
        log.info("no config file found - using built-in defaults")
        return Config()

    try:
        import yaml
    except ImportError:
        log.warning("PyYAML is not installed - ignoring %s and using defaults", path)
        return Config()

    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        log.warning("%s is not a YAML mapping - using defaults", path)
        return Config()

    for key in raw:
        if key not in {"general", "targets", "probes", "ui", "thresholds"}:
            log.warning("config: ignoring unknown top-level section %r", key)

    probes_raw = raw.get("probes") or {}
    probes = ProbesCfg(
        ping=_section(PingCfg, probes_raw.get("ping"), "probes.ping."),
        iface=_section(IfaceCfg, probes_raw.get("iface"), "probes.iface."),
        tcp=_section(TcpCfg, probes_raw.get("tcp"), "probes.tcp."),
        dns=_section(DnsCfg, probes_raw.get("dns"), "probes.dns."),
        system=_section(SystemCfg, probes_raw.get("system"), "probes.system."),
        bandwidth=_section(BandwidthCfg, probes_raw.get("bandwidth"), "probes.bandwidth."),
    )
    for key in probes_raw:
        if key not in {"ping", "iface", "tcp", "dns", "system", "bandwidth"}:
            log.warning("config: ignoring unknown probe %r", key)

    cfg = Config(
        general=_section(GeneralCfg, raw.get("general"), "general."),
        targets=_targets(raw.get("targets")),
        probes=probes,
        ui=_section(UiCfg, raw.get("ui"), "ui."),
        thresholds=_section(Thresholds, raw.get("thresholds"), "thresholds."),
        source_path=str(path),
    )

    # Guard rails: a 0.05 s ping interval would melt a Pi 3B, and raw retention
    # shorter than the rollup cadence would lose samples before they are folded.
    cfg.probes.ping.interval = max(0.25, float(cfg.probes.ping.interval))
    cfg.probes.iface.interval = max(0.25, float(cfg.probes.iface.interval))
    cfg.probes.tcp.interval = max(1.0, float(cfg.probes.tcp.interval))
    cfg.probes.dns.interval = max(5.0, float(cfg.probes.dns.interval))
    cfg.probes.system.interval = max(1.0, float(cfg.probes.system.interval))
    cfg.probes.bandwidth.interval = max(60.0, float(cfg.probes.bandwidth.interval))
    cfg.general.raw_retention_hours = max(0.5, float(cfg.general.raw_retention_hours))
    cfg.general.history_points = max(60, int(cfg.general.history_points))
    cfg.ui.refresh_ms = max(100, int(cfg.ui.refresh_ms))
    if not cfg.ui.windows:
        cfg.ui.windows = [60, 180, 600, 1800]
    cfg.ui.windows = sorted({int(w) for w in cfg.ui.windows if int(w) > 0})
    if cfg.ui.window_seconds not in cfg.ui.windows:
        cfg.ui.window_seconds = cfg.ui.windows[0]

    return cfg
