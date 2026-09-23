# LOCAL_CONSOLE_OPTIONS.md — ways to drive the eVOLVER from a monitor plugged into the Pi

Companion to `PI3B_HEADED_OS_ASSESSMENT.md`, which asks *"can the current software survive a
desktop?"* This asks the better question: **what should the local console actually be?**

The headline from the assessment is the thing to design around: **Chromium is roughly three
times the size of the control server it exists to display** (~420 MB measured across its process
tree, against ~110 MB for the server). Every option below is a different answer to "how much of
that can we not pay?"

A second observation reframes the request. "Minimal desktop environment, terminal, and web
browser" is really **two separate needs**:

1. *A terminal at the bench.* Linux already gives you six, for free, on the framebuffer console
   — `Ctrl-Alt-F1` through `F6`, no desktop, no compositor, no X. This need costs **nothing**
   and is already satisfied by the OS you have.
2. *Eyes on the instrument.* This is the part that costs money, and a web browser is the most
   expensive way to buy it.

Separating them is most of the win.

---

## The options, cheapest first

All totals are for a Pi 3B with **905 MB usable**, and all include the unavoidable base:
Debian + systemd + sshd (60–90 MB) + `tailscaled` (30–45 MB) + the eVOLVER server (105–120 MB)
= **195–255 MB before anything is displayed at all**.

| # | Approach | Adds | System total | Headroom | Extra disk |
|---|---|---|---|---|---|
| **E** | Display on a **separate device** | 0 | 195–255 | 650–710 | 0 |
| **D** | **TUI on the framebuffer console** | **50** *(measured)* | **245–305** | **600–660** | ~15 MB |
| **C** | `cog` / WPE WebKit on DRM, no compositor | 110–180 *(est.)* | 305–435 | 470–600 | ~150–250 MB |
| **B** | `cage` kiosk compositor + Chromium | 290–400 | 485–655 | 250–420 | 1.1–1.6 GB |
| **A** | Full desktop + Chromium *(the original plan)* | 355–515 | 550–770 | 135–355 | 1.2–1.8 GB |

Recall from the assessment that the root filesystem has **3.1 GB free of 6.9 GB**, and that the
server's own low-disk warning trips below 1 GB or 10 % free. Options A and B put you near that
line permanently; C and D do not touch it.

---

## D — A TUI on the framebuffer console *(recommended, and prototyped)*

Boot to a plain TTY. No X, no Wayland, no compositor, no GPU, no CMA, no browser. Autologin on
`tty1` runs a text UI that talks to **the same REST + WebSocket API the web GUI uses**, so it
carries no control logic and adds no safety surface — it is a client, exactly like the browser.

I built one and measured it against a live 16-vial mock run (`evolver_console.py`, 132 lines):

| | |
|---|---|
| **RSS** | **50.0 MB** (Python + textual + python-socketio + requests) |
| **CPU** | **0.6 % of one x86 core** while live |
| Install size | ~15 MB (`textual` 7.3 MB + `rich` 3.0 MB + `plotext` 2.9 MB) |
| Graphics stack required | **none** |

It renders a 16-vial table (OD, temperature, per-vial state), the experiment status bar, a live
alerts pane fed by the `alert` socket event, a braille OD trace for the selected vial, and an
`e` key bound to `POST /api/actuators/emergency_stop`. Screenshot: `evolver_console.svg`.

**Why this is the strongest option here, beyond the memory number:**

- **It sidesteps the whole `dtoverlay` hazard.** §2 of the assessment — a desktop install
  re-enabling Bluetooth and moving `/dev/ttyAMA0` out from under the RS485 link — is a risk you
  take on *because you install desktop packages*. Installing `textual` does not pull in
  `bluez`, `hciuart`, or a display manager. The risk largely evaporates.
- **It is also the remote tool.** The same binary over SSH, unchanged, from a laptop or a phone.
  You get the bench console and the 2 a.m. "is vial 7 still alive" check from one thing.
- **You can shrink CMA.** With no display pipeline you can drop `gpu_mem` to the minimum and
  recover a slice of the 256 MB currently reserved. Options A–C need it.
- **Thermals stay where they are.** No renderer means no sustained all-core load, so §5.4's
  throttling question does not arise.
- **The framebuffer console is not a fallback, it is a feature.** `Ctrl-Alt-F2` is a real root
  shell for when something is wrong, with no window manager between you and it.

**Honest limitations.** No real plots — a braille sparkline answers "is this vial climbing?"
but not "what did the last 24 h look like?" (`plotext` renders respectable line charts in a
terminal if you want more). No calibration wizards; those stay in the web GUI, which is the
right place for them anyway since they are done at a workstation, not one-handed at the bench.
And it is code you now own, though at 132 lines against a stable API that is a small tax.

**Deploy sketch:**

```bash
sudo systemctl edit getty@tty1        # ExecStart=-/sbin/agetty --autologin pi %I $TERM
# ~/.bash_profile on tty1 only:
[ "$(tty)" = /dev/tty1 ] && exec python3 /home/pi/evolver-gui/evolver_console.py
```

Run it under a systemd unit with `Restart=always` if you want it to survive its own crashes, and
give it `OOMScoreAdjust=100` so it dies before the control server does.

---

## C — `cog` / WPE WebKit, if you want the real web GUI locally

WPE WebKit is a WebKit port built for embedded devices: it renders straight to DRM/KMS with **no
X, no Wayland, no desktop**, and `cog` is its single-page kiosk launcher. This is what digital
signage and set-top boxes use, and it is the right tool if the requirement is genuinely "the
actual dashboard, pixel for pixel, on the bench monitor".

**The GUI is compatible.** I scanned `index.html` for engine-sensitive features: it uses CSS
grid, `gap`, `inset` and `accent-color`, one `ResizeObserver`, and Canvas 2D via uPlot. No
`:has()`, no container queries, no `backdrop-filter`, no WebGL, no `localStorage`, no clipboard
API, no exotic JS. Nothing here is a WebKit risk.

**But verify the numbers yourself — I could not.** The 110–180 MB in the table is an estimate,
not a measurement; I had no ARM box and no WebKit build to test against. Before committing:

```bash
apt-cache policy cog wpewebkit-2.0 cog-wl   # confirm it is packaged for trixie arm64
sudo apt install cog
cog --platform=drm http://localhost:5000
ps -eo rss,comm --sort=-rss | head          # the number that decides this
```

If `cog` measures under ~200 MB, this is a very strong second place: you keep the exact GUI, pay
about a third of Chromium, install ~200 MB instead of 1.5 GB, and still avoid a desktop
environment. If it measures near Chromium, fall back to D or E.

---

## B — `cage` + Chromium, if you must have Chromium

`cage` is a Wayland kiosk compositor — one fullscreen application, no panel, no greeter, no file
manager. It saves the 60–110 MB of desktop environment from option A and a chunk of the disk,
but it does not touch the number that matters, because Chromium is the number that matters.

Worth it only if something in the GUI turns out to need Blink specifically. Nothing I found does.

---

## E — Put the display on a different machine

The cleanest architectural answer: **the instrument controller should not also be the
workstation.** Leave the Pi doing exactly what it does today and hang the display off something
else pointed at `http://192.168.1.2:5000`:

- **An old tablet or laptop** on a stand — free, full browser, zero cost to the instrument.
- **A second Pi (Zero 2 W / Pi 4) as a dedicated panel** — £15–£60, and its problems are its own.
- **A cheap HDMI thin client / mini PC** if a permanent bench station is wanted.

This is the only option with literally no effect on the control loop, and it composes with D:
a TUI on the Pi's own console for when you are hands-on at the machine, and a browser on a
nearby screen for plots and wizards.

---

## Worth adding regardless of which you pick

**A physical emergency stop.** A GPIO button wired to a tiny always-running service that calls
`POST /api/actuators/emergency_stop` — plus an I²C OLED showing experiment name, elapsed time,
and worst-vial OD. `DEPLOY.md`'s abort criteria already treat emergency stop as the critical
safety action, and today reaching it requires a working display stack, a working browser, and a
working network. A button does not. This is a couple of hours of work and it is the single
highest-value thing on this page for a machine holding live cultures unattended.

**Fix the CDN dependency first.** §1 of the assessment applies to options A, B and C equally —
none of them can load the GUI without internet until `socket.io.min.js` is vendored. Option D is
the only one immune, because it never loads the page.

---

## Recommendation

1. **Vendor `socket.io.min.js`** — required by everything except D, and a 15-minute fix.
2. **Do not install a desktop environment.** You already have framebuffer terminals; the DE buys
   nothing and carries the `dtoverlay` risk.
3. **Deploy the TUI on `tty1`** (option D) as the bench console. 50 MB, no graphics stack, and
   it doubles as your SSH and phone tool.
4. **Measure `cog`** (option C). If it comes in under ~200 MB, add it as a second TTY or a
   keybind for when you want the real dashboard at the bench.
5. **Keep the web GUI as the workstation interface** (option E) — plots, wizards, calibration.
   That is what it is good at, and a laptop is where that work actually happens.
6. **Add the physical e-stop button** independently of all of the above.

Total cost to the instrument: **~50 MB and no graphics stack**, against 355–515 MB and a
1.5 GB install for the original plan.
