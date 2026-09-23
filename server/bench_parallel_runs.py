"""server/bench_parallel_runs.py — what does a SECOND experiment actually cost?

Companion to `bench_growth_rate.py` and `bench_read_paths.py`. Answers the
question that gates `PARALLEL_EXPERIMENTS.md`: if the engine grows from one
loaded experiment to N, how much of the 10 s sensor tick does that spend, and
how much resident memory does it add?

Method
------
Compare one 16-vial engine against two 8-vial engines against three engines
sharing the same sixteen vials, all ticking identical sensor arrays.

**Independent engine instances are deliberately the WORST case.** They are
Option C in `PARALLEL_EXPERIMENTS.md` §4 — every run redundantly composes and
re-sends its own actuator vectors. The recommended supervisor design (Option B)
composes once per tick for the whole machine, so its real cost is strictly
lower than what this script reports. Treat every number here as an upper bound.

What to look for
----------------
The per-vial work does not change: sixteen vials are sixteen vials whether they
belong to one experiment or three. What multiplies by N is the per-run constant,
and on this codebase that constant is dominated by one thing — the `state.json`
persist in `_save_state_locked`, which fsyncs on every OD tick. The script
isolates it and splits it into a CPU part (portable, scales with the box) and an
fsync part (device latency — on the Pi's SD card this is the term that matters
and it does NOT scale like CPU).

Run it ON THE PI, not on a laptop::

    python3 server/bench_parallel_runs.py

Writes only into a temporary directory; touches no experiment data.
"""

from __future__ import annotations

import gc
import json
import os
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402

from data_logger import DataLogger  # noqa: E402
from experiment_engine import ExperimentEngine  # noqa: E402
from mock_serial_manager import MockSerialManager  # noqa: E402

N_VIALS = 16
BASE_TICK_S = 10.0        # SPEC §9 fast lane
OD_TICK_S = 60.0          # SPEC §9 control lane
TICKS = 400
TEMP_CAL = np.array([[-0.11] * N_VIALS, [83.0] * N_VIALS])


def _make(root: Path, name: str, vials: list[int], mode: str = "turbidostat"):
    d = root / name
    eng = ExperimentEngine(
        MockSerialManager(time_multiplier=1.0), DataLogger(d), d, temp_cal=TEMP_CAL
    )
    media = {
        "bottles": [{
            "id": "b1", "name": "LB", "contents": "LB",
            "initial_volume_ml": 2000.0, "low_volume_alert_ml": 100.0,
        }],
        "vial_to_bottle": {str(v): "b1" for v in vials},
        "waste": {"name": "W", "capacity_ml": 4000.0, "high_fill_alert_ml": 3600.0},
    }
    params = {
        "temperature_c": 37, "stir_rate": 10, "od_lower_thresh": 0.2,
        "od_upper_thresh": 0.4, "pump_wait_minutes": 15, "volume_ml": 25,
    }
    eng.create_experiment(name=name, mode=mode, vials=vials,
                          parameters=params, media=media)
    eng.start_experiment()
    return eng


def _tick(engines, i: int, od: bool) -> None:
    temps = [37.0 + 0.01 * (i % 7)] * N_VIALS
    ods = [0.25 + 0.002 * (i % 13)] * N_VIALS if od else None
    ts = f"2026-01-01T00:{(i // 60) % 60:02d}:{i % 60:02d}+00:00"
    flags = ["ok"] * N_VIALS if od else None
    for e in engines:
        e.run_cycle(ts, temps, ods, od_flags=flags)


def _bench(engines, label: str) -> dict:
    for i in range(40):                       # warm: fill histories and caches
        _tick(engines, i, od=(i % 6 == 0))
    gc.collect()
    fast: list[float] = []
    slow: list[float] = []
    for i in range(40, 40 + TICKS):
        is_od = (i % 6 == 0)
        t0 = time.perf_counter()
        _tick(engines, i, od=is_od)
        dt = (time.perf_counter() - t0) * 1000.0
        (slow if is_od else fast).append(dt)
    return {
        "label": label,
        "fast": statistics.median(fast),
        "od": statistics.median(slow),
        "od_p95": sorted(slow)[int(len(slow) * 0.95)],
    }


def _engine_state_bytes(engines) -> int:
    """Bytes reachable from engine internals — the part that scales with runs."""
    seen: set[int] = set()
    total = 0

    def walk(obj, depth=0):
        nonlocal total
        if depth > 6 or id(obj) in seen:
            return
        seen.add(id(obj))
        total += sys.getsizeof(obj)
        if isinstance(obj, dict):
            for k, v in obj.items():
                walk(k, depth + 1)
                walk(v, depth + 1)
        elif isinstance(obj, (list, tuple, set)):
            for v in obj:
                walk(v, depth + 1)
        elif hasattr(obj, "__dict__"):
            walk(obj.__dict__, depth + 1)
        elif hasattr(obj, "__iter__") and not isinstance(obj, (str, bytes, np.ndarray)):
            try:
                for v in list(obj):
                    walk(v, depth + 1)
            except Exception:
                pass

    for e in engines:
        walk(e)
    return total


def _decompose_persist(engine, root: Path) -> dict:
    """Split _save_state_locked into CPU and fsync. The fsync term is device
    latency and is what an SD card makes expensive."""
    exp_dir = next((root / "solo").glob("*/state.json"), None)
    blob = exp_dir.read_text(encoding="utf-8") if exp_dir else "{}"
    payload = json.loads(blob)
    scratch = Path(tempfile.mkdtemp(prefix="persist_"))

    def timed(fn, n=200):
        fn()
        t0 = time.perf_counter()
        for _ in range(n):
            fn()
        return (time.perf_counter() - t0) / n * 1000.0

    def serialize():
        json.dumps(payload, indent=4)

    def write(fsync: bool):
        p = scratch / "s.tmp"
        with p.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=4)
            f.flush()
            if fsync:
                os.fsync(f.fileno())
        os.replace(p, scratch / "s.json")

    out = {
        "bytes": len(blob),
        "serialize": timed(serialize),
        "no_fsync": timed(lambda: write(False)),
        "fsync": timed(lambda: write(True)),
    }
    shutil.rmtree(scratch, ignore_errors=True)
    return out


def main() -> int:
    print("=" * 74)
    print("cost of parallel experiments   (upper bound: independent engines)")
    print("=" * 74)
    print(f"  python {sys.version.split()[0]}  on  {sys.platform}  numpy {np.__version__}")
    try:
        info = Path("/proc/cpuinfo").read_text(encoding="utf-8")
        for key in ("Model", "model name", "Hardware", "Revision"):
            for line in info.splitlines():
                if line.startswith(key):
                    print(f"  {line.strip()}")
                    break
    except Exception:
        pass

    root = Path(tempfile.mkdtemp(prefix="evbench_parallel_"))
    try:
        configs = [
            ([("solo", list(range(16)), "turbidostat")], "1 run x 16 vials"),
            ([("runA", list(range(0, 8)), "turbidostat"),
              ("runB", list(range(8, 16)), "chemostat")], "2 runs x 8 vials"),
            ([("r1", list(range(0, 6)), "turbidostat"),
              ("r2", list(range(6, 11)), "chemostat"),
              ("r3", list(range(11, 16)), "morbidostat")], "3 runs (6/5/5 vials)"),
        ]
        results = []
        first_engines = None
        for spec, label in configs:
            engines = [_make(root, n, v, m) for n, v, m in spec]
            if first_engines is None:
                first_engines = engines
            r = _bench(engines, label)
            r["state_kb"] = _engine_state_bytes(engines) / 1024.0
            results.append(r)

        print(f"\n  {'configuration':<24}{'fast tick':>12}{'OD tick':>12}"
              f"{'OD p95':>11}{'engine state':>15}")
        for r in results:
            print(f"  {r['label']:<24}{r['fast']:>9.3f} ms{r['od']:>9.3f} ms"
                  f"{r['od_p95']:>8.3f} ms{r['state_kb']:>12.1f} kB")

        d2 = results[1]["od"] - results[0]["od"]
        d3 = results[2]["od"] - results[1]["od"]
        print(f"\n  marginal OD-tick cost of run #2: {d2:+.3f} ms  "
              f"(state {results[1]['state_kb'] - results[0]['state_kb']:+.1f} kB)")
        print(f"  marginal OD-tick cost of run #3: {d3:+.3f} ms  "
              f"(state {results[2]['state_kb'] - results[1]['state_kb']:+.1f} kB)")

        p = _decompose_persist(first_engines[0], root)
        print("\n  " + "-" * 70)
        print("  where the marginal cost goes: the per-run state.json persist")
        print("  " + "-" * 70)
        print(f"  state.json size ...................... {p['bytes']:>8} B")
        print(f"  json.dumps only (CPU) ................ {p['serialize']:>8.3f} ms")
        print(f"  + write + atomic replace, no fsync ... {p['no_fsync']:>8.3f} ms")
        print(f"  + fsync  (= full _save_state_locked) . {p['fsync']:>8.3f} ms")
        fsync_ms = p["fsync"] - p["no_fsync"]
        print(f"\n  fsync share: {100 * fsync_ms / p['fsync']:.1f} % ({fsync_ms:.3f} ms) "
              "— device latency, not CPU. On an SD card this is the term")
        print("  that grows, and it is why saves should be debounced before N > 1.")
        print(f"  logical write volume: {24 * 60 * p['bytes'] / 1e6:.1f} MB/day per run "
              "(one persist per OD tick)")

        worst = results[-1]["od_p95"]
        print()
        if worst > BASE_TICK_S * 1000 * 0.5:
            print(f"  VERDICT: TOO EXPENSIVE — {worst:.0f} ms p95 eats half the "
                  f"{BASE_TICK_S:.0f} s tick. Debounce state saves first.")
            return 1
        if worst > BASE_TICK_S * 1000 * 0.15:
            print(f"  VERDICT: TIGHT — {worst:.0f} ms p95 against a "
                  f"{BASE_TICK_S:.0f} s tick. Watch the loop period under load.")
            return 0
        print(f"  VERDICT: comfortable — {worst:.1f} ms p95 for three runs against a "
              f"{BASE_TICK_S:.0f} s tick")
        print(f"  ({100 * worst / (BASE_TICK_S * 1000):.2f} % of the budget; the tick "
              "already spends ~2.3 s asleep on the UART).")
        return 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
