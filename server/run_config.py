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
import math
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


VIAL_TARGET_KEYS: frozenset[str] = frozenset({"temperature_c", "stir_rate"})


def normalise_vial_targets(raw: Any) -> dict[int, dict[str, Any]]:
    """The ``vial_targets`` config key as ``{vial: {key: value}}`` with int
    vials (JSON gives string keys). A target an operator kept for the rest of
    the run through a supervised override (SPEC §21) -- the top layer of
    actuator-target resolution, above the vial's group. Unknown keys and
    malformed entries are dropped rather than raised: this is read on every
    resume and a bad entry must not strand a run."""
    out: dict[int, dict[str, Any]] = {}
    if not isinstance(raw, Mapping):
        return out
    for k, entry in raw.items():
        try:
            vial = int(k)
        except (TypeError, ValueError):
            continue
        if not (0 <= vial < N_VIALS) or not isinstance(entry, Mapping):
            continue
        kept = {key: entry[key] for key in VIAL_TARGET_KEYS if entry.get(key) is not None}
        if kept:
            out[vial] = kept
    return out


def stir_by_vial(
    groups: Iterable[Group],
    vial_targets: Optional[Mapping[int, Mapping[str, Any]]] = None,
) -> dict[int, int]:
    """Each grouped vial's stir PWM (``stir_rate``, scalar or 16-list), with
    any kept per-vial override target on top."""
    out: dict[int, int] = {}
    for g in groups:
        raw = g.parameters.get("stir_rate")
        for v in g.vials:
            out[v] = int(_per_vial_value(raw, v, DEFAULT_STIR_RATE))
    for v, entry in (vial_targets or {}).items():
        if v in out and entry.get("stir_rate") is not None:
            out[v] = int(entry["stir_rate"])
    return out


def temperature_c_by_vial(
    groups: Iterable[Group],
    vial_targets: Optional[Mapping[int, Mapping[str, Any]]] = None,
) -> dict[int, float]:
    """Each grouped vial's target temperature in °C.

    A kept per-vial override target (``vial_targets``) wins; then
    ``temperature_c`` (scalar or 16-list); the legacy scalar ``temperature``
    is the fallback, then 37 °C -- the same precedence
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
    for v, entry in (vial_targets or {}).items():
        if v in out and entry.get("temperature_c") is not None:
            out[v] = float(entry["temperature_c"])
    return out


def config_stir_by_vial(config: Mapping) -> dict[int, int]:
    """:func:`stir_by_vial` straight from a config dict (calibration layer)."""
    return stir_by_vial(
        groups_from_config(config),
        normalise_vial_targets(config.get("vial_targets")),
    )


def config_temperature_c_by_vial(config: Mapping) -> dict[int, float]:
    """:func:`temperature_c_by_vial` straight from a config dict."""
    return temperature_c_by_vial(
        groups_from_config(config),
        normalise_vial_targets(config.get("vial_targets")),
    )


# ---------------------------------------------------------------------------
# Stir pause before OD reads (SPEC §9.1)
# ---------------------------------------------------------------------------
#
# A vortex, foam or bubble train that reaches the light path spoils an OD
# read. Whether it does depends on fill height, stir rate and the culture, all
# of which can differ between groups -- so the pause is a GROUP parameter like
# ``stir_rate``, not a run-wide one. It is NOT in RUN_WIDE_KEYS: the OD read
# still serves all sixteen vials on every tick, and what a vial pauses for is
# which of those reads it ACCEPTS (``server/od_stir_pause.py``). Two groups --
# or two parallel experiments -- that disagree therefore never need merging.
#
# Shape (all keys optional; a missing block, or enabled=false, means the vial
# never pauses, which is how every config from before this existed loads)::
#
#     "od_stir_pause": {"enabled": true, "settle_seconds": 20,
#                       "period_seconds": 120, "pause_pwm": 0,
#                       "min_mix_seconds": 30}
#
# One object per group, deliberately with no per-vial lists inside it: a vial
# that needs different timing goes in its own group.

STIR_PAUSE_KEY = "od_stir_pause"

STIR_PAUSE_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "settle_seconds": 20.0,
    "period_seconds": 120.0,
    "pause_pwm": 0,
    "min_mix_seconds": 30.0,
}

STIR_PAUSE_SETTLE_MIN_SECONDS = 5.0
STIR_PAUSE_SETTLE_MAX_SECONDS = 120.0
STIR_PAUSE_MIN_MIX_MAX_SECONDS = 600.0
STIR_PAUSE_PERIOD_MAX_SECONDS = 3600.0
STIR_MAX = 15

#: Above this fraction of each period with the stirrer paused, a group is
#: refused. Every second of pause is a second without mixing or oxygen
#: transfer, and for a sedimenting or strictly aerobic culture the pause
#: changes the physiology the run is measuring.
STIR_PAUSE_MAX_PAUSED_FRACTION = 0.5
#: Above this, the group is accepted with a warning.
STIR_PAUSE_WARN_PAUSED_FRACTION = 1.0 / 3.0

#: The sensor loop's base tick, which is also how long one OD acquisition
#: holds the bus (3 dark + 5 lit reads, ~9.4 s measured -- CLAUDE.md fact 13).
#: Every pause is quantised up to it. Passed in rather than imported so this
#: module stays free of app/engine imports.
DEFAULT_TICK_SECONDS = 10.0


@dataclass(frozen=True)
class StirPause:
    """A validated, enabled stir pause for one group."""

    settle_seconds: float
    period_seconds: float
    pause_pwm: int
    min_mix_seconds: float

    def to_dict(self) -> dict:
        return {
            "enabled": True,
            "settle_seconds": self.settle_seconds,
            "period_seconds": self.period_seconds,
            "pause_pwm": self.pause_pwm,
            "min_mix_seconds": self.min_mix_seconds,
        }


def stir_pause_paused_seconds(
    pause: StirPause, tick_seconds: float = DEFAULT_TICK_SECONDS,
) -> float:
    """Seconds per period the stirrer is actually held at ``pause_pwm``.

    The settle is honoured as a MINIMUM rounded up to whole ticks (the read
    that is accepted is the first one to START at least ``settle_seconds``
    after the pause), and the stirrer stays paused through that read, which
    occupies one more tick."""
    tick = max(float(tick_seconds), 1e-9)
    return math.ceil(pause.settle_seconds / tick - 1e-9) * tick + tick


def stir_pause_paused_fraction(
    pause: StirPause, tick_seconds: float = DEFAULT_TICK_SECONDS,
) -> float:
    return stir_pause_paused_seconds(pause, tick_seconds) / pause.period_seconds


def _stir_pause_number(block: Mapping, key: str, where: str) -> float:
    value = block.get(key, STIR_PAUSE_DEFAULTS[key])
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{where}: '{STIR_PAUSE_KEY}.{key}' must be a number, got {value!r}")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{where}: '{STIR_PAUSE_KEY}.{key}' must be finite")
    return value


def normalise_stir_pause(
    raw: Any,
    *,
    where: str = "experiment",
    stir_rates: Iterable[int] = (),
    tick_seconds: float = DEFAULT_TICK_SECONDS,
) -> Optional[StirPause]:
    """Validate one group's ``od_stir_pause`` block. ``None`` = no pause.

    Raises ``ValueError`` naming ``where`` (the API maps it to HTTP 400) for:
    an unknown key (a typo must not silently mean "never pause"), a
    non-boolean ``enabled``, a settle outside 5-120 s, a ``pause_pwm`` that is
    not below every one of the group's ``stir_rates`` (pausing must slow the
    stirrer, never speed it up), a period too short to fit the settle, the
    read and ``min_mix_seconds`` of stirring, and a paused fraction above
    :data:`STIR_PAUSE_MAX_PAUSED_FRACTION`."""
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError(f"{where}: '{STIR_PAUSE_KEY}' must be an object")
    unknown = sorted(set(raw) - set(STIR_PAUSE_DEFAULTS))
    if unknown:
        raise ValueError(
            f"{where}: unknown '{STIR_PAUSE_KEY}' key(s) {unknown}; "
            f"allowed: {sorted(STIR_PAUSE_DEFAULTS)}"
        )
    enabled = raw.get("enabled", STIR_PAUSE_DEFAULTS["enabled"])
    if not isinstance(enabled, bool):
        raise ValueError(f"{where}: '{STIR_PAUSE_KEY}.enabled' must be true or false")
    if not enabled:
        return None

    settle = _stir_pause_number(raw, "settle_seconds", where)
    period = _stir_pause_number(raw, "period_seconds", where)
    min_mix = _stir_pause_number(raw, "min_mix_seconds", where)
    pwm_raw = raw.get("pause_pwm", STIR_PAUSE_DEFAULTS["pause_pwm"])
    if isinstance(pwm_raw, bool) or not isinstance(pwm_raw, (int, float)) \
            or float(pwm_raw) != int(pwm_raw):
        raise ValueError(f"{where}: '{STIR_PAUSE_KEY}.pause_pwm' must be an integer")
    pause_pwm = int(pwm_raw)

    if not (STIR_PAUSE_SETTLE_MIN_SECONDS <= settle <= STIR_PAUSE_SETTLE_MAX_SECONDS):
        raise ValueError(
            f"{where}: '{STIR_PAUSE_KEY}.settle_seconds' must be "
            f"{STIR_PAUSE_SETTLE_MIN_SECONDS:g}-{STIR_PAUSE_SETTLE_MAX_SECONDS:g} s, "
            f"got {settle:g}"
        )
    if not (0.0 <= min_mix <= STIR_PAUSE_MIN_MIX_MAX_SECONDS):
        raise ValueError(
            f"{where}: '{STIR_PAUSE_KEY}.min_mix_seconds' must be "
            f"0-{STIR_PAUSE_MIN_MIX_MAX_SECONDS:g} s, got {min_mix:g}"
        )
    if not (0 <= pause_pwm <= STIR_MAX):
        raise ValueError(
            f"{where}: '{STIR_PAUSE_KEY}.pause_pwm' must be 0-{STIR_MAX}, got {pause_pwm}"
        )
    rates = sorted({int(r) for r in stir_rates})
    if rates and pause_pwm >= rates[0]:
        raise ValueError(
            f"{where}: '{STIR_PAUSE_KEY}.pause_pwm' ({pause_pwm}) must be below "
            f"the group's stir rate ({rates[0]}) -- a pause that does not slow "
            "the stirrer settles nothing"
        )

    pause = StirPause(
        settle_seconds=settle,
        period_seconds=period,
        pause_pwm=pause_pwm,
        min_mix_seconds=min_mix,
    )
    paused = stir_pause_paused_seconds(pause, tick_seconds)
    shortest = paused + min_mix
    if period < shortest:
        raise ValueError(
            f"{where}: '{STIR_PAUSE_KEY}.period_seconds' ({period:g}) is too "
            f"short: each cycle holds the stirrer paused ~{paused:g} s (the "
            f"settle rounded up to whole {tick_seconds:g} s ticks, plus the "
            f"read itself) and then stirs at least min_mix_seconds "
            f"({min_mix:g}), so it needs >= {shortest:g} s"
        )
    if period > STIR_PAUSE_PERIOD_MAX_SECONDS:
        raise ValueError(
            f"{where}: '{STIR_PAUSE_KEY}.period_seconds' must be <= "
            f"{STIR_PAUSE_PERIOD_MAX_SECONDS:g} s, got {period:g}"
        )
    fraction = paused / period
    if fraction > STIR_PAUSE_MAX_PAUSED_FRACTION + 1e-9:
        raise ValueError(
            f"{where}: the stirrer would be paused {100 * fraction:.0f} % of "
            f"the time (~{paused:g} s of every {period:g} s); the limit is "
            f"{100 * STIR_PAUSE_MAX_PAUSED_FRACTION:.0f} %. Lengthen "
            "period_seconds or shorten settle_seconds"
        )
    return pause


def stir_pause_by_vial(
    groups: Iterable[Group],
    tick_seconds: float = DEFAULT_TICK_SECONDS,
) -> dict[int, StirPause]:
    """``{vial: StirPause}`` for every vial whose group pauses before OD reads.
    Vials whose group does not pause are absent.

    Reader mode: the config was validated at create, so the stir-rate check
    is not repeated here (a kept override target may since have moved a
    vial's stir; the scheduler composes ``min(stir, pause_pwm)`` for that)."""
    out: dict[int, StirPause] = {}
    for g in groups:
        pause = normalise_stir_pause(
            g.parameters.get(STIR_PAUSE_KEY),
            where=f"group {g.name!r}", tick_seconds=tick_seconds,
        )
        if pause is None:
            continue
        for v in g.vials:
            out[v] = pause
    return out


def group_stir_rates(group: Group) -> list[int]:
    """The stir PWM of each of ``group``'s vials (scalar or 16-list)."""
    raw = group.parameters.get("stir_rate")
    return [int(_per_vial_value(raw, v, DEFAULT_STIR_RATE)) for v in group.vials]


def config_stir_pause_by_vial(config: Mapping) -> dict[int, StirPause]:
    """:func:`stir_pause_by_vial` straight from a config dict (calibration
    layer: the blank needs to know which vials will be read paused)."""
    return stir_pause_by_vial(groups_from_config(config))
