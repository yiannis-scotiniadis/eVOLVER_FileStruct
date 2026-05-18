"""Control-mode strategy package (SPEC §9).

One module per mode (Phase 1: turbidostat; Phase 2: chemostat,
morbidostat, growth-rate). Each mode exposes a controller class with
:meth:`push_od` and :meth:`decide` so the engine can swap modes by
construction rather than by ``if``-cascade.
"""

from __future__ import annotations

from .chemostat import ChemostatController
from .morbidostat import EscalationEvent, MorbidostatController
from .turbidostat import PumpAction, TurbidostatController

__all__ = [
    "ChemostatController",
    "EscalationEvent",
    "MorbidostatController",
    "PumpAction",
    "TurbidostatController",
]
