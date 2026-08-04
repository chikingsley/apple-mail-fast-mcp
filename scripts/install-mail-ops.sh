#!/usr/bin/env bash

set -euo pipefail

readonly LABEL="studio.peacockery.apple-mail-ops"
readonly OLD_LABEL="studio.peacockery.apple-mail-junk-flag-cleaner"
readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
readonly SOURCE_PLIST="${PROJECT_DIR}/deploy/macos/${LABEL}.plist"
readonly SOURCE_CONFIG="${PROJECT_DIR}/deploy/macos/mail-ops.json"
readonly SOURCE_GOG_WRAPPER="${PROJECT_DIR}/deploy/macos/gog-file-keyring-wrapper.sh"
readonly TARGET_PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
readonly OLD_PLIST="${HOME}/Library/LaunchAgents/${OLD_LABEL}.plist"
readonly CONFIG_DIR="${HOME}/.config/apple-mail-fast-mcp"
readonly BIN_DIR="${CONFIG_DIR}/bin"
readonly TARGET_CONFIG="${CONFIG_DIR}/mail-ops.json"
readonly TARGET_GOG_WRAPPER="${BIN_DIR}/gog"
readonly STATE_DIR="${HOME}/.local/state/apple-mail-fast-mcp"
readonly TARGET_STATUS="${STATE_DIR}/ops-status.json"
readonly LOG_DIR="${HOME}/Library/Logs/apple-mail-fast-mcp"
readonly APPLESCRIPT_HELPER_SOCKET="${CONFIG_DIR}/applescript-helper.sock"
readonly GUI_DOMAIN="gui/$(id -u)"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This installer runs on macOS." >&2
  exit 1
fi

command -v uv >/dev/null
command -v launchctl >/dev/null
command -v agent-cli >/dev/null

if [[ ! -S "${APPLESCRIPT_HELPER_SOCKET}" || -L "${APPLESCRIPT_HELPER_SOCKET}" ]]; then
  echo "AppleScript helper socket is unavailable: ${APPLESCRIPT_HELPER_SOCKET}" >&2
  exit 1
fi

install -d -m 700 \
  "${HOME}/Library/LaunchAgents" \
  "${LOG_DIR}" \
  "${CONFIG_DIR}" \
  "${BIN_DIR}" \
  "${STATE_DIR}"
install -m 600 "${SOURCE_CONFIG}" "${TARGET_CONFIG}"
install -m 700 "${SOURCE_GOG_WRAPPER}" "${TARGET_GOG_WRAPPER}"
install -m 600 "${SOURCE_PLIST}" "${TARGET_PLIST}"
/usr/libexec/PlistBuddy -c "Set :WorkingDirectory ${PROJECT_DIR}" "${TARGET_PLIST}"
/usr/libexec/PlistBuddy -c \
  "Set :EnvironmentVariables:APPLE_MAIL_MCP_APPLESCRIPT_SOCKET ${APPLESCRIPT_HELPER_SOCKET}" \
  "${TARGET_PLIST}"
/usr/libexec/PlistBuddy -c \
  "Set :EnvironmentVariables:APPLE_MAIL_MCP_JUNK_CONFIG ${TARGET_CONFIG}" \
  "${TARGET_PLIST}"
/usr/libexec/PlistBuddy -c \
  "Set :EnvironmentVariables:APPLE_MAIL_MCP_OPS_STATUS ${TARGET_STATUS}" \
  "${TARGET_PLIST}"
/usr/libexec/PlistBuddy -c \
  "Set :EnvironmentVariables:PATH ${BIN_DIR}:${HOME}/.local/bin:${HOME}/.opencode/bin:${HOME}/.kimi-code/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
  "${TARGET_PLIST}"
/usr/libexec/PlistBuddy -c "Set :StandardOutPath ${LOG_DIR}/mail-ops.out.log" "${TARGET_PLIST}"
/usr/libexec/PlistBuddy -c "Set :StandardErrorPath ${LOG_DIR}/mail-ops.err.log" "${TARGET_PLIST}"
plutil -lint "${TARGET_PLIST}"

launchctl bootout "${GUI_DOMAIN}/${LABEL}" 2>/dev/null || true
launchctl bootstrap "${GUI_DOMAIN}" "${TARGET_PLIST}"
launchctl kickstart -k "${GUI_DOMAIN}/${LABEL}"
launchctl print "${GUI_DOMAIN}/${LABEL}"

launchctl bootout "${GUI_DOMAIN}/${OLD_LABEL}" 2>/dev/null || true
if [[ -f "${OLD_PLIST}" && ! -L "${OLD_PLIST}" ]]; then
  unlink "${OLD_PLIST}"
fi
