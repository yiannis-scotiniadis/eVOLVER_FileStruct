"""Verification script for the DataLogger (SPEC §8).

Run from the project root:
    python server/test_data_logger.py
"""

from __future__ import annotations

import csv
import json
import math
import shutil
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from data_logger import (  # noqa: E402
    N_VIALS,
    OD_HEADER,
    PUMP_HEADER,
    TEMP_HEADER,
    DataLogger,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _make_arrays(n: int = N_VIALS):
    """16 vials of dummy sensor data, distinct per vial so the tests can
    verify the right row landed in the right CSV."""
    temp_cal = [37.0 + i * 0.1 for i in range(n)]
    temp_raw = [400 + i for i in range(n)]
    od_cal = [0.1 + i * 0.01 for i in range(n)]
    od_raw = [50000 + i * 10 for i in range(n)]
    return temp_cal, temp_raw, od_cal, od_raw


def _read_csv(path: Path):
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.reader(f))


class TmpRoot:
    """Context manager that gives a fresh temp directory and cleans up."""

    def __enter__(self) -> Path:
        self.path = Path(tempfile.mkdtemp(prefix="evolver-test-"))
        return self.path

    def __exit__(self, *args) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


def test_start_experiment_creates_directory_and_config() -> None:
    with TmpRoot() as root:
        dl = DataLogger(root)
        config = dl.start_experiment(
            name="exp1",
            mode="turbidostat",
            vials=[0, 3, 7],
            parameters={"temperature_c": 37, "stir_rate": 10},
            notes="hello",
        )
        exp_dir = root / "exp1"
        assert exp_dir.is_dir()
        assert (exp_dir / "config.json").is_file()
        saved = json.loads((exp_dir / "config.json").read_text(encoding="utf-8"))
        assert saved == config
        assert saved["mode"] == "turbidostat"
        assert saved["vials"] == [0, 3, 7]
        assert saved["parameters"]["temperature_c"] == 37
        assert saved["notes"] == "hello"
        # Only files for selected vials, one OD + one temp + one pump_log each.
        for v in [0, 3, 7]:
            assert (exp_dir / f"vial{v:02d}_OD.csv").is_file()
            assert (exp_dir / f"vial{v:02d}_temp.csv").is_file()
            assert (exp_dir / f"vial{v:02d}_pump_log.csv").is_file()
        for v in [1, 2, 4, 5, 6, 8, 15]:
            assert not (exp_dir / f"vial{v:02d}_OD.csv").exists()
        # Headers match SPEC §8.
        assert _read_csv(exp_dir / "vial00_OD.csv")[0] == list(OD_HEADER)
        assert _read_csv(exp_dir / "vial00_temp.csv")[0] == list(TEMP_HEADER)
        assert _read_csv(exp_dir / "vial00_pump_log.csv")[0] == list(PUMP_HEADER)
        dl.stop_experiment()
    print("PASS  start_experiment creates directory, config.json, and per-vial CSVs")


def test_log_sensor_cycle_appends_rows() -> None:
    with TmpRoot() as root:
        dl = DataLogger(root)
        dl.start_experiment(name="exp1", mode="turbidostat", vials=[0, 5])
        temp_cal, temp_raw, od_cal, od_raw = _make_arrays()
        ts1 = _now_iso()
        dl.log_sensor_cycle(ts1, temp_cal, temp_raw, od_cal, od_raw)
        ts2 = _now_iso()
        dl.log_sensor_cycle(ts2, temp_cal, temp_raw, od_cal, od_raw)
        dl.stop_experiment()

        rows = _read_csv(root / "exp1" / "vial00_OD.csv")
        assert rows[0] == list(OD_HEADER)
        assert len(rows) == 3, f"expected header + 2 rows, got {len(rows)}"
        # raw_adc must be an integer per the SPEC §8 format example.
        assert rows[1][2] == "50000"
        assert rows[1][3] == f"{0.1:.4f}"
        assert rows[1][0] == ts1

        rows5 = _read_csv(root / "exp1" / "vial05_OD.csv")
        # vial 5 should get vial 5's data (od_cal[5] = 0.15, od_raw[5] = 50050)
        assert rows5[1][2] == "50050"
        assert rows5[1][3] == f"{0.15:.4f}"

        # Vial not in the experiment should have no file at all.
        assert not (root / "exp1" / "vial03_OD.csv").exists()
    print("PASS  log_sensor_cycle appends one row per active vial per cycle")


def test_idle_logger_is_noop() -> None:
    """log_sensor_cycle / log_pump_event before start_experiment must
    silently do nothing and create no files."""
    with TmpRoot() as root:
        dl = DataLogger(root)
        temp_cal, temp_raw, od_cal, od_raw = _make_arrays()
        dl.log_sensor_cycle(_now_iso(), temp_cal, temp_raw, od_cal, od_raw)
        dl.log_pump_event(_now_iso(), 0, "influx", 5.0)
        # The root exists (created in __init__) but should be empty.
        assert list(root.iterdir()) == []
    print("PASS  logging is a no-op before start_experiment")


def test_stop_makes_subsequent_logging_noop() -> None:
    with TmpRoot() as root:
        dl = DataLogger(root)
        dl.start_experiment(name="exp1", mode="manual", vials=[0])
        temp_cal, temp_raw, od_cal, od_raw = _make_arrays()
        dl.log_sensor_cycle(_now_iso(), temp_cal, temp_raw, od_cal, od_raw)
        dl.stop_experiment()
        dl.log_sensor_cycle(_now_iso(), temp_cal, temp_raw, od_cal, od_raw)
        dl.log_pump_event(_now_iso(), 0, "influx", 3.0)
        # Only one data row should have been recorded (plus the header).
        rows = _read_csv(root / "exp1" / "vial00_OD.csv")
        assert len(rows) == 2, f"expected header + 1 row, got {len(rows)}"
        pumps = _read_csv(root / "exp1" / "vial00_pump_log.csv")
        assert len(pumps) == 1, "expected pump log to still contain only the header"
    print("PASS  stop_experiment makes log_* a no-op")


def test_log_pump_event_appends_with_cached_od() -> None:
    """If od_at_pump is omitted, the logger fills it from the most recent
    sensor cycle's calibrated OD for that vial."""
    with TmpRoot() as root:
        dl = DataLogger(root)
        dl.start_experiment(name="exp1", mode="turbidostat", vials=[0, 3])
        temp_cal, temp_raw, od_cal, od_raw = _make_arrays()
        dl.log_sensor_cycle(_now_iso(), temp_cal, temp_raw, od_cal, od_raw)
        dl.log_pump_event(_now_iso(), 3, "influx", 8.2)
        dl.log_pump_event(_now_iso(), 3, "efflux", 13.2, od_at_pump=0.99)
        dl.stop_experiment()

        rows = _read_csv(root / "exp1" / "vial03_pump_log.csv")
        assert rows[0] == list(PUMP_HEADER)
        assert len(rows) == 3, rows
        assert rows[1][2] == "influx"
        assert rows[1][3] == "8.20"
        # vial 3 od_cal was 0.13 -> cached and used.
        assert rows[1][4] == f"{0.13:.4f}"
        assert rows[2][2] == "efflux"
        assert rows[2][3] == "13.20"
        # Explicit od_at_pump must override the cache.
        assert rows[2][4] == f"{0.99:.4f}"
    print("PASS  log_pump_event uses cached OD; explicit od_at_pump wins")


def test_log_pump_event_for_nonactive_vial_is_ignored() -> None:
    """Manual pump of a vial that isn't in the experiment must not crash
    and must not log anything to a (non-existent) pump file."""
    with TmpRoot() as root:
        dl = DataLogger(root)
        dl.start_experiment(name="exp1", mode="manual", vials=[0, 1])
        dl.log_pump_event(_now_iso(), 7, "influx", 1.0)
        dl.stop_experiment()
        assert not (root / "exp1" / "vial07_pump_log.csv").exists()
    print("PASS  log_pump_event ignores vials outside the active experiment")


def test_elapsed_hours_is_monotonic() -> None:
    """elapsed_hours is computed against the experiment start time."""
    with TmpRoot() as root:
        dl = DataLogger(root)
        dl.start_experiment(name="exp1", mode="manual", vials=[0])
        # Cheat: rewrite the start time to 1 hour ago to verify elapsed_hours.
        dl._active_start = datetime.now(timezone.utc).replace(microsecond=0)
        dl._active_start = dl._active_start.fromtimestamp(
            dl._active_start.timestamp() - 3600, tz=timezone.utc
        )
        temp_cal, temp_raw, od_cal, od_raw = _make_arrays()
        ts = _now_iso()
        dl.log_sensor_cycle(ts, temp_cal, temp_raw, od_cal, od_raw)
        dl.stop_experiment()
        rows = _read_csv(root / "exp1" / "vial00_OD.csv")
        elapsed = float(rows[1][1])
        assert 0.99 < elapsed < 1.01, f"expected ~1.0 h, got {elapsed}"
    print(f"PASS  elapsed_hours computes from experiment start ({elapsed:.4f} h)")


def test_nan_values_emit_empty_strings() -> None:
    """Failed sensor reads come through as NaN; CSV cells should be empty
    so pandas / Excel see them as missing rather than literal 'nan'."""
    with TmpRoot() as root:
        dl = DataLogger(root)
        dl.start_experiment(name="exp1", mode="manual", vials=[0])
        temp_cal = [float("nan")] * N_VIALS
        temp_raw = [float("nan")] * N_VIALS
        od_cal = [float("nan")] * N_VIALS
        od_raw = [float("nan")] * N_VIALS
        dl.log_sensor_cycle(_now_iso(), temp_cal, temp_raw, od_cal, od_raw)
        # Pump with NaN cached OD should still write the row, with empty od_at_pump.
        dl.log_pump_event(_now_iso(), 0, "influx", 5.0)
        dl.stop_experiment()
        od_rows = _read_csv(root / "exp1" / "vial00_OD.csv")
        assert od_rows[1][2] == ""
        assert od_rows[1][3] == ""
        pump_rows = _read_csv(root / "exp1" / "vial00_pump_log.csv")
        assert pump_rows[1][4] == ""
    print("PASS  NaN sensor values render as empty CSV cells")


def test_od_diagnostics_columns() -> None:
    with TmpRoot() as root:
        dl = DataLogger(root)
        dl.start_experiment(name="exp1", mode="turbidostat", vials=[0, 1])
        temp_cal, temp_raw, od_cal, od_raw = _make_arrays()
        n_valid = [5] * N_VIALS
        flags = ["ok"] * N_VIALS
        flags[0] = "out_of_range"
        flags[1] = "dropped"
        dark = [2000 + i for i in range(N_VIALS)]
        dl.log_sensor_cycle(
            _now_iso(), temp_cal, temp_raw, od_cal, od_raw,
            od_n_valid=n_valid, od_flags=flags, od_dark=dark,
        )
        # A second cycle WITHOUT diagnostics (naive read) -> blank columns.
        dl.log_sensor_cycle(_now_iso(), temp_cal, temp_raw, od_cal, od_raw)
        dl.stop_experiment()

        rows0 = _read_csv(root / "exp1" / "vial00_OD.csv")
        assert rows0[0] == list(OD_HEADER)
        # columns: timestamp, elapsed, raw_adc, calibrated, n_valid, flag, dark
        assert rows0[1][4] == "5" and rows0[1][5] == "out_of_range"
        assert rows0[1][6] == "2000"
        # Naive second row leaves the diagnostic columns blank.
        assert rows0[2][4] == "" and rows0[2][5] == "" and rows0[2][6] == ""

        rows1 = _read_csv(root / "exp1" / "vial01_OD.csv")
        assert rows1[1][5] == "dropped" and rows1[1][6] == "2001"
    print("PASS  OD diagnostics columns (n_valid, flag, dark) written and blank-safe")


def test_cannot_start_two_experiments() -> None:
    with TmpRoot() as root:
        dl = DataLogger(root)
        dl.start_experiment(name="exp1", mode="manual", vials=[0])
        raised = False
        try:
            dl.start_experiment(name="exp2", mode="manual", vials=[1])
        except RuntimeError:
            raised = True
        assert raised, "second start_experiment should have raised"
        dl.stop_experiment()
        # Now it should succeed.
        dl.start_experiment(name="exp2", mode="manual", vials=[1])
        dl.stop_experiment()
    print("PASS  cannot start two experiments concurrently")


def test_duplicate_directory_rejected() -> None:
    with TmpRoot() as root:
        dl = DataLogger(root)
        dl.start_experiment(name="exp1", mode="manual", vials=[0])
        dl.stop_experiment()
        raised = False
        try:
            dl.start_experiment(name="exp1", mode="manual", vials=[1])
        except FileExistsError:
            raised = True
        assert raised
    print("PASS  duplicate experiment directory rejected")


def test_invalid_inputs_rejected() -> None:
    with TmpRoot() as root:
        dl = DataLogger(root)
        bad_cases = [
            {"name": "../escape", "mode": "m", "vials": [0]},
            {"name": "has space", "mode": "m", "vials": [0]},
            {"name": "ok", "mode": "m", "vials": []},
            {"name": "ok", "mode": "m", "vials": [16]},
            {"name": "ok", "mode": "m", "vials": [-1]},
            {"name": "ok", "mode": "m", "vials": [0, 0]},
            {"name": "ok", "mode": "m", "vials": [True]},  # bool is not int here
        ]
        for case in bad_cases:
            try:
                dl.start_experiment(**case)
            except ValueError:
                pass
            else:
                raise AssertionError(f"accepted bad input: {case}")
        # log_pump_event also validates direction.
        dl.start_experiment(name="ok", mode="m", vials=[0])
        try:
            dl.log_pump_event(_now_iso(), 0, "sideways", 1.0)
        except ValueError:
            pass
        else:
            raise AssertionError("accepted invalid direction")
        dl.stop_experiment()
    print("PASS  invalid inputs rejected (name, vials, direction)")


def test_list_experiments() -> None:
    with TmpRoot() as root:
        dl = DataLogger(root)
        dl.start_experiment(name="exp_a", mode="manual", vials=[0])
        dl.stop_experiment()
        dl.start_experiment(name="exp_b", mode="turbidostat", vials=[1, 2])
        entries = dl.list_experiments()
        names = {e["name"] for e in entries}
        assert names == {"exp_a", "exp_b"}
        running = next(e for e in entries if e["name"] == "exp_b")
        stopped = next(e for e in entries if e["name"] == "exp_a")
        assert running["status"] == "running"
        assert stopped["status"] == "stopped"
        assert running["config"]["mode"] == "turbidostat"
        dl.stop_experiment()
    print("PASS  list_experiments reports running vs stopped correctly")


def test_concurrent_logging() -> None:
    """Two threads logging into the same experiment must not lose rows
    nor corrupt CSV lines."""
    with TmpRoot() as root:
        dl = DataLogger(root)
        dl.start_experiment(name="exp1", mode="manual", vials=list(range(N_VIALS)))
        temp_cal, temp_raw, od_cal, od_raw = _make_arrays()
        per_thread = 50
        errors: list[BaseException] = []

        def writer():
            try:
                for _ in range(per_thread):
                    dl.log_sensor_cycle(
                        _now_iso(), temp_cal, temp_raw, od_cal, od_raw
                    )
                    dl.log_pump_event(_now_iso(), 0, "influx", 1.5)
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=writer) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors, errors
        rows = _read_csv(root / "exp1" / "vial00_OD.csv")
        # header + 4 threads * 50 rows = 201
        assert len(rows) == 1 + 4 * per_thread, f"row count {len(rows)}"
        # Every data row should parse cleanly (no torn writes).
        for row in rows[1:]:
            assert len(row) == len(OD_HEADER), row
            assert math.isclose(float(row[3]), 0.1, abs_tol=1e-9)
        pumps = _read_csv(root / "exp1" / "vial00_pump_log.csv")
        assert len(pumps) == 1 + 4 * per_thread
        dl.stop_experiment()
    print("PASS  concurrent logging is row-safe across threads")


def test_create_then_activate_split() -> None:
    """create_experiment writes the directory + CSVs but does NOT begin
    logging; activate_experiment flips into write mode."""
    with TmpRoot() as root:
        dl = DataLogger(root)
        config = dl.create_experiment(name="split1", mode="turbidostat", vials=[0, 2])
        assert config["name"] == "split1"
        assert (root / "split1" / "config.json").is_file()
        assert (root / "split1" / "vial00_OD.csv").is_file()
        assert (root / "split1" / "vial02_OD.csv").is_file()
        assert dl.is_running is False, "create_experiment must not flip is_running"

        # Logging is still a no-op while only CREATED.
        temp_cal, temp_raw, od_cal, od_raw = _make_arrays()
        dl.log_sensor_cycle(_now_iso(), temp_cal, temp_raw, od_cal, od_raw)
        assert _read_csv(root / "split1" / "vial00_OD.csv") == [list(OD_HEADER)], \
            "logging should be inert in CREATED state"

        # Activate, then logging should write rows.
        active = dl.activate_experiment("split1")
        assert dl.is_running is True
        assert active["vials"] == [0, 2]
        dl.log_sensor_cycle(_now_iso(), temp_cal, temp_raw, od_cal, od_raw)
        rows = _read_csv(root / "split1" / "vial00_OD.csv")
        assert len(rows) == 2, f"expected header + 1 row, got {len(rows)}"

        # Deactivate and another activate must work too.
        dl.deactivate_experiment()
        assert dl.is_running is False
        dl.activate_experiment("split1")
        assert dl.is_running is True
        dl.deactivate_experiment()
    print("PASS  create_experiment / activate_experiment split works (logging inert in CREATED)")


def test_activate_with_explicit_start_preserves_elapsed_hours() -> None:
    """A resumed experiment passes its original `started` timestamp so
    elapsed_hours in CSV rows stays continuous across restarts."""
    with TmpRoot() as root:
        dl = DataLogger(root)
        dl.create_experiment(name="resume1", mode="manual", vials=[0])
        # Simulate: original experiment started 2 hours ago.
        original_start = datetime.now(timezone.utc).replace(microsecond=0)
        original_start = original_start.fromtimestamp(
            original_start.timestamp() - 7200, tz=timezone.utc
        )
        dl.activate_experiment("resume1", start=original_start)
        temp_cal, temp_raw, od_cal, od_raw = _make_arrays()
        dl.log_sensor_cycle(_now_iso(), temp_cal, temp_raw, od_cal, od_raw)
        dl.deactivate_experiment()
        rows = _read_csv(root / "resume1" / "vial00_OD.csv")
        elapsed = float(rows[1][1])
        assert 1.99 < elapsed < 2.01, (
            f"expected ~2 h elapsed (from resumed start time), got {elapsed}"
        )
    print(f"PASS  activate_experiment(start=...) preserves elapsed_hours across resume ({elapsed:.4f} h)")


def test_activate_missing_directory_raises() -> None:
    with TmpRoot() as root:
        dl = DataLogger(root)
        try:
            dl.activate_experiment("does_not_exist")
        except FileNotFoundError:
            print("PASS  activate_experiment raises FileNotFoundError for missing dir")
            return
        raise AssertionError("expected FileNotFoundError")


def main() -> int:
    test_start_experiment_creates_directory_and_config()
    test_log_sensor_cycle_appends_rows()
    test_idle_logger_is_noop()
    test_stop_makes_subsequent_logging_noop()
    test_log_pump_event_appends_with_cached_od()
    test_log_pump_event_for_nonactive_vial_is_ignored()
    test_elapsed_hours_is_monotonic()
    test_nan_values_emit_empty_strings()
    test_od_diagnostics_columns()
    test_cannot_start_two_experiments()
    test_duplicate_directory_rejected()
    test_invalid_inputs_rejected()
    test_list_experiments()
    test_concurrent_logging()
    test_create_then_activate_split()
    test_activate_with_explicit_start_preserves_elapsed_hours()
    test_activate_missing_directory_raises()
    print("\nAll data_logger tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
