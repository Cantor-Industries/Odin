# Odin

Companion computer for a Raspberry Pi 5 drone: low-latency WebRTC video from a
CSI camera, plus a Python MAVLink telemetry service and a fighter-jet style HUD,
all viewable in a laptop browser at a single URL.

## Install

On a Raspberry Pi 5 with 64-bit Raspberry Pi OS and a CSI camera:

```bash
git clone <this-repo> ~/Odin
cd ~/Odin/odin
./scripts/install.sh
```

One script handles apt packages, the pinned/checksummed go2rtc + ffmpeg
binaries, the Python environment, camera-sensor detection, and enabling both
services at boot. Re-running it is the supported update path. See
`odin/README.md` for options, offline installs, and what it adapts
automatically.

## Layout

```
Odin/                       repo root
├── README.md               this file
├── agents.md               the original build spec / handoff document
├── .gitignore
└── odin/                   the application
    ├── README.md           the real documentation: install, URLs, HUD, troubleshooting
    ├── pyproject.toml      Python deps (uv project) + uv.lock
    ├── bin/                static ffmpeg + go2rtc (fetched, checksum-verified, git-ignored)
    ├── go2rtc.yaml         stream definition, ports, CORS origin
    ├── scripts/            install.sh, fetch-binaries.sh, uninstall.sh
    ├── systemd/            unit templates (@ODIN_USER@, @ODIN_ROOT@)
    ├── telemetry/          Python services
    │   ├── server.py       MAVLink -> state -> WebSocket + web page + /api/*
    │   ├── camera_pipe.py  supervises rpicam-vid + ffmpeg
    │   ├── derived.py      home, distance, flight timer, mAh, efficiency
    │   ├── sysinfo.py      CPU, temperature, throttling, WiFi
    │   ├── sim_mavlink.py  standalone fake APM (the HUD has an in-process sim too)
    │   ├── camera_ctl.py   colour controls from the command line
    │   ├── config.env      telemetry source, device, baud, cells, HTTP port
    │   ├── camera.env      camera colour settings (written at runtime)
    │   └── requirements.txt  legacy pip list; the installer prefers uv
    └── web/                no build step, plain ES modules
        ├── index.html      page shell: video, WebSocket, colour panel
        ├── hud.js          the HUD (HUD_CONFIG + class HUD)
        └── player.js       native WebRTC player with extended getStats()
```

`bin/` and `.venv/` are deliberately not committed: the binaries are ~100 MB of
downloads and the virtualenv is reproducible. Both are created by
`scripts/install.sh`.

## Start / stop

Installed as systemd units (start automatically on every boot):

```bash
./odin/scripts/install.sh                   # install + enable + start
./odin/scripts/uninstall.sh                # stop + disable + remove
systemctl status go2rtc drone-telemetry
journalctl -u drone-telemetry -f
```

Run by hand while developing:

```bash
cd ~/Odin/odin
./bin/go2rtc -config go2rtc.yaml &
./.venv/bin/python telemetry/server.py --source auto
```

## URLs

| URL | What |
|---|---|
| `http://<pi>:8080` | Main page: video + HUD + colour controls |
| `http://<pi>:1984` | go2rtc status UI |
| `http://<pi>:8080/api/state` | Current telemetry snapshot |
| `http://<pi>:8080/api/source` | Telemetry source (GET / POST for the SIM toggle) |
| `http://<pi>:8080/health` | Liveness probe |

## Key documents

* `odin/README.md` — the working documentation: HUD rules, controls, latency
  notes, troubleshooting.
* `agents.md` — the original specification, kept for reference. Note that it
  describes `~/drone-vision`; the code now lives in `Odin/odin`.
