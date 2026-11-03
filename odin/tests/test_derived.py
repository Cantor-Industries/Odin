"""Tests for telemetry/derived.py.

This is the only module in Odin that does real arithmetic, and it is the one
place where a wrong answer reaches the pilot's eyes as a plausible number.
Everything else is transport or drawing.

Two kinds of test here:

  * concrete cases with hand-computed answers, so a change that breaks the
    maths fails for a readable reason;
  * hypothesis properties, so the invariants hold for inputs nobody thought
    to write down (and geographically unlikely but numerically legal ones).

The properties are deliberately stated as *invariants*, not golden values:
haversine is symmetric, bearing is in [0, 360), distance is non-negative,
consumed charge is monotonic. Those hold regardless of implementation detail.
"""

from __future__ import annotations

import math

import derived
import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# mode_code
# ---------------------------------------------------------------------------

def test_mode_code_known_modes():
    assert derived.mode_code("LOITER") == "LOIT"
    assert derived.mode_code("RTL") == "RTL"
    assert derived.mode_code("GUIDED_NOGPS") == "GUID"
    assert derived.mode_code("stabilize") == "STAB"      # case-insensitive


def test_mode_code_unknown_with_custom_mode():
    # The APM sends custom_mode for firmware this table does not know.
    assert derived.mode_code("SOMETHING_NEW", custom_mode=42) == "MODE42"


def test_mode_code_unknown_without_custom_mode_truncates():
    assert derived.mode_code("SOMETHINGELSE") == "SOME"


def test_mode_code_missing_is_none_not_empty_string():
    # None drives "---" in the HUD. "" would render as a blank box.
    assert derived.mode_code(None) is None
    assert derived.mode_code("") is None


# ---------------------------------------------------------------------------
# haversine_m
# ---------------------------------------------------------------------------

def test_haversine_zero_distance():
    assert derived.haversine_m(0.0, 0.0, 0.0, 0.0) == pytest.approx(0.0, abs=1e-9)


def test_haversine_one_degree_of_latitude():
    # One degree of latitude is 111.32 km at any longitude.
    d = derived.haversine_m(0.0, 0.0, 1.0, 0.0)
    assert d == pytest.approx(111_319.5, rel=1e-4)


def test_haversine_antipodal_does_not_overflow():
    # The min(1.0, sqrt(a)) clamp exists for exactly this: a is ~1.0 and
    # floating point can push it just over, making asin() raise.
    d = derived.haversine_m(0.0, 0.0, 0.0, 180.0)
    assert d == pytest.approx(math.pi * 6378137.0, rel=1e-6)


def test_haversine_known_local_distance():
    # Two points ~1 km apart near Nairobi, checked against the same formula
    # computed independently.
    d = derived.haversine_m(-1.2921, 36.8219, -1.3011, 36.8219)
    assert 990 < d < 1010


@given(
    lat1=st.floats(-89.9, 89.9),
    lon1=st.floats(-180, 180),
    lat2=st.floats(-89.9, 89.9),
    lon2=st.floats(-180, 180),
)
@settings(max_examples=200, deadline=None)
def test_haversine_is_symmetric(lat1, lon1, lat2, lon2):
    """Distance A->B equals B->A. If this fails, a sign or argument order is wrong."""
    ab = derived.haversine_m(lat1, lon1, lat2, lon2)
    ba = derived.haversine_m(lat2, lon2, lat1, lon1)
    # Absolute tolerance: a metre on a globe-sized number is well below GPS noise.
    assert ab == pytest.approx(ba, abs=1e-6)


@given(
    lat1=st.floats(-89.9, 89.9),
    lon1=st.floats(-180, 180),
    lat2=st.floats(-89.9, 89.9),
    lon2=st.floats(-180, 180),
)
@settings(max_examples=200, deadline=None)
def test_haversine_never_negative_and_never_nan(lat1, lon1, lat2, lon2):
    d = derived.haversine_m(lat1, lon1, lat2, lon2)
    assert not math.isnan(d)
    assert d >= 0.0


# ---------------------------------------------------------------------------
# bearing_deg
# ---------------------------------------------------------------------------

def test_bearing_north():
    # Due north: 0 degrees, not 360.
    assert derived.bearing_deg(0.0, 0.0, 1.0, 0.0) == pytest.approx(0.0, abs=1e-6)


def test_bearing_east():
    assert derived.bearing_deg(0.0, 0.0, 0.0, 1.0) == pytest.approx(90.0, abs=1e-6)


def test_bearing_south():
    assert derived.bearing_deg(1.0, 0.0, 0.0, 0.0) == pytest.approx(180.0, abs=1e-6)


def test_bearing_west():
    # Due west is 270, and it must come back as 270 rather than -90.
    assert derived.bearing_deg(0.0, 1.0, 0.0, 0.0) == pytest.approx(270.0, abs=1e-6)


def test_bearing_due_north_does_not_return_360():
    """A 360 reading would spin the compass needle to a different place than 0."""
    b = derived.bearing_deg(10.0, 20.0, 10.0001, 20.0)
    assert 0.0 <= b < 360.0


@given(
    lat1=st.floats(-89.9, 89.9),
    lon1=st.floats(-180, 180),
    lat2=st.floats(-89.9, 89.9),
    lon2=st.floats(-180, 180),
)
@settings(max_examples=300, deadline=None)
def test_bearing_always_in_range(lat1, lon1, lat2, lon2):
    """The HUD rotates the compass by this value; out of range wraps wrong."""
    b = derived.bearing_deg(lat1, lon1, lat2, lon2)
    assert not math.isnan(b)
    assert 0.0 <= b < 360.0


@given(
    lat=st.floats(-89.9, 89.9),
    lon=st.floats(-175, 175),
    dlat=st.floats(-0.001, 0.001),
    dlon=st.floats(-0.001, 0.001),
)
@settings(max_examples=200, deadline=None)
def test_bearing_round_trip_through_distance(lat, lon, dlat, dlon):
    """
    Moving a short distance then asking for the bearing *back* should be
    roughly opposite to the outgoing bearing.

    This is the sign-error check: a flipped atan2 or a swapped argument pair
    gives a reciprocal that is not ~180 degrees away.
    """
    lat2, lon2 = lat + dlat, lon + dlon
    # Bearing between identical points is undefined (both come back 0), so
    # the reciprocal check is meaningless there.
    assume(abs(dlat) > 1e-9 or abs(dlon) > 1e-9)
    # Short hops near the poles are geometrically odd; skip those rather
    # than loosen the bound into uselessness.
    assume(abs(lat) < 80)

    out = derived.bearing_deg(lat, lon, lat2, lon2)
    back = derived.bearing_deg(lat2, lon2, lat, lon)
    diff = abs((out - back) - 180.0) % 360.0
    diff = min(diff, 360.0 - diff)
    assert diff < 1.0


# ---------------------------------------------------------------------------
# DerivedState: position, distance, home
# ---------------------------------------------------------------------------

def test_distance_ignores_gps_jitter(derived_state):
    """Sub-metre noise while parked must not add 'distance flown'."""
    for _ in range(50):
        derived_state.on_position(-1.2921, 36.8219, 0.0)
        derived_state.on_position(-1.29210001, 36.82190001, 0.0)
    assert derived_state.total_dist_m == 0.0


def test_distance_accumulates_real_movement(derived_state):
    # ~111 m north.
    derived_state.on_position(-1.2921, 36.8219, 0.0)
    derived_state.on_position(-1.2931, 36.8219, 1.0)
    assert derived_state.total_dist_m == pytest.approx(111.2, abs=2.0)


def test_zero_zero_position_is_ignored(derived_state):
    """APM sends 0/0 before it has a fix; that must not become a home."""
    derived_state.on_position(0.0, 0.0, 0.0)
    assert derived_state._last_lat is None
    assert derived_state.total_dist_m == 0.0


def test_home_set_on_arm_when_position_known(derived_state):
    derived_state.on_position(-1.2921, 36.8219, 0.0)
    derived_state.on_armed(True, 10.0)
    assert derived_state.home_source == "arm"
    assert derived_state.home_lat == pytest.approx(-1.2921)


def test_home_not_set_on_arm_without_position(derived_state):
    derived_state.on_armed(True, 10.0)
    assert derived_state.home_lat is None
    assert derived_state.home_source is None


def test_home_fallback_to_first_good_fix(derived_state):
    derived_state.on_gps(-1.29, 36.82, fix=3, sats=8)
    assert derived_state.home_source == "first_fix"


def test_home_needs_enough_satellites(derived_state):
    derived_state.on_gps(-1.29, 36.82, fix=3, sats=3)
    assert derived_state.home_lat is None


def test_home_needs_a_3d_fix(derived_state):
    derived_state.on_gps(-1.29, 36.82, fix=2, sats=12)
    assert derived_state.home_lat is None


def test_home_from_fc_is_scaled_from_e7(derived_state):
    """HOME_POSITION carries degrees * 1e7; forget the divide and home is 1e7 off."""
    derived_state.on_home_position(-12921000, 368219000)
    assert derived_state.home_lat == pytest.approx(-1.2921, abs=1e-9)
    assert derived_state.home_lon == pytest.approx(36.8219, abs=1e-9)


def test_home_distance_and_bearing(derived_state):
    derived_state.on_position(-1.0, 36.0, 0.0)
    derived_state.on_armed(True, 1.0)
    # Fly ~111 m north of home.
    derived_state.on_position(-0.999, 36.0, 2.0)
    assert derived_state.home_distance() == pytest.approx(111.2, abs=2.0)
    # Home is now due south of us: 180.
    assert derived_state.home_bearing() == pytest.approx(180.0, abs=1.0)


def test_home_distance_is_none_before_home(derived_state):
    derived_state.on_position(-1.0, 36.0, 0.0)
    assert derived_state.home_distance() is None
    assert derived_state.home_bearing() is None


# ---------------------------------------------------------------------------
# DerivedState: flight timer
# ---------------------------------------------------------------------------

def test_flight_time_none_before_arming(derived_state):
    assert derived_state.flight_time_s(0.0) is None


def test_flight_time_running_while_armed(derived_state):
    derived_state.on_armed(True, 100.0)
    assert derived_state.flight_time_s(160.0) == pytest.approx(60.0)


def test_flight_time_stops_at_disarm(derived_state):
    derived_state.on_armed(True, 100.0)
    derived_state.on_armed(False, 160.0)
    # Time keeps passing, but the timer must freeze at the disarm.
    assert derived_state.flight_time_s(999.0) == pytest.approx(60.0)


def test_second_arm_does_not_restart_timer(derived_state):
    """A spurious re-arm while already armed must not reset the clock."""
    derived_state.on_armed(True, 100.0)
    derived_state.on_armed(True, 150.0)
    assert derived_state.flight_time_s(160.0) == pytest.approx(60.0)


def test_flight_time_never_negative(derived_state):
    # Clock going backwards (NTP step) must not produce -5 seconds.
    derived_state.on_armed(True, 100.0)
    assert derived_state.flight_time_s(50.0) == 0.0


# ---------------------------------------------------------------------------
# DerivedState: battery
# ---------------------------------------------------------------------------

def test_cell_voltage_divides_by_cells():
    import derived as d
    st3 = d.DerivedState(batt_cells=3)
    assert st3.cell_voltage(11.1) == pytest.approx(3.7)


def test_cell_voltage_unknown_cells_is_none():
    """TELEM_CELLS=0 means 'do not know', so per-cell must be --- not pack/0."""
    import derived as d
    st0 = d.DerivedState(batt_cells=0)
    assert st0.cell_voltage(11.1) is None


def test_cell_voltage_zero_voltage_is_none():
    import derived as d
    assert d.DerivedState(batt_cells=3).cell_voltage(0.0) is None
    assert d.DerivedState(batt_cells=3).cell_voltage(None) is None


def test_mah_integration(derived_state):
    """1 A for 3600 s is 1000 mAh."""
    derived_state.on_current(1.0, 3600.0)
    assert derived_state.mah_used == pytest.approx(1000.0, rel=1e-9)


def test_mah_ignores_zero_and_negative_current(derived_state):
    # A sensor that reports 0 A is not "no data", but it is not consumption
    # either. Negative (regen/telemetry glitch) must not subtract.
    derived_state.on_current(0.0, 10.0)
    derived_state.on_current(-5.0, 10.0)
    assert derived_state.mah_used is None


def test_mah_ignores_non_positive_dt(derived_state):
    derived_state.on_current(1.0, 0.0)
    derived_state.on_current(1.0, -1.0)
    assert derived_state.mah_used is None


@given(
    samples=st.lists(
        st.tuples(
            # Positive current only: 0 A is "sensor says nothing is flowing",
            # which on_current() deliberately ignores instead of starting a
            # counter at zero. The monotonic invariant is about real draws.
            st.floats(0.001, 60.0, allow_nan=False, allow_infinity=False),
            st.floats(0.001, 2.0, allow_nan=False, allow_infinity=False),
        ),
        min_size=1,
        max_size=50,
    )
)
@settings(max_examples=100, deadline=None)
def test_mah_never_decreases(samples):
    """Charge consumed is monotonic; a dip means a sign error crept in."""
    import derived as d
    state = d.DerivedState(batt_cells=3)     # own fixture: hypothesis needs a
    prev = 0.0                               # fresh object per example
    for amps, dt in samples:
        state.on_current(amps, dt)
        now = state.mah_used
        assert now is not None
        assert now >= prev
        prev = now


# ---------------------------------------------------------------------------
# DerivedState: efficiency
# ---------------------------------------------------------------------------

def test_efficiency_none_without_consumption(derived_state):
    assert derived_state.efficiency(0.0) == (None, None)


def test_efficiency_per_hour(derived_state):
    # 1 A for 3600 s = 1000 mAh, over one hour = 1000 mAh/h.
    derived_state.on_armed(True, 0.0)
    derived_state.on_current(1.0, 3600.0)
    per_km, per_h = derived_state.efficiency(3600.0)
    assert per_h == pytest.approx(1000.0, rel=1e-6)
    assert per_km is None              # no distance flown


def test_efficiency_per_km(derived_state):
    derived_state.total_dist_m = 1000.0
    derived_state.on_current(1.0, 3600.0)   # 1000 mAh
    per_km, _ = derived_state.efficiency(0.0)
    assert per_km == pytest.approx(1000.0, rel=1e-6)


def test_efficiency_below_50m_is_noise(derived_state):
    """Tiny distances produce huge mAh/km; report unknown instead."""
    derived_state.total_dist_m = 40.0
    derived_state.on_current(1.0, 3600.0)
    per_km, _ = derived_state.efficiency(0.0)
    assert per_km is None


def test_efficiency_figures_stay_finite(derived_state):
    # Very short flight: must not divide by ~0 and produce inf/NaN.
    derived_state.on_armed(True, 0.0)
    derived_state.on_current(50.0, 1.0)
    derived_state.total_dist_m = 60.0
    per_km, per_h = derived_state.efficiency(0.01)
    for v in (per_km, per_h):
        assert v is None or math.isfinite(v)


# ---------------------------------------------------------------------------
# reset
# ---------------------------------------------------------------------------

def test_reset_forgets_home_and_totals(derived_state):
    derived_state.on_position(-1.0, 36.0, 0.0)
    derived_state.on_armed(True, 0.0)
    derived_state.on_current(1.0, 100.0)
    derived_state.on_position(-1.1, 36.1, 10.0)

    derived_state.reset()

    assert derived_state.home_lat is None
    assert derived_state.total_dist_m == 0.0
    assert derived_state.mah_used is None
    assert derived_state.flight_start is None
    assert derived_state.home_distance() is None


def test_reset_keeps_configured_cell_count():
    """reset() clears a flight, not the vehicle's battery configuration."""
    import derived as d
    st4 = d.DerivedState(batt_cells=4)
    st4.reset()
    assert st4.batt_cells == 4
