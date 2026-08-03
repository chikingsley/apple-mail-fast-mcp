#!/bin/bash

set -euo pipefail

readonly PASSWORD_FILE="${GOG_KEYRING_PASSWORD_FILE:-${HOME}/.config/gogcli/keyring-password}"

if [[ ! -f "${PASSWORD_FILE}" ]]; then
  echo "gog keyring password file is unavailable: ${PASSWORD_FILE}" >&2
  exit 1
fi

export GOG_KEYRING_PASSWORD
GOG_KEYRING_PASSWORD="$(<"${PASSWORD_FILE}")"
exec /opt/homebrew/bin/gog "$@"
