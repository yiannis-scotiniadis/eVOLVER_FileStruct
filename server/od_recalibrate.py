"""server/od_recalibrate.py — apply a different OD curve to a finished run.

A run's ``vialNN_OD.csv`` records the counts behind every OD (schema v2:
``lit_adc``, ``dark_adc``, ``dark_used``, ...), so a better curve -- the
organism's first real dilution series, a recalibration after the run, a curve
fitted off the Pi -- can be pushed through the same counts afterwards. This
module does exactly that, and nothing else: it never touches the run's own
files.

    python server/od_recalibrate.py MyRun --profile escherichia-coli-k-12
    python server/od_recalibrate.py MyRun --profile pichia --version 2026-10-01T120000Z --vials 0,1
    python server/od_recalibrate.py MyRun --envelope my_curve.json --no-reanchor

Output goes to ``experiments/<run>/recalibrated/<profile>__<version>/``:
one ``vialNN_OD.csv`` per vial and a ``recalibration.json`` saying what was
done. Each CSV keeps the live file's first seven columns and their meaning
(``raw_adc`` = the signal fed to the curve, ``calibrated_od`` = the new OD),
so ``replay_growth.py --od-dir`` and positional parsers read it unchanged.

**The signal is rebuilt in the TARGET curve's space**, not the run's: a
dark-subtracted target gets ``lit - dark``, a raw-signal target gets
``lit``. The dark follows the live rule exactly -- this row's dark read, else
the dark the live reader used (its last good one), else the last dark seen
earlier in the file; a dark-subtracted target with none of those is
``dropped``, never "dark = 0".

**Re-anchoring.** A curve's row 2 carries a machine/medium offset that the
per-run blank removes (CALIBRATION_PROTOCOL §5.4). With ``reanchor=True``
(the default) the target curve is re-anchored at the run's OWN blank,
recomputed in the target's signal space -- so re-applying the run's own
pinned curve reproduces its logged OD. Without a blank (or for a vial the
blank excluded) the target's own row 2 is used, and the report says the OD
carries that curve's offset.

Pre-v2 files (seven columns) have only the signal the run's curve was fed,
rounded to a whole count. It is reconstructed per row: a dark-subtracted run
logged ``lit - dark``, so a raw-signal target needs the row's dark added back;
a raw-signal run logged ``lit``, so a dark-subtracted target needs a dark the
run may never have read -- such a vial is refused, with the reason.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from calibration_service import N_VIALS, od_profile_id_of  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXPERIMENTS_DIR = PROJECT_ROOT / "experiments"
CALIBRATION_DIR = PROJECT_ROOT / "calibration"

RECAL_SUBDIR = "recalibrated"
REPORT_NAME = "recalibration.json"
REPORT_SCHEMA = 1
# The live file's seven, then what the recalibration adds (append-only, as
# for the live file).
RECAL_HEADER = (
    "timestamp", "elapsed_hours", "raw_adc", "calibrated_od", "n_valid", "flag", "dark",
    "signal", "dark_used", "original_od", "original_flag",
)
# A recalibration id is a directory name: profile slug, version stamp, and an
# optional suffix. Validated wherever one arrives from outside.
RID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}__[0-9A-Za-z-]{1,40}(__raw-c)?$")
# Yield the GIL this often so a recalibration running beside the sensor loop
# (app.py runs it on a background thread) never holds the loop off its tick.
_YIELD_EVERY_ROWS = 2000


class RecalibrationError(ValueError):
    """The recalibration cannot be done as asked (bad curve, no data)."""


def _float(cell) -> Optional[float]:
    if cell is None:
        return None
    s = str(cell).strip()
    if not s:
        return None
    try:
        x = float(s)
    except ValueError:
        return None
    return x if math.isfinite(x) else None


def _fmt_exact(x: Optional[float]) -> str:
    return "" if x is None or not math.isfinite(x) else format(float(x), ".10g")


def _fmt_int(x: Optional[float]) -> str:
    return "" if x is None or not math.isfinite(x) else str(int(round(x)))


def _fmt_od(x: Optional[float]) -> str:
    return "" if x is None or not math.isfinite(x) else f"{x:.4f}"


def _load_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def target_rows(env: dict) -> tuple[np.ndarray, bool]:
    """The 4x16 rows and dark-subtraction flag of an OD envelope, checked."""
    data = (env or {}).get("data") or {}
    rows = np.asarray(data.get("rows", []), dtype=float)
    if rows.shape != (4, N_VIALS):
        raise RecalibrationError(f"OD curve rows have shape {rows.shape}, expected (4, {N_VIALS})")
    return rows, bool(data.get("dark_subtracted", False))


def rid_for(env: dict, reanchor: bool) -> str:
    """``<profile>__<version>`` (``__raw-c`` when not re-anchored)."""
    profile = od_profile_id_of(env) or "curve"
    version = str(env.get("version") or "unversioned")
    rid = f"{profile}__{version}" + ("" if reanchor else "__raw-c")
    if not RID_RE.match(rid):
        raise RecalibrationError(f"cannot name a recalibration {rid!r}")
    return rid


def source_dark_subtracted(exp_dir: Path, config: dict, store=None) -> tuple[bool, str]:
    """Whether the run's OWN curve was fed dark-subtracted signal, and how
    that was determined. Only needed for pre-v2 rows (a v2 row says so
    itself). Pre-Session-AA runs never were."""
    cal = config.get("calibration") or {}
    if "od_dark_subtracted" in cal:
        return bool(cal["od_dark_subtracted"]), "config calibration.od_dark_subtracted"
    snap = _load_json(exp_dir / "od_calibration.json")
    if snap is not None:
        return bool((snap.get("data") or {}).get("dark_subtracted", False)), "od_calibration.json"
    if store is not None and cal.get("od"):
        env = store.get_version("od", cal["od"])
        if env is not None:
            return bool((env.get("data") or {}).get("dark_subtracted", False)), \
                f"store version {cal['od']}"
    acq = (config.get("parameters") or {}).get("od_acquisition") or {}
    if acq.get("dark_subtract") is True:
        return True, "od_acquisition.dark_subtract"
    return False, "assumed (no record; runs before Session AA were never dark-subtracted)"


def _reanchor(rows: np.ndarray, target_ds: bool, blank: Optional[dict],
              vial: int) -> tuple[Optional[float], Optional[str]]:
    """``(c, None)`` -- the target's row 2 re-anchored at this vial's blank,
    in the target's signal space -- or ``(None, why not)``."""
    if blank is None:
        return None, "no per-run blank"
    c_run = (blank.get("fit") or {}).get("c_run") or {}
    if str(vial) not in c_run:
        return None, "vial excluded from the per-run blank"
    data = blank.get("data") or {}
    try:
        lit = float(data["blank_median"][vial])
        dark = float(data["dark_median"][vial]) if target_ds else 0.0
    except (KeyError, IndexError, TypeError, ValueError):
        return None, "the blank does not record its counts for this vial"
    signal = lit - dark
    a, b, _c, d = rows[:, vial]
    if not (math.isfinite(signal) and a < signal < b):
        return None, (f"blank signal {signal:.0f} is outside the target curve's "
                      f"domain ({a:.0f}, {b:.0f})")
    return float(math.log10((b - a) / (signal - a) - 1.0) / d), None


def _read_od_file(path: Path) -> tuple[list[str], list[dict]]:
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader.fieldnames or []), list(reader)


def recalibrate_vial(
    rows_in: list[dict],
    *,
    vial: int,
    rows: np.ndarray,
    target_ds: bool,
    c: float,
    source_ds: bool,
) -> tuple[list[list[str]], dict]:
    """Push one vial's logged counts through column ``vial`` of ``rows``
    (with row 2 = ``c``). Returns the output rows and per-vial counts."""
    a, b, _c, d = (float(x) for x in rows[:, vial])
    out: list[list[str]] = []
    counts = {"rows": 0, "ok": 0, "out_of_range": 0, "dropped": 0,
              "reconstructed_lit": 0, "dark_fallback": 0}
    last_dark: Optional[float] = None
    for i, r in enumerate(rows_in):
        if i and i % _YIELD_EVERY_ROWS == 0:
            time.sleep(0)
        counts["rows"] += 1
        v2 = "lit_adc" in r
        dark_now = _float(r.get("dark_adc")) if v2 else None
        if dark_now is None:
            dark_now = _float(r.get("dark"))
        dark_live = _float(r.get("dark_used")) if v2 else None
        # This row's dark, else the one the live reader used, else the last
        # one seen in this file -- the live rule, replayed.
        dark = dark_now if dark_now is not None else (
            dark_live if dark_live is not None else last_dark)
        if dark_now is None and dark is not None:
            counts["dark_fallback"] += 1
        if dark_now is not None:
            last_dark = dark_now
        elif dark_live is not None:
            last_dark = dark_live

        lit = _float(r.get("lit_adc")) if v2 else None
        logged_signal = _float(r.get("raw_adc"))
        if v2 and lit is not None:
            row_ds = (r.get("dark_subtracted") or "").strip() == "1"
        else:
            row_ds = source_ds
        signal: Optional[float] = None
        if lit is not None:
            if not target_ds:
                signal = lit
            elif dark is not None:
                signal = lit - dark
        elif logged_signal is not None:
            # A pre-v2 row (or a v2 row without lit): only the rounded signal
            # the run's own curve was fed survives.
            counts["reconstructed_lit"] += 1
            if row_ds == target_ds:
                signal = logged_signal
            elif row_ds and not target_ds:
                # logged = lit - dark_used; the row's own dark is the best
                # record of dark_used left.
                signal = logged_signal + dark if dark is not None else None
            else:
                signal = logged_signal - dark if dark is not None else None

        if signal is None:
            od, flag = None, "dropped"
        elif a < signal < b:
            od = c - math.log10((b - a) / (signal - a) - 1.0) / d
            flag = "ok"
        else:
            od, flag = None, "out_of_range"
        counts[flag] += 1
        out.append([
            r.get("timestamp", ""), r.get("elapsed_hours", ""),
            _fmt_int(signal), _fmt_od(od), r.get("n_valid", "") or "", flag,
            r.get("dark", "") or "",
            _fmt_exact(signal), _fmt_exact(dark if target_ds else None),
            r.get("calibrated_od", "") or "", r.get("flag", "") or "",
        ])
    return out, counts


def recalibrate_run(
    exp_dir: Path,
    target_env: dict,
    *,
    vials: Optional[list[int]] = None,
    reanchor: bool = True,
    store=None,
    progress: Optional[Callable[[int, int], None]] = None,
) -> dict:
    """Recalibrate a finished run's OD onto ``target_env``. Writes
    ``exp_dir/recalibrated/<rid>/`` (replacing a previous one of the same
    id, atomically) and returns its ``recalibration.json`` report."""
    exp_dir = Path(exp_dir)
    config = _load_json(exp_dir / "config.json")
    if config is None:
        raise FileNotFoundError(f"no config.json in {exp_dir}")
    rows, target_ds = target_rows(target_env)
    run_vials = [int(v) for v in config.get("vials", [])]
    if vials is None:
        vials = run_vials
    else:
        vials = sorted({int(v) for v in vials})
        stray = [v for v in vials if v not in run_vials]
        if stray:
            raise RecalibrationError(f"vials {stray} are not part of this run {run_vials}")
    if not vials:
        raise RecalibrationError("no vials to recalibrate")
    rid = rid_for(target_env, reanchor)
    source_ds, source_ds_how = source_dark_subtracted(exp_dir, config, store)
    blank = _load_json(exp_dir / "od_blank.json") if reanchor else None
    cal = config.get("calibration") or {}

    report_vials: dict = {}
    warnings: list[str] = []
    out_root = exp_dir / RECAL_SUBDIR
    out_root.mkdir(exist_ok=True)
    staging = out_root / f".{rid}.tmp-{os.getpid()}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()
    try:
        for n, v in enumerate(vials):
            if progress is not None:
                progress(n, len(vials))
            entry: dict = {}
            path = exp_dir / f"vial{v:02d}_OD.csv"
            if not path.is_file():
                entry["refused"] = "no OD file"
                report_vials[str(v)] = entry
                continue
            header, rows_in = _read_od_file(path)
            v2 = "lit_adc" in header
            entry["source_schema"] = 2 if v2 else 1
            has_dark = any(_float(r.get("dark_adc") or r.get("dark")) is not None
                           or _float(r.get("dark_used")) is not None for r in rows_in)
            if target_ds and not has_dark and not (not v2 and source_ds):
                entry["refused"] = ("the target curve is dark-subtracted and this "
                                    "vial's log records no dark read")
                report_vials[str(v)] = entry
                continue
            if reanchor:
                c, why = _reanchor(rows, target_ds, blank, v)
            else:
                c, why = None, "re-anchoring not requested"
            entry["reanchored"] = c is not None
            if c is None:
                c = float(rows[2, v])
                entry["offset_note"] = (f"{why}: OD carries the target curve's own "
                                        "row-2 offset")
            entry["c"] = c
            out, counts = recalibrate_vial(rows_in, vial=v, rows=rows, target_ds=target_ds,
                                           c=c, source_ds=source_ds)
            entry.update(counts)
            with (staging / f"vial{v:02d}_OD.csv").open("w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(RECAL_HEADER)
                w.writerows(out)
            report_vials[str(v)] = entry
        refused = sorted(int(k) for k, e in report_vials.items() if "refused" in e)
        if refused:
            warnings.append(f"vials {refused} were not recalibrated — see 'vials'")
        carried = sorted(int(k) for k, e in report_vials.items()
                         if "refused" not in e and not e.get("reanchored"))
        if carried and reanchor:
            warnings.append(f"vials {carried} could not be re-anchored at the run's "
                            "blank: their OD carries the curve's own offset")
        report = {
            "schema": REPORT_SCHEMA,
            "id": rid,
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "experiment": exp_dir.name,
            "target": {
                "profile": od_profile_id_of(target_env),
                "organism": (target_env.get("profile") or {}).get("organism"),
                "version": target_env.get("version"),
                "source": target_env.get("source"),
                "dark_subtracted": target_ds,
            },
            "run": {
                "od_version": cal.get("od"),
                "od_profile": cal.get("od_profile"),
                "organism": cal.get("od_organism"),
                "dark_subtracted": source_ds,
                "dark_subtracted_from": source_ds_how,
                "od_csv_schema": config.get("od_csv_schema", 1),
            },
            "reanchor": reanchor,
            "blank": None if blank is None else {
                "version": blank.get("version"),
                "parent_od_cal": (blank.get("conditions") or {}).get("parent_od_cal"),
            },
            "vials": report_vials,
            "warnings": warnings,
        }
        (staging / REPORT_NAME).write_text(json.dumps(report, indent=2), encoding="utf-8")
        final = out_root / rid
        if final.exists():
            old = out_root / f".{rid}.old-{os.getpid()}"
            os.replace(final, old)
            os.replace(staging, final)
            shutil.rmtree(old, ignore_errors=True)
        else:
            os.replace(staging, final)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    if progress is not None:
        progress(len(vials), len(vials))
    return report


def list_recalibrations(exp_dir: Path) -> list[dict]:
    """Every finished recalibration of a run, newest first."""
    root = Path(exp_dir) / RECAL_SUBDIR
    out = []
    if not root.is_dir():
        return out
    for d in root.iterdir():
        if not d.is_dir() or not RID_RE.match(d.name):
            continue
        report = _load_json(d / REPORT_NAME)
        if report is not None:
            out.append((report.get("created") or "", d.stat().st_mtime_ns, report))
    out.sort(key=lambda item: item[:2], reverse=True)
    return [report for *_key, report in out]


def recalibration_dir(exp_dir: Path, rid: str) -> Path:
    """The directory of recalibration ``rid``, refusing anything that is not
    a well-formed id inside this run's recalibrated/ (no traversal)."""
    if not RID_RE.match(rid or ""):
        raise RecalibrationError(f"not a recalibration id: {rid!r}")
    root = (Path(exp_dir) / RECAL_SUBDIR).resolve()
    d = (root / rid).resolve()
    if d.parent != root or not (d / REPORT_NAME).is_file():
        raise FileNotFoundError(rid)
    return d


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("experiment", help="directory name under experiments/")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--profile", help="organism profile id (see the Calibration tab)")
    src.add_argument("--envelope", type=Path,
                     help="an OD calibration envelope JSON (a curve from anywhere)")
    ap.add_argument("--version", help="profile version (default: its current one)")
    ap.add_argument("--vials", help="comma-separated vials (default: all the run's)")
    ap.add_argument("--no-reanchor", action="store_true",
                    help="keep the curve's own row 2 instead of the run's blank")
    ap.add_argument("--experiments-dir", type=Path, default=EXPERIMENTS_DIR)
    ap.add_argument("--calibration-dir", type=Path, default=CALIBRATION_DIR)
    args = ap.parse_args(argv)

    exp_dir = args.experiments_dir / args.experiment
    if not (exp_dir / "config.json").is_file():
        print(f"no such experiment: {exp_dir}", file=sys.stderr)
        return 1
    state = _load_json(exp_dir / "state.json") or {}
    if state.get("status") == "running":
        print(f"'{args.experiment}' is still running — stop it first", file=sys.stderr)
        return 1
    from calibration_service import CalibrationStore
    store = CalibrationStore(args.calibration_dir, experiments_root=args.experiments_dir)
    if args.envelope is not None:
        env = _load_json(args.envelope)
        if env is None:
            print(f"cannot read {args.envelope}", file=sys.stderr)
            return 1
    else:
        try:
            env = store.get_od_profile(args.profile, args.version)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if env is None:
            print(f"no OD profile {args.profile!r} (known: {sorted(store.od_profiles())})",
                  file=sys.stderr)
            return 1
    vials = [int(x) for x in args.vials.split(",")] if args.vials else None
    try:
        report = recalibrate_run(exp_dir, env, vials=vials,
                                 reanchor=not args.no_reanchor, store=store)
    except RecalibrationError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"wrote {exp_dir / RECAL_SUBDIR / report['id']}")
    for v, e in sorted(report["vials"].items(), key=lambda kv: int(kv[0])):
        if "refused" in e:
            print(f"  vial {int(v):2d}: refused — {e['refused']}")
        else:
            print(f"  vial {int(v):2d}: {e['ok']} ok, {e['out_of_range']} out of range, "
                  f"{e['dropped']} dropped"
                  + ("" if e.get("reanchored") else f"  [{e.get('offset_note')}]"))
    for w in report["warnings"]:
        print(f"warning: {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
