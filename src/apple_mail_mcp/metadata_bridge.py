"""Read-only SQL transport through the signed Full Disk Access helper."""

from __future__ import annotations

import json
import os
import socket
import stat
import struct
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path


def _receive(connection: socket.socket, count: int) -> bytes:
    result = bytearray()
    while len(result) < count:
        chunk = connection.recv(count - len(result))
        if not chunk:
            raise RuntimeError("Metadata helper returned an incomplete response")
        result.extend(chunk)
    return bytes(result)


def query_metadata(path: Path, sql: str, parameters: list[Any]) -> list[dict[str, Any]]:
    """Execute a bounded, read-only metadata query without Python needing FDA."""
    info = path.lstat()
    if (
        not stat.S_ISSOCK(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise RuntimeError("Metadata helper socket must be an owner-only socket")
    payload = (
        "SQL\n"
        + json.dumps(
            {
                "sql": sql,
                "parameters": [str(value) for value in parameters],
            }
        )
    ).encode()
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(30)
        connection.connect(str(path))
        connection.sendall(struct.pack("!I", len(payload)) + payload)
        status, count = struct.unpack("!BI", _receive(connection, 5))
        if count > 32 * 1024 * 1024:
            raise RuntimeError("Metadata helper response exceeds 32 MiB")
        response = _receive(connection, count).decode()
    if status:
        raise RuntimeError(response)
    rows = json.loads(response)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise RuntimeError("Metadata helper returned an invalid result")
    return rows
