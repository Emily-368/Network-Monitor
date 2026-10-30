# netmon

A real-time network monitor for a Raspberry Pi 3B+ with a 5" DSI panel.
Python, Tkinter, SQLite. No browser, no Grafana, no Docker — it has to live
inside 1 GB of RAM and leave the link it is measuring alone.

![dashboard](docs/dashboard.png)

```
NETMON  pi · eth0          ● ONLINE  Internet (1.1.1.1)        window 3m  16:43:49
┌── LATENCY ──────── OK ─┬── PACKET LOSS ── WARN ─┬── BANDWIDTH VS THROUGHPUT ─ OK ─┐
│ 18.8 ms      Internet  │ 1.67 %     rolling 60s │ 0.781 Mbps in          ↑ 0.621 │
│ avg 19.1 p95 42.7 …    │ 9 of 179 lost in 3m    │ capacity 94.2↓ 11.8↑ Mbps …    │
│ ╭─╮                    │      ▌                 │ ───╮                            │
│ ─╯ ╰──────────         │   ▌  █    ▌            │    ╰─────────                   │
│   ▮  ▮▮      ▮ (loss)  │                        │ in  ▌░░░░░░░░░ 0.8%            │
│                        │                        │ out ██░░░░░░░░ 5.3%            │
├── JITTER ──┬── TCP ────┬── ERRORS ──┬── AVAILABILITY ──┬── UPTIME ───────────────┤
│ 2.4 ms     │ 28.5 ms   │ 0 /s       │ 99.974 % 24h     │ 3d 4h                   │
├─────────────────────────────────────────────────────────────────────────────────┤
│ TARGET    HOST          NOW   AVG   P95   JIT  LOSS%    1H     24H  STATE        │
│ Gateway   192.168.1.1   3.5   1.9   3.7   0.2      0   100  99.998  OK           │
│ Internet  1.1.1.1      18.8  19.1  42.7   2.4   1.67 99.861 99.974  WARN         │
│ DNS alt   8.8.8.8      30.9  30.3  53.6   4.0      0   100  99.998  OK           │
└─────────────────────────────────────────────────────────────────────────────────┘
```

---

## Install

On the Pi:

```bash
git clone <this repo> ~/netmon && cd ~/netmon && bash scripts/install.sh
```

The installer is idempotent. It installs `python3-tk`, builds a virtualenv,
enables unprivileged ICMP, creates the history directory, adds a desktop
autostart entry, and finishes by running the self-test.

> If you copied these files from Windows, the shell scripts may have CRLF line
> endings and will fail with `bad interpreter`. Fix once with
> `sudo apt install dos2unix && dos2unix scripts/*.sh`.

Then:

```bash
python -m netmon              # dashboard (collector runs inside it)
python -m netmon --windowed   # same, in a window — useful over VNC
python -m netmon --headless   # collector only, no display
python -m netmon --selftest   # check the environment and probe once
python -m netmon --report     # availability summary from the stored history
```

On screen: **tap** anywhere (or `←`/`→`, or `1`–`4`) to change the time window,
`b` to run a capacity test now, `f` to toggle fullscreen, `q` to quit.

---

## What it measures, and why these definitions

The three real-time-sensitive metrics get the top row and the full chart width.

| Metric | How it is measured | Where it comes from |
|---|---|---|
| **Latency** | round-trip time of each ICMP echo, 1 Hz | `icmplib` |
| **Packet loss** | echoes sent − answered, over a rolling 60-round window | `icmplib` |
| **Jitter** | mean absolute difference between *consecutive* RTTs | derived |
| **Throughput** | bytes/s across the NIC, sampled at 1 Hz | `psutil` → kernel counters |
| **Bandwidth** | capacity, by filling the link — hourly | `speedtest-cli` or `iperf3` |
| **TCP** | real SYN/SYN-ACK/ACK handshake time, retransmit rate, socket states | `asyncio`, `/proc/net/snmp`, `psutil` |
| **Availability** | answered echoes ÷ sent echoes, over 1 h and 24 h | SQLite |
| **Error rate** | `errin+errout+dropin+dropout` per second, and as a share of packets | `psutil` |
| **Uptime** | how long the monitor has been running, plus outage count and last outage | derived |

Three of these are routinely conflated, so to be explicit:

**Bandwidth is not throughput.** Bandwidth is what the link *can* carry and can
only be found by saturating it — which is why it runs once an hour, not once a
second. Throughput is what is *actually* flowing, read for free from the kernel's
byte counters at 1 Hz. The tile shows both on one Mbps axis: throughput is the
line, capacity is the dashed reference, and the meter underneath gives
throughput as a share of capacity so the headroom stays readable even when the
link is nearly idle and the two numbers are three orders of magnitude apart.
Press `b` and watch the throughput line jump to the capacity line — that is the
whole distinction in one picture.

**Jitter is the mean consecutive delta, not the standard deviation.** It is
packet *delay variation* — what actually breaks a voice call. A link with a
steady 200 ms RTT has high latency and near-zero jitter.

**Loss is counted per round, not per window-average.** A burst where 2 of 5
echoes come back is 40 % loss for that round, so partial loss is visible rather
than smeared.

**Availability is not rounded to 100 %.** 99.974 % is about four outage-minutes
a day; rounding it away hides exactly the thing being watched.

---

## Configuration

Everything lives in `config.yaml`; every key has a working default and unknown
keys are reported at startup rather than ignored. The parts worth changing:

```yaml
targets:
  - { name: Gateway,  host: auto,    primary: false }   # auto = default gateway
  - { name: Internet, host: 1.1.1.1, primary: true  }   # primary drives the big tiles

probes:
  ping:      { interval: 1.0, count: 1, loss_window: 60, outage_after: 3 }
  bandwidth: { method: speedtest, interval: 3600 }      # or iperf3

thresholds:
  latency_warn_ms: 40     # amber
  latency_crit_ms: 120    # red
  loss_warn_pct: 1.0
```

Pinging the gateway *and* something external is the useful pattern: when both
go red the problem is yours, when only the external one does it is upstream.

### Capacity test: prefer iperf3

`speedtest` measures the whole path to the internet, takes ~30 s, and is the
heaviest thing this program does. If you have any always-on machine on the LAN,
run `iperf3 -s` there and use it instead — it measures your own link, finishes
in seconds, and costs almost no memory:

```yaml
probes:
  bandwidth:
    method: iperf3
    iperf3_server: "192.168.1.50"
```

Note that the Pi 3B+ NIC shares the USB 2.0 bus, so it tops out around
300 Mbps regardless of what your line can do.

---

## Running it unattended

**With the panel attached** (the normal case): the installer's autostart entry
runs the dashboard when the desktop session starts. Enable desktop autologin
first — `sudo raspi-config` → System Options → Boot / Auto Login → Desktop
Autologin. `scripts/start-dashboard.sh` disables screen blanking before launch.

**Headless**: use `systemd/netmon-collector.service`.

> Do **not** run the collector service and the dashboard at the same time. The
> dashboard collects too, so both running means every target is probed twice and
> two processes write the same SQLite file.

### Unprivileged ICMP

netmon never needs root. On Linux, a process may open an ICMP *datagram* socket
if its group is inside `net.ipv4.ping_group_range`; the installer opens that
range for everyone:

```bash
echo 'net.ipv4.ping_group_range = 0 2147483647' | sudo tee /etc/sysctl.d/99-netmon-icmp.conf
sudo sysctl -p /etc/sysctl.d/99-netmon-icmp.conf
```

If that is not available, netmon falls back to the system `ping` binary on its
own — slower, one process per round, but it works. `--selftest` says which
backend is in use.

---

## Resource use and the SD card

Measured on a Pi 3B+: roughly 60–70 MB resident for the whole thing (one Python
process, Tk, ~1800 samples per series in memory) and a few percent of one core
at the default 1 Hz. Capacity tests are the only heavy moment.

SD cards die from small frequent writes, so the history is deliberately
conservative:

* inserts are buffered and committed once every 5 s, not per sample;
* 1 Hz raw rows are folded into per-minute aggregates after 6 hours and then
  deleted, so the file reaches a steady size (a few MB) instead of growing;
* WAL journaling with `synchronous=NORMAL` — a power cut can cost the last few
  seconds of samples and nothing else.

Set `general.db_path` to a USB stick if you want to be stricter, or raise
`probes.ping.interval` to 2–5 s if you do not need per-second resolution.

---

## Layout of the code

```
netmon/
  __main__.py      CLI: dashboard / --headless / --selftest / --report
  config.py        YAML over dataclass defaults, with guard rails
  collector.py     asyncio scheduler in a background thread
  state.py         in-RAM history + the snapshot the UI reads
  storage.py       SQLite: buffered writes, rollup, purge, availability
  probes/
    ping.py        latency, loss, jitter   (icmplib, system ping fallback)
    iface.py       throughput, error rates (psutil)
    tcp.py         handshake time, retransmits, socket states
    bandwidth.py   capacity (speedtest / iperf3), in a worker thread
  ui/
    theme.py       dark palette, status levels, fonts
    widgets.py     canvas charts and tiles — no matplotlib
    app.py         the 800x480 layout
```

The collector runs in a background thread and the dashboard in the Tk main
thread; they share one `LiveState` behind a lock, and the UI only ever reads
copied snapshots. One process means one interpreter on a 1 GB board, and
`--headless` covers the no-screen case.

The charts are hand-drawn on a Tk canvas because a matplotlib redraw costs
tenths of a second and tens of megabytes on this hardware. Samples are bucketed
into one column per ~2 px and drawn as a min/max range band behind a mean line,
so a 30-minute window does not alias its spikes away. Every y axis starts at
zero.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| everything reads NO REPLY | ICMP blocked — run `--selftest`, check the sysctl above |
| `ModuleNotFoundError: tkinter` | `sudo apt install python3-tk` (and the venv needs `--system-site-packages`) |
| throughput always 0 | wrong interface — set `general.interface` to `eth0`/`wlan0` |
| capacity test always fails | `pip install speedtest-cli`, or switch to `method: iperf3` |
| TCP socket states blank | harmless; `/proc/net/tcp` was not readable |
| screen blanks after 10 min | start via `scripts/start-dashboard.sh`, which runs `xset -dpms` |
| `bad interpreter` on a script | CRLF line endings — `dos2unix scripts/*.sh` |

Panel note: netmon only needs an X session at 800x480. If your 5" DSI panel
needs a `dtoverlay` line in `/boot/firmware/config.txt`, follow the vendor's
instructions for that part — it is independent of this program.
