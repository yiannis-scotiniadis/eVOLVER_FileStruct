"""server/bench_read_paths.py — are the run-length-scaling read paths affordable?

Companion to `bench_growth_rate.py`, for the three paths whose cost grew with
how long a run had been going rather than with how much was asked for:

1. `ExperimentEngine.get_data`   — the dashboard's per-vial plot fetch
2. `data_export.build_bundle`    — the export button
3. `CalibrationStore.pump_seconds_since` — the staleness banner

All three were bounded in 2026-08-23's read-path work. This measures whether
they stayed bounded on the machine you actually run on.

Run it ON THE PI::

    python3 server/bench_read_paths.py            # ~1 and ~3 day fixtures
    python3 server/bench_read_paths.py --days 7   # the full week

The Pi figures quoted in SPEC §8 are extrapolations from an x86 box at a 30–60x
scalar-CPython penalty, and SD-card I/O is not something that extrapolates at
all — which is the whole reason this script exists. It writes its fixtures to a
temporary directory and cleans up after itself; nothing touches `experiments/`.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
import tempfile
import time
import tracemalloc
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import data_export as dx                                  # noqa: E402
from calibration_service import CalibrationStore          # noqa: E402
from experiment_engine import ExperimentEngine            # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DT = 10.0                      # sensor cadence, s
N_VIALS = 16
TICK_BUDGET_S = 10.0
BASE = datetime(2026, 8, 1, tzinfo=timezone.utc)

OD_HEADER = ("timestamp,elapsed_hours,raw_adc,calibrated_od,n_valid,flag,dark")


def _build_run(root: Path, days: float, vials: int = N_VIALS) -> Path:
    exp = root / f"D{days:g}"
    exp.mkdir(parents=True, exist_ok=True)
    (exp / "config.json").write_text(
        json.dumps({"name": exp.name, "vials": list(range(vials))}),
        encoding="utf-8",
    )
    n = int(days * 24 * 3600 / DT)
    for v in range(vials):
        with (exp / f"vial{v:02d}_OD.csv").open(
            "w", encoding="utf-8", newline=""
        ) as f:
            f.write(OD_HEADER + "\r\n")
            for i in range(n):
                ts = (BASE + timedelta(seconds=DT * i)).isoformat(timespec="seconds")
                f.write(f"{ts},{i * DT / 3600:.4f},52000,0.3421,5,ok,\r\n")
    return exp


def _timed(fn, reps=1):
    """(seconds, peak_bytes). Time and memory are measured in SEPARATE runs:
    tracemalloc roughly triples the cost of allocation-heavy code, so timing
    under it reports fiction."""
    t0 = time.perf_counter()
    for _ in range(reps):
        fn()
    elapsed = (time.perf_counter() - t0) / reps
    tracemalloc.start()
    fn()
    _cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return elapsed, peak


def _cpu_note() -> None:
    try:
        info = Path("/proc/cpuinfo").read_text(encoding="utf-8")
        for key in ("Model", "model name", "Hardware", "Revision"):
            for line in info.splitlines():
                if line.startswith(key):
                    print(f"  {line.strip()}")
                    break
    except Exception:
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=float, nargs="*", default=[1.0, 3.0],
                    help="run lengths to simulate (default: 1 3)")
    args = ap.parse_args(argv)

    print("=" * 72)
    print("read-path cost on this machine")
    print("=" * 72)
    print(f"  python {sys.version.split()[0]} on {sys.platform}")
    _cpu_note()

    root = Path(tempfile.mkdtemp(prefix="evolver-bench-read-"))
    verdicts: list[str] = []
    try:
        # -- 1. get_data ---------------------------------------------------
        print("\n1. get_data  -- one dashboard plot fetch, max_points=500")
        print("   Flat in run length is the target: a 1 h window should cost the")
        print("   same on a week-old run as on a fresh one.")
        print(f"   {'run':>6} {'MB/vial':>8} {'hours=1':>12} {'peak':>9} "
              f"{'All':>12} {'peak':>9}")
        first_window_ms = None
        for days in args.days:
            exp = _build_run(root, days, vials=1)
            eng = ExperimentEngine(None, None, root)
            mb = (exp / "vial00_OD.csv").stat().st_size / 1e6
            t_w, p_w = _timed(
                lambda: eng.get_data(exp.name, 0, "od", hours=1.0, max_points=500)
            )
            t_a, p_a = _timed(
                lambda: eng.get_data(exp.name, 0, "od", max_points=500)
            )
            print(f"   {days:5g}d {mb:8.1f} {t_w * 1000:11.1f}ms {p_w / 1e6:8.1f}M "
                  f"{t_a * 1000:11.1f}ms {p_a / 1e6:8.1f}M")
            if first_window_ms is None:
                first_window_ms = t_w * 1000
            last_window_ms = t_w * 1000
            shutil.rmtree(exp)
        growth = last_window_ms / max(first_window_ms, 1e-9)
        verdicts.append(
            f"get_data 1 h window grew {growth:.1f}x across the tested run "
            f"lengths ({'FLAT - good' if growth < 2.0 else 'STILL SCALING'})"
        )

        # -- 2. build_bundle ----------------------------------------------
        print("\n2. build_bundle  -- the export button, 16 vials, od+temp")
        print("   Peak memory is the number that matters: a Pi 1 Model B has")
        print("   512 MB total, and the dict-based join this replaced peaked at")
        print("   190 MB on a 7-day run.")
        print(f"   {'run':>6} {'MB on disk':>11} {'time':>10} {'peak':>9} {'zip':>8}")
        worst_peak = 0.0
        for days in args.days:
            exp = _build_run(root, days)
            disk = sum(p.stat().st_size for p in exp.glob("*_OD.csv")) / 1e6
            payload_holder = {}

            def run():
                payload_holder["p"] = dx.build_bundle(
                    exp, name=exp.name, vials=list(range(N_VIALS)),
                    parameters=["od", "temp"],
                )[1]
            t_b, p_b = _timed(run)
            worst_peak = max(worst_peak, p_b / 1e6)
            print(f"   {days:5g}d {disk:11.1f} {t_b:9.2f}s {p_b / 1e6:8.1f}M "
                  f"{len(payload_holder['p']) / 1e6:7.1f}M")
            shutil.rmtree(exp)
        verdicts.append(
            f"build_bundle peaked at {worst_peak:.1f} MB "
            f"({'fine' if worst_peak < 50 else 'TOO HIGH for a 512 MB Pi'})"
        )

        # -- 3. pump_seconds_since ----------------------------------------
        print("\n3. pump_seconds_since  -- the staleness banner")
        print("   Cold is the first call after a restart; warm is every call")
        print("   after, and should be independent of how many rows exist.")
        print(f"   {'exps':>6} {'rows':>9} {'cold':>10} {'warm':>10}")
        warm_ms = 0.0
        for n_exp, rows_per in ((5, 200), (20, 800)):
            proot = root / f"campaign{n_exp}"
            total = 0
            for e in range(n_exp):
                d = proot / f"exp{e:03d}"
                d.mkdir(parents=True, exist_ok=True)
                for v in range(4):
                    with (d / f"vial{v:02d}_pump_log.csv").open(
                        "w", encoding="utf-8", newline=""
                    ) as f:
                        w = csv.writer(f)
                        w.writerow(["timestamp", "elapsed_hours", "direction",
                                    "duration_seconds", "od_at_pump"])
                        for i in range(rows_per):
                            ts = (BASE + timedelta(minutes=15 * i)).isoformat(
                                timespec="seconds")
                            w.writerow([ts, f"{i * 0.25:.4f}", "influx",
                                        "12.00", "0.5"])
                            total += 1
            store = CalibrationStore(PROJECT_ROOT / "calibration", proot)
            t0 = time.perf_counter()
            store.pump_seconds_since("2026-08-01T000000Z")
            cold = time.perf_counter() - t0
            t0 = time.perf_counter()
            for _ in range(5):
                store.pump_seconds_since("2026-08-01T000000Z")
            warm = (time.perf_counter() - t0) / 5
            warm_ms = warm * 1000
            print(f"   {n_exp:6d} {total:9d} {cold * 1000:9.1f}ms "
                  f"{warm * 1000:9.1f}ms")
            shutil.rmtree(proot)
        verdicts.append(
            f"pump_seconds_since warm call {warm_ms:.1f} ms "
            f"({'fine' if warm_ms < 250 else 'SLOW - it is on the banner path'})"
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print("\n" + "=" * 72)
    for v in verdicts:
        print("  " + v)
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
