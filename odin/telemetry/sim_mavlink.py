#!/usr/bin/env python3
# Copyright 2026 Cantor Industries
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Fake APM 2.8 / ArduCopter: sends MAVLink 1 telemetry over UDP.

Purpose: exercise the real parsing path (pymavlink -> state -> WebSocket ->
HUD) without a flight controller attached.

    # terminal 1
    python sim_mavlink.py --out udpout:127.0.0.1:14550
    # terminal 2
    python server.py --source udp:127.0.0.1:14550

Sends: HEARTBEAT, ATTITUDE, VFR_HUD, GLOBAL_POSITION_INT, GPS_RAW_INT,
SYS_STATUS and occasional STATUSTEXT. Modes, arming and battery evolve
slowly so the HUD visibly responds.

READ-ONLY by design in the other direction: the simulator never listens
for commands and never acts on them. It only transmits.
"""

from __future__ import annotations

import argparse
import math
import time

from pymavlink import mavutil

mavutil.mavlink.WIRE_PROTOCOL_VERSION = "1.0"

COPTER_MODES = {
    "STABILIZE": 0, "ACRO": 1, "ALT_HOLD": 2, "AUTO": 3, "GUIDED": 4,
    "LOITER": 5, "RTL": 6, "CIRCLE": 7, "LAND": 9, "POSHOLD": 16,
}

MAV_TYPE_QUADROTOR = 2
MAV_AUTOPILOT_ARDUPILOTMEGA = 3
MAV_STATE_STANDBY = 3
MAV_STATE_ACTIVE = 4

MAV_MODE_FLAG_CUSTOM_MODE_ENABLED = 1
MAV_MODE_FLAG_SAFETY_ARMED = 128


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Fake APM MAVLink telemetry source")
    p.add_argument("--out", default="udpout:127.0.0.1:14550",
                   help="udpout:HOST:PORT (default: udpout:127.0.0.1:14550)")
    p.add_argument("--rate", type=float, default=20.0, help="attitude rate, Hz")
    p.add_argument("--duration", type=float, default=0.0,
                   help="stop after N seconds (0 = run forever)")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    print(f"sim_mavlink: sending MAVLink 1 to {args.out} at {args.rate:g} Hz", flush=True)

    mav = mavutil.mavlink_connection(
        args.out, source_system=1, source_component=1,
    )

    t0 = time.time()
    next_att = 0.0
    next_fast = 0.0
    next_slow = 0.0
    next_text = t0 + 0.5
    text_idx = 0
    dist_travelled = 0.0
    last_pos_t = t0

    texts = [
        (6, "SIM: starting up - fake telemetry, no flight controller"),
        (6, "SIM: this is simulated MAVLink"),
        (6, "SIM: APM:Copter V3.2.1 (simulated)"),
        (4, "SIM: battery low warning"),
        (6, "SIM: EKF2 IMU0 is using GPS"),
        (6, "SIM: arm/disarm toggles every 60s"),
    ]

    while True:
        now = time.time()
        t = now - t0
        if args.duration and t > args.duration:
            break

        # --- slow-changing logical state -------------------------------
        # armed for the middle third of each 120 s cycle
        armed = (int(t / 60) % 2) == 1
        mode_names = ["STABILIZE", "LOITER", "ALT_HOLD", "GUIDED"]
        mode = mode_names[int(t / 30) % len(mode_names)]
        custom_mode = COPTER_MODES[mode]
        batt_v = max(10.4, 12.6 - 0.004 * t)
        batt_pct = max(0, int((batt_v - 10.4) / (12.6 - 10.4) * 100))

        # --- flight dynamics ------------------------------------------
        roll = math.radians(14.0 * math.sin(t / 5.0) + 2.5 * math.sin(t / 1.1))
        pitch = math.radians(6.0 * math.sin(t / 7.0 + 0.6))
        yaw = math.radians((55.0 + t * 3.0) % 360.0)
        heading = int(math.degrees(yaw)) % 360
        alt = 25.0 + 6.0 * math.sin(t / 9.0)
        climb = 6.0 / 9.0 * math.cos(t / 9.0)
        groundspeed = 9.0 + 2.0 * math.sin(t / 6.0)
        airspeed = groundspeed * 0.95
        throttle = 0 if not armed else 45 + int(12 * math.sin(t / 4.0))

        # A slowly moving position, so distance-flown / distance-to-home /
        # efficiency have something real to integrate. ~4 m/s along a gentle
        # curve around the launch point.
        if armed:
            dist_travelled += groundspeed * (now - last_pos_t)
        last_pos_t = now
        # Latitude/longitude offsets from the launch point. 1e-5 deg latitude
        # is about 1.1 m, so this is a wander of a few hundred metres.
        off_n = math.sin(t / 40.0) * 300.0        # metres north
        off_e = (1.0 - math.cos(t / 40.0)) * 220.0  # metres east
        lat = -35.363261 + off_n / 111320.0
        lon = 149.165230 + off_e / (111320.0 * math.cos(math.radians(-35.36)))

        if now >= next_att:
            next_att = now + 1.0 / max(1.0, args.rate)
            mav.mav.attitude_send(
                int(t * 1000), roll, pitch, yaw,
                0.0, 0.0, 0.0, force_mavlink1=True,
            )

        if now >= next_fast:
            next_fast = now + 0.1
            mav.mav.vfr_hud_send(
                airspeed, groundspeed, heading, throttle, alt, climb,
                force_mavlink1=True,
            )
            mav.mav.global_position_int_send(
                int(t * 1000),
                int(lat * 1e7), int(lon * 1e7),
                int((584.0 + alt) * 1000), int(alt * 1000),
                0, 0, int(-climb * 100), heading,
                force_mavlink1=True,
            )

        if now >= next_slow:
            next_slow = now + 0.5
            base_mode = MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
            if armed:
                base_mode |= MAV_MODE_FLAG_SAFETY_ARMED
            mav.mav.heartbeat_send(
                MAV_TYPE_QUADROTOR,
                MAV_AUTOPILOT_ARDUPILOTMEGA,
                base_mode,
                custom_mode,
                MAV_STATE_ACTIVE if armed else MAV_STATE_STANDBY,
                3,
                force_mavlink1=True,
            )
            mav.mav.sys_status_send(
                0, 0, 0,
                180,
                int(batt_v * 1000),
                int((9.5 + 2.0 * math.sin(t / 8.0)) * 100),
                batt_pct,
                0, 0, 0, 0, 0, 0,
                force_mavlink1=True,
            )
            mav.mav.gps_raw_int_send(
                int(t * 1e6), 3,
                int(lat * 1e7), int(lon * 1e7),
                int((584.0 + alt) * 1000),
                70,      # eph  = HDOP * 100  -> HDOP 0.70
                120,     # epv
                90,      # velocity, cm/s
                heading, # course over ground, cdeg
                11,      # satellites visible
                force_mavlink1=True,
            )

        if now >= next_text:
            next_text = now + 7.0
            sev, txt = texts[text_idx % len(texts)]
            text_idx += 1
            mav.mav.statustext_send(sev, txt.encode("utf-8")[:50], force_mavlink1=True)

        if args.verbose and int(t * 10) % 50 == 0:
            print(f"  t={t:6.1f} mode={mode:10s} armed={armed!s:5s} "
                  f"batt={batt_v:5.2f}V {batt_pct:3d}%", flush=True)

        time.sleep(1.0 / max(5.0, args.rate * 2))

    print("sim_mavlink: done", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
