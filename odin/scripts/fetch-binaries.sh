#!/usr/bin/env bash
# Fetch the two prebuilt binaries Odin needs, pinned by version and verified by
# checksum. They are not committed: together they are ~103 MB of somebody
# else's release artifacts.
#
#   ./scripts/fetch-binaries.sh            # download what is missing
#   ./scripts/fetch-binaries.sh --force    # re-download everything
#   ./scripts/fetch-binaries.sh --check    # verify only, no network
#
# Offline machines: copy a working bin/ directory from another install into
# this repo, then run with --check. Nothing here is required at runtime.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="$REPO/bin"

# Pinned versions and their SHA-256. Both hashes below were verified against
# the artifacts actually running on the reference Pi 5, so a matching hash
# means "byte-identical to what is known to work here".
#
# go2rtc's hash comes from the digest GitHub publishes on the release asset;
# ffmpeg's is the tarball from johnvansickle.com (no checksum file is
# published there, so the tarball hash was recorded on first verified download).
GO2RTC_VERSION="1.9.15"
GO2RTC_SHA256="af416f22b21a24ab83b8d78642190b4932ae695b4e386853f11d1b0319781763"

FFMPEG_VERSION="7.0.2"
FFMPEG_TARBALL_SHA256="f4149bb2b0784e30e99bdda85471c9b5930d3402014e934a5098b41d0f7201b1"
FFMPEG_BIN_SHA256="6bb182d0d75d23028db82e9e4f723ca69b853d055698486e6984ddb2c06fb8ce"
FFPROBE_BIN_SHA256="d17ae9b4c297d48e2521ba14e417bb0537c6ff77c584cdbcd6bb0d8d0307a2e8"

GO2RTC_URL="https://github.com/AlexxIT/go2rtc/releases/download/v${GO2RTC_VERSION}/go2rtc_linux_arm64"
FFMPEG_URL="https://johnvansickle.com/ffmpeg/releases/ffmpeg-${FFMPEG_VERSION}-arm64-static.tar.xz"

FORCE=0
CHECK_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    --check) CHECK_ONLY=1 ;;
    -h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "error: unknown option $arg" >&2; exit 2 ;;
  esac
done

ARCH="$(uname -m)"
if [[ "$ARCH" != "aarch64" && "$ARCH" != "arm64" ]]; then
  echo "error: these binaries are aarch64 only; this machine is $ARCH." >&2
  echo "       On a different architecture, install go2rtc and ffmpeg yourself" >&2
  echo "       and point bin/ at them (see README)." >&2
  exit 1
fi

sha_of() { sha256sum "$1" 2>/dev/null | cut -d' ' -f1; }

verify() {
  local path="$1" want="$2" name="$3"
  [[ -f "$path" ]] || return 1
  local got; got="$(sha_of "$path")"
  if [[ "$got" != "$want" ]]; then
    echo "  $name: checksum mismatch" >&2
    echo "    expected $want" >&2
    echo "    got      $got" >&2
    return 1
  fi
  return 0
}

# --- check-only mode: report and exit non-zero if anything is wrong ---------
if [[ "$CHECK_ONLY" == 1 ]]; then
  ok=0
  verify "$BIN/go2rtc" "$GO2RTC_SHA256" "go2rtc" || ok=1
  verify "$BIN/ffmpeg" "$FFMPEG_BIN_SHA256" "ffmpeg" || ok=1
  verify "$BIN/ffprobe" "$FFPROBE_BIN_SHA256" "ffprobe" || ok=1
  if [[ "$ok" == 0 ]]; then
    echo "binaries OK (go2rtc ${GO2RTC_VERSION}, ffmpeg ${FFMPEG_VERSION})"
    exit 0
  fi
  echo "error: bin/ is missing files or they do not match the pinned versions." >&2
  echo "       Run ./scripts/fetch-binaries.sh (needs network), or copy a known" >&2
  echo "       good bin/ directory in from another Odin install." >&2
  exit 1
fi

mkdir -p "$BIN"

# --- go2rtc -----------------------------------------------------------------
if [[ "$FORCE" == 0 ]] && verify "$BIN/go2rtc" "$GO2RTC_SHA256" "go2rtc"; then
  echo "go2rtc ${GO2RTC_VERSION} already present"
else
  echo "==> downloading go2rtc ${GO2RTC_VERSION} (arm64)"
  tmp="$(mktemp "$BIN/.go2rtc.XXXXXX")"
  trap 'rm -f "$tmp"' EXIT
  curl -fsSL --retry 3 --connect-timeout 15 -o "$tmp" "$GO2RTC_URL"
  got="$(sha_of "$tmp")"
  if [[ "$got" != "$GO2RTC_SHA256" ]]; then
    echo "error: go2rtc checksum mismatch" >&2
    echo "  expected $GO2RTC_SHA256" >&2
    echo "  got      $got" >&2
    exit 1
  fi
  chmod +x "$tmp"
  mv -f "$tmp" "$BIN/go2rtc"
  trap - EXIT
  echo "    installed $BIN/go2rtc"
fi

# --- ffmpeg + ffprobe (one static tarball) ----------------------------------
if [[ "$FORCE" == 0 ]] \
   && verify "$BIN/ffmpeg" "$FFMPEG_BIN_SHA256" "ffmpeg" \
   && verify "$BIN/ffprobe" "$FFPROBE_BIN_SHA256" "ffprobe"; then
  echo "ffmpeg ${FFMPEG_VERSION} already present"
else
  echo "==> downloading ffmpeg ${FFMPEG_VERSION} static (arm64, ~19 MB)"
  work="$(mktemp -d)"
  trap 'rm -rf "$work"' EXIT
  curl -fsSL --retry 3 --connect-timeout 15 -o "$work/ff.tar.xz" "$FFMPEG_URL"

  got="$(sha_of "$work/ff.tar.xz")"
  if [[ "$got" != "$FFMPEG_TARBALL_SHA256" ]]; then
    echo "error: ffmpeg tarball checksum mismatch" >&2
    echo "  expected $FFMPEG_TARBALL_SHA256" >&2
    echo "  got      $got" >&2
    exit 1
  fi

  tar -xf "$work/ff.tar.xz" -C "$work"
  src="$(find "$work" -maxdepth 2 -type d -name "ffmpeg-*-arm64-static" | head -1)"
  if [[ -z "$src" ]]; then
    echo "error: unexpected ffmpeg tarball layout" >&2
    exit 1
  fi

  # Verify the extracted executables too: the tarball hash protects the
  # download, but this catches a wrong archive that is internally consistent.
  verify "$src/ffmpeg"  "$FFMPEG_BIN_SHA256"  "ffmpeg"  || exit 1
  verify "$src/ffprobe" "$FFPROBE_BIN_SHA256" "ffprobe" || exit 1

  install -m 0755 "$src/ffmpeg"  "$BIN/ffmpeg"
  install -m 0755 "$src/ffprobe" "$BIN/ffprobe"
  trap - EXIT
  rm -rf "$work"
  echo "    installed $BIN/ffmpeg and $BIN/ffprobe"
fi

echo
echo "binaries ready in $BIN"
"$BIN/go2rtc" --version 2>/dev/null | head -1 | sed 's/^/  /'
"$BIN/ffmpeg" -version 2>/dev/null | head -1 | sed 's/^/  /'
