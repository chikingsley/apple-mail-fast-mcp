# /// script
# requires-python = ">=3.14"
# dependencies = ["fastmcp==4.0.3"]
# ///
"""Portable stdio adapter: credentials are read from disk, never shell startup."""

import json
import os
import stat
import sys
from pathlib import Path

from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server import create_proxy

root = Path.home() / ".config/peacockery-communications"
service = sys.argv[1]
if service not in {"apple-mail", "apple-calendar", "apple-contacts", "beeper"}:
    raise SystemExit("Choose apple-mail, apple-calendar, apple-contacts or beeper")
config = json.loads((root / "endpoints.json").read_text())
token_path = root / "service-token"
if token_path.is_symlink():
    raise SystemExit("Service token must not be a symlink")
with os.fdopen(os.open(token_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))) as stream:
    info = os.fstat(stream.fileno())
    if not stat.S_ISREG(info.st_mode) or (
        os.name != "nt" and (stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid())
    ):
        raise SystemExit("Service token must be an owner-only regular file")
    token = stream.read().strip()
if len(token) < 32:
    raise SystemExit("Service token is missing or invalid")
proxy = create_proxy(
    StreamableHttpTransport(config[service], auth=token),
    name=service,
    provider_error_strategy="raise",
)
proxy.run(show_banner=False)
