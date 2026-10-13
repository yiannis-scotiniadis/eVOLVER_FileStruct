# eVOLVER Pilot Deployment Runbook

This is the operator runbook for deploying the eVOLVER Phase 1 MVP to the lab hardware (Raspberry Pi reachable at `pi@evolver-001` over Tailscale, four SAMD21 Arduinos on RS485, 16 smart sleeves). It covers RPi install, hardware validation on water, and the first live-culture pilot.

**Addressing note:** every `pi@evolver-001` and `evolver-001:5000` below is the Tailscale MagicDNS name. The dedicated Netgear N300 LAN router this rig used to sit behind, and the static `192.168.1.2` it handed out, are decommissioned — do not address the rig by LAN IP.

The runbook assumes Phases 0–2 of the deployment plan are already complete (tests passing, artifacts created, work committed and tagged). For full context see `SPEC.md`, `CLAUDE.md`, and the plan file under `~/.claude/plans/`.

---

## Prerequisites — Python 3.11 on Raspbian Jessie

The legacy RPi ships Python 3.4.2 (Jessie); the new server requires 3.10+ (Flask 3.x, numpy 1.25, `X | Y` union types). These steps build Python 3.11 into `/opt/python3.11` without touching the system Python (so the old supervisor processes keep working as a fallback). Skip this section if the RPi already has Python 3.11 (e.g. after a Bookworm reflash).

### N1. Bring the RPi online via Ethernet

This section is the ancient-Jessie-card escape path (see `SETUP_FROM_SCRATCH.md` — it does
not apply on a modern Bookworm/Trixie card). It predates Tailscale: the dedicated Netgear
N300 it originally described is now decommissioned, so if you're actually on this path, plug
the RPi's Ethernet port into *whatever* network gives it internet egress and SSH in on
whatever address DHCP hands it (or the `.local` mDNS name) — not a static `192.168.1.2`.
Once Tailscale is installed, switch to `ssh pi@evolver-001` for everything from here on:

```bash
ssh pi@<dhcp-or-.local-address>   # temporary, first-boot only
ping -c 3 8.8.8.8                 # need internet for apt + source downloads
```

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
wget https://github.com/openssl/openssl/releases/download/OpenSSL_1_1_1w/openssl-1.1.1w.tar.gz
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
ssh pi@evolver-001
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

The Pi keeps two directories, and every procedure in this document depends on keeping them
apart:

| Directory | What it is | Written by |
|---|---|---|
| `/home/pi/evolver-src` | A full git clone: history, docs, tests, tags. Never run. | `git`, and nothing else |
| `/home/pi/evolver-gui` | The runtime directory `install.sh` and `evolver.service` use. **Not a git checkout:** an export of one release tag, plus the rig's state. | `deploy.sh` (code), the server (state) |

`deploy.sh` builds `evolver-gui` from a tag with `git archive`, which honours the
`export-ignore` rules in `.gitattributes`, so docs, tests, `mac_original/`/`rpi_original/` and
dev tooling never reach the runtime directory. Because `evolver-gui` has no git history of its
own, `deploy.sh` records what it deployed in `evolver-gui/DEPLOYED_VERSION` (tag, commit, time).

```bash
cd /home/pi
git clone https://github.com/yiannis-scotiniadis/eVOLVER_FileStruct.git evolver-src
cd evolver-src
git checkout <newest-tag>        # deploy.sh runs from this checkout; the default branch may not have it
./deploy.sh <newest-tag>         # builds /home/pi/evolver-gui from that tag
```

`deploy.sh` never touches `evolver-gui/{calibration,experiments,exports,logs,machine,.venv}` —
the rig's persistent state. Its sync uses `rsync --delete`, so before it changes anything it
**refuses** to run if pointed at `evolver-src` itself, at any directory containing `.git`, at
`$HOME`, at a non-empty directory that isn't an eVOLVER runtime, or at a runtime directory
holding top-level files no release put there — and it refuses while `evolver.service` is
running. Each of those is a way the sync would otherwise delete something. A refusal changes
nothing; read the message, fix what it names, run it again.

If no remote exists, rsync the dev tree — **including `.git`**, since `deploy.sh` exports from
git — into `evolver-src` (never into `evolver-gui`) and run `deploy.sh` from there:

```powershell
# From the Windows dev machine — this seeds evolver-src, not evolver-gui
rsync -avz --exclude='__pycache__' --exclude='.venv/' --exclude='experiments/' --exclude='logs/' `
    "c:\Research\BIOLOGY\PersonalResearch\EVOLVER_GUI_DEV\eVOLVER_FileStruct\" `
    pi@evolver-001:/home/pi/evolver-src/
```

### P3.4 Run the installer

```bash
cd /home/pi/evolver-gui
./install.sh
```

The installer (safe to re-run; every update does):
- Picks a Python ≥ 3.10 (system `python3`, else `/opt/python3.11`).
- Creates `.venv/` — rebuilding it only if the interpreter's version changed — and installs the
  pinned requirements, skipping pip entirely when `requirements.txt` is unchanged since the
  last successful install.
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

Hit `http://evolver-001:5000` from the lab Mac. Confirm:
- Dashboard renders the 4×4 vial grid.
- Sensor values update every ~10 s (temperature and OD together).
  A fresh page load paints the last known values immediately rather than waiting.
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

From the lab Mac browser at `http://evolver-001:5000`:

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

- `experiments/<name>/vial00_OD.csv` and `vial00_temp.csv` should both accumulate rows
  every 10 s. (They are separate files, each with its own timestamp column, and nothing
  joins them by row index — so if the OD lane is ever decimated, differing row counts
  between them are expected rather than a fault.)
- `vial00_growth.csv` (SPEC §17) also accumulates a row every 60 s. In *this* run every
  row will have an empty `growth_rate_per_hour` and a `flags` column reading `low_od`
  (plus `uncalibrated_floor` if no OD blank has been committed) — water is below the
  estimator's OD floor, so there is nothing to fit. **That is the expected result, not a
  fault**; the flags column existing and being populated is what you are checking. Note
  also that `pump_wait: 1 minute` is below the estimator's 10 min minimum fit span, so
  experiment creation returns a warning saying no growth rate will be reported for the
  run. Expected here too — dilution itself is unaffected.
- No pumps should fire (water OD stays well below upper threshold).
- After confirming no-pump behavior, **manually trigger an influx for 5 s** via the manual control. Verify water actually moves through the tubing and the pump log records the event.

Stop the experiment.

### P4.7 Enable systemd autostart

```bash
sudo systemctl enable --now evolver
sudo systemctl status evolver
sudo journalctl -u evolver -f
```

The dashboard should be reachable at `http://evolver-001:5000`. Reboot the RPi:

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

- **Logs:** two sinks, both live. `sudo journalctl -u evolver -f` is the full stream (the unit no longer redirects stdout to a file). The server also writes rotating logs under `/home/pi/evolver-gui/logs/`: `evolver.log` (10 MB × 5) and `errors.log` (WARNING and above, 5 MB × 5) — tail with `tail -f /home/pi/evolver-gui/logs/evolver.log`. Both suspend themselves below a 128 MB free-space floor so logging can never fill the card the experiment data is on; `curl -s localhost:5000/api/health` reports `file_logging.suspended` if that happens.
- **Event log:** every discrete occurrence (pump fires and *suppressed* attempts, alerts, maintenance, refills, sensor and serial faults) lands in `experiments/{name}/events.csv` and ships inside every export ZIP. The last 500 events are also served from memory at `GET /api/events/recent`, which is what the dashboard's Alerts drawer reads — so alerts survive a page reload and a second browser.
- **Upgrading from a pre-Session-M deploy:** the unit changed from `StandardOutput=append:/var/log/evolver/app.log` to `journal`. Run `sudo systemctl daemon-reload && sudo systemctl restart evolver` after copying the new unit, then delete the stale `/var/log/evolver/app.log` — nothing rotates it and nothing writes to it any more.
- **Clean shutdown / restart:** `sudo systemctl stop` (or `restart`) `evolver` fires the SIGTERM handler, which calls `stop_experiment(reason="shutdown")` — this **ends** any running experiment (marks it STOPPED; it cannot be re-`start`ed), zeros stir, and parks heaters at 4095. It also *attempts* to stop the pumps, but see "Emergency stop" below: **that call does nothing.** Any pump command already on the wire runs to completion. **A clean restart does NOT resume the experiment.**
- **Crash recovery:** `resume_on_startup()` fires *only* after an **unclean** exit (power loss / crash that left `state.json` at RUNNING). On the next boot it rebuilds and resumes that experiment. A deliberate stop is never resumed.
- **Source of truth for a running experiment:** `experiments/<name>/state.json`. Back this up before any risky operation.

## Emergency stop — read this before you need it

**There is no software emergency stop for the pumps.** Measured on the bench 2026-09-23; full
evidence in `FLUIDICS_FIRMWARE_AUDIT.md`, summarised in `SPEC.md` §16.3.5.

The 2016 fluidics firmware queues command frames and runs them **one at a time to
completion**. It never preempts and it accepts no stop command. Specifically:

- The all-stop frame `SerialManager.stop_all_pumps()` sends **does not halt a running pump**
  and **does not flush the queue** — frames already sent still run.
- A zero-duration command for the running pump does nothing.
- The dashboard's emergency-stop control, `watchdog.emergency_shutdown`, and the shutdown
  handler all call that same inert function.

**Heaters and stir are not affected by this** — those subsystems accept a new setpoint
immediately, so parking heaters at 4095 and zeroing stir both work. It is fluidics only.

### What to actually do

| Situation | Action |
|---|---|
| A pump is running and must stop **now** | **Cut power to the auxiliary pump board.** This is the only thing that works. |
| Runaway dosing, pumps not currently mid-frame | `sudo systemctl stop evolver` — prevents any *further* frames being sent, which is the real mitigation |
| Something is about to overflow | Cut power, then lift the efflux line or aspirate manually |

Worst case exposure is the duration of the longest single pump command the server issues —
today up to 20 s for an automatic dilution, more for a long manual pump.

### Recommended hardware fix

The correct fix is a **relay or solid-state contactor on the auxiliary pump board's supply
rail, driven from a spare Pi GPIO**, plus a **physical E-stop button in series with the same
rail**. Either a human or the watchdog can then kill the pumps regardless of firmware state,
and `watchdog.py` finally gets something real to act on. The button also covers the case where
the Pi itself is wedged — not hypothetical on this rig, given its NetworkManager history.

Until that exists, **do not run unattended dilutions with a vial whose free headroom is
smaller than the largest bolus the controller can request.**

## Installing updates

Versions ship as git tags (`pilot-v0.1.11`, …). This is the whole procedure. Every step names
the directory it runs in, because running a command in the wrong one of the two directories
(P3.3) is how this procedure has failed before — see "What went wrong on 2026-10-11" below.

**Rules**
- **`git` only ever runs in `evolver-src`, and `deploy.sh` only ever runs from `evolver-src`.**
  `evolver-gui` is not a git checkout; `git` there answers "not a git repository", and that is
  correct. If a rig's `evolver-gui` *is* a git checkout, migrate it first (below).
- **Deploy by tag, never a branch.** Tags are reproducible and give a clean rollback target.
  `deploy.sh` refuses branch names.
- **Never move or delete a tag once it is pushed — cut a new one.** The rig records the tag it
  deployed, and a rollback names it. `pilot-v0.2.0` was deployed to eVOLVER-001 and then
  renamed on GitHub to `pilot-v0.1.10`; anything that only knew the old name couldn't find it.
  (`DEPLOYED_VERSION` also records the commit, which `deploy.sh` accepts when a tag is gone.)
- **Updates end the running experiment.** Stopping the service fires the SIGTERM handler, which
  calls `stop_experiment` and marks the run STOPPED (it cannot be re-`start`ed — you'd create a
  fresh experiment). **Update between experiments, not mid-run.** `deploy.sh` refuses while
  the service is running.
- **`deploy.sh` cannot touch state.** `calibration/`, `experiments/`, `exports/`, `logs/`,
  `machine/` and `.venv/` are excluded from its sync, so an update cannot alter or lose them.
  Back `experiments/` up off-Pi periodically anyway — SD cards fail.
- **Test the new tag before it touches the live rig** — the test suite on a dev machine and/or
  the new tag in `--mock` mode.

### Update an existing Pi to a new version

```bash
# 0. While the server is still running, do everything that needs the network.
#    Read the new tag's notes for state.json / calibration / requirements changes.
cd /home/pi/evolver-src
git fetch origin --tags
git checkout <new-tag>                      # so the deploy.sh you run is the newest one
cat /home/pi/evolver-gui/DEPLOYED_VERSION   # note tag= and commit=: your rollback target

# 1. Stop the service (ends any running experiment, zeros actuators).
sudo systemctl stop evolver

# 2. Export the new tag into evolver-gui. Still in evolver-src.
./deploy.sh <new-tag>

# 3. Apply it. install.sh re-registers the systemd unit, rebuilds the venv only if the
#    interpreter changed, and runs pip only if requirements.txt changed — so a routine
#    update needs no network from here on. To force a clean venv: rm -rf .venv first.
cd /home/pi/evolver-gui && ./install.sh

# 4. Start and verify.
sudo systemctl start evolver
systemctl status evolver --no-pager               # active (running)?
cat DEPLOYED_VERSION                              # tag= is <new-tag>
tail -n 40 logs/evolver.log                       # "loaded calibration", clean sensor cycles?
curl -s localhost:5000/api/sensors/all | head -c 200; echo
# Hard-refresh the dashboard; spot-check one manual control.
```

If `deploy.sh` refuses, nothing has changed — every check runs before the sync — so
`sudo systemctl start evolver` brings the old version straight back while you sort out what the
message names. If `install.sh` fails at the pip step (an unresolvable dependency, or no network
when `requirements.txt` changed), the service stays down until you fix it or roll back — which is
exactly why you test the new tag off the live rig first.

### Rollback

```bash
sudo systemctl stop evolver
cd /home/pi/evolver-src && ./deploy.sh <previous-tag>   # do NOT check out the old tag first
cd /home/pi/evolver-gui && ./install.sh                  # Permission denied? bash install.sh
sudo systemctl start evolver
```

Roll back with the **newest** `deploy.sh`: leave `evolver-src` on the newest checkout and just
name the older tag. `deploy.sh` exports by tag name, so the checkout doesn't need to match — and
`pilot-v0.1.9` through `pilot-v0.1.11` carry the `deploy.sh` that caused the 2026-10-11
incident. If the tag no longer exists, pass the `commit=` value from `DEPLOYED_VERSION` instead.
Tags cut before the fix ship `install.sh` without its executable bit, hence the
`bash install.sh` fallback. Nothing to restore afterwards: neither direction touches state.

### Migrating a single-clone rig (one-time)

Before the `evolver-src`/`evolver-gui` split, `evolver-gui` *was* the git clone and an update was
`git checkout <tag>` inside it. A rig still in that layout has to be migrated once, and
`deploy.sh` refuses to run against it until it has been.

**eVOLVER-001 is half-migrated (as of 2026-10-13).** Its `evolver-gui` holds the export of
`pilot-v0.1.10` (commit `80f201a`, deployed under the since-renamed tag `pilot-v0.2.0`) next to
a gutted `.git` that has only `.git/logs/` left — so `git` there says "not a git repository" —
and `evolver-src` does not exist yet. The steps below cover it; skip step 2a there, because the
clone it reads is already gone.

`<newest-tag>` must be a tag cut **after** the 2026-10-13 `deploy.sh` fix. `pilot-v0.1.11` and
everything before it carry the old script, which has none of the checks described below.

```bash
# 1. Stop the service (ends any running experiment).
sudo systemctl stop evolver

# 2a. Working clone only: record what the old release put at the top level, so deploy.sh
#     may replace those entries (docs, tests, mac_original/, ...) instead of refusing on them.
git -C /home/pi/evolver-gui ls-tree --name-only HEAD > /home/pi/evolver-gui/.deploy-manifest

# 2b. Take the git metadata out of the runtime directory. Move it, don't delete it: a working
#     clone holds any local branches and unpushed commits. (eVOLVER-001's local branch,
#     pi-inline-edits, was pushed and bundled in ~/pi-inline-edits.bundle back in June.)
mkdir -p ~/backups
mv /home/pi/evolver-gui/.git ~/backups/evolver-gui-dotgit-$(date +%F)

# 3. Create the source clone, with an identity for the calibration commit in step 6.
cd /home/pi
git clone https://github.com/yiannis-scotiniadis/eVOLVER_FileStruct.git evolver-src
cd evolver-src
git checkout <newest-tag>
git config user.name  "<your name>"
git config user.email "<your email>"

# 4. Deploy. If deploy.sh lists top-level entries that aren't part of the release, move each
#    one out and run it again. On eVOLVER-001 that is the two old backup folders:
./deploy.sh <newest-tag>
mv /home/pi/evolver-gui/evolver-backup-2026-06-0* ~/backups/
./deploy.sh <newest-tag>                       # succeeds; writes evolver-gui/DEPLOYED_VERSION

# 5. Install, start, verify — steps 3 and 4 of the update procedure.
cd /home/pi/evolver-gui && ./install.sh
sudo systemctl start evolver
```

**6. Commit back the calibration that exists only on the rig** — see the next section. On
eVOLVER-001 that is the Tier 2 pump calibration (`calibration/pump/2026-09-06T222026Z.json`), the
*C. necator* H16 organism profile (`calibration/od/2026-10-11T215618Z.json`) and the
`current.json` that points at both. Until they're committed they exist on that SD card and
nowhere else.

### What went wrong on 2026-10-11

eVOLVER-001 had never been migrated, so `evolver-gui` was still the git clone, with `deploy.sh`
checked out inside it. Running `./deploy.sh pilot-v0.2.0` there exported the tag and rsynced it
with `--delete` back over the clone it was running from. A `git archive` export has no `.git`,
so the sync deleted the clone's `HEAD`, `objects/`, `refs/` and `config`. `.git/logs/` survived
only by accident: `--exclude logs` was unanchored, so it matched `.git/logs/` as well as
`evolver-gui/logs/`. The same unanchored excludes stripped `evolver-backup-2026-06-0*/` of
everything but their `experiments/` folders. No code or rig state was lost — the commit is on
GitHub, and calibration, experiments, logs and the venv were excluded from the sync.

`deploy.sh` now: refuses to target its own clone, any directory containing `.git`, `$HOME`, or a
non-runtime directory; anchors every exclude to the top level and excludes `.git`
unconditionally; refuses to delete top-level entries no release put there; refuses while the
service runs; and records the deployed tag and commit in `DEPLOYED_VERSION`, since the runtime
directory has no history to ask.

### Clean re-install (new SD card / new Pi)

**See [`SETUP_FROM_SCRATCH.md`](SETUP_FROM_SCRATCH.md)** — the full card-rebuild runbook, from a
freshly flashed headless OS to an operational rig. It covers the parts this document assumes you
already have: harvesting the old card (`experiments/` exists nowhere else), the static IP on
NetworkManager rather than `dhcpcd`, the `disable-bt` UART configuration and how to verify
`/dev/ttyAMA0` is actually the PL011, restoring the state git does not carry, Tailscale node
identity, and which parts of Phase 4 a rebuild may skip.

The shape of it: flash Raspberry Pi OS Lite (64-bit, ≥32 GB, username `pi`) → harvest the old
card → DHCP network path (no static IP needed — Tailscale carries the real address) → UART
(`enable_uart=1` + `dtoverlay=disable-bt`, serial console off) →
`apt install python3-venv python3-pip git rsync` → `git clone` into `evolver-src` →
`git checkout <tag>` → `./deploy.sh <tag>` to build `evolver-gui` → restore `experiments/`,
`calibration/` and `machine/` into `evolver-gui` → `cd evolver-gui && ./install.sh` → Tailscale
(rejoin as `evolver-001`) → mock test → Phase 4 hardware validation →
`systemctl enable --now evolver`.

On a modern OS the N1–N5 Python-build prerequisites do **not** apply — those exist only for the
legacy Jessie escape path.

### Calibration: update-safe now, but provenance needs a manual step

The `evolver-src`/`evolver-gui` split (P3.3) resolves the failure mode this section used to
warn about: `git checkout <tag>` only ever runs inside `evolver-src`, which is not where the
live rig reads calibration from, so a checkout can no longer silently overwrite this rig's
`temp_calibration.txt`/`OD_cal.txt`/`current.json`, and re-calibrating on the Pi (a Tier 2/3
session, or a per-run OD blank) can no longer block the next update either. `evolver-gui`'s
`calibration/` is not git-managed at all — it starts as a copy of whatever `evolver-src`
tracked at the tag you deployed, then diverges as the wizards install new versions, exactly
like `experiments/`.

The tradeoff: that divergence is real calibration history (gravimetric pump runs, OD dilution
series, thermistor two-point sessions) that no longer lands in git automatically, so it isn't
backed up by `git push` the way it used to be intended to. After any bench calibration
session, copy the new envelopes back into the source tree and commit them on a branch —
`evolver-src` sits on a detached tag checkout, where a commit is easily lost:

```bash
cd /home/pi/evolver-src
git switch -c calibration/$(date +%F)
rsync -a /home/pi/evolver-gui/calibration/ calibration/
git status --short calibration/        # new envelopes, current.json, the derived .txt views
git add calibration/                   # _sessions/ and reconciliation_log.json are gitignored
git commit -m "calibration: <what you ran> on eVOLVER-001"
git push -u origin HEAD
git switch --detach <deployed-tag>     # back to a release checkout before the next deploy
```

Then merge that branch into the release branch on the dev machine, so later tags carry it.
This is a manual, deliberate step (do it after a calibration session, not as part of a
routine code update) rather than something `deploy.sh` does for you — going the other
direction automatically is exactly the overwrite hazard this section used to describe.

## Remote access keepalive (Tailscale)

eVOLVER-001 is reached over Tailscale. On Yale's symmetric-NAT WiFi the Pi's per-peer reachability decays when idle — a peer can only reach the Pi after the Pi has initiated a path to *that* peer. `deploy/ts-keepalive/` holds a systemd timer that, every 20 s, has the Pi `tailscale ping` every enrolled peer (self excluded) to keep all paths warm. Install on the Pi:

```bash
cd /home/pi/evolver-gui/deploy/ts-keepalive
sudo install -m 0755 ts-keepalive-pingall.sh /usr/local/bin/ts-keepalive-pingall.sh
sudo install -m 0644 ts-keepalive.service /etc/systemd/system/ts-keepalive.service
sudo install -m 0644 ts-keepalive.timer   /etc/systemd/system/ts-keepalive.timer
sudo systemctl daemon-reload && sudo systemctl enable --now ts-keepalive.timer
```

This is a workaround for a hostile network; wired Ethernet (removes the symmetric NAT) is the real fix. A brand-new device may need a one-time `tailscale ping <device>` from the Pi until the Pi's netmap learns it. Full rationale in `deploy/ts-keepalive/README.md`.

## Abort criteria

Any of these → stop and reassess:

- Sensor returns NaN for > 3 consecutive ticks on hardware (P4.2) — serial wiring issue.
- Vial heats toward 80 °C when commanded to 30 °C (P4.3) — calibration slope sign is wrong; heater will cook the next culture.
- Watchdog fires during validation (P4) — sensor loop is stalling.
- Manual emergency stop does not park heaters at 4095 within 10 s (P0.3 or P4.3) — safety system is broken; do not put live cells on the platform.
