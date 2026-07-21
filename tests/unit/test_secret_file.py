"""Tests for strict local credential-file handling."""

from __future__ import annotations

import os

import pytest

from apple_mail_fast_mcp.secret_file import SecretFileError, read_secret_file


def _secret_file(tmp_path, content: bytes = b"secret\n"):
    path = tmp_path / "secret"
    path.write_bytes(content)
    path.chmod(0o600)
    return path


def test_reads_owner_only_secret_and_strips_edges(tmp_path) -> None:
    path = _secret_file(tmp_path, b"  secret with spaces  \n")
    assert read_secret_file(str(path), label="test secret") == "secret with spaces"


def test_accepts_read_only_owner_mode(tmp_path) -> None:
    path = _secret_file(tmp_path)
    path.chmod(0o400)
    assert read_secret_file(str(path), label="test secret") == "secret"


def test_rejects_relative_path() -> None:
    with pytest.raises(SecretFileError, match="must be absolute"):
        read_secret_file("relative-secret", label="test secret")


def test_rejects_group_or_other_permissions(tmp_path) -> None:
    path = _secret_file(tmp_path)
    path.chmod(0o640)
    with pytest.raises(SecretFileError, match="0400 or 0600"):
        read_secret_file(str(path), label="test secret")


def test_rejects_wrong_owner(tmp_path, monkeypatch) -> None:
    path = _secret_file(tmp_path)
    monkeypatch.setattr(os, "getuid", lambda: path.stat().st_uid + 1)
    with pytest.raises(SecretFileError, match="owned by the current user"):
        read_secret_file(str(path), label="test secret")


def test_rejects_symlink(tmp_path) -> None:
    target = _secret_file(tmp_path)
    link = tmp_path / "secret-link"
    link.symlink_to(target)
    with pytest.raises(SecretFileError, match="Cannot open"):
        read_secret_file(str(link), label="test secret")


def test_rejects_directory(tmp_path) -> None:
    directory = tmp_path / "secret-directory"
    directory.mkdir(mode=0o700)
    with pytest.raises(SecretFileError, match="regular file"):
        read_secret_file(str(directory), label="test secret")


def test_rejects_empty_file(tmp_path) -> None:
    path = _secret_file(tmp_path, b" \n")
    with pytest.raises(SecretFileError, match="is empty"):
        read_secret_file(str(path), label="test secret")


def test_rejects_non_utf8_file(tmp_path) -> None:
    path = _secret_file(tmp_path, b"\xff")
    with pytest.raises(SecretFileError, match="UTF-8"):
        read_secret_file(str(path), label="test secret")


def test_rejects_oversized_file(tmp_path) -> None:
    path = _secret_file(tmp_path, b"abcde")
    with pytest.raises(SecretFileError, match="4-byte limit"):
        read_secret_file(str(path), label="test secret", max_bytes=4)
