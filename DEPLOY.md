# eVOLVER Pilot Deployment Runbook

This is the operator runbook for deploying the eVOLVER Phase 1 MVP to the lab hardware (Raspberry Pi at `192.168.1.2`, four SAMD21 Arduinos on RS485, 16 smart sleeves). It covers RPi install, hardware validation on water, and the first live-culture pilot.

The runbook assumes Phases 0–2 of the deployment plan are already complete (tests passing, artifacts created, work committed and tagged). For full context see `SPEC.md`, `CLAUDE.md`, and the plan file under `~/.claude/plans/`.

---

## Prerequisites — Python 3.11 on Raspbian Jessie

The legacy RPi ships Python 3.4.2 (Jessie); the new server requires 3.10+ (Flask 3.x, numpy 1.25, `X | Y` union types). These steps build Python 3.11 into `/opt/python3.11` without touching the system Python (so the old supervisor processes keep working as a fallback). Skip this section if the RPi already has Python 3.11 (e.g. after a Bookworm reflash).

### N1. Bring the RPi online via Ethernet

Plug a LAN cable from the RPi's Ethernet port into the Netgear N300 router (WiFi is currently unavailable). SSH in over the wired link:

```bash
ssh pi@192.168.1.2
ping -c 3 8.8.8.8        # need internet for apt + source downloads
ping -c 3 192.168.1.1    # need the router
```

If the static IP `192.168.1.2` is only bound to the WiFi interface, edit `/etc/dhcpcd.conf` or `/etc/network/interfaces` to bind it to `eth0` too, then `sudo systemctl restart networking`.

### N2. Stop the legacy supervisor processes

```bash
sudo supervisorctl stop all
sudo supervisorctl status        # confirm STOPPED
sudo fuser /dev/ttyAMA0          # must print nothing
```

If `fuser` shows a PID, kill it before continuing — neither the Python build nor the new server can share `/dev/ttyAMA0`.

### N3. Refresh apt sources (Jessie's mirrors are dead → archive.debian.org)

```bash
sudo cp /etc/apt/sources.list /etc/apt/sources.list.bak
sudo tee /etc/apt/sources.list >/dev/null <<'EOF'
deb http://archive.raspbian.org/raspbian/ jessie main contrib non-free rpi
deb http://archive.debian.org/debian/ jessie main contrib non-free
deb http://archive.debian.org/debian-security/ jessie/updates main contrib non-free
EOF
sudo apt-get -o Acquire::Check-Valid-Until=false update
```

`Check-Valid-Until=false` is required because Jessie's `Release` files are years past expiry.

### N4. Build OpenSSL 1.1.1 (Jessie ships 1.0.1, Python 3.10+ needs 1.1.1+)

```bash
sudo apt-get install -y build-essential gcc make wget pkg-config \
    zlib1g-dev libbz2-dev libreadline-dev libsqlite3-dev \
    libncursesw5-dev libffi-dev tk-dev uuid-dev libgdbm-dev liblzma-dev

cd /tmp
wget https://www.openssl.org/source/old/1.1.1/openssl-1.1.1w.tar.gz
tar xzf openssl-1.1.1w.tar.gz
cd openssl-1.1.1w
./config --prefix=/opt/openssl-1.1.1 --openssldir=/opt/openssl-1.1.1 shared zlib
make -j$(nproc)
sudo make install
```

Goes into `/opt/openssl-1.1.1`; does not replace the system OpenSSL (`/usr/lib/...`) so any legacy program that links against it keeps working.

### N5. Build Python 3.11 from source into `/opt/python3.11`

```bash
cd /tmp
wget https://www.python.org/ftp/python/3.11.9/Python-3.11.9.tgz
tar xzf Python-3.11.9.tgz
cd Python-3.11.9
LDFLAGS="-Wl,-rpath=/opt/openssl-1.1.1/lib" \
./configure --prefix=/opt/python3.11 \
    --with-openssl=/opt/openssl-1.1.1 \
    --enable-optimizations
make -j$(nproc)                   # 30–60 min on a Pi 2/3
sudo make altinstall              # altinstall (not install) → only /opt/python3.11/bin/python3.11

/opt/python3.11/bin/python3.11 --version
/opt/python3.11/bin/python3.11 -c "import ssl; print(ssl.OPENSSL_VERSION)"
```

Expected: `Python 3.11.9` and `OpenSSL 1.1.1w  11 Sep 2023`.

**If the build fails repeatedly** on missing headers or linker errors that apt-archive can't supply, fall back to **reflashing the SD card with current Raspberry Pi OS Bookworm** (ships Python 3.11 natively) — ~30 min if you have a spare card, and the old card stays intact as rollback.

---

## Phase 3 — Deploy to the Raspberry Pi

### P3.1 SSH in and stop the old system

```bash
ssh pi@192.168.1.2
sudo supervisorctl stop all
sudo supervisorctl status      # confirm all STOPPED
sudo fuser /dev/ttyAMA0        # must print nothing
```

If `fuser` shows a PID, kill it before continuing — the new server cannot share `/dev/ttyAMA0`.

### P3.2 Verify Python 3.11 is available

```bash
/opt/python3.11/bin/python3.11 --version            # expect: Python 3.11.9
/opt/python3.11/bin/python3.11 -c "import ssl; print(ssl.OPENSSL_VERSION)"
                                                    # expect: OpenSSL 1.1.1w
```

If `/opt/python3.11/bin/python3.11` is missing, complete the Prerequisites section above (N3–N5) first. `install.sh` will refuse to run without it.

### P3.3 Get the code onto the RPi

```bash
cd /home/pi
git clone <repo-url> evolver-gui
cd evolver-gui
git checkout pilot-v0.1.0
```

If no remote exists, `rsync` from the dev machine:

```powershell
# From the Windows dev machine
rsync -avz --exclude='.git' --exclude='__pycache__' --exclude='experiments/' `
    "c:\Research\BIOLOGY\PersonalResearch\EVOLVER_GUI_DEV\eVOLVER_FileStruct\" `
    pi@192.168.1.2:/home/pi/evolver-gui/
```

### P3.4 Run the installer

```bash
cd /home/pi/evolver-gui
chmod +x install.sh
./install.sh
```

The installer:
- Installs `python3`, `python3-venv`, `python3-pip` via apt.
- Creates a venv at `.venv/` and installs pinned requirements.
- Adds `pi` to `dialout` for `/dev/ttyAMA0` access (logout/login required).
- Generates a Flask secret at `/etc/evolver/secret.env` (mode 0600, root-owned).
- Registers `/etc/systemd/system/evolver.service` but does **not** start it.

Log out and back in so the `dialout` membership takes effect.

### P3.5 Verify calibration files are in place

```bash
ls -la /home/pi/evolver-gui/calibration/
head -1 /home/pi/evolver-gui/calibration/temp_calibration.txt
```

The first row of `temp_calibration.txt` must be 16 negative floats (e.g. `-0.103,-0.111,...`). A positive slope means the inverted-`xr` convention is broken and the system would drive heaters wrong-way — **abort and fix before continuing**.

### P3.6 First boot — mock mode (no serial)

```bash
cd /home/pi/evolver-gui
.venv/bin/python server/app.py --mock
```

`.venv/bin/python` is a symlink to `/opt/python3.11/bin/python3.11` — verify with `ls -l .venv/bin/python*` if anything looks wrong.

Hit `http://192.168.1.2:5000` from the lab Mac. Confirm:
- Dashboard renders the 4×4 vial grid.
- Sensor values update every ~10 s.
- Server log says `loaded calibration from /home/pi/evolver-gui/calibration` (not the warning fallback).

`Ctrl+C` to stop. The mock proves install is good before we touch the serial port.

---

## Phase 4 — Hardware validation (water-only)

### P4.1 Start the real server (foreground, not systemd yet)

```bash
cd /home/pi/evolver-gui
.venv/bin/python server/app.py
```

Confirm:
- `loaded calibration from /home/pi/evolver-gui/calibration` in the log.
- `sensor loop started (interval=10.0s)`.
- `watchdog started (timeout=10.0 min, ...)` — note: 10-min timeout is the pilot setting; raise back to 30 after the pilot.

If the log says `calibration files not found`, **abort** — the relative path is wrong.

### P4.2 Sensor sanity check (read-only)

From the lab Mac browser at `http://192.168.1.2:5000`:

- All 16 temperatures should be in [18, 30] °C (room temp range), not all identical, not NaN.
- All 16 OD values should be near 0 with empty vials (within a few hundredths of OD600).
- Identical or NaN values across many vials → wrong `/dev/ttyAMA0`, dead Arduinos, or wiring issue. Stop and investigate.

### P4.3 Heater convention probe — single vial

This is the make-or-break test for the inverted-`xr` convention. Pick a vial that's easy to access with a probe thermometer (vial 0 is typical).

Via the per-vial manual control modal:

1. Set vial 0 to `22 °C`. Should be a no-op at room temp (raw ≈ 580).
2. Wait 30 s. Reading should stay near 22 °C and not climb.
3. Set vial 0 to `30 °C` (raw ≈ 480 — modestly warm).
4. Wait 5 min with the probe thermometer in the vial. Reading should climb toward 30 °C, **not** toward 80 °C.
5. **Critical:** click **Emergency Stop**. Within 10 s the heater should stop driving; within 2 min the vial should start cooling toward room temp.

If at step 3 the vial heats past 40 °C, **emergency stop immediately**. The slope sign in `temp_calibration.txt` is wrong for this hardware and the system is trying to drive the heater to max.

### P4.4 Stir mapping pass

Identify which logical vial 0..15 is which physical sleeve. In the manual control modal:

- Set vial 0 stir to 8, all others to 0. Walk to the eVOLVER and note which sleeve spins.
- Stop vial 0, set vial 1 to 8, observe.
- Repeat for all 16 vials.

Write down the logical → physical mapping. You'll need it for every future experiment until the mapping is hardwired in config.

### P4.5 Pump flow validation

With empty vials (or a measured-volume container under each efflux line):

- Influx on vial 0 for 10 s → should dispense ~10 × `flow_rate[0]` ml (default ~1 ml/s; see `experiment_engine.py` for per-vial defaults).
- Sample several vials, not all 16 — but enough to catch a stuck or backwards-plumbed pump.
- Any pump dispensing < 50 % of expected indicates an occlusion or stale calibration. Note it and decide whether to swap tubing or recalibrate before the pilot.

### P4.6 Water-only turbidostat — full cycle

Pick one mapped, validated vial (e.g. vial 0). Fill to 25 ml with water. Connect a water bottle to the influx and a waste container to the efflux. Create a single-vial turbidostat experiment:

- Vials: `[0]`
- Mode: turbidostat
- `lower_thresh`: 0.05
- `upper_thresh`: 0.10  (water OD will never reach this — controller should never fire)
- `pump_wait`: 1 minute
- Temperature: 30 °C
- Stir: 8

Start the experiment. Watch for 15 minutes:

- `experiments/<name>/vial00_OD.csv` should accumulate rows every 10 s.
- No pumps should fire (water OD stays well below upper threshold).
- After confirming no-pump behavior, **manually trigger an influx for 5 s** via the manual control. Verify water actually moves through the tubing and the pump log records the event.

Stop the experiment.

### P4.7 Enable systemd autostart

```bash
sudo systemctl enable --now evolver
sudo systemctl status evolver
sudo journalctl -u evolver -f
```

The dashboard should be reachable at `http://192.168.1.2:5000`. Reboot the RPi:

```bash
sudo reboot
```

After ~1 minute, hit the dashboard again. If it loads, systemd autostart works.

---

## Phase 5 — First live-culture pilot

Only after Phase 4 passes cleanly.

### P5.1 Single-vial live-culture turbidostat

Pick one validated vial. Load with media + E. coli inoculum at OD ~0.05. Create a turbidostat experiment:

- Vials: `[0]`
- Mode: turbidostat
- `lower_thresh`: 0.15
- `upper_thresh`: 0.30
- `pump_wait`: 15 minutes (default)
- Temperature: 37 °C
- Stir: 8

Expected behavior:

- First 8 cycles (~80 s) suppressed by the warmup gate.
- OD climbs through the 0.15–0.30 window.
- First pump fires on the next cycle after avg-OD crosses 0.30.
- Pump duration: `-ln(0.15 / avg_OD) × 25 / flow_rate`, capped to 20 s.
- After pump fires, OD drops toward 0.15; controller waits for it to climb again.

Monitor in person for the first hour. If pump firing looks wrong (firing too often, not at all, or too long), emergency stop and investigate. `experiments/<name>/state.json` shows the controller's current target and last pump time — useful for debugging.

### P5.2 Scale to 2 vials

Once 1-vial is stable for ~6 hours, add a second vial with the same parameters. Confirm the two vials don't interfere on the shared RS485 bus (sensor reads still come back cleanly for both; pumps fire independently).

### P5.3 Beyond the pilot

Once 2-vial is stable for 24 h, scale up incrementally — 4 → 8 → 16. Don't jump straight to 16; thermal or pump issues are easier to catch on a fraction of the cultures.

After the pilot, restore `WATCHDOG_TIMEOUT_MINUTES = 30` in `server/app.py:66`.

---

## Operations

- **Logs:** `sudo journalctl -u evolver -f` (live tail) or `/var/log/evolver/app.log` (persistent).
- **Restart:** `sudo systemctl restart evolver` — triggers `resume_on_startup()` which rebuilds the in-flight experiment from `state.json`.
- **Clean shutdown:** `sudo systemctl stop evolver` — fires the SIGTERM handler which zeros stir, parks heaters at 4095, and stops pumps before exit.
- **Source of truth for a running experiment:** `experiments/<name>/state.json`. Back this up before any risky operation.

## Abort criteria

Any of these → stop and reassess:

- Sensor returns NaN for > 3 consecutive ticks on hardware (P4.2) — serial wiring issue.
- Vial heats toward 80 °C when commanded to 30 °C (P4.3) — calibration slope sign is wrong; heater will cook the next culture.
- Watchdog fires during validation (P4) — sensor loop is stalling.
- Manual emergency stop does not park heaters at 4095 within 10 s (P0.3 or P4.3) — safety system is broken; do not put live cells on the platform.
