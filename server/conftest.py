"""Shared pytest fixtures for server/.

Every app a test builds with ``app.create_app`` starts a real sensor-loop
daemon thread that ticks every 10 s for the life of the process. Tests
build the app inside a ``TemporaryDirectory`` and most never stop it, so
once the suite runs longer than one tick those threads wake up, find a
RUNNING experiment whose directory has been deleted, and log
FileNotFoundError tracebacks into whichever test happens to be running.
Stop them when the test that created them ends.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))


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
