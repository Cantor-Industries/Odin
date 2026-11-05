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
System and link metrics for the HUD's top-right strip and Radio Link panel.

Reads /proc, /sys and two small commands. No psutil, no extra packages: the Pi
already has everything, and this project deliberately keeps its dependency
list to pymavlink + aiohttp.

Every function returns None for anything it cannot determine. The spec is
explicit that unknown values must render "---", never 0.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time

IFACE = "wlan0"


def active_wifi_iface(preferred: str = IFACE) -> str:
    """The interface that actually carries WiFi on this machine.

    Hardcoding wlan0 is wrong often enough to matter: a Pi can name its
    interface wlp1s0, and a USB WiFi dongle appears as wlan1. Falls back to
    the preferred name when nothing better is found, so error messages still
    mention something concrete.
    """
    if os.path.exists(f"/sys/class/net/{preferred}/wireless"):
        return preferred
    try:
        for name in sorted(os.listdir("/sys/class/net")):
            if os.path.exists(f"/sys/class/net/{name}/wireless"):
                return name
    except OSError:
        pass
    return preferred



# --------------------------------------------------------------------------
# CPU / SoC
# --------------------------------------------------------------------------

def _read(path: str) -> str | None:
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return None


def cpu_times() -> tuple[int, int] | None:
    """(busy, total) jiffies from /proc/stat, or None."""
    text = _read("/proc/stat")
    if not text:
        return None
    line = text.splitlines()[0]
    parts = line.split()
    if not parts or parts[0] != "cpu":
        return None
    try:
        values = [int(v) for v in parts[1:]]
    except ValueError:
        return None
    if len(values) < 4:
        return None
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    total = sum(values)
    return total - idle, total


class CpuSampler:
    """Turns /proc/stat counters into a percentage between calls."""

    def __init__(self) -> None:
        self._prev: tuple[int, int] | None = cpu_times()

    def percent(self) -> float | None:
        cur = cpu_times()
        if not cur or not self._prev:
            self._prev = cur
            return None
        d_busy, d_total = cur[0] - self._prev[0], cur[1] - self._prev[1]
        self._prev = cur
        if d_total <= 0:
            return None
        return max(0.0, min(100.0, 100.0 * d_busy / d_total))


def cpu_mhz() -> float | None:
    """Current CPU clock. Pi 5 exposes it per-core; use cpu0."""
    text = _read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
    if text:
        try:
            return float(text.strip()) / 1000.0        # kHz -> MHz
        except ValueError:
            pass
    text = _read("/proc/cpuinfo")
    if text:
        m = re.search(r"^cpu MHz\s*:\s*([\d.]+)", text, re.M)
        if m:
            return float(m.group(1))
    return None


def soc_temp_c() -> float | None:
    """SoC temperature, preferring the value the firmware reports."""
    if shutil.which("vcgencmd"):
        try:
            out = subprocess.run(["vcgencmd", "measure_temp"],
                                 capture_output=True, text=True, timeout=3)
            m = re.search(r"temp=([\d.]+)", out.stdout)
            if m:
                return float(m.group(1))
        except (OSError, subprocess.SubprocessError):
            pass
    text = _read("/sys/class/thermal/thermal_zone0/temp")
    if text:
        try:
            return float(text.strip()) / 1000.0
        except ValueError:
            pass
    return None


def throttled() -> dict | None:
    """Decode `vcgencmd get_throttled`.

    The bits have two halves: the low 16 bits are "is happening right now",
    the high 16 are "has happened since boot". The HUD only warns about the
    live ones, but we expose both so the panel can be honest.
    """
    if not shutil.which("vcgencmd"):
        return None
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"],
                             capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"0x([0-9a-fA-F]+)", out.stdout)
    if not m:
        return None
    raw = int(m.group(1), 16)
    live, occurred = raw & 0xFFFF, (raw >> 16) & 0xFFFF
    return {
        "raw": raw,
        "live": {
            "under_voltage": bool(live & 0x1),
            "freq_capped": bool(live & 0x2),
            "throttled": bool(live & 0x4),
            "soft_temp_limit": bool(live & 0x8),
        },
        "occurred": {
            "under_voltage": bool(occurred & 0x1),
            "freq_capped": bool(occurred & 0x2),
            "throttled": bool(occurred & 0x4),
            "soft_temp_limit": bool(occurred & 0x8),
        },
    }


# --------------------------------------------------------------------------
# WiFi
# --------------------------------------------------------------------------

def wifi(iface: str = IFACE) -> dict:
    """Signal, quality, link rate and frequency for the video interface.

    `iw dev X link` is authoritative but slower; /proc/net/wireless is the
    fallback so the HUD still shows something if iw is missing.
    """
    out: dict = {
        "iface": iface,
        "signal_dbm": None,
        "quality_pct": None,
        "rate_mbps": None,
        "freq_mhz": None,
        "ssid": None,
    }

    if shutil.which("iw"):
        try:
            res = subprocess.run(["iw", "dev", iface, "link"],
                                 capture_output=True, text=True, timeout=3)
            text = res.stdout
            m = re.search(r"signal:\s*(-?\d+)\s*dBm", text)
            if m:
                out["signal_dbm"] = int(m.group(1))
            m = re.search(r"freq:\s*([\d.]+)", text)
            if m:
                out["freq_mhz"] = int(float(m.group(1)))
            # Prefer the receive rate: that is what carries our video.
            m = re.search(r"rx bitrate:\s*([\d.]+)\s*MBit/s", text)
            if not m:
                m = re.search(r"tx bitrate:\s*([\d.]+)\s*MBit/s", text)
            if m:
                out["rate_mbps"] = float(m.group(1))
            m = re.search(r"SSID:\s*(.+)", text)
            if m:
                out["ssid"] = m.group(1).strip()
        except (OSError, subprocess.SubprocessError):
            pass

    # /proc/net/wireless gives link quality even when iw is unavailable.
    text = _read("/proc/net/wireless")
    if text:
        for line in text.splitlines():
            if line.strip().startswith(iface + ":"):
                nums = line.split(":", 1)[1].split()
                try:
                    quality = float(nums[1].rstrip("."))
                    level = float(nums[2].rstrip("."))
                except (IndexError, ValueError):
                    continue
                if out["quality_pct"] is None:
                    # cfg80211 reports 0..70 as a rough quality value.
                    out["quality_pct"] = round(max(0.0, min(100.0, quality / 70.0 * 100.0)), 1)
                if out["signal_dbm"] is None:
                    out["signal_dbm"] = int(level)

    if out["signal_dbm"] is not None and out["quality_pct"] is None:
        # dBm is negative; -50 is excellent, -90 is unusable.
        q = (out["signal_dbm"] + 90) / 40.0 * 100.0
        out["quality_pct"] = round(max(0.0, min(100.0, q)), 1)

    return out


# --------------------------------------------------------------------------
# Sample cache
# --------------------------------------------------------------------------

class SystemSampler:
    """Collects the cheap stuff often and the shell-outs less often."""

    def __init__(self, slow_interval: float = 2.0) -> None:
        self.cpu = CpuSampler()
        self.slow_interval = slow_interval
        self._slow_at = 0.0
        self._slow: dict = {}

    def sample(self) -> dict:
        now = time.monotonic()
        if now - self._slow_at >= self.slow_interval or not self._slow:
            self._slow_at = now
            self._slow = {
                "cpu_mhz": cpu_mhz(),
                "cpu_temp_c": soc_temp_c(),
                "throttled": throttled(),
                "wifi": wifi(active_wifi_iface()),
            }
        payload = dict(self._slow)
        payload["cpu_pct"] = self.cpu.percent()
        return payload
