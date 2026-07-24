#!/usr/bin/env bash

set -euo pipefail

readonly LABEL="studio.peacockery.apple-mail-mcp"
readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
readonly SOURCE_PLIST="${PROJECT_DIR}/deploy/macos/${LABEL}.plist"
readonly TARGET_PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
readonly LOG_DIR="${HOME}/Library/Logs/apple-mail-fast-mcp"
readonly CONFIG_DIR="${HOME}/.config/apple-mail-fast-mcp"
readonly BEARER_TOKEN_FILE="${CONFIG_DIR}/http-bearer-token"
readonly PEACOCKERY_IMAP_PASSWORD_FILE="${CONFIG_DIR}/imap-password-peacockery"
readonly PEACOCKERY_IMAP_PASSWORD_FILE_ENV="APPLE_MAIL_MCP_IMAP_PASSWORD_FILE_SIMON_PEACOCKERY_STUDIO"
readonly CHIBUZOR_IMAP_PASSWORD_FILE="${CONFIG_DIR}/imap-password-chibuzor-ejimofor-gmail-com"
readonly CHIBUZOR_IMAP_PASSWORD_FILE_ENV="APPLE_MAIL_MCP_IMAP_PASSWORD_FILE_CHIBUZOR_EJIMOFOR_GMAIL_COM"
readonly CHEEZ_IMAP_PASSWORD_FILE="${CONFIG_DIR}/imap-password-cheez2012-gmail-com"
readonly CHEEZ_IMAP_PASSWORD_FILE_ENV="APPLE_MAIL_MCP_IMAP_PASSWORD_FILE_CHEEZ2012_GMAIL_COM"
readonly MARICOPA_IMAP_PASSWORD_FILE="${CONFIG_DIR}/imap-password-chi2137976-maricopa-edu"
readonly MARICOPA_IMAP_PASSWORD_FILE_ENV="APPLE_MAIL_MCP_IMAP_PASSWORD_FILE_CHI2137976_MARICOPA_EDU"
readonly SIMON_GMAIL_IMAP_PASSWORD_FILE="${CONFIG_DIR}/imap-password-simon-ejimofor30-gmail-com"
readonly SIMON_GMAIL_IMAP_PASSWORD_FILE_ENV="APPLE_MAIL_MCP_IMAP_PASSWORD_FILE_SIMON_EJIMOFOR30_GMAIL_COM"
readonly BLACK_KING_RUSSIA_IMAP_PASSWORD_FILE="${CONFIG_DIR}/imap-password-blackkingrussia-gmail-com"
readonly BLACK_KING_RUSSIA_IMAP_PASSWORD_FILE_ENV="APPLE_MAIL_MCP_IMAP_PASSWORD_FILE_BLACKKINGRUSSIA_GMAIL_COM"
readonly HENRY_EBONGA_IMAP_PASSWORD_FILE="${CONFIG_DIR}/imap-password-henry-ebonga-gmail-com"
readonly HENRY_EBONGA_IMAP_PASSWORD_FILE_ENV="APPLE_MAIL_MCP_IMAP_PASSWORD_FILE_HENRY_EBONGA_GMAIL_COM"
readonly OUTLOOK_IMAP_PASSWORD_FILE="${CONFIG_DIR}/imap-password-chibuzor-ejimofor-outlook-com"
readonly OUTLOOK_IMAP_PASSWORD_FILE_ENV="APPLE_MAIL_MCP_IMAP_PASSWORD_FILE_CHIBUZOR_EJIMOFOR_OUTLOOK_COM"
readonly WEIGHANCHOR_IMAP_PASSWORD_FILE="${CONFIG_DIR}/imap-password-chi-weighanchor-com"
readonly WEIGHANCHOR_IMAP_PASSWORD_FILE_ENV="APPLE_MAIL_MCP_IMAP_PASSWORD_FILE_CHI_WEIGHANCHOR_COM"
readonly APPLESCRIPT_HELPER_APP="${HOME}/Applications/Apple Mail MCP Helper.app"
readonly APPLESCRIPT_HELPER_SOCKET="${CONFIG_DIR}/applescript-helper.sock"
readonly GUI_DOMAIN="gui/$(id -u)"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This installer must run on macOS." >&2
  exit 1
fi

command -v uv >/dev/null
command -v launchctl >/dev/null
command -v openssl >/dev/null

if [[ -z "${APPLE_MAIL_MCP_CODESIGN_IDENTITY:-}" ]]; then
  "${SCRIPT_DIR}/create-macos-signing-identity.sh"
fi

install -d -m 700 \
  "${HOME}/Library/LaunchAgents" \
  "${LOG_DIR}" \
  "${CONFIG_DIR}"

validate_secret_file() {
  local path="$1"
  local label="$2"
  local mode
  local owner

  if [[ ! -f "${path}" || -L "${path}" ]]; then
    echo "${label} must be a regular, non-symlink file: ${path}" >&2
    return 1
  fi
  mode="$(stat -f '%Lp' "${path}")"
  owner="$(stat -f '%u' "${path}")"
  if [[ "${owner}" != "$(id -u)" ]]; then
    echo "${label} must be owned by the current user: ${path}" >&2
    return 1
  fi
  if [[ "${mode}" != "400" && "${mode}" != "600" ]]; then
    echo "${label} must have mode 0400 or 0600, not ${mode}: ${path}" >&2
    return 1
  fi
}

if [[ -L "${BEARER_TOKEN_FILE}" ]]; then
  echo "HTTP bearer token must not be a symlink: ${BEARER_TOKEN_FILE}" >&2
  exit 1
fi
if [[ ! -e "${BEARER_TOKEN_FILE}" ]]; then
  umask 077
  openssl rand -hex 32 >"${BEARER_TOKEN_FILE}"
fi
validate_secret_file "${BEARER_TOKEN_FILE}" "HTTP bearer token"

"${SCRIPT_DIR}/install-macos-helper.sh"
if [[ ! -S "${APPLESCRIPT_HELPER_SOCKET}" || -L "${APPLESCRIPT_HELPER_SOCKET}" ]]; then
  echo "AppleScript helper socket is unavailable: ${APPLESCRIPT_HELPER_SOCKET}" >&2
  exit 1
fi

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
/usr/libexec/PlistBuddy -c \
  "Set :ProgramArguments:15 ${BEARER_TOKEN_FILE}" "${TARGET_PLIST}"
/usr/libexec/PlistBuddy -c \
  "Set :EnvironmentVariables:APPLE_MAIL_MCP_APPLESCRIPT_SOCKET ${APPLESCRIPT_HELPER_SOCKET}" \
  "${TARGET_PLIST}"
configure_password_file() {
  local path="$1"
  local env_name="$2"
  local label="$3"
  if [[ -e "${path}" ]]; then
    validate_secret_file "${path}" "${label}"
    /usr/libexec/PlistBuddy -c \
      "Set :EnvironmentVariables:${env_name} ${path}" \
      "${TARGET_PLIST}"
  else
    /usr/libexec/PlistBuddy -c \
      "Delete :EnvironmentVariables:${env_name}" \
      "${TARGET_PLIST}"
  fi
}

configure_password_file \
  "${PEACOCKERY_IMAP_PASSWORD_FILE}" \
  "${PEACOCKERY_IMAP_PASSWORD_FILE_ENV}" \
  "Peacockery IMAP password"
configure_password_file \
  "${CHIBUZOR_IMAP_PASSWORD_FILE}" \
  "${CHIBUZOR_IMAP_PASSWORD_FILE_ENV}" \
  "Chibuzor Gmail IMAP password"
configure_password_file \
  "${CHEEZ_IMAP_PASSWORD_FILE}" \
  "${CHEEZ_IMAP_PASSWORD_FILE_ENV}" \
  "Cheez Gmail IMAP password"
configure_password_file \
  "${MARICOPA_IMAP_PASSWORD_FILE}" \
  "${MARICOPA_IMAP_PASSWORD_FILE_ENV}" \
  "Maricopa Gmail IMAP password"
configure_password_file \
  "${SIMON_GMAIL_IMAP_PASSWORD_FILE}" \
  "${SIMON_GMAIL_IMAP_PASSWORD_FILE_ENV}" \
  "Simon Gmail IMAP password"
configure_password_file \
  "${BLACK_KING_RUSSIA_IMAP_PASSWORD_FILE}" \
  "${BLACK_KING_RUSSIA_IMAP_PASSWORD_FILE_ENV}" \
  "Black King Russia Gmail IMAP password"
configure_password_file \
  "${HENRY_EBONGA_IMAP_PASSWORD_FILE}" \
  "${HENRY_EBONGA_IMAP_PASSWORD_FILE_ENV}" \
  "Henry Ebonga Gmail IMAP password"
configure_password_file \
  "${OUTLOOK_IMAP_PASSWORD_FILE}" \
  "${OUTLOOK_IMAP_PASSWORD_FILE_ENV}" \
  "Chibuzor Outlook IMAP password"
configure_password_file \
  "${WEIGHANCHOR_IMAP_PASSWORD_FILE}" \
  "${WEIGHANCHOR_IMAP_PASSWORD_FILE_ENV}" \
  "WeighAnchor Outlook IMAP password"
plutil -lint "${TARGET_PLIST}"

launchctl bootout "${GUI_DOMAIN}/${LABEL}" 2>/dev/null || true
for attempt in 1 2 3; do
  if launchctl bootstrap "${GUI_DOMAIN}" "${TARGET_PLIST}"; then
    break
  fi
  if [[ "${attempt}" -eq 3 ]]; then
    echo "Could not bootstrap ${LABEL} after ${attempt} attempts." >&2
    exit 1
  fi
  echo "launchd is still releasing ${LABEL}; retrying in one second." >&2
  sleep 1
done
launchctl kickstart -k "${GUI_DOMAIN}/${LABEL}"
launchctl print "${GUI_DOMAIN}/${LABEL}"

echo "Apple Mail MCP is listening on http://127.0.0.1:8765/mcp"
echo "AppleScript helper: ${APPLESCRIPT_HELPER_APP}"
echo "Bearer token file: ${BEARER_TOKEN_FILE}"
if [[ -e "${PEACOCKERY_IMAP_PASSWORD_FILE}" ]]; then
  echo "Peacockery IMAP password file enabled."
else
  echo "Peacockery IMAP password file not present; AppleScript fallback remains enabled."
fi

MAIL_ACCOUNT_COUNT="$(
  "${APPLESCRIPT_HELPER_APP}/Contents/MacOS/AppleMailMCPHelper" \
    --request-mail-automation
)"
if [[ ! "${MAIL_ACCOUNT_COUNT}" =~ ^[0-9]+$ ]]; then
  echo "Mail Automation check returned an invalid account count." >&2
  exit 1
fi
echo "Mail Automation verified: ${MAIL_ACCOUNT_COUNT} accounts visible."
