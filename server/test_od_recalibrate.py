"""Tests for post-hoc OD recalibration (server/od_recalibrate.py).

A run is simulated end to end through the SAME conversion the live reader
uses (``serial_manager.od_reading_from_samples``) and the real DataLogger:
the photodiode sees ``dark + S_true(OD)``, the run reads through its own
(wrong) pinned curve re-anchored at its blank, and the log is what the
recalibration has to work from.

Run from the project root:
    python -m pytest server/test_od_recalibrate.py
"""

from __future__ import annotations

import csv
import json
import math
import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import od_curve_fit as F  # noqa: E402
import od_recalibrate as R  # noqa: E402
from calibration_service import N_VIALS, make_envelope  # noqa: E402
from data_logger import OD_HEADER_V1, DataLogger  # noqa: E402
from serial_manager import od_reading_from_samples  # noqa: E402

REPO_CAL = Path(__file__).resolve().parent.parent / "calibration"
LEGACY = np.genfromtxt(REPO_CAL / "OD_cal.txt", delimiter=",")
DARK = 1200.0
VIALS = [0, 1, 2]


def _shift(rows, by):
    out = np.array(rows, dtype=float)
    out[0] += by
    out[1] += by
    return out


# The organism's TRUE dark-subtracted response, and the curve the run was
# (wrongly) given: the 2016 shape on dark-subtracted signal.
TRUTH = _shift(LEGACY, -DARK)
TRUTH[2] -= 0.35
TRUTH[3] *= 1.15
RUN_CURVE = _shift(LEGACY, -DARK)


def _env(rows, ds, version, profile=None):
    env = make_envelope("od", operator="t", source="od-dilution-series", conditions={"n": 1},
                        data={"rows": np.asarray(rows).tolist(), "dark_subtracted": ds},
                        version=version)
    if profile:
        env["profile"] = {"id": profile, "organism": profile.title()}
    return env


def _s_true(od):
    a, b, c, d = TRUTH
    return a + (b - a) / (1.0 + np.power(10.0, d * (c - od)))


def _c_at(rows, signal):
    a, b, _c, d = rows
    return np.log10((b - a) / (signal - a) - 1.0) / d


# None: a read below the dark asymptote (a real out_of_range, on any curve
# that shares this rig's floor).
TRUE_ODS = [0.05, 0.2, 0.45, 0.8, 1.2, 1.9, None]


def _simulate(tmp: Path, *, run_env, blank_vials=VIALS, dark_drops=(0, 3)) -> Path:
    """Log a run: one row per TRUE_ODS entry; the dark read drops on the
    ticks in ``dark_drops`` (tick 0 has no fallback -> dropped)."""
    root = tmp / "experiments"
    dl = DataLogger(root)
    dl.create_experiment(name="r", mode="turbidostat", vials=VIALS)
    exp = root / "r"
    rows, ds = R.target_rows(run_env)
    cfg = json.loads((exp / "config.json").read_text())
    cfg["calibration"] = {"od": run_env["version"], "od_profile": "wrong",
                          "od_organism": "Wrong", "od_dark_subtracted": ds}
    (exp / "config.json").write_text(json.dumps(cfg))
    (exp / "od_calibration.json").write_text(json.dumps(run_env))

    # The blank: sterile medium, true OD 0, read lit and dark.
    blank_lit = DARK + _s_true(0.0)
    blank_signal = blank_lit - DARK if ds else blank_lit
    c_run = _c_at(rows, blank_signal)
    blank = {
        "version": "2026-09-10T000000Z",
        "conditions": {"parent_od_cal": run_env["version"], "dark_subtracted": ds},
        "data": {"blank_median": blank_lit.tolist(), "dark_median": [DARK] * N_VIALS,
                 "blank_signal": blank_signal.tolist()},
        "fit": {"updated_rows": [2], "c_run": {str(v): float(c_run[v]) for v in blank_vials}},
    }
    (exp / "od_blank.json").write_text(json.dumps(blank))
    live = rows.copy()
    for v in blank_vials:
        live[2, v] = c_run[v]

    dl.activate_experiment("r")
    last_good = np.full(N_VIALS, np.nan)
    mask = np.full(N_VIALS, ds)
    for i, od in enumerate(TRUE_ODS):
        if od is None:
            lit = DARK + TRUTH[0] - 50.0
        else:
            lit = DARK + _s_true(np.full(N_VIALS, od)) + 0.37     # a fractional median
        dark = np.full(N_VIALS, np.nan if i in dark_drops else DARK)
        reading, last_good = od_reading_from_samples(
            lit, np.full(N_VIALS, 5), dark, np.where(np.isnan(dark), 0, 3),
            mask, live, last_good, 2125)
        dl.log_sensor_cycle(
            f"2026-09-29T12:{i:02d}:00+00:00", [30.0] * N_VIALS, [500.0] * N_VIALS,
            od_calibrated=reading.calibrated, od_raw=reading.raw,
            od_n_valid=reading.n_valid, od_flags=reading.flags, od_dark=reading.dark,
            od_lit=reading.lit, od_dark_used=reading.dark_used,
            od_dark_n_valid=reading.dark_n_valid,
            od_dark_subtracted=reading.dark_subtracted, od_led_power=2125,
        )
    dl.deactivate_experiment("r")
    return exp


def _csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _to_v1(exp: Path, *, drop_dark: bool = False) -> None:
    """Rewrite the run's OD files as a pre-upgrade run would have left them."""
    for v in VIALS:
        path = exp / f"vial{v:02d}_OD.csv"
        rows = _csv(path)
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(OD_HEADER_V1)
            for r in rows:
                w.writerow([r[k] if not (drop_dark and k == "dark") else ""
                            for k in OD_HEADER_V1])
    cfg = json.loads((exp / "config.json").read_text())
    cfg.pop("od_csv_schema", None)
    (exp / "config.json").write_text(json.dumps(cfg))


def test_reapplying_the_runs_own_curve_reproduces_its_od():
    with tempfile.TemporaryDirectory() as tmp:
        run_env = _env(RUN_CURVE, True, "2026-09-01T000000Z", "wrong")
        exp = _simulate(Path(tmp), run_env=run_env)
        blank = json.loads((exp / "od_blank.json").read_text())
        report = R.recalibrate_run(exp, run_env)
        assert report["id"] == "wrong__2026-09-01T000000Z"
        out = exp / "recalibrated" / report["id"]
        for v in VIALS:
            e = report["vials"][str(v)]
            assert e["reanchored"] is True
            assert abs(e["c"] - blank["fit"]["c_run"][str(v)]) < 1e-12
            logged = _csv(exp / f"vial{v:02d}_OD.csv")
            recal = _csv(out / f"vial{v:02d}_OD.csv")
            assert len(recal) == len(logged) == len(TRUE_ODS)
            for lo, re_ in zip(logged, recal):
                assert re_["flag"] == lo["flag"], (v, lo, re_)
                assert re_["original_od"] == lo["calibrated_od"]
                if lo["flag"] == "ok":
                    assert abs(float(re_["calibrated_od"]) - float(lo["calibrated_od"])) <= 5e-5 + 1e-9
                    assert re_["raw_adc"] == lo["raw_adc"]
            flags = [r["flag"] for r in recal]
            assert flags[0] == "dropped"        # no dark yet, no fallback
            assert flags[3] == "ok"             # dropped dark, last good used
            assert flags[-1] == "out_of_range"
            assert e["dark_fallback"] == 1 and e["dropped"] == 1
        # The run's own files are untouched.
        assert list(_csv(exp / "vial00_OD.csv")[0].keys())[:7] == list(OD_HEADER_V1)


def test_the_organisms_true_curve_recovers_the_true_od_in_either_signal_space():
    with tempfile.TemporaryDirectory() as tmp:
        exp = _simulate(Path(tmp), run_env=_env(RUN_CURVE, True, "2026-09-01T000000Z", "wrong"))
        for target in (_env(TRUTH, True, "2026-10-01T000000Z", "e-coli"),
                       _env(_shift(TRUTH, DARK), False, "2026-10-02T000000Z", "e-coli")):
            report = R.recalibrate_run(exp, target)
            out = exp / "recalibrated" / report["id"]
            recal = _csv(out / "vial01_OD.csv")
            assert recal[-1]["flag"] == "out_of_range"
            for i, (od, row) in enumerate(zip(TRUE_ODS, recal)):
                if row["flag"] != "ok":
                    continue
                # The run's own log was 0.2-0.4 OD off; the true curve is not.
                assert abs(float(row["calibrated_od"]) - od) < 1e-3, (i, od, row)
            logged = _csv(exp / "vial01_OD.csv")
            assert abs(float(logged[2]["calibrated_od"]) - TRUE_ODS[2]) > 0.05
        # A raw-signal target needs no dark: the first tick is not dropped.
        raw_report = next(r for r in R.list_recalibrations(exp)
                          if r["id"] == "e-coli__2026-10-02T000000Z")
        assert raw_report["target"]["dark_subtracted"] is False
        assert raw_report["vials"]["0"]["dropped"] == 0


def test_no_blank_or_an_excluded_vial_carries_the_curves_own_offset():
    with tempfile.TemporaryDirectory() as tmp:
        exp = _simulate(Path(tmp), run_env=_env(RUN_CURVE, True, "2026-09-01T000000Z", "wrong"),
                        blank_vials=[0, 1])
        target = _env(TRUTH, True, "2026-10-01T000000Z", "e-coli")
        report = R.recalibrate_run(exp, target)
        assert report["vials"]["2"]["reanchored"] is False
        assert "excluded" in report["vials"]["2"]["offset_note"]
        assert report["vials"]["2"]["c"] == TRUTH[2, 2]
        assert any("[2]" in w for w in report["warnings"])
        raw_c = R.recalibrate_run(exp, target, reanchor=False)
        assert raw_c["id"].endswith("__raw-c")
        assert all(not e["reanchored"] for e in raw_c["vials"].values())
        # Two distinct recalibrations, listed newest first.
        assert len(R.list_recalibrations(exp)) == 2


def test_a_pre_upgrade_log_is_reconstructed_or_refused_per_vial():
    with tempfile.TemporaryDirectory() as tmp:
        # A Session AA run: dark-subtracted, seven columns, dark column kept.
        exp = _simulate(Path(tmp) / "a", run_env=_env(RUN_CURVE, True, "2026-09-01T000000Z"))
        _to_v1(exp)
        truth_raw = _env(_shift(TRUTH, DARK), False, "2026-10-02T000000Z", "e-coli")
        report = R.recalibrate_run(exp, truth_raw)
        assert report["vials"]["0"]["source_schema"] == 1
        assert report["run"]["dark_subtracted"] is True
        recal = _csv(exp / "recalibrated" / report["id"] / "vial00_OD.csv")
        # raw_adc was rounded to a whole count; the OD error that leaves is tiny.
        for od, row in zip(TRUE_ODS, recal):
            if od is not None and row["flag"] == "ok":
                assert abs(float(row["calibrated_od"]) - od) < 2e-3, (od, row)
        # The 2016 situation: raw-signal run, no dark ever read.
        run2 = _env(LEGACY, False, "2016-01-01T000000Z")
        exp2 = _simulate(Path(tmp) / "b", run_env=run2)
        _to_v1(exp2, drop_dark=True)
        (exp2 / "od_calibration.json").unlink()
        cfg = json.loads((exp2 / "config.json").read_text())
        cfg["calibration"].pop("od_dark_subtracted")
        (exp2 / "config.json").write_text(json.dumps(cfg))
        ds_target = R.recalibrate_run(exp2, _env(TRUTH, True, "2026-10-01T000000Z", "e-coli"))
        assert all("no dark read" in e["refused"] for e in ds_target["vials"].values())
        assert ds_target["run"]["dark_subtracted_from"].startswith("assumed")
        ok = R.recalibrate_run(exp2, truth_raw)
        assert ok["vials"]["1"]["ok"] > 0 and "refused" not in ok["vials"]["1"]


def test_the_cli_and_replay_growth_read_a_recalibration():
    import replay_growth
    from calibration_service import CalibrationStore
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        exp = _simulate(tmp, run_env=_env(RUN_CURVE, True, "2026-09-01T000000Z", "wrong"))
        store = CalibrationStore(tmp / "calibration", experiments_root=tmp / "experiments")
        target = _env(TRUTH, True, "2026-10-01T000000Z")
        store.save_od_profile_version("e-coli", "E. coli", target)
        args = ["r", "--experiments-dir", str(tmp / "experiments"),
                "--calibration-dir", str(tmp / "calibration")]
        assert R.main(args + ["--profile", "nope"]) == 1
        assert R.main(args + ["--profile", "e-coli", "--vials", "0,1"]) == 0
        out = exp / "recalibrated" / "e-coli__2026-10-01T000000Z"
        assert sorted(p.name for p in out.iterdir()) == [
            "recalibration.json", "vial00_OD.csv", "vial01_OD.csv"]
        series = replay_growth.load_od_series(exp, 1, out)
        assert abs(series[1][1] - TRUE_ODS[2]) < 1e-3
        assert abs(replay_growth.load_od_series(exp, 1)[1][1] - TRUE_ODS[2]) > 0.05
        # Refused while running; vials outside the run are an error.
        assert R.main(args + ["--profile", "e-coli", "--vials", "9"]) == 1
        (exp / "state.json").write_text(json.dumps({"status": "running"}))
        assert R.main(args + ["--profile", "e-coli"]) == 1


def test_recalibration_ids_cannot_escape_the_run():
    with tempfile.TemporaryDirectory() as tmp:
        exp = _simulate(Path(tmp), run_env=_env(RUN_CURVE, True, "2026-09-01T000000Z", "wrong"))
        rid = R.recalibrate_run(exp, _env(TRUTH, True, "2026-10-01T000000Z", "e-coli"))["id"]
        assert R.recalibration_dir(exp, rid).name == rid
        for bad in ("../r", "e-coli__2026/../..", "", ".e-coli__x", "E-COLI__x"):
            with pytest.raises(R.RecalibrationError):
                R.recalibration_dir(exp, bad)
        with pytest.raises(FileNotFoundError):
            R.recalibration_dir(exp, "e-coli__2027-01-01T000000Z")
        assert not any(p.name.startswith(".") for p in (exp / "recalibrated").iterdir())
        assert math.isfinite(R.list_recalibrations(exp)[0]["vials"]["0"]["c"])
