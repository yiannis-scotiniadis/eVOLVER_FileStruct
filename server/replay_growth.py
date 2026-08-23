"""server/replay_growth.py — replay a logged run through the growth estimator.

Offline and strictly read-only: it reads ``experiments/{name}/`` and walks the
estimator forward **causally**, at the engine's own recompute cadence, feeding
it only the samples that existed at each point. That causality is the whole
point — a post-hoc fit over the finished series would not tell you what the
dashboard would have shown at hour 3.

    python server/replay_growth.py TestForLabMeeting2
    python server/replay_growth.py TestForLabMeeting3 --vial 4 --verbose

Reusable on any future run, including the first real one. It is also what
``verify_growth_rate.py`` section 3 drives, and what the regression tests use
to assert the guard rails hold on real file shapes.

**What replaying the existing runs can and cannot show.** Every run currently
in ``experiments/`` came from ``MockSerialManager`` at its default
``time_multiplier = 100``, so the culture crosses the whole OD band in two or
three samples and ``pump_wait_minutes = 5`` puts every inter-dilution segment
below ``MIN_FIT_SPAN_SECONDS``. That makes them a genuine test of the guard
rails — correct flags, no crash on multi-hundred-hour gaps, no NaN reaching the
payload — and no test at all of accuracy. Accuracy comes from
``verify_growth_rate.py``'s synthetic and 1x-time generated datasets.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import growth_rate as growth  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXPERIMENTS_DIR = PROJECT_ROOT / "experiments"


def _parse_iso(s: str) -> Optional[datetime]:
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _epoch(s: str) -> Optional[float]:
    dt = _parse_iso(s)
    return dt.timestamp() if dt is not None else None


def load_od_series(exp_dir: Path, vial: int) -> list:
    """``[(t_epoch_seconds, od), ...]`` from ``vialNN_OD.csv``.

    NaN and blank OD cells are dropped, never interpolated (§8). Real
    timestamps are used rather than the nominal cadence: the sensor loop's
    period is ``max(10 s, work)`` and genuinely varies.
    """
    path = exp_dir / f"vial{vial:02d}_OD.csv"
    if not path.is_file():
        return []
    out: list = []
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            t = _epoch(row.get("timestamp", ""))
            raw = (row.get("calibrated_od") or "").strip()
            if t is None or not raw:
                continue
            try:
                od = float(raw)
            except ValueError:
                continue
            if od != od:                      # NaN
                continue
            out.append((t, od))
    out.sort(key=lambda p: p[0])
    return out


def load_dilution_events(
    exp_dir: Path, vial: int, flow_rate_ml_s: float = 1.0
) -> list:
    """``[DilutionEvent, ...]`` from ``vialNN_pump_log.csv``.

    The log has one row per *direction*; a dilution is the influx and efflux
    rows sharing a timestamp. The boundary spans from the fire time to the end
    of the LONGER pump, because that whole interval is disturbed — with
    ``pump_time`` capped at 20 s against a 10 s loop it can cover two or three
    sensor cycles (§4.5).

    ``delivered_ml`` is reconstructed from the influx duration for the gated
    diagnostic only. It never reaches the reported growth rate, so an
    approximate flow rate here cannot bias μ — which is precisely why the
    reported path was kept free of volume.
    """
    path = exp_dir / f"vial{vial:02d}_pump_log.csv"
    if not path.is_file():
        return []
    by_time: dict = {}
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            t = _epoch(row.get("timestamp", ""))
            if t is None:
                continue
            try:
                dur = float(row.get("duration_seconds") or 0.0)
            except ValueError:
                dur = 0.0
            entry = by_time.setdefault(t, {"influx": 0.0, "efflux": 0.0})
            direction = (row.get("direction") or "").strip()
            if direction in entry:
                entry[direction] = max(entry[direction], dur)
    events = []
    for t in sorted(by_time):
        e = by_time[t]
        events.append(
            growth.DilutionEvent(
                t_start=t,
                t_efflux_end=t + max(e["influx"], e["efflux"]),
                delivered_ml=e["influx"] * flow_rate_ml_s,
            )
        )
    return events


def replay_vial(
    exp_dir: Path,
    vial: int,
    config: dict,
    *,
    interval_seconds: float = growth.RECOMPUTE_INTERVAL_SECONDS,
    od_floor: float = growth.DEFAULT_OD_FLOOR,
    pump_calibrated: bool = False,
    extra_flags: tuple = (growth.FLAG_UNCALIBRATED_FLOOR,),
) -> list:
    """Walk the estimator forward over one vial's logged series.

    Returns ``[(t, GrowthReport), ...]`` — one entry per recompute tick, using
    only the samples and events that existed at that instant.
    """
    samples = load_od_series(exp_dir, vial)
    if not samples:
        return []
    params = config.get("parameters") or {}
    mode = config.get("mode", "turbidostat")
    volume_ml = float(params.get("volume_ml", growth.DEFAULT_VOLUME_ML))
    events = load_dilution_events(exp_dir, vial)

    lo_thresh = _per_vial(params, ("od_lower_thresh", "od_lower"), vial, 0.2)
    hi_thresh = _per_vial(
        params, ("od_upper_thresh", "od_upper", "target_od"), vial, 0.4,
    )
    od_range = (max(lo_thresh * 0.5, od_floor), hi_thresh * 1.5)

    out: list = []
    t0 = samples[0][0]
    t_end = samples[-1][0]
    next_tick = t0
    si = 0
    ei = 0
    seen: list = []
    seen_events: list = []
    while next_tick <= t_end:
        while si < len(samples) and samples[si][0] <= next_tick:
            seen.append(samples[si])
            si += 1
        while ei < len(events) and events[ei].t_start <= next_tick:
            seen_events.append(events[ei])
            ei += 1
        # Trim to the same rolling window the engine keeps.
        cutoff = next_tick - growth.HISTORY_WINDOW_SECONDS
        while seen and seen[0][0] < cutoff:
            seen.pop(0)
        while seen_events and seen_events[0].t_efflux_end < cutoff:
            seen_events.pop(0)

        report = growth.estimate(
            samples=list(seen),
            now=next_tick,
            dilution_events=list(seen_events),
            mode=mode,
            od_floor=od_floor,
            od_range=od_range,
            volume_ml=volume_ml,
            dilution_rate_per_hour=params.get("dilution_rate_per_hour"),
            pump_calibrated=pump_calibrated,
            samples_seen=len(seen),
            extra_flags=extra_flags,
        )
        out.append((next_tick, report))
        next_tick += interval_seconds
    return out


def _per_vial(params: dict, keys: tuple, vial: int, default: float) -> float:
    for k in keys:
        if k in params:
            v = params[k]
            if isinstance(v, list):
                if vial < len(v):
                    return float(v[vial])
                continue
            return float(v)
    return default


def replay_experiment(name: str, exp_root: Path = EXPERIMENTS_DIR, **kw) -> dict:
    """Replay every vial of an experiment. Returns ``{vial: [(t, report)]}``."""
    exp_dir = exp_root / name
    config_path = exp_dir / "config.json"
    if not config_path.is_file():
        raise FileNotFoundError(f"no config.json in {exp_dir}")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    return {
        int(v): replay_vial(exp_dir, int(v), config, **kw)
        for v in config.get("vials", [])
    }


def summarise(name: str, results: dict) -> str:
    """A compact per-vial report of what the replay produced."""
    lines = [f"=== {name} ==="]
    for vial in sorted(results):
        ticks = results[vial]
        if not ticks:
            lines.append(f"  vial {vial:2d}: no OD data")
            continue
        estimable = [r for _, r in ticks if r.growth.mu_per_hour is not None]
        flag_counts: dict = {}
        for _, r in ticks:
            for f in r.growth.flags:
                flag_counts[f] = flag_counts.get(f, 0) + 1
        regimes = sorted({r.regime for _, r in ticks})
        if estimable:
            mus = [r.growth.mu_per_hour for r in estimable]
            r2s = [r.growth.r_squared for r in estimable if r.growth.r_squared is not None]
            detail = (
                f"mu {min(mus):+.3f}..{max(mus):+.3f}/h  "
                f"median R2 {sorted(r2s)[len(r2s) // 2]:.3f}"
                if r2s else f"mu {min(mus):+.3f}..{max(mus):+.3f}/h"
            )
        else:
            detail = "no estimate at any tick"
        lines.append(
            f"  vial {vial:2d}: {len(ticks):4d} ticks, "
            f"{len(estimable):4d} estimable  [{','.join(regimes)}]  {detail}"
        )
        if flag_counts:
            top = sorted(flag_counts.items(), key=lambda kv: -kv[1])
            lines.append(
                "            flags: "
                + ", ".join(f"{k}x{v}" for k, v in top)
            )
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("experiment", help="directory name under experiments/")
    ap.add_argument("--vial", type=int, default=None, help="replay one vial only")
    ap.add_argument(
        "--interval", type=float, default=growth.RECOMPUTE_INTERVAL_SECONDS,
        help="recompute cadence in seconds (default: the engine's 60 s)",
    )
    ap.add_argument(
        "--verbose", action="store_true",
        help="print every tick rather than a summary",
    )
    args = ap.parse_args(argv)

    exp_dir = EXPERIMENTS_DIR / args.experiment
    if not (exp_dir / "config.json").is_file():
        print(f"no such experiment: {exp_dir}", file=sys.stderr)
        return 1
    config = json.loads((exp_dir / "config.json").read_text(encoding="utf-8"))
    vials = [args.vial] if args.vial is not None else config.get("vials", [])

    results = {
        int(v): replay_vial(exp_dir, int(v), config, interval_seconds=args.interval)
        for v in vials
    }
    print(f"mode={config.get('mode')}  "
          f"params={json.dumps(config.get('parameters', {}))}")
    if args.verbose:
        for vial in sorted(results):
            print(f"--- vial {vial}")
            for t, r in results[vial]:
                g = r.growth
                print(
                    f"  t+{(t - results[vial][0][0]) / 3600:7.3f} h  "
                    f"regime={r.regime:10s} "
                    f"mu={'None' if g.mu_per_hour is None else f'{g.mu_per_hour:+.4f}'}  "
                    f"R2={'None' if g.r_squared is None else f'{g.r_squared:.4f}'}  "
                    f"n={g.n_points:4d} win={g.windows_searched:3d}  "
                    f"flags={'|'.join(g.flags)}"
                )
    print(summarise(args.experiment, results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
