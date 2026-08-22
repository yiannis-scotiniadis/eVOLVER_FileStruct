"""Verification for the event log, rate limiting, and error classification
(SPEC §20.1-§20.3, ROADMAP Session M).

Run from the project root:
    python -m pytest server/test_event_log.py
"""

from __future__ import annotations

import csv
import logging
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import data_export as dx  # noqa: E402
import event_log as evlog  # noqa: E402
from data_logger import EVENT_HEADER, DataLogger  # noqa: E402


class _FakeClock:
    """Manually advanced monotonic clock, so window-expiry tests do not sleep."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = float(t)

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += float(seconds)


# ---------------------------------------------------------------------------
# Rate limiting (§20.3)
# ---------------------------------------------------------------------------

def test_repeated_identical_errors_are_bounded_and_counted():
    """The M checklist item: 100 identical serial errors must produce a bounded
    number of log lines carrying a count -- not 100 rows, and not 1 row that
    hides how often it happened."""
    ev = evlog.EventLog(every_nth=10)
    emitted = [
        ev.record(level="critical", category="serial", message="bus silent")
        for _ in range(100)
    ]
    surfaced = [e for e in emitted if e is not None]

    # First occurrence + every 10th => 11 surfaced, not 100.
    assert len(surfaced) == 11, len(surfaced)
    assert [e["count"] for e in surfaced] == [1, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100]

    # ...and the drawer shows ONE row, with the true total (SPEC §20.4).
    rows = ev.recent()
    assert len(rows) == 1
    assert rows[0]["count"] == 100


def test_distinct_messages_are_not_collapsed_together():
    ev = evlog.EventLog()
    for v in range(4):
        ev.record(level="warning", category="sensor", message=f"vial {v} dropped")
    assert len(ev.recent()) == 4


def test_dedup_key_collapses_messages_that_differ_only_in_detail():
    """A failure whose message embeds a changing exception string must still
    collapse -- otherwise the rate limiter never fires."""
    ev = evlog.EventLog(every_nth=10)
    for i in range(30):
        ev.record(
            level="critical", category="pump",
            message=f"pump failed: errno {i}",
            dedup_key="pump_command_failed",
        )
    rows = ev.recent()
    assert len(rows) == 1
    assert rows[0]["count"] == 30


def test_rate_limit_window_resets_after_quiet_period():
    """A fault that recurs tomorrow must alert again rather than being muted
    forever by a counter set during yesterday's incident."""
    clock = _FakeClock()
    ev = evlog.EventLog(every_nth=10, reset_after_seconds=300.0, clock=clock)

    assert ev.record(level="warning", category="serial", message="x") is not None
    assert ev.record(level="warning", category="serial", message="x") is None

    clock.advance(301.0)
    again = ev.record(level="warning", category="serial", message="x")
    assert again is not None
    assert again["count"] == 1
    assert len(ev.recent()) == 2  # a genuinely new incident, not the old row


# ---------------------------------------------------------------------------
# Ring buffer (§20.2 / §20.4)
# ---------------------------------------------------------------------------

def test_ring_is_newest_first_and_bounded():
    ev = evlog.EventLog(ring_size=5)
    for i in range(20):
        ev.record(level="info", category="pump", message=f"event {i}")
    rows = ev.recent(limit=100)
    assert len(rows) == 5
    assert rows[0]["message"] == "event 19"
    assert rows[-1]["message"] == "event 15"


def test_level_filter_is_a_minimum_severity():
    ev = evlog.EventLog()
    ev.record(level="info", category="pump", message="i")
    ev.record(level="warning", category="pump", message="w")
    ev.record(level="critical", category="pump", message="c")

    assert {e["message"] for e in ev.recent(level="warning")} == {"w", "c"}
    assert {e["message"] for e in ev.recent(level="critical")} == {"c"}
    assert len(ev.recent(level="info")) == 3


def test_category_and_vial_filters():
    ev = evlog.EventLog()
    ev.record(level="info", category="pump", message="p", vial=3)
    ev.record(level="info", category="sensor", message="s", vial=3)
    ev.record(level="info", category="pump", message="q", vial=7)

    assert len(ev.recent(category="pump")) == 2
    assert len(ev.recent(vial=3)) == 2
    assert len(ev.recent(category="pump", vial=7)) == 1


def test_acknowledge_marks_entry_and_records_its_own_event():
    ev = evlog.EventLog()
    entry = ev.record(level="critical", category="serial", message="bus down")
    assert ev.counts()["unacked_critical"] == 1

    acked = ev.acknowledge(entry["id"], by="yiannis")
    assert acked["acknowledged"] is True
    assert acked["acknowledged_by"] == "yiannis"
    assert ev.counts()["unacked_critical"] == 0

    # The acknowledgement is itself an event (SPEC §20.4).
    messages = [e["message"] for e in ev.recent()]
    assert any(m.startswith("Acknowledged:") for m in messages)
    assert ev.acknowledge(99999) is None


def test_unacked_only_filter():
    ev = evlog.EventLog()
    a = ev.record(level="critical", category="serial", message="a")
    ev.record(level="critical", category="serial", message="b")
    ev.acknowledge(a["id"])
    remaining = [e["message"] for e in ev.recent(level="critical", unacked_only=True)]
    assert remaining == ["b"]


# ---------------------------------------------------------------------------
# events.csv fan-out (§20.2)
# ---------------------------------------------------------------------------

def test_events_csv_written_only_while_an_experiment_is_active():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        dl = DataLogger(root)
        ev = evlog.EventLog(dl)

        # Idle: the ring still records, the CSV does not exist.
        ev.record(level="warning", category="serial", message="idle fault")
        assert len(ev.recent()) == 1

        dl.create_experiment(name="run1", mode="turbidostat", vials=[0, 1])
        events_path = root / "run1" / "events.csv"
        # Pre-created with a header, so an export always finds it.
        assert events_path.is_file()
        assert events_path.read_text(encoding="utf-8").strip() == ",".join(EVENT_HEADER)

        dl.activate_experiment("run1")
        ev.record(level="warning", category="pump", message="suppressed", vial=1,
                  data={"reason": "media_empty"})

        rows = list(csv.DictReader(events_path.open(encoding="utf-8", newline="")))
        assert len(rows) == 1
        assert rows[0]["level"] == "warning"
        assert rows[0]["category"] == "pump"
        assert rows[0]["vial"] == "1"
        assert "media_empty" in rows[0]["data_json"]
        assert float(rows[0]["elapsed_hours"]) >= 0.0


def test_machine_wide_events_have_a_blank_vial_column():
    """log_event must NOT filter on experiment vial membership the way
    log_escalation_event does -- system events carry vial=None."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        dl = DataLogger(root)
        dl.create_experiment(name="run1", mode="turbidostat", vials=[0])
        dl.activate_experiment("run1")

        assert dl.log_event(_ts(), "critical", "serial", "bus silent") is True
        # A vial outside the experiment is still part of the run's story.
        assert dl.log_event(_ts(), "info", "pump", "manual pump", vial=9) is True

        rows = list(csv.DictReader(
            (root / "run1" / "events.csv").open(encoding="utf-8", newline="")))
        assert [r["vial"] for r in rows] == ["", "9"]


def test_newlines_in_messages_cannot_split_a_csv_row():
    """events.csv is read back line-by-line, so an embedded newline (easy to
    get from an exception string) would corrupt every downstream reader."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        dl = DataLogger(root)
        dl.create_experiment(name="run1", mode="turbidostat", vials=[0])
        dl.activate_experiment("run1")
        dl.log_event(_ts(), "critical", "serial",
                     "Traceback:" + chr(10) + "  line 1" + chr(10) + "  line 2")

        text = (root / "run1" / "events.csv").read_text(encoding="utf-8")
        assert len(text.strip().splitlines()) == 2  # header + exactly one row


def test_event_log_survives_a_broken_data_logger():
    """Observability must never take down the caller it is observing."""
    class Exploding:
        def log_event(self, **kw):
            raise RuntimeError("disk on fire")

    ev = evlog.EventLog(Exploding())
    entry = ev.record(level="critical", category="serial", message="x")
    assert entry is not None          # still surfaced to the operator
    assert len(ev.recent()) == 1      # still in the ring


def _ts() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Export bundle (§20.2)
# ---------------------------------------------------------------------------

def test_export_bundle_contains_events_csv():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        dl = DataLogger(root)
        dl.create_experiment(name="run1", mode="turbidostat", vials=[0])
        dl.activate_experiment("run1")
        dl.log_sensor_cycle(
            timestamp_iso=_ts(),
            temperature_calibrated=[37.0] * 16, temperature_raw=[400] * 16,
            od_calibrated=[0.2] * 16, od_raw=[50000] * 16,
        )
        dl.log_event(_ts(), "warning", "pump", "suppressed: media_empty", vial=0)

        fname, blob = dx.build_bundle(
            root / "run1", name="run1", vials=[0], parameters=["od", "temp"],
        )
        assert fname.endswith(".zip")
        with zipfile.ZipFile(_bytes_io(blob)) as zf:
            names = zf.namelist()
            assert "events.csv" in names
            assert "suppressed: media_empty" in zf.read("events.csv").decode("utf-8")


def _bytes_io(b: bytes):
    import io
    return io.BytesIO(b)


def test_export_bundle_without_events_csv_still_builds():
    """Experiments created before Session M have no events.csv; the export must
    not break on them."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        exp = root / "legacy"
        exp.mkdir()
        (exp / "vial00_OD.csv").write_text(
            "timestamp,elapsed_hours,raw_adc,calibrated_od,n_valid,flag,dark" + chr(10),
            encoding="utf-8",
        )
        fname, blob = dx.build_bundle(
            exp, name="legacy", vials=[0], parameters=["od", "temp"],
        )
        with zipfile.ZipFile(_bytes_io(blob)) as zf:
            assert "events.csv" not in zf.namelist()


# ---------------------------------------------------------------------------
# Bus + vial classification (§20.3)
# ---------------------------------------------------------------------------

def test_bus_health_transient_then_persistent_then_recovered():
    bus = evlog.BusHealth(failure_threshold=3)

    # A single lossy frame is TRANSIENT -- counted, never alerted.
    assert bus.record("od", False) == evlog.ErrorClass.TRANSIENT
    assert bus.state("od") == "degraded"
    # Still degraded: no repeat classification, so no alert storm.
    assert bus.record("od", False) is None
    # Crossing the threshold is PERSISTENT, reported exactly once.
    assert bus.record("od", False) == evlog.ErrorClass.PERSISTENT
    assert bus.state("od") == "down"
    assert bus.record("od", False) is None
    assert bus.record("od", False) is None

    assert bus.record("od", True) == evlog.ErrorClass.RECOVERED
    assert bus.state("od") == "ok"
    assert bus.record("od", True) is None


def test_bus_health_subsystems_are_independent():
    bus = evlog.BusHealth(failure_threshold=2)
    bus.record("od", False)
    bus.record("od", False)
    assert bus.state("od") == "down"
    assert bus.state("temperature") == "ok"
    snap = bus.snapshot()
    assert snap["od"]["consecutive_failures"] == 2
    assert snap["threshold"] == 2


def test_read_ok_helpers_distinguish_out_of_range_from_a_dead_bus():
    nan = float("nan")
    assert evlog.temperature_read_ok([nan] * 15 + [37.0]) is True
    assert evlog.temperature_read_ok([nan] * 16) is False

    # out_of_range means the culture outgrew the calibration -- the bus is fine.
    assert evlog.od_read_ok(flags=["out_of_range"] * 16) is True
    assert evlog.od_read_ok(flags=["dropped"] * 16) is False
    assert evlog.od_read_ok(flags=["dropped"] * 15 + ["ok"]) is True
    # n_valid is authoritative when present.
    assert evlog.od_read_ok(flags=["dropped"] * 16, n_valid=[0] * 15 + [3]) is True
    assert evlog.od_read_ok(n_valid=[0] * 16, calibrated=[nan] * 16) is False


def test_vial_health_streaks_and_recovery():
    vh = evlog.VialHealth(4, degraded_threshold=3)
    nan = float("nan")

    for _ in range(3):
        vh.record_cycle(temperature=[37.0, nan, 37.0, 37.0],
                        od_flags=["ok"] * 4, od_n_valid=[3] * 4)
    snap = vh.snapshot()
    assert snap[1]["state"] == "degraded"
    assert snap[1]["dropped_streak"] == 3
    assert snap[0]["state"] == "ok"

    # One good cycle clears it -- these are *consecutive* failures.
    vh.record_cycle(temperature=[37.0] * 4, od_flags=["ok"] * 4, od_n_valid=[3] * 4)
    assert vh.snapshot()[1]["state"] == "ok"


def test_vial_health_tracks_out_of_range_separately_from_dropped():
    vh = evlog.VialHealth(2, degraded_threshold=2)
    for _ in range(2):
        vh.record_cycle(temperature=[37.0, 37.0],
                        od_flags=["out_of_range", "ok"], od_n_valid=[3, 3])
    snap = vh.snapshot()
    assert snap[0]["state"] == "out_of_range"
    assert snap[0]["dropped_streak"] == 0  # not a fault of the sleeve
    assert snap[1]["state"] == "ok"


# ---------------------------------------------------------------------------
# Rotating, disk-aware file logs (§20.1)
# ---------------------------------------------------------------------------

def test_file_logging_rotates_within_its_cap():
    with tempfile.TemporaryDirectory() as tmp:
        logs = Path(tmp) / "logs"
        handlers = evlog.setup_file_logging(
            logs, max_bytes=2048, backup_count=2,
            error_max_bytes=2048, error_backup_count=2,
        )
        try:
            log = logging.getLogger("evolver.test.rotate")
            for i in range(400):
                log.info("padding line %04d %s", i, "x" * 80)
            handlers["app"].flush()

            produced = sorted(p.name for p in logs.glob("evolver.log*"))
            # 1 active + at most backup_count rotated.
            assert len(produced) <= 3, produced
            assert "evolver.log" in produced
            assert all(p.stat().st_size <= 4096 for p in logs.glob("evolver.log*"))
        finally:
            _detach(handlers)


def test_errors_log_only_receives_warning_and_above():
    with tempfile.TemporaryDirectory() as tmp:
        logs = Path(tmp) / "logs"
        handlers = evlog.setup_file_logging(logs)
        try:
            log = logging.getLogger("evolver.test.levels")
            log.info("this is routine")
            log.warning("this is not")
            for h in handlers.values():
                h.flush()
            errors = (logs / "errors.log").read_text(encoding="utf-8")
            assert "this is not" in errors
            assert "this is routine" not in errors
            assert "this is routine" in (logs / "evolver.log").read_text(encoding="utf-8")
        finally:
            _detach(handlers)


def test_log_writes_stop_gracefully_below_the_disk_floor():
    """The M checklist item. A stuck loop must not be able to fill the card the
    experiment is also writing to."""
    with tempfile.TemporaryDirectory() as tmp:
        logs = Path(tmp) / "logs"
        # A floor larger than any real disk => immediately suspended.
        handlers = evlog.setup_file_logging(logs, floor_bytes=1 << 62)
        try:
            log = logging.getLogger("evolver.test.floor")
            for _ in range(50):
                log.error("should not reach disk")
            handlers["app"].flush()

            assert handlers["app"].suspended is True
            assert handlers["app"].skipped_records >= 50
            assert (logs / "evolver.log").stat().st_size == 0
            status = evlog.file_log_status()
            assert status["enabled"] is True and status["suspended"] is True

            # Lower the floor: writing resumes without a restart.
            for h in handlers.values():
                h.floor_bytes = 0
                h.refresh_suspended(force=True)
            log.error("this one should land")
            handlers["app"].flush()
            assert "this one should land" in (
                logs / "evolver.log").read_text(encoding="utf-8")
        finally:
            _detach(handlers)


def test_setup_file_logging_is_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        logs = Path(tmp) / "logs"
        root = logging.getLogger()
        before = len(root.handlers)
        h1 = evlog.setup_file_logging(logs)
        h2 = evlog.setup_file_logging(logs)
        try:
            # Second call replaces rather than stacks.
            assert len(root.handlers) == before + 2
        finally:
            _detach(h2)
            assert len(root.handlers) == before


def _detach(handlers: dict) -> None:
    root = logging.getLogger()
    for h in handlers.values():
        root.removeHandler(h)
        h.close()
    evlog._INSTALLED_HANDLERS.clear()
