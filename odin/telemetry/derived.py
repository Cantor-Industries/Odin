#!/usr/bin/env python3
"""
Derived flight values for the HUD.

The HUD spec puts arithmetic in the backend and leaves the browser to draw.
This module owns that arithmetic: home position, distance flown, flight timer,
battery per-cell voltage, consumed mAh and the efficiency figures.

Everything here is pure and defensive. A missing input yields None, never 0 and
never NaN, because the HUD renders None as "---" and 0 would be a lie (0 m
distance-to-home on the pad is very different from "we do not know yet").

Read-only by design: nothing in this file talks to the flight controller.
"""

from __future__ import annotations

import math
import time

# --------------------------------------------------------------------------
# Tunables
# --------------------------------------------------------------------------

# Copter modes to the four-letter codes the OSD uses. The long names in
# server.py are for logs; these are what fits in the mode box.
MODE_SHORT = {
    "STABILIZE": "STAB",
    "ALT_HOLD": "ALTH",
    "LOITER": "LOIT",
    "AUTO": "AUTO",
    "GUIDED": "GUID",
    "GUIDED_NOGPS": "GUID",
    "RTL": "RTL",
    "SMART_RTL": "SRTL",
    "AUTO_RTL": "ARTL",
    "LAND": "LAND",
    "ACRO": "ACRO",
    "POSHOLD": "POSH",
    "POSITION": "POSI",
    "CIRCLE": "CIRC",
    "DRIFT": "DRFT",
    "SPORT": "SPRT",
    "BRAKE": "BRAK",
    "FLIP": "FLIP",
    "AUTOTUNE": "ATUN",
    "THROW": "THRW",
    "AVOID_ADSB": "AVOI",
    "FLOWHOLD": "FLOW",
    "FOLLOW": "FOLW",
    "ZIGZAG": "ZIGZ",
    "SYSTEMID": "SYST",
    "AUTOROTATE": "AROT",
    "TURTLE": "TRTL",
    "OF_LOITER": "OFLO",
}


def mode_code(mode: str | None, custom_mode: int | None = None) -> str | None:
    """Four-letter flight-mode code, or MODE<n> for an unknown mode."""
    if not mode:
        return None
    code = MODE_SHORT.get(str(mode).upper())
    if code:
        return code
    if custom_mode is not None:
        return f"MODE{custom_mode}"
    return str(mode).upper()[:4]


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    r = 6378137.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing from point 1 to point 2, 0..360."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


class DerivedState:
    """Accumulates the quantities that need history (distance, mAh, timer).

    Fed one sample per telemetry message batch. Must be sampled at a fairly
    steady rate for the integrals to be accurate; snapshot() does that.
    """

    # Do not integrate distance while barely moving: GPS position noise would
    # otherwise accumulate tens of metres of "flight" while sitting on the pad.
    MIN_DIST_STEP_M = 0.7
    # Before a home position is known, ignore the first unstable fixes.
    HOME_MIN_SATS = 5

    def __init__(self, batt_cells: int | None = 3) -> None:
        self.batt_cells = batt_cells
        self.reset()

    def reset(self) -> None:
        self.home_lat: float | None = None
        self.home_lon: float | None = None
        self.home_source: str | None = None   # "arm" | "first_fix" | "fc"
        self.total_dist_m = 0.0
        self.mah_used: float | None = None    # None until current is seen
        self.flight_start: float | None = None
        self.flight_end: float | None = None
        self._prev_pos: tuple[float, float] | None = None
        self._prev_ts: float | None = None
        self._prev_path_ts: float | None = None
        self._last_lat: float | None = None
        self._last_lon: float | None = None

    # -- inputs ---------------------------------------------------------
    def on_position(self, lat: float | None, lon: float | None, now: float) -> None:
        if lat is None or lon is None:
            return
        if lat == 0.0 and lon == 0.0:
            return                      # APM sends 0/0 before it has a fix
        self._last_lat, self._last_lon = lat, lon

        # Distance flown: integrate the ground path.
        if self._prev_pos is not None:
            step = haversine_m(self._prev_pos[0], self._prev_pos[1], lat, lon)
            if step >= self.MIN_DIST_STEP_M:
                self.total_dist_m += step
                self._prev_pos = (lat, lon)
        else:
            self._prev_pos = (lat, lon)

    def on_armed(self, armed: bool, now: float) -> None:
        if armed and self.flight_start is None:
            self.flight_start = now
            self.flight_end = None
            # Home is where we armed, if we already have a position.
            if (self.home_lat is None and self._last_lat is not None):
                self._set_home(self._last_lat, self._last_lon, "arm")
        elif not armed and self.flight_start is not None:
            self.flight_end = now

    def on_gps(self, lat, lon, fix: int, sats: int) -> None:
        """Fallback home: first good fix, if we never armed."""
        if self.home_lat is not None:
            return
        if fix is None or fix < 3 or (sats or 0) < self.HOME_MIN_SATS:
            return
        if lat in (None, 0.0) or lon in (None, 0.0):
            return
        self._set_home(lat, lon, "first_fix")

    def on_home_position(self, lat, lon) -> None:
        """HOME_POSITION message, if the firmware sends one."""
        if lat in (None, 0.0) or lon in (None, 0.0):
            return
        self._set_home(lat / 1e7, lon / 1e7, "fc")

    def on_current(self, amps: float | None, dt: float) -> None:
        """Integrate current into consumed mAh."""
        if amps is None or amps <= 0 or dt <= 0:
            return
        if self.mah_used is None:
            self.mah_used = 0.0
        self.mah_used += amps * dt / 3.6    # A*s -> mAh

    def _set_home(self, lat, lon, source: str) -> None:
        self.home_lat, self.home_lon = lat, lon
        self.home_source = source

    # -- outputs --------------------------------------------------------
    def flight_time_s(self, now: float) -> float | None:
        if self.flight_start is None:
            return None
        end = self.flight_end if self.flight_end is not None else now
        return max(0.0, end - self.flight_start)

    def home_distance(self) -> float | None:
        if self.home_lat is None or self._last_lat is None:
            return None
        return haversine_m(self._last_lat, self._last_lon,
                           self.home_lat, self.home_lon)

    def home_bearing(self) -> float | None:
        if self.home_lat is None or self._last_lat is None:
            return None
        return bearing_deg(self._last_lat, self._last_lon,
                           self.home_lat, self.home_lon)

    def cell_voltage(self, batt_v: float | None) -> float | None:
        if not batt_v or not self.batt_cells:
            return None
        return batt_v / self.batt_cells

    def efficiency(self, now: float) -> tuple[float | None, float | None]:
        """(mAh per km, mAh per hour). None when the inputs are missing."""
        if not self.mah_used:
            return None, None
        per_km = None
        km = self.total_dist_m / 1000.0
        if km > 0.05:                    # below ~50 m the figure is noise
            per_km = self.mah_used / km
        per_h = None
        secs = self.flight_time_s(now)
        if secs and secs > 5:
            per_h = self.mah_used / (secs / 3600.0)
        return per_km, per_h
