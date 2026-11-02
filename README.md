# Odin

Companion computer for a Raspberry Pi 5 drone: low-latency WebRTC video from a
CSI camera, plus a Python MAVLink telemetry service and a fighter-jet style HUD,
all viewable in a laptop browser at a single URL.

## Layout

```
Odin/                       repo root
├── README.md               this file
├── agents.md               the original build spec / handoff document
├── .gitignore
└── odin/                   the application
    ├── README.md           the real documentation: URLs, HUD keys, troubleshooting
    ├── bin/                static ffmpeg + go2rtc binaries (git-ignored)
    ├── go2rtc.yaml         stream definition, ports, CORS origin
    ├── scripts/            install-services.sh, uninstall-services.sh
    ├── systemd/            go2rtc.service, drone-telemetry.service
    ├── telemetry/          Python services
    │   ├── server.py       MAVLink -> state -> WebSocket + web page + /api/*
    │   ├── camera_pipe.py  supervises rpicam-vid + ffmpeg
    │   ├── derived.py      home, distance, flight timer, mAh, efficiency
    │   ├── sysinfo.py      CPU, temperature, throttling, WiFi
    │   ├── sim_mavlink.py  standalone fake APM (the HUD has an in-process sim too)
    │   ├── camera_ctl.py   colour controls from the command line
    │   ├── config.env      telemetry source, device, baud, HTTP port
    │   ├── camera.env      camera colour settings (written at runtime)
    │   └── requirements.txt
    └── web/                no build step, plain ES modules
        ├── index.html      page shell: video, WebSocket, colour panel
        ├── hud.js          the HUD (HUD_CONFIG + class HUD)
        └── player.js       native WebRTC player with extended getStats()
```

`bin/` and `.venv/` are deliberately not committed: the binaries are ~100 MB of
downloads and the virtualenv is reproducible. See `odin/README.md` for how they
were obtained.

## Start / stop

Installed as systemd units (start automatically on every boot):

```bash
sudo ./odin/scripts/install-services.sh     # install + enable + start
sudo ./odin/scripts/uninstall-services.sh   # remove
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
