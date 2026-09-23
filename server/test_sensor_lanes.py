"""The split sensor loop: a fast safety lane and a decimated OD lane.

Both lanes currently run at 10 s (``app.py`` OD_EVERY_N_TICKS = 1), so the
temperature-only tick these tests drive is not exercised in production right
now. They are kept because decimating the OD lane is what buys time to stop
the stirrers before a read, and this is the machinery that has to still be
correct when that comes back.

Covers the properties the split is supposed to preserve, each of which is a
way it could silently go wrong:

  * heater safety must keep running on the fast lane -- if it drifted onto the
    OD lane its 3-consecutive-reads latch would go from 30 s to 3 min;
  * OD-dependent work must NOT run on the fast lane, or a controller decides
    six times on one sample;
  * counters that tick on different lanes must not reset each other;
  * the dashboard must be servable from cache, or a page load renders empty
    for up to a full OD period.

Run from the project root:
    python -m pytest server/test_sensor_lanes.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from experiment_engine import (  # noqa: E402
    DEFAULT_CYCLE_INTERVAL_SECONDS,
    N_VIALS,
    validate_control_parameters,
)
import event_log as evlog  # noqa: E402

from test_experiment_engine import (  # noqa: E402
    TmpRoot,
    _fresh,
    _make_sensor_arrays,
)


def _turb(**overrides) -> dict:
    p = {
        "temperature_c": 37, "stir_rate": 10,
        "od_lower_thresh": 0.2, "od_upper_thresh": 0.4,
        "min_samples_before_action": 1,
        "pump_wait_minutes": 1,
    }
    p.update(overrides)
    return p


# ---------------------------------------------------------------------------
# run_cycle: what each lane does
# ---------------------------------------------------------------------------

def test_fast_tick_runs_heater_safety_but_makes_no_control_decision() -> None:
    """A temperature-only tick must still be able to latch an overtemp fault.

    This is the whole reason the loop was split rather than simply slowed to
    60 s: _handle_heater_safety_locked needs three consecutive over-critical
    reads, so its detection latency is 3x the period it runs at.
    """
    with TmpRoot() as root:
        engine, *_ = _fresh(root)
        engine.create_experiment(
            name="t", mode="turbidostat", vials=[0],
            parameters=_turb(pump_wait_minutes=0.0),
        )
        engine.start_experiment("t")
        temps, _ = _make_sensor_arrays(temp_c=99.0)
        actions = []
        for _ in range(3):
            # od_calibrated omitted entirely -> fast tick
            actions.extend(engine.run_cycle("2026-05-14T10:00:00+00:00", temps))
            engine._clock.state["t"] += 10.0
        assert engine._vial_faults[0] == "overtemp", engine._vial_faults
        assert actions == [], f"a fast tick must not fire pumps, got {actions}"
        engine.stop_experiment(reason="cleanup")


def test_fast_tick_does_not_advance_the_controller() -> None:
    """OD history and dilution decisions belong to the OD lane. If a fast tick
    pushed OD, the controller's 5-sample rolling mean would span 50 s of one
    repeated sample instead of five real ones."""
    with TmpRoot() as root:
        engine, *_ = _fresh(root)
        engine.create_experiment(
            name="t", mode="turbidostat", vials=[0],
            parameters=_turb(pump_wait_minutes=0.0),
        )
        engine.start_experiment("t")
        temps, ods = _make_sensor_arrays(od=0.5)
        c = engine._controllers[0]
        for _ in range(5):
            engine.run_cycle("2026-05-14T10:00:00+00:00", temps)
            engine._clock.state["t"] += 10.0
        assert c.total_samples_seen == 0, "fast tick pushed OD into the controller"
        assert len(engine._od_history[0]) == 0, "fast tick pushed OD history"

        engine.run_cycle("2026-05-14T10:01:00+00:00", temps, ods)
        assert c.total_samples_seen == 1
        assert len(engine._od_history[0]) == 1
        engine.stop_experiment(reason="cleanup")


def test_temperature_and_od_streaks_do_not_reset_each_other() -> None:
    """The lanes tick at different rates, so a good read on one must not clear
    the other's streak. A single fused counter is reset by the fast lane five
    times per OD period and an OD dropout streak could never reach 3."""
    nan = float("nan")
    with TmpRoot() as root:
        engine, *_ = _fresh(root)
        engine.create_experiment(
            name="t", mode="turbidostat", vials=[0],
            parameters=_turb(),
        )
        engine.start_experiment("t")
        temps, _ = _make_sensor_arrays()
        # Three dropped OD reads on the OD lane.
        for _ in range(3):
            engine.run_cycle(
                "2026-05-14T10:00:00+00:00", temps, [nan] * N_VIALS,
                od_flags=["dropped"] * N_VIALS,
            )
            engine._clock.state["t"] += 60.0
        assert engine._od_nan_streak[0] == 3

        # Good temperature-only ticks in between must NOT clear it.
        for _ in range(5):
            engine.run_cycle("2026-05-14T10:01:00+00:00", temps)
            engine._clock.state["t"] += 10.0
        assert engine._od_nan_streak[0] == 3, "a fast tick erased the OD streak"
        assert engine._temp_nan_streak[0] == 0
        engine.stop_experiment(reason="cleanup")


def test_legacy_fused_nan_streak_restores_into_both_lanes() -> None:
    """state.json written before the split carries one fused `nan_streak`."""
    with TmpRoot() as root:
        engine, *_ = _fresh(root)
        engine.create_experiment(
            name="t", mode="turbidostat", vials=[0],
            parameters=_turb(),
        )
        engine.start_experiment("t")
        engine.stop_experiment(reason="cleanup")

        state_path = root / "t" / "state.json"
        state = json.loads(state_path.read_text())
        state["status"] = "running"
        state.pop("temp_nan_streak", None)
        state.pop("od_nan_streak", None)
        state["nan_streak"] = {"0": 2}
        state_path.write_text(json.dumps(state))

        engine2, *_ = _fresh(root)
        resumed = engine2.resume_on_startup()
        assert resumed == "t", resumed
        # Conservative: seed BOTH lanes, so a warning can only come sooner.
        assert engine2._temp_nan_streak[0] == 2, engine2._temp_nan_streak
        assert engine2._od_nan_streak[0] == 2, engine2._od_nan_streak
        engine2.stop_experiment(reason="cleanup")


# ---------------------------------------------------------------------------
# Chemostat: bolus interval vs the lane that honours it
# ---------------------------------------------------------------------------

def test_bolus_interval_below_the_control_interval_is_rejected() -> None:
    """decide() is only called once per control tick, so a shorter bolus
    interval is not honoured -- and because the bolus is sized from ELAPSED
    time while the overlap safety cap is sized from the NOMINAL interval,
    every bolus would clip and the run would under-deliver silently."""
    rates = [1.0] * N_VIALS
    below = [DEFAULT_CYCLE_INTERVAL_SECONDS * f for f in (0.2, 0.5, 0.9)]
    for interval in below:
        with pytest.raises(ValueError) as exc:
            validate_control_parameters(
                "chemostat",
                {"dilution_rate_per_hour": 0.5,
                 "bolus_interval_seconds": interval},
                rates, [0],
                control_interval_seconds=DEFAULT_CYCLE_INTERVAL_SECONDS,
            )
        assert "control interval" in str(exc.value), exc.value
    # At or above the control interval it is accepted.
    for interval in (DEFAULT_CYCLE_INTERVAL_SECONDS,
                     DEFAULT_CYCLE_INTERVAL_SECONDS * 6):
        validate_control_parameters(
            "chemostat",
            {"dilution_rate_per_hour": 0.5, "bolus_interval_seconds": interval},
            rates, [0],
            control_interval_seconds=DEFAULT_CYCLE_INTERVAL_SECONDS,
        )


# ---------------------------------------------------------------------------
# Health tracking across lanes
# ---------------------------------------------------------------------------

def test_vial_health_od_streak_survives_fast_ticks() -> None:
    vh = evlog.VialHealth(4, degraded_threshold=3)
    for _ in range(3):
        vh.record_cycle(
            temperature=[30.0] * 4,
            od_flags=["dropped"] * 4,
            od_n_valid=[0] * 4,
            od_acquired=True,
        )
    assert vh.snapshot()[0]["state"] == "degraded"
    # Five good temperature-only ticks must not clear it.
    for _ in range(5):
        vh.record_cycle(temperature=[30.0] * 4, od_acquired=False)
    snap = vh.snapshot()[0]
    assert snap["state"] == "degraded", snap
    assert snap["od_dropped_streak"] == 3, snap
    assert snap["temp_dropped_streak"] == 0, snap


def test_bus_health_od_not_marked_down_by_fast_ticks() -> None:
    """A temperature-only tick is not an OD failure. Recording it as one would
    drive the OD bus to "down" within three fast ticks and pin it there."""
    bus = evlog.BusHealth(failure_threshold=3)
    vials = evlog.VialHealth(N_VIALS)
    for _ in range(10):
        evlog.classify_cycle(
            bus, vials,
            temperature=[30.0] * N_VIALS,
            od_acquired=False,
        )
    assert bus.snapshot()["od"]["state"] == "ok", bus.snapshot()


# ---------------------------------------------------------------------------
# The cached snapshot the dashboard paints from
# ---------------------------------------------------------------------------

def _make_app(tmp: Path):
    import app as A
    A.EXPERIMENTS_DIR = tmp / "experiments"
    A.EXPERIMENTS_DIR.mkdir(parents=True, exist_ok=True)
    A.EXPORTS_DIR = tmp / "exports"
    A.LOGS_DIR = tmp / "logs"
    flask_app, _socketio = A.create_app(use_mock=True)
    return A, flask_app


def test_sensors_latest_is_served_from_cache_without_serial_io() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        A, flask_app = _make_app(Path(tmp))
        c = flask_app.test_client()

        # Before the loop has completed a tick there is nothing to serve, and
        # the route must say so rather than performing a live read.
        r = c.get("/api/sensors/latest")
        assert r.status_code in (200, 503), r.status_code

        # Seed the cache the way the loop does and confirm the route reads it.
        state = _closure(flask_app.view_functions["api_sensor_latest"], "state")
        payload = {
            "timestamp": "2026-05-14T10:00:00+00:00",
            "temperature": {"calibrated": [30.0] * N_VIALS, "raw": [0] * N_VIALS},
            "od": {"calibrated": [0.4] * N_VIALS, "raw": [0] * N_VIALS,
                   "timestamp": "2026-05-14T09:59:00+00:00", "age_seconds": 60.0},
            "intervals": {"base_seconds": 10.0, "od_seconds": 60.0},
        }
        state.last_sensor_update = payload

        # Count real reads by wrapping the manager. MockSerialManager keeps no
        # counter of its own, and an assertion that silently no-ops is worse
        # than no assertion -- this is the claim the route's docstring makes,
        # so it has to actually be checked.
        reads = {"n": 0}
        for meth in ("read_temperature", "read_od", "read_od_enhanced"):
            orig = getattr(state.manager, meth)

            def wrapped(*a, _o=orig, **k):
                reads["n"] += 1
                return _o(*a, **k)
            setattr(state.manager, meth, wrapped)

        got = c.get("/api/sensors/latest").get_json()
        assert got["timestamp"] == payload["timestamp"]
        assert got["od"]["timestamp"] == payload["od"]["timestamp"]
        assert got["intervals"]["od_seconds"] == 60.0
        assert "age_seconds" in got
        assert reads["n"] == 0, "/api/sensors/latest performed serial I/O"

        # Contrast: the live-read route DOES hit the bus. That difference is
        # the reason /api/sensors/latest exists.
        c.get("/api/sensors/temperature")
        assert reads["n"] > 0, "expected /api/sensors/temperature to read the bus"


def _closure(fn, name):
    return fn.__closure__[fn.__code__.co_freevars.index(name)].cell_contents


def test_a_nonzero_settle_is_refused() -> None:
    """The stirrers never stop, and the only way a settle could currently be
    honoured is by blocking the sensor thread -- which stalls the TEMPERATURE
    lane too and multiplies over-temperature detection latency by three. The
    value is refused rather than documented, because that failure is silent
    and lands on the safety path."""
    import app as A
    saved = A.OD_SETTLE_SECONDS
    try:
        for bad in (0.5, 2.0, 30.0, -1.0):
            with pytest.raises(ValueError) as exc:
                A.configure_cadence(od_settle_seconds=bad)
            assert "not implemented" in str(exc.value), exc.value
        assert A.OD_SETTLE_SECONDS == 0.0
        A.configure_cadence(od_settle_seconds=0.0)  # zero is fine
        assert A.OD_SETTLE_SECONDS == 0.0
    finally:
        A.OD_SETTLE_SECONDS = saved


def test_od_lane_is_currently_every_tick() -> None:
    """The shipped configuration: one 10 s cycle for temperature AND OD, and
    no stir interruption. Guards against the decimation being re-enabled
    without the growth-rate span constants being re-derived alongside it
    (GROWTH_RATE_METHOD.md 5.2)."""
    import app as A
    import growth_rate as g
    assert A.OD_EVERY_N_TICKS == 1, "OD lane is decimated"
    assert A.OD_INTERVAL_SECONDS == A.SENSOR_LOOP_INTERVAL_SECONDS
    assert A.OD_SETTLE_SECONDS == 0.0, "a settle implies stopping the stirrers"
    # The constants that must move with the OD cadence, at their 10 s values.
    assert g.PREFERRED_FIT_SPAN_SECONDS == 1800.0
    assert g.MIN_SAMPLES == 30
    assert g.HISTORY_WINDOW_SECONDS == 3 * 3600.0


def test_configure_cadence_recomputes_the_od_interval() -> None:
    import app as A
    saved = (A.SENSOR_LOOP_INTERVAL_SECONDS, A.OD_EVERY_N_TICKS,
             A.OD_SETTLE_SECONDS, A.OD_INTERVAL_SECONDS)
    try:
        A.configure_cadence(base_seconds=5.0, od_every=4, od_settle_seconds=0.0)
        assert A.OD_INTERVAL_SECONDS == 20.0
        with pytest.raises(ValueError):
            A.configure_cadence(od_every=0)
        with pytest.raises(ValueError):
            A.configure_cadence(base_seconds=0)
    finally:
        (A.SENSOR_LOOP_INTERVAL_SECONDS, A.OD_EVERY_N_TICKS,
         A.OD_SETTLE_SECONDS, A.OD_INTERVAL_SECONDS) = saved


# ---------------------------------------------------------------------------
# Logging: two files, two row rates
# ---------------------------------------------------------------------------

def test_temperature_rows_outnumber_od_rows() -> None:
    """vialNN_temp.csv and vialNN_OD.csv are separate files read
    independently, so a fast lane that logs only temperature is safe. What
    must NOT change is either file's COLUMNS -- data_export has no schema
    marker, so a positional parser breaks on an inserted column."""
    from data_logger import DataLogger
    with TmpRoot() as root:
        dl = DataLogger(root)
        dl.create_experiment(name="x", mode="turbidostat", vials=[0])
        dl.activate_experiment("x")
        temps = [30.0] * N_VIALS
        for i in range(6):
            kwargs = dict(
                timestamp_iso=f"2026-05-14T10:00:{i:02d}+00:00",
                temperature_calibrated=temps, temperature_raw=temps,
            )
            if i == 0:  # one OD tick in six
                kwargs.update(od_calibrated=[0.4] * N_VIALS,
                              od_raw=[100.0] * N_VIALS)
            dl.log_sensor_cycle(**kwargs)
        dl.deactivate_experiment()

        temp_rows = (root / "x" / "vial00_temp.csv").read_text().strip().splitlines()
        od_rows = (root / "x" / "vial00_OD.csv").read_text().strip().splitlines()
        # header + N data rows
        assert len(temp_rows) == 1 + 6, temp_rows
        assert len(od_rows) == 1 + 1, od_rows
        # Column count is unchanged in both files.
        assert len(temp_rows[0].split(",")) == 4, temp_rows[0]
        assert len(od_rows[0].split(",")) == 7, od_rows[0]


def test_od_calibrated_without_od_raw_is_rejected() -> None:
    from data_logger import DataLogger
    with TmpRoot() as root:
        dl = DataLogger(root)
        dl.create_experiment(name="x", mode="turbidostat", vials=[0])
        dl.activate_experiment("x")
        with pytest.raises(ValueError):
            dl.log_sensor_cycle(
                timestamp_iso="2026-05-14T10:00:00+00:00",
                temperature_calibrated=[30.0] * N_VIALS,
                temperature_raw=[30.0] * N_VIALS,
                od_calibrated=[0.4] * N_VIALS,
            )
        dl.deactivate_experiment()
