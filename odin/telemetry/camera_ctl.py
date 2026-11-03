#!/usr/bin/env python3
"""
Camera colour / image controls, applied at runtime.

The camera pipeline runs inside go2rtc's `exec:` source, and go2rtc starts
it with an environment inherited from the go2rtc service, NOT from this
process. So "changing a colour setting" really means:

    1. write the new value to telemetry/config.env   (persists across boots)
    2. restart the `drone` stream in go2rtc, so the helper re-reads it

go2rtc restarts a stream when its source is re-set via
    PATCH /api/streams?name=drone&src=<source>
The helper picks up the new env because go2rtc re-execs it... except a
service-level env change requires restarting go2rtc itself, which would drop
the WebRTC video. To avoid that, this tool ALSO supports a live path:

    1. write settings to telemetry/camera.env (read by camera_pipe.py at start)
    2. PATCH the go2rtc stream -> helper exits, go2rtc re-runs it
    3. the new helper reads camera.env and applies the new colour settings

So the colour settings live in their own file, and only the camera process is
restarted. Video comes back in about a second. The HUD is unaffected.

Usage:
    python camera_ctl.py show
    python camera_ctl.py set saturation=1.4 contrast=1.1
    python camera_ctl.py set awb=cloudy ev=0.3
    python camera_ctl.py reset            # back to libcamera defaults
    python camera_ctl.py presets          # list named presets
    python camera_ctl.py preset punchy
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CAMERA_ENV = os.path.join(HERE, "camera.env")
GO2RTC = os.environ.get("GO2RTC_URL", "http://127.0.0.1:1984")
STREAM = os.environ.get("GO2RTC_STREAM", "drone")
VIDEO_STATS_PATH = os.environ.get("VIDEO_STATS_PATH",
                                  "/tmp/odin-video.json")

# key -> (rpicam-vid flag, validator, help)
CONTROLS = {
    "brightness": ("--brightness", (-1.0, 1.0), "overall brightness, -1..1 (0 default)"),
    "contrast":   ("--contrast",   (0.0, 4.0),  "contrast, 1.0 = normal"),
    "saturation": ("--saturation", (0.0, 4.0),  "colour saturation, 0 = greyscale, 1 = normal"),
    "sharpness":  ("--sharpness",  (0.0, 4.0),  "sharpening, 1.0 = normal"),
    "ev":         ("--ev",        (-10.0, 10.0), "exposure compensation in EV"),
    "awb":        ("--awb",        None,        "auto|incandescent|tungsten|fluorescent|indoor|daylight|cloudy"),
    "awbgains":   ("--awbgains",   None,        "manual white balance as R,B (disables auto AWB)"),
    "metering":   ("--metering",   None,        "centre|spot|average"),
    "exposure":   ("--exposure",   None,        "normal|sport"),
    "denoise":    ("--denoise",    None,        "auto|off|cdn_off|cdn_fast|cdn_hq"),
    "hflip":      ("--hflip",      None,        "1 to mirror horizontally"),
    "vflip":      ("--vflip",      None,        "1 to flip vertically"),
    "rotation":   ("--rotation",   None,        "0 or 180"),
    "tuning":     ("--tuning-file", None,       "sensor tuning JSON; 'noir' for NoIR modules, 'default'/'none' for IR-cut"),
    "width":      ("--width",      (160, 3280), "capture width"),
    "height":     ("--height",     (120, 2464), "capture height"),
    "fps":        ("--framerate",  (1, 90),     "capture framerate"),
    "quality":    ("--quality",    (1, 100),    "MJPEG quality (higher = more CPU/bandwidth)"),
    "bitrate":    ("--bitrate",    None,        "output bitrate, e.g. 2500k (passed to ffmpeg)"),
}

ENUMS = {
    "awb": {"auto", "incandescent", "tungsten", "fluorescent", "indoor",
            "daylight", "cloudy", "custom"},
    "metering": {"centre", "spot", "average", "custom"},
    "exposure": {"normal", "sport"},
    "denoise": {"auto", "off", "cdn_off", "cdn_fast", "cdn_hq"},
    "tuning": {"noir", "default", "none"},
}

# Friendly tuning names -> libcamera tuning files for this sensor.
TUNING_FILES = {
    "noir": "/usr/share/libcamera/ipa/rpi/pisp/imx219_noir.json",
}

PRESETS = {
    # name: settings
    "default": {},
    "punchy": {"saturation": "1.4", "contrast": "1.15", "sharpness": "1.2"},
    "flat":   {"saturation": "0.85", "contrast": "0.95", "sharpness": "1.0"},
    "grey":   {"saturation": "0.0"},
    "overcast": {"awb": "cloudy", "brightness": "0.08", "contrast": "1.05"},
    "lowlight": {"ev": "1.0", "denoise": "cdn_hq", "fps": "20"},
    "sunny":  {"awb": "daylight", "ev": "-0.3", "contrast": "1.1"},
}

# name -> env var read by camera_pipe.py
ENV_NAMES = {
    "brightness": "CAM_BRIGHTNESS", "contrast": "CAM_CONTRAST",
    "saturation": "CAM_SATURATION", "sharpness": "CAM_SHARPNESS",
    "ev": "CAM_EV", "awb": "CAM_AWB", "awbgains": "CAM_AWBGAINS",
    "metering": "CAM_METERING", "exposure": "CAM_EXPOSURE",
    "denoise": "CAM_DENOISE", "hflip": "CAM_HFLIP", "vflip": "CAM_VFLIP",
    "rotation": "CAM_ROTATION", "width": "CAM_WIDTH", "height": "CAM_HEIGHT",
    "fps": "CAM_FPS", "quality": "CAM_JPEG_QUALITY", "bitrate": "CAM_BITRATE",
    "tuning": "CAM_TUNING",
}


# --------------------------------------------------------------------------
# camera.env read/write
# --------------------------------------------------------------------------

def read_env() -> dict[str, str]:
    """Read camera.env. Keys are the env-var names (CAM_*)."""
    if not os.path.exists(CAMERA_ENV):
        return {}
    out = {}
    with open(CAMERA_ENV) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def to_friendly(env_key: str) -> str:
    """CAM_SATURATION -> saturation (for display)."""
    rev = {v: k for k, v in ENV_NAMES.items()}
    return rev.get(env_key, env_key)


def write_env(values: dict[str, str]) -> None:
    """Write camera.env atomically. Values must already be CAM_* keyed."""
    lines = [
        "# Camera colour / image settings - read by telemetry/camera_pipe.py",
        "# Edit with: ./telemetry/camera_ctl.py set saturation=1.3",
        "# Changes apply to the live stream immediately (the camera restarts).",
        "",
    ]
    for key, val in sorted(values.items()):
        lines.append(f"{key}={val}")
    tmp = CAMERA_ENV + ".tmp"
    with open(tmp, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    os.replace(tmp, CAMERA_ENV)


def validate(name: str, value: str) -> str:
    if name not in CONTROLS:
        raise SystemExit(f"unknown control '{name}'. Known: {', '.join(sorted(CONTROLS))}")
    if name in ENUMS and value not in ENUMS[name]:
        raise SystemExit(f"invalid value '{value}' for {name}. "
                         f"Allowed: {', '.join(sorted(ENUMS[name]))}")
    if name == "tuning":
        # accept the friendly names, or any existing tuning JSON path
        if value in TUNING_FILES:
            return TUNING_FILES[value]
        if value in ("default", "none", "off"):
            return value
        if not os.path.exists(value):
            raise SystemExit(f"tuning file not found: {value}")
        return value
    lo_hi = CONTROLS[name][1]
    if lo_hi:
        try:
            f = float(value)
        except ValueError:
            raise SystemExit(f"{name} must be a number") from None
        if not (lo_hi[0] <= f <= lo_hi[1]):
            raise SystemExit(f"{name} must be between {lo_hi[0]} and {lo_hi[1]}")
    return value


# --------------------------------------------------------------------------
# live apply
# --------------------------------------------------------------------------

def stream_source() -> str:
    """Read the current source of the drone stream from go2rtc (diagnostics)."""
    with urllib.request.urlopen(f"{GO2RTC}/api/streams", timeout=5) as r:
        data = json.load(r)
    info = data.get(STREAM) or {}
    producers = info.get("producers") or []
    for p in producers:
        # go2rtc exposes the configured URI as "source" at runtime.
        url = p.get("source") or p.get("url") or ""
        if url.startswith("exec:"):
            return url[5:]
    raise SystemExit(f"no exec source found for stream '{STREAM}' in go2rtc")


def wait_for_restart(prev_frames, timeout: float = 6.0) -> bool:
    """Wait until camera_pipe.py has rebuilt the pipeline.

    camera.env is the only interface: the supervisor watches its mtime and
    restarts rpicam-vid + ffmpeg in place. go2rtc is deliberately NOT asked to
    restart, because PATCHing a stream with an unchanged source is a silent
    no-op (it made this command report success while the old settings ran on).

    A real restart is detected by the encoder's frame counter resetting.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.3)
        try:
            with open(VIDEO_STATS_PATH) as fh:
                stats = json.load(fh)
        except (OSError, ValueError):
            continue
        if not stats.get("ok"):
            continue
        frames = stats.get("frames")
        if frames is None:
            continue
        if prev_frames is None or frames < prev_frames:
            return True
    return False


def current_frames():
    try:
        with open(VIDEO_STATS_PATH) as fh:
            return json.load(fh).get("frames")
    except (OSError, ValueError):
        return None


def restart_stream() -> None:
    """Apply pending camera.env changes live and wait until they take effect."""
    prev = current_frames()
    if wait_for_restart(prev):
        print("camera restarted - changes are live")
    else:
        print("warning: camera did not report a restart; "
              "is the video pipeline running?")


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_show() -> None:
    cur = read_env()
    print(f"camera.env: {CAMERA_ENV}")
    if not cur:
        print("  (empty - libcamera defaults)")
    for k in sorted(cur):
        print(f"  {to_friendly(k):11s} = {cur[k]}")
    missing = [k for k in cur if k not in set(ENV_NAMES.values())]
    if missing:
        print(f"  warning: unknown keys {missing}")


def cmd_set(pairs: list[str], apply: bool) -> None:
    cur = read_env()
    changed = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"expected key=value, got '{pair}'")
        k, _, v = pair.partition("=")
        k = k.strip().lower()
        v = validate(k, v.strip())
        env_key = ENV_NAMES[k]          # store as the env var the pipeline reads
        cur[env_key] = v
        changed[k] = v
    write_env(cur)
    for k, v in changed.items():
        print(f"set {k} = {v}")
    if apply:
        restart_stream()
    else:
        print("saved; run 'camera_ctl.py apply' to take effect on the live stream")


def cmd_reset(apply: bool) -> None:
    write_env({})
    print("cleared all colour settings (libcamera defaults)")
    if apply:
        restart_stream()


def cmd_presets() -> None:
    for name, settings in PRESETS.items():
        body = ", ".join(f"{k}={v}" for k, v in settings.items()) or "(defaults)"
        print(f"  {name:10s} {body}")


def cmd_preset(name: str, apply: bool) -> None:
    if name not in PRESETS:
        raise SystemExit(f"unknown preset '{name}'. Try: {', '.join(PRESETS)}")
    # a preset replaces everything, so start from empty
    write_env({ENV_NAMES[k]: v for k, v in PRESETS[name].items()})
    print(f"applied preset '{name}'")
    if apply:
        restart_stream()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Camera colour / image controls")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("show", help="show current settings")

    sp = sub.add_parser("set", help="set one or more controls")
    sp.add_argument("pairs", nargs="+", metavar="key=value")
    sp.add_argument("--no-apply", action="store_true",
                    help="save only; do not restart the stream")

    rp = sub.add_parser("reset", help="clear all settings")
    rp.add_argument("--no-apply", action="store_true")

    sub.add_parser("presets", help="list presets")

    pp = sub.add_parser("preset", help="apply a named preset")
    pp.add_argument("name")
    pp.add_argument("--no-apply", action="store_true")

    sub.add_parser("apply", help="restart the stream so saved settings take effect")

    sub.add_parser("controls", help="list available controls")

    args = p.parse_args(argv)

    if args.cmd == "show":
        cmd_show()
    elif args.cmd == "set":
        cmd_set(args.pairs, not args.no_apply)
    elif args.cmd == "reset":
        cmd_reset(not args.no_apply)
    elif args.cmd == "presets":
        cmd_presets()
    elif args.cmd == "preset":
        cmd_preset(args.name, not args.no_apply)
    elif args.cmd == "apply":
        restart_stream()
    elif args.cmd == "controls":
        for k, (_flag, rng, help_) in sorted(CONTROLS.items()):
            r = f"  [{rng[0]}..{rng[1]}]" if rng else ""
            print(f"  {k:11s} {help_}{r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
