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
Camera pipeline helper: rpicam-vid (MJPEG) -> ffmpeg (H.264) -> stdout.

go2rtc runs this through its `exec:` source and reads the H.264 bitstream
from our stdout.

Why not a shell pipeline in go2rtc.yaml?
    go2rtc terminates the process it spawned (a `sh -c` wrapper) but not the
    grandchildren, so rpicam-vid and ffmpeg survived as orphans holding the
    CSI camera and the next viewer got a dead stream. This helper owns both
    children and stops them together.

Latency notes (measured on this Pi 5 at 720p30):
    * DO NOT use `-fflags nobuffer` on the MJPEG input. It makes ffmpeg's
      demuxer hand out partial packets: it dropped 92 of 142 frames (35%) in
      testing and added ~2 s of buffering instead of removing it.
    * The MJPEG demuxer defaults to 25 fps and would resample, so the input
      rate must be set explicitly with `-framerate`.
    * `-fps_mode passthrough` (an OUTPUT option) keeps every frame and stops
      ffmpeg duplicating frames to hit a nominal rate.
    * x264 `-tune zerolatency` already disables B-frames and lookahead.

Colour controls come from environment variables so they can be tuned without
editing code (see telemetry/config.env). Unset means "leave it to libcamera".
"""

from __future__ import annotations

import fcntl
import glob
import json
import os
import re
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
FFMPEG = os.path.join(BASE, "bin", "ffmpeg")
LOCK_PATH = "/tmp/odin-camera.lock"
CAMERA_ENV = os.path.join(HERE, "camera.env")

# Live pipeline telemetry, rewritten once a second and read by server.py to
# feed the HUD. Lives in /tmp because it is runtime state, not configuration.
STATS_PATH = os.environ.get("VIDEO_STATS_PATH", "/tmp/odin-video.json")

# Keys that came from camera.env (as opposed to systemd or the real
# environment). reload_camera_env() uses this to know which stale keys to drop
# when a line is deleted from the file.
_FILE_KEYS: set[str] = set()


def load_camera_env() -> None:
    """Read telemetry/camera.env into os.environ.

    Real environment variables win, so systemd Environment=/EnvironmentFile=
    can still override the file. This file is what camera_ctl.py rewrites at
    runtime: the supervisor watches its mtime and restarts only rpicam-vid
    when it changes, leaving ffmpeg (and its pipe) untouched.
    """
    global _FILE_KEYS
    if not os.path.exists(CAMERA_ENV):
        return
    try:
        with open(CAMERA_ENV) as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
                    if k.startswith("CAM_"):
                        _FILE_KEYS.add(k)
    except OSError as e:
        print(f"camera_pipe: cannot read {CAMERA_ENV}: {e}", file=sys.stderr)


def camera_env_mtime() -> float:
    try:
        return os.stat(CAMERA_ENV).st_mtime
    except OSError:
        return 0.0


def reload_camera_env() -> None:
    """Re-read camera.env over the current process environment.

    Unlike load_camera_env(), this REPLACES values that are already set, which
    is the whole point of a live colour change.

    Keys that were set by a PREVIOUS read of this file are removed first, so
    deleting a line takes effect. Without that, removing CAM_TUNING=default left
    the old value in os.environ and it kept overriding the sensor's default
    tuning file, silently bringing back the NoIR magenta cast. Values injected
    by systemd/EnvironmentFile are left alone because they are not in
    _FILE_KEYS.
    """
    file_keys: set[str] = set()
    try:
        with open(CAMERA_ENV) as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if not k or not k.startswith("CAM_"):
                    continue
                os.environ[k] = v      # replaced, not merely defaulted
                file_keys.add(k)
    except OSError as e:
        print(f"camera_pipe: cannot re-read {CAMERA_ENV}: {e}", file=sys.stderr)
        return

    global _FILE_KEYS
    for stale in _FILE_KEYS - file_keys:
        os.environ.pop(stale, None)
    _FILE_KEYS = file_keys


load_camera_env()

WIDTH = int(os.environ.get("CAM_WIDTH", "1280"))
HEIGHT = int(os.environ.get("CAM_HEIGHT", "720"))
FPS = int(os.environ.get("CAM_FPS", "30"))
QUALITY = os.environ.get("CAM_JPEG_QUALITY", "80")
BITRATE = os.environ.get("CAM_BITRATE", "2500k")
X264_PRESET = os.environ.get("CAM_X264_PRESET", "ultrafast")

# --- colour / image controls ---------------------------------------------
# name in config.env -> rpicam-vid flag
COLOUR_OPTS = {
    "CAM_BRIGHTNESS": "--brightness",   # -1.0 .. 1.0   (0 = default)
    "CAM_CONTRAST": "--contrast",       # 0.0 .. ~2.0   (1 = default)
    "CAM_SATURATION": "--saturation",   # 0 = greyscale, 1 = default
    "CAM_SHARPNESS": "--sharpness",     # 0 .. ~2.0     (1 = default)
    "CAM_EV": "--ev",                   # exposure compensation, e.g. 0.5
    "CAM_AWB": "--awb",                 # auto|incandescent|tungsten|fluorescent|indoor|daylight|cloudy
    "CAM_AWBGAINS": "--awbgains",       # R,B e.g. 1.5,1.2 (disables auto AWB)
    "CAM_METERING": "--metering",       # centre|spot|average
    "CAM_EXPOSURE": "--exposure",       # normal|sport
    "CAM_DENOISE": "--denoise",         # auto|off|cdn_off|cdn_fast|cdn_hq
    "CAM_HFLIP": "--hflip",             # flag, set to 1 to enable
    "CAM_VFLIP": "--vflip",             # flag, set to 1 to enable
    "CAM_ROTATION": "--rotation",       # 0 or 180
    "CAM_TUNING": "--tuning-file",      # sensor tuning JSON, see below
}

# Tuning files live under libcamera's IPA directory. Which subdirectory holds
# the RPi sensor tunings depends on the libcamera build: Pi 5 (PiSP) uses
# rpi/pisp, older Pis use rpi/vc4. Both are checked so the same code works on
# anything from a Pi 3 to a Pi 5.
TUNING_DIRS = (
    "/usr/share/libcamera/ipa/rpi/pisp",
    "/usr/share/libcamera/ipa/rpi/vc4",
    "/usr/share/libcamera/ipa/rpi",
)


def detect_sensor() -> str | None:
    """Name of the attached camera sensor, e.g. "imx219", or None.

    Tries rpicam-vid first because that is the same tool that will use the
    tuning file, then falls back to the I2C device name. Deliberately quiet:
    this runs while the pipeline is starting and a missing camera is normal.
    """
    try:
        out = subprocess.run(["rpicam-vid", "--list-cameras"],
                             capture_output=True, text=True, timeout=5).stdout
        m = re.search(r"^\s*\d+\s*:\s*([a-z0-9]+)", out, re.MULTILINE)
        if m:
            return m.group(1)
    except (OSError, subprocess.SubprocessError):
        pass

    # /sys/bus/i2c/devices/<bus>-<addr>/name holds the sensor's I2C name.
    try:
        for name_path in glob.glob("/sys/bus/i2c/devices/*/name"):
            try:
                with open(name_path) as fh:
                    name = fh.read().strip()
            except OSError:
                continue
            if re.match(r"^(imx|ov)\d", name):
                return name
    except OSError:
        pass
    return None


def tuning_file(sensor: str | None, noir: bool = True) -> str | None:
    """The libcamera tuning JSON for `sensor`, or None to use libcamera's own.

    A NoIR module (no IR-cut filter) needs the matching *_noir.json: with the
    stock file auto-white-balance pushes red and blue and the picture comes out
    magenta (measured +36.7 -> -2.5 magenta excess on an imx219 NoIR). Every
    other module should get libcamera's default, which is why this returns
    None rather than guessing a filename that may not exist.

    Returning None is important: rpicam-vid only *warns* about a missing
    tuning file and then produces bad colour, so a wrong path fails silently.
    """
    if not sensor:
        return None
    candidate = f"{sensor}_noir.json" if noir else f"{sensor}.json"
    for d in TUNING_DIRS:
        path = os.path.join(d, candidate)
        if os.path.exists(path):
            return path
    return None


# Whether this module is NoIR is a property of the hardware, not something to
# guess. The installer writes CAM_NOIR=1|0 into camera.env after detection;
# the default here keeps the reference Pi's behaviour (NoIR) when unset.
def camera_is_noir() -> bool:
    raw = os.environ.get("CAM_NOIR", "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    return True


# These take no value; presence of a truthy env var adds the flag.
FLAG_OPTS = {"CAM_HFLIP", "CAM_VFLIP"}


def log(msg: str) -> None:
    print(f"camera_pipe: {msg}", file=sys.stderr, flush=True)


def build_rpicam_cmd() -> list[str]:
    # Read the frame-geometry settings at call time, not at import time: the
    # supervisor re-runs this after a camera.env change, and fps/quality are
    # exposed in the colour panel. Using module globals here meant a change was
    # saved, reported as applied, and then silently ignored.
    width = os.environ.get("CAM_WIDTH", str(WIDTH))
    height = os.environ.get("CAM_HEIGHT", str(HEIGHT))
    fps = os.environ.get("CAM_FPS", str(FPS))
    quality = os.environ.get("CAM_JPEG_QUALITY", QUALITY)

    cmd = [
        "rpicam-vid",
        "-t", "0",
        "-n",
        "--width", str(width),
        "--height", str(height),
        "--framerate", str(fps),
        "--codec", "mjpeg",
        "--quality", str(quality),
        # emit data immediately instead of filling libcamera's buffers first
        "--flush",
    ]

    for env_name, flag in COLOUR_OPTS.items():
        raw = os.environ.get(env_name, "").strip()
        if not raw:
            # Tuning is special: unset means "pick the right file for the
            # sensor actually attached", not "leave it to libcamera". An
            # explicit "none"/"default" opts out.
            if env_name == "CAM_TUNING":
                raw = tuning_file(detect_sensor(), noir=camera_is_noir()) or ""
                if not raw:
                    continue
            else:
                continue
        if env_name in FLAG_OPTS:
            if raw.lower() not in ("0", "false", "no", "off"):
                cmd.append(flag)
            continue
        if env_name == "CAM_TUNING":
            # Accept the friendly names as well as a real path. "default" means
            # "no --tuning-file", i.e. libcamera's own choice for the sensor.
            if raw.lower() in ("none", "default", "off"):
                continue
            if raw.lower() in ("auto", "noir", "on"):
                # Resolve against the sensor that is actually attached, so the
                # same install works on an imx219, imx477, imx708 or ov5647.
                raw = tuning_file(detect_sensor(), noir=camera_is_noir()) or ""
                if not raw:
                    continue        # libcamera's default is the safe answer
            if not os.path.exists(raw):
                # rpicam-vid only warns about a missing tuning file and then
                # carries on with bad colour, so never pass a path that is not
                # there. Falling back to libcamera's choice is visible and
                # recoverable; a wrong sensor's tuning file is neither.
                log(f"CAM_TUNING={raw!r} is not a file; using libcamera's default")
                continue
        cmd += [flag, raw]

    cmd += ["-o", "-"]
    return cmd


def _half_rate(spec: str) -> str:
    """Halve an ffmpeg rate string such as '2500k' or '3M'."""
    text = spec.strip()
    mult = 1
    if text and text[-1].lower() == "k":
        mult, text = 1000, text[:-1]
    elif text and text[-1].lower() == "m":
        mult, text = 1_000_000, text[:-1]
    try:
        return str(int(int(float(text) * mult / 2)))
    except ValueError:
        return spec


def build_ffmpeg_cmd(progress_fd: int | None = None) -> list[str]:
    # VBV buffer size. It used to be 1x bitrate = a full SECOND of rate-control
    # slack, which x264 may spend by holding frames. Half that keeps quality
    # stable while halving the worst-case delay.
    #
    # Read at call time (not import time) so a camera.env edit is honoured: the
    # supervisor rebuilds this command after every colour change.
    bitrate = os.environ.get("CAM_BITRATE", BITRATE)
    preset = os.environ.get("CAM_X264_PRESET", X264_PRESET)
    fps = os.environ.get("CAM_FPS", str(FPS))
    bufsize = os.environ.get("CAM_X264_BUFSIZE", "") or _half_rate(bitrate)
    progress = (["-progress", f"pipe:{progress_fd}", "-nostats"]
                if progress_fd is not None else [])
    return [
        FFMPEG,
        "-hide_banner", "-loglevel", "error",
        # Input rate must be explicit: the mjpeg demuxer assumes 25 fps.
        "-framerate", str(fps),
        "-f", "mjpeg", "-i", "-",
        "-c:v", "libx264",
        "-preset", preset, "-tune", "zerolatency",
        "-g", str(fps), "-keyint_min", str(fps), "-sc_threshold", "0",
        "-pix_fmt", "yuv420p",
        "-b:v", bitrate, "-maxrate", bitrate, "-bufsize", bufsize,
        # Flush the encoder's AVIO buffer after every packet. Without this,
        # ffmpeg buffers up to 32 KiB (~100 ms at 2.5 Mbit/s) before writing
        # to the pipe, which delays every frame on its way to go2rtc.
        "-flush_packets", "1",
        # Belt and braces: no B-frames, no lookahead, no scene-cut reordering.
        # NOTE: do NOT add aud=1 here. It would make frame counting easy, but
        # go2rtc's H.264 parser then coalesces a whole GOP into a single packet
        # (measured: 1 pkt/s of 175 KB instead of 30 pkt/s of 5.8 KB), which
        # collapses the WebRTC stream to 1 fps. Frame count is taken from
        # ffmpeg's -progress output instead.
        "-x264-params", "bframes=0:rc-lookahead=0:sync-lookahead=0:scenecut=0",
        # Machine-readable frame/bitrate counters once a second, on a pipe we
        # own, keeping stdout purely for the H.264 bitstream.
        *progress,
        # output option: keep every frame, no dup/drop to a nominal rate
        "-fps_mode", "passthrough",
        "-f", "h264", "-",
    ]


class _RelayStats:
    """Measure what the Pi-side pipeline adds, without a per-frame protocol.

    We cannot see the sensor's capture timestamp from here (rpicam-vid hands us
    a bare MJPEG stream with no timestamps), so this does not claim a true
    glass-to-lens figure. What it measures precisely, and what actually drives
    perceived lag:

      * encoded_fps - frames/s leaving x264, from ffmpeg's -progress counters
      * actual_kbps - bytes/s leaving x264, same source
      * backlog_ms  - bytes sitting unread in the pipe between ffmpeg and us,
                      converted to milliseconds at the current bitrate. This is
                      real queueing delay, and it is the number that balloons
                      when the Pi cannot keep up.

    Frame counting deliberately does NOT parse NAL units: x264 ultrafast emits
    several slices per picture, so slice counting overstates the rate ~3x, and
    adding AUD to fix that makes go2rtc collapse the stream to 1 fps. ffmpeg
    already knows the exact frame count, so we ask it.
    """

    def __init__(self, host_bitrate: str = "2500k") -> None:
        self.host_bitrate = host_bitrate
        self._frames = 0            # cumulative, from ffmpeg
        self._bytes = 0             # cumulative, from ffmpeg
        self._last_frames = 0
        self._last_bytes = 0
        self._last_write = 0.0
        self._window_start = time.monotonic()
        self._fps = 0.0
        self._kbps = 0.0
        self._backlog_bytes = 0
        self._backlog_ms = 0.0
        self._prog_buf = b""

    def _bits_per_sec(self) -> float:
        spec = self.host_bitrate.strip().lower()
        try:
            if spec.endswith("k"):
                return float(spec[:-1]) * 1000
            if spec.endswith("m"):
                return float(spec[:-1]) * 1_000_000
            return float(spec)
        except ValueError:
            return 2_500_000.0

    def read_progress(self, chunk: bytes) -> None:
        """Parse ffmpeg -progress key=value lines (emitted about once a second)."""
        self._prog_buf += chunk
        # progress blocks are separated by progress=continue / progress=end
        while b"\n" in self._prog_buf:
            line, _, rest = self._prog_buf.partition(b"\n")
            self._prog_buf = rest
            key, sep, val = line.strip().partition(b"=")
            if not sep:
                continue
            val = val.strip()
            try:
                if key == b"frame":
                    self._frames = int(val)
                elif key == b"total_size":
                    self._bytes = int(val)
            except ValueError:
                pass

    def sample_backlog(self, fd: int) -> None:
        """Bytes queued but not yet read from ffmpeg's stdout pipe."""
        try:
            import array
            import fcntl
            import termios

            buf = array.array("i", [0])
            fcntl.ioctl(fd, termios.FIONREAD, buf, True)
            self._backlog_bytes = int(buf[0])
            bps = self._bits_per_sec() / 8.0
            self._backlog_ms = (self._backlog_bytes / bps * 1000.0) if bps else 0.0
        except Exception:  # noqa: BLE001
            self._backlog_bytes = 0
            self._backlog_ms = 0.0

    def maybe_write(self, path: str, meta: dict, pending_fd: int | None = None,
                    interval: float = 1.0) -> None:
        now = time.monotonic()
        if now - self._last_write < interval:
            return
        if pending_fd is not None:
            self.sample_backlog(pending_fd)

        dt = now - self._window_start
        dframes = self._frames - self._last_frames
        dbytes = self._bytes - self._last_bytes
        if dt >= 0.25:
            # Only publish a rate once the window is long enough to mean
            # something. The first sample after a pipeline restart can arrive
            # microseconds after the window opened, and dividing by that near-
            # zero dt produced absurd readings like "138367 fps".
            self._fps = dframes / dt
            self._kbps = dbytes * 8 / dt / 1000.0
            self._last_frames = self._frames
            self._last_bytes = self._bytes
            self._window_start = now
        self._last_write = now

        payload = dict(meta)
        payload.update({
            "ok": True,
            "t": round(time.time(), 3),
            "encoded_fps": round(self._fps, 1),
            "actual_kbps": round(self._kbps),
            "frames": self._frames,
            "backlog_bytes": self._backlog_bytes,
            "backlog_ms": round(self._backlog_ms, 1),
        })
        tmp = f"{path}.tmp"
        try:
            with open(tmp, "w") as fh:
                json.dump(payload, fh)
            os.replace(tmp, path)
        except OSError:
            pass


def _spawn_pipeline() -> tuple[subprocess.Popen, subprocess.Popen, int]:
    """Start rpicam-vid + ffmpeg, wired together. Returns (cam, enc, prog_r)."""
    cam = subprocess.Popen(build_rpicam_cmd(), stdout=subprocess.PIPE,
                           stderr=sys.stderr, start_new_session=True)
    prog_r, prog_w = os.pipe()
    try:
        enc = subprocess.Popen(build_ffmpeg_cmd(progress_fd=prog_w),
                               stdin=cam.stdout, stdout=subprocess.PIPE,
                               stderr=sys.stderr, pass_fds=(prog_w,),
                               start_new_session=True)
    except OSError:
        os.close(prog_r)
        os.close(prog_w)
        _kill(cam)
        raise
    os.close(prog_w)      # only ffmpeg should hold the write end
    cam.stdout.close()    # ffmpeg owns the read end now
    return cam, enc, prog_r


def main() -> int:
    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("another instance owns the camera; exiting")
        return 0

    if not os.path.exists(FFMPEG):
        log(f"ffmpeg not found at {FFMPEG}")
        return 1

    log(f"{WIDTH}x{HEIGHT}@{FPS} mjpeg q{QUALITY} -> x264 {BITRATE} "
        f"preset={X264_PRESET}")
    colours = {k: os.environ[k] for k in COLOUR_OPTS if os.environ.get(k, "").strip()}
    if colours:
        log("colour: " + " ".join(f"{k.replace('CAM_','').lower()}={v}"
                                  for k, v in colours.items()))

    try:
        cam, enc, prog_r = _spawn_pipeline()
    except FileNotFoundError:
        log("rpicam-vid not found")
        return 1
    except OSError as e:
        log(f"cannot start pipeline: {e}")
        return 1

    stopping = {"flag": False}

    def stop(_signum=None, _frame=None):
        if stopping["flag"]:
            return
        stopping["flag"] = True
        _kill(enc)
        _kill(cam)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, stop)

    rc = 0
    out = sys.stdout.buffer
    relay = _RelayStats(host_bitrate=BITRATE)
    meta = {
        "width": WIDTH, "height": HEIGHT, "fps": FPS,
        "preset": X264_PRESET, "bitrate": BITRATE,
    }

    # camera.env is written by the colour panel and by camera_ctl.py. go2rtc's
    # PATCH /api/streams will NOT restart an exec producer whose source string
    # is unchanged, so asking it to "restart" for a colour change was a silent
    # no-op. The supervisor watches the file itself and rebuilds both children
    # in place, which keeps go2rtc's pipe (our stdout) open the whole time.
    env_mtime = camera_env_mtime()

    try:
        while True:
            # read1(), NOT read(n).
            #
            # BufferedReader.read(n) blocks until it has n bytes or EOF. At
            # ~2.5 Mbit/s a 64 KiB read() therefore waits ~210 ms (6-7 frames)
            # before forwarding anything, and that delay is added to every
            # frame end to end - the single largest avoidable latency in this
            # pipeline after the browser's jitter buffer.
            #
            # read1() returns as soon as ANY data is available (one raw read
            # from the pipe), so each encoded frame is forwarded immediately.
            # 64 KiB is now only an upper bound, not a minimum.
            chunk = enc.stdout.read1(65536)
            if not chunk:
                break
            out.write(chunk)
            out.flush()

            # Drain ffmpeg's -progress pipe without blocking. It only produces
            # output about once a second, so poll it alongside the video data.
            prog = os.read(prog_r, 4096) if _readable(prog_r) else b""
            if prog:
                relay.read_progress(prog)

            relay.maybe_write(STATS_PATH, meta, pending_fd=enc.stdout.fileno())

            # Live colour change: rebuild the pipeline, keeping stdout open.
            if camera_env_mtime() != env_mtime:
                time.sleep(0.2)              # let the writer finish
                reload_camera_env()
                env_mtime = camera_env_mtime()
                log("camera.env changed -> restarting pipeline: "
                    + " ".join(build_rpicam_cmd()[1:]))
                old_cam, old_enc, old_prog = cam, enc, prog_r
                try:
                    cam, enc, prog_r = _spawn_pipeline()
                except Exception as e:  # noqa: BLE001
                    log(f"pipeline restart failed ({e}); keeping old one")
                    cam, enc, prog_r = old_cam, old_enc, old_prog
                else:
                    _kill(old_enc)
                    _kill(old_cam)
                    for p in (old_enc, old_cam):
                        try:
                            p.wait(timeout=3)
                        except Exception:  # noqa: BLE001
                            _kill(p, force=True)
                    try:
                        os.close(old_prog)
                    except OSError:
                        pass
                    relay = _RelayStats(host_bitrate=BITRATE)
    except (BrokenPipeError, OSError):
        log("output pipe closed by go2rtc")
    finally:
        stop()

    for p in (enc, cam):
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            _kill(p, force=True)
    try:
        os.close(prog_r)
    except OSError:
        pass

    cam_rc, enc_rc = cam.returncode, enc.returncode
    log(f"stopped (rpicam={cam_rc} ffmpeg={enc_rc})")
    if enc_rc not in (0, None, -15, -9):
        rc = enc_rc
    return rc


def _readable(fd: int, timeout: float = 0.0) -> bool:
    """True if `fd` has data ready. Non-blocking poll; never consumes data."""
    try:
        import select
        return bool(select.select([fd], [], [], timeout)[0])
    except (OSError, ValueError):
        return False


def _kill(proc, force: bool = False) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL if force else signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
