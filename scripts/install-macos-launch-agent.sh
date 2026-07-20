#!/usr/bin/env bash

set -euo pipefail

readonly LABEL="studio.peacockery.apple-mail-mcp"
readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
readonly SOURCE_PLIST="${PROJECT_DIR}/deploy/macos/${LABEL}.plist"
readonly TARGET_PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
readonly LOG_DIR="${HOME}/Library/Logs/apple-mail-fast-mcp"
readonly GUI_DOMAIN="gui/$(id -u)"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This installer must run on macOS." >&2
  exit 1
fi

command -v uv >/dev/null
command -v launchctl >/dev/null

install -d -m 700 "${HOME}/Library/LaunchAgents" "${LOG_DIR}"

(
  cd "${PROJECT_DIR}"
  uv sync --locked --no-dev
)

install -m 600 "${SOURCE_PLIST}" "${TARGET_PLIST}"
/usr/libexec/PlistBuddy -c "Set :WorkingDirectory ${PROJECT_DIR}" "${TARGET_PLIST}"
/usr/libexec/PlistBuddy -c \
  "Set :StandardOutPath ${LOG_DIR}/service.out.log" "${TARGET_PLIST}"
/usr/libexec/PlistBuddy -c \
  "Set :StandardErrorPath ${LOG_DIR}/service.err.log" "${TARGET_PLIST}"
plutil -lint "${TARGET_PLIST}"

launchctl bootout "${GUI_DOMAIN}/${LABEL}" 2>/dev/null || true
launchctl bootstrap "${GUI_DOMAIN}" "${TARGET_PLIST}"
launchctl kickstart -k "${GUI_DOMAIN}/${LABEL}"
launchctl print "${GUI_DOMAIN}/${LABEL}"

echo "Apple Mail MCP is listening on http://127.0.0.1:8765/mcp"
