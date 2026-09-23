# PI3B_HEADED_OS_ASSESSMENT.md — can this stack share a Pi 3B with a desktop?

**Question.** Is the current server + GUI efficient enough that the eVOLVER's Raspberry Pi can
also run a headed OS (minimal desktop + terminal + browser), so the instrument has a local
console as well as a remote one?

**Verdict: not yet — but the gap is small, and none of it is architectural.**
The control server is *not* the constraint: it is ~110 MB and under 1 % of one core of four.
Chromium is roughly three times the size of the thing it exists to display, and that is fine on
905 MB — with maybe 150–350 MB to spare. What blocks the plan is a short list of specific
defects, one of which makes the local browser unable to load the GUI at all, and one of which is
a property of the *desktop install itself* rather than of this code. Call it one to two days.

Assessed against `b5e4619` / `pilot-v0.1.5`, the deployed tag.
Target: **Raspberry Pi 3 Model B Rev 1.2** — quad Cortex-A53 @ 1.2 GHz, aarch64, 905 MB usable,
904 MB swap, 256 MB CMA, Debian 13 trixie, kernel 6.12.75, Python 3.13.5. Root filesystem
6.9 GB with **3.1 GB free**.

> **On the numbers.** Everything marked *measured* was measured against a synthetic 7-day
> 16-vial run (60 480 rows/vial, 95 MB on disk) on x86-64 (Xeon @ 2.1 GHz, CPython 3.11). Pi
> figures are **estimates at ×7** (plausible range ×5–10) for CPython scalar work on a
> Cortex-A53. That factor is a guess until you run `server/bench_growth_rate.py` **on the Pi** —
> it prints a scalar-throughput calibration for exactly this purpose. The repo's own SPEC §17
> gives ×30–60, but that figure is for the Pi 1's ARM1176 and does not apply to this board.

---

## 1. The finding that decides it

**The GUI cannot load without internet access to a public CDN.**

`frontend/templates/index.html:2444` is the only external URL in the entire front end:

```html
<script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
```

There is no vendored copy in `frontend/static/js/` (only uPlot is local), no fallback, and no
service worker. Line 6008 is a bare `const socket = io({...})` with no `typeof io` guard, so a
failed CDN load throws a `ReferenceError` that aborts the rest of the inline script — the
`socket.on` handlers at 6009–6027 are never registered.

Verified empirically: with `cdn.socket.io` unreachable, the page renders its static shell and
sits on **"Connecting…" forever**. No sensor data, no alerts, no experiment status. With the
file served locally instead, it connects immediately (`socket.connected === true`).

This already makes the instrument dependent on Yale's network for a machine sitting next to it,
and it is fatal to the headed plan: a browser on the Pi itself, on a lab bench, is precisely the
case where you cannot assume egress.

**Fix:** vendor the file. One line, ~50 KB, no behaviour change.

```bash
curl -o frontend/static/js/socket.io.min.js https://cdn.socket.io/4.7.5/socket.io.min.js
sed -i 's#https://cdn.socket.io/4.7.5/socket.io.min.js#/static/js/socket.io.min.js#' \
    frontend/templates/index.html
```

Add a `typeof io === "undefined"` guard around line 6008 while you are there, so a future
loading failure degrades to a visible error rather than a dead page.

---

## 2. The desktop install can silently kill the RS485 link

This is the one risk that has nothing to do with the quality of this code, and the one most
likely to be discovered the expensive way — mid-run, as intermittent sensor faults.

`/boot/firmware/config.txt` carries `dtoverlay=disable-bt` and `enable_uart=1`, and `DEPLOY.md`
records that pairing as part of the install. On a Pi 3B the SoC has two UARTs, and which one
lands on GPIO 14/15 depends entirely on that overlay:

| config | `/dev/ttyAMA0` is… | GPIO 14/15 is… | Failure mode if RS485 ends up here |
|---|---|---|---|
| `disable-bt` **(current, correct)** | PL011, on the header | PL011 | — works |
| Bluetooth re-enabled | **the Bluetooth modem** | mini-UART (`ttyS0`) | Total, clean: no responses at all |
| `miniuart-bt`, or app pointed at `ttyS0` | PL011 | PL011 / mini-UART | **Intermittent under load** |

The third row is the nasty one. The mini-UART has no independent clock — its baud rate is
derived from the VPU core clock. When the core clock moves (and under a desktop it moves
constantly: DVFS, GPU load, thermal governor), the effective baud drifts and framing errors
appear *only when the machine is busy*. That is the hardest possible fault to attribute, and it
would look exactly like a flaky RS485 transceiver.

There is a second interaction specific to going headed: on a Pi 3, `enable_uart=1` while the
mini-UART is in play pins `core_freq=250`, which caps GPU performance — so a "fix" that moves
serial to `ttyS0` also degrades the desktop you installed the fix for.

**What to do.** Treat `config.txt` as instrument configuration, not OS configuration:

```bash
sudo cp /boot/firmware/config.txt /boot/firmware/config.txt.evolver-known-good
# after ANY desktop/BT package install, before starting a run:
diff /boot/firmware/config.txt.evolver-known-good /boot/firmware/config.txt
ls -l /dev/serial* /dev/ttyAMA0 /dev/ttyS0     # confirm what ttyAMA0 actually resolves to
systemctl status hciuart bluetooth             # must not have come back
```

Worth adding to `DEPLOY.md` as a post-install gate, and worth a startup assertion in the server
— it already knows its port, and it could refuse to start (or shout) if `/dev/ttyAMA0` is not
the PL011. That check is cheap and would convert a week of confusing sensor faults into one
clear log line.

---

## 3. What the server actually costs

Better than the docs claim, and the Pi-awareness in the code is real rather than aspirational.

| Measured (x86) | Value | Pi 3B estimate (×7) |
|---|---|---|
| Idle RSS, no experiment, no clients | 65 MB | ~70 MB |
| **Steady-state RSS, 16 vials, 7-day run, 1 client** | **102 MB** | **~110 MB** |
| Dependency floor (numpy + flask + flask-socketio + pyserial) | 54 MB | ~58 MB |
| Sensor-tick CPU, 16 vials | **2.1 ms** | ~15 ms |
| Growth recompute, staggered, per tick | 3.4 ms | ~24 ms |
| Whole sensor loop, duty cycle | 0.08 % of a core | **~0.5 % of one of four** |
| Startup: import + `create_app` | 0.6 s | ~4 s |
| `sensor_update` payload, 16 vials | 13 kB | — |

The observed load average on the rig (0.00 / 0.11 / 0.08) is consistent with the ~0.5 % estimate.

**Memory is bounded.** A 500×-accelerated run simulating 8.3 days showed RSS climbing from 70 MB
to 102 MB over the first ~5 simulated days, then **flat at 102.3 MB** for the remaining 3 — the
3 h growth-history window saturating, then steady state. The retention logic is genuinely
time-trimmed (`experiment_engine.py:2002`, `:2071`) and the event ring is `deque(maxlen=500)`
(`event_log.py:343`). One exception in §7.

**The tick is I/O-bound, not CPU-bound.** At 9600 baud, one OD acquisition is 5 light + 3 dark
round trips plus a temperature read — roughly **2.3 s of the 10 s tick spent blocked on the
UART with the GIL released**, against ~15 ms of actual CPU. That is an unusually good property
for the question being asked: the control loop coexists with a busy browser far better than its
CPU share alone would suggest, because it is asleep on a serial port for most of its period.

**The read-path optimisation in SPEC §8.1 is real.** On a 7-day file, `get_data` with a 1 h
window is **19.6 ms / 0.3 MB** and flat in run length, against ~2.2 s for the same file read
whole. `build_bundle` streams: 9.5 MB peak on a 95 MB dataset. Both confirmed independently.

---

## 4. Where the real-time guarantees live — and one exception

Mostly delegated to firmware, which is why this plan is viable at all:

- **Pump dosing is firmware-timed.** `pump_command` (`serial_manager.py:604–629`) sends
  `st{addr},0,{whole_seconds},` and returns. The Pi never sleeps for the duration and then
  sends a stop. There is exactly one `time.sleep` in the whole server tree —
  `serial_manager.py:250`, the 50 ms inter-command spacing.
- **Temperature regulation is firmware-side.** `xr` is a setpoint the Arduino's closed loop
  chases; no PWM in Python.
- **Stir is set-and-forget.**

So Python-side scheduling jitter cannot corrupt a dose or a heater duty cycle. Contention from a
desktop degrades *responsiveness and OD sampling density*, not experimental integrity.

**The exception, and it matters here.** Over-temperature *safety* is **not** delegated.
`_handle_heater_safety_locked` (`experiment_engine.py:2647–2688`) is a Python-side check on the
sensor-loop thread requiring **three consecutive** over-critical reads before latching a fault
and parking the heater. Worst-case detection latency is therefore **3 × the actual loop period**
— and the loop period is `max(10 s, work)`, not a guaranteed 10 s. Anything that stretches the
loop stretches the overtemp response by three times as much. That is the mechanism by which
memory pressure from a browser could reach a live culture, and it is why §5 is not merely a
performance note.

---

## 5. What breaks first, in order

### 5.1 Memory pressure → swap on SD → the per-tick fsync stalls

`run_cycle` calls `_save_state_locked()` **every tick, inside the engine lock**
(`experiment_engine.py:1568`, lock opened at `:1371`). That method does a full JSON serialise at
`indent=4`, `f.flush()`, **`os.fsync()`**, then `os.replace()` (`:3107–3114`). No dirty check,
no throttle.

Measured: **1.09 ms — 90 % of `run_cycle` and 51 % of the entire tick** — and that is on a
container overlay filesystem. On an SD card an fsync is typically 5–50 ms and spikes to
hundreds of ms under concurrent write load. It is 8.6 kB rewritten and force-committed 8 640
times a day, forever.

This is the single component that degrades worst when a desktop and a browser start sharing the
card, and it is the one holding the engine lock while it does so.

**The swap sitting behind it makes this worse than it looks.** 904 MB of file-backed swap on the
same SD card, currently 0 used. The moment Chromium pushes the box into swap, the control loop's
fsync queues behind pageout on the same device — seconds, not milliseconds, with the engine lock
held. **Move to zram before going headed**: compressed RAM swap, no SD I/O, no wear, and the
four mostly-idle cores are exactly the resource it spends.

```bash
sudo apt install zram-tools
# /etc/default/zramswap:  ALGO=zstd  PERCENT=50   (≈450 MB of zram)
sudo dphys-swapfile swapoff && sudo systemctl disable dphys-swapfile
sudo sysctl -w vm.swappiness=100      # zram wants a HIGH swappiness, unlike disk swap
```

zram does not create memory — if you genuinely exceed physical RAM you still OOM. But that is
the better failure: paired with the `OOMScoreAdjust` settings in §5.5, you get a browser killed
cleanly instead of an instrument stalled for seconds.

**Also fix the fsync itself.** Write `state.json` on *change* plus a slow heartbeat (60 s, and
unconditionally on pump events and state transitions) rather than every tick — crash recovery
does not need 10 s granularity. Point Chromium's cache away from the card
(`--disk-cache-dir=/dev/shm --disk-cache-size=32000000`). Longer term, `experiments/` on a USB
SSD fixes SD wear and fsync latency together.

### 5.2 Two user-triggerable CPU and memory bombs, now one click from the console

Both are already reachable remotely; a local browser makes them likelier, on a machine with far
less headroom.

| Action | Measured (x86, 7-day run) | Pi 3B estimate |
|---|---|---|
| Plots tab, **"All" range**, 16 vials × (od+temp+pump) | ~71 s CPU, ~81 MB transient | **~8 min CPU, ~81 MB** |
| Export bundle, whole run, 16 vials | 21.7 s, 9.5 MB peak | ~2.5 min, ~10 MB |
| Plots tab, 1 h range (the default) | 16 × 20 ms | ~2 s — fine |

The "All" range is deliberately O(n) and documented as such (`experiment_engine.py:1798`), and
the front end fires every vial in parallel (`index.html:4792`), so it lands as ~6 concurrent
full-file reads. It is not a bug — it is a request the UI should not offer so casually on this
hardware. Cap it, or gate it behind a confirmation once a run exceeds some row count.

### 5.3 The sensor loop can already block for far longer than a tick

Independent of the headed plan, but it compounds with §4's 3-tick debounce:

- A **silent RS485 bus** costs 5 × 5.05 s of OD plus 3 dark reads plus temperature — **~30–45 s
  in one tick** (`serial_manager.py:39`, `:325`; defaults `n_samples=5`, `n_dark=3`).
- `POST /api/calibration/raw/od_led` accepts `n_samples` up to 25 (`app.py:1777`) and
  `collect_od_raw` holds the serial lock across all of them — **up to ~126 s** on a dead bus,
  with the sensor loop blocked behind it.
- `start_experiment` / `stop_experiment` hold the **engine** lock while doing serial I/O
  (`experiment_engine.py:896` → `:2815`), so an HTTP stop can queue behind an in-flight read.

None of these are caused by adding a desktop. They set the floor on how well the loop's timing
can be guaranteed, which is what the overtemp debounce is measured against.

### 5.4 Thermal headroom

53.2 °C idle, `throttled=0x0`, inside an enclosed instrument — warm for an idle Pi 3B, which
suggests limited airflow. The Pi 3 Model B (unlike the 3B+, which has a 60 °C soft limit)
throttles progressively from about 80 °C and hard-limits at 85 °C. A browser rendering on all
four cores in a closed box plausibly adds 25–35 °C, which puts throttling in play — and
throttling halves everything at once.

Cheap to settle empirically: open the GUI locally, load the Plots tab, and watch
`vcgencmd measure_temp` and `vcgencmd get_throttled`. A heatsink, or a small fan, is the fix if
it bites.

### 5.5 Nothing protects the control server from the desktop

`evolver.service` sets no `Nice`, no `OOMScoreAdjust`, no `MemoryMin`. Under memory pressure the
kernel is free to pick the control server as the OOM victim, or to swap it out — and the process
that fsyncs every 10 s is the worst possible swap candidate. Add:

```ini
[Service]
Nice=-5
OOMScoreAdjust=-800
MemoryMin=160M
IOSchedulingClass=best-effort
IOSchedulingPriority=2
```

and give the browser session the opposite treatment (`OOMScoreAdjust=200`) so the desktop dies
before the instrument does.

---

## 6. Werkzeug — a real weak link, but do not "fix" it with waitress

`app.py:2277` runs the Flask **development** server with `allow_unsafe_werkzeug=True`, and the
server says so on every boot. For 1–3 clients this is not a CPU problem, and it is not on the
critical path for the headed plan. But it is the weakest robustness link in the stack, and a
headed box means a local browser *plus* your remote one.

**The obvious swap is a trap, and I checked.** Serving this app through `waitress` was measured
in this session: the Socket.IO handshake still advertises `upgrades: ["websocket"]`, and the
actual WebSocket connection then **fails** — only `polling` succeeds. Waitress does not support
the WSGI extensions `simple-websocket` needs. So a "drop-in" waitress swap would silently
downgrade every client to HTTP long-polling, on a box where each poll is a fresh request holding
a thread — strictly worse than what runs today, *and* it would burn a failed upgrade attempt on
every reconnect.

What actually runs today does work: `python-engineio` hard-depends on `simple-websocket`, so
Werkzeug + threading mode serves real WebSockets, and a client was confirmed connected over the
`websocket` transport.

If you do want to move off the dev server, it has to be an **async-mode change**, not a WSGI
swap — `gunicorn` with an `eventlet` or `gevent-websocket` worker, and `async_mode` changed to
match at `app.py:475`. That is a real change with real testing behind it, and it is P2 work.
Leaving Werkzeug in place for a 1–3 client instrument is defensible; just do it knowingly.

---

## 7. Two accumulating costs

### 7.1 The pump-log index — measured, and worse than the memory picture suggested

`CalibrationStore._pump_log_cache` (`calibration_service.py:225`) keeps, per pump-log file, two
Python lists appended one element per pump event and **never trimmed** (`:618–619`). Eviction
only happens when a file disappears (`:657`), and the scan covers **every experiment in the
tree** (`:640`) — so the retained set is O(total pump events across the whole campaign), not
O(current run).

Measured directly, 27 experiment dirs / 432 pump logs / 226 320 pump rows:

| | measured (x86) | Pi 3B estimate |
|---|---|---|
| `pump_seconds_since`, **cold** | **12 973 ms** | ~90 s |
| `pump_seconds_since`, warm | 15 ms | ~105 ms |
| index resident afterwards | **14.3 MB** | same |
| retention rate | **66 bytes per pump row, forever** | same |

Both symptoms are the same object: a multi-second-to-minute stall on the first call after a
restart, on the request thread, plus unbounded resident growth thereafter. The cold cost scales
with the *whole experiments tree*, so it gets worse every campaign, not every run.

It is dormant today: `pump_seconds_since` → `staleness()` short-circuits while
`calibration/current.json` has `"pump": null`, which it does. It arms itself **the moment Tier 2
pump calibration lands** — which, per `PI_BRIEFING.md`, is the first bench ask. This is why the
memory measurement in §3 plateaus, and why it will stop plateauing after calibration.

Bound it *before* running the pump calibration, not after.

### 7.2 `ts-keepalive` — the cost is real, though the keepalive is doing its job

`deploy/ts-keepalive/ts-keepalive.timer` fires every 20 s, forever: a bash script that runs
`tailscale status --json`, pipes it through **a fresh `python3` interpreter** to parse peers,
then spawns a `timeout` + `tailscale ping` subprocess per peer, in parallel.

That is ~4 320 cycles a day. Python interpreter startup alone measures 24.5 ms here and would be
~150–250 ms on a Pi 3B — call it **~15 minutes a day of interpreter startup**, before the
`tailscale` subprocesses. Negligible on an idle headless box; on a headed box contending for CPU
and page cache, it is pure tax.

To be accurate about the justification, though: the README does **not** record this keepalive as
ineffective. It records the *alternatives* as ineffective (pinging a public node, `netcheck`,
keeping one peer warm) and this approach as "what works". The right criticism is of the
implementation, not the mechanism:

- Replace the `python3 -c` parse with `jq`, or with `tailscale status --peers --json` filtered
  in shell — removes an interpreter start per cycle for nothing.
- Widen the interval and let `AccuracySec` do more work; 20 s is a guess, not a measurement.
- Best: the README already names the real fix — **wired Ethernet removes the symmetric NAT and
  the keepalive with it**. Going headed makes that more attractive anyway, since a bench console
  implies the box is somewhere a cable can reach.

---

## 8. The budgets

### Memory — against 905 MB usable

| Component | MB |
|---|---|
| Debian 13 base — systemd, journald, sshd, networking | 60–90 |
| `tailscaled` | 30–45 |
| **eVOLVER server** (measured 102 MB, +ARM/3.13) | **105–120** |
| Minimal WM/compositor + greeter + panel | 60–110 |
| Terminal emulator | 15–25 |
| **Chromium, one tab of this GUI** | **280–380** |
| **Steady-state total** | **550–770** |
| **Headroom** | **135–355** |
| — transient: Plots "All" range | +81 |
| — transient: export bundle | +10–20 |

Chromium was measured in this session at ~420 MB across its process tree (renderer 128 MB,
browser 90 MB, GPU 66–76 MB, network 66 MB, zygotes 73 MB shared) on x86 headless against this
exact page; a headed ARM build with the V3D driver should land somewhat lower.

The page itself is light and gives Chromium little to do: **645 DOM nodes**, one uPlot instance
factory, plots capped at 2000 points (`index.html:4523`), no polling loops, and updates gated on
view visibility. The 231 kB `index.html` is served **uncompressed** — irrelevant from page cache
locally, worth ~45 kB gzipped for the remote case, and a two-line `after_request` fix.

### Disk — against 3.1 GB free of 6.9 GB, and this is the tighter one

| | GB |
|---|---|
| Free today | 3.1 |
| Minimal desktop + Chromium install | −1.2 to −1.8 |
| **Free after** | **1.3–1.9** |
| Experiment data | ~8 MB/day (SPEC §8.1: 53 MB per 7-day 16-vial run) |

Runs are cheap enough that you would survive. The sharper consequence is that the server's own
disk monitor is calibrated for a roomier card: it warns below **1 GB free or 10 % free**
(`app.py:88–91`), and 10 % of 6.9 GB is 690 MB. At 1.3–1.9 GB free you are sitting close to the
warn line permanently, and a couple of long campaigns plus a Chromium cache would cross it — so
the dashboard's low-disk banner becomes background noise exactly when you need it to mean
something. The log-suspend floor (128 MB) is closer than it should be too.

**A 32 GB card removes this whole section**, and buys back SD endurance against the 10 s CSV
writes at the same time. It is the cheapest item on this entire list.

---

## 9. What to do

**Blocking — the headed console does not work without this**

1. Vendor `socket.io.min.js` locally and guard the `io` reference. *(§1, ~15 min)*

**Before installing a desktop**

2. Snapshot `/boot/firmware/config.txt`, add a post-install diff gate to `DEPLOY.md`, and add a
   startup assertion that `/dev/ttyAMA0` is the PL011. *(§2)*
3. Reflash to a 32 GB card, or accept a permanently-near-threshold disk warning. *(§8)*

**Before putting a browser on the box**

4. Switch to zram and disable the SD-backed swapfile. *(§5.1)*
5. Throttle the `state.json` fsync to on-change + 60 s heartbeat. *(§5.1)*
6. Add `OOMScoreAdjust` / `MemoryMin` / `Nice` to `evolver.service`; opposite for the desktop
   session. *(§5.5)*
7. Bound or gate the Plots "All" range; run the export bundle off the request thread. *(§5.2)*
8. Point Chromium's cache at `/dev/shm` and cap it. *(§5.1)*

**Measure on the actual Pi, before trusting any number above**

9. `python3 server/bench_growth_rate.py` — pins the ×7 scaling factor.
10. `python3 server/bench_read_paths.py` — SD-card I/O does not extrapolate from x86.
11. `vcgencmd measure_temp` / `get_throttled` with the GUI open locally and Plots loaded. *(§5.4)*

**Before the pump calibration bench session**

12. Bound `_pump_log_cache`. *(§7.1)*

**Worth doing anyway**

13. gzip responses (`flask-compress`, or a small `after_request`).
14. De-Python the `ts-keepalive` cycle, or retire it via wired Ethernet. *(§7.2)*
15. `experiments/` on a USB SSD — fixes fsync latency, SD wear, and browser-vs-instrument card
    contention in one move.

**Do *not* do**

16. Do not swap Werkzeug for `waitress`. It silently breaks WebSocket and downgrades every
    client to long-polling. Moving off the dev server means an async-mode change, and that is
    P2. *(§6)*

---

## 10. The honest summary

The software is not bloated and it is not naive about this hardware — the staggered growth
recompute, the tail reads, the bounded ring buffer, the prefix-sum window search and the
firmware-side actuation are all the right calls, and several of them were clearly made *because*
the target is a small ARM box. On CPU, this server could share a Pi 3B with a desktop several
times over.

What stands between here and a headed console is: a CDN link that should never have been a
runtime dependency; a UART configuration that a desktop install can quietly undo; an fsync that
runs 8 640 times a day when it needs to run a few hundred; a UI affordance that can ask for
eight minutes of CPU with one click; a systemd unit that does not defend the instrument against
the desktop you are about to install next to it; and a 6.9 GB card that a desktop nearly fills.
Those are fixes, not a redesign, and most of them are configuration rather than code.

The one thing worth flagging beyond the immediate question: the overtemp check is a three-tick
debounce on a loop whose period is not guaranteed, on a rig where a silent bus can stretch a
single tick to 45 seconds. That is worth a look on its own merits, before it becomes the
headed-OS project's problem.
