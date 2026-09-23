#!/usr/bin/env python3
"""server/capture_fluidics_ack.py — what does the fluidics Arduino actually say?

RUN ON THE PI, with the server stopped. Reads and writes the RS485 bus
directly; imports nothing from the server package and needs only pyserial.

Why this exists
---------------
``SerialManager.pump_command`` (``serial_manager.py:604``) writes an ``st``
frame and returns without reading anything back. Three pieces of 2016 evidence
say the fluidics Arduino answers anyway:

  * ``Fluidic_status()`` (``rpi_original/evolver_UPD.py:58``) writes ``re !``
    and greps the reply for ``'ready'`` — there is a ready/busy handshake.
  * ``rpi_original/extras/pump_rs485.py:12`` writes an ``st`` frame, then
    ``readline()``s and prints the result as ``"Completed Command:"``.
  * ``CLAUDE.md``: "Arduinos may acknowledge but the exp_manager never reads a
    response for these."

If that is true, every manual pump leaves bytes in the Pi's input buffer. The
reader never resyncs the stream — ``flushInput()`` is called exactly once, at
construction (``serial_manager.py:128``) — so the next ``read_until(b"end")``
returns ``<ack>temp…end``, fails its ``line[:4] == "temp"`` check, and yields
16 NaNs. Three of those in a row drives ``BusHealth`` to ``down`` and fires the
critical "RS485 bus silent" alert, which is the opposite of what happened: the
bus was not silent, it was talking out of turn.

That chain is reproducible in software against the real ``SerialManager``. What
is NOT known, because the 2016 firmware source is unavailable, is the first
link: whether the fluidics Arduino emits anything at all, what bytes, and how
long after the command. This script measures it.

What it does
------------
1. Opens the port, flushes, and listens for a few seconds with no traffic — is
   the bus quiet when nobody is talking?
2. Fires ONE fluidics command (by default a bare ``st !`` status poll, which
   moves no liquid) and timestamps every byte that arrives for the next 70 s.
3. Partway through that window, sends one ``xr`` temperature frame and shows
   whether the ``read_until(b"end")`` the server would have done at that
   moment parses or is rejected. This is the actual hypothesis test.
4. Prints a verdict, and writes the raw capture plus the transcript to disk.

Usage
-----
Stop the server first — ``/dev/ttyAMA0`` admits one process::

    sudo systemctl stop evolver

Then, from ``/home/pi/evolver-gui``::

    # safest: status poll only, no pump runs, no liquid moves
    .venv/bin/python server/capture_fluidics_ack.py

    # fire a real pump (vial 3 influx, 5 s) and watch for the completion ack
    .venv/bin/python server/capture_fluidics_ack.py --fire 3:influx:5 --yes

    # reproduce the sterilisation burst: 4 commands 50 ms apart, which is what
    # the server's MIN_INTER_COMMAND_SECONDS actually allows
    .venv/bin/python server/capture_fluidics_ack.py \
        --fire 3:influx:5 --repeat 4 --spacing 0.05 --yes

    # validate the tool with no hardware attached
    .venv/bin/python server/capture_fluidics_ack.py --self-test

Restart the server when done: ``sudo systemctl start evolver``.

Safety
------
* The ``xr`` probe sends **4095** to all 16 vials, which is heaters OFF.
  Under the inverted convention 0 would request ~82 °C (``CLAUDE.md``,
  Testing). It leaves them parked off; the server re-asserts setpoints when it
  restarts. ``--no-xr`` skips the probe entirely.
* Stir and OD are never touched.
* ``--fire`` runs a real peristaltic pump and moves real liquid. It requires an
  explicit ``--yes``. The default probe does not fire anything.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

try:
    import serial as pyserial
except ImportError:  # let --self-test run on a machine without pyserial
    pyserial = None


# --- Protocol constants (CLAUDE.md "RS485 serial protocol") -----------------
PORT = "/dev/ttyAMA0"
BAUDRATE = 9600
N_VIALS = 16
COMMAND_TERMINATOR = " !"
RESPONSE_END = b"end"
PREFIX_FLUIDICS = "st"
PREFIX_TEMPERATURE = "xr"
RESPONSE_TEMP = "temp"

# CLAUDE.md: the ONLY definitive heater "off". 0 pins the heater at maximum.
HEATER_OFF_SETPOINT = 4095

# 8N1 => 10 bits on the wire per byte. Used to report how long each frame
# actually occupied the bus, which is the number MIN_INTER_COMMAND_SECONDS
# (50 ms, serial_manager.py:247) is implicitly claiming to exceed.
BYTE_SECONDS = 10.0 / BAUDRATE


# ---------------------------------------------------------------------------
# Frame construction — mirrors SerialManager exactly
# ---------------------------------------------------------------------------

def pump_body(vial: int, direction: str, seconds: int) -> str:
    """Single-fire body, byte-identical to ``SerialManager.pump_command``.

    Influx vial N is bit N, efflux vial N is bit N+16; the mask is rendered
    with ``format(mask, 'b')``, so leading zeros are omitted exactly as the
    shipped code omits them.
    """
    bit = vial if direction == "influx" else vial + N_VIALS
    return "{0:b},0,{1},".format(1 << bit, int(seconds))


def temperature_body(setpoint: int) -> str:
    """``xr`` body, mirroring ``SerialManager._format_csv``."""
    return ",".join(str(int(setpoint)) for _ in range(N_VIALS)) + ","


def frame(prefix: str, body: str) -> bytes:
    return (prefix + body + COMMAND_TERMINATOR).encode("ascii")


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------

class BusReader(threading.Thread):
    """Timestamps every byte that arrives, one byte at a time.

    At 9600 baud a byte is ~1.04 ms, so per-byte timestamping is comfortably
    within reach and gives the inter-frame gaps we need to tell an Arduino's
    reply apart from a later unsolicited ack.
    """

    def __init__(self, ser, t0: float) -> None:
        super().__init__(name="bus-reader", daemon=True)
        self.ser = ser
        self.t0 = t0
        self.events: list[tuple[float, bytes]] = []
        self._stop_evt = threading.Event()  # not _stop: Thread._stop is internal

    def run(self) -> None:
        while not self._stop_evt.is_set():
            try:
                chunk = self.ser.read(1)
            except Exception as exc:  # port yanked mid-capture
                self.events.append((time.monotonic() - self.t0, b""))
                print("  ! reader stopped: %s" % exc)
                return
            if chunk:
                self.events.append((time.monotonic() - self.t0, chunk))

    def stop(self) -> None:
        self._stop_evt.set()

    @property
    def stream(self) -> bytes:
        return b"".join(c for _, c in self.events)


def group_bursts(events, gap: float) -> list[dict]:
    """Split the byte stream into bursts separated by ``gap`` seconds of
    silence. A burst is the unit an operator can reason about: one frame, or
    one collision fragment."""
    bursts: list[dict] = []
    cur = None
    for t, b in events:
        if not b:
            continue
        if cur is None or (t - cur["t_end"]) > gap:
            cur = {"t_start": t, "t_end": t, "data": bytearray()}
            bursts.append(cur)
        cur["t_end"] = t
        cur["data"] += b
    return bursts


def hexdump(data: bytes, indent: str = "        ", width: int = 16) -> str:
    out = []
    for off in range(0, len(data), width):
        row = data[off:off + width]
        hexpart = " ".join("%02x" % b for b in row)
        txt = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
        out.append("%s%04x  %-*s  |%s|" % (indent, off, width * 3 - 1, hexpart, txt))
    return "\n".join(out)


def simulate_read_until(stream: bytes, terminator: bytes = RESPONSE_END):
    """What ``SerialManager._read_response`` would have pulled off this stream.

    Returns ``(raw, found_terminator)``. ``found_terminator=False`` is the 5 s
    timeout case, where ``read_until`` returns whatever it had.
    """
    i = stream.find(terminator)
    if i == -1:
        return stream, False
    return stream[: i + len(terminator)], True


def verdict_on_frame(raw: bytes) -> tuple[bool, str]:
    """Apply ``_read_response``'s acceptance test verbatim (serial_manager.py:275)."""
    line = raw.decode("ascii", errors="replace").strip()
    ok = line[:4] == RESPONSE_TEMP and line[-3:] == "end"
    return ok, line


# ---------------------------------------------------------------------------
# Self-test fake — a byte stream, not a message queue
# ---------------------------------------------------------------------------

class FakeSerial:
    """In-process stand-in so ``--self-test`` can exercise the whole pipeline.

    Deliberately models the port as a **byte stream with delivery times**,
    which is the thing ``server/test_serial_manager.py``'s ``FakeSerial`` does
    not do (it pops one whole pre-framed message per ``read_until``, so
    leftover bytes are structurally unrepresentable and this class of bug is
    invisible to the suite).
    """

    def __init__(self, ack=b"ready", ack_delay=5.0, temp_delay=0.15, echo=False):
        self.timeout = 0.05
        self.write_timeout = 2.0
        self.ack = ack
        self.ack_delay = ack_delay
        self.temp_delay = temp_delay
        self.echo = echo
        self.write_log: list[bytes] = []
        self._sched: list[tuple[float, bytes]] = []
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._last_len = 0

    def reset_input_buffer(self) -> None:
        with self._lock:
            self._buf.clear()
            self._sched = []

    flushInput = reset_input_buffer

    def write(self, data: bytes) -> int:
        now = time.monotonic()
        with self._lock:
            self.write_log.append(bytes(data))
            self._last_len = len(data)
            if self.echo:
                self._sched.append((now + 0.002, bytes(data)))
            if data.startswith(b"st") and self.ack:
                self._sched.append((now + self.ack_delay, self.ack))
            elif data.startswith(b"xr"):
                body = b",".join(b"516" for _ in range(N_VIALS))
                self._sched.append((now + self.temp_delay, b"temp" + body + b",end"))
        return len(data)

    def flush(self) -> None:
        time.sleep(self._last_len * BYTE_SECONDS)

    def read(self, n: int = 1) -> bytes:
        deadline = time.monotonic() + self.timeout
        while True:
            with self._lock:
                now = time.monotonic()
                due = [s for s in self._sched if s[0] <= now]
                if due:
                    self._sched = [s for s in self._sched if s[0] > now]
                    for _, b in due:
                        self._buf += b
                if self._buf:
                    out = bytes(self._buf[:n])
                    del self._buf[:n]
                    return out
            if time.monotonic() >= deadline:
                return b""
            time.sleep(0.005)

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

def preflight(port: str, force: bool) -> bool:
    ok = True
    if not Path(port).exists():
        print("  FAIL  %s does not exist" % port)
        return False
    print("  ok    %s exists" % port)

    if shutil.which("systemctl"):
        try:
            r = subprocess.run(
                ["systemctl", "is-active", "evolver"],
                capture_output=True, text=True, timeout=10,
            )
            state = (r.stdout or r.stderr).strip()
            if state == "active":
                print("  FAIL  the evolver service is ACTIVE — it owns the port.")
                print("        sudo systemctl stop evolver")
                ok = False
            else:
                print("  ok    evolver service is %s" % (state or "not running"))
        except Exception:
            print("  ?     could not query the evolver service")

    if shutil.which("fuser"):
        try:
            r = subprocess.run(
                ["fuser", port], capture_output=True, text=True, timeout=10
            )
            if r.returncode == 0 and (r.stdout.strip() or r.stderr.strip()):
                print("  FAIL  another process holds %s: %s"
                      % (port, (r.stdout + r.stderr).strip()))
                ok = False
            else:
                print("  ok    nothing else holds %s" % port)
        except Exception:
            print("  ?     could not run fuser on %s" % port)

    if not ok and force:
        print("  !     --force given; continuing anyway (expect garbage)")
        return True
    return ok


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

class Tee:
    def __init__(self, path: Path):
        self.fh = open(path, "w", encoding="utf-8")

    def write(self, line: str = "") -> None:
        print(line)
        self.fh.write(line + "\n")
        self.fh.flush()

    def close(self) -> None:
        self.fh.close()


def parse_fire(spec: str) -> tuple[int, str, int]:
    parts = spec.split(":")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError(
            "--fire wants VIAL:DIRECTION:SECONDS, e.g. 3:influx:5"
        )
    vial, direction, seconds = parts
    vial_i = int(vial)
    if not (0 <= vial_i < N_VIALS):
        raise argparse.ArgumentTypeError("vial must be 0..%d" % (N_VIALS - 1))
    if direction not in ("influx", "efflux"):
        raise argparse.ArgumentTypeError("direction must be influx or efflux")
    seconds_i = int(seconds)
    if not (1 <= seconds_i <= 60):
        raise argparse.ArgumentTypeError("seconds must be 1..60 (whole seconds only)")
    return vial_i, direction, seconds_i


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Capture whatever the fluidics Arduino emits after an st command.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--port", default=PORT)
    p.add_argument("--baud", type=int, default=BAUDRATE)
    p.add_argument("--duration", type=float, default=70.0,
                   help="seconds to listen after the command (default 70)")
    p.add_argument("--baseline", type=float, default=5.0,
                   help="seconds of silent listening before any command (default 5)")
    p.add_argument("--fire", type=parse_fire, metavar="VIAL:DIRECTION:SECONDS",
                   help="fire a real pump instead of the bare 'st !' poll. "
                        "MOVES LIQUID. Requires --yes.")
    p.add_argument("--raw-body", default=None,
                   help="send an arbitrary fluidics body verbatim after the "
                        "'st' prefix (advanced; bypasses --fire)")
    p.add_argument("--repeat", type=int, default=1,
                   help="send the command N times (default 1) to reproduce a "
                        "queued burst")
    p.add_argument("--spacing", type=float, default=0.05,
                   help="seconds between repeats (default 0.05, the server's "
                        "MIN_INTER_COMMAND_SECONDS)")
    p.add_argument("--xr-at", type=float, default=20.0,
                   help="seconds into the window at which to send the xr probe "
                        "(default 20)")
    p.add_argument("--xr-setpoint", type=int, default=HEATER_OFF_SETPOINT,
                   help="xr setpoint for all 16 vials (default 4095 = heaters OFF; "
                        "LOWER IS HOTTER, 0 is ~82 C)")
    p.add_argument("--no-xr", action="store_true", help="skip the xr probe entirely")
    p.add_argument("--gap", type=float, default=0.25,
                   help="idle seconds that separate one burst from the next")
    p.add_argument("--out", default=None,
                   help="output directory (default: logs/ beside this repo, else cwd)")
    p.add_argument("--yes", action="store_true", help="confirm --fire")
    p.add_argument("--force", action="store_true", help="run even if preflight fails")
    p.add_argument("--self-test", action="store_true",
                   help="run against an in-process fake; no hardware needed")
    p.add_argument("--self-test-ack", default="ready",
                   help="bytes the fake Arduino emits after an st command "
                        "(empty string = silent firmware)")
    args = p.parse_args(argv)

    if args.xr_setpoint < 300:
        print("REFUSING: xr setpoint %d is hotter than ~48 C. "
              "Lower is hotter on this rig; 4095 is off." % args.xr_setpoint)
        return 2
    if args.fire and not args.yes and not args.self_test:
        print("REFUSING: --fire runs a real pump and moves liquid. Add --yes.")
        return 2

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if args.out:
        outdir = Path(args.out)
    else:
        repo_logs = Path(__file__).resolve().parent.parent / "logs"
        outdir = repo_logs if repo_logs.is_dir() else Path.cwd()
    outdir.mkdir(parents=True, exist_ok=True)
    log_path = outdir / ("fluidics_ack_%s.log" % stamp)
    bin_path = outdir / ("fluidics_ack_%s.bin" % stamp)
    out = Tee(log_path)

    # --- what we are about to send ---------------------------------------
    if args.raw_body is not None:
        cmd = frame(PREFIX_FLUIDICS, args.raw_body)
        what = "raw body %r" % args.raw_body
    elif args.fire:
        vial, direction, seconds = args.fire
        cmd = frame(PREFIX_FLUIDICS, pump_body(vial, direction, seconds))
        what = "vial %d %s for %d s (LIQUID WILL MOVE)" % (vial, direction, seconds)
    else:
        cmd = (PREFIX_FLUIDICS + COMMAND_TERMINATOR).encode("ascii")
        what = "bare status poll (no pump runs)"

    out.write("=" * 72)
    out.write("fluidics ack capture — %s" % datetime.now().isoformat(timespec="seconds"))
    out.write("=" * 72)
    out.write("port          %s @ %d baud%s"
              % (args.port, args.baud, "  [SELF-TEST, no hardware]" if args.self_test else ""))
    out.write("command       %r" % cmd)
    out.write("              %s" % what)
    out.write("              %d byte(s) = %.0f ms of wire time"
              % (len(cmd), len(cmd) * BYTE_SECONDS * 1000))
    if args.repeat > 1:
        out.write("repeats       %d, %.0f ms apart" % (args.repeat, args.spacing * 1000))
    out.write("baseline      %.1f s   window %.1f s" % (args.baseline, args.duration))
    if args.no_xr:
        out.write("xr probe      disabled")
    else:
        out.write("xr probe      at t+%.1f s, setpoint %d (%s)"
                  % (args.xr_at, args.xr_setpoint,
                     "heaters OFF" if args.xr_setpoint >= HEATER_OFF_SETPOINT
                     else "WARNING: not the off setpoint"))
    out.write("")

    # --- open -------------------------------------------------------------
    if args.self_test:
        ack = args.self_test_ack.encode("ascii")
        ser = FakeSerial(ack=ack, ack_delay=min(5.0, max(0.5, args.duration / 8)))
        out.write("self-test: fake Arduino acks %r %.1f s after the st command"
                  % (ack, ser.ack_delay))
        out.write("")
    else:
        out.write("preflight")
        if not preflight(args.port, args.force):
            out.write("")
            out.write("Aborted. Stop the server, then re-run.")
            out.close()
            return 1
        out.write("")
        if pyserial is None:
            out.write("pyserial is not installed in this interpreter.")
            out.close()
            return 1
        ser = pyserial.Serial(port=args.port, baudrate=args.baud,
                              timeout=0.05, write_timeout=2.0)

    try:
        ser.reset_input_buffer()
    except AttributeError:
        ser.flushInput()

    t0 = time.monotonic()
    reader = BusReader(ser, t0)
    reader.start()

    marks: list[tuple[float, str]] = []

    def send(payload: bytes, label: str) -> None:
        t_queued = time.monotonic() - t0
        ser.write(payload)
        try:
            ser.flush()          # block until the bytes are actually on the wire
        except Exception:
            pass
        t_done = time.monotonic() - t0
        marks.append((t_queued, "TX %s %r" % (label, payload)))
        out.write("  t=%7.3f  TX %-6s %r  (write returned at t=%.3f, "
                  "wire clear at t=%.3f, %.0f ms)"
                  % (t_queued, label, payload, t_queued, t_done,
                     (t_done - t_queued) * 1000))

    # --- phase 0: silent baseline ----------------------------------------
    out.write("phase 0 — silent baseline (%.1f s, no traffic)" % args.baseline)
    time.sleep(args.baseline)
    n_baseline = len(reader.events)
    out.write("  %d byte(s) arrived unprompted" % n_baseline)
    out.write("")

    # --- phase 1: fire ----------------------------------------------------
    out.write("phase 1 — fluidics command")
    t_cmd = time.monotonic() - t0
    for i in range(args.repeat):
        if i:
            time.sleep(args.spacing)
        send(cmd, "st")
    out.write("")

    # --- phase 2: listen, with the xr probe partway through ---------------
    out.write("phase 2 — listening %.1f s" % args.duration)
    t_xr = None
    xr_cursor = None
    deadline = time.monotonic() + args.duration
    xr_due = (None if args.no_xr
              else time.monotonic() + max(0.0, args.xr_at - (time.monotonic() - t0 - t_cmd)))
    while time.monotonic() < deadline:
        if xr_due is not None and time.monotonic() >= xr_due:
            xr_cursor = len(reader.stream)   # bytes unread at the moment of the probe
            send(frame(PREFIX_TEMPERATURE, temperature_body(args.xr_setpoint)), "xr")
            t_xr = time.monotonic() - t0
            xr_due = None
        time.sleep(0.05)
    reader.stop()
    reader.join(timeout=2.0)
    out.write("")

    stream = reader.stream
    bin_path.write_bytes(stream)

    # --- bursts -----------------------------------------------------------
    bursts = group_bursts(reader.events, args.gap)
    out.write("captured %d byte(s) in %d burst(s)  (gap threshold %.2f s)"
              % (len(stream), len(bursts), args.gap))
    out.write("")
    for i, b in enumerate(bursts):
        data = bytes(b["data"])
        rel = b["t_start"] - t_cmd
        out.write("  burst %d   t=%.3f  (%+.3f s from the st command)  "
                  "%d byte(s), spanning %.3f s"
                  % (i, b["t_start"], rel, len(data), b["t_end"] - b["t_start"]))
        out.write("      text  %r" % data.decode("ascii", errors="replace"))
        out.write(hexdump(data))
        out.write("")

    # --- verdicts ---------------------------------------------------------
    out.write("=" * 72)
    out.write("VERDICT")
    out.write("=" * 72)

    if n_baseline:
        out.write("* The bus is NOT quiet when idle: %d byte(s) arrived before any"
                  % n_baseline)
        out.write("  command was sent. Something is chattering; the desync would be")
        out.write("  continuous rather than pump-triggered.")
    else:
        out.write("* Bus quiet when idle (0 bytes in %.1f s baseline)." % args.baseline)

    echoes = [b for b in bursts if bytes(b["data"]).startswith(cmd[:4])]
    if echoes:
        out.write("* LOCAL ECHO DETECTED — a burst begins with our own command bytes.")
        out.write("  The transceiver is echoing transmissions back. Every command the")
        out.write("  server writes lands in its own input buffer; this is a different")
        out.write("  and larger problem than an Arduino ack.")

    # A burst that is a well-formed `temp...end` frame is the solicited answer
    # to our own xr probe, not something the fluidics Arduino volunteered.
    def _is_temp_frame(data: bytes) -> bool:
        line = data.decode("ascii", errors="replace").strip()
        return line[:4] == RESPONSE_TEMP and line[-3:] == "end"

    post = [b for b in bursts
            if b["t_start"] > t_cmd and not _is_temp_frame(bytes(b["data"]))]
    if not post:
        out.write("* The fluidics Arduino emitted NOTHING in %.0f s after the command."
                  % args.duration)
        out.write("  The stale-byte mechanism is ruled out for this command shape.")
        out.write("  Re-run with --fire (a completion ack may only follow a real pump")
        out.write("  run) before concluding the firmware is silent.")
    else:
        first = post[0]
        out.write("* The fluidics Arduino DOES emit unsolicited bytes "
                  "(%d burst(s), excluding the solicited xr reply)." % len(post))
        out.write("  first burst at t+%.3f s after the command: %r"
                  % (first["t_start"] - t_cmd,
                     bytes(first["data"]).decode("ascii", errors="replace")))
        if args.fire:
            _, _, secs = args.fire
            when = first["t_start"] - t_cmd
            out.write("  pump duration was %d s; the burst landed %s that, so it is a"
                      % (secs, "AFTER" if when > secs else "DURING"))
            out.write("  %s." % ("completion ack" if when > secs else "receipt ack"))
        fb = bytes(first["data"])
        out.write("  newline-terminated: %s   (b9b135a assumed NOT -- it switched"
                  % ("yes" if (b"\n" in fb or b"\r" in fb) else "NO"))
        out.write("                          readline() to read_until(b'end'))")
        out.write("  contains 'end': %s"
                  % ("yes" if RESPONSE_END in bytes(first["data"]) else "no"))
        out.write("  => every unread burst prefixes the next read_until(b'end').")

    if t_xr is not None and xr_cursor is not None:
        # Faithful model of the server's buffer state. Nothing drains the port
        # between reads, so the bytes waiting when the xr frame goes out are
        # everything that arrived since the last read consumed an 'end' -- here,
        # since capture start. read_until() therefore starts at offset 0 of the
        # captured stream and runs on into whatever the xr elicits.
        raw, found = simulate_read_until(stream)
        ok, line = verdict_on_frame(raw)
        out.write("")
        out.write("* xr probe sent at t=%.3f. Bytes already unread at that moment: %d."
                  % (t_xr, xr_cursor))
        out.write("  What SerialManager._read_response would have pulled:")
        out.write("      %r" % line)
        if not found:
            out.write("  -> no 'end' terminator within the window: the 5 s read would")
            out.write("     have TIMED OUT. All 16 vials NaN.")
        elif ok:
            out.write("  -> ACCEPTED. This probe did not desync.")
        else:
            out.write("  -> REJECTED by the `line[:4] == 'temp'` check "
                      "(serial_manager.py:275).")
            out.write("     All 16 vials NaN. One of these is 'Bus lossy'; three")
            out.write("     consecutive is the critical 'RS485 bus silent' alert.")
            out.write("     THIS IS THE BUG, MEASURED ON HARDWARE.")

    out.write("")
    out.write("raw capture   %s" % bin_path)
    out.write("transcript    %s" % log_path)
    if not args.no_xr and not args.self_test:
        out.write("")
        out.write("NOTE: heaters were left parked at %d (OFF). The server re-asserts"
                  % args.xr_setpoint)
        out.write("      setpoints on restart: sudo systemctl start evolver")
    out.close()

    try:
        ser.close()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
