#!/usr/bin/env python3
"""evolver_console.py — a local console for the eVOLVER that needs no graphics stack.

Runs on the Pi's framebuffer TTY (or over SSH, unchanged). Talks to the same
REST + WebSocket API the web GUI uses, so it carries no control logic of its own.

    pip install textual python-socketio[client] requests plotext
    python3 evolver_console.py --host 127.0.0.1 --port 5000
"""
from __future__ import annotations
import argparse, threading
from collections import deque

import requests, socketio
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Header, Footer, DataTable, Static, Log
from textual.reactive import reactive

N_VIALS = 16
TRACE = 240  # 40 min of OD history at the 10 s OD cadence (app.py OD_INTERVAL_SECONDS)


class Sparkline(Static):
    """Braille OD trace for the selected vial. No GPU, no canvas, no toolkit."""
    BARS = "▁▂▃▄▅▆▇█"

    def render_trace(self, pts, vial):
        pts = [p for p in pts if p is not None]
        if len(pts) < 2:
            return f"vial {vial:02d}  OD trace   [dim]collecting…[/dim]"
        lo, hi = min(pts), max(pts)
        span = (hi - lo) or 1e-9
        bar = "".join(self.BARS[min(7, int((p - lo) / span * 7.99))] for p in pts[-96:])
        return (f"vial {vial:02d}  OD [bold]{pts[-1]:.3f}[/bold]   "
                f"[dim]{lo:.3f}–{hi:.3f} over {len(pts)*10//60} min[/dim]\n[green]{bar}[/green]")


class EvolverConsole(App):
    CSS = """
    Screen { layout: vertical; }
    #grid { height: 1fr; }
    #side { width: 46; }
    DataTable { height: 1fr; }
    #trace { height: 5; border: round $accent; padding: 0 1; }
    #status { height: 3; border: round $accent; padding: 0 1; }
    Log { height: 1fr; border: round $accent; }
    """
    BINDINGS = [("q", "quit", "Quit"), ("up", "prev", "Prev vial"),
                ("down", "next", "Next vial"), ("e", "estop", "EMERGENCY STOP")]
    selected = reactive(0)

    def __init__(self, base):
        super().__init__()
        self.base = base
        self.hist = {v: deque(maxlen=TRACE) for v in range(N_VIALS)}
        self.latest = {"temperature": [None]*N_VIALS, "od": [None]*N_VIALS}
        self.exp = {}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="grid"):
            yield DataTable(id="vials", cursor_type="row", zebra_stripes=True)
            with Vertical(id="side"):
                yield Static(id="status")
                yield Sparkline(id="trace")
                yield Log(id="events", highlight=True)
        yield Footer()

    def on_mount(self):
        t = self.query_one("#vials", DataTable)
        self.col = dict(zip(("vial", "od", "temp", "state"),
                            t.add_columns("vial", "OD", "temp °C", "state")))
        for v in range(N_VIALS):
            t.add_row(f"{v:02d}", "—", "—", "—", key=str(v))
        self.query_one("#events", Log).write_line("connecting…")
        threading.Thread(target=self._io, daemon=True).start()

    def _io(self):
        sio = socketio.Client(reconnection=True)

        @sio.on("sensor_update")
        def _(msg): self.call_from_thread(self._apply, msg)

        @sio.on("alert")
        def _(msg):
            self.call_from_thread(self.query_one("#events", Log).write_line,
                                  f"[{msg.get('level','?')}] {msg.get('message','')}")
        sio.connect(self.base, transports=["websocket"])
        sio.wait()

    def _apply(self, msg):
        t = (msg.get("temperature") or {}).get("calibrated") or []
        od_block = msg.get("od") or {}
        o = od_block.get("calibrated") or []
        # OD is acquired on a slower lane than the rest of the payload and is
        # re-sent unchanged on every base tick in between. Append to the
        # sparkline history only when the OD timestamp actually advances, or
        # the trace shows one real sample as OD_EVERY_N_TICKS identical
        # points and its time axis silently compresses.
        od_ts = od_block.get("timestamp") or msg.get("timestamp")
        od_is_new = od_ts != getattr(self, "_last_od_ts", None)
        self._last_od_ts = od_ts
        self.latest = {"temperature": t, "od": o}
        self.exp = msg.get("experiment") or {}
        tbl = self.query_one("#vials", DataTable)
        active = set(self.exp.get("vials") or [])
        for v in range(N_VIALS):
            od = o[v] if v < len(o) else None
            tp = t[v] if v < len(t) else None
            ok = isinstance(od, (int, float)) and od == od
            if od_is_new:
                self.hist[v].append(od if ok else None)
            tbl.update_cell(str(v), self.col["od"], f"{od:.3f}" if ok else "—")
            tbl.update_cell(str(v), self.col["temp"],
                            f"{tp:.2f}" if isinstance(tp,(int,float)) and tp==tp else "—")
            tbl.update_cell(str(v), self.col["state"], "running" if v in active else "idle")
        self._render_side()

    def _render_side(self):
        e = self.exp
        self.query_one("#status", Static).update(
            f"[bold]{e.get('name','(no experiment)')}[/bold]  {e.get('status','—')}  "
            f"{e.get('mode','')}  t+{e.get('elapsed_hours',0):.1f} h")
        s = self.query_one("#trace", Sparkline)
        s.update(s.render_trace(list(self.hist[self.selected]), self.selected))

    def action_prev(self): self.selected = max(0, self.selected-1); self._render_side()
    def action_next(self): self.selected = min(N_VIALS-1, self.selected+1); self._render_side()

    def action_estop(self):
        try:
            requests.post(f"{self.base}/api/actuators/emergency_stop", timeout=5)
            self.query_one("#events", Log).write_line("[critical] EMERGENCY STOP sent")
        except Exception as exc:
            self.query_one("#events", Log).write_line(f"[error] e-stop failed: {exc}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1"); ap.add_argument("--port", type=int, default=5000)
    a = ap.parse_args()
    EvolverConsole(f"http://{a.host}:{a.port}").run()
