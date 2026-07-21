#!/usr/bin/env bash

set -euo pipefail

readonly IDENTITY_NAME="Apple Mail MCP Local Signing"
readonly SYSTEM_KEYCHAIN="/Library/Keychains/System.keychain"
readonly BUILD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/apple-mail-mcp-signing.XXXXXX")"
readonly CERTIFICATE_PATH="${BUILD_DIR}/signing.crt"

cleanup() {
  rm -rf -- "${BUILD_DIR}"
}
trap cleanup EXIT

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This script must run on macOS." >&2
  exit 1
fi

command -v openssl >/dev/null
command -v security >/dev/null
command -v sudo >/dev/null

identity_exists() {
  security find-identity -v -p codesigning "${SYSTEM_KEYCHAIN}" 2>/dev/null \
    | grep -Fq "\"${IDENTITY_NAME}\""
}

if identity_exists; then
  echo "Code-signing identity already exists: ${IDENTITY_NAME}"
  exit 0
fi

if security find-certificate \
  -c "${IDENTITY_NAME}" \
  -p "${SYSTEM_KEYCHAIN}" >"${CERTIFICATE_PATH}" 2>/dev/null; then
  echo "Found ${IDENTITY_NAME}; repairing its code-signing trust."
else
  readonly PRIVATE_KEY_PATH="${BUILD_DIR}/signing.key"
  readonly PKCS12_PATH="${BUILD_DIR}/signing.p12"
  readonly PKCS12_PASSWORD="$(openssl rand -hex 24)"

  umask 077
  openssl req -x509 -newkey rsa:3072 -sha256 -nodes \
    -keyout "${PRIVATE_KEY_PATH}" \
    -out "${CERTIFICATE_PATH}" \
    -days 3650 \
    -subj "/CN=${IDENTITY_NAME}/O=Peacockery Studio/OU=Local Development" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,digitalSignature,keyCertSign" \
    -addext "extendedKeyUsage=codeSigning" \
    >/dev/null 2>&1
  openssl pkcs12 -export \
    -inkey "${PRIVATE_KEY_PATH}" \
    -in "${CERTIFICATE_PATH}" \
    -out "${PKCS12_PATH}" \
    -name "${IDENTITY_NAME}" \
    -passout "pass:${PKCS12_PASSWORD}"

  sudo security import "${PKCS12_PATH}" \
    -k "${SYSTEM_KEYCHAIN}" \
    -P "${PKCS12_PASSWORD}" \
    -T /usr/bin/codesign \
    -T /usr/bin/security
fi

sudo security add-trusted-cert \
  -d \
  -r trustRoot \
  -p codeSign \
  -k "${SYSTEM_KEYCHAIN}" \
  "${CERTIFICATE_PATH}"

if ! identity_exists; then
  echo "The signing identity was installed but is not valid for code signing." >&2
  exit 1
fi

echo "Created code-signing identity: ${IDENTITY_NAME}"
echo "The helper installer will now select it automatically."
