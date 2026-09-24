"""server/run_config.py — vial groups within one experiment (ROADMAP Session Y).

Pure and I/O-free: plain data in, frozen dataclasses out. No imports from
``experiment_engine``, ``app`` or any control mode, so group normalisation and
validation are testable without an engine (same discipline as ``fluidics.py``
and ``growth_rate.py``).

**What a group is.** A named subset of an experiment's vials with its own
control mode and its own parameters. Groups partition the experiment; they do
not have lifecycles of their own. One experiment still has one start, one
stop, one directory, one per-run OD blank and one media configuration.
Anything that needs an independent lifecycle is a separate experiment.

**Config shape**::

    "parameters": {"volume_ml": 25, "efflux_extra_seconds": 2,
                   "stir_rate": 8, "temperature_c": 37},
    "groups": [
      {"name": "ctrl", "vials": [0, 1, 2, 3], "mode": "turbidostat",
       "parameters": {"od_lower_thresh": 0.2, "od_upper_thresh": 0.4}},
      {"name": "sel", "vials": [8, 9, 10, 11], "mode": "chemostat",
       "parameters": {"dilution_rate_per_hour": 0.3, "temperature_c": 30}}
    ]

The run-level ``parameters`` are defaults, and a group's ``parameters``
shallow-override them. A config with no ``groups`` key is one implicit group
holding every vial, so every pre-groups config and ``state.json`` loads
unchanged.

**Run-wide keys** (:data:`RUN_WIDE_KEYS`) describe the machine or the fluidics,
not a culture, and are refused inside a group:

* ``volume_ml`` is the efflux straw height. It is an operator-set scalar
  (CLAUDE.md, "Vial working volume"), not a per-arm parameter.
* ``efflux_extra_seconds`` is the overrun that engages the straw.
* ``vial_capacity_ml`` / ``overflow_margin_ml`` are vial geometry (SPEC
  §16.3.4).
* ``od_acquisition`` configures ONE OD read that serves all sixteen vials.
* ``pump_flow_rates`` is calibration, and belongs to the pumps.

**Mode-agnostic by design.** Nothing here knows what a control mode is. A
group's ``mode`` is a key into the engine's ``CONTROL_MODES`` registry, and
its ``parameters`` are opaque apart from the run-wide keys above and the
actuator targets (``temperature_c``, ``stir_rate``) resolved at the bottom.
The allowed modes are passed in by the caller, so registering a new mode --
including the planned ``"custom"`` mode of CUSTOM_CONTROLLER_DESIGN.md, whose
per-group script would ride in the group's ``parameters`` -- needs no change
to this module.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional

N_VIALS = 16

GROUP_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

# The name a config without `groups` is normalised under. Never written back
# to config.json: a legacy config stays byte-identical on disk.
IMPLICIT_GROUP_NAME = "all"

# Top-level `mode` of an experiment whose groups do not share one. Kept only
# for display and back-compat readers; no behavioural code may branch on it.
MIXED_MODE = "mixed"

RUN_WIDE_KEYS: frozenset[str] = frozenset({
    "volume_ml",
    "efflux_extra_seconds",
    "vial_capacity_ml",
    "overflow_margin_ml",
    "od_acquisition",
    "pump_flow_rates",
})

# Keys that name the same setting. When a group overrides one of them, the
# run-level default for the OTHER spelling is dropped from that group's merged
# parameters -- otherwise the controller builders, which prefer one spelling,
# would silently keep the run default and ignore the group's override.
_ALIASES: tuple[frozenset[str], ...] = (
    frozenset({"od_lower_thresh", "od_lower"}),
    frozenset({"od_upper_thresh", "od_upper"}),
    frozenset({"temperature_c", "temperature"}),
)

DEFAULT_STIR_RATE = 10
DEFAULT_TEMPERATURE_C = 37.0


@dataclass(frozen=True)
class Group:
    """One vial group. ``parameters`` are the MERGED parameters the group's
    controllers are built from; ``overrides`` are the group's own, as supplied,
    which is what gets written back to ``config.json``."""

    name: str
    mode: str
    vials: tuple[int, ...]
    parameters: Mapping[str, Any] = field(default_factory=dict)
    overrides: Mapping[str, Any] = field(default_factory=dict)
    implicit: bool = False

    def to_config(self) -> dict:
        """The ``config.json`` form: overrides only, never the merged view,
        so a later edit of a run-level default still reaches the group."""
        return {
            "name": self.name,
            "mode": self.mode,
            "vials": list(self.vials),
            "parameters": copy.deepcopy(dict(self.overrides)),
        }

    def summary(self) -> dict:
        return {"name": self.name, "mode": self.mode, "vials": list(self.vials)}


def merge_parameters(run_parameters: Mapping, overrides: Mapping) -> dict:
    """Run-level defaults shallow-overridden by one group's parameters."""
    merged = copy.deepcopy(dict(run_parameters or {}))
    for key in overrides:
        for alias_set in _ALIASES:
            if key in alias_set:
                for other in alias_set - {key}:
                    merged.pop(other, None)
    merged.update(copy.deepcopy(dict(overrides)))
    return merged


def _check_vial_list(vials: Any, where: str) -> tuple[int, ...]:
    if not isinstance(vials, (list, tuple)) or not vials:
        raise ValueError(f"{where}: 'vials' must be a non-empty list of ints in 0..15")
    out: list[int] = []
    for v in vials:
        if isinstance(v, bool) or not isinstance(v, int) or not (0 <= v < N_VIALS):
            raise ValueError(
                f"{where}: invalid vial {v!r}; expected an int in 0..{N_VIALS - 1}"
            )
        out.append(int(v))
    if len(set(out)) != len(out):
        raise ValueError(f"{where}: duplicate vial numbers")
    return tuple(sorted(out))


def normalize_groups(
    *,
    mode: Optional[str],
    vials: Optional[Iterable[int]],
    parameters: Optional[Mapping],
    groups: Optional[list],
    supported_modes: Optional[Iterable[str]] = None,
) -> list[Group]:
    """Normalise and validate an experiment's groups.

    ``groups`` absent or empty means the legacy single-mode form: one implicit
    group named :data:`IMPLICIT_GROUP_NAME` holding ``vials`` under ``mode``.

    With ``groups`` given, ``mode`` is ignored and ``vials`` (if non-empty)
    must equal the union of the groups' vials. Raises ``ValueError`` naming the
    offending group on any problem; the API maps that to HTTP 400.

    ``supported_modes`` is the caller's registry of runnable modes. ``None``
    is reader mode: modes are not checked at all, which is right for code
    that only resolves actuator targets from an already-validated config (the
    calibration layer), and wrong for create.
    """
    supported = None if supported_modes is None else frozenset(supported_modes)
    run_parameters = dict(parameters or {})
    vials_list = list(vials) if vials else []

    def _check_mode(value: Any, where: str) -> str:
        if supported is None:
            return "" if value is None else str(value)
        if value not in supported:
            raise ValueError(
                f"{where}: unsupported mode {value!r}; supported: {sorted(supported)}"
            )
        return str(value)

    if not groups:
        if supported is not None and mode not in supported:
            raise ValueError(
                f"unsupported mode {mode!r}; supported: {sorted(supported)}"
            )
        mode = _check_mode(mode, "experiment")
        if not vials_list:
            raise ValueError("'vials' must be a non-empty list (or supply 'groups')")
        return [Group(
            name=IMPLICIT_GROUP_NAME,
            mode=str(mode),
            vials=_check_vial_list(vials_list, "experiment"),
            parameters=copy.deepcopy(run_parameters),
            overrides={},
            implicit=True,
        )]

    if not isinstance(groups, list):
        raise ValueError("'groups' must be a list of group objects")

    out: list[Group] = []
    seen_names: set[str] = set()
    owner: dict[int, str] = {}
    for i, g in enumerate(groups):
        if not isinstance(g, Mapping):
            raise ValueError(f"'groups[{i}]' must be an object")
        name = g.get("name")
        if not isinstance(name, str) or not GROUP_NAME.match(name):
            raise ValueError(
                f"'groups[{i}].name' must match {GROUP_NAME.pattern!r}; got {name!r}"
            )
        if name in seen_names:
            raise ValueError(f"duplicate group name {name!r}")
        seen_names.add(name)
        where = f"group {name!r}"

        g_mode = _check_mode(g.get("mode"), where)
        g_vials = _check_vial_list(g.get("vials"), where)
        for v in g_vials:
            if v in owner:
                raise ValueError(
                    f"vial {v} is in both group {owner[v]!r} and group {name!r}; "
                    "a vial may belong to one group only"
                )
            owner[v] = name

        overrides = g.get("parameters") or {}
        if not isinstance(overrides, Mapping):
            raise ValueError(f"{where}: 'parameters' must be an object")
        run_wide = sorted(set(overrides) & RUN_WIDE_KEYS)
        if run_wide:
            raise ValueError(
                f"{where}: {run_wide} are run-wide settings (straw height, "
                "overrun, vial geometry, OD acquisition, pump calibration) and "
                "must be set on the experiment, not on a group"
            )

        out.append(Group(
            name=name,
            mode=str(g_mode),
            vials=g_vials,
            parameters=merge_parameters(run_parameters, overrides),
            overrides=copy.deepcopy(dict(overrides)),
        ))

    union = sorted(owner)
    vials_given = sorted(_check_vial_list(vials_list, "experiment")) if vials_list else []
    if vials_given and vials_given != union:
        raise ValueError(
            f"'vials' ({vials_given}) must equal the union of the groups' vials "
            f"({union}); omit 'vials' when supplying 'groups'"
        )
    return out


def groups_from_config(
    config: Mapping, supported_modes: Optional[Iterable[str]] = None,
) -> list[Group]:
    """Groups of a persisted ``config.json`` (or the config ``resume`` rebuilds
    from ``state.json``). A config without ``groups`` is one implicit group."""
    return normalize_groups(
        mode=config.get("mode"),
        vials=config.get("vials"),
        parameters=config.get("parameters") or {},
        groups=config.get("groups"),
        supported_modes=supported_modes,
    )


def run_mode(groups: Iterable[Group]) -> str:
    """The experiment's top-level ``mode``: the shared mode, or ``"mixed"``."""
    modes = {g.mode for g in groups}
    return modes.pop() if len(modes) == 1 else MIXED_MODE


def is_grouped(groups: Iterable[Group]) -> bool:
    """True when the groups came from an explicit ``groups`` list."""
    return any(not g.implicit for g in groups)


def group_by_vial(groups: Iterable[Group]) -> dict[int, Group]:
    return {v: g for g in groups for v in g.vials}


def _per_vial_value(value: Any, vial: int, default: Any) -> Any:
    """A scalar applies to every vial; a 16-list is indexed by absolute vial."""
    if value is None:
        return default
    if isinstance(value, (list, tuple)):
        if len(value) != N_VIALS:
            raise ValueError(f"per-vial list must have length {N_VIALS}, got {len(value)}")
        return value[vial]
    return value


def stir_by_vial(groups: Iterable[Group]) -> dict[int, int]:
    """Each grouped vial's stir PWM (``stir_rate``, scalar or 16-list)."""
    out: dict[int, int] = {}
    for g in groups:
        raw = g.parameters.get("stir_rate")
        for v in g.vials:
            out[v] = int(_per_vial_value(raw, v, DEFAULT_STIR_RATE))
    return out


def temperature_c_by_vial(groups: Iterable[Group]) -> dict[int, float]:
    """Each grouped vial's target temperature in °C.

    ``temperature_c`` (scalar or 16-list) wins; the legacy scalar
    ``temperature`` is the fallback, then 37 °C -- the same precedence
    ``ExperimentEngine._apply_initial_actuators_locked`` always used.
    """
    out: dict[int, float] = {}
    for g in groups:
        p = g.parameters
        legacy = p.get("temperature")
        fallback = (
            float(legacy)
            if isinstance(legacy, (int, float)) and not isinstance(legacy, bool)
            else DEFAULT_TEMPERATURE_C
        )
        raw = p.get("temperature_c")
        for v in g.vials:
            out[v] = float(_per_vial_value(raw, v, fallback))
    return out


def config_stir_by_vial(config: Mapping) -> dict[int, int]:
    """:func:`stir_by_vial` straight from a config dict (calibration layer)."""
    return stir_by_vial(groups_from_config(config))


def config_temperature_c_by_vial(config: Mapping) -> dict[int, float]:
    """:func:`temperature_c_by_vial` straight from a config dict."""
    return temperature_c_by_vial(groups_from_config(config))
