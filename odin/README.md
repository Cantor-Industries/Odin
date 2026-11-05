# Drone Vision

Live camera video plus a flight HUD, viewable in a laptop browser over WiFi.

```
Pi camera --> rpicam-vid (MJPEG) --> ffmpeg (x264) --> go2rtc --WebRTC--> browser video
APM 2.8   --serial/UDP--> Python service (pymavlink) --WebSocket JSON--> same page (HUD canvas)
```

Video is served by **go2rtc**; the HUD is drawn in the browser on a canvas over
the video. Telemetry is **read-only**: this system never sends commands to the
flight controller.

---

## URLs

Replace `10.0.61.170` with the Pi's current address (`hostname -I`).

| URL | What |
|---|---|
| **http://10.0.61.170:8080** | **Main page: video + HUD + colour controls** |
| http://10.0.61.170:1984 | go2rtc web UI |
| http://10.0.61.170:1984/stream.html?src=drone&mode=webrtc | Video only |
| http://10.0.61.170:8080/api/state | Latest telemetry as JSON |
| http://10.0.61.170:8080/health | Liveness probe |
| http://10.0.61.170:8080/api/camera | Camera colour settings |

---

## Install on a new Pi

One script does everything, and re-running it is how you update or repair an
install:

```bash
git clone <this-repo> ~/Odin
cd ~/Odin/odin
./scripts/install.sh
```

It will:

1. check that this is a 64-bit Raspberry Pi OS machine;
2. `apt-get install` the pieces that cannot come from pip — `rpicam-apps-core`
   (`rpicam-vid`), `iw`, `raspi-utils-core` (`vcgencmd`);
3. download **go2rtc 1.9.15** and **static ffmpeg 7.0.2** for arm64, verifying
   each against a pinned SHA-256 (`scripts/fetch-binaries.sh`);
4. build `.venv` from `pyproject.toml` + `uv.lock` with **uv** (or fall back to
   `python -m venv` + pip if uv is not installed);
5. detect the camera sensor and set the matching tuning file;
6. render the systemd units for *your* username and path, enable both, and
   start them.

It asks for sudo only for the steps that need root (apt, unit install, group
membership); everything inside the repo stays owned by you.

Options:

| Flag | Effect |
|---|---|
| `--no-apt` | do not run apt; just report what is missing |
| `--no-enable` | install and start the units, but do not start them at boot |

Uninstall with `./scripts/uninstall.sh` (`--purge` also deletes `.venv/` and
`bin/`).

**Requirements:** Raspberry Pi 5 (or another Pi with a `rpicam-vid`-capable
camera stack) running 64-bit Raspberry Pi OS / Debian, plus a CSI camera.
Network access is needed once, at install time, for apt and the binaries.

### What the installer adapts automatically

These used to be hardcoded assumptions; each is now detected or configured:

| Assumption | Now |
|---|---|
| user `cosmos`, repo at `~/Odin` | units are `@ODIN_USER@`/`@ODIN_ROOT@` templates rendered at install |
| repo path appears in `go2rtc.yaml` | `exec:` is relative, resolved against `WorkingDirectory` |
| sensor is `imx219` | `detect_sensor()` reads `rpicam-vid --list-cameras`, then I2C |
| camera is NoIR | `CAM_NOIR=1\|0` in `camera.env`, written by the installer |
| WiFi is `wlan0` | `active_wifi_iface()` scans `/sys/class/net/*/wireless` |
| battery is 3S | `TELEM_CELLS=0` (unknown) unless you set it |

If you attach a different camera, re-run `./scripts/install.sh`: it detects the
new sensor and updates the tuning file. Unknown sensors get libcamera's default
rather than a path that does not exist (rpicam-vid only *warns* about those and
then renders bad colour, so a wrong path fails silently).

### Offline / no internet

The binaries are not committed (~103 MB), so a machine with no network needs
them carried across:

```bash
# on a machine that has them
scp -r ~/Odin/odin/bin  user@newpi:~/Odin/odin/

# on the new Pi
./scripts/fetch-binaries.sh --check    # verifies the copied files
./scripts/install.sh --no-apt          # needs apt packages preinstalled
```

---

## Start / stop / status

```bash
sudo systemctl status  go2rtc drone-telemetry
sudo systemctl restart go2rtc
sudo systemctl restart drone-telemetry
sudo systemctl stop    go2rtc drone-telemetry

# logs
journalctl -u drone-telemetry -f
journalctl -u go2rtc -f
```

Both units are enabled at boot (`Restart=always`), so after a power cycle the
page loads with video and a HUD, even with **no flight controller attached**.

---

## Telemetry source

Edit `telemetry/config.env`, then `sudo systemctl restart drone-telemetry`.

| `TELEM_SOURCE` | Meaning |
|---|---|
| `auto` | **Default.** Probe `/dev/ttyUSB*`, `/dev/ttyACM*`, `/dev/ttyAMA0`; only accept a port that emits a MAVLink heartbeat. Waits and retries forever if none does. |
| `serial:/dev/ttyUSB0:57600` | Real FC on a USB-serial adapter (recommended). |
| `serial:/dev/ttyAMA0:57600` | Pi GPIO UART. **Requires a 5V→3.3V level shifter** — see Phase B. |
| `udp:127.0.0.1:14550` | MAVLink over UDP (simulator, SITL, MAVProxy). |
| `sim` | Fake data generated inside the service; no MAVLink at all. |
| `file:/path/flight.tlog` | Replay a captured log. |

`TELEM_DEVICE` and `TELEM_BAUD` set the serial defaults (APM 2.8 is 57600).

### Status badge

The HUD always says which kind of data you are looking at:

* `LIVE` — real MAVLink telemetry
* `SIM` — simulated (either `--source sim`, or the bundled simulator; the
  latter is detected from its `SIM:` status messages). Fake data can never be
  mistaken for live.
* `NO FC` — no heartbeat. Every flight value reads `---` and the flight
  instruments dim to 40%, so the page still looks like an instrument panel.

### HUD controls

The HUD is `web/hud.js` (an ES module) driven by `web/index.html`. All of its
look lives in the `HUD_CONFIG` object at the top of that file. Keys:

| Key | Action |
|---|---|
| `H` | hide / show the HUD |
| `R` | Radio Link panel |
| `E` | Efficiency Stats panel |
| `V` | Video Decoder panel |
| `U` | metric / imperial |
| `A` | altitude source: relative / MSL |
| `F` | fullscreen |
| `S` | toggle the simulator (same as clicking the status chip) |

Choices persist in `localStorage`. The SIM toggle itself does **not** persist:
see below.

### Simulator

Nothing simulates at boot. The service starts on its configured source
(`TELEM_SOURCE`, default `auto`) and, with no flight controller attached, simply
shows `NO FC`. To exercise the HUD:

* press **`S`**, or click the status chip in the top centre; or
* `curl -X POST localhost:8080/api/source -H 'Content-Type: application/json' -d '{"mode":"sim"}'`

The backend starts an in-process fake feed through exactly the same
`TelemetryState.handle()` path as real MAVLink, so the HUD, derived values and
socket payload are identical — only the badge differs (`SIM` in amber). Switching
back goes to the configured source, so `auto` returns to waiting for a real
controller.

Safety rules built into the endpoint:

* It **refuses** to switch to SIM while real MAVLink is actually live
  (`409`, chip shows `SIM BLOCKED` for a few seconds). A real flight must never
  be overlaid with invented numbers.
* The switch is temporary. To change the *boot* default, add `"persist": true`
  — that writes `telemetry/source.env` (git-ignored), which overrides
  `TELEM_SOURCE` at startup. Delete that file to fall back to `config.env`.

`telemetry/sim_mavlink.py` still exists for testing the real MAVLink path over
UDP; it is not used by the toggle and is never started by the units.

**No backgrounds.** By default nothing is drawn behind any text, box or
panel: the speed, altitude and mode boxes are thin outlines, and legibility
comes from a dark shadow halo around each glyph. Set `HUD_CONFIG.backgrounds
= true` to bring back the faint reference-style strips.

**Washes that fade out.** The pitch ladder carries `ladder.wash`, and the speed
and altitude tapes carry `speed.wash` / `alt.wash` — the same light grey at 15%.
On the tapes the wash runs from the tape out past the value box and dissolves
into the margin over `speed.fade` / `alt.fade` (a fraction of frame width), so
it never ends on a hard edge. The value boxes match: the edge against the tape
is a solid line, the two horizontal rules dissolve outward, and there is
deliberately no vertical line at the outer end. Set a `wash` to `null` to remove
it, or `fade: 0` for an abrupt edge like the reference.

**Three text sizes.** `HUD_CONFIG.text` holds `s`, `m` and `l` as fractions of
frame height; there are no other sizes. The font is monospaced so digits are
equal width and numbers do not shuffle as they change. Number and unit always
share one baseline, decimals are fixed per field.

**Overlap suppression.** Each label reserves a box before it is drawn. If a
label would collide with one of equal or higher priority, it is dropped. That
is why the heading cardinals have a row of their own above the numbers, and
why the home marker is on that same row. The speed and altitude value boxes
reserve their outline as an exclusion zone, so tape numbers falling behind
them are dropped rather than printed through the readout.

**Bottom row.** Row 1 carries vertical speed and total distance. Row 2 has
three parts: range to home flows from the left margin, battery / current /
throttle / pitch are anchored to the right margin, and the compass group is
centred in the span between them by its measured width. The compass is a
north-referenced card, not a decoration: the needle swings opposite to the
heading and the fixed caret above the ring is the nose, while the green arrow
points at home. Its three layers never cross — the needle stays inside the
ring, the caret outside it. The icon size is `HUD_CONFIG.bottom.iconRadius`,
and row 2 is anchored from that radius, so changing it cannot push the dial
through the bottom margin.

**No repeated metrics.** Every reading appears exactly once. Groundspeed is the
speed tape's value box, altitude is the altitude tape's, heading is the number
under the heading tape, and range/bearing to home are the bottom strip and the
line under the speed tape. Panels add only what is shown nowhere else: the radio
panel keeps link delay, MAVLink data rate, link rate and the signal sparkline;
the decoder panel keeps resolution, preset, buffer, the histogram and the packet
counters. Bitrate and frame rate live in the top strip only. If you add a field,
check it is not already on screen.

---

## Testing without a flight controller

Terminal 1 — fake APM over UDP (MAVLink 1, real message path):

```bash
cd ~/Odin/odin
./.venv/bin/python telemetry/sim_mavlink.py --out udpout:127.0.0.1:14550
```

Terminal 2 — service pointed at it:

```bash
./.venv/bin/python telemetry/server.py --source udp:127.0.0.1:14550 --print
```

`--print` also dumps the JSON state to the console once per second.
Open the page: video is live and the HUD shows `SIM`.

Starting or stopping the simulator is picked up automatically, with no service
restart (`link_ok` flips after 3 s without a heartbeat).

For pure UI work with no MAVLink at all: `--source sim`.

---

## Camera colour controls

Either use the **gear button** on the HUD, or the CLI:

```bash
./telemetry/camera_ctl.py show
./telemetry/camera_ctl.py set saturation=1.4 contrast=1.15
./telemetry/camera_ctl.py set awb=cloudy ev=0.3
./telemetry/camera_ctl.py preset overcast
./telemetry/camera_ctl.py reset
./telemetry/camera_ctl.py controls    # list everything
```

Settings are stored in `telemetry/camera.env` and applied live: `camera_pipe.py`
watches that file and restarts only the camera+encoder pair, so video is back
within about a second and go2rtc never sees the pipe close.
Presets: `punchy`, `flat`, `grey`, `overcast`, `sunny`, `lowlight`.

Controls: brightness, contrast, saturation, sharpness, EV, white balance,
metering, exposure mode, denoise, H/V flip, rotation, resolution, fps, JPEG
quality and output bitrate.

`telemetry/camera.env` is the only interface. The `CAM_TUNING` line is normally
**absent**, which means "use the sensor's default" — the NoIR tuning file on
this module. Writing `CAM_TUNING=default` selects the stock IR-cut tuning and
brings back a strong magenta cast.

---

## Video latency and the stats

Video numbers appear in two places on the HUD:

* the **top-right strip**: current bitrate (red), browser frame rate, WiFi
  signal, frequency, and the Pi's CPU / clock / temperature;
* the **Video Decoder panel** (`V`): resolution, encoder preset, buffer, a
  30 ms frame-rate histogram, lost-gap figures, packets/s, NACK, dropped and
  lost packet counts.

`latAvg` in the page is the rolling end-to-end estimate — browser jitter buffer
+ decode + Pi queueing + half the link round trip. It is an estimate, not a
glass-to-glass measurement: nothing in this pipeline timestamps a frame at
capture, so camera exposure and the x264 encode time are not included. It
measures the delay you can actually act on.

What was fixed to make it fast:

* **`read1()` instead of `read(n)`** in `camera_pipe.py`. `BufferedReader.read`
  blocks until it has the full 64 KiB, which at 2.5 Mbit/s held ~210 ms of video
  before forwarding anything.
* **`-flush_packets 1`** so ffmpeg does not sit on a 32 KiB AVIO buffer.
* **`-bufsize` halved** to half the bitrate, instead of a full second of
  rate-control slack.
* **`RTCRtpReceiver.jitterBufferTarget = 30 ms`** on the browser side, instead
  of the default ~300-500 ms. This is the single biggest client-side win.
* **`api.origin: "*"`** in `go2rtc.yaml`, which lets the HUD talk to go2rtc's
  signalling WebSocket directly instead of hiding it in an iframe.

Do **not** add `aud=1` to the encoder: it makes go2rtc's H.264 parser coalesce a
whole GOP into one packet, which collapses the stream to 1 fps.

Result on this Pi 5 over Wi-Fi: 24 fps at 1280x720, ~2.4 Mbit/s, ~15-20 ms of
measured post-encode latency.

---

## Phase B: connecting the APM 2.8

**Props off.** Read all of this before wiring.

1. **Wiring.** Use a USB-serial adapter (FTDI/CP2102) — it avoids level
   shifting entirely and is what this build defaults to.
   * APM telemetry port → adapter: TX↔RX, RX↔TX, GND↔GND.
   * **Do not** power the Pi from the APM telemetry port.
   * **If you use the Pi GPIO UART instead:** APM 2.8 serial is **5 V logic**
     and the Pi's GPIO is **3.3 V**. APM TX *must* go through a level shifter
     before touching the Pi's RX pin, or you will damage the Pi.
     Enabling the GPIO UART also means editing `/boot/firmware/config.txt`
     and disabling the serial console — do that only with explicit approval,
     since the Pi may be reached over that UART.

2. **Find the device.** Plug in the adapter, then:

   ```bash
   ls /dev/ttyUSB* /dev/ttyACM*
   sudo systemctl restart drone-telemetry
   journalctl -u drone-telemetry -f      # should log "heartbeat found on ..."
   ```

   With `TELEM_SOURCE=auto` this happens by itself; no config change is needed
   if the device turns up on a standard name. Otherwise set
   `TELEM_SOURCE=serial:/dev/ttyUSB0:57600` explicitly.

3. **Stream rates.** Old ArduCopter firmware does not stream data until asked.
   Preferred: in Mission Planner, set the telemetry port's stream rates
   (`SR0_EXTRA1`, `SR0_EXTRA2`, `SR0_EXTRA3`, `SR0_POSITION`, `SR0_RAW_SENS`,
   `SR0_EXT_STAT`) to roughly 4–10 Hz.
   Fallback is `--request-streams`, which sends `REQUEST_DATA_STREAM` — that is
   the *only* message this service can ever send, and only if you enable it.

4. **Verify values.** Tilt the drone by hand and check that the horizon moves
   the right way; compare heading, battery voltage, mode and armed state against
   Mission Planner. Check battery voltage against a multimeter.

5. **Unplug / replug** the serial link: the HUD should show `NO FC`, dim, and
   recover on its own, without restarting the service.

---

## Layout

```
~/Odin/odin/
  pyproject.toml, uv.lock               Python deps, pinned (uv project)
  bin/go2rtc, bin/ffmpeg, bin/ffprobe   fetched + checksum-verified, git-ignored
  go2rtc.yaml                           stream definition + ports
  scripts/install.sh                    the installer: apt, binaries, venv, units
  scripts/fetch-binaries.sh             download/verify the static binaries
  scripts/uninstall.sh                  stop + disable + remove (--purge too)
  telemetry/server.py                   MAVLink -> state -> WebSocket + web page
  telemetry/camera_pipe.py              supervises rpicam-vid + ffmpeg
  telemetry/camera_ctl.py               colour controls (CLI)
  telemetry/sim_mavlink.py              fake APM for testing the real MAVLink path
  telemetry/config.env                  telemetry source + port (boot default)
  telemetry/source.env                  optional boot override (git-ignored)
  telemetry/camera.env                  colour settings (written at runtime)
  telemetry/derived.py                  home, distance, timer, mAh, efficiency
  telemetry/sysinfo.py                  CPU, temperature, throttling, WiFi
  telemetry/requirements.txt            legacy pip list (the installer prefers uv)
  web/index.html                        page shell: video, WS, colour panel
  web/hud.js                            the HUD: HUD_CONFIG + class HUD
  web/player.js                         native WebRTC player with getStats()
  systemd/*.service                     templates: @ODIN_USER@, @ODIN_ROOT@
  .venv/                                created by uv, git-ignored
```

No build step: both modules are loaded directly as ES modules from `/static/`.

## Starting on boot

`./scripts/install.sh` renders the unit templates into
`/etc/systemd/system/`, enables both and starts them. After a reboot the page
comes up on its own — nothing needs logging in. The camera pipeline is started
by go2rtc on demand (first viewer), so an idle Pi is not encoding video.

If a unit refuses to come back after a reboot, check
`systemctl is-enabled <unit>`: it must say `enabled`, not `static`. A unit with
no `[Install]` section enables "successfully" and then never starts again.

## Tests

```bash
uv run pytest -q          # 68 tests, a few seconds
uv run ruff check telemetry tests
```

CI runs both on every push (`.github/workflows/tests.yml`), on Python 3.11 and
3.13. Nothing in the suite needs a camera, go2rtc, a flight controller or arm64:
`tests/test_derived.py` is arithmetic and `tests/test_http.py` drives aiohttp
in-process.

What is worth testing here, and why:

* **`derived.py`** — the only module doing real arithmetic, and the only place
  where a wrong answer appears to the pilot as a plausible number. Covered by
  concrete cases with hand-computed values (one degree of latitude, 1 A for an
  hour, antipodal points) plus hypothesis properties (haversine is symmetric,
  bearing stays in `[0, 360)`, consumed mAh never decreases). Every one of
  these invariants fails if a sign or argument order is flipped.
* **The JSON content-type guard** — `tests/test_http.py` asserts that
  `text/plain`, form-encoded and multipart POSTs get `415` and cause no side
  effect. `text/plain` is a CORS-simple type, so without this a page the
  operator visits could fire a state-changing request cross-origin.
* **The absence of a command path** — `test_no_control_endpoint_exists` fails
  if anyone adds `/api/goto`, `/api/arm`, `/api/mode`, `/api/command` or
  `/api/param`. That is deliberate friction: adding a control endpoint should
  start a conversation about a safety gate, not pass CI silently.

Not covered, deliberately: the camera pipeline, go2rtc and the HUD. They need
real hardware or a browser, and their failure modes are visible immediately
rather than silently wrong.

## Troubleshooting

**Install failed at the apt step.** A fresh Pi image often has a stale package
index. Run `sudo apt-get update` and retry, or use `--no-apt` and install the
handful of packages yourself (`rpicam-apps-core iw raspi-utils-core`).

**`error: bin/ is missing files or they do not match the pinned versions`.**
The binaries were not downloaded, or a partial download left something behind.
Run `./scripts/fetch-binaries.sh --force`. If the machine is offline, copy a
working `bin/` directory in from another install and run
`./scripts/fetch-binaries.sh --check`.

**Video is magenta / purple.** The module is NoIR (no IR-cut filter) but is not
being given the `*_noir` tuning file. Check `CAM_NOIR` in
`telemetry/camera.env`; `1` selects `<sensor>_noir.json`. Re-run
`./scripts/install.sh` to re-detect the sensor. Conversely, a normal module
given a NoIR tuning file looks washed out — set `CAM_NOIR=0`.

**"failed to acquire camera" / no camera found.** The CSI ribbon, or another
process holds the camera. `rpicam-vid --list-cameras` must show a sensor. Only
one process may stream at a time; `camera_pipe.py` takes a lock in
`/tmp/odin-camera.lock`.

**Page loads but video is black / "error".**
`journalctl -u go2rtc -f` and look for `camera_pipe`. A wedged camera shows
`Failed to queue buffer ... Operation already in progress`. This is a known
Pi 5 kernel driver bug (`rp1_cfe_downstream`, crash in `cfe_stop_streaming`)
triggered by killing a process mid-stream; a reboot clears it. The helper in
`camera_pipe.py` exists to avoid it by shutting both children down together.

**`NO FC` forever with an FC plugged in.** Wrong device or baud, TX/RX not
crossed, no common ground, or the FC is not streaming (see step 3 above).
Try `--source auto --print` to watch for heartbeats.

**Per-cell voltage shows `---`.** `TELEM_CELLS` is `0` (unknown). Set it to the
real cell count (3 for 3S) in `telemetry/config.env`, then restart
`drone-telemetry`. The APM reports pack voltage only.

**A service ran until the first reboot and then vanished.** `systemctl
is-enabled <unit>` says `static` — the unit has no `[Install]` section, so
`enable` silently does nothing. See "Starting on boot".

**HUD values look wrong.** GPS altitude is absolute (MSL); the HUD's altitude
is relative (from `GLOBAL_POSITION_INFO.relative_alt`). Check
`http://<pi>:8080/api/state` for the raw numbers.

## Known tradeoffs

* **OSD is browser-drawn**, not burned into the video: no second encode pass,
  sharper text, and the video stays clean for future computer vision. The
  downside is the HUD is not in recordings or in other players (VLC).
* Video is encoded in **software** (the Pi 5 has no H.264 hardware encoder).
  720p30 costs roughly 20 % of the 4 cores; that leaves headroom for vision work.

## License

Odin is Copyright 2026 Cantor Industries, licensed under the Apache License,
Version 2.0 — see [LICENSE](../LICENSE), [NOTICE](../NOTICE) and
[THIRD_PARTY.md](../THIRD_PARTY.md). The `go2rtc` and `ffmpeg` binaries are
downloaded at install time and never committed, so this repository ships none
of their code.
