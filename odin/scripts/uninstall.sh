#!/usr/bin/env bash
# Remove the Odin services so they no longer start at boot.
#
#     ./scripts/uninstall.sh          # stop, disable and remove the units
#     ./scripts/uninstall.sh --purge   # also delete .venv/ and bin/
#
# The repository, your telemetry/camera settings and any flight logs are left
# alone unless --purge is given.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR=/etc/systemd/system
PURGE=0
for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=1 ;;
    -h|--help) sed -n '2,8p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "error: unknown option $arg" >&2; exit 2 ;;
  esac
done

echo "==> stopping and disabling"
sudo systemctl disable --now drone-telemetry.service 2>/dev/null || true
sudo systemctl disable --now go2rtc.service            2>/dev/null || true

echo "==> removing unit files"
sudo rm -f "$UNIT_DIR/drone-telemetry.service" "$UNIT_DIR/go2rtc.service"
sudo systemctl daemon-reload
sudo systemctl reset-failed 2>/dev/null || true

if [[ "$PURGE" == 1 ]]; then
  echo "==> removing downloaded binaries and the virtualenv"
  rm -rf "$REPO/bin" "$REPO/.venv"
  echo "    (telemetry/, web/ and your settings are untouched)"
fi

echo "Done. Nothing will start at boot any more."
