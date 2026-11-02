# Drone Vision: go2rtc (video) + Python telemetry/OSD service

Handoff document for the coding agent running on the Raspberry Pi 5.
Read the whole file before changing anything.

## 1. Goal

Turn this Raspberry Pi 5 (mounted on a drone) into a companion computer that:

1. Reads MAVLink telemetry from an ArduPilot flight controller over serial.
2. Captures live video from a Raspberry Pi camera module (CSI).
3. Serves **low-latency video over WebRTC** plus a **live OSD/HUD** over WiFi, viewable in a **laptop browser** at a single URL.

Priority: reuse open-source components. Write as little custom code as possible. Python for all custom code.

## 2. Hardware and constraints (facts to respect)

| Item | Detail |
|---|---|
| Computer | Raspberry Pi 5, 64-bit Raspberry Pi OS (verify with `uname -m`, expect `aarch64`) |
| Camera | Raspberry Pi CSI camera module. Model unknown, detect with `rpicam-hello --list-cameras` |
| Flight controller | **APM 2.8** (ArduCopter 3.2.x era). Speaks **MAVLink 1**, default telemetry baud **57600** |
| FC link | Serial. Either Pi GPIO UART (`/dev/ttyAMA0`) or a USB-serial adapter (`/dev/ttyUSB0`). **Ask the user which one** |
| Network | Laptop and Pi on the same WiFi (LAN or Pi hotspot) |
| **FC connection status** | **The Pi may NOT be physically connected to the flight controller right now.** Build and test everything without it (see section 6b), so that plugging it in later is a config change, not a code change |

Hard constraints:

- **Must work with no flight controller attached.** The telemetry service must start, stay running, and serve the page and a simulated or "NO TELEMETRY" HUD when no serial device exists. A missing FC is a normal state, not an error.
- **The Pi 5 has no hardware H.264 encoder.** Video must be encoded in software (x264, ultrafast/zerolatency) or sent as MJPEG. Keep resolution at about 720p30 to leave CPU for future vision work.
- **APM 2.8 serial is 5V logic. Pi GPIO is 3.3V.** Never wire APM TX directly to a Pi GPIO RX without a level shifter. If the user is using GPIO UART, remind them. A USB-serial adapter avoids the issue.
- **MAVLink 1 on old firmware:** the FC will not stream data unless stream rates are requested (via `SR0_*` params or `REQUEST_DATA_STREAM`). See section 6.
- **Telemetry is READ-ONLY.** Do not send arm, disarm, mode-change, parameter-write or any command to the flight controller. The only message the service may send is a data-stream rate request (and only if the user approves).
- **Do not change the Pi's network configuration** (WiFi, hotspot, firewall, SSH) without explicit user approval. The user is connected over SSH and could lock themselves out.

## 3. Architecture

```
Pi camera ──► rpicam-vid / ffmpeg (x264) ──► go2rtc ──WebRTC──► laptop browser (video)
                                               (:1984 UI/API, :8555 WebRTC)

APM 2.8 ──serial──► Python service (pymavlink) ──WebSocket JSON──► same browser page (HUD canvas)
                         (:8080 serves web page + /ws)
```

Why the OSD is drawn in the browser rather than burned into the video:

- Avoids a second encode pass on the Pi (no hardware encoder).
- HUD stays sharp and can be toggled.
- Video stays clean for future computer-vision work on the Pi.

Tradeoff: the OSD is not in recordings or in other players (e.g. VLC). If the user later wants it burned in, see section 10.

## 4. Components

| Component | Role | Notes |
|---|---|---|
| **go2rtc** | Video server: ingests camera pipeline, serves WebRTC | Single binary, use the `linux_arm64` release from `github.com/AlexxIT/go2rtc`. Web UI/API on 1984, WebRTC on 8555 (TCP/UDP) |
| **rpicam-vid** / **ffmpeg** | Camera capture and encode | Launched by go2rtc via its `exec:` source |
| **pymavlink** | MAVLink parsing | `pip install pymavlink` (use a venv) |
| **aiohttp** (or FastAPI + uvicorn) | Serves static page and WebSocket | Pick one, keep it small |
| **systemd** | Auto-start on boot | One unit per service |

## 5. Video pipeline (go2rtc)

1. Install go2rtc to `~/Odin/odin/bin/go2rtc`, `chmod +x`.
2. Verify the camera works first: `rpicam-hello --list-cameras`, then `rpicam-hello -t 5000`.
3. Create `~/Odin/odin/go2rtc.yaml`. Starting sketch (**verify and adjust, the exec line is the least certain part**):

```yaml
api:
  listen: ":1984"
webrtc:
  listen: ":8555"
streams:
  drone:
    # Option A: let rpicam-vid produce H.264 in software and pipe it in.
    # Check `rpicam-vid --help` on this Pi for the exact codec/libav flags.
    - exec:rpicam-vid -t 0 -n --width 1280 --height 720 --framerate 30 --codec libav --libav-format h264 -o -
    # Option B (fallback): MJPEG from rpicam-vid, transcode with ffmpeg
    # - exec:sh -c "rpicam-vid -t 0 -n --codec mjpeg --width 1280 --height 720 --framerate 30 -o - | ffmpeg -f mjpeg -i - -c:v libx264 -preset ultrafast -tune zerolatency -g 30 -f rtsp {output}"
```

4. Consult the go2rtc README ("Source: exec") and `rpicam-vid --help` rather than trusting this sketch. Test the pipeline by hand in a terminal before putting it in go2rtc.
5. Start go2rtc, open `http://<pi-ip>:1984` from the laptop, and confirm the `drone` stream plays in **WebRTC** mode.

Targets: glass-to-glass latency under about 300 ms, Pi CPU under about 50% while streaming.

## 6. Telemetry service (Python)

File: `~/Odin/odin/telemetry/server.py`

Responsibilities:

1. Connect with pymavlink: `mavutil.mavlink_connection(DEVICE, baud=BAUD)`. Make `DEVICE` and `BAUD` env vars or CLI args (typical values `/dev/ttyUSB0`, `57600`). The source is chosen as described in **section 6b** (default `auto`). The FC may not be attached, so wait for the first HEARTBEAT without blocking the web server or WebSocket.
2. Make sure data is streaming. Preferred: the user sets `SR0_EXTRA1`, `SR0_EXTRA2`, `SR0_EXTRA3`, `SR0_POSITION`, `SR0_RAW_SENS`, `SR0_EXT_STAT` (about 4 to 10 Hz) in Mission Planner. Fallback, only with user approval: send `request_data_stream_send` for the needed streams.
3. Maintain a state dict from these messages:
   - `ATTITUDE`: roll, pitch, yaw (radians, convert to degrees)
   - `VFR_HUD`: airspeed, groundspeed, altitude, climb, heading, throttle
   - `GLOBAL_POSITION_INT`: lat, lon, relative altitude
   - `GPS_RAW_INT`: fix type, satellites
   - `SYS_STATUS`: battery voltage, current, remaining %
   - `HEARTBEAT`: armed flag (`base_mode & MAV_MODE_FLAG_SAFETY_ARMED`) and flight mode (decode `custom_mode` using the ArduCopter mode table; pymavlink's `mavutil.mode_mapping()` can help)
   - `STATUSTEXT`: last message, shown briefly
4. Run an asyncio loop that broadcasts the state as JSON over a WebSocket (`/ws`) at about 20 Hz to all clients. Include a `link_ok` flag, false if no heartbeat for more than 3 s.
5. Serve the static page in `~/Odin/odin/web/` at `/` on port 8080.
6. Handle serial disconnects and reconnect automatically. Never crash the process on parse errors.
7. Log to stdout (journald).

JSON shape (keep stable):

```json
{
  "t": 1730000000.0,
  "link_ok": true,
  "source": "serial",
  "armed": false,
  "mode": "STABILIZE",
  "roll": 1.2, "pitch": -0.4, "yaw": 87.0,
  "heading": 87, "alt": 12.3, "groundspeed": 3.1, "airspeed": 3.0, "climb": 0.2,
  "throttle": 42,
  "batt_v": 11.8, "batt_a": 9.5, "batt_pct": 74,
  "gps_fix": 3, "sats": 10, "lat": 0.0, "lon": 0.0,
  "msg": ""
}
```

## 6b. Telemetry sources: laying the foundation without the FC

Design the service around a **pluggable telemetry source**, so the same parsing, state, WebSocket and HUD code runs whether or not the flight controller is attached.

Select the source with `--source` (or env `TELEM_SOURCE`):

| Source | Example | Use |
|---|---|---|
| `serial` | `serial:/dev/ttyUSB0:57600` | Real FC over serial (final setup) |
| `udp` | `udp:127.0.0.1:14550` | MAVLink over UDP: ArduPilot SITL, MAVProxy, or the simulator below |
| `sim` | `sim` | Pure in-process fake data (no MAVLink at all), for HUD/UI work |
| `auto` (default) | `auto` | Use the serial device if it exists and a heartbeat arrives; otherwise stay in waiting mode and retry every few seconds |

Requirements:

1. **Same code path for real and fake data.** `serial` and `udp` both go through pymavlink and the same message handlers. Only the connection string differs. This way the real parsing logic is exercised before the drone is ever connected.
2. **Waiting mode, not crashing.** If the configured device is missing or silent, log one line, set `link_ok: false`, keep the WebSocket and web page running, and retry with backoff. When the FC appears, switch to live data automatically with no restart.
3. **Visible state in the HUD.** Add a `source` field to the JSON (`"serial"`, `"udp"`, `"sim"`, `"none"`). The HUD shows a small badge: `LIVE`, `SIM` or `NO FC`. A simulated feed must never be mistaken for live data. When `source` is `sim`, show an obvious "SIMULATED" label.
4. **MAVLink simulator (`telemetry/sim_mavlink.py`).** A small script using pymavlink that acts as a fake APM: sends MAVLink 1 `HEARTBEAT`, `ATTITUDE`, `VFR_HUD`, `GLOBAL_POSITION_INT`, `GPS_RAW_INT`, `SYS_STATUS` and an occasional `STATUSTEXT` to a UDP port, with smoothly varying roll/pitch/yaw, a slowly draining battery, a mode change and an arm/disarm toggle. Run with `python sim_mavlink.py --out udpout:127.0.0.1:14550` and the service with `--source udp:127.0.0.1:14550`. This tests the full path: parse, state, WebSocket, HUD.
5. **Optional, better fidelity:** ArduPilot SITL (`sim_vehicle.py -v ArduCopter --out=udp:127.0.0.1:14550`) if installing it is reasonable on the Pi. Treat as optional. The small simulator above is enough.
6. **Keep serial config in one place.** `~/Odin/odin/telemetry/config.env` holds `TELEM_SOURCE`, device and baud. Plugging in the FC later should mean editing that file (and, if needed, the stream-rate params on the FC), then `systemctl restart drone-telemetry`.
7. **Don't assume the serial device name.** When no FC is attached, `/dev/ttyUSB0` will not exist, and `/dev/ttyAMA0` may exist without anything on it. In `auto` mode, list candidates (`/dev/ttyUSB*`, `/dev/ttyACM*`, `/dev/ttyAMA0`) and only count one as the FC if a MAVLink HEARTBEAT is received on it.
8. **Raw logging hook (nice to have):** an option `--log-raw file.tlog` that writes the incoming MAVLink stream (pymavlink supports `.tlog`). Later, real flight data can be replayed through the HUD without the drone (`--source` pointing at a replay of the tlog). This is optional, build it only after the basics work.

## 7. Web page (HUD)

File: `~/Odin/odin/web/index.html` (single file, vanilla JS, no build step).

- Full-window layout. The video is the go2rtc player embedded in an `<iframe>` pointing at `http://<pi-ip>:1984/stream.html?src=drone&mode=webrtc` (verify the URL against the go2rtc UI; alternatively use go2rtc's `video-rtc.js` element). Use `location.hostname` so it works for any Pi IP.
- A transparent `<canvas>` absolutely positioned over the video, `pointer-events: none`.
- JS opens `ws://<host>:8080/ws` and redraws on each message (use `requestAnimationFrame`).
- HUD elements: artificial horizon / pitch ladder, heading tape, altitude and speed readouts, battery voltage and %, GPS fix and sats, flight mode, ARMED/DISARMED, and a red "NO TELEMETRY" banner when `link_ok` is false or the WebSocket is closed.
- Auto-reconnect the WebSocket with backoff.
- Keep it readable: high-contrast text with a dark outline, since it overlays video.

## 8. Deployment layout

```
~/Odin/odin/
  DRONE_VISION_DESIGN.md
  bin/go2rtc
  go2rtc.yaml
  telemetry/server.py
  telemetry/sim_mavlink.py      # fake APM for testing without the FC
  telemetry/config.env          # TELEM_SOURCE, device, baud
  telemetry/requirements.txt
  web/index.html
  systemd/go2rtc.service
  systemd/drone-telemetry.service
  README.md
```

- Use a Python venv in `~/Odin/odin/.venv`.
- systemd units: `Restart=always`, `After=network-online.target`. The telemetry unit should run as a user in the `dialout` group (serial access). Enable both with `systemctl enable --now`.
- Add a short `README.md` with: how to start/stop, the URLs, how to change serial device and baud.

## 9. Work plan (do in order, report after each step)

The work is split into **Phase A (no flight controller needed, do now)** and **Phase B (needs the FC connected, do later)**.

### Phase A: build everything without the FC

1. **Recon, no changes:** OS/arch, Python version, camera detection, list of serial devices (`ls /dev/ttyAMA* /dev/ttyUSB* /dev/serial*`), free disk, current IP and network. Report to the user. Do not assume an FC is attached; ask the user only if a device looks like one.
2. **Video first:** get go2rtc installed and the camera streaming over WebRTC in the laptop browser. Measure latency and CPU.
3. **Telemetry service with pluggable sources (section 6b):** write `server.py` with the `serial`, `udp`, `sim` and `auto` sources, waiting mode, and the `source`/`link_ok` fields. Start with a `--print` mode (dump JSON to console), then add the WebSocket.
4. **MAVLink simulator:** write `sim_mavlink.py` and verify the service parses it through `--source udp:...` exactly as it would parse the real FC.
5. **HUD page:** build `index.html` and develop it against the simulator. Include the `LIVE` / `SIM` / `NO FC` badge and the NO TELEMETRY banner.
6. **Combine and systemd:** auto-start on boot with `TELEM_SOURCE=auto` and **no FC attached**. After reboot, the page must load with live video and a "NO FC" HUD. Reboot test.
7. **Report:** URLs, measured latency/CPU, how to run the simulator, and the Phase B checklist below.

### Phase B: bring-up when the FC is connected (props OFF)

1. **Wiring:** confirm the user's wiring. APM telemetry port is 5V logic, so a level shifter is needed for GPIO UART, or use a USB-serial adapter. Cross TX/RX, common ground. Do not power the Pi from the APM telemetry port.
2. **GPIO UART only:** enabling it means editing `/boot/firmware/config.txt` and disabling the serial console. This requires user approval first.
3. **Find the device and baud:** run `auto` mode or a quick pymavlink heartbeat probe, expect 57600 baud on a stock APM. Set `config.env`.
4. **Stream rates:** if no attitude data arrives, ask the user to set the `SR0_*` params in Mission Planner (see section 6), or get approval for `REQUEST_DATA_STREAM`.
5. **Verify values:** tilt the drone by hand and check roll/pitch signs and heading against Mission Planner. Check battery voltage against a multimeter. Check mode and armed state.
6. **Reboot test with the FC attached.** Then test unplug/replug.

### Acceptance checks

Phase A (no FC):

- [ ] `http://<pi-ip>:8080` shows live WebRTC video from the Pi camera.
- [ ] With no FC and no simulator, the HUD shows `NO FC` / "NO TELEMETRY" and nothing crashes or restarts in a loop (`journalctl` is quiet).
- [ ] With `sim_mavlink.py` running, the HUD moves correctly through the real MAVLink parsing path, and shows `LIVE` only if the source is real (and a clear `SIMULATED` label for `sim`/simulator data).
- [ ] Starting or stopping the simulator is picked up automatically with no service restart.
- [ ] Everything starts after a reboot with no manual steps.
- [ ] No MAVLink message is ever sent to the FC.

Phase B (FC attached):

- [ ] Tilting the drone by hand moves the horizon with correct roll/pitch signs.
- [ ] Unplugging the serial link shows "NO TELEMETRY" and recovers when replugged, without restarting the service.
- [ ] Battery, GPS, mode and armed state match Mission Planner.
- [ ] No commands are sent to the FC (except approved stream-rate requests).

## 10. Future / optional (do NOT build unless asked)

- **Burned-in OSD:** picamera2 frames, drawn with OpenCV, piped to ffmpeg, RTSP-pushed into go2rtc. Higher CPU, but the OSD appears in any player and in recordings.
- Pi WiFi hotspot mode for field use (needs user approval, see constraints).
- Recording to disk, object detection/tracking, sending commands back to the FC.

## 11. Ground rules for the agent

- Ask before: installing system-wide packages with `sudo`, editing `/boot/firmware/config.txt`, touching network/SSH/firewall settings, or sending any MAVLink message.
- Prefer a venv over system-wide pip installs.
- If something in this document conflicts with what you find on the Pi (device names, flags, versions), trust the Pi, verify against official docs, and tell the user what differed.
- Keep custom code small and commented. Do not add frameworks or dependencies that are not needed.
