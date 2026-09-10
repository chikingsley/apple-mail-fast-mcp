"""Regressions for the September 10 lost quote, signature, and draft-identity incident."""

import json
from unittest.mock import MagicMock

import pytest

from apple_mail_fast_mcp.mail_connector import _MAILBOX_RESOLVER_HANDLERS
from apple_mail_fast_mcp.native_drafts import (
    _COMPOSE_LOCK,
    NativeDraftError,
    create_native_draft,
    creation_script,
)


def _arguments():
    return {
        "account": "CICA",
        "sender": "Chi <chi@example.com>",
        "seed": "reply",
        "seed_id": "240451",
        "seed_mailbox": "Archive/Vendors/Piping",
        "to": None,
        "cc": None,
        "bcc": None,
        "subject": None,
        "body": "Hi Scott,\n\nPlease confirm the product.",
        "reply_all": False,
        "attachment_block": "",
        "mailbox_handlers": _MAILBOX_RESOLVER_HANDLERS,
    }


def test_regression_native_preflight_failure_does_not_create_or_downgrade():
    """Regression: unavailable native composition must not create a degraded draft."""
    run = MagicMock(side_effect=RuntimeError("NATIVE_COMPOSITION_ACCESS_REQUIRED"))
    with pytest.raises(NativeDraftError, match="ACCESS_REQUIRED"):
        create_native_draft(run, **_arguments())
    assert run.call_count == 1
    assert run.call_args.args[0].startswith("COMPOSE\n")


def test_regression_busy_compose_never_waits_then_creates_after_timeout():
    """Regression: waiting beyond a cancelled request must not create another draft."""
    run = MagicMock()
    assert _COMPOSE_LOCK.acquire(blocking=False)
    try:
        with pytest.raises(NativeDraftError, match="busy"):
            create_native_draft(run, **_arguments())
    finally:
        _COMPOSE_LOCK.release()
    run.assert_not_called()


def test_regression_expired_pipeline_never_starts_next_mutation(monkeypatch):
    """Regression: a slow preflight must not start a draft after the total budget expires."""
    from apple_mail_fast_mcp import native_drafts

    clock = iter([0.0, 0.0, 31.0])
    monkeypatch.setattr(native_drafts, "monotonic", lambda: next(clock))
    run = MagicMock(return_value=json.dumps({"session_token": "unique-token"}))
    with pytest.raises(NativeDraftError, match="time budget exhausted") as raised:
        create_native_draft(run, **_arguments())
    assert run.call_count == 1
    assert run.call_args.args[0].startswith("COMPOSE\n")
    assert raised.value.composer_id == ""


def test_regression_reply_preserves_native_subject_and_never_sets_entire_content():
    """Regression: reply construction must retain the quoted conversation and subject."""
    arguments = _arguments()
    arguments.pop("body")
    script = creation_script(**arguments)
    assert 'resolveMailbox(targetAccount, "Archive/Vendors/Piping")' in script
    assert "whose id is 240451" in script
    assert "reply originalMessage opening window true" in script
    assert "set subject of d" not in script
    assert "set content of" not in script
    assert "content:" not in script


def test_regression_native_editor_bound_to_preflight_and_reports_unverified_mime():
    """Regression: existing same-subject composers must not become insertion targets."""
    run = MagicMock(
        side_effect=[
            json.dumps({"session_token": "unique-token"}),
            json.dumps(
                {
                    "composer_id": "7",
                    "subject": "Re: Fwd: Nichias",
                    "before_ids": ["42"],
                    "parent_message_id": "parent@example.com",
                }
            ),
            json.dumps({"inserted_text_verified": True, "previous_content_preserved": True}),
            json.dumps({"candidate_ids": ["43"]}),
        ]
    )
    result = create_native_draft(run, **_arguments())
    insertion = json.loads(run.call_args_list[2].args[0].split("\n", 1)[1])
    assert insertion["session_token"] == "unique-token"
    assert insertion["body"] == _arguments()["body"]
    assert result["draft_id"] == "43"
    assert result["verification_status"] == "saved_mime_inspection_required"
    assert "id of account of mailbox of savedDraft" in run.call_args_list[3].args[0]
    assert "extract address from sender of savedDraft" in run.call_args_list[3].args[0]


def test_regression_ambiguous_save_returns_composer_without_guessing_draft():
    """Regression: concurrent saves must not attribute another draft to this request."""
    run = MagicMock(
        side_effect=[
            json.dumps({"session_token": "unique-token"}),
            json.dumps(
                {
                    "composer_id": "7",
                    "subject": "Re: Nichias",
                    "before_ids": [],
                    "parent_message_id": "parent@example.com",
                }
            ),
            json.dumps({"inserted_text_verified": True}),
            json.dumps({"candidate_ids": ["43", "44"]}),
        ]
    )
    with pytest.raises(NativeDraftError, match="found 2") as raised:
        create_native_draft(run, **_arguments())
    assert raised.value.composer_id == "7"
    assert raised.value.draft_id == ""


@pytest.mark.asyncio
async def test_regression_public_create_uses_native_defaults(monkeypatch, tmp_path):
    """Regression: the public operation must preserve the user's Mail defaults."""
    from apple_mail_fast_mcp import server

    fake_mail = MagicMock()
    fake_mail.create_draft.return_value = {"draft_id": "123", "sent_message_id": ""}
    monkeypatch.setattr(server, "mail", fake_mail)
    monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path))
    result = await server.create_draft(
        reply_to="42", seed_mailbox="Archive/Vendors/Piping", from_account="CICA", body="Hi Scott"
    )
    assert result["success"]
    assert fake_mail.create_draft.call_args.kwargs["composition_mode"] == "mail_defaults"
    assert result["details"]["verification_status"] == "unverified"


@pytest.mark.asyncio
async def test_regression_failed_replacement_keeps_original(monkeypatch, tmp_path):
    """Regression: failed recreation must not delete the original draft."""
    from apple_mail_fast_mcp import server

    fake_mail = MagicMock()
    fake_mail.get_draft_state.return_value = {
        "subject": "test",
        "body": "old",
        "to": ["a@example.com"],
        "cc": [],
        "bcc": [],
        "attachment_names": [],
        "in_reply_to": "",
        "mime_content_type": "text/plain; charset=utf-8",
    }
    fake_mail.create_draft.side_effect = RuntimeError("provider unavailable")
    monkeypatch.setattr(server, "mail", fake_mail)
    monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path))
    result = await server.update_draft("123", body="new")
    assert not result["success"]
    fake_mail.delete_draft.assert_not_called()


@pytest.mark.asyncio
async def test_regression_rich_update_refuses_to_flatten_original(monkeypatch, tmp_path):
    """Regression: updates must not silently strip HTML, signatures, or quotation."""
    from apple_mail_fast_mcp import server

    fake_mail = MagicMock()
    fake_mail.get_draft_state.return_value = {
        "subject": "test",
        "body": "signature plus quote",
        "to": ["a@example.com"],
        "attachment_names": [],
        "in_reply_to": "",
        "mime_content_type": "multipart/alternative",
    }
    monkeypatch.setattr(server, "mail", fake_mail)
    monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path))
    result = await server.update_draft("123", body="new")
    assert result["error_type"] == "native_update_required"
    fake_mail.delete_draft.assert_not_called()
    fake_mail.create_draft.assert_not_called()
