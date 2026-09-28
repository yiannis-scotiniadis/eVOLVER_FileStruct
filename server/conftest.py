"""Shared pytest fixtures for server/.

Every app a test builds with ``app.create_app`` starts a real sensor-loop
daemon thread that ticks every 10 s for the life of the process. Tests
build the app inside a ``TemporaryDirectory`` and most never stop it, so
once the suite runs longer than one tick those threads wake up, find a
RUNNING experiment whose directory has been deleted, and log
FileNotFoundError tracebacks into whichever test happens to be running.
Stop them when the test that created them ends.

And no test may see the developer's runtime calibration state: apps built
without a calibration dir of their own used the repo's ``calibration/``,
including ``_sessions/``. Since Session AA an open Tier 3 session there
reserves the machine (409 on manual pumps, experiment create, ...), so a
wizard left open in the GUI failed unrelated tests. Each test gets a copy
of the calibration store without the runtime files.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

REPO_CAL = Path(__file__).resolve().parent.parent / "calibration"
# Per-machine runtime state that must never leak into a test.
_RUNTIME = {"_sessions", "reconciliation_log.json"}


@pytest.fixture(autouse=True)
def _isolated_calibration_dir(tmp_path_factory):
    import app as A

    cal = tmp_path_factory.mktemp("calibration")
    for entry in REPO_CAL.iterdir():
        if entry.name in _RUNTIME or entry.name.endswith(".tmp"):
            continue
        if entry.is_dir():
            shutil.copytree(entry, cal / entry.name)
        else:
            shutil.copy2(entry, cal / entry.name)
    saved = (A.CAL_DIR, A.TEMP_CAL_PATH, A.OD_CAL_PATH)
    A.CAL_DIR = cal
    A.TEMP_CAL_PATH = cal / "temp_calibration.txt"
    A.OD_CAL_PATH = cal / "OD_cal.txt"
    try:
        yield
    finally:
        A.CAL_DIR, A.TEMP_CAL_PATH, A.OD_CAL_PATH = saved


@pytest.fixture(autouse=True)
def _stop_sensor_threads_after_each_test():
    import app as A

    created: list = []
    original_init = A.AppState.__init__

    def recording_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        created.append(self)

    A.AppState.__init__ = recording_init
    try:
        yield
    finally:
        A.AppState.__init__ = original_init
        for state in created:
            state.sensor_thread_stop.set()
