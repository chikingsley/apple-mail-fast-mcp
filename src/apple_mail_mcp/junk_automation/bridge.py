"""Owner-only client for the signed macOS AppleScript helper."""

from __future__ import annotations

import base64
import json
import os
import socket
import stat
import struct
from pathlib import Path

CHECKOUT = Path(__file__).resolve().parents[3]
SOCKET_PATH = Path.home() / ".config/apple-mail-fast-mcp/applescript-helper.sock"
MAX_REQUEST = 4 * 1024 * 1024
MAX_RESPONSE = 32 * 1024 * 1024


class AppleScriptError(RuntimeError):
    """The signed helper rejected or failed an AppleScript request."""


class MailIndexError(RuntimeError):
    """The signed helper rejected or failed a read-only database query."""


def encoded(value: str) -> str:
    """Return an injection-safe AppleScript expression for arbitrary text."""
    payload = base64.b64encode(value.encode()).decode()
    return f'my unb64("{payload}")'


def preamble() -> str:
    """Return compact handlers for safe inputs and tabular outputs."""
    return """use framework "Foundation"
use scripting additions

on unb64(value)
  set dataValue to current application's NSData's alloc()'s initWithBase64EncodedString:value options:0
  return (current application's NSString's alloc()'s initWithData:dataValue encoding:4) as text
end unb64

on b64(value)
  set textValue to value as text
  set dataValue to (current application's NSString's stringWithString:textValue)'s dataUsingEncoding:4
  return (dataValue's base64EncodedStringWithOptions:0) as text
end b64

on emit(values)
  set encodedValues to {}
  repeat with value in values
    set end of encodedValues to my b64(value)
  end repeat
  set oldDelimiters to AppleScript's text item delimiters
  set AppleScript's text item delimiters to tab
  set output to encodedValues as text
  set AppleScript's text item delimiters to oldDelimiters
  return output & linefeed
end emit
"""


def _receive(connection: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = connection.recv(size - len(chunks))
        if not chunk:
            raise AppleScriptError("AppleScript helper closed an incomplete response")
        chunks.extend(chunk)
    return bytes(chunks)


def _request(source: str, *, timeout: float) -> str:
    path = Path(os.environ.get("APPLE_MAIL_SOCKET", SOCKET_PATH)).expanduser()
    info = path.lstat()
    if stat.S_ISSOCK(info.st_mode) is False or info.st_uid != os.getuid():
        raise AppleScriptError(f"Unsafe AppleScript helper socket: {path}")
    payload = source.encode()
    if len(payload) > MAX_REQUEST:
        raise AppleScriptError("AppleScript request exceeds 4 MiB")
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(timeout)
        connection.connect(str(path))
        connection.sendall(struct.pack("!I", len(payload)) + payload)
        status, length = struct.unpack("!BI", _receive(connection, 5))
        if length > MAX_RESPONSE:
            raise AppleScriptError("AppleScript response exceeds 32 MiB")
        message = _receive(connection, length).decode()
    if status:
        raise AppleScriptError(message)
    return message


def run(script: str, *, timeout: float = 60) -> str:
    """Execute AppleScript through the stable signed helper identity."""
    return _request(script, timeout=timeout)


def query(sql: str, parameters: list[str] | None = None) -> list[dict[str, object]]:
    """Run one read-only Envelope Index query through the signed helper."""
    request = json.dumps({"sql": sql, "parameters": parameters or []}, separators=(",", ":"))
    try:
        value = json.loads(_request(f"SQL\n{request}", timeout=10))
    except (AppleScriptError, json.JSONDecodeError) as exc:
        raise MailIndexError(str(exc)) from exc
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise MailIndexError("Mail index helper returned an invalid result")
    return value


def rows(output: str) -> list[list[str]]:
    """Decode helper rows emitted by the AppleScript preamble."""
    return [
        [base64.b64decode(value).decode() for value in line.split("\t")]
        for line in output.splitlines()
        if line
    ]
