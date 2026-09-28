"""Tests for the Tier 3 calibration work (ROADMAP Session AA / SPEC §19.5).

Service- and serial-level: the derived-view integrity check, structured
staleness, the machine-reserving Tier 3 sessions and their safety tick, the
two-point thermistor wizard and the OD dilution-series wizard. The HTTP
surface is in test_calibration_tier3_api.py; the pure 4-parameter fit is in
test_od_curve_fit.py.

Run from the project root:
    python -m pytest server/test_calibration_tier3.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from calibration_service import (  # noqa: E402
    CalibrationStore,
    N_VIALS,
    TempStabilityTracker,
    make_envelope,
    per_vial_sources,
)

REPO_CAL = Path(__file__).resolve().parent.parent / "calibration"


def _legacy_store(tmp: Path) -> CalibrationStore:
    """A store bootstrapped from copies of the committed 2016 files."""
    cal = tmp / "calibration"
    cal.mkdir(parents=True, exist_ok=True)
    for f in ("OD_cal.txt", "temp_calibration.txt"):
        (cal / f).write_bytes((REPO_CAL / f).read_bytes())
    store = CalibrationStore(cal, tmp / "experiments")
    store.bootstrap_from_legacy()
    return store


# ---------------------------------------------------------------------------
# Phase 0: derived-view integrity + structured staleness
# ---------------------------------------------------------------------------

def test_ensure_views_leaves_consistent_legacy_files_byte_identical():
    """The views are tracked in git; a boot must not reformat them."""
    with tempfile.TemporaryDirectory() as tmp:
        store = _legacy_store(Path(tmp))
        before = {
            f: (store.root / f).read_bytes()
            for f in ("OD_cal.txt", "temp_calibration.txt")
        }
        assert store.ensure_legacy_views() == []
        for f, data in before.items():
            assert (store.root / f).read_bytes() == data
        assert not (store.root / "OD_cal.meta.json").exists()


def test_ensure_views_repairs_truncated_and_out_of_date_views():
    with tempfile.TemporaryDirectory() as tmp:
        store = _legacy_store(Path(tmp))
        rows = np.genfromtxt(store.root / "OD_cal.txt", delimiter=",")
        # A half-written view (the pre-atomic failure mode).
        text = (store.root / "OD_cal.txt").read_text()
        (store.root / "OD_cal.txt").write_text(text[: len(text) // 2])
        assert store.ensure_legacy_views() == ["od"]
        assert np.allclose(
            np.genfromtxt(store.root / "OD_cal.txt", delimiter=","), rows,
        )
        # current.json names a version whose view was never written (a crash
        # between save_version's pointer update and its view regeneration).
        new = make_envelope(
            "temperature", operator="t", source="two-point-reference",
            conditions={"n": 1},
            data={"slope": [-0.1] * N_VIALS, "intercept": [80.0] * N_VIALS},
            version="2026-09-01T000000Z",
        )
        store._write_version_locked("temperature", new)
        current = store._read_current()
        current["temperature"] = new["version"]
        store._write_current(current)
        assert store.ensure_legacy_views() == ["temperature"]
        temp = np.genfromtxt(store.root / "temp_calibration.txt", delimiter=",")
        assert np.allclose(temp[0], -0.1) and np.allclose(temp[1], 80.0)


def test_ensure_views_repairs_a_sidecar_that_disagrees():
    with tempfile.TemporaryDirectory() as tmp:
        store = _legacy_store(Path(tmp))
        (store.root / "OD_cal.meta.json").write_text(
            json.dumps({"dark_subtracted": True})
        )
        assert store.ensure_legacy_views() == ["od"]
        meta = json.loads((store.root / "OD_cal.meta.json").read_text())
        assert meta["dark_subtracted"] is False


def test_per_vial_sources_falls_back_to_the_envelope_source():
    env = {"source": "legacy-import-2016", "fit": {}}
    assert per_vial_sources(env) == ["legacy-import-2016"] * N_VIALS
    env["fit"]["per_vial_source"] = ["two-point-reference"] * N_VIALS
    assert per_vial_sources(env)[0] == "two-point-reference"


def _tier3_temperature(version, *, flagged=None, verified=(), legacy_vials=()):
    sources = [
        "legacy-import-2016" if v in legacy_vials else "two-point-reference"
        for v in range(N_VIALS)
    ]
    return make_envelope(
        "temperature", operator="t", source="two-point-reference",
        conditions={"n": 1},
        data={"slope": [-0.11] * N_VIALS, "intercept": [82.0] * N_VIALS},
        fit={
            "per_vial_source": sources,
            "per_vial_version": [version] * N_VIALS,
            "flagged_vials": {str(v): "outside pack" for v in (flagged or [])},
        },
        qc={
            "passed": True,
            # A missing vial map warns on every write until P1 is done.
            "warnings": ["vial_map.json is missing (prerequisite P1)"],
            "failures": [],
            "overridden_by": None,
            "verification": {"passed_vials": list(verified)},
        },
        version=version,
    )


def test_staleness_vial_map_warning_alone_is_not_stale():
    """Before Session AA any qc warning marked temperature stale, so every
    new version would have read 'stale' forever (the vial-map warning)."""
    with tempfile.TemporaryDirectory() as tmp:
        store = _legacy_store(Path(tmp))
        store.save_version("temperature", _tier3_temperature("2026-09-01T000000Z"))
        stale = store.staleness()["temperature"]
        assert stale["stale"] is False, stale


def test_staleness_unverified_flagged_vials_and_partial_legacy():
    with tempfile.TemporaryDirectory() as tmp:
        store = _legacy_store(Path(tmp))
        store.save_version("temperature", _tier3_temperature(
            "2026-09-01T000000Z", flagged=[0, 3], verified=[3],
            legacy_vials=[7, 8],
        ))
        reasons = store.staleness()["temperature"]["reasons"]
        assert any("[0]" in r and "not independently verified" in r
                   for r in reasons), reasons
        assert not any("[0, 3]" in r for r in reasons)
        assert any("[7, 8]" in r and "2016" in r for r in reasons), reasons


def test_staleness_legacy_import_keeps_its_audit_message():
    with tempfile.TemporaryDirectory() as tmp:
        store = _legacy_store(Path(tmp))
        reasons = store.staleness()["temperature"]["reasons"]
        assert any("never verified" in r for r in reasons)
        assert any("vial 0 is the prime suspect" in r for r in reasons)


def test_tracker_reset_forgets_old_scale_history():
    tr = TempStabilityTracker(clock=lambda: 1000.0)
    tr.note([37.0] * N_VIALS)
    tr.reset()
    assert tr.settled([0], {0: 37.0})["per_vial"]["0"]["n_samples"] == 0
