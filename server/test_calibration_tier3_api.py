"""HTTP-level tests for the Tier 3 calibration wizards (ROADMAP Session AA).

Builds a real app against MockSerialManager with a temp calibration dir, via
the helpers in test_calibration_api.py.

Run from the project root:
    python -m pytest server/test_calibration_tier3_api.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from calibration_service import N_VIALS, make_envelope  # noqa: E402
from test_calibration_api import _make_app, _state  # noqa: E402


def _stop(flask_app) -> None:
    """Stop the sensor thread so the temp dir can be removed cleanly."""
    _state(flask_app).sensor_thread_stop.set()


# ---------------------------------------------------------------------------
# Phase 0: a new calibration goes live without a restart
# ---------------------------------------------------------------------------

def test_temperature_install_reloads_every_live_cache():
    with tempfile.TemporaryDirectory() as tmp:
        flask_app = _make_app(Path(tmp))
        try:
            state = _state(flask_app)
            state.cal_service.note_temperatures([37.0] * N_VIALS)
            slope = [-0.1] * N_VIALS
            intercept = [79.0] * N_VIALS
            version = state.cal_service.store.save_version(
                "temperature",
                make_envelope(
                    "temperature", operator="t", source="two-point-reference",
                    conditions={"n": 1},
                    data={"slope": slope, "intercept": intercept},
                    version="2026-09-01T000000Z",
                ),
            )
            state.reload_calibration("temperature", version)

            want = np.vstack([slope, intercept])
            assert np.allclose(state.manager.temp_cal, want)
            assert np.allclose(state.temp_cal, want)
            assert np.allclose(state.engine._engine_kwargs["temp_cal"], want)
            # Engines built from now on get the new curve.
            assert np.allclose(state.engine._new_engine()._temp_cal, want)
            # The sliders' °C<->raw conversion is served the new curve.
            client = flask_app.test_client()
            cal = client.get("/api/actuators/state").get_json()["temp_calibration"]
            assert np.allclose(cal["slope"], slope)
            assert np.allclose(cal["intercept"], intercept)
            # The settle tracker's old-scale history is gone.
            assert state.cal_service.tracker._samples == []
            # The install went through the event funnel.
            kinds = [e.get("data", {}) or {} for e in state.event_log.recent()]
            assert any(d.get("subsystem") == "temperature" for d in kinds), kinds
        finally:
            _stop(flask_app)
