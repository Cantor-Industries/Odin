#!/usr/bin/env bash
# Install the odin services so they start automatically on every boot.
#
# Run once (needs root for the systemd install):
#
#     sudo ./scripts/install-services.sh
#
# After this, both units are enabled (start at boot) and started now. They are
# also set to restart forever if they die, so a wedged camera or an unplugged
# flight controller will not leave the Pi without a HUD.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR=/etc/systemd/system

if [[ $EUID -ne 0 ]]; then
  echo "error: run this with sudo (it writes to $UNIT_DIR)" >&2
  exit 1
fi

echo "==> repository: $REPO"

# --- sanity checks, so a broken install is caught here and not at boot ------
for f in \
  "$REPO/bin/go2rtc" \
  "$REPO/go2rtc.yaml" \
  "$REPO/.venv/bin/python" \
  "$REPO/telemetry/server.py" \
  "$REPO/telemetry/config.env" \
  "$REPO/telemetry/camera.env"
do
  if [[ ! -e "$f" ]]; then
    echo "error: missing $f" >&2
    exit 1
  fi
done

# The units hard-code absolute paths. If the repo has been moved or renamed
# since they were written, rewrite them now so the installed copy is correct.
# Matches any /a/b/odin style root, so a rename of the parent dir is handled.
echo "==> syncing paths in the unit files to $REPO"
for unit in "$REPO"/systemd/*.service; do
  sed -i -E "s#(/[A-Za-z0-9_./-]*)/odin/#$REPO/#g" "$unit"
done
# go2rtc.yaml runs camera_pipe.py through exec:, and must match too.
sed -i -E "s#(/[A-Za-z0-9_./-]*)/odin/#$REPO/#g" "$REPO/go2rtc.yaml"

# --- stop the manual nohup processes, so ports are free --------------------
echo "==> stopping any manually started copies"
systemctl stop go2rtc.service drone-telemetry.service 2>/dev/null || true
for pat in "bin/go2rtc" "telemetry/server.py" "telemetry/sim_mavlink.py"; do
  # pkill -f would also match this script, so resolve PIDs first.
  pids="$(ps -eo pid=,cmd= | awk -v p="$pat" 'index($0,p){print $1}')"
  [[ -n "$pids" ]] && kill -TERM $pids 2>/dev/null || true
done
sleep 2

# --- install --------------------------------------------------------------
echo "==> installing unit files"
install -m 0644 "$REPO/systemd/go2rtc.service"          "$UNIT_DIR/go2rtc.service"
install -m 0644 "$REPO/systemd/drone-telemetry.service" "$UNIT_DIR/drone-telemetry.service"

systemctl daemon-reload

echo "==> enabling (start at boot) and starting now"
systemctl enable --now go2rtc.service
systemctl enable --now drone-telemetry.service

# `systemctl enable` returns 0 even when it does nothing at all, which is what
# happens for a unit with no [Install] section (systemd calls those "static").
# That exact bug meant go2rtc ran until the first reboot and never came back.
# Assert the outcome instead of trusting the exit code.
echo "==> verifying both units are enabled for boot"
for u in go2rtc.service drone-telemetry.service; do
  state="$(systemctl is-enabled "$u" 2>&1 || true)"
  if [[ "$state" != "enabled" ]]; then
    echo "error: $u is not enabled (systemctl reports '$state')." >&2
    echo "       A unit with no [Install] section cannot be enabled." >&2
    exit 1
  fi
  printf "    %-24s %s\n" "$u" "$state"
done

sleep 3
echo
echo "==> status"
systemctl --no-pager --lines=0 status go2rtc.service          || true
systemctl --no-pager --lines=0 status drone-telemetry.service || true

echo
echo "Both services are enabled. They will start automatically on every boot."
echo "  logs:    journalctl -u go2rtc -f"
echo "           journalctl -u drone-telemetry -f"
echo "  page:    http://$(hostname -I | awk '{print $1}'):8080"
echo
echo "To undo:  sudo ./scripts/uninstall-services.sh"
