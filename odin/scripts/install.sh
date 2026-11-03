#!/usr/bin/env bash
# Install Odin on a Raspberry Pi so it starts automatically on every boot.
#
#   ./scripts/install.sh              # normal install
#   ./scripts/install.sh --no-apt     # do not run apt (offline / managed host)
#   ./scripts/install.sh --no-enable  # install units but do not enable at boot
#
# Safe to re-run: it is the supported way to apply an update or repair a
# half-finished install. It does not touch your telemetry/camera settings.
#
# What it does, in order:
#   1. checks this is a 64-bit Raspberry Pi OS with the camera tools
#   2. installs the apt packages the Pi needs (rpicam-vid, iw, vcgencmd)
#   3. downloads the pinned go2rtc + ffmpeg binaries, verified by checksum
#   4. creates .venv with uv (or falls back to python -m venv + pip)
#   5. detects the camera sensor and sets the right tuning file
#   6. installs, enables and starts both systemd units
#
# Run it as your normal user; it re-executes itself under sudo only for the
# steps that need root, so everything under the repo keeps your ownership.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR=/etc/systemd/system

DO_APT=1
DO_ENABLE=1
for arg in "$@"; do
  case "$arg" in
    --no-apt)    DO_APT=0 ;;
    --no-enable) DO_ENABLE=0 ;;
    --apt)       DO_APT=1 ;;
    -h|--help)   sed -n '2,21p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "error: unknown option $arg" >&2; exit 2 ;;
  esac
done

say()  { printf '\n==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

# Prefer the invoking user's account even when the script was started with
# sudo, so the units run as the human who owns the checkout rather than root.
TARGET_USER="${SUDO_USER:-$(id -un)}"
TARGET_GROUP="$(id -gn "$TARGET_USER")"
if [[ "$TARGET_USER" == "root" ]]; then
  die "refusing to install as root. Run this as your normal user (it will ask for sudo when needed)."
fi

# ---------------------------------------------------------------------------
say "checking the machine"
# ---------------------------------------------------------------------------
ARCH="$(uname -m)"
[[ "$ARCH" == "aarch64" ]] || die "this install targets 64-bit Raspberry Pi OS (aarch64); found $ARCH.
       For a different architecture see 'Other hardware' in README.md."
[[ -f /etc/os-release ]] || die "cannot identify the OS (no /etc/os-release)"
# shellcheck disable=SC1091
. /etc/os-release
case "${ID:-}${ID_LIKE:-}" in
  *debian*|*raspbian*) : ;;
  *) warn "expected Debian/Raspberry Pi OS based; found ${PRETTY_NAME:-unknown}. Continuing." ;;
esac
echo "    repo:  $REPO"
echo "    user:  $TARGET_USER (group $TARGET_GROUP)"
echo "    os:    ${PRETTY_NAME:-unknown} ($ARCH)"

for f in go2rtc.yaml pyproject.toml telemetry/server.py \
         telemetry/camera_pipe.py telemetry/config.env; do
  [[ -e "$REPO/$f" ]] || die "missing $f - is $REPO the odin/ directory of the repo?"
done

# ---------------------------------------------------------------------------
say "system packages"
# ---------------------------------------------------------------------------
# rpicam-vid and libcamera are the only way to drive a Pi CSI camera; iw and
# vcgencmd feed the HUD's WiFi and throttling readouts. None of these exist as
# pip packages, and the static ffmpeg does not replace them.
APT_PKGS=(rpicam-apps-core iw raspi-utils-core)
missing=()
command -v rpicam-vid >/dev/null 2>&1 || missing+=(rpicam-apps-core)
command -v iw         >/dev/null 2>&1 || missing+=(iw)
command -v vcgencmd   >/dev/null 2>&1 || missing+=(raspi-utils-core)

if [[ ${#missing[@]} -eq 0 ]]; then
  echo "    all present: rpicam-vid, iw, vcgencmd"
elif [[ "$DO_APT" == 0 ]]; then
  warn "missing: ${missing[*]} (skipped because --no-apt)"
  echo "    install them with: sudo apt-get install -y ${missing[*]}"
else
  echo "    installing: ${missing[*]}"
  # Update the index first: a fresh Pi image often has a stale one and the
  # install then fails on a package that certainly exists.
  sudo apt-get update -qq || warn "apt-get update failed; trying the install anyway"
  sudo apt-get install -y --no-install-recommends "${missing[@]}" || die "apt-get install failed.
       Without rpicam-vid there is no video. Re-run with --no-apt to skip
       this step if the machine is offline or managed by something else."
fi

# ---------------------------------------------------------------------------
say "prebuilt binaries (go2rtc, ffmpeg)"
# ---------------------------------------------------------------------------
# These are pinned by checksum in fetch-binaries.sh. Already-valid files are
# left alone, so re-running the installer does not re-download 20 MB.
"$REPO/scripts/fetch-binaries.sh"

# ---------------------------------------------------------------------------
say "python environment"
# ---------------------------------------------------------------------------
# uv if available: it is much faster and records exact versions in uv.lock.
# Otherwise plain venv+pip still works, so the install never depends on a tool
# the user has not installed.
UV="$(command -v uv || true)"
if [[ -n "$UV" ]]; then
  echo "    using uv ($("$UV" --version))"
  ( cd "$REPO" && "$UV" sync --no-dev --quiet )
else
  warn "uv not found; falling back to python -m venv + pip"
  echo "    (install uv later for faster, locked installs: https://docs.astral.sh/uv/)"
  PY="$(command -v python3 || true)"
  [[ -n "$PY" ]] || die "python3 not found. Install it: sudo apt-get install -y python3-venv"
  [[ -d "$REPO/.venv" ]] || "$PY" -m venv "$REPO/.venv"
  "$REPO/.venv/bin/python" -m pip install --quiet --upgrade pip
  "$REPO/.venv/bin/python" -m pip install --quiet -r "$REPO/telemetry/requirements.txt"
fi

[[ -x "$REPO/.venv/bin/python" ]] || die "venv was not created at $REPO/.venv"
"$REPO/.venv/bin/python" - <<'PY' || die "the venv cannot import the dependencies"
import aiohttp, pymavlink, serial
print("    imports OK: aiohttp %s, pymavlink %s" % (aiohttp.__version__,
                                                    pymavlink.__version__))
PY

# ---------------------------------------------------------------------------
say "camera"
# ---------------------------------------------------------------------------
# Detect the sensor rather than assuming imx219, and only force a NoIR tuning
# file when the module really has no IR-cut filter. Getting this wrong is not
# loud: rpicam-vid merely warns about a bad tuning file and then produces a
# magenta picture.
CAM_ENV="$REPO/telemetry/camera.env"
detect="$("$REPO/.venv/bin/python" - "$REPO" <<'PY'
import sys
sys.path.insert(0, sys.argv[1] + "/telemetry")
import camera_pipe as cp
sensor = cp.detect_sensor()
print(sensor or "")
PY
)" || detect=""

if [[ -z "$detect" ]]; then
  warn "no camera sensor detected."
  echo "    Odin will still start; video stays unavailable until a camera is"
  echo "    attached. Re-run this script after connecting one."
  echo "    (CSI ribbon seated, and 'rpicam-vid --list-cameras' shows it)"
else
  echo "    sensor: $detect"
  # Reverse-engineer a sensible NoIR default: the reference hardware is NoIR,
  # but an unknown module is more likely to have an IR-cut filter.
  noir_default=1
  if [[ "$detect" == "imx219" ]]; then noir_default=1; else noir_default=0; fi

  if grep -q '^CAM_NOIR=' "$CAM_ENV" 2>/dev/null; then
    echo "    keeping existing CAM_NOIR from camera.env (edit it to change)"
  else
    [[ -f "$CAM_ENV" ]] || printf '# Camera colour / image settings\n' > "$CAM_ENV"
    printf '\n# 1 = NoIR module (no IR-cut filter) - needs the *_noir tuning file\n# 0 = normal module with an IR-cut filter\nCAM_NOIR=%s\n' \
      "$noir_default" >> "$CAM_ENV"
    echo "    wrote CAM_NOIR=$noir_default (set 0/1 in camera.env if wrong)"
  fi

  tuning="$("$REPO/.venv/bin/python" - "$REPO" <<'PY'
import os, sys
sys.path.insert(0, sys.argv[1] + "/telemetry")
import camera_pipe as cp
noir = cp.camera_is_noir()
print(cp.tuning_file(cp.detect_sensor(), noir=noir) or "libcamera default")
PY
)"
  echo "    tuning: $tuning"
  if [[ "$noir_default" == 1 && "$tuning" == "libcamera default" ]]; then
    warn "no *_noir.json for $detect; using libcamera's default. If the picture"
    warn "looks magenta, the module is NoIR and has no tuning file for it."
  fi
fi

# ---------------------------------------------------------------------------
say "systemd units"
# ---------------------------------------------------------------------------
# The units in systemd/ are templates with @ODIN_USER@ and @ODIN_ROOT@
# placeholders. They are rendered into /etc/systemd/system, never edited in
# place, so the repo stays clean and re-running is idempotent.
render_unit() {
  local src="$1" dst="$2"
  sed -e "s#@ODIN_USER@#$TARGET_USER#g" \
      -e "s#@ODIN_ROOT@#$REPO#g" \
      "$src" > "$dst"
  chmod 0644 "$dst"
}

# Group memberships: dialout for the USB-serial flight controller, video for
# the camera nodes. A fresh user often lacks one, and the service then fails
# with a permission error that looks like broken hardware.
for g in dialout video; do
  if getent group "$g" >/dev/null 2>&1; then
    if ! id -nG "$TARGET_USER" | tr ' ' '\n' | grep -qx "$g"; then
      echo "    adding $TARGET_USER to group $g"
      sudo usermod -aG "$g" "$TARGET_USER"
      warn "group membership changed: log out and back in (or reboot) for"
      warn "  interactive use. The services pick it up on the next restart."
    fi
  else
    warn "group $g does not exist on this system"
  fi
done

if [[ $EUID -eq 0 ]]; then
  render_unit "$REPO/systemd/go2rtc.service"          "$UNIT_DIR/go2rtc.service"
  render_unit "$REPO/systemd/drone-telemetry.service" "$UNIT_DIR/drone-telemetry.service"
  systemctl daemon-reload
else
  say "installing units (sudo)"
  tmpd="$(mktemp -d)"
  trap 'rm -rf "$tmpd"' EXIT
  render_unit "$REPO/systemd/go2rtc.service"          "$tmpd/go2rtc.service"
  render_unit "$REPO/systemd/drone-telemetry.service" "$tmpd/drone-telemetry.service"
  echo "    rendered units for user $TARGET_USER at $REPO"
  sudo install -m 0644 "$tmpd/go2rtc.service"          "$UNIT_DIR/go2rtc.service"
  sudo install -m 0644 "$tmpd/drone-telemetry.service" "$UNIT_DIR/drone-telemetry.service"
  sudo systemctl daemon-reload
  rm -rf "$tmpd"; trap - EXIT
fi

# Stop any hand-started copies so the ports are free for the units.
say "starting services"
for pat in "bin/go2rtc" "telemetry/server.py" "telemetry/sim_mavlink.py"; do
  # pkill -f would also match this script, so resolve PIDs first.
  pids="$(ps -eo pid=,cmd= | awk -v p="$pat" 'index($0,p){print $1}')"
  [[ -n "$pids" ]] && kill -TERM $pids 2>/dev/null || true
done
sleep 1

if [[ "$DO_ENABLE" == 1 ]]; then
  sudo systemctl enable --now go2rtc.service
  sudo systemctl enable --now drone-telemetry.service

  # `systemctl enable` returns 0 even when it does nothing at all, which is
  # what happens for a unit with no [Install] section (systemd calls those
  # "static"). Assert the outcome instead of trusting the exit code.
  for u in go2rtc.service drone-telemetry.service; do
    state="$(systemctl is-enabled "$u" 2>&1 || true)"
    [[ "$state" == "enabled" ]] || die "$u is not enabled (reports '$state').
       A unit with no [Install] section cannot be enabled."
  done
else
  sudo systemctl restart go2rtc.service drone-telemetry.service 2>/dev/null || true
  echo "    --no-enable: units installed and started, but not enabled at boot"
fi

sleep 2
say "status"
systemctl --no-pager --lines=0 status go2rtc.service          2>&1 | sed 's/^/    /' || true
systemctl --no-pager --lines=0 status drone-telemetry.service 2>&1 | sed 's/^/    /' || true

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
cat <<EOF

Odin is installed${DO_ENABLE:+ and enabled at boot}.
  page:  http://${IP:-<pi-address>}:8080
  video: http://${IP:-<pi-address>}:1984

  logs:  journalctl -u go2rtc -f
         journalctl -u drone-telemetry -f
  undo:  ./scripts/uninstall.sh
EOF
