#!/bin/sh
set -eu

authorization=$(cat "$HOME/.config/apple-mail-fast-mcp/mcp-authorization")
printf '{"Authorization":"%s"}\n' "$authorization"
