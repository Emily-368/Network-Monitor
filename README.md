# netmon

A real-time network monitor for a Raspberry Pi 3B with a DFRobot DFR0550-V2, a 5"
800x480 DSI touchscreen.
Python, Tkinter, SQLite. No browser, no Grafana, no Docker — it has to live
inside 1 GB of RAM and leave the link it is measuring alone.

```
raspberrypi · eth0                ● ONLINE                          3m  16:43:49
192.168.1.50                  Internet (1.1.1.1)
┌── LATENCY ──────── OK ─┬── PACKET LOSS ──── OK ─┬── THROUGHPUT ───────────── OK ─┐
│ 18.8 ms      Internet  │ 1.67 %     rolling 60s │ 0.781 Mbps in          ↑ 0.621 │
│ avg 19.1 p95 42.7 …    │ 9 of 179 lost in 3m    │ cap 94↓ 11.8↑ NIC limit · 3h   │
│ ╭─╮                    │      ▌                 │ ───╮                            │
│ ─╯ ╰──────────         │   ▌  █    ▌            │    ╰─────────                   │
│   ▮  ▮▮      ▮ (loss)  │                        │ in  ▌░░░░░░░░░ 0.8%            │
│                        │                        │ out ██░░░░░░░░ 5.3%            │
├── JITTER ─┬── TCP ───┬── DNS ───┬── LINK ───┬── AVAIL 24H ──┬── PI HEALTH ─────┤
│ 2.4 ms    │ 28.5 ms  │ 24 ms    │ 100 Mbps  │ 99.974 %      │ 52 °C  power OK  │
├── TARGETS ───────────────────────────────────────────────────────────────────────┤
│ TARGET    HOST          NOW   AVG   P95   JIT  LOSS%    1H     24H  STATE        │
│ Gateway   192.168.1.1   3.5   1.9   3.7   0.2      0   100  99.998  OK           │
│ Internet  1.1.1.1      18.8  19.1  42.7   2.4   1.67 99.861 99.974  WARN         │
│ Google    8.8.8.8      30.9  30.3  53.6   4.0      0   100  99.998  OK           │
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

On the panel: **tap the header** for a menu (run a capacity test, exit), and **tap
anywhere else** to change the time window. With a keyboard or over VNC: `←`/`→` or
`1`–`4` for the window, `b` for a capacity test, `f` for fullscreen, `q` to quit.

## The header

The top left shows the Pi's name, interface and IP address, so you can read off
what to `ssh` to. The centre is one plain-English verdict. It walks outward from
the Pi and reports the first thing broken, since everything beyond it is only
broken as a consequence:

| Verdict | Meaning | Look at |
|---|---|---|
| `LINK DOWN` | no carrier on the interface | cable, Wi-Fi |
| `NO IP ADDRESS` | no DHCP lease | router, DHCP |
| `NO ROUTE` | an address but no default gateway | network config |
| `LAN FAULT` | the router does not answer | your network |
| `ISP / UPSTREAM FAULT` | the router answers, nothing past it does | your provider |
| `DNS FAULT` | pings work, names do not resolve | DNS settings |
| `TCP FAULT` | pings work, TCP connections do not | firewall, filtering |

---

## What it measures, and why these definitions

The three real-time-sensitive metrics get the top row and the full chart width.

| Metric | How it is measured | Where it comes from |
|---|---|---|
| **Latency** | round-trip time of each ICMP echo, 1 Hz | `icmplib` |
| **Packet loss** | echoes sent − answered, over a rolling 60-round window | `icmplib` |
| **Jitter** | mean absolute difference between *consecutive* RTTs | derived |
| **Throughput** | bytes/s across the Pi's own NIC, sampled at 1 Hz | `psutil` → kernel counters |
| **Bandwidth** | capacity, by filling the link — daily or on demand | `speedtest-cli` or `iperf3` |
| **TCP** | real SYN/SYN-ACK/ACK handshake time, retransmit rate | `asyncio`, `/proc/net/snmp` |
| **DNS** | a timed lookup through the system resolver, every 15 s | `getaddrinfo` |
| **Link** | carrier, negotiated speed, Wi-Fi signal | sysfs, `/proc/net/wireless`, `psutil` |
| **Availability** | answered echoes ÷ sent echoes, over 1 h and 24 h | SQLite |
| **Error rate** | `errin+errout+dropin+dropout` per second, and as a share of packets | `psutil` |
| **Outages** | count over 24 h and the last one's length; survives restarts | SQLite |
| **Pi health** | SoC temperature, under-voltage and throttling flags, CPU, uptime | sysfs, `vcgencmd`, `psutil` |

Three of these are routinely conflated, so to be explicit:

**Throughput is the Pi's own traffic.** The Pi is an endpoint on your network, not
a device everything passes through, so other devices' traffic never reaches these
counters. A household streaming 4K still reads near zero here. Seeing whole-network
traffic would need a mirror port on a managed switch, or the router's own counters.

**Bandwidth is not throughput.** Bandwidth is what the link *can* carry and can
only be found by saturating it — which is why it runs daily, not once a second.
Throughput is what is *actually* flowing, read for free from the kernel's byte
counters at 1 Hz. The tile shows both on one Mbps axis: throughput is the
line, capacity is the dashed reference, and the meter underneath gives
throughput as a share of capacity so the headroom stays readable even when the
link is nearly idle and the two numbers are three orders of magnitude apart.
Press `b` and watch the throughput line jump to the capacity line — that is the
whole distinction in one picture. The charts shade the period of each test, so the
latency spike it causes is not mistaken for a network problem.

**Jitter is the mean consecutive delta, not the standard deviation.** It is
packet *delay variation* — what actually breaks a voice call. A link with a
steady 200 ms RTT has high latency and near-zero jitter.

**Loss is counted per round, not per window-average.** A burst where 2 of 5
echoes come back is 40 % loss for that round, so partial loss is visible rather
than smeared.

**One lost echo is not an alarm.** At one echo a second over a 60-second window,
a single lost echo is 1.67 %, so the warning starts at 2 % (two lost echoes).

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
  bandwidth: { method: speedtest, interval: 86400 }     # or iperf3

thresholds:
  latency_warn_ms: 40     # amber
  latency_crit_ms: 120    # red
  loss_warn_pct: 2.0
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

### What a Pi 3B can measure

The Pi 3B's Ethernet is 100 Mbps (about 94 Mbps in practice) and shares the USB 2.0
bus; its Wi-Fi is 2.4 GHz only and usually slower still. A capacity test reports the
slower of your line and the Pi, so on a faster plan it reads about 94 Mbps however
fast the line is, and the tile marks that result `NIC limit`. Latency, loss, jitter,
DNS and availability are not affected. A Pi 4 has true gigabit Ethernet and runs
netmon unchanged.

---

## Running it unattended

**With the panel attached** (the normal case): the installer's autostart entry
runs the dashboard when the desktop session starts. Enable desktop autologin
first — `sudo raspi-config` → System Options → Boot / Auto Login → Desktop
Autologin. The installer turns screen blanking off through `raspi-config`, and
`scripts/start-dashboard.sh` also disables it under X11. It can start before the
network is up: the interface and gateway follow the default route once there is one.

**Headless**: `bash scripts/install.sh --headless-service` installs
`systemd/netmon-collector.service` for the current user and enables it.

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

Measured on a Pi 3B+, the 3B's close sibling: roughly 60–70 MB resident for the whole thing (one Python
process, Tk, ~1800 samples per series in memory) and a few percent of one core
at the default 1 Hz. Capacity tests are the only heavy moment.

SD cards die from small frequent writes, so the history is deliberately
conservative:

* inserts are buffered and committed once every 5 s, not per sample;
* 1 Hz raw rows are folded into per-minute aggregates and deleted after 6 hours,
  so the file reaches a steady size (a few MB) instead of growing; each rollup
  reads only the minutes that are new, in a worker thread, so it neither rewrites
  old rows nor delays the probes;
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
  verdict.py       the header's plain-English diagnosis
  collector.py     asyncio scheduler in a background thread
  state.py         in-RAM history + the snapshot the UI reads
  storage.py       SQLite: buffered writes, rollup, purge, availability
  probes/
    ping.py        latency, loss, jitter   (icmplib, system ping fallback)
    iface.py       throughput, errors, address, link state, Wi-Fi signal
    tcp.py         handshake time, retransmits
    dns.py         timed lookup through the system resolver
    system.py      Pi temperature, under-voltage, throttling, CPU
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
| `UNDER-VOLTAGE` or `power dipped` in the Pi health tile | use a better power supply; it can cause network errors too |
| `power unknown` in the Pi health tile | `vcgencmd` missing or not permitted — add the user to the `video` group |
| screen blanks after 10 min | `sudo raspi-config nonint do_blanking 1`, then reboot |
| `bad interpreter` on a script | CRLF line endings — `dos2unix scripts/*.sh` |

---

## The display

The DFRobot DFR0550-V2 is a 5" 800x480 IPS panel with 5-point capacitive touch. It
connects with the supplied ribbon cable to the Pi's DISPLAY (DSI) port and draws its
power from the Pi. Set the backlight with the small dial on the back of the panel.

netmon only needs a desktop session at 800x480. `python -m netmon --selftest` lists
the framebuffers it can see and the session type.

| Symptom | Cause |
|---|---|
| panel stays blank | check the ribbon at both ends; DFRobot publishes no `config.txt` lines for the V2 |
| touch weak or not responding | check the power supply, and that the resolution is 800x480; DFRobot's FAQ also suggests trying an older OS |
| `screen is ...x... but the layout needs` in the log | the desktop is not running at 800x480: check the display settings |
