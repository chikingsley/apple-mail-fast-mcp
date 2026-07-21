"""Tests for Streamable HTTP bearer and Origin protection."""

from __future__ import annotations

from typing import Any

import pytest

from apple_mail_fast_mcp.server import HTTPGuard, _load_http_bearer_token


async def _exercise_guard(headers: list[tuple[bytes, bytes]]):
    messages: list[dict[str, Any]] = []
    called = False

    async def downstream(scope, receive, send) -> None:
        nonlocal called
        called = True
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    guard = HTTPGuard(downstream, "t" * 32)
    await guard({"type": "http", "headers": headers}, receive, send)
    return called, messages


@pytest.mark.asyncio
async def test_missing_bearer_is_unauthorized() -> None:
    called, messages = await _exercise_guard([])
    assert called is False
    assert messages[0]["status"] == 401
    assert (b"www-authenticate", b"Bearer") in messages[0]["headers"]


@pytest.mark.asyncio
async def test_wrong_bearer_is_unauthorized() -> None:
    called, messages = await _exercise_guard([(b"authorization", b"Bearer wrong")])
    assert called is False
    assert messages[0]["status"] == 401


@pytest.mark.asyncio
async def test_origin_is_forbidden_even_with_valid_bearer() -> None:
    called, messages = await _exercise_guard(
        [
            (b"authorization", b"Bearer " + b"t" * 32),
            (b"origin", b"https://untrusted.example"),
        ]
    )
    assert called is False
    assert messages[0]["status"] == 403


@pytest.mark.asyncio
async def test_valid_bearer_reaches_downstream_app() -> None:
    called, messages = await _exercise_guard([(b"authorization", b"Bearer " + b"t" * 32)])
    assert called is True
    assert messages[0]["status"] == 204


def test_http_token_is_required(monkeypatch) -> None:
    monkeypatch.delenv("APPLE_MAIL_MCP_BEARER_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="HTTP transport requires"):
        _load_http_bearer_token(
            token_file=None,
            token_env="APPLE_MAIL_MCP_BEARER_TOKEN",
        )


def test_http_token_rejects_short_value(monkeypatch) -> None:
    monkeypatch.setenv("APPLE_MAIL_MCP_BEARER_TOKEN", "too-short")
    with pytest.raises(RuntimeError, match="at least 32"):
        _load_http_bearer_token(
            token_file=None,
            token_env="APPLE_MAIL_MCP_BEARER_TOKEN",
        )


def test_http_token_file_takes_precedence(tmp_path, monkeypatch) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("f" * 32, encoding="utf-8")
    token_file.chmod(0o600)
    monkeypatch.setenv("APPLE_MAIL_MCP_BEARER_TOKEN", "e" * 32)
    assert (
        _load_http_bearer_token(
            token_file=str(token_file),
            token_env="APPLE_MAIL_MCP_BEARER_TOKEN",
        )
        == "f" * 32
    )
