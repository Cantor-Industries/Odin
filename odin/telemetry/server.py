#!/usr/bin/env python3
"""
Drone vision telemetry service.

Reads MAVLink telemetry (read-only), maintains a flight state dict, and
serves it to the browser over a WebSocket at 20 Hz, together with the
static HUD page.

Sources (--source / TELEM_SOURCE):
    serial:/dev/ttyUSB0:57600   real flight controller over serial
    udp:127.0.0.1:14550         MAVLink over UDP (sim_mavlink.py, SITL, MAVProxy)
    sim                         in-process fake data, no MAVLink at all
    auto                        probe serial candidates, wait if none answers
    file:/path/to/log.tlog      replay a captured telemetry log

A missing flight controller is a NORMAL state. The service starts, serves
the page and reports link_ok=false / source="none" until a heartbeat arrives.

This service NEVER sends commands to the flight controller. The only
optional outgoing message is a data-stream rate request, and only when
explicitly enabled with --request-streams.
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import logging
import math
import os
import signal
import sys
import threading
import time
import types
from pathlib import Path

from aiohttp import WSCloseCode, WSMsgType, web
from pymavlink import mavutil

import aiohttp

# Local modules. sys.path already contains this directory when run as a script,
# but an explicit insert keeps `python -c` and unit tests working too.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import derived  # noqa: E402
import sysinfo  # noqa: E402

log = logging.getLogger("telemetry")

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_ENV = BASE_DIR / "telemetry" / "config.env"
# Optional boot override written by the HUD's SIM toggle ("persist"). Kept
# apart from config.env so it is obvious whether a boot default was changed.
SOURCE_ENV = BASE_DIR / "telemetry" / "source.env"
WEB_DIR = BASE_DIR / "web"

HEARTBEAT_TIMEOUT_S = 3.0      # link_ok goes false after this long without HEARTBEAT
SERIAL_SILENCE_RECONNECT_S = 10.0  # serial: reconnect if no heartbeat for this long
AUTO_RETRY_S = 5.0             # auto mode: pause between candidate scans
STATUSTEXT_TTL_S = 6.0

# Video pipeline telemetry written by telemetry/camera_pipe.py (see _RelayStats).
VIDEO_STATS_PATH = Path(os.environ.get("VIDEO_STATS_PATH",
                                       "/tmp/odin-video.json"))
VIDEO_STATS_MAX_AGE_S = 5.0    # older than this = pipeline not running

MAV_MODE_FLAG_SAFETY_ARMED = 128

# ArduCopter custom_mode table. Covers old (3.2.x) and newer firmware.
COPTER_MODES = {
    0: "STABILIZE", 1: "ACRO", 2: "ALT_HOLD", 3: "AUTO", 4: "GUIDED",
    5: "LOITER", 6: "RTL", 7: "CIRCLE", 8: "POSITION", 9: "LAND",
    10: "OF_LOITER", 11: "DRIFT", 13: "SPORT", 14: "FLIP", 15: "AUTOTUNE",
    16: "POSHOLD", 17: "BRAKE", 18: "THROW", 19: "AVOID_ADSB",
    20: "GUIDED_NOGPS", 21: "SMART_RTL", 22: "FLOWHOLD", 23: "FOLLOW",
    24: "ZIGZAG", 25: "SYSTEMID", 26: "AUTOROTATE", 27: "AUTO_RTL",
    28: "TURTLE",
}

SERIAL_CANDIDATES = ("/dev/ttyUSB*", "/dev/ttyACM*", "/dev/ttyAMA0")

# --------------------------------------------------------------------------
# Camera colour controls (mirrored from telemetry/camera_ctl.py)
# --------------------------------------------------------------------------
CAMERA_CONTROLS = {
    "brightness": {"env": "CAM_BRIGHTNESS", "range": [-1.0, 1.0], "default": "0"},
    "contrast":   {"env": "CAM_CONTRAST",   "range": [0.0, 4.0],  "default": "1"},
    "saturation": {"env": "CAM_SATURATION", "range": [0.0, 4.0],  "default": "1"},
    "sharpness":  {"env": "CAM_SHARPNESS",  "range": [0.0, 4.0],  "default": "1"},
    "ev":         {"env": "CAM_EV",         "range": [-10.0, 10.0], "default": "0"},
    "awb":        {"env": "CAM_AWB",
                   "enum": ["auto", "incandescent", "tungsten", "fluorescent",
                            "indoor", "daylight", "cloudy", "custom"]},
    "metering":   {"env": "CAM_METERING",
                   "enum": ["centre", "spot", "average", "custom"]},
    "fps":        {"env": "CAM_FPS", "range": [1, 60]},
    "quality":    {"env": "CAM_JPEG_QUALITY", "range": [1, 100]},
    "hflip":      {"env": "CAM_HFLIP"},
    "vflip":      {"env": "CAM_VFLIP"},
    "tuning":     {"env": "CAM_TUNING", "enum": ["auto", "default"]},
}

CAMERA_PRESETS = {
    "default": {},
    "punchy": {"saturation": "1.4", "contrast": "1.15", "sharpness": "1.2"},
    "flat": {"saturation": "0.85", "contrast": "0.95", "sharpness": "1.0"},
    "overcast": {"awb": "cloudy", "brightness": "0.08", "contrast": "1.05"},
    "sunny": {"awb": "daylight", "ev": "-0.3", "contrast": "1.1"},
}



# --------------------------------------------------------------------------
# Flight state
# --------------------------------------------------------------------------

def _round(value, places: int):
    """round() that preserves None. Unknown must stay unknown, not become 0."""
    if value is None:
        return None
    try:
        return round(float(value), places)
    except (TypeError, ValueError):
        return None


def _default_state() -> dict:
    return {
        "t": 0.0,
        "link_ok": False,
        "source": "none",
        "armed": False,
        "mode": "",
        "custom_mode": None,
        "roll": 0.0, "pitch": 0.0, "yaw": 0.0,
        "heading": 0, "alt": 0.0, "groundspeed": 0.0, "airspeed": 0.0,
        "climb": 0.0,
        "throttle": 0,
        "batt_v": 0.0, "batt_a": 0.0, "batt_pct": -1,
        "gps_fix": 0, "sats": 0, "lat": 0.0, "lon": 0.0,
        "msg": "",
        # extra field (additive; the shape above is untouched): true when the
        # feed identifies itself as simulated, so the HUD can label it.
        "sim": False,

        # --- HUD spec section 5 additions -------------------------------
        # All optional: None means "unknown", which the HUD draws as "---".
        # Never default these to 0, because 0 is a real reading.
        "hdop": None,
        "alt_rel": None,
        "alt_msl": None,
        "home_dist": None,
        "home_bearing": None,
        "home_set": False,
        "flight_time_s": None,
        "total_dist_m": None,
        "batt_cells": None,
        "batt_cell_v": None,
        "mah_used": None,
        "mah_per_km": None,
        "mah_per_hour": None,
    }


class TelemetryState:
    """Thread-safe holder for the latest flight state.

    The reader runs in its own thread (pymavlink is a blocking API); the
    asyncio side only ever reads snapshots, so the event loop never blocks.
    """

    def __init__(self, batt_cells: int | None = 3) -> None:
        self._lock = threading.Lock()
        self._data = _default_state()
        self._hb_time = 0.0
        self._msg_time = 0.0
        # Derived values need history (home, distance flown, mAh), so they live
        # in their own object rather than in the telemetry dict.
        self.derived = derived.DerivedState(batt_cells=batt_cells)
        self._prev_sample_ts: float | None = None
        self._last_current: float | None = None
        self._system = sysinfo.SystemSampler()
        self._bytes_out = 0
        self._bytes_in = 0

    # -- writes ---------------------------------------------------------
    def set_source(self, source: str) -> None:
        with self._lock:
            self._data["source"] = source
            self._data["sim"] = (source == "sim")
            if source in ("none", "sim"):
                # Switching source invalidates the old link. Without this the
                # heartbeat timestamp from the previous source keeps link_ok
                # true for another few seconds, so the HUD would still say
                # LIVE (or SIM) after the thing producing it had gone.
                self._hb_time = 0.0
                self._data["armed"] = False
        if source in ("none", "sim"):
            # A new link means a new flight: forget the old home and totals so
            # the HUD does not show the previous session's distance to home.
            self.derived.reset()

    def handle(self, m) -> None:
        """Update the state from one pymavlink message."""
        try:
            mtype = m.get_type()
        except Exception:
            return
        if mtype == "BAD_DATA":
            return

        # Telemetry data rate: count the wire bytes pymavlink just parsed.
        try:
            self._bytes_in += len(m.get_msgbuf())
        except Exception:  # noqa: BLE001
            pass

        now = time.time()
        with self._lock:
            d = self._data
            if mtype == "HEARTBEAT":
                self._hb_time = now
                armed = bool(getattr(m, "base_mode", 0) & MAV_MODE_FLAG_SAFETY_ARMED)
                d["armed"] = armed
                d["mode"] = COPTER_MODES.get(getattr(m, "custom_mode", -1), str(getattr(m, "custom_mode", "")))
                d["custom_mode"] = getattr(m, "custom_mode", None)
                self.derived.on_armed(armed, now)
            elif mtype == "ATTITUDE":
                d["roll"] = round(math.degrees(getattr(m, "roll", 0.0)), 2)
                d["pitch"] = round(math.degrees(getattr(m, "pitch", 0.0)), 2)
                d["yaw"] = round((math.degrees(getattr(m, "yaw", 0.0))) % 360.0, 2)
            elif mtype == "VFR_HUD":
                d["airspeed"] = round(getattr(m, "airspeed", 0.0), 2)
                d["groundspeed"] = round(getattr(m, "groundspeed", 0.0), 2)
                d["alt"] = round(getattr(m, "alt", 0.0), 2)
                d["climb"] = round(getattr(m, "climb", 0.0), 2)
                d["heading"] = int(getattr(m, "heading", 0))
                d["throttle"] = int(getattr(m, "throttle", 0))
            elif mtype == "GLOBAL_POSITION_INT":
                d["lat"] = getattr(m, "lat", 0) / 1e7
                d["lon"] = getattr(m, "lon", 0) / 1e7
                rel = getattr(m, "relative_alt", None)
                msl = getattr(m, "alt", None)
                d["alt"] = round(rel / 1000.0, 2) if rel is not None else None
                d["alt_rel"] = d["alt"]
                d["alt_msl"] = round(msl / 1000.0, 2) if msl is not None else None
                self.derived.on_position(d["lat"], d["lon"], now)
            elif mtype == "HOME_POSITION":
                self.derived.on_home_position(getattr(m, "latitude", None),
                                              getattr(m, "longitude", None))
            elif mtype == "GPS_RAW_INT":
                d["gps_fix"] = int(getattr(m, "fix_type", 0))
                d["sats"] = int(getattr(m, "satellites_visible", 0))
                # eph is HDOP * 100, and arrives as 65535 (unknown) on 3.2.
                eph = getattr(m, "eph", None)
                if eph not in (None, 65535, 0):
                    d["hdop"] = round(eph / 100.0, 2)
                if getattr(m, "alt", None):
                    d["alt_msl"] = round(m.alt / 1000.0, 2)
                # GPS position is authoritative when present. Note: GPS_RAW_INT.alt
                # is absolute (MSL) altitude, so it is NOT used for the HUD's
                # relative altitude (that comes from GLOBAL_POSITION_INT).
                if getattr(m, "lat", 0) or getattr(m, "lon", 0):
                    d["lat"] = m.lat / 1e7
                    d["lon"] = m.lon / 1e7
                    self.derived.on_gps(d["lat"], d["lon"], d["gps_fix"], d["sats"])
            elif mtype == "SYS_STATUS":
                d["batt_v"] = round(getattr(m, "voltage_battery", 0) / 1000.0, 2)
                cur = getattr(m, "current_battery", -1)
                # -1 means "no measurement"; 0 from a real sensor is noise but
                # valid, so keep the two apart.
                if cur in (None, -1):
                    d["batt_a"] = None
                else:
                    d["batt_a"] = round(cur / 100.0, 2)
                d["batt_pct"] = int(getattr(m, "battery_remaining", -1))
            elif mtype == "STATUSTEXT":
                text = getattr(m, "text", "")
                if isinstance(text, bytes):
                    text = text.decode("utf-8", "replace")
                text = str(text).rstrip("\x00").strip()
                d["msg"] = text
                self._msg_time = now
                # The bundled simulator prefixes its messages with "SIM:".
                # Treat that as a permanent marker for this link so fake data
                # can never be displayed as LIVE.
                if text.upper().startswith("SIM:") or text.upper().startswith("APM:SIM"):
                    d["sim"] = True

    # -- reads ----------------------------------------------------------
    def video_stats(self) -> dict:
        """Latest Pi-side video pipeline numbers, or an idle placeholder.

        Written once a second by camera_pipe.py. A missing or stale file is
        normal (the camera stream may not have a viewer yet), so this returns
        a well-formed object with ok=False rather than raising.
        """
        try:
            payload = json.loads(VIDEO_STATS_PATH.read_text())
        except (OSError, ValueError):
            return {"ok": False}
        if not isinstance(payload, dict):
            return {"ok": False}
        age = time.time() - float(payload.get("t") or 0)
        payload["age_s"] = round(age, 1)
        if age > VIDEO_STATS_MAX_AGE_S:
            payload["ok"] = False
            payload["stale"] = True
        return payload

    def snapshot(self) -> dict:
        now = time.time()
        # Integrate current into consumed mAh. dt comes from the sampling
        # interval, so this must run on a steady heartbeat (the broadcast loop
        # calls it at 20 Hz).
        dt = 0.0 if self._prev_sample_ts is None else now - self._prev_sample_ts
        self._prev_sample_ts = now
        with self._lock:
            d = dict(self._data)
            batt_a = d.get("batt_a")
        if batt_a is not None and dt > 0:
            self.derived.on_current(batt_a, dt)

        with self._lock:
            d["t"] = round(now, 3)
            d["link_ok"] = (now - self._hb_time) < HEARTBEAT_TIMEOUT_S if self._hb_time else False
            if d["msg"] and now - self._msg_time > STATUSTEXT_TTL_S:
                d["msg"] = ""
            d["lat"] = round(d["lat"], 7)
            d["lon"] = round(d["lon"], 7)

        # --- derived values (computed outside the flight-data lock) -----
        dv = self.derived
        d["mode_code"] = derived.mode_code(d.get("mode"), d.get("custom_mode"))
        d["flight_time_s"] = _round(dv.flight_time_s(now), 1)
        d["total_dist_m"] = _round(dv.total_dist_m, 1)
        d["home_dist"] = _round(dv.home_distance(), 1)
        d["home_bearing"] = _round(dv.home_bearing(), 1)
        d["home_set"] = dv.home_lat is not None
        d["batt_cells"] = dv.batt_cells
        d["batt_cell_v"] = _round(dv.cell_voltage(d.get("batt_v")), 3)
        d["mah_used"] = _round(dv.mah_used, 1)
        per_km, per_h = dv.efficiency(now)
        d["mah_per_km"] = _round(per_km, 0)
        d["mah_per_hour"] = _round(per_h, 0)

        d["video"] = self.video_stats()
        d["sys"] = self._system.sample()
        d["wifi"] = d["sys"].get("wifi")
        d["data_kbps"] = self._data_rate()
        return d

    def _data_rate(self) -> float | None:
        """Incoming MAVLink data rate in kbps.

        Deliberately counts only what arrives from the flight controller, which
        is what "Data:" means in the Radio Link panel. Counting the WebSocket
        JSON we send back to browsers would inflate it to hundreds of kbps and
        tell you nothing about the telemetry link.
        """
        now = time.monotonic()
        total = self._bytes_in
        prev = getattr(self, "_rate_mark", None)
        if prev is None:
            self._rate_mark = (now, total)
            return None
        dt = now - prev[0]
        if dt < 2.0:
            return getattr(self, "_rate_kbps", None)
        rate = (total - prev[1]) * 8 / dt / 1000.0
        self._rate_mark = (now, total)
        self._rate_kbps = round(rate, 1)
        return self._rate_kbps

    @property
    def last_heartbeat(self) -> float:
        return self._hb_time


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------

class SourceStopped(Exception):
    pass

class MavlinkSource:
    """serial / udp / file sources: all go through pymavlink + the same handlers."""

    def __init__(self, state: TelemetryState, spec: str, baud: int = 57600,
                 log_raw: str | None = None) -> None:
        self.state = state
        self.spec = spec
        self.baud = baud
        self.log_raw = log_raw
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def _connect(self, device: str):
        kwargs = dict(source_system=255, source_component=0)
        if device.startswith("serial:") or device.startswith("/dev/"):
            # accepted forms: serial:/dev/ttyUSB0:57600  serial:/dev/ttyUSB0  /dev/ttyUSB0
            if device.startswith("serial:"):
                dev, _, maybe_baud = device[len("serial:"):].rpartition(":")
                if not maybe_baud.isdigit():
                    dev, maybe_baud = device[len("serial:"):], ""
                    if not dev:
                        raise ValueError(f"bad serial spec: {device}")
                else:
                    self.baud = int(maybe_baud)
            else:
                dev = device
            mav = mavutil.mavlink_connection(dev, baud=self.baud, **kwargs)
            if self.log_raw:
                # pymavlink only mirrors raw bytes for serial links
                mav.logfile_raw = open(self.log_raw, "ab")
            log.info("serial connected: %s @ %d baud", dev, self.baud)
            return mav, "serial"
        if device.startswith("file:"):
            path = device[len("file:"):]
            mav = mavutil.mavlink_connection(path, **kwargs)
            log.info("replaying log: %s", path)
            return mav, "file"
        if device.startswith("udp:") or device.startswith("udpin:"):
            mav = mavutil.mavlink_connection(device, **kwargs)
            log.info("UDP listening: %s", device)
            return mav, "udp"
        raise ValueError(f"unsupported source spec: {device}")

    def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            mav = None
            kind = "none"
            try:
                mav, kind = self._connect(self.spec)
                self.state.set_source(kind)
                backoff = 1.0
                self._read_loop(mav, kind)
                if kind == "file":
                    return  # replay finished; nothing to reconnect to
            except SourceStopped:
                return
            except Exception as e:  # noqa: BLE001 - never crash the service
                log.warning("source %s unavailable: %s (retry in %.0fs)",
                            self.spec, e, backoff)
                self.state.set_source("none")
            finally:
                if mav is not None:
                    try:
                        mav.close()
                    except Exception:
                        pass
            if self._stop.wait(backoff):
                return
            backoff = min(backoff * 1.5, 10.0)

    def _read_loop(self, mav, kind: str) -> None:
        last_hb = 0.0
        started = time.time()
        idle_since = time.time()
        while not self._stop.is_set():
            msg = mav.recv_match(blocking=True, timeout=0.5)
            now = time.time()
            if msg is None:
                if kind == "serial" and last_hb and now - last_hb > SERIAL_SILENCE_RECONNECT_S:
                    raise IOError("no heartbeat for %.0fs" % (now - last_hb))
                if kind == "file" and now - idle_since > 2.0:
                    log.info("end of log file")
                    return
                if kind == "udp" and not last_hb and now - started > 3600:
                    raise IOError("no data on UDP link for an hour")
                continue
            idle_since = now
            if msg.get_type() == "HEARTBEAT":
                last_hb = now
            self.state.handle(msg)


class SimSource:
    """Pure in-process fake data. No MAVLink involved at all.

    Generates lightweight objects shaped like pymavlink messages and feeds
    them through the very same TelemetryState.handle(), so the HUD code sees
    the same state machine as with real data.
    """

    def __init__(self, state: TelemetryState) -> None:
        self.state = state
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()
        # Leaving _hb_time set would keep link_ok true for the heartbeat
        # timeout after a switch, so the HUD would briefly claim whatever
        # replaced the simulator was already live.
        self.state.set_source("none")

    @staticmethod
    def _msg(mtype: str, **kw) -> types.SimpleNamespace:
        kw["_type"] = mtype
        m = types.SimpleNamespace(**kw)
        m.get_type = lambda: m._type
        return m

    def run(self) -> None:
        self.state.set_source("sim")
        log.info("simulation source active (in-process fake data)")
        t0 = time.time()
        next_hb = next_att = next_hud = next_gps = next_sys = 0.0
        next_text = t0 + 0.2
        while not self._stop.is_set():
            now = time.time()
            t = now - t0
            # gentle, realistic-ish motion
            roll = math.radians(12.0 * math.sin(t / 4.0) + 3.0 * math.sin(t / 1.3))
            pitch = math.radians(5.0 * math.sin(t / 6.0 + 0.7))
            yaw = math.radians((40.0 + t * 2.0) % 360.0)
            armed = int(t / 60) % 2 == 1
            mode = COPTER_MODES[[0, 5, 2][int(t / 45) % 3]]
            batt = max(10.5, 12.6 - t * 0.0035)
            if now >= next_hb:
                next_hb = now + 1.0
                self.state.handle(self._msg(
                    "HEARTBEAT",
                    base_mode=MAV_MODE_FLAG_SAFETY_ARMED if armed else 0,
                    custom_mode={v: k for k, v in COPTER_MODES.items()}[mode]))
            if now >= next_att:
                next_att = now + 0.05
                self.state.handle(self._msg(
                    "ATTITUDE", roll=roll, pitch=pitch, yaw=yaw,
                    rollspeed=0.0, pitchspeed=0.0, yawspeed=0.0))
            if now >= next_hud:
                next_hud = now + 0.2
                self.state.handle(self._msg(
                    "VFR_HUD", airspeed=8.0 + 2.0 * math.sin(t / 5),
                    groundspeed=9.0 + 2.0 * math.sin(t / 5),
                    alt=25.0 + 4.0 * math.sin(t / 7),
                    climb=2.0 * math.cos(t / 7), heading=int(math.degrees(yaw)) % 360,
                    throttle=45 + int(10 * math.sin(t / 3))))
            if now >= next_gps:
                next_gps = now + 0.2
                self.state.handle(self._msg(
                    "GLOBAL_POSITION_INT", lat=int(-35.363261e7), lon=int(149.165230e7),
                    alt=int(600000), relative_alt=int((25.0 + 4.0 * math.sin(t / 7)) * 1000),
                    vx=0, vy=0, vz=-50, hdg=int(math.degrees(yaw)) % 360))
                self.state.handle(self._msg(
                    "GPS_RAW_INT", fix_type=3, satellites_visible=11, eph=90,
                    lat=int(-35.363261e7), lon=int(149.165230e7), alt=584000))
            if now >= next_sys:
                next_sys = now + 1.0
                self.state.handle(self._msg(
                    "SYS_STATUS", voltage_battery=int(batt * 1000),
                    current_battery=int((9.5 + 2 * math.sin(t / 9)) * 100),
                    battery_remaining=max(0, int((batt - 10.5) / (12.6 - 10.5) * 100)),
                    load=180))
            if now >= next_text:
                next_text = now + 20.0
                self.state.handle(self._msg("STATUSTEXT", text="SIM: fake telemetry, no flight controller"))
            self._stop.wait(0.02)


class AutoSource:
    """Probe serial candidates; only accept a port that emits a HEARTBEAT."""

    def __init__(self, state: TelemetryState, baud: int, log_raw: str | None = None) -> None:
        self.state = state
        self.baud = baud
        self.log_raw = log_raw
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    @staticmethod
    def candidates() -> list[str]:
        out: list[str] = []
        for pat in SERIAL_CANDIDATES:
            out.extend(sorted(glob.glob(pat)))
        return out

    def _probe(self, dev: str, timeout: float = 4.0) -> bool:
        """Read only. Never writes to the port."""
        log.info("probing %s @ %d baud for MAVLink heartbeat", dev, self.baud)
        try:
            mav = mavutil.mavlink_connection(dev, baud=self.baud,
                                             source_system=255, source_component=0)
        except Exception as e:  # noqa: BLE001
            log.info("probe %s: cannot open (%s)", dev, e)
            return False
        try:
            deadline = time.time() + timeout
            while time.time() < deadline and not self._stop.is_set():
                msg = mav.recv_match(blocking=True, timeout=0.5)
                if msg is not None and msg.get_type() == "HEARTBEAT":
                    log.info("heartbeat found on %s", dev)
                    return True
            return False
        except Exception as e:  # noqa: BLE001
            log.info("probe %s: %s", dev, e)
            return False
        finally:
            try:
                mav.close()
            except Exception:
                pass

    def run(self) -> None:
        log.info("auto mode: waiting for a flight controller")
        while not self._stop.is_set():
            cands = self.candidates()
            if not cands:
                log.info("auto: no serial devices present; waiting")
            for dev in cands:
                if self._stop.is_set():
                    return
                if not self._probe(dev):
                    continue
                src = MavlinkSource(self.state, f"serial:{dev}", self.baud, self.log_raw)
                try:
                    mav, kind = src._connect(f"serial:{dev}")
                    self.state.set_source(kind)
                    src._read_loop(mav, kind)
                except Exception as e:  # noqa: BLE001
                    log.warning("auto: link on %s lost: %s", dev, e)
                finally:
                    self.state.set_source("none")
                break
            if self._stop.wait(AUTO_RETRY_S):
                return


# --------------------------------------------------------------------------
# HTTP + WebSocket
# --------------------------------------------------------------------------

class WebServer:
    def __init__(self, state: TelemetryState, host: str, port: int,
                 sources: "SourceManager | None" = None) -> None:
        self.state = state
        self.host = host
        self.port = port
        self.sources = sources
        self.clients: set[web.WebSocketResponse] = set()
        self.started_at = time.time()

    async def close_clients(self) -> None:
        """Close all WebSockets with a normal close frame.

        Called on shutdown. aiohttp's runner cleanup waits for handlers to
        return, so a single half-dead browser connection can hold the process
        open past systemd's stop timeout.
        """
        clients = list(self.clients)
        self.clients.clear()
        for ws in clients:
            try:
                await ws.close(code=WSCloseCode.GOING_AWAY, message=b"server shutdown")
            except Exception:  # noqa: BLE001
                pass

    # -- telemetry source (sim toggle) ----------------------------------
    def _source_env_path(self):
        return BASE_DIR / "telemetry" / "source.env"

    def _source_status(self) -> dict:
        st = (self.sources.status() if self.sources is not None
              else {"mode": "none", "configured": "none", "sim": False,
                    "sim_uptime_s": None})
        # A switch here is temporary. The boot default lives in config.env, and
        # is only changed by an explicit "persist" request, so a reboot never
        # comes back up in simulation by accident.
        return st

    async def api_source_get(self, request: web.Request) -> web.Response:
        return web.json_response(self._source_status())

    async def api_source_set(self, request: web.Request) -> web.Response:
        """Switch telemetry to the simulator or back to the real source.

        Body: {"mode": "sim"} or {"mode": "live"}
              {"mode": ..., "persist": true} also writes source.env, which
              overrides TELEM_SOURCE at the next boot.

        Switching to sim is refused while real MAVLink is actually flowing:
        pretending to be live is exactly what the HUD rules forbid, and mixing
        simulated values into a real flight would be worse.
        """
        if self.sources is None:
            return web.json_response(
                {"ok": False, "error": "source switching unavailable"}, status=503)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"ok": False, "error": "expected an object"}, status=400)

        mode = str(body.get("mode", "")).strip().lower()
        if mode not in ("sim", "live"):
            return web.json_response(
                {"ok": False, "error": "mode must be 'sim' or 'live'"}, status=400)

        if mode == "sim" and self.sources.mode not in ("sim", "none"):
            # Ask whether real data is actually flowing right now. Reading a
            # cached link_ok was wrong twice over: it lingers for the heartbeat
            # timeout after the source goes away, and it can be stale by a
            # frame. A live flight controller is the one thing we must never
            # paper over with fake numbers.
            if self.state.snapshot().get("link_ok"):
                return web.json_response({
                    "ok": False,
                    "error": "real MAVLink is live; disconnect the flight "
                             "controller or switch the service source before "
                             "simulating",
                }, status=409)

        try:
            applied = self.sources.set_mode(mode)
        except Exception as e:  # noqa: BLE001
            return web.json_response({"ok": False, "error": str(e)}, status=500)

        if body.get("persist"):
            try:
                p = self._source_env_path()
                p.write_text(
                    "# Overrides TELEM_SOURCE from config.env at boot.\n"
                    "# Written by the HUD source toggle; delete this file to\n"
                    "# fall back to TELEM_SOURCE.\n"
                    f"TELEM_SOURCE={'sim' if mode == 'sim' else self.sources.configured}\n")
            except OSError as e:
                return web.json_response(
                    {"ok": False, "error": f"switched but could not persist: {e}"},
                    status=500)

        status = self._source_status()
        status["ok"] = True
        status["applied"] = applied
        return web.json_response(status)

    # -- camera colour controls -----------------------------------------
    def _camera_env(self) -> dict[str, str]:
        """Current colour settings (key -> value) from telemetry/camera.env."""
        path = BASE_DIR / "telemetry" / "camera.env"
        out: dict[str, str] = {}
        if not path.exists():
            return out
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
        return out

    async def api_camera_get(self, request: web.Request) -> web.Response:
        return web.json_response({
            "settings": self._camera_env(),
            "controls": CAMERA_CONTROLS,
            "presets": CAMERA_PRESETS,
        })

    async def api_camera_set(self, request: web.Request) -> web.Response:
        """Persist new colour settings and restart the camera stream.

        Only whitelisted keys are accepted; values are validated by type.
        Requires a JSON body like {"saturation": "1.2", "awb": "cloudy"}.
        Send "reset": true to clear everything back to defaults.
        """
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"ok": False, "error": "expected an object"}, status=400)

        env_path = BASE_DIR / "telemetry" / "camera.env"
        if body.get("reset"):
            current: dict[str, str] = {}
        else:
            current = self._camera_env()

        # Remember where the encoder's frame counter stood, so we can tell a
        # real pipeline restart from an unchanged one. Only a live pipeline
        # counts: the camera runs on demand, so a stale stats file means no
        # viewer is connected and there is nothing to restart yet.
        prev_stats = self.state.video_stats()
        prev_frames = prev_stats.get("frames") if prev_stats.get("ok") else None

        for k, v in body.items():
            if k == "reset":
                continue
            if k not in CAMERA_CONTROLS:
                return web.json_response(
                    {"ok": False, "error": f"unknown control '{k}'"}, status=400)
            spec = CAMERA_CONTROLS[k]
            if v is None or str(v).strip() == "":
                current.pop(k, None)
                continue
            val = str(v).strip()
            allowed = spec.get("enum")
            if allowed and val not in allowed:
                return web.json_response(
                    {"ok": False, "error": f"{k}: allowed {allowed}"}, status=400)
            rng = spec.get("range")
            if rng:
                try:
                    num = float(val)
                except ValueError:
                    return web.json_response(
                        {"ok": False, "error": f"{k} must be numeric"}, status=400)
                if not (rng[0] <= num <= rng[1]):
                    return web.json_response(
                        {"ok": False, "error": f"{k} must be {rng[0]}..{rng[1]}"}, status=400)
            # map the friendly key to the env var camera_pipe.py reads
            current[spec["env"]] = val

        try:
            lines = ["# Camera colour / image settings - read by telemetry/camera_pipe.py",
                     "# Written by the HUD panel and telemetry/camera_ctl.py", ""]
            lines += [f"{k}={v}" for k, v in sorted(current.items())]
            tmp = env_path.with_suffix(".tmp")
            tmp.write_text("\n".join(lines) + "\n")
            tmp.replace(env_path)
        except OSError as e:
            return web.json_response({"ok": False, "error": f"write failed: {e}"}, status=500)

        applied, err = await self._await_camera_restart(prev_frames)
        return web.json_response({
            "ok": applied,
            "settings": current,
            "restarted": applied,
            "error": err,
        })

    async def _await_camera_restart(self, prev_frames: int | None,
                                    timeout: float = 6.0) -> tuple[bool, str | None]:
        """Confirm camera_pipe.py actually rebuilt the pipeline.

        The supervisor watches camera.env itself and restarts rpicam-vid +
        ffmpeg in place. go2rtc is NOT asked to restart: PATCH /api/streams
        with an unchanged source is a silent no-op, which previously made the
        colour panel report success while the old settings kept running.

        Success is proven by the pipeline's own frame counter resetting, which
        only happens when a new encoder process starts.

        When no viewer is connected, go2rtc has no exec producer running at all
        (the camera is started on demand), so there is nothing to restart yet.
        The settings are already saved and will be picked up when a viewer
        arrives. That is "applied", not a failure.
        """
        if prev_frames is None:
            # Pipeline was not running when we saved: nothing to restart.
            return True, None

        deadline = time.time() + timeout
        while time.time() < deadline:
            await asyncio.sleep(0.3)
            stats = self.state.video_stats()
            if not stats.get("ok"):
                continue
            frames = stats.get("frames")
            if frames is None:
                continue
            # A fresh pipeline starts counting from a low number again.
            if frames < prev_frames:
                log.info("camera pipeline restarted (frame counter %s -> %s)",
                         prev_frames, frames)
                return True, None
        return False, "camera did not restart; check the video pipeline"

    async def ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=15.0, max_msg_size=4096)
        await ws.prepare(request)
        self.clients.add(ws)
        peer = request.remote
        log.info("ws client connected: %s (total %d)", peer, len(self.clients))
        try:
            async for msg in ws:
                if msg.type == WSMsgType.ERROR:
                    break
                if msg.type == WSMsgType.TEXT:
                    # Application-level ping. The client echoes its own
                    # timestamp so it can compute round-trip time without
                    # assuming the Pi's clock matches the laptop's (the old
                    # snapshot-time subtraction produced nonsense whenever the
                    # two clocks drifted, including negative latencies).
                    try:
                        req = json.loads(msg.data)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(req, dict) and "ping" in req:
                        await ws.send_str(json.dumps({"pong": req["ping"]}))
        finally:
            self.clients.discard(ws)
            log.info("ws client gone: %s (total %d)", peer, len(self.clients))
        return ws

    async def api_state(self, request: web.Request) -> web.Response:
        return web.json_response(self.state.snapshot())

    async def index(self, request: web.Request) -> web.FileResponse:
        # No caching: the HUD is edited on the Pi and the browser must never
        # keep running stale JavaScript. A cached copy once re-saved
        # CAM_TUNING=default over the NoIR setting, silently bringing back the
        # magenta cast.
        return web.FileResponse(WEB_DIR / "index.html", headers={
            "Cache-Control": "no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        })

    async def health(self, request: web.Request) -> web.Response:
        """Liveness probe: proves the process is up even with no flight controller."""
        snap = self.state.snapshot()
        return web.json_response({
            "ok": True,
            "uptime_s": round(time.time() - self.started_at, 1),
            "link_ok": snap["link_ok"],
            "source": snap["source"],
        })

    async def broadcast_loop(self) -> None:
        interval = 1.0 / 20.0
        next_tick = time.monotonic()
        while True:
            next_tick += interval
            # Snapshot even with no clients: the derived values (flight timer,
            # mAh integration, distance) must keep advancing so a browser that
            # connects later sees a correct running total.
            payload = json.dumps(self.state.snapshot())
            if self.clients:
                self.state._bytes_out += len(payload) * len(self.clients)
                dead = []
                for ws in list(self.clients):
                    try:
                        await ws.send_str(payload)
                    except Exception:  # noqa: BLE001
                        dead.append(ws)
                for ws in dead:
                    self.clients.discard(ws)
            delay = next_tick - time.monotonic()
            if delay < 0:
                next_tick = time.monotonic()
                delay = 0
            await asyncio.sleep(delay)

    def build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/ws", self.ws_handler)
        app.router.add_get("/api/state", self.api_state)
        app.router.add_get("/health", self.health)
        app.router.add_get("/api/camera", self.api_camera_get)
        app.router.add_post("/api/camera", self.api_camera_set)
        app.router.add_get("/api/source", self.api_source_get)
        app.router.add_post("/api/source", self.api_source_set)
        app.router.add_get("/", self.index)
        app.router.add_static("/static/", WEB_DIR, show_index=False)
        return app


# --------------------------------------------------------------------------
# Config / main
# --------------------------------------------------------------------------

def load_config_env() -> dict[str, str]:
    """Tiny KEY=VALUE parser for telemetry/config.env (no shell evaluation)."""
    cfg: dict[str, str] = {}
    if not CONFIG_ENV.exists():
        return cfg
    for raw in CONFIG_ENV.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip().strip('"').strip("'")
        cfg[k.strip()] = v
    return cfg


def load_source_env() -> dict[str, str]:
    """Optional override written by the HUD's SIM toggle ("persist").

    Kept separate from config.env so a reboot default is easy to read and easy
    to undo (delete the file). Without it the boot source is TELEM_SOURCE.
    """
    cfg: dict[str, str] = {}
    if not SOURCE_ENV.exists():
        return cfg
    try:
        for raw in SOURCE_ENV.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            cfg[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        return {}
    return cfg


def parse_args(argv=None) -> argparse.Namespace:
    cfg = load_config_env()

    def env(name: str, default: str) -> str:
        return os.environ.get(name) or cfg.get(name) or default

    p = argparse.ArgumentParser(description="Drone vision telemetry service")
    p.add_argument("--source", default=None,
                   help="serial:DEV:BAUD | udp:HOST:PORT | sim | auto | file:LOG.tlog")
    p.add_argument("--device", default=env("TELEM_DEVICE", "/dev/ttyUSB0"),
                   help="serial device used in auto mode")
    p.add_argument("--baud", type=int, default=int(env("TELEM_BAUD", "57600")))
    p.add_argument("--host", default=env("TELEM_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(env("TELEM_PORT", "8080")))
    p.add_argument("--log-raw", default=env("TELEM_LOG_RAW", "") or None,
                   help="append incoming MAVLink to a .tlog file")
    p.add_argument("--print", dest="print_state", action="store_true",
                   help="print JSON state to stdout once per second")
    p.add_argument("--request-streams", action="store_true",
                   help="send REQUEST_DATA_STREAM (only with explicit approval)")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--cells", type=int, default=int(env("TELEM_CELLS", "0")),
                   help="battery cell count for per-cell voltage (APM does not "
                        "report it); 0 = unknown, HUD shows per-cell as ---")
    args = p.parse_args(argv)
    # Boot default: CLI flag, then the HUD's persisted override, then config.env.
    override = load_source_env()
    args.source = (args.source
                   or override.get("TELEM_SOURCE")
                   or env("TELEM_SOURCE", "auto"))
    return args


async def run(args) -> None:
    state = TelemetryState(batt_cells=args.cells)
    src = SourceManager(state, args)
    src.start()

    srv = WebServer(state, args.host, args.port, src)
    app = srv.build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, args.host, args.port)
    await site.start()
    log.info("web server on http://%s:%d  (source=%s)", args.host, args.port, args.source)

    tasks = [asyncio.create_task(srv.broadcast_loop())]
    if args.print_state:
        tasks.append(asyncio.create_task(printer(state)))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover
            pass

    await stop.wait()
    log.info("shutting down")

    async def _shutdown() -> None:
        try:
            src.stop_current()
        except Exception:  # noqa: BLE001
            pass
        for t in tasks:
            t.cancel()
        # Close WebSockets first: aiohttp's runner cleanup waits for handlers
        # to return, and a browser that vanished without a clean close leaves
        # one hanging. `ws.close()` itself waits for the peer's close frame,
        # so it needs its own bound as well.
        try:
            await asyncio.wait_for(srv.close_clients(), timeout=1.0)
        except asyncio.TimeoutError:
            log.warning("websocket clients did not close in time")
        try:
            await asyncio.wait_for(runner.cleanup(), timeout=1.0)
        except asyncio.TimeoutError:
            log.warning("aiohttp runner did not shut down in time")

    # Hard deadline. systemd sends SIGTERM and waits TimeoutStopSec (5s) before
    # SIGKILL; overshooting that looked like a clean stop in the journal while
    # actually being killed. Shutdown is best-effort: the port is released by
    # process exit either way.
    try:
        await asyncio.wait_for(_shutdown(), timeout=2.5)
    except asyncio.TimeoutError:
        log.warning("shutdown exceeded 2.5s")
    except Exception:  # noqa: BLE001
        log.exception("error during shutdown")

    # Source threads are daemon threads blocked in serial reads or probes, and
    # asyncio may still hold abandoned tasks. exit() is the only reliable way
    # to leave on time. Flush logging first; nothing else needs to reach disk.
    logging.shutdown()
    os._exit(0)


def _guarded_run(src) -> None:
    try:
        src.run()
    except Exception:  # noqa: BLE001 - a source bug must not kill the service
        log.exception("telemetry source crashed")


class SourceManager:
    """Owns whichever telemetry source is currently running.

    This exists so the browser can switch the simulator on and off at runtime
    (the SIM toggle on the HUD) without restarting the service and without a
    second `sim_mavlink.py` process on the Pi.

    Switching always stops the old source and starts the new one in a fresh
    thread. `MavlinkSource` and `SimSource` both block in their own loop, so
    they are never asked to run twice.
    """

    def __init__(self, state: TelemetryState, args) -> None:
        self.state = state
        self.args = args
        self._thread: threading.Thread | None = None
        self._src: object | None = None
        self._lock = threading.Lock()
        # What the service was configured to use. Toggling sim off returns to
        # this, so an install with TELEM_SOURCE=auto goes back to waiting for a
        # real flight controller instead of stopping dead.
        self.configured = args.source
        self.mode = args.source        # "auto" | "serial" | "udp" | "sim" | "file"
        self._sim_started_at: float | None = None

    # -- lifecycle ------------------------------------------------------
    def start(self) -> None:
        self._start(self.configured)

    def _make(self, spec: str):
        """Build a source object and the mode name to report."""
        if spec in ("sim", "sim:"):
            return SimSource(self.state), "sim"
        if spec == "auto":
            return AutoSource(self.state, self.args.baud, self.args.log_raw), "auto"
        src = MavlinkSource(self.state, spec, self.args.baud, self.args.log_raw)
        for prefix in ("serial", "udp", "udpin", "file"):
            if spec.startswith(prefix):
                return src, "udp" if prefix == "udpin" else prefix
        return src, "none"

    def _start(self, spec: str) -> None:
        src, mode = self._make(spec)
        with self._lock:
            self._src = src
            self.mode = mode
            self._sim_started_at = time.time() if mode == "sim" else None
            if mode == "sim":
                # SimSource sets this itself, but doing it here means the very
                # first snapshot after the switch is already labelled SIM.
                self.state.set_source("sim")
            self._thread = threading.Thread(
                target=_guarded_run, args=(src,),
                name=f"telem-{mode}", daemon=True)
            self._thread.start()
        log.info("telemetry source started: %s (%s)", spec, mode)

    def stop_current(self) -> None:
        with self._lock:
            src = self._src
            self._src = None
        if src is not None and hasattr(src, "stop"):
            try:
                src.stop()
            except Exception:  # noqa: BLE001
                pass

    def set_mode(self, mode: str) -> str:
        """Switch between the configured source and the simulator.

        Only two targets are meaningful from the browser: "sim" and "live".
        Anything else is rejected.
        """
        if mode == "sim":
            if self.mode != "sim":
                self.stop_current()
                self._start("sim")
            return "sim"
        if mode in ("live", "auto", "real"):
            if self.mode == "sim":
                # Return to the configured source. MavlinkSource retries on its
                # own, so an unplugged flight controller simply shows NO FC.
                self.stop_current()
                self._start(self.configured)
            return self.mode
        raise ValueError(f"unknown telemetry mode: {mode}")

    # -- introspection --------------------------------------------------
    def status(self) -> dict:
        return {
            "mode": self.mode,
            "configured": self.configured,
            "sim": self.mode == "sim",
            "sim_uptime_s": (round(time.time() - self._sim_started_at, 1)
                             if self._sim_started_at else None),
        }


async def printer(state: TelemetryState) -> None:
    while True:
        print(json.dumps(state.snapshot()), flush=True)
        await asyncio.sleep(1.0)


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

    if args.request_streams:
        log.warning("--request-streams is enabled: the service may send "
                    "REQUEST_DATA_STREAM to the flight controller")

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
