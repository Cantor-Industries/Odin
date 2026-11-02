#!/usr/bin/env bash
# Remove the odin services so they no longer start at boot.
#
#     sudo ./scripts/uninstall-services.sh
#
# The repository, the virtualenv and the binaries are left untouched.
set -euo pipefail

UNIT_DIR=/etc/systemd/system

if [[ $EUID -ne 0 ]]; then
  echo "error: run this with sudo (it writes to $UNIT_DIR)" >&2
  exit 1
fi

echo "==> stopping and disabling"
systemctl disable --now drone-telemetry.service 2>/dev/null || true
systemctl disable --now go2rtc.service           2>/dev/null || true

echo "==> removing unit files"
rm -f "$UNIT_DIR/drone-telemetry.service" "$UNIT_DIR/go2rtc.service"
systemctl daemon-reload
systemctl reset-failed 2>/dev/null || true

echo "Done. Nothing will start at boot any more."
