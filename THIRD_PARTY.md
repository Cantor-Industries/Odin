# Third-party software

Odin is Copyright 2026 Cantor Industries and licensed under the
[Apache License, Version 2.0](LICENSE). This file lists everything Odin uses
that Cantor Industries did not write, with its license and where it comes
from. Every dependency keeps its own license; nothing here changes the terms
of Odin itself.

## Python dependencies

Pinned in `odin/pyproject.toml` and `odin/uv.lock`, installed into `.venv`
at install time.

| Package | Pinned version | License | Role |
|---|---|---|---|
| [pymavlink](https://github.com/ArduPilot/pymavlink) | 2.4.50 | LGPL-3.0-or-later (generated dialect code is MIT) | MAVLink parsing |
| [pyserial](https://github.com/pyserial/pyserial) | 3.5 | BSD-3-Clause | serial transport for pymavlink |
| [aiohttp](https://github.com/aio-libs/aiohttp) | 3.14.5 | Apache-2.0 | web page, WebSocket, camera API |

Development-only tooling, not installed by the runtime installer
(`odin/pyproject.toml`, `[dependency-groups] dev`):

| Package | Pinned version | License |
|---|---|---|
| [pytest](https://github.com/pytest-dev/pytest) | 8.4.2 | MIT |
| [pytest-asyncio](https://github.com/pytest-dev/pytest-asyncio) | 1.2.0 | Apache-2.0 |
| [hypothesis](https://github.com/HypothesisWorks/hypothesis) | 6.140.3 | MPL-2.0 |
| [ruff](https://github.com/astral-sh/ruff) | 0.14.4 | MIT OR Apache-2.0 |

## Downloaded binaries

`odin/scripts/fetch-binaries.sh` downloads these at install time, pinned by
version and SHA-256. They are **not committed** to this repository and are
not distributed with it; users fetch them directly from the sources below.

| Binary | Version | Source | License |
|---|---|---|---|
| [go2rtc](https://github.com/AlexxIT/go2rtc) | 1.9.15 | GitHub release asset `go2rtc_linux_arm64` | MIT |
| [FFmpeg](https://ffmpeg.org) + ffprobe | 7.0.2 | [johnvansickle.com](https://johnvansickle.com/ffmpeg/) static build, arm64 | GPL-3.0 (the tarball ships `GPLv3.txt`; see its `readme.txt`) |

The johnvansickle static ffmpeg builds include GPL components, hence GPL-3.0.
The repository deliberately does not commit or ship these binaries, so this
project is not distributing GPL code. If you redistribute `bin/` yourself,
the GPL-3.0 terms apply to those binaries.

## Operating-system packages

Installed with apt from Raspberry Pi OS / Debian repositories by
`odin/scripts/install.sh`:

| Package | License |
|---|---|
| `rpicam-apps-core` (rpicam-vid, libcamera) | BSD-2-Clause (Raspberry Pi) and upstream libcamera licenses |
| `iw`, `raspi-utils-core` (vcgencmd) | as licensed by their Debian/Raspberry Pi packages |

## Notes

- When bumping a pinned version, update the relevant row above and, for the
  binaries, the SHA-256 values in `odin/scripts/fetch-binaries.sh`.
- Check the upstream license before adding a dependency: this project avoids
  AGPL components (for example Ultralytics YOLO) on purpose.
