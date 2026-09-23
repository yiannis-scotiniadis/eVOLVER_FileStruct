# SETUP_FROM_SCRATCH.md — building eVOLVER-001's Pi card from a fresh headless OS

Companion to `DEPLOY.md`. That document is the *operator runbook* for a rig that already
exists — validation, pilot procedure, updating between tags. This one covers the case its
"Clean re-install (new SD card / new Pi)" section only sketches in a single line: **you have
flashed a clean headless Raspberry Pi OS onto a new microSD card, and you need to get from
that to an operational eVOLVER.**

Target: **eVOLVER-001** — Raspberry Pi 3 Model B Rev 1.2, aarch64, 905 MB RAM, static
`192.168.1.2` on the Netgear N300, `/dev/ttyAMA0` wired to the RS485 transceiver, 4 × SAMD21,
16 smart sleeves, 32 pumps. Deploying `pilot-v0.1.5`.

**`DEPLOY.md`'s "Prerequisites — Python 3.11 on Raspbian Jessie" (N1–N5) does not apply here.**
That is the source-build escape path for the ancient card. Bookworm ships Python 3.11 and
Trixie ships 3.13; `install.sh` finds either as `python3` and `requirements.txt` is pinned to
work with both (`numpy>=2.1,<3` exists precisely because numpy 1.25 had no cp313 wheel).
Anywhere `DEPLOY.md` says `/etc/dhcpcd.conf`, read "NetworkManager" — see Stage 3.

---

## The mental model: what the card carries and what git carries

Getting this split right is most of the job. Three categories:

| Category | Lives where | Rebuild action |
|---|---|---|
| **Code** — server, frontend, `install.sh`, `evolver.service`, docs | git, tag `pilot-v0.1.5` | `git clone` + `git checkout <tag>` |
| **Calibration provenance** — `calibration/*.txt`, `current.json`, `od/`, `temperature/` | **git (tracked)** | comes with the clone — then *verify against this rig* |
| **Rig state** — `experiments/`, `logs/`, `calibration/_sessions/`, `reconciliation_log.json`, `/etc/evolver/secret.env`, Tailscale node identity, SSH keys, `/boot/firmware/config.txt` | **only the old card** | harvest in Stage 0, restore in Stage 7 |

The trap is the middle row. Calibration is **tracked in git** (`DEPLOY.md`, "⚠ Calibration is
not update-safe yet"), so a clone silently hands you *whatever was committed*, which is
correct today only because nobody has re-calibrated on the Pi without committing. Stage 7
makes you check rather than assume.

The other trap: `experiments/` is the lab notebook. It is gitignored, it exists nowhere else,
and pulling the card is the moment you can lose it. Stage 0 is not optional.

---

## Stage 0 — Harvest the old card *(before the new card goes anywhere near the Pi)*

### 0.1 Confirm nothing is running

```bash
ssh pi@192.168.1.2
curl -s localhost:5000/api/health | head -c 400; echo
sudo systemctl status evolver --no-pager
```

If an experiment is RUNNING, **stop it deliberately and let it finish stopping**. A card swap
is an unclean exit as far as `state.json` is concerned; a deliberate `stop` is never resumed,
but a card yanked mid-run leaves `state.json` at RUNNING and the *new* card would fire
`resume_on_startup()` against hardware you have not yet validated.

```bash
sudo systemctl stop evolver        # fires SIGTERM -> stop_experiment(reason="shutdown")
sudo systemctl status evolver --no-pager    # inactive (dead)
```

### 0.2 Take the backup

```bash
sudo tar czf /home/pi/evolver-001-card-$(date +%F).tar.gz \
    --ignore-failed-read \
    -C / \
    etc/evolver/secret.env \
    boot/firmware/config.txt \
    boot/firmware/cmdline.txt \
    var/lib/tailscale/tailscaled.state \
    home/pi/.ssh/authorized_keys \
    home/pi/evolver-gui/experiments \
    home/pi/evolver-gui/logs \
    home/pi/evolver-gui/calibration
sudo chown pi:pi /home/pi/evolver-001-card-$(date +%F).tar.gz
ls -lh /home/pi/evolver-001-card-*.tar.gz
tar tzf /home/pi/evolver-001-card-$(date +%F).tar.gz | wc -l    # sanity: not empty
```

`--ignore-failed-read` is there because a couple of those paths may legitimately not exist
(`logs/`, `authorized_keys` on a password-auth card). Read tar's warnings — a missing
`experiments/` or `tailscaled.state` is not a warning to shrug at.

Pull it **off the Pi** — the point of the exercise is to not depend on this card:

```powershell
# from the Windows dev machine
scp pi@192.168.1.2:/home/pi/evolver-001-card-*.tar.gz `
    "C:\Research\BIOLOGY\PersonalResearch\EVOLVER_GUI_DEV\_card_backup\"
```

### 0.3 Record the things that are settings, not files

```bash
git -C /home/pi/evolver-gui describe --tags       # the tag actually deployed
git -C /home/pi/evolver-gui status --short        # MUST be clean; anything here is uncommitted rig state
timedatectl                                       # timezone — CSV timestamps depend on it
hostnamectl                                       # hostname (Tailscale/MagicDNS name)
tailscale status --self --json | head -40         # node name + Tailscale IP
nmcli con show                                    # or `cat /etc/dhcpcd.conf` on the old OS
systemctl list-unit-files --state=enabled | grep -v '@'
crontab -l; sudo crontab -l
head -1 /home/pi/evolver-gui/calibration/temp_calibration.txt
```

`git status --short` deserves a pause. If `calibration/` shows as modified, **someone
re-calibrated on the Pi and never committed it** — those values are the real ones and the git
copy is stale. Copy them out by hand and commit them from the dev machine before you rebuild,
or the rebuild will quietly install worse calibration than the rig had.

### 0.4 Label and shelve the old card

Write the tag and date on it. **Do not reuse it.** It is your rollback until the new card has
carried a real run, and it is the only copy of anything you failed to harvest above.

---

## Stage 1 — Verify what Imager wrote

You have already flashed. Confirm these, because two of them are expensive to fix later:

| Setting | Value | Why |
|---|---|---|
| OS | Raspberry Pi OS **Lite (64-bit)**, Bookworm or Trixie | Lite = headless. 64-bit for aarch64 numpy wheels. |
| Card size | **32 GB or larger** | The old 6.9 GB card sat at 3.1 GB free; the server's disk monitor warns below 1 GB *or 10 % free* (`DISK_WARN_FREE_BYTES` / `DISK_WARN_FREE_PCT`, `app.py:157–158`), so a small card puts the dashboard's low-disk banner permanently near the line. A bigger card also buys SD endurance against the 10 s CSV writes. |
| Username | **`pi`** | `install.sh` hardcodes `/home/pi/evolver-gui` and `usermod -aG dialout pi`; `evolver.service` hardcodes `User=pi` and three `/home/pi/...` paths. A different username means editing both files. |
| Hostname | match the old card (Stage 0.3) | It is the Tailscale/MagicDNS name. |
| SSH | enabled, **public-key** auth | Paste the same key that is in the harvested `authorized_keys`. |
| Locale / timezone | **match the old card** | Experiment CSV timestamps are wall-clock. A timezone change makes the notebook discontinuous across the rebuild. |
| Wi-Fi | optional | The rig is reachable at `192.168.1.2` over the lab router. Wired Ethernet is strictly better here — it removes the symmetric NAT that `deploy/ts-keepalive/` exists to work around. |

If you got the username wrong, reflash. It is five minutes now against edits to two files plus
every path in `DEPLOY.md` forever.

---

## Stage 2 — First boot and OS baseline

Card in, power on, wait ~90 s. Find it and get in:

```bash
ping -c 3 <hostname>.local
ssh pi@<hostname>.local
```

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y git python3-venv python3-pip
sudo reboot            # if the upgrade touched the kernel or firmware
```

Confirm the interpreter `install.sh` will pick:

```bash
python3 --version                  # expect 3.11 (Bookworm) or 3.13 (Trixie)
python3 -c 'import sys; print(sys.version_info >= (3,10))'    # must be True
uname -m                           # aarch64
```

If that prints `armv7l`, you flashed the 32-bit image. Reflash — `requirements.txt` leans on
aarch64 wheels and this is not worth fighting.

---

## Stage 3 — Network identity (static `192.168.1.2`)

Bookworm and Trixie use **NetworkManager**, not `dhcpcd`. `DEPLOY.md`'s `/etc/dhcpcd.conf`
instruction is the Jessie-era path and editing that file on a modern card does nothing.

```bash
nmcli con show                                   # find the profile name, usually "Wired connection 1"

sudo nmcli con mod "Wired connection 1" \
    ipv4.method manual \
    ipv4.addresses 192.168.1.2/24 \
    ipv4.gateway 192.168.1.1 \
    ipv4.dns "192.168.1.1 8.8.8.8"

sudo nmcli con up "Wired connection 1"
```

Verify — and note you will lose the `.local` SSH session at this point, so reconnect on the
new address:

```bash
ip -4 addr show                                  # 192.168.1.2/24 on eth0
ping -c 3 192.168.1.1                            # router
ping -c 3 8.8.8.8                                # internet (needed for apt, pip, Tailscale)
```

Do the same for the Wi-Fi profile if the rig is on Wi-Fi, so the static address is bound to
whichever interface actually comes up.

---

## Stage 4 — The UART **← the step that must not be got wrong**

This is the one place where a plausible-looking configuration produces an instrument that
appears to work and then corrupts runs intermittently. `PI3B_HEADED_OS_ASSESSMENT.md` §2 works
through the mechanism; the short version is that on a Pi 3B the SoC has two UARTs and
`dtoverlay` decides which one lands on GPIO 14/15:

| config | `/dev/ttyAMA0` is… | Failure mode |
|---|---|---|
| `dtoverlay=disable-bt` **(required)** | PL011, on the header | — works |
| Bluetooth enabled (the default) | the **Bluetooth modem** | total and clean — no responses at all |
| `miniuart-bt`, or the app pointed at `ttyS0` | mini-UART on the header | **intermittent, under load only** |

The third row is the one that costs you a week. The mini-UART derives its baud rate from the
VPU core clock, so framing errors appear only when the machine is busy — indistinguishable
from a flaky RS485 transceiver.

### 4.1 Enable the hardware UART, disable the serial login console

```bash
sudo raspi-config
#   3 Interface Options -> I6 Serial Port
#     "Would you like a login shell to be accessible over serial?"  -> NO
#     "Would you like the serial port hardware to be enabled?"      -> YES
```

Non-interactively:

```bash
sudo raspi-config nonint do_serial_hw 0      # 0 = enable hardware UART
sudo raspi-config nonint do_serial_cons 1    # 1 = disable serial login console
```

Older `raspi-config` builds have only a combined `do_serial`, which sets both at once. Either
way, **4.2 and 4.3 are the authoritative check** — verify the resulting files by hand rather
than trusting the helper.

A login console on `serial0` would sit on the same pins as the RS485 bus and inject getty
prompts into the Arduino conversation.

### 4.2 Add `disable-bt` and confirm the file by hand

```bash
grep -nE 'enable_uart|dtoverlay|dtparam=uart' /boot/firmware/config.txt
```

The `[all]` section must contain **both**:

```
enable_uart=1
dtoverlay=disable-bt
```

Add whichever is missing:

```bash
sudo sh -c 'printf "\nenable_uart=1\ndtoverlay=disable-bt\n" >> /boot/firmware/config.txt'
```

There must be **no** `dtoverlay=miniuart-bt` anywhere in the file.

### 4.3 Clear the console off the serial line

```bash
cat /boot/firmware/cmdline.txt
```

It must **not** contain `console=serial0,115200` (or `console=ttyAMA0,...`). If it does:

```bash
sudo sed -i 's/console=serial0,[0-9]* //; s/console=ttyAMA0,[0-9]* //' /boot/firmware/cmdline.txt
```

`cmdline.txt` is a **single line** — never let an edit introduce a newline.

### 4.4 Stop Bluetooth from coming back

```bash
sudo systemctl disable --now hciuart
sudo systemctl disable --now bluetooth
sudo reboot
```

### 4.5 Verify — after the reboot, before anything else

```bash
ls -l /dev/serial* /dev/ttyAMA0 /dev/ttyS0
dmesg | grep -iE 'ttyAMA|ttyS0|uart|bluetooth' | head -20
systemctl status hciuart bluetooth --no-pager | head -20
```

Expected on a correctly configured Pi 3B:

- `/dev/serial0 -> ttyAMA0`
- `/dev/ttyAMA0` exists and `dmesg` attributes it to the **PL011** (`fe201000.serial` /
  `3f201000.serial`, `uart-pl011`)
- `hciuart` and `bluetooth` both **inactive / disabled**
- nothing holds the port: `sudo fuser /dev/ttyAMA0` prints nothing

**If `/dev/serial0` points at `ttyS0`, stop and fix it before continuing.** Nothing downstream
of here will tell you it is wrong until sensor data starts looking strange under load.

### 4.6 Snapshot the known-good config

```bash
sudo cp /boot/firmware/config.txt /boot/firmware/config.txt.evolver-known-good
```

Per `PI3B_HEADED_OS_ASSESSMENT.md` §2, treat `config.txt` as **instrument configuration, not
OS configuration**. After any future package install that could touch Bluetooth or display:

```bash
diff /boot/firmware/config.txt.evolver-known-good /boot/firmware/config.txt
```

---

## Stage 5 — SD-card hygiene *(optional, ~10 minutes, recommended)*

Headless, this is not urgent the way it is for a headed box — but the underlying cost is real
either way. `run_cycle` calls `_save_state_locked()` every tick: a full JSON serialise plus
`os.fsync()` plus `os.replace()`, **8 640 times a day**, inside the engine lock
(`run_cycle` at `experiment_engine.py:1379`, its unconditional `_save_state_locked()` at `:1700`, the `os.fsync` at `:3249`). An SD-backed swapfile on the same card puts
pageout in front of that fsync whenever memory gets tight.

Move swap to compressed RAM, where the four mostly-idle cores pay for it instead of the card:

```bash
sudo apt install -y zram-tools
sudo sed -i 's/^#\?ALGO=.*/ALGO=zstd/; s/^#\?PERCENT=.*/PERCENT=50/' /etc/default/zramswap
sudo systemctl restart zramswap

sudo dphys-swapfile swapoff
sudo systemctl disable dphys-swapfile

echo 'vm.swappiness=100' | sudo tee /etc/sysctl.d/99-zram.conf   # zram wants HIGH, unlike disk swap
sudo sysctl --system

swapon --show          # expect /dev/zram0, ~450 MB, and no /var/swap
```

While you are here, `evolver.service` sets no scheduling or OOM protection at all
(assessment §5.5). On a headless card there is nothing to compete with it, so this is
defensive rather than necessary — but it is free, and it belongs in a commit rather than a
hand-edit on the Pi (see "Known issues" below for why hand-edits to tracked files are a trap).

---

## Stage 6 — Get the code onto the card

The clone directory **must** be `/home/pi/evolver-gui` — `install.sh` refuses to run anywhere
else and `evolver.service` hardcodes it. Note that this differs from the GitHub repo name, so
name it explicitly:

```bash
cd /home/pi
git clone https://github.com/yiannis-scotiniadis/eVOLVER_FileStruct.git evolver-gui
cd evolver-gui
git checkout pilot-v0.1.5
git describe --tags                      # pilot-v0.1.5
```

If the repo is private, either use a personal access token for the HTTPS clone, or give this
card its own SSH deploy key:

```bash
ssh-keygen -t ed25519 -C "evolver-001-$(date +%F)" -f ~/.ssh/id_ed25519 -N ""
cat ~/.ssh/id_ed25519.pub                # add as a read-only Deploy Key on the GitHub repo
git clone git@github.com:yiannis-scotiniadis/eVOLVER_FileStruct.git evolver-gui
```

**Deploy by tag, never by branch.** `DEPLOY.md`'s update protocol depends on it: a tag is
reproducible and gives a clean rollback target; `main` is not.

Fallback with no network path to GitHub — rsync from the dev machine:

```powershell
rsync -avz --exclude='.git' --exclude='.venv' --exclude='__pycache__' --exclude='experiments/' `
    "C:\Research\BIOLOGY\PersonalResearch\EVOLVER_GUI_DEV\eVOLVER_FileStruct\" `
    pi@192.168.1.2:/home/pi/evolver-gui/
```

This loses the git history, and with it `git describe` and one-command rollback. Use it only
if you must.

---

## Stage 7 — Restore what git does not carry

Unpack the Stage 0 backup somewhere scratch, then move each piece into place.

```bash
mkdir -p ~/restore && tar xzf ~/evolver-001-card-<date>.tar.gz -C ~/restore
```

### 7.1 The lab notebook

```bash
rsync -a ~/restore/home/pi/evolver-gui/experiments/ /home/pi/evolver-gui/experiments/
ls /home/pi/evolver-gui/experiments/ | wc -l          # same count as the old card
```

`logs/` is optional — restore it if you want log continuity, skip it if you want a clean slate.

### 7.2 Calibration runtime state

The tracked files (`temp_calibration.txt`, `OD_cal.txt`, `current.json`, `od/`, `temperature/`)
came with the clone. Restore only the two gitignored pieces:

```bash
rsync -a ~/restore/home/pi/evolver-gui/calibration/_sessions/ \
         /home/pi/evolver-gui/calibration/_sessions/ 2>/dev/null || true
cp ~/restore/home/pi/evolver-gui/calibration/reconciliation_log.json \
   /home/pi/evolver-gui/calibration/ 2>/dev/null || true
```

### 7.3 Verify calibration is *this rig's* — do not skip

```bash
head -1 calibration/temp_calibration.txt
```

Must be sixteen **negative** floats, and specifically these:

```
-0.10267,-0.11112,-0.11212,-0.11394,-0.11,-0.10994,-0.11082,-0.11276,-0.1097,-0.11088,-0.11312,-0.11347,-0.1092,-0.11023,-0.10947,-0.11273
```

A **positive** slope means the inverted-`xr` convention is broken and the system would drive
every heater the wrong way. `xr` is a closed-loop setpoint, not a PWM: lower `xr` = hotter, and
`xr=0` requests ~82 °C. **Abort and fix before continuing.**

```bash
cat calibration/current.json      # od + temperature: "2026-08-20T134535Z"; pump: null; stir: null
git status --short calibration/   # MUST be clean
```

If `git status` shows `calibration/` as modified after the restore, the restored values
disagree with the tag. Work out which is right — the versioned JSON envelopes under
`calibration/od/` and `calibration/temperature/` are the source of truth and the `.txt` files
are a **derived view** regenerated from `current.json`. Never hand-edit the `.txt` files.

### 7.4 The Flask secret

Two choices, both fine:

- **Restore it** (`sudo install -m600 -o root -g root ~/restore/etc/evolver/secret.env /etc/evolver/secret.env`) — existing browser sessions survive the swap.
- **Let `install.sh` generate a new one** — everyone gets logged out once. No data implication.

If you restore it, do so *before* Stage 8 — `install.sh` keeps an existing file and only
generates when one is missing.

### 7.5 SSH keys

```bash
mkdir -p ~/.ssh && chmod 700 ~/.ssh
cat ~/restore/home/pi/.ssh/authorized_keys >> ~/.ssh/authorized_keys
sort -u -o ~/.ssh/authorized_keys ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

Your laptop will warn about a changed host key on the next connect — expected, it is a new
card. Clear the old entry: `ssh-keygen -R 192.168.1.2`.

---

## Stage 8 — Run the installer

```bash
cd /home/pi/evolver-gui
chmod +x install.sh
./install.sh
```

It prints six steps. What each one means:

1. **Picks the interpreter** — prefers system `python3` if ≥ 3.10, else `/opt/python3.11`. On this card it should say 3.11 or 3.13; if it announces `/opt/python3.11` something is wrong.
2. **Creates `.venv/`** and installs `requirements.txt` (flask 3.1.3, flask-socketio 5.6.1, pyserial 3.5, numpy 2.x) via piwheels + PyPI. Slowest step; a few minutes on a Pi 3B.
3. **Adds `pi` to `dialout`** — required for `/dev/ttyAMA0`.
4. **Creates `/var/log/evolver`** (vestigial; the unit logs to the journal now).
5. **Generates `/etc/evolver/secret.env`** if absent, mode 0600, root-owned.
6. **Installs `evolver.service`** and runs `daemon-reload` — it does **not** start it.

Then log out and back in so `dialout` takes effect:

```bash
exit
ssh pi@192.168.1.2
id                          # must list dialout
ls -l /home/pi/evolver-gui/.venv/bin/python*
```

`install.sh`'s closing message mentions `supervisorctl stop all`. **That is legacy** — there is
no supervisor on this card. Confirm and move on:

```bash
sudo fuser /dev/ttyAMA0     # must print nothing
```

---

## Stage 9 — Tailscale

```bash
curl -fsSL https://tailscale.com/install.sh | sh
```

**Keep the old node identity** (preferred — same Tailscale IP, same MagicDNS name, no
re-enrolment of peers):

```bash
sudo systemctl stop tailscaled
sudo install -m600 -o root -g root \
    ~/restore/var/lib/tailscale/tailscaled.state /var/lib/tailscale/tailscaled.state
sudo systemctl start tailscaled
tailscale status
```

**Or enrol fresh** — in which case **delete the old node in the Tailscale admin console
first**, otherwise the new one is named `<hostname>-1` and every saved MagicDNS name in the
lab breaks:

```bash
sudo tailscale up --hostname=<the name from Stage 0.3>
```

Then the keepalive, which exists because Yale Wi-Fi's symmetric NAT makes the Pi's
reachability per-peer and idle paths go stale:

```bash
cd /home/pi/evolver-gui/deploy/ts-keepalive
sudo install -m 0755 ts-keepalive-pingall.sh /usr/local/bin/ts-keepalive-pingall.sh
sudo install -m 0644 ts-keepalive.service /etc/systemd/system/ts-keepalive.service
sudo install -m 0644 ts-keepalive.timer   /etc/systemd/system/ts-keepalive.timer
sudo systemctl daemon-reload && sudo systemctl enable --now ts-keepalive.timer
systemctl status ts-keepalive.timer --no-pager
```

A brand-new peer may need a one-time `tailscale ping <device>` from the Pi before the Pi's
netmap learns it. **If this card is on wired Ethernet, you can skip the keepalive entirely** —
wired removes the symmetric NAT, which is the real fix, and the timer costs ~15 min/day of
Python interpreter startup for nothing (assessment §7.2).

---

## Stage 10 — Mock validation *(proves the install before you touch serial)*

```bash
cd /home/pi/evolver-gui
.venv/bin/python server/app.py --mock
```

From a browser at `http://192.168.1.2:5000`, confirm:

- the 4×4 vial grid renders
- temperature updates ~every 10 s, OD ~every 60 s (the OD tile shows its own age)
- a fresh page load paints last-known values immediately rather than waiting
- the log says **`loaded calibration from /home/pi/evolver-gui/calibration`**, not the fallback warning

`Ctrl+C` to stop.

> **If the dashboard renders but sits on "Connecting…" forever**, that is not a broken install.
> `frontend/templates/index.html:2457` loads `socket.io` from `https://cdn.socket.io/`, there is
> no vendored copy, and the bare `const socket = io({...})` at `:6064` has no `typeof io` guard — so no CDN means no WebSocket and a
> dead page (assessment §1). Check the Pi's egress. See "Known issues" for the real fix.

---

## Stage 11 — Hardware validation

Full procedure: `DEPLOY.md` Phase 4. Below is what a **card rebuild** specifically demands,
and what it honestly does not.

### Mandatory — the serial path is new even though the rig is not

**P4.1 — real server, foreground, not systemd yet**

```bash
cd /home/pi/evolver-gui
.venv/bin/python server/app.py
```

Look for `loaded calibration from …`, `sensor loop started (interval=10.0s)`, and
`watchdog started`. If it says `calibration files not found`, abort — the path is wrong.

**P4.2 — sensor sanity (read-only). This is your UART proof.**

- 16 temperatures in [18, 30] °C, not all identical, not NaN
- 16 ODs near 0 on empty vials

Read the failure modes against Stage 4: *nothing at all* means `disable-bt` did not take and
`ttyAMA0` is the Bluetooth modem. *Values that are fine now and garbage under load* means you
are on the mini-UART. *Identical or NaN across many vials* is wiring or dead Arduinos.

**P4.3 — heater convention probe, single vial.** Do this even though the calibration file is
restored and unchanged. You have proven the file is right; you have not proven this card drives
it right. Vial 0, probe thermometer in:

1. set 22 °C — no-op at room temp
2. wait 30 s — must not climb
3. set 30 °C — wait 5 min, must climb toward 30, **not** toward 80
4. **Emergency Stop** — heaters must stop driving within 10 s and the vial start cooling within 2 min

If it heats past 40 °C at step 3, **emergency stop immediately.**

**P4.6 (short form) — one water vial, 15 minutes.** Worth running on a rebuild even though the
controller logic is unchanged, because it is the only thing that exercises the CSV write path
on a brand-new filesystem. Vial 0, 25 ml water, turbidostat, `lower_thresh` 0.05 /
`upper_thresh` 0.10 (water never reaches it), 30 °C, stir 8. Confirm
`experiments/<name>/vial00_temp.csv` accumulates rows every 10 s and `vial00_OD.csv` every
60 s (differing row counts between the two are expected — separate files, separate timestamp
columns, nothing joins them by index), and that no pump fires. Then fire a manual 5 s influx
and confirm water moves and the event is logged.

### Skippable on a rebuild — with the reason

**P4.4 stir mapping.** The logical→physical sleeve mapping lives in your notebook, not on the
card. Nothing in the rebuild can have changed it. Spot-check one vial to confirm the bus
addresses the right hardware, then stop.

**P4.5 full pump sweep.** Flow rates are not on the card either — and per `CLAUDE.md`, Tier 2
gravimetric pump calibration has still never been run, so what is in use is the hardcoded
16-value default broadcast to both directions regardless. Sample two or three pumps as a
plumbing sanity check. This is not the moment to fix that; see below.

---

## Stage 12 — Enable autostart and prove it survives a reboot

```bash
sudo systemctl enable --now evolver
sudo systemctl status evolver --no-pager
sudo journalctl -u evolver -f
```

```bash
sudo reboot
# wait ~60 s
curl -s http://192.168.1.2:5000/api/health | head -c 400; echo
```

The dashboard must come back on its own. This is the last thing that distinguishes "it ran once
when I typed the command" from "it is an instrument."

---

## Stage 13 — Baselines and gates *(do this while the card is fresh)*

### 13.1 Pin the numbers the assessment could only estimate

`PI3B_HEADED_OS_ASSESSMENT.md` scales x86 measurements to the Pi by a **guessed ×7**. A new
card with a new filesystem is exactly when to replace the guess with a measurement:

```bash
cd /home/pi/evolver-gui
.venv/bin/python server/bench_growth_rate.py     # pins the scalar-throughput factor
.venv/bin/python server/bench_read_paths.py      # SD-card I/O does not extrapolate from x86
vcgencmd measure_temp; vcgencmd get_throttled    # expect throttled=0x0
df -h /; free -m; swapon --show
```

Record all of it in `ROADMAP.md` or a session note. The read-path numbers in particular will
differ from the old card and are the reference for any future "is the Pi getting slower"
question.

### 13.2 Standing gates

```bash
# after ANY package install that could touch Bluetooth, display, or firmware:
diff /boot/firmware/config.txt.evolver-known-good /boot/firmware/config.txt
ls -l /dev/serial* /dev/ttyAMA0
systemctl status hciuart bluetooth --no-pager | head

# before every run:
head -1 /home/pi/evolver-gui/calibration/temp_calibration.txt   # slopes still negative
git -C /home/pi/evolver-gui describe --tags                     # the tag you think you deployed
git -C /home/pi/evolver-gui status --short                      # clean
```

### 13.3 Optional — the framebuffer console

`LOCAL_CONSOLE_OPTIONS.md` recommends the TUI (`evolver_console.py`, 50 MB, no graphics stack)
over a desktop, and a headless card is exactly the right substrate for it: it is a pure API
client, it adds no safety surface, and installing it pulls in nothing that could undo Stage 4 —
which is precisely what a desktop install risks. Its dependencies are **not** in
`requirements.txt`:

```bash
.venv/bin/pip install textual "python-socketio[client]" requests plotext
.venv/bin/python evolver_console.py --host 127.0.0.1 --port 5000     # also works over SSH
```

To put it on `tty1` at the bench:

```bash
sudo systemctl edit getty@tty1     # ExecStart=-/sbin/agetty --autologin pi %I $TERM
# in ~/.bash_profile:
[ "$(tty)" = /dev/tty1 ] && exec /home/pi/evolver-gui/.venv/bin/python /home/pi/evolver-gui/evolver_console.py
```

`Ctrl-Alt-F2` still gives you a real root shell with nothing in the way. **Do not install a
desktop environment** — you already have six framebuffer terminals for free, and the DE carries
the `dtoverlay` risk from Stage 4 for no benefit.

---

## Known issues this rebuild does *not* fix

A fresh card is a clean OS, not a clean codebase. These ship with `pilot-v0.1.5`:

> Line numbers below are against `pilot-v0.1.5` as checked out today. They drift with every
> commit — and the ones quoted in `PI3B_HEADED_OS_ASSESSMENT.md` are against `b5e4619` and no
> longer line up. The **symbol name** is the durable reference; grep for it.

| # | Issue | Where | Impact on a fresh card |
|---|---|---|---|
| 1 | **`socket.io` loaded from a public CDN**, no vendored copy, no `typeof io` guard | `index.html:2457`, `:6064` | Dashboard is unusable without internet egress. Assessment §1. |
| 2 | **`calibration/` is tracked in git** | `.gitignore` | A future `git checkout <tag>` can overwrite this rig's calibration; re-calibrating on the Pi without committing blocks the next checkout. `DEPLOY.md` standing warning. |
| 3 | **`state.json` fsync every tick**, inside the engine lock | `run_cycle` `experiment_engine.py:1379` → `:1700`; `os.fsync` `:3249` | 8 640 forced commits/day to the card. Wants on-change + 60 s heartbeat. Assessment §5.1. |
| 4 | **`_pump_log_cache` never trimmed**, scans the whole experiments tree | `calibration_service.py:225`, `:621` | Dormant while `current.json` has `"pump": null`. **Arms itself the moment Tier 2 pump calibration lands** — bound it *before* that bench session, not after. Assessment §7.1. |
| 5 | **Plots "All" range** is O(n) across 16 vials in parallel | `get_data` `experiment_engine.py:1934` (see the comment at `:1986`), `index.html:4833` | ~8 min of Pi CPU from one click on a 7-day run. Assessment §5.2. |
| 6 | **`evolver.service` has no `Nice` / `OOMScoreAdjust` / `MemoryMin`** | `evolver.service` | Harmless headless; matters the moment anything else shares the box. Assessment §5.5. |
| 7 | **Werkzeug dev server** with `allow_unsafe_werkzeug=True` | `app.py:2542` | Fine for 1–3 clients. **Do not "fix" it with waitress** — measured to silently break WebSocket and downgrade every client to long-polling. Moving off it means an async-mode change. Assessment §6. |
| 8 | **Over-temp detection is a 3-tick debounce** on a loop whose period is `max(10 s, work)` | `_handle_heater_safety_locked` `experiment_engine.py:2783` | A silent RS485 bus can stretch one tick to 30–45 s, and the debounce to three times that. Worth attention on its own merits. Assessment §4. |

**Fix #1 and #2 as commits from the dev machine, then deploy a new tag — never as hand-edits on
the Pi.** Editing a tracked file in place leaves the working tree dirty, which makes the next
`git checkout <tag>` fail or clobber the change. That is the same trap `DEPLOY.md` documents
for calibration, and it applies identically here. The `socket.io` fix is two lines:

```bash
# on the dev machine, on a branch
curl -o frontend/static/js/socket.io.min.js https://cdn.socket.io/4.7.5/socket.io.min.js
sed -i 's#https://cdn.socket.io/4.7.5/socket.io.min.js#/static/js/socket.io.min.js#' \
    frontend/templates/index.html
# add a `typeof io === "undefined"` guard, run the suites, tag pilot-v0.1.6, deploy per DEPLOY.md
```

---

## Abort criteria

Stop and reassess on any of these:

- `/dev/serial0` resolves to `ttyS0`, or `dmesg` does not attribute `ttyAMA0` to the PL011 *(Stage 4.5)*
- `hciuart` or `bluetooth` active after the reboot *(Stage 4.5)*
- First row of `temp_calibration.txt` is not the sixteen negative slopes in Stage 7.3
- `git status --short` on `calibration/` is dirty after restore *(Stage 7.3)*
- Server logs `calibration files not found` *(P4.1)*
- Sensor NaN for > 3 consecutive ticks on hardware *(P4.2)*
- A vial heats toward 80 °C when commanded to 30 °C *(P4.3)* — **emergency stop immediately**
- Emergency stop does not park heaters at 4095 within 10 s *(P4.3)* — do not put live cells on the platform
- Watchdog fires during validation

---

## Checklist

```
[ ]  0.1  no experiment running; `systemctl stop evolver` completed cleanly
[ ]  0.2  backup tarball taken AND copied off the Pi
[ ]  0.3  tag, timezone, hostname, Tailscale identity, `git status` recorded
[ ]  0.4  old card labelled and shelved as rollback
[ ]  1    Lite 64-bit, >=32 GB, username `pi`, hostname + timezone match old card
[ ]  2    apt full-upgrade; git + python3-venv installed; python3 >= 3.10; aarch64
[ ]  3    static 192.168.1.2 via nmcli; router and internet reachable
[ ]  4.1  serial hardware ON, serial login console OFF
[ ]  4.2  config.txt has enable_uart=1 AND dtoverlay=disable-bt; no miniuart-bt
[ ]  4.3  cmdline.txt has no console=serial0
[ ]  4.4  hciuart + bluetooth disabled; rebooted
[ ]  4.5  /dev/serial0 -> ttyAMA0 (PL011); fuser prints nothing
[ ]  4.6  config.txt.evolver-known-good snapshot taken
[ ]  5    zram active, dphys-swapfile disabled            (optional)
[ ]  6    cloned to /home/pi/evolver-gui; `git describe` == pilot-v0.1.5
[ ]  7.1  experiments/ restored, directory count matches
[ ]  7.2  calibration/_sessions/ + reconciliation_log.json restored
[ ]  7.3  temp_calibration.txt row 1 == the 16 negative slopes; git status clean
[ ]  7.4  /etc/evolver/secret.env restored or generated
[ ]  7.5  authorized_keys restored; laptop known_hosts cleared
[ ]  8    install.sh completed; `id` lists dialout after re-login
[ ]  9    Tailscale up with the old identity; ts-keepalive enabled (or skipped, wired)
[ ] 10    --mock: grid renders, cadences correct, "loaded calibration" in log
[ ] 11    P4.1 clean start / P4.2 sensor sanity / P4.3 heater probe + e-stop / P4.6 water run
[ ] 12    systemctl enable --now evolver; survives a reboot
[ ] 13.1  bench_growth_rate.py + bench_read_paths.py + vcgencmd baselines recorded
[ ] 13.3  evolver_console.py on tty1                      (optional)
```

---

## Related documents

- `DEPLOY.md` — operator runbook: Phase 4/5 validation detail, update-by-tag protocol, rollback, operations
- `CLAUDE.md` — hardware facts, RS485 protocol, the inverted-`xr` convention, the six facts
- `PI3B_HEADED_OS_ASSESSMENT.md` — §1 CDN dependency, §2 the UART hazard, §5 what breaks first, §8 the budgets
- `LOCAL_CONSOLE_OPTIONS.md` — why the TUI and not a desktop
- `CALIBRATION_PROTOCOL.md` — the Tier 1/2/3 procedures none of which this rebuild performs
- `SPEC.md` — §8.1 read paths, §19 calibration provenance, §20.1 logging
