#!/usr/bin/env bash

set -euo pipefail

readonly LABEL="studio.peacockery.apple-mail-mcp-helper"
readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
readonly SOURCE_DIR="${PROJECT_DIR}/native/macos-helper"
readonly SOURCE_PLIST="${PROJECT_DIR}/deploy/macos/${LABEL}.plist"
readonly APP_NAME="Apple Mail MCP Helper.app"
readonly EXECUTABLE_NAME="AppleMailMCPHelper"
readonly APP_PATH="${HOME}/Applications/${APP_NAME}"
readonly EXECUTABLE_PATH="${APP_PATH}/Contents/MacOS/${EXECUTABLE_NAME}"
readonly TARGET_PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
readonly CONFIG_DIR="${HOME}/.config/apple-mail-fast-mcp"
readonly SOCKET_PATH="${CONFIG_DIR}/applescript-helper.sock"
readonly LOG_DIR="${HOME}/Library/Logs/apple-mail-fast-mcp"
readonly SIGNING_IDENTITY="${APPLE_MAIL_MCP_CODESIGN_IDENTITY:--}"
readonly BUILD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/apple-mail-mcp-helper.XXXXXX")"
readonly GUI_DOMAIN="gui/$(id -u)"

cleanup() {
  rm -rf -- "${BUILD_DIR}"
}
trap cleanup EXIT

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This installer must run on macOS." >&2
  exit 1
fi

command -v codesign >/dev/null
command -v install >/dev/null
command -v plutil >/dev/null
command -v xcrun >/dev/null

xcrun swiftc \
  -parse-as-library \
  -O \
  -framework AppKit \
  "${SOURCE_DIR}/${EXECUTABLE_NAME}.swift" \
  -o "${BUILD_DIR}/${EXECUTABLE_NAME}"

launchctl bootout "${GUI_DOMAIN}/${LABEL}" 2>/dev/null || true

install -d -m 755 \
  "${APP_PATH}/Contents" \
  "${APP_PATH}/Contents/MacOS" \
  "${HOME}/Library/LaunchAgents"
install -d -m 700 "${CONFIG_DIR}" "${LOG_DIR}"
install -m 644 "${SOURCE_DIR}/Info.plist" "${APP_PATH}/Contents/Info.plist"
install -m 755 "${BUILD_DIR}/${EXECUTABLE_NAME}" "${EXECUTABLE_PATH}"
plutil -lint "${APP_PATH}/Contents/Info.plist"

codesign \
  --force \
  --options runtime \
  --sign "${SIGNING_IDENTITY}" \
  --entitlements "${SOURCE_DIR}/${EXECUTABLE_NAME}.entitlements" \
  "${APP_PATH}"
codesign --verify --deep --strict --verbose=2 "${APP_PATH}"

if [[ ! -x "${EXECUTABLE_PATH}" || -L "${EXECUTABLE_PATH}" ]]; then
  echo "Installed helper is not a regular executable: ${EXECUTABLE_PATH}" >&2
  exit 1
fi

"${EXECUTABLE_PATH}" --self-check
install -m 600 "${SOURCE_PLIST}" "${TARGET_PLIST}"
/usr/libexec/PlistBuddy -c \
  "Set :ProgramArguments:0 ${EXECUTABLE_PATH}" "${TARGET_PLIST}"
/usr/libexec/PlistBuddy -c \
  "Set :ProgramArguments:2 ${SOCKET_PATH}" "${TARGET_PLIST}"
/usr/libexec/PlistBuddy -c \
  "Set :StandardOutPath ${LOG_DIR}/helper.out.log" "${TARGET_PLIST}"
/usr/libexec/PlistBuddy -c \
  "Set :StandardErrorPath ${LOG_DIR}/helper.err.log" "${TARGET_PLIST}"
plutil -lint "${TARGET_PLIST}"

for attempt in 1 2 3; do
  if launchctl bootstrap "${GUI_DOMAIN}" "${TARGET_PLIST}"; then
    break
  fi
  if [[ "${attempt}" -eq 3 ]]; then
    echo "Could not bootstrap ${LABEL} after ${attempt} attempts." >&2
    exit 1
  fi
  sleep 1
done
launchctl kickstart -k "${GUI_DOMAIN}/${LABEL}"

for _ in {1..50}; do
  if [[ -S "${SOCKET_PATH}" ]]; then
    break
  fi
  sleep 0.1
done
if [[ ! -S "${SOCKET_PATH}" || -L "${SOCKET_PATH}" ]]; then
  echo "AppleScript helper did not create a Unix socket: ${SOCKET_PATH}" >&2
  exit 1
fi
if [[ "$(stat -f '%u' "${SOCKET_PATH}")" != "$(id -u)" ]]; then
  echo "AppleScript helper socket is not owned by the current user." >&2
  exit 1
fi
if [[ "$(stat -f '%Lp' "${SOCKET_PATH}")" != "600" ]]; then
  echo "AppleScript helper socket must have mode 0600." >&2
  exit 1
fi

echo "Installed ${APP_PATH}"
if [[ "${SIGNING_IDENTITY}" == "-" ]]; then
  echo "Code signing: ad hoc"
else
  echo "Code signing identity: ${SIGNING_IDENTITY}"
fi
echo "AppleScript helper socket: ${SOCKET_PATH}"
