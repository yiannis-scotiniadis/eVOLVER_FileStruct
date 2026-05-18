"""Verification script for the MorbidostatController.

Run from the project root::

    python server/test_morbidostat.py
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from control_modes.morbidostat import (  # noqa: E402
    EscalationEvent,
    MorbidostatController,
    estimate_growth_rate,
)
from control_modes.turbidostat import PumpAction, TurbidostatController  # noqa: E402


def _make(**overrides) -> MorbidostatController:
    kw = dict(
        vial=0,
        target_od=0.4,
        od_lower=0.2,
        pump_wait_seconds=900.0,
        flow_rate_ml_s=1.0,
        volume_ml=25.0,
        efflux_extra_seconds=5.0,
        history_window=5,
        pump_duration_cap_seconds=20.0,
        # Disable the >=8-sample warmup gate for these unit tests; the
        # gate itself is exercised by test_turbidostat.py.
        min_samples_before_action=1,
        initial_drug_conc=1.0,
        drug_step=2.0,
        adaptation_threshold_per_hour=0.4,
        growth_window_seconds=1800.0,
        growth_min_samples=6,
        escalation_cooldown_seconds=3600.0,
        escalation_reminder_interval_seconds=1800.0,
    )
    kw.update(overrides)
    return MorbidostatController(**kw)


def _feed_exponential(
    c: MorbidostatController, *, mu_per_hour: float, span_seconds: float,
    samples: int, start_od: float = 0.1, t0: float = 0.0,
) -> float:
    """Feed an exponential-growth OD curve; return the final timestamp."""
    mu_per_sec = mu_per_hour / 3600.0
    dt = span_seconds / max(samples - 1, 1)
    for i in range(samples):
        t = t0 + i * dt
        od = start_od * math.exp(mu_per_sec * (t - t0))
        c.push_od(now=t, od=od)
    return t0 + (samples - 1) * dt


def test_dilution_delegates_to_turbidostat() -> None:
    """With slow growth (no escalation), pump decisions match turbidostat."""
    morb = _make(adaptation_threshold_per_hour=10.0)  # unreachable
    turb = TurbidostatController(
        vial=0, od_lower=0.2, od_upper=0.4,
        pump_wait_seconds=900.0, flow_rate_ml_s=1.0, volume_ml=25.0,
        efflux_extra_seconds=5.0, history_window=5, pump_duration_cap_seconds=20.0,
        min_samples_before_action=1,  # disable warmup gate (matches morb in _make)
    )
    for _ in range(5):
        morb.push_od(now=0.0, od=0.5)
        turb.push_od(0.5)
    m_action = morb.decide(now=10000.0)
    t_action = turb.decide(now=10000.0)
    assert isinstance(m_action, PumpAction) and isinstance(t_action, PumpAction)
    assert abs(m_action.pump_time - t_action.pump_time) < 1e-12
    assert m_action.efflux_extra_seconds == t_action.efflux_extra_seconds
    print("PASS  dilution decision delegates to inner turbidostat")


def test_growth_rate_estimation_log_linear() -> None:
    """Feed a known exponential trajectory; recover mu within 5%."""
    true_mu = 0.5  # /hr
    span = 1800.0
    samples = 30
    history = []
    mu_per_sec = true_mu / 3600.0
    for i in range(samples):
        t = i * span / (samples - 1)
        od = 0.1 * math.exp(mu_per_sec * t)
        history.append((t, od))
    est = estimate_growth_rate(history, now=span, window_seconds=span, min_samples=6)
    assert est is not None
    assert abs(est - true_mu) / true_mu < 0.05, f"estimated {est}, true {true_mu}"
    print(f"PASS  growth-rate fit recovers exponential rate (est={est:.4f}, true={true_mu})")


def test_growth_rate_returns_none_with_sparse_history() -> None:
    history = [(0.0, 0.1), (10.0, 0.11)]  # 2 samples < min 6
    assert estimate_growth_rate(history, now=10.0, window_seconds=1800.0, min_samples=6) is None
    print("PASS  growth-rate fit returns None when < min_samples")


def test_growth_rate_returns_none_when_flat() -> None:
    """All samples at same x -> degenerate fit -> None."""
    history = [(100.0, 0.1) for _ in range(10)]
    result = estimate_growth_rate(history, now=200.0, window_seconds=1800.0, min_samples=6)
    assert result is None
    print("PASS  growth-rate fit returns None on degenerate (flat-time) history")


def test_escalation_triggers_above_threshold() -> None:
    c = _make(adaptation_threshold_per_hour=0.3)
    _feed_exponential(c, mu_per_hour=0.8, span_seconds=1800.0, samples=20)
    # Trigger evaluation via decide()
    c.decide(now=1800.0)
    event = c.pending_escalation()
    assert event is not None, "expected escalation event"
    assert isinstance(event, EscalationEvent)
    assert event.vial == 0
    assert event.old_drug_conc == 1.0
    assert event.new_drug_conc == 2.0
    assert event.growth_rate > 0.3
    assert c.awaiting_escalation_confirm is True
    print(f"PASS  escalation triggers above threshold (mu={event.growth_rate:.3f}/hr)")


def test_escalation_consumed_on_read() -> None:
    c = _make(adaptation_threshold_per_hour=0.3)
    _feed_exponential(c, mu_per_hour=0.8, span_seconds=1800.0, samples=20)
    c.decide(now=1800.0)
    first = c.pending_escalation()
    second = c.pending_escalation()
    assert first is not None
    assert second is None, "pending_escalation should be consumed on first read"
    print("PASS  pending_escalation consumed on read")


def test_no_double_trigger_while_awaiting_confirm() -> None:
    """Even with continued high growth, no new event until confirmed."""
    c = _make(adaptation_threshold_per_hour=0.3)
    _feed_exponential(c, mu_per_hour=0.8, span_seconds=1800.0, samples=20)
    c.decide(now=1800.0)
    assert c.pending_escalation() is not None
    # Keep feeding high growth; should not re-trigger
    _feed_exponential(c, mu_per_hour=0.8, span_seconds=1800.0,
                      samples=20, start_od=0.5, t0=1800.0)
    c.decide(now=3600.0)
    assert c.pending_escalation() is None, "should not re-trigger while awaiting"
    print("PASS  no double-trigger while awaiting confirmation")


def test_escalation_cooldown_respected() -> None:
    """After confirm, no new event until cooldown elapsed."""
    c = _make(adaptation_threshold_per_hour=0.3, escalation_cooldown_seconds=600.0)
    _feed_exponential(c, mu_per_hour=0.8, span_seconds=1800.0, samples=20)
    c.decide(now=1800.0)
    assert c.pending_escalation() is not None
    c.confirm_escalation(new_conc=2.0, timestamp=1800.0)
    # Within cooldown, feed more high growth -> no new event
    _feed_exponential(c, mu_per_hour=0.8, span_seconds=300.0,
                      samples=10, start_od=0.5, t0=1800.0)
    c.decide(now=2100.0)
    assert c.pending_escalation() is None
    # After cooldown elapses
    _feed_exponential(c, mu_per_hour=0.8, span_seconds=300.0,
                      samples=10, start_od=0.5, t0=2400.0)
    c.decide(now=2700.0)
    event = c.pending_escalation()
    assert event is not None, "should re-trigger after cooldown"
    assert event.old_drug_conc == 2.0
    assert event.new_drug_conc == 4.0
    print("PASS  escalation cooldown gates subsequent triggers")


def test_confirm_updates_drug_conc() -> None:
    c = _make(adaptation_threshold_per_hour=0.3)
    _feed_exponential(c, mu_per_hour=0.8, span_seconds=1800.0, samples=20)
    c.decide(now=1800.0)
    c.pending_escalation()
    c.confirm_escalation(new_conc=2.0, timestamp=1800.0)
    assert c.drug_conc == 2.0
    assert c.awaiting_escalation_confirm is False
    assert c.last_escalation_time == 1800.0
    # Log got back-patched
    assert c.escalation_log[-1]["confirmed_time"] == 1800.0
    assert c.escalation_log[-1]["confirmed_conc"] == 2.0
    assert c.escalation_count == 1
    print("PASS  confirm_escalation updates drug_conc and back-patches log")


def test_confirm_with_override_concentration() -> None:
    c = _make(adaptation_threshold_per_hour=0.3)
    _feed_exponential(c, mu_per_hour=0.8, span_seconds=1800.0, samples=20)
    c.decide(now=1800.0)
    c.pending_escalation()
    # Override: user actually swapped to 5x instead of proposed 2x
    c.confirm_escalation(new_conc=5.0, timestamp=1800.0)
    assert c.drug_conc == 5.0
    assert c.escalation_log[-1]["confirmed_conc"] == 5.0
    print("PASS  confirm_escalation accepts override concentration")


def test_reminder_due_after_interval() -> None:
    c = _make(adaptation_threshold_per_hour=0.3,
              escalation_reminder_interval_seconds=600.0)
    _feed_exponential(c, mu_per_hour=0.8, span_seconds=1800.0, samples=20)
    c.decide(now=1800.0)
    c.pending_escalation()
    # No reminder due immediately after proposal
    assert not c.due_for_reminder(now=1800.0 + 100.0)
    # Due after reminder interval
    assert c.due_for_reminder(now=1800.0 + 700.0)
    c.mark_reminder_sent(now=1800.0 + 700.0)
    # Not due again until another interval passes
    assert not c.due_for_reminder(now=1800.0 + 1000.0)
    assert c.due_for_reminder(now=1800.0 + 1400.0)
    print("PASS  due_for_reminder fires after each reminder interval")


def test_state_roundtrip_preserves_all() -> None:
    c = _make(adaptation_threshold_per_hour=0.3)
    _feed_exponential(c, mu_per_hour=0.8, span_seconds=1800.0, samples=20)
    c.decide(now=1800.0)
    c.pending_escalation()
    c.confirm_escalation(new_conc=2.0, timestamp=1800.0)
    # Add more history
    _feed_exponential(c, mu_per_hour=0.2, span_seconds=600.0,
                      samples=10, start_od=0.3, t0=1800.0)
    c.decide(now=2400.0)

    state = c.to_state()
    other = _make(adaptation_threshold_per_hour=0.3)
    other.restore_state(state)

    assert other.drug_conc == c.drug_conc
    assert other.last_escalation_time == c.last_escalation_time
    assert other.awaiting_escalation_confirm == c.awaiting_escalation_confirm
    assert len(other.escalation_log) == len(c.escalation_log)
    assert other.escalation_log[-1]["confirmed_conc"] == 2.0
    assert len(other.timestamped_od_history) == len(c.timestamped_od_history)
    # Inner turbidostat state preserved
    assert other._inner.target == c._inner.target
    assert other._inner.last_pump_time == c._inner.last_pump_time
    print("PASS  state round-trip preserves drug_conc, log, awaiting flag, history, inner state")


def test_negative_growth_no_escalation() -> None:
    """Decaying OD -> mu < 0 -> never escalates."""
    c = _make(adaptation_threshold_per_hour=0.1)
    _feed_exponential(c, mu_per_hour=-0.5, span_seconds=1800.0, samples=20)
    c.decide(now=1800.0)
    assert c.pending_escalation() is None
    assert c.awaiting_escalation_confirm is False
    print("PASS  negative growth rate does not trigger escalation")


def main() -> int:
    test_dilution_delegates_to_turbidostat()
    test_growth_rate_estimation_log_linear()
    test_growth_rate_returns_none_with_sparse_history()
    test_growth_rate_returns_none_when_flat()
    test_escalation_triggers_above_threshold()
    test_escalation_consumed_on_read()
    test_no_double_trigger_while_awaiting_confirm()
    test_escalation_cooldown_respected()
    test_confirm_updates_drug_conc()
    test_confirm_with_override_concentration()
    test_reminder_due_after_interval()
    test_state_roundtrip_preserves_all()
    test_negative_growth_no_escalation()
    print("\nAll morbidostat tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
