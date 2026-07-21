"""Strict reader for local credential files."""

from __future__ import annotations

import os
import stat
from pathlib import Path

DEFAULT_MAX_SECRET_BYTES = 16 * 1024


class SecretFileError(ValueError):
    """A configured secret file is missing, unsafe, or malformed."""


def read_secret_file(
    path_value: str,
    *,
    label: str,
    max_bytes: int = DEFAULT_MAX_SECRET_BYTES,
) -> str:
    """Read one owner-only UTF-8 secret without following symlinks."""
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        raise SecretFileError(f"{label} file path must be absolute: {path}")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        reason = exc.strerror or type(exc).__name__
        raise SecretFileError(f"Cannot open {label} file {path}: {reason}") from exc

    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise SecretFileError(f"{label} file must be a regular file: {path}")
        if file_stat.st_uid != os.getuid():
            raise SecretFileError(f"{label} file must be owned by the current user: {path}")

        mode = stat.S_IMODE(file_stat.st_mode)
        if mode not in {0o400, 0o600}:
            raise SecretFileError(
                f"{label} file must have mode 0400 or 0600, not {mode:04o}: {path}"
            )
        payload = os.read(descriptor, max_bytes + 1)
    finally:
        os.close(descriptor)

    if len(payload) > max_bytes:
        raise SecretFileError(f"{label} file exceeds the {max_bytes}-byte limit: {path}")
    try:
        secret = payload.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise SecretFileError(f"{label} file must contain UTF-8 text: {path}") from exc
    if not secret:
        raise SecretFileError(f"{label} file is empty: {path}")
    return secret
